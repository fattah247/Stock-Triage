"""v3.4 coverage accounting and scoped catch-up helpers."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from ..config import Config
from ..db import WriterQueue, connect, configure_pragmas, run_migrations
from ..discovery import persist_document_candidates, persist_watermark
from ..download.pipeline import (
    AttachmentDownloader,
    DownloadAttemptPersist,
    DownloadCandidate,
    _attempt_from_exception,
    _existing_downloads_for_candidate,
    _persist_download,
    _persist_download_attempt,
    _resource_state_for_candidate,
    _resources_for_candidate,
    _should_fetch_resource,
    _upsert_download_resource,
)
from ..fiscal_year import resolve_fiscal_year_end_month
from ..intelligence import DERIVED_VERSION, run_intelligence
from ..intelligence.pipeline import _filing_deadline_templates, _stable_id
from ..logging_setup import get_logger
from ..parse import run_parse
from ..extract import run_extract
from ..rules import run_rules
from ..sources import DiscoveryWindow
from ..sources.adapters import IDXDisclosureAdapter, IDXFinancialReportAdapter

COVERAGE_VERSION = "v3_4-v1"
_SCOPE_CHOICES = {"all", "watchlist", "sector", "ticker-set"}
_ACTION_CHOICES = {"discover-missing", "download-missing", "process-missing", "rerun-blocked"}


@dataclass(frozen=True)
class CoverageMaterializeResult:
    selected_tickers: int
    row_count: int
    range_start: date
    range_end: date


def default_coverage_range(today: date | None = None) -> tuple[date, date]:
    anchor = today or date.today()
    return date(anchor.year - 2, 1, 1), date(anchor.year, 12, 31)


def materialize_coverage(
    cfg: Config,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    scope: str = "watchlist",
    sector: str | None = None,
    ticker: str | None = None,
    tickers: Iterable[str] | None = None,
) -> CoverageMaterializeResult:
    cfg.ensure_dirs()
    range_start, range_end = _normalized_range(date_from, date_to)
    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
        reader = connect(cfg.paths.db_path, read_only=True)
        try:
            configure_pragmas(reader, cfg)
            selected = _select_scope_tickers(
                reader,
                cfg,
                scope=scope,
                sector=sector,
                ticker=ticker,
                tickers=tickers,
            )
            rows = _build_coverage_rows(reader, cfg, selected, range_start=range_start, range_end=range_end)
        finally:
            reader.close()
        row_count = wq.submit(lambda conn, built=rows, selected=selected: _persist_coverage_rows(conn, rows=built, tickers=selected))
    return CoverageMaterializeResult(
        selected_tickers=len(selected),
        row_count=row_count,
        range_start=range_start,
        range_end=range_end,
    )


def load_coverage_status(
    cfg: Config,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    scope: str = "watchlist",
    sector: str | None = None,
    ticker: str | None = None,
    tickers: Iterable[str] | None = None,
    limit: int = 20,
    materialize: bool = True,
) -> dict[str, object]:
    cfg.ensure_dirs()
    if materialize:
        materialize_coverage(
            cfg,
            date_from=date_from,
            date_to=date_to,
            scope=scope,
            sector=sector,
            ticker=ticker,
            tickers=tickers,
        )
    conn = connect(cfg.paths.db_path, read_only=True)
    try:
        configure_pragmas(conn, cfg)
        return load_coverage_status_from_conn(
            conn,
            cfg,
            date_from=date_from,
            date_to=date_to,
            scope=scope,
            sector=sector,
            ticker=ticker,
            tickers=tickers,
            limit=limit,
        )
    finally:
        conn.close()


def load_coverage_status_from_conn(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    scope: str = "watchlist",
    sector: str | None = None,
    ticker: str | None = None,
    tickers: Iterable[str] | None = None,
    limit: int = 20,
) -> dict[str, object]:
    range_start, range_end = _normalized_range(date_from, date_to)
    selected = _select_scope_tickers(
        conn,
        cfg,
        scope=scope,
        sector=sector,
        ticker=ticker,
        tickers=tickers,
    )
    if not selected:
        return {
            "coverage_version": COVERAGE_VERSION,
            "scope": scope,
            "date_from": range_start.isoformat(),
            "date_to": range_end.isoformat(),
            "ticker_count": 0,
            "summary": {
                "expected_count": 0,
                "analyzed_count": 0,
                "downloaded_count": 0,
                "discovered_count": 0,
                "blocked_count": 0,
                "unknown_count": 0,
            },
            "gap_leaders": [],
            "undercovered_watchlist": [],
            "state_breakdown": [],
        }

    placeholders = ",".join("?" for _ in selected)
    params: list[object] = [
        *selected,
        COVERAGE_VERSION,
        range_start.isoformat(),
        range_end.isoformat(),
    ]
    watchlist = _effective_watchlist(cfg)
    rows = conn.execute(
        f"""
        SELECT fc.*, c.issuer_name
        FROM filing_coverage fc
        LEFT JOIN companies c ON c.ticker = fc.ticker
        WHERE fc.ticker IN ({placeholders})
          AND fc.coverage_version = ?
          AND date(fc.deadline_at) BETWEEN date(?) AND date(?)
        ORDER BY fc.deadline_at ASC, fc.ticker ASC
        """,
        tuple(params),
    ).fetchall()
    normalized_rows = [dict(row) for row in rows]
    state_counts = {
        "expected_count": len(normalized_rows),
        "analyzed_count": sum(1 for row in normalized_rows if row["actual_state"] == "analyzed"),
        "downloaded_count": sum(1 for row in normalized_rows if row["actual_state"] == "downloaded"),
        "discovered_count": sum(1 for row in normalized_rows if row["actual_state"] == "discovered"),
        "blocked_count": sum(1 for row in normalized_rows if row["actual_state"] == "blocked"),
        "unknown_count": sum(1 for row in normalized_rows if row["actual_state"] == "unknown"),
    }
    gap_rows = [row for row in normalized_rows if row["actual_state"] != "analyzed"]
    gap_rows.sort(
        key=lambda row: (
            0 if row["expected_state"] == "late" else 1 if row["expected_state"] == "due" else 2,
            0 if row["actual_state"] == "blocked" else 1 if row["actual_state"] == "unknown" else 2,
            -int(row["candidate_count"] or 0),
            row["deadline_at"],
            row["ticker"],
        )
    )
    gap_leaders = [_serialize_coverage_row(row) for row in gap_rows[:limit]]
    undercovered_watchlist = [
        _serialize_coverage_row(row)
        for row in gap_rows
        if str(row.get("ticker") or "").upper() in watchlist
    ][:limit]
    state_breakdown = [
        {"state": state, "count": count}
        for state, count in (
            ("analyzed", state_counts["analyzed_count"]),
            ("downloaded", state_counts["downloaded_count"]),
            ("discovered", state_counts["discovered_count"]),
            ("blocked", state_counts["blocked_count"]),
            ("unknown", state_counts["unknown_count"]),
        )
    ]
    return {
        "coverage_version": COVERAGE_VERSION,
        "scope": scope,
        "date_from": range_start.isoformat(),
        "date_to": range_end.isoformat(),
        "ticker_count": len(selected),
        "summary": state_counts,
        "gap_leaders": gap_leaders,
        "undercovered_watchlist": undercovered_watchlist,
        "state_breakdown": state_breakdown,
    }


def preview_coverage_action(
    cfg: Config,
    *,
    action: str,
    date_from: date | None = None,
    date_to: date | None = None,
    scope: str = "watchlist",
    sector: str | None = None,
    ticker: str | None = None,
    tickers: Iterable[str] | None = None,
    limit: int = 20,
) -> dict[str, object]:
    _validate_action(action)
    cfg.ensure_dirs()
    range_start, range_end = _normalized_range(date_from, date_to)
    materialize_coverage(
        cfg,
        date_from=range_start,
        date_to=range_end,
        scope=scope,
        sector=sector,
        ticker=ticker,
        tickers=tickers,
    )
    conn = connect(cfg.paths.db_path, read_only=True)
    try:
        configure_pragmas(conn, cfg)
        selected = _select_scope_tickers(conn, cfg, scope=scope, sector=sector, ticker=ticker, tickers=tickers)
        coverage_rows = _load_selected_coverage_rows(conn, selected, range_start=range_start, range_end=range_end)
        plan = _build_action_plan(conn, selected, coverage_rows, action=action, limit=limit)
        current_snapshot_count = _count_current_snapshots(conn, selected=selected) if selected else 0
    finally:
        conn.close()
    return {
        "action": action,
        "scope": scope,
        "scope_label": _scope_label(scope, sector=sector, ticker=ticker, tickers=tickers),
        "date_from": range_start.isoformat(),
        "date_to": range_end.isoformat(),
        "ticker_count": len(selected),
        "expected_filing_count": len(coverage_rows),
        "discovered_count": sum(1 for row in coverage_rows if row["candidate_count"] > 0),
        "downloaded_count": sum(1 for row in coverage_rows if row["downloaded_count"] > 0),
        "analyzed_count": sum(1 for row in coverage_rows if row["analyzed_count"] > 0),
        "blocked_count": sum(1 for row in coverage_rows if row["actual_state"] == "blocked"),
        "current_snapshot_count": current_snapshot_count,
        "dominant_blocked_reasons": _dominant_blocked_reasons(coverage_rows),
        "preview_generated_at": datetime.now(timezone.utc).isoformat(),
        "action_target_count": plan["target_count"],
        "action_targets": plan["targets"],
        "target_tickers": plan["target_tickers"],
    }


def run_coverage_action(
    cfg: Config,
    *,
    action: str,
    date_from: date | None = None,
    date_to: date | None = None,
    scope: str = "watchlist",
    sector: str | None = None,
    ticker: str | None = None,
    tickers: Iterable[str] | None = None,
    limit: int = 20,
) -> dict[str, object]:
    _validate_action(action)
    preview = preview_coverage_action(
        cfg,
        action=action,
        date_from=date_from,
        date_to=date_to,
        scope=scope,
        sector=sector,
        ticker=ticker,
        tickers=tickers,
        limit=limit,
    )
    log = get_logger("stock_triage.coverage")
    executed = 0
    target_tickers = list(preview["target_tickers"])
    if action == "discover-missing":
        executed = _run_discover_for_scope(
            cfg,
            target_tickers=target_tickers,
            date_from=date.fromisoformat(str(preview["date_from"])),
            date_to=date.fromisoformat(str(preview["date_to"])),
        )
    elif action == "download-missing":
        executed = _run_download_for_scope(cfg, candidate_ids=list(preview["action_targets"]))
    elif action == "process-missing":
        executed = _run_process_for_scope(cfg, document_ids=list(preview["action_targets"]), target_tickers=target_tickers)
    elif action == "rerun-blocked":
        executed = _run_rerun_blocked_for_scope(cfg, document_ids=list(preview["action_targets"]), target_tickers=target_tickers)
    log.info("coverage action executed", extra={"action": action, "executed": executed, "scope": scope})
    refreshed = load_coverage_status(
        cfg,
        date_from=date.fromisoformat(str(preview["date_from"])),
        date_to=date.fromisoformat(str(preview["date_to"])),
        scope=scope,
        sector=sector,
        ticker=ticker,
        tickers=tickers,
        limit=limit,
        materialize=True,
    )
    return {
        "action": action,
        "executed": executed,
        "preview": preview,
        "status": refreshed,
    }


def _normalized_range(date_from: date | None, date_to: date | None) -> tuple[date, date]:
    default_from, default_to = default_coverage_range()
    start = date_from or default_from
    end = date_to or default_to
    if start > end:
        raise ValueError("date_from must be on or before date_to")
    return start, end


def _validate_scope(scope: str) -> None:
    if scope not in _SCOPE_CHOICES:
        raise ValueError(f"unsupported scope: {scope}")


def _validate_action(action: str) -> None:
    if action not in _ACTION_CHOICES:
        raise ValueError(f"unsupported action: {action}")


def _effective_watchlist(cfg: Config) -> set[str]:
    sidecar = cfg.paths.data_dir / "watchlist.json"
    if sidecar.exists():
        try:
            raw = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = None
        if isinstance(raw, list):
            return {str(item).upper() for item in raw if str(item).strip()}
    return {ticker.upper() for ticker in cfg.alerts.watchlist}


def _select_scope_tickers(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    scope: str,
    sector: str | None,
    ticker: str | None,
    tickers: Iterable[str] | None,
) -> list[str]:
    _validate_scope(scope)
    if scope == "ticker-set":
        selected = {str(item).upper() for item in (tickers or []) if str(item).strip()}
        if ticker:
            selected.add(ticker.upper())
        if not selected:
            raise ValueError("scope=ticker-set requires --ticker or --tickers")
        return sorted(selected)
    if scope == "watchlist":
        return sorted(_effective_watchlist(cfg))
    if scope == "sector":
        if not sector:
            raise ValueError("scope=sector requires --sector")
        rows = conn.execute(
            "SELECT ticker FROM companies WHERE COALESCE(active_status, 'listed') != 'delisted' AND sector = ? ORDER BY ticker ASC",
            (sector,),
        ).fetchall()
        return [str(row["ticker"]) for row in rows]
    rows = conn.execute(
        "SELECT ticker FROM companies WHERE COALESCE(active_status, 'listed') != 'delisted' ORDER BY ticker ASC"
    ).fetchall()
    return [str(row["ticker"]) for row in rows]


def _build_coverage_rows(
    conn: sqlite3.Connection,
    cfg: Config,
    tickers: list[str],
    *,
    range_start: date,
    range_end: date,
) -> list[dict[str, object]]:
    now = datetime.now(timezone.utc).isoformat()
    rows: list[dict[str, object]] = []
    for ticker in tickers:
        company = conn.execute(
            "SELECT ticker, issuer_name, sector, fiscal_year_end_month FROM companies WHERE ticker = ?",
            (ticker,),
        ).fetchone()
        if company is None:
            continue
        fiscal_year_end_month, knowledge_source = resolve_fiscal_year_end_month(
            conn,
            cfg,
            ticker=ticker,
            stored_month=int(company["fiscal_year_end_month"] or 12),
        )
        for year in range(range_start.year - 1, range_end.year + 2):
            templates = _filing_deadline_templates(fiscal_year_end_month, around_year=year)
            for template in templates:
                deadline_at = date.fromisoformat(str(template["deadline_at"]))
                if deadline_at < range_start or deadline_at > range_end:
                    continue
                row = _build_coverage_row_for_template(
                    conn,
                    ticker=ticker,
                    template=template,
                    knowledge_source=knowledge_source,
                    updated_at=now,
                )
                rows.append(row)
    return rows


def _build_coverage_row_for_template(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    template: dict[str, Any],
    knowledge_source: str,
    updated_at: str,
) -> dict[str, object]:
    obligation_type = str(template["obligation_type"])
    document_type = "annual_report" if obligation_type == "annual_report" else "financial_statement"
    source_name = "idx_disclosures" if obligation_type == "annual_report" else "idx_financial_reports"
    period_end = str(template["period_end_date"])
    deadline_at = str(template["deadline_at"])
    window_start = str(template["window_start"])
    matched_documents = _matched_documents_for_expectation(
        conn,
        ticker=ticker,
        obligation_type=obligation_type,
        document_type=document_type,
        period_end=period_end,
        window_start=window_start,
        deadline_at=deadline_at,
    )
    matched_candidates = _matched_candidates_for_expectation(
        conn,
        ticker=ticker,
        source_name=source_name,
        obligation_type=obligation_type,
        window_start=window_start,
        deadline_at=deadline_at,
    )
    candidate_count = len(matched_candidates)
    downloaded_count = len([doc for doc in matched_documents if doc["process_state"] != "failed"])
    analyzed_count = len([doc for doc in matched_documents if doc["process_state"] == "analyzed"])
    blocked_reason = _blocked_reason_for(
        matched_documents=matched_documents,
        matched_candidates=matched_candidates,
    )
    actual_state = _actual_state_for(
        candidate_count=candidate_count,
        downloaded_count=downloaded_count,
        analyzed_count=analyzed_count,
        blocked_reason=blocked_reason,
    )
    expected_state = _expected_state_for(template)
    payload = {
        "matched_document_ids": [doc["document_id"] for doc in matched_documents],
        "matched_candidate_ids": [candidate["candidate_id"] for candidate in matched_candidates],
        "reason": template["reason"],
        "report_type": obligation_type,
    }
    return {
        "coverage_id": _stable_id("coverage", ticker, obligation_type, period_end, COVERAGE_VERSION),
        "ticker": ticker,
        "source_name": source_name,
        "obligation_type": obligation_type,
        "document_type": document_type,
        "fiscal_year": int(template["fiscal_year"]),
        "period_label": str(template["period_label"]),
        "period_end_date": period_end,
        "window_start": window_start,
        "deadline_at": deadline_at,
        "expected_state": expected_state,
        "actual_state": actual_state,
        "candidate_count": candidate_count,
        "downloaded_count": downloaded_count,
        "analyzed_count": analyzed_count,
        "blocked_reason": blocked_reason,
        "knowledge_source": knowledge_source,
        "payload_json": json.dumps(payload, sort_keys=True),
        "coverage_version": COVERAGE_VERSION,
        "updated_at": updated_at,
    }


def _expected_state_for(template: dict[str, Any]) -> str:
    today = date.today()
    window_start = date.fromisoformat(str(template["window_start"]))
    deadline_at = date.fromisoformat(str(template["deadline_at"]))
    if today < window_start:
        return "upcoming"
    if today <= deadline_at:
        return "due"
    return "late"


def _matched_documents_for_expectation(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    obligation_type: str,
    document_type: str,
    period_end: str,
    window_start: str,
    deadline_at: str,
) -> list[dict[str, object]]:
    if document_type == "financial_statement":
        rows = conn.execute(
            """
            SELECT DISTINCT d.document_id, d.process_state, d.process_error_code
            FROM documents d
            LEFT JOIN metric_items mi ON mi.document_id = d.document_id
            WHERE d.ticker = ?
              AND d.document_type = 'financial_statement'
              AND (
                    mi.period_end_date = ?
                 OR date(COALESCE(d.source_modified_at, d.retrieved_at, d.created_at)) BETWEEN date(?) AND date(?)
              )
            ORDER BY d.updated_at DESC, d.document_id ASC
            """,
            (ticker, period_end, window_start, deadline_at),
        ).fetchall()
        return [dict(row) for row in rows]
    rows = conn.execute(
        """
        SELECT d.document_id, d.process_state, d.process_error_code
        FROM documents d
        WHERE d.ticker = ?
          AND d.document_type = 'annual_report'
          AND date(COALESCE(d.source_modified_at, d.retrieved_at, d.created_at)) BETWEEN date(?) AND date(?)
        ORDER BY d.updated_at DESC, d.document_id ASC
        """,
        (ticker, window_start, deadline_at),
    ).fetchall()
    return [dict(row) for row in rows]


def _matched_candidates_for_expectation(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    source_name: str,
    obligation_type: str,
    window_start: str,
    deadline_at: str,
) -> list[dict[str, object]]:
    title_filter = None
    if obligation_type == "annual_report":
        title_filter = "%annual report%"
    rows = conn.execute(
        """
        SELECT dc.candidate_id, dc.source_record_id, dc.title, dr.status, dr.last_error_code
        FROM document_candidates dc
        LEFT JOIN download_resources dr ON dr.candidate_id = dc.candidate_id
        WHERE dc.ticker = ?
          AND dc.source_name = ?
          AND date(COALESCE(dc.upstream_published_at, dc.discovered_at)) BETWEEN date(?) AND date(?)
          AND (? IS NULL OR lower(COALESCE(dc.title, '')) LIKE ? OR lower(COALESCE(dc.raw_metadata, '')) LIKE '%laporan tahunan%')
        ORDER BY COALESCE(dc.upstream_published_at, dc.discovered_at) DESC, dc.candidate_id ASC
        """,
        (ticker, source_name, window_start, deadline_at, title_filter, title_filter),
    ).fetchall()
    return [dict(row) for row in rows]


def _blocked_reason_for(
    *,
    matched_documents: list[dict[str, object]],
    matched_candidates: list[dict[str, object]],
) -> str | None:
    failed_documents = [doc for doc in matched_documents if doc.get("process_state") == "failed"]
    if failed_documents:
        return str(failed_documents[0].get("process_error_code") or "document_failed")
    failed_candidates = [candidate for candidate in matched_candidates if str(candidate.get("status") or "").startswith("failed")]
    if failed_candidates:
        for candidate in failed_candidates:
            reason = str(candidate.get("last_error_code") or "").strip()
            if reason and reason.lower() != "error":
                return reason
        return str(failed_candidates[0].get("last_error_code") or "download_failed")
    return None


def _actual_state_for(
    *,
    candidate_count: int,
    downloaded_count: int,
    analyzed_count: int,
    blocked_reason: str | None,
) -> str:
    if analyzed_count > 0:
        return "analyzed"
    if blocked_reason:
        return "blocked"
    if downloaded_count > 0:
        return "downloaded"
    if candidate_count > 0:
        return "discovered"
    return "unknown"


def _persist_coverage_rows(conn: sqlite3.Connection, *, rows: list[dict[str, object]], tickers: list[str]) -> int:
    if tickers:
        placeholders = ",".join("?" for _ in tickers)
        conn.execute(
            f"DELETE FROM filing_coverage WHERE coverage_version = ? AND ticker IN ({placeholders})",
            (COVERAGE_VERSION, *tickers),
        )
    for row in rows:
        conn.execute(
            """
            INSERT INTO filing_coverage (
                coverage_id, ticker, source_name, obligation_type, document_type, fiscal_year,
                period_label, period_end_date, window_start, deadline_at, expected_state, actual_state,
                candidate_count, downloaded_count, analyzed_count, blocked_reason, knowledge_source,
                payload_json, coverage_version, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, obligation_type, period_end_date, coverage_version)
            DO UPDATE SET
                coverage_id = excluded.coverage_id,
                source_name = excluded.source_name,
                document_type = excluded.document_type,
                fiscal_year = excluded.fiscal_year,
                period_label = excluded.period_label,
                window_start = excluded.window_start,
                deadline_at = excluded.deadline_at,
                expected_state = excluded.expected_state,
                actual_state = excluded.actual_state,
                candidate_count = excluded.candidate_count,
                downloaded_count = excluded.downloaded_count,
                analyzed_count = excluded.analyzed_count,
                blocked_reason = excluded.blocked_reason,
                knowledge_source = excluded.knowledge_source,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (
                row["coverage_id"],
                row["ticker"],
                row["source_name"],
                row["obligation_type"],
                row["document_type"],
                row["fiscal_year"],
                row["period_label"],
                row["period_end_date"],
                row["window_start"],
                row["deadline_at"],
                row["expected_state"],
                row["actual_state"],
                row["candidate_count"],
                row["downloaded_count"],
                row["analyzed_count"],
                row["blocked_reason"],
                row["knowledge_source"],
                row["payload_json"],
                row["coverage_version"],
                row["updated_at"],
            ),
        )
    return len(rows)


def _load_selected_coverage_rows(
    conn: sqlite3.Connection,
    selected: list[str],
    *,
    range_start: date,
    range_end: date,
) -> list[dict[str, object]]:
    if not selected:
        return []
    placeholders = ",".join("?" for _ in selected)
    rows = conn.execute(
        f"""
        SELECT *
        FROM filing_coverage
        WHERE ticker IN ({placeholders})
          AND coverage_version = ?
          AND date(deadline_at) BETWEEN date(?) AND date(?)
        ORDER BY deadline_at ASC, ticker ASC
        """,
        (*selected, COVERAGE_VERSION, range_start.isoformat(), range_end.isoformat()),
    ).fetchall()
    return [dict(row) for row in rows]


def _build_action_plan(
    conn: sqlite3.Connection,
    selected: list[str],
    coverage_rows: list[dict[str, object]],
    *,
    action: str,
    limit: int,
) -> dict[str, object]:
    target_ids: list[str] = []
    target_tickers: set[str] = set()
    if action == "discover-missing":
        for row in coverage_rows:
            if row["actual_state"] == "unknown":
                target_tickers.add(str(row["ticker"]))
        target_ids = sorted(target_tickers)[:limit]
    elif action == "download-missing":
        for row in coverage_rows:
            if row["actual_state"] not in {"discovered", "blocked"}:
                continue
            payload = json.loads(row["payload_json"] or "{}")
            for candidate_id in payload.get("matched_candidate_ids") or []:
                target_ids.append(str(candidate_id))
                target_tickers.add(str(row["ticker"]))
        target_ids = list(dict.fromkeys(target_ids))[:limit]
    else:
        for row in coverage_rows:
            if action == "process-missing" and row["actual_state"] not in {"downloaded", "blocked"}:
                continue
            if action == "rerun-blocked" and row["actual_state"] != "blocked":
                continue
            payload = json.loads(row["payload_json"] or "{}")
            for document_id in payload.get("matched_document_ids") or []:
                target_ids.append(str(document_id))
                target_tickers.add(str(row["ticker"]))
        target_ids = list(dict.fromkeys(target_ids))[:limit]
    return {
        "target_count": len(target_ids),
        "targets": target_ids,
        "target_tickers": sorted(target_tickers or set(selected if action == "discover-missing" and target_ids else [])),
    }


def _count_current_snapshots(conn: sqlite3.Connection, *, selected: list[str]) -> int:
    if not selected:
        return 0
    placeholders = ",".join("?" for _ in selected)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM company_snapshots
        WHERE snapshot_kind = 'current'
          AND derived_version = ?
          AND ticker IN ({placeholders})
        """,
        (DERIVED_VERSION, *selected),
    ).fetchone()
    return int(row["count"] or 0)


def _dominant_blocked_reasons(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    counts: dict[str, int] = {}
    for row in rows:
        reason = str(row.get("blocked_reason") or "")
        if reason:
            counts[reason] = counts.get(reason, 0) + 1
    return [
        {"reason": reason, "count": count}
        for reason, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:5]
    ]


def _scope_label(scope: str, *, sector: str | None, ticker: str | None, tickers: Iterable[str] | None) -> str:
    if scope == "sector":
        return f"sector:{sector}"
    if scope == "ticker-set":
        values = sorted({*(tickers or []), *( [ticker] if ticker else [])})
        return f"tickers:{','.join(values)}"
    return scope


def _serialize_coverage_row(row: dict[str, object]) -> dict[str, object]:
    payload = json.loads(row.get("payload_json") or "{}")
    return {
        "ticker": row["ticker"],
        "issuer_name": row.get("issuer_name"),
        "obligation_type": row["obligation_type"],
        "period_label": row["period_label"],
        "period_end_date": row["period_end_date"],
        "deadline_at": row["deadline_at"],
        "expected_state": row["expected_state"],
        "actual_state": row["actual_state"],
        "candidate_count": row["candidate_count"],
        "downloaded_count": row["downloaded_count"],
        "analyzed_count": row["analyzed_count"],
        "blocked_reason": row["blocked_reason"],
        "knowledge_source": row["knowledge_source"],
        "matched_document_ids": payload.get("matched_document_ids") or [],
        "matched_candidate_ids": payload.get("matched_candidate_ids") or [],
    }


def _run_discover_for_scope(cfg: Config, *, target_tickers: list[str], date_from: date, date_to: date) -> int:
    targets = {ticker.upper() for ticker in target_tickers}
    if not targets:
        return 0
    inserted_total = 0
    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
        for adapter in (IDXDisclosureAdapter(), IDXFinancialReportAdapter()):
            window = DiscoveryWindow(start=date_from, end=date_to)
            candidates = [candidate for candidate in adapter.discover(window) if (candidate.ticker or "").upper() in targets]
            result = wq.submit(lambda conn, c=candidates: persist_document_candidates(conn, c))
            wq.submit(
                lambda conn, a=adapter, w=window, cnt=len(candidates): persist_watermark(
                    conn,
                    source_name=a.name,
                    source_class=a.source_class,
                    window=w,
                    raw_state={"candidate_count": cnt, "coverage_scope": "ticker-set"},
                )
            )
            inserted_total += result.inserted + result.updated
    return inserted_total


def _run_download_for_scope(cfg: Config, *, candidate_ids: list[str]) -> int:
    if not candidate_ids:
        return 0
    with WriterQueue(cfg) as wq, AttachmentDownloader(cfg) as downloader:
        wq.submit(run_migrations)
        reader = connect(cfg.paths.db_path, read_only=True)
        try:
            configure_pragmas(reader, cfg)
            rows = reader.execute(
                f"""
                SELECT candidate_id, source_name, source_record_id, ticker, title, upstream_published_at, attachment_url, raw_metadata
                FROM document_candidates
                WHERE candidate_id IN ({",".join("?" for _ in candidate_ids)})
                ORDER BY candidate_id ASC
                """,
                tuple(candidate_ids),
            ).fetchall()
            candidates = [
                DownloadCandidate(
                    candidate_id=row["candidate_id"],
                    source_name=row["source_name"],
                    source_record_id=row["source_record_id"],
                    ticker=row["ticker"],
                    title=row["title"],
                    attachment_url=row["attachment_url"],
                    attachment_filename=(json.loads(row["raw_metadata"] or "{}").get("filename") or json.loads(row["raw_metadata"] or "{}").get("file_name")),
                    upstream_published_at=row["upstream_published_at"],
                    raw_metadata=json.loads(row["raw_metadata"] or "{}"),
                )
                for row in rows
            ]
            result = 0
            for candidate in candidates:
                resources = _resources_for_candidate(candidate)
                existing_downloads = _existing_downloads_for_candidate(reader, candidate)
                for resource in resources:
                    state = _resource_state_for_candidate(reader, candidate, resource)
                    existing = existing_downloads.get(resource.source_file_id)
                    if not _should_fetch_resource(
                        resource=resource,
                        existing=existing,
                        state=state,
                        include_existing=False,
                        retry_failed=True,
                        include_quarantined=True,
                        now=datetime.now(timezone.utc),
                    ):
                        continue
                    started_at = datetime.now(timezone.utc)
                    try:
                        fetched = downloader.fetch(candidate, resource, existing=existing)
                    except Exception as exc:  # noqa: BLE001
                        finished_at = datetime.now(timezone.utc)
                        failure = _attempt_from_exception(exc, started_at=started_at, finished_at=finished_at)
                        wq.submit(lambda conn, c=candidate, r=resource, a=failure: _persist_download_attempt(conn, candidate=c, resource=r, attempt=a))
                        wq.submit(lambda conn, c=candidate, r=resource, a=failure: _upsert_download_resource(conn, cfg=cfg, candidate=c, resource=r, status=a.status, document_id=None, attempt=a))
                        continue
                    if fetched is None:
                        continue
                    persist = wq.submit(lambda conn, c=candidate, r=resource, f=fetched: _persist_download(conn, candidate=c, resource=r, fetched=f))
                    finished_at = datetime.now(timezone.utc)
                    success_attempt = DownloadAttemptPersist(
                        status="downloaded" if persist.inserted else "duplicate",
                        started_at=started_at,
                        finished_at=finished_at,
                        http_status=200,
                        document_id=persist.document_id,
                    )
                    wq.submit(lambda conn, c=candidate, r=resource, a=success_attempt: _persist_download_attempt(conn, candidate=c, resource=r, attempt=a))
                    wq.submit(lambda conn, c=candidate, r=resource, d=persist.document_id, a=success_attempt: _upsert_download_resource(conn, cfg=cfg, candidate=c, resource=r, status="downloaded", document_id=d, attempt=a))
                    result += 1
        finally:
            reader.close()
    return result


def _run_process_for_scope(cfg: Config, *, document_ids: list[str], target_tickers: list[str]) -> int:
    if not document_ids and not target_tickers:
        return 0
    executed = 0
    for document_id in document_ids:
        parse_result = run_parse(cfg, document_id=document_id, limit=1, include_parsed=True)
        extract_result = run_extract(cfg, document_id=document_id, limit=1, include_extracted=True)
        rule_result = run_rules(cfg, document_id=document_id, limit=1, include_analyzed=True)
        executed += parse_result.selected + extract_result.selected + rule_result.selected
    for ticker in target_tickers:
        intelligence_result = run_intelligence(cfg, ticker=ticker, limit=1)
        executed += intelligence_result.selected_tickers
    return executed


def _run_rerun_blocked_for_scope(cfg: Config, *, document_ids: list[str], target_tickers: list[str]) -> int:
    if not document_ids and not target_tickers:
        return 0
    executed = 0
    for document_id in document_ids:
        parse_result = run_parse(cfg, document_id=document_id, limit=1, include_parsed=True, retry_failed_only=True)
        extract_result = run_extract(cfg, document_id=document_id, limit=1, include_extracted=True, retry_failed_only=True)
        rule_result = run_rules(cfg, document_id=document_id, limit=1, include_analyzed=True)
        executed += parse_result.selected + extract_result.selected + rule_result.selected
    for ticker in target_tickers:
        intelligence_result = run_intelligence(cfg, ticker=ticker, limit=1)
        executed += intelligence_result.selected_tickers
    return executed
