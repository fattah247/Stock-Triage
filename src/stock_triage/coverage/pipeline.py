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
from ..recon import PlaywrightSession
from ..extract import run_extract
from ..rules import run_rules
from ..sources import DiscoveryWindow
from ..sources.adapters import IDXDisclosureAdapter, IDXFinancialReportAdapter
from ..sources.idx_artifacts import parse_financial_report_json

COVERAGE_VERSION = "v3_4-v1"
_SCOPE_CHOICES = {"all", "watchlist", "sector", "ticker-set"}
_ACTION_CHOICES = {"discover-missing", "download-missing", "process-missing", "rerun-blocked"}
BLOCK_CLASS_ORCHESTRATION_ERROR = "orchestration_error"
BLOCK_CLASS_UPSTREAM_403_TRANSIENT = "upstream_403_transient"
BLOCK_CLASS_UPSTREAM_403_PERSISTENT = "upstream_403_persistent"
BLOCK_CLASS_ACCESS_CONSTRAINT = "access_constraint"
_BLOCK_CLASS_RANK = {
    BLOCK_CLASS_ORCHESTRATION_ERROR: 0,
    BLOCK_CLASS_UPSTREAM_403_TRANSIENT: 1,
    BLOCK_CLASS_UPSTREAM_403_PERSISTENT: 2,
    BLOCK_CLASS_ACCESS_CONSTRAINT: 3,
}


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
    source_name: str | None = None,
    document_type: str | None = None,
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
            source_name=source_name,
            document_type=document_type,
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
    source_name: str | None = None,
    document_type: str | None = None,
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
    normalized_rows = [
        row
        for row in normalized_rows
        if _coverage_row_matches_filters(row, source_name=source_name, document_type=document_type)
    ]
    normalized_rows = _enrich_coverage_rows_with_access_state(conn, normalized_rows)
    state_counts = {
        "expected_count": len(normalized_rows),
        "analyzed_count": sum(1 for row in normalized_rows if row["actual_state"] == "analyzed"),
        "downloaded_count": sum(1 for row in normalized_rows if row["actual_state"] == "downloaded"),
        "discovered_count": sum(1 for row in normalized_rows if row["actual_state"] == "discovered"),
        "blocked_count": sum(1 for row in normalized_rows if row["actual_state"] == "blocked"),
        "unknown_count": sum(1 for row in normalized_rows if row["actual_state"] == "unknown"),
        "blocked_fixable_count": sum(1 for row in normalized_rows if row.get("blocked_bucket") == "fixable"),
        "blocked_retryable_count": sum(1 for row in normalized_rows if row.get("blocked_bucket") == "retryable"),
        "blocked_access_constrained_count": sum(1 for row in normalized_rows if row.get("blocked_bucket") == "access_constrained"),
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
        "source_name": source_name,
        "document_type": document_type,
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
    source_name: str | None = None,
    document_type: str | None = None,
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
        filtered_rows = [
            row
            for row in coverage_rows
            if _coverage_row_matches_filters(row, source_name=source_name, document_type=document_type)
        ]
        filtered_rows = _enrich_coverage_rows_with_access_state(conn, filtered_rows)
        plan = _build_action_plan(conn, selected, filtered_rows, action=action, limit=limit)
        current_snapshot_count = _count_current_snapshots(conn, selected=selected) if selected else 0
    finally:
        conn.close()
    return {
        "action": action,
        "scope": scope,
        "scope_label": _scope_label(
            scope,
            sector=sector,
            ticker=ticker,
            tickers=tickers,
            source_name=source_name,
            document_type=document_type,
        ),
        "source_name": source_name,
        "document_type": document_type,
        "date_from": range_start.isoformat(),
        "date_to": range_end.isoformat(),
        "ticker_count": len(selected),
        "expected_filing_count": len(filtered_rows),
        "discovered_count": sum(1 for row in filtered_rows if row["candidate_count"] > 0),
        "downloaded_count": sum(1 for row in filtered_rows if row["downloaded_count"] > 0),
        "analyzed_count": sum(1 for row in filtered_rows if row["analyzed_count"] > 0),
        "blocked_count": sum(1 for row in filtered_rows if row["actual_state"] == "blocked"),
        "blocked_fixable_count": sum(1 for row in filtered_rows if row.get("blocked_bucket") == "fixable"),
        "blocked_retryable_count": sum(1 for row in filtered_rows if row.get("blocked_bucket") == "retryable"),
        "blocked_access_constrained_count": sum(1 for row in filtered_rows if row.get("blocked_bucket") == "access_constrained"),
        "current_snapshot_count": current_snapshot_count,
        "dominant_blocked_reasons": _dominant_blocked_reasons(filtered_rows),
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
    source_name: str | None = None,
    document_type: str | None = None,
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
        source_name=source_name,
        document_type=document_type,
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
            source_name=source_name,
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
        source_name=source_name,
        document_type=document_type,
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
    raw_blocked_reason = _raw_blocked_reason_for(
        matched_documents=matched_documents,
        matched_candidates=matched_candidates,
    )
    blocked_reason = _blocked_reason_for(raw_blocked_reason)
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
        "raw_blocked_reason": raw_blocked_reason,
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


def _raw_blocked_reason_for(
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


def _blocked_reason_for(raw_blocked_reason: str | None) -> str | None:
    if not raw_blocked_reason:
        return None
    normalized = str(raw_blocked_reason).strip().lower()
    if normalized in {"http_403", "browser_http_403", "cloudflare_403"}:
        return BLOCK_CLASS_UPSTREAM_403_TRANSIENT
    return BLOCK_CLASS_ORCHESTRATION_ERROR


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
    _persist_access_state_rows(conn, rows=rows)
    return len(rows)


def _persist_access_state_rows(conn: sqlite3.Connection, *, rows: list[dict[str, object]]) -> None:
    now = datetime.now(timezone.utc)
    for row in rows:
        blocked_reason = str(row.get("blocked_reason") or "").strip()
        payload = json.loads(str(row.get("payload_json") or "{}"))
        candidate_ids = [str(item) for item in payload.get("matched_candidate_ids") or [] if item]
        if not blocked_reason or not candidate_ids:
            continue
        placeholders = ",".join("?" for _ in candidate_ids)
        candidate_rows = conn.execute(
            f"""
            SELECT
                dc.candidate_id,
                dc.source_name,
                dc.ticker,
                dr.resource_id,
                dr.last_http_status,
                dr.last_error_code
            FROM document_candidates dc
            LEFT JOIN download_resources dr ON dr.candidate_id = dc.candidate_id
            WHERE dc.candidate_id IN ({placeholders})
            ORDER BY dr.last_attempted_at DESC, dr.resource_id DESC
            """,
            tuple(candidate_ids),
        ).fetchall()
        seen_candidates: set[str] = set()
        for candidate in candidate_rows:
            candidate_id = str(candidate["candidate_id"])
            if candidate_id in seen_candidates:
                continue
            seen_candidates.add(candidate_id)
            artifact_id = candidate_id
            endpoint_family = _endpoint_family_for(
                source_name=str(candidate["source_name"] or row.get("source_name") or ""),
                block_class=blocked_reason,
            )
            retry_eligible_until = None
            if blocked_reason == BLOCK_CLASS_UPSTREAM_403_TRANSIENT:
                retry_eligible_until = (now + timedelta(hours=24)).isoformat()
            existing = conn.execute(
                """
                SELECT first_blocked_at, retry_count, needs_alternative_source_check, block_class
                FROM artifact_access_state
                WHERE artifact_id = ?
                """,
                (artifact_id,),
            ).fetchone()
            first_blocked_at = str(existing["first_blocked_at"]) if existing and existing["first_blocked_at"] else now.isoformat()
            retry_count = int(existing["retry_count"] or 0) + 1 if existing else 1
            block_class = _resolved_access_block_class(
                blocked_reason=blocked_reason,
                retry_count=retry_count,
                expected_state=str(row.get("expected_state") or ""),
                deadline_at=str(row.get("deadline_at") or ""),
                previous_block_class=str(existing["block_class"] or "") if existing and existing["block_class"] else None,
            )
            retry_eligible_until = _retry_eligible_until_for_block_class(now=now, block_class=block_class)
            needs_alternative_source_check = 1 if block_class in {BLOCK_CLASS_UPSTREAM_403_PERSISTENT, BLOCK_CLASS_ACCESS_CONSTRAINT} else 0
            conn.execute(
                """
                INSERT INTO artifact_access_state (
                    artifact_id, candidate_id, source_name, ticker, endpoint_family, document_type,
                    first_blocked_at, last_blocked_at, last_success_at, retry_count, block_class,
                    retry_eligible_until, needs_alternative_source_check, last_http_status, last_error_code,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(artifact_id)
                DO UPDATE SET
                    candidate_id = excluded.candidate_id,
                    source_name = excluded.source_name,
                    ticker = excluded.ticker,
                    endpoint_family = excluded.endpoint_family,
                    document_type = excluded.document_type,
                    last_blocked_at = excluded.last_blocked_at,
                    retry_count = excluded.retry_count,
                    block_class = excluded.block_class,
                    retry_eligible_until = excluded.retry_eligible_until,
                    needs_alternative_source_check = excluded.needs_alternative_source_check,
                    last_http_status = excluded.last_http_status,
                    last_error_code = excluded.last_error_code,
                    updated_at = excluded.updated_at
                """,
                (
                    artifact_id,
                    candidate_id,
                    str(candidate["source_name"] or row.get("source_name") or ""),
                    str(candidate["ticker"] or row.get("ticker") or ""),
                    endpoint_family,
                    str(row.get("document_type") or ""),
                    first_blocked_at,
                    now.isoformat(),
                    None,
                    retry_count,
                    block_class,
                    retry_eligible_until,
                    needs_alternative_source_check,
                    candidate["last_http_status"],
                    candidate["last_error_code"],
                    first_blocked_at,
                    now.isoformat(),
                ),
            )


def _endpoint_family_for(*, source_name: str, block_class: str) -> str:
    normalized_source = source_name.strip().lower()
    if normalized_source == "idx_financial_reports":
        return "idx_financial_reports_artifact" if block_class.startswith("upstream_403") else "idx_financial_reports_pipeline"
    if normalized_source == "idx_disclosures":
        return "idx_disclosures_attachment" if block_class.startswith("upstream_403") else "idx_disclosures_pipeline"
    return f"{normalized_source or 'unknown'}_artifact"


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


def _enrich_coverage_rows_with_access_state(
    conn: sqlite3.Connection,
    rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    candidate_ids: list[str] = []
    for row in rows:
        payload = json.loads(str(row.get("payload_json") or "{}"))
        candidate_ids.extend(str(item) for item in payload.get("matched_candidate_ids") or [] if item)
    access_by_candidate = _load_access_state_by_candidate_ids(conn, candidate_ids)
    enriched: list[dict[str, object]] = []
    for row in rows:
        payload = json.loads(str(row.get("payload_json") or "{}"))
        matched_candidate_ids = [str(item) for item in payload.get("matched_candidate_ids") or [] if item]
        access_states = [access_by_candidate[candidate_id] for candidate_id in matched_candidate_ids if candidate_id in access_by_candidate]
        effective_block_class = _effective_block_class_for_row(
            blocked_reason=str(row.get("blocked_reason") or "") or None,
            access_states=access_states,
        )
        blocked_bucket = _blocked_bucket_for_class(effective_block_class) if row.get("actual_state") == "blocked" else None
        retry_eligible_until = _latest_retry_eligible_until(access_states)
        needs_alternative_source_check = any(bool(state.get("needs_alternative_source_check")) for state in access_states)
        enriched.append(
            {
                **row,
                "effective_block_class": effective_block_class,
                "blocked_bucket": blocked_bucket,
                "retry_eligible_until": retry_eligible_until,
                "needs_alternative_source_check": needs_alternative_source_check,
            }
        )
    return enriched


def _load_access_state_by_candidate_ids(
    conn: sqlite3.Connection,
    candidate_ids: list[str],
) -> dict[str, dict[str, object]]:
    if not candidate_ids:
        return {}
    deduped_ids = list(dict.fromkeys(candidate_ids))
    rows = conn.execute(
        f"""
        SELECT candidate_id, block_class, retry_eligible_until, needs_alternative_source_check
        FROM artifact_access_state
        WHERE candidate_id IN ({",".join("?" for _ in deduped_ids)})
        """,
        tuple(deduped_ids),
    ).fetchall()
    return {str(row["candidate_id"]): dict(row) for row in rows}


def _effective_block_class_for_row(
    *,
    blocked_reason: str | None,
    access_states: list[dict[str, object]],
) -> str | None:
    classes = [str(state.get("block_class") or "") for state in access_states if state.get("block_class")]
    if blocked_reason:
        classes.append(blocked_reason)
    if not classes:
        return None
    return max(classes, key=lambda value: _BLOCK_CLASS_RANK.get(value, -1))


def _blocked_bucket_for_class(block_class: str | None) -> str | None:
    if block_class == BLOCK_CLASS_ORCHESTRATION_ERROR:
        return "fixable"
    if block_class in {BLOCK_CLASS_UPSTREAM_403_TRANSIENT, BLOCK_CLASS_UPSTREAM_403_PERSISTENT}:
        return "retryable"
    if block_class == BLOCK_CLASS_ACCESS_CONSTRAINT:
        return "access_constrained"
    return None


def _latest_retry_eligible_until(access_states: list[dict[str, object]]) -> str | None:
    values = [str(state.get("retry_eligible_until") or "") for state in access_states if state.get("retry_eligible_until")]
    return max(values) if values else None


def _resolved_access_block_class(
    *,
    blocked_reason: str,
    retry_count: int,
    expected_state: str,
    deadline_at: str,
    previous_block_class: str | None,
) -> str:
    if blocked_reason != BLOCK_CLASS_UPSTREAM_403_TRANSIENT:
        return blocked_reason
    if previous_block_class == BLOCK_CLASS_ACCESS_CONSTRAINT:
        return BLOCK_CLASS_ACCESS_CONSTRAINT
    if retry_count >= 3 and _is_past_deadline_window(expected_state=expected_state, deadline_at=deadline_at):
        return BLOCK_CLASS_ACCESS_CONSTRAINT
    if retry_count >= 2:
        return BLOCK_CLASS_UPSTREAM_403_PERSISTENT
    return BLOCK_CLASS_UPSTREAM_403_TRANSIENT


def _retry_eligible_until_for_block_class(*, now: datetime, block_class: str) -> str | None:
    if block_class == BLOCK_CLASS_UPSTREAM_403_TRANSIENT:
        return (now + timedelta(hours=24)).isoformat()
    if block_class == BLOCK_CLASS_UPSTREAM_403_PERSISTENT:
        return (now + timedelta(hours=72)).isoformat()
    return None


def _is_past_deadline_window(*, expected_state: str, deadline_at: str) -> bool:
    if expected_state == "late":
        return True
    if not deadline_at:
        return False
    try:
        return date.fromisoformat(deadline_at) < date.today()
    except ValueError:
        return False


def _retry_ready(row: dict[str, object]) -> bool:
    retry_eligible_until = str(row.get("retry_eligible_until") or "").strip()
    if not retry_eligible_until:
        return True
    try:
        return datetime.fromisoformat(retry_eligible_until) <= datetime.now(timezone.utc)
    except ValueError:
        return True


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
        candidate_ids: list[str] = []
        for row in coverage_rows:
            if row["actual_state"] == "blocked" and row.get("blocked_bucket") == "access_constrained":
                continue
            if row["actual_state"] not in {"discovered", "blocked"}:
                continue
            payload = json.loads(row["payload_json"] or "{}")
            for candidate_id in payload.get("matched_candidate_ids") or []:
                candidate_ids.append(str(candidate_id))
                target_tickers.add(str(row["ticker"]))
        target_ids = _prioritize_candidate_ids(conn, candidate_ids)[:limit]
    else:
        for row in coverage_rows:
            if action == "process-missing" and row["actual_state"] not in {"downloaded", "blocked"}:
                continue
            if action == "rerun-blocked":
                if row["actual_state"] != "blocked":
                    continue
                if row.get("blocked_bucket") == "access_constrained":
                    continue
                if row.get("blocked_bucket") == "retryable" and not _retry_ready(row):
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
        reason = str(row.get("effective_block_class") or row.get("blocked_reason") or "")
        if reason:
            counts[reason] = counts.get(reason, 0) + 1
    return [
        {"reason": reason, "count": count}
        for reason, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:5]
    ]


def _scope_label(
    scope: str,
    *,
    sector: str | None,
    ticker: str | None,
    tickers: Iterable[str] | None,
    source_name: str | None,
    document_type: str | None,
) -> str:
    suffix_parts: list[str] = []
    if source_name:
        suffix_parts.append(f"source:{source_name}")
    if document_type:
        suffix_parts.append(f"document_type:{document_type}")
    if scope == "sector":
        label = f"sector:{sector}"
    elif scope == "ticker-set":
        values = sorted({*(tickers or []), *([ticker] if ticker else [])})
        label = f"tickers:{','.join(values)}"
    else:
        label = scope
    if suffix_parts:
        label = f"{label}|{'|'.join(suffix_parts)}"
    return label


def _coverage_row_matches_filters(
    row: dict[str, object],
    *,
    source_name: str | None,
    document_type: str | None,
) -> bool:
    if source_name and str(row.get("source_name") or "") != source_name:
        return False
    if document_type and str(row.get("document_type") or "") != document_type:
        return False
    return True


def _serialize_coverage_row(row: dict[str, object]) -> dict[str, object]:
    payload = json.loads(row.get("payload_json") or "{}")
    return {
        "coverage_id": row["coverage_id"],
        "ticker": row["ticker"],
        "issuer_name": row.get("issuer_name"),
        "obligation_type": row["obligation_type"],
        "source_name": row["source_name"],
        "document_type": row["document_type"],
        "period_label": row["period_label"],
        "period_end_date": row["period_end_date"],
        "deadline_at": row["deadline_at"],
        "expected_state": row["expected_state"],
        "actual_state": row["actual_state"],
        "candidate_count": row["candidate_count"],
        "downloaded_count": row["downloaded_count"],
        "analyzed_count": row["analyzed_count"],
        "blocked_reason": row["blocked_reason"],
        "effective_block_class": row.get("effective_block_class"),
        "blocked_bucket": row.get("blocked_bucket"),
        "retry_eligible_until": row.get("retry_eligible_until"),
        "needs_alternative_source_check": bool(row.get("needs_alternative_source_check")),
        "knowledge_source": row["knowledge_source"],
        "matched_document_ids": payload.get("matched_document_ids") or [],
        "matched_candidate_ids": payload.get("matched_candidate_ids") or [],
    }


def _run_discover_for_scope(
    cfg: Config,
    *,
    target_tickers: list[str],
    date_from: date,
    date_to: date,
    source_name: str | None = None,
) -> int:
    targets = {ticker.upper() for ticker in target_tickers}
    if not targets:
        return 0
    inserted_total = 0
    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
        adapters = [IDXDisclosureAdapter(), IDXFinancialReportAdapter()]
        if source_name:
            adapters = [adapter for adapter in adapters if adapter.name == source_name]
        for adapter in adapters:
            window = DiscoveryWindow(start=date_from, end=date_to)
            if adapter.name == "idx_financial_reports":
                candidates = _discover_financial_reports_for_tickers(cfg, target_tickers=sorted(targets), window=window)
            else:
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


def _discover_financial_reports_for_tickers(
    cfg: Config,
    *,
    target_tickers: list[str],
    window: DiscoveryWindow,
) -> list[object]:
    adapter = IDXFinancialReportAdapter()
    session = PlaywrightSession(
        profile_dir=cfg.browser.profile_dir,
        artifacts_dir=cfg.browser.artifacts_dir,
        headless=cfg.browser.headless,
        challenge_timeout_s=cfg.browser.challenge_timeout_s,
        page_settle_ms=cfg.browser.page_settle_ms,
        channel=cfg.browser.channel,
    )
    year = adapter._resolve_year(window)
    candidates = []
    seen_record_ids: set[str] = set()
    with session.open_page(page_url=adapter.public_url) as page:
        for ticker in target_tickers:
            for report_type in adapter.report_types:
                first_url = adapter._build_api_url(
                    year=year,
                    page_size=adapter.default_page_size,
                    index_from=1,
                    report_type=report_type,
                    kode_emiten=ticker,
                )
                first = adapter._fetch_payload(session=session, page=page, api_url=first_url)
                pages = adapter._total_pages(
                    result_count=int(first.get("ResultCount") or 0),
                    page_size=adapter.default_page_size,
                )
                payloads = [first]
                for page_no in range(2, pages + 1):
                    next_url = adapter._build_api_url(
                        year=year,
                        page_size=adapter.default_page_size,
                        index_from=page_no,
                        report_type=report_type,
                        kode_emiten=ticker,
                    )
                    payloads.append(adapter._fetch_payload(session=session, page=page, api_url=next_url))
                for payload in payloads:
                    for candidate in parse_financial_report_json(payload):
                        if (candidate.ticker or "").upper() != ticker:
                            continue
                        if candidate.source_record_id in seen_record_ids:
                            continue
                        seen_record_ids.add(candidate.source_record_id)
                        candidates.append(candidate)
    return candidates


def _prioritize_candidate_ids(conn: sqlite3.Connection, candidate_ids: list[str]) -> list[str]:
    if not candidate_ids:
        return []
    deduped_ids = list(dict.fromkeys(candidate_ids))
    rows = conn.execute(
        f"""
        SELECT candidate_id, source_name, ticker, title, raw_metadata
        FROM document_candidates
        WHERE candidate_id IN ({",".join("?" for _ in deduped_ids)})
        """,
        tuple(deduped_ids),
    ).fetchall()
    by_id = {str(row["candidate_id"]): dict(row) for row in rows}
    grouped: dict[tuple[str, str, str, str, str], list[str]] = {}
    ordered_misc: list[str] = []
    for candidate_id in deduped_ids:
        row = by_id.get(candidate_id)
        if row is None:
            continue
        if str(row.get("source_name") or "") != "idx_financial_reports":
            ordered_misc.append(candidate_id)
            continue
        raw = json.loads(row.get("raw_metadata") or "{}")
        key = (
            str(row.get("source_name") or ""),
            str(row.get("ticker") or ""),
            str(raw.get("report_year") or ""),
            str(raw.get("report_period") or ""),
            str(raw.get("report_type") or ""),
        )
        grouped.setdefault(key, []).append(candidate_id)

    ordered: list[str] = []
    ordered.extend(ordered_misc)
    for key in sorted(grouped):
        ranked = sorted(grouped[key], key=lambda cid: _financial_candidate_priority(by_id[cid]))
        ordered.append(ranked[0])
    return ordered


def _financial_candidate_priority(row: dict[str, object]) -> tuple[int, str]:
    title = str(row.get("title") or "").lower()
    raw = json.loads(row.get("raw_metadata") or "{}")
    file_type = str(raw.get("file_type") or "").lower()
    if "financialstatement" in title and title.endswith(".pdf"):
        rank = 0
    elif title.endswith(".pdf") and "laporan keuangan" in title:
        rank = 1
    elif title.endswith(".pdf"):
        rank = 2
    elif file_type == ".pdf":
        rank = 3
    elif title.endswith(".xlsx"):
        rank = 4
    elif title.endswith(".zip"):
        rank = 5
    else:
        rank = 6
    return rank, title


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
