"""Phase 15 daily digest, review queue, and KPI utilities."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from ..alerts.pipeline import _SEVERITY_ORDER
from ..config import Config
from ..coverage import COVERAGE_VERSION, default_coverage_range
from ..db import WriterQueue, connect, configure_pragmas, run_migrations
from ..fiscal_year import load_fiscal_year_overrides
from .pipeline import DERIVED_VERSION


@dataclass(frozen=True)
class DigestRunResult:
    digest_id: str
    digest_date: str
    scope: str
    companies: int
    review_items: int
    alerts: int
    anomalies: int


@dataclass(frozen=True)
class ReviewQueueReport:
    rows: list[dict[str, object]]
    status_counts: dict[str, int]


@dataclass(frozen=True)
class ReviewUpdateResult:
    review_item_id: str
    previous_status: str
    current_status: str
    note: str | None


@dataclass(frozen=True)
class GoldenSetRunResult:
    golden_run_id: str
    scope: str
    target_ticker: str | None
    case_count: int
    passed_count: int
    failed_count: int
    warning_count: int


def run_daily_digest(
    cfg: Config,
    *,
    digest_date: date | None = None,
    scope: str = "watchlist",
    limit: int = 25,
) -> DigestRunResult:
    cfg.ensure_dirs()
    target_date = digest_date or datetime.now(timezone.utc).date()
    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
        reader = connect(cfg.paths.db_path, read_only=True)
        try:
            configure_pragmas(reader, cfg)
            payload = _build_digest_payload(reader, cfg, digest_date=target_date, scope=scope, limit=limit)
        finally:
            reader.close()
        result = wq.submit(lambda conn, p=payload, d=target_date.isoformat(), s=scope: _persist_digest(conn, p, digest_date=d, scope=s))
    return result


def load_review_queue(
    cfg: Config,
    *,
    status: str = "open",
    limit: int = 50,
    ticker: str | None = None,
) -> ReviewQueueReport:
    cfg.ensure_dirs()
    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
    conn = connect(cfg.paths.db_path, read_only=True)
    try:
        configure_pragmas(conn, cfg)
        rows = _load_review_rows(conn, status=status, limit=limit, ticker=ticker)
        counts_rows = conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM review_queue_items
            GROUP BY status
            ORDER BY status ASC
            """
        ).fetchall()
        return ReviewQueueReport(
            rows=rows,
            status_counts={row["status"]: row["count"] for row in counts_rows},
        )
    finally:
        conn.close()


def update_review_status(
    cfg: Config,
    *,
    review_item_id: str,
    status: str,
    note: str | None = None,
) -> ReviewUpdateResult:
    cfg.ensure_dirs()
    normalized_status = status.strip().lower()
    if normalized_status not in {"open", "resolved", "snoozed"}:
        raise ValueError("status must be one of: open, resolved, snoozed")
    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
        return wq.submit(lambda conn: _update_review_status(conn, review_item_id=review_item_id, status=normalized_status, note=note))


def load_intelligence_kpis(cfg: Config) -> dict[str, object]:
    cfg.ensure_dirs()
    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
    conn = connect(cfg.paths.db_path, read_only=True)
    try:
        configure_pragmas(conn, cfg)
        return _load_kpis(conn)
    finally:
        conn.close()


def run_golden_set(
    cfg: Config,
    *,
    ticker: str | None = None,
    limit: int = 25,
) -> GoldenSetRunResult:
    cfg.ensure_dirs()
    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
        reader = connect(cfg.paths.db_path, read_only=True)
        try:
            configure_pragmas(reader, cfg)
            payload = _build_golden_set_payload(reader, ticker=ticker, limit=limit)
        finally:
            reader.close()
        return wq.submit(lambda conn, p=payload, t=ticker: _persist_golden_set_run(conn, payload=p, ticker=t))


def load_latest_golden_runs(cfg: Config, *, limit: int = 10) -> list[dict[str, object]]:
    cfg.ensure_dirs()
    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
    conn = connect(cfg.paths.db_path, read_only=True)
    try:
        configure_pragmas(conn, cfg)
        rows = conn.execute(
            """
            SELECT golden_run_id, scope, target_ticker, case_count, passed_count, failed_count, warning_count,
                   payload_json, derived_version, created_at
            FROM golden_set_runs
            WHERE derived_version = ?
            ORDER BY created_at DESC, golden_run_id DESC
            LIMIT ?
            """,
            (DERIVED_VERSION, limit),
        ).fetchall()
        result_rows: list[dict[str, object]] = []
        for row in rows:
            payload = json.loads(row["payload_json"] or "{}")
            result_rows.append(
                {
                    "golden_run_id": row["golden_run_id"],
                    "scope": row["scope"],
                    "target_ticker": row["target_ticker"],
                    "case_count": row["case_count"],
                    "passed_count": row["passed_count"],
                    "failed_count": row["failed_count"],
                    "warning_count": row["warning_count"],
                    "payload": payload,
                    "summary": payload.get("summary"),
                    "no_case_reason": payload.get("no_case_reason"),
                    "derived_version": row["derived_version"],
                    "created_at": row["created_at"],
                }
            )
        return result_rows
    finally:
        conn.close()


def load_intelligence_observability(cfg: Config) -> dict[str, object]:
    cfg.ensure_dirs()
    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
    conn = connect(cfg.paths.db_path, read_only=True)
    try:
        configure_pragmas(conn, cfg)
        return _load_observability(conn, cfg)
    finally:
        conn.close()


def load_nuance_audit(
    cfg: Config,
    *,
    ticker: str | None = None,
    limit: int = 20,
) -> dict[str, object]:
    cfg.ensure_dirs()
    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
    conn = connect(cfg.paths.db_path, read_only=True)
    try:
        configure_pragmas(conn, cfg)
        return _load_nuance_audit(conn, ticker=ticker, limit=limit)
    finally:
        conn.close()


def _build_digest_payload(conn, cfg: Config, *, digest_date: date, scope: str, limit: int) -> dict[str, object]:
    watchlist = {ticker.upper() for ticker in cfg.alerts.watchlist}
    review_rows = _load_review_rows(conn, status="open", limit=100, ticker=None)
    review_by_ticker: dict[str, int] = {}
    for row in review_rows:
        ticker = str(row.get("ticker") or "")
        review_by_ticker[ticker] = review_by_ticker.get(ticker, 0) + 1

    since = datetime.combine(digest_date, datetime.min.time(), tzinfo=timezone.utc) - timedelta(days=7)
    alert_rows = conn.execute(
        """
        SELECT ticker, signal_kind, signal_name, severity, payload_json, created_at
        FROM alert_events
        WHERE created_at >= ?
        ORDER BY created_at DESC
        """,
        (since.isoformat(),),
    ).fetchall()
    alerts_by_ticker: dict[str, list[dict[str, object]]] = {}
    for row in alert_rows:
        ticker = str(row["ticker"] or "")
        alerts_by_ticker.setdefault(ticker, []).append(
            {
                "signal_kind": row["signal_kind"],
                "signal_name": row["signal_name"],
                "severity": row["severity"],
                "created_at": row["created_at"],
                "payload": json.loads(row["payload_json"] or "{}"),
            }
        )

    snapshot_rows = conn.execute(
        """
        SELECT ticker, as_of_date, payload_json, updated_at
        FROM company_snapshots
        WHERE snapshot_kind = 'current'
          AND derived_version = ?
        ORDER BY updated_at DESC, ticker ASC
        """,
        (DERIVED_VERSION,),
    ).fetchall()

    company_cards: list[dict[str, object]] = []
    sector_moves: dict[str, dict[str, object]] = {}
    for row in snapshot_rows:
        payload = json.loads(row["payload_json"])
        ticker = str(row["ticker"])
        if scope == "watchlist" and watchlist and ticker not in watchlist:
            continue
        top_card = payload.get("top_card", {})
        peer_view = payload.get("peer_comparison", {})
        sector_view = payload.get("sector_context", {})
        anomalies = list(peer_view.get("anomalies") or [])
        card = {
            "ticker": ticker,
            "issuer_name": payload.get("issuer_name"),
            "sector": payload.get("sector"),
            "subsector": payload.get("subsector"),
            "as_of_date": row["as_of_date"],
            "overall_state": top_card.get("overall_state"),
            "decision_state": top_card.get("decision_state"),
            "watchlist_priority": top_card.get("watchlist_priority"),
            "thesis_state": top_card.get("thesis_state"),
            "management_credibility": top_card.get("management_credibility"),
            "review_item_count": review_by_ticker.get(ticker, 0),
            "alert_count": len(alerts_by_ticker.get(ticker, [])),
            "peer_anomaly_count": len(anomalies),
            "reasons": list(top_card.get("reasons") or [])[:3],
            "peer_highlights": [item.get("summary") for item in anomalies[:2]],
        }
        company_cards.append(card)
        sector = str(payload.get("sector") or "unknown")
        entry = sector_moves.setdefault(
            sector,
            {
                "sector": sector,
                "critical_companies": 0,
                "open_review_items": 0,
                "peer_anomalies": 0,
            },
        )
        if top_card.get("overall_state") == "critical":
            entry["critical_companies"] += 1
        entry["open_review_items"] += review_by_ticker.get(ticker, 0)
        entry["peer_anomalies"] += len(anomalies)
        if "heatmap" not in entry:
            entry["heatmap"] = sector_view.get("heatmap", {})

    company_cards.sort(key=_digest_sort_key)
    focus_items = [
        {
            "review_item_id": row["review_item_id"],
            "ticker": row["ticker"],
            "item_type": row["item_type"],
            "priority": row["priority"],
            "rationale": row["rationale"],
            "status": row["status"],
        }
        for row in review_rows[:limit]
    ]
    payload = {
        "digest_date": digest_date.isoformat(),
        "scope": scope,
        "company_cards": company_cards[:limit],
        "review_focus": focus_items,
        "sector_moves": sorted(sector_moves.values(), key=lambda item: (-item["critical_companies"], -item["open_review_items"], item["sector"]))[:10],
        "alert_summary": {
            "total_alerts": sum(len(items) for items in alerts_by_ticker.values()),
            "tickers_with_alerts": sum(1 for items in alerts_by_ticker.values() if items),
            "highest_severity": _highest_alert_severity(alert_rows),
        },
    }
    return payload


def _persist_digest(conn, payload: dict[str, object], *, digest_date: str, scope: str) -> DigestRunResult:
    now = datetime.now(timezone.utc).isoformat()
    digest_id = _stable_id("digest", digest_date, scope, DERIVED_VERSION)
    conn.execute(
        """
        INSERT INTO daily_digests (
            digest_id, digest_date, scope, payload_json, derived_version, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(digest_date, scope, derived_version)
        DO UPDATE SET
            digest_id = excluded.digest_id,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        (
            digest_id,
            digest_date,
            scope,
            json.dumps(payload, sort_keys=True),
            DERIVED_VERSION,
            now,
            now,
        ),
    )
    return DigestRunResult(
        digest_id=digest_id,
        digest_date=digest_date,
        scope=scope,
        companies=len(payload.get("company_cards") or []),
        review_items=len(payload.get("review_focus") or []),
        alerts=int(payload.get("alert_summary", {}).get("total_alerts") or 0),
        anomalies=sum(int(item.get("peer_anomaly_count") or 0) for item in payload.get("company_cards") or []),
    )


def _load_review_rows(conn, *, status: str, limit: int, ticker: str | None) -> list[dict[str, object]]:
    rows = _load_review_rows_raw(conn, status=status, limit=max(limit * 4, 100), ticker=ticker)
    return _group_review_rows(rows, limit=limit)


def _load_review_rows_raw(conn, *, status: str, limit: int, ticker: str | None) -> list[dict[str, object]]:
    where = []
    params: list[object] = [DERIVED_VERSION]
    if status != "all":
        where.append("rqi.status = ?")
        params.append(status)
    if ticker:
        where.append("rqi.ticker = ?")
        params.append(ticker)
    where_sql = f"AND {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        f"""
        SELECT
            rqi.review_item_id,
            rqi.item_type,
            rqi.ticker,
            rqi.document_id,
            rqi.source_key,
            rqi.priority,
            rqi.status,
            rqi.rationale,
            rqi.payload_json,
            rqi.created_at,
            rqi.updated_at,
            d.source_name,
            d.local_path,
            cs.as_of_date,
            cs.payload_json AS snapshot_payload_json
        FROM review_queue_items rqi
        LEFT JOIN documents d ON d.document_id = rqi.document_id
        LEFT JOIN company_snapshots cs
          ON cs.ticker = rqi.ticker
         AND cs.snapshot_kind = 'current'
         AND cs.derived_version = ?
        WHERE 1 = 1
          {where_sql}
        ORDER BY
          CASE rqi.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
          rqi.updated_at DESC,
          rqi.review_item_id ASC
        LIMIT ?
        """,
        tuple(params + [limit]),
    ).fetchall()
    result: list[dict[str, object]] = []
    for row in rows:
        snapshot = json.loads(row["snapshot_payload_json"]) if row["snapshot_payload_json"] else {}
        result.append(
            {
                "review_item_id": row["review_item_id"],
                "item_type": row["item_type"],
                "ticker": row["ticker"],
                "document_id": row["document_id"],
                "source_key": row["source_key"],
                "priority": row["priority"],
                "status": row["status"],
                "rationale": row["rationale"],
                "payload": json.loads(row["payload_json"] or "{}"),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "source_name": row["source_name"],
                "local_path": row["local_path"],
                "as_of_date": row["as_of_date"],
                "decision_state": snapshot.get("top_card", {}).get("decision_state"),
                "overall_state": snapshot.get("top_card", {}).get("overall_state"),
            }
        )
    return result


_REVIEW_PRIORITY_SCORE = {"high": 3, "medium": 2, "low": 1}
_DECISION_STATE_PENALTY = {"Avoid": 35, "Review manually": 25, "Watch": 10, "Buy": 0}


def _group_review_rows(rows: list[dict[str, object]], *, limit: int) -> list[dict[str, object]]:
    grouped: dict[str, dict[str, object]] = {}
    for row in rows:
        family = _review_issue_family(row)
        group_key = f"{row.get('ticker') or '-'}:{family}"
        entry = grouped.setdefault(
            group_key,
            {
                "group_key": group_key,
                "issue_family": family,
                "review_item_ids": [],
                "rows": [],
                "reasons": [],
                "source_names": set(),
                "local_path": None,
                "decision_state": row.get("decision_state"),
                "overall_state": row.get("overall_state"),
                "updated_at": row.get("updated_at"),
                "as_of_date": row.get("as_of_date"),
            },
        )
        entry["review_item_ids"].append(str(row["review_item_id"]))
        entry["rows"].append(row)
        if row.get("rationale"):
            entry["reasons"].append(str(row["rationale"]))
        if row.get("source_name"):
            entry["source_names"].add(str(row["source_name"]))
        if not entry.get("local_path") and row.get("local_path"):
            entry["local_path"] = row["local_path"]
        entry["decision_state"] = _pick_decision_state(entry.get("decision_state"), row.get("decision_state"))
        entry["overall_state"] = _pick_overall_state(entry.get("overall_state"), row.get("overall_state"))
        if str(row.get("updated_at") or "") > str(entry.get("updated_at") or ""):
            entry["updated_at"] = row.get("updated_at")
            entry["as_of_date"] = row.get("as_of_date")

    grouped_rows: list[dict[str, object]] = []
    for entry in grouped.values():
        members = list(entry["rows"])
        members.sort(
            key=lambda item: (
                -_REVIEW_PRIORITY_SCORE.get(str(item.get("priority") or "low"), 0),
                str(item.get("updated_at") or ""),
                str(item.get("review_item_id") or ""),
            ),
            reverse=False,
        )
        primary = members[0]
        high_count = sum(1 for item in members if item.get("priority") == "high")
        medium_count = sum(1 for item in members if item.get("priority") == "medium")
        low_count = sum(1 for item in members if item.get("priority") == "low")
        persistence_count = sum(
            1 for item in members if str(item.get("item_type") or "") in {"persistence_focus", "persistence_contradiction"}
        )
        persistence_members = [
            item
            for item in members
            if str(item.get("item_type") or "") in {"persistence_focus", "persistence_contradiction"}
        ]
        persistence_states = _dedupe_strings(
            [str((item.get("payload") or {}).get("persistence_state") or "") for item in persistence_members]
        )
        persistence_labels = _dedupe_strings(
            [str((item.get("payload") or {}).get("label") or "") for item in persistence_members]
        )
        ranking_inputs = {
            "high_priority_count": high_count,
            "medium_priority_count": medium_count,
            "low_priority_count": low_count,
            "member_count": len(members),
            "persistence_count": persistence_count,
            "persistence_states": persistence_states,
            "decision_state_penalty": _DECISION_STATE_PENALTY.get(str(entry.get("decision_state") or "Watch"), 0),
        }
        rank_score = (
            high_count * 100
            + medium_count * 40
            + low_count * 10
            + len(members) * 8
            + persistence_count * 25
            + _DECISION_STATE_PENALTY.get(str(entry.get("decision_state") or "Watch"), 0)
        )
        grouped_rows.append(
            {
                "review_item_id": primary["review_item_id"],
                "review_item_ids": list(entry["review_item_ids"]),
                "group_key": entry["group_key"],
                "item_type": primary["item_type"],
                "item_types": sorted({str(item.get("item_type") or "") for item in members}),
                "issue_family": entry["issue_family"],
                "ticker": primary["ticker"],
                "document_id": primary["document_id"],
                "source_key": primary["source_key"],
                "priority": _highest_priority(members),
                "status": primary["status"],
                "rationale": _dedupe_strings(entry["reasons"])[0] if entry["reasons"] else primary.get("rationale"),
                "reasons": _dedupe_strings(entry["reasons"])[:3],
                "payload": primary["payload"],
                "created_at": min(str(item.get("created_at") or "") for item in members),
                "updated_at": entry["updated_at"],
                "source_name": ", ".join(sorted(entry["source_names"])) if entry["source_names"] else primary.get("source_name"),
                "local_path": entry.get("local_path"),
                "as_of_date": entry.get("as_of_date"),
                "decision_state": entry.get("decision_state"),
                "overall_state": entry.get("overall_state"),
                "group_count": len(members),
                "rank_score": rank_score,
                "ranking_inputs": ranking_inputs,
                "has_persistence": bool(persistence_members),
                "persistence_states": persistence_states,
                "persistence_labels": persistence_labels[:3],
                "persistence_summary": _summarize_persistence_group(
                    labels=persistence_labels,
                    states=persistence_states,
                ),
            }
        )
    grouped_rows.sort(
        key=lambda item: (
            -int(item.get("rank_score") or 0),
            str(item.get("updated_at") or ""),
            str(item.get("ticker") or ""),
        ),
        reverse=False,
    )
    return grouped_rows[:limit]


def _highest_priority(rows: list[dict[str, object]]) -> str:
    return min(
        (str(item.get("priority") or "low") for item in rows),
        key=lambda value: {"high": 0, "medium": 1, "low": 2}.get(value, 3),
    )


def _pick_decision_state(current: object, candidate: object) -> str | None:
    order = {"Avoid": 0, "Review manually": 1, "Watch": 2, "Buy": 3}
    current_value = str(current) if current else None
    candidate_value = str(candidate) if candidate else None
    if current_value is None:
        return candidate_value
    if candidate_value is None:
        return current_value
    return current_value if order.get(current_value, 99) <= order.get(candidate_value, 99) else candidate_value


def _pick_overall_state(current: object, candidate: object) -> str | None:
    order = {"critical": 0, "review_required": 1, "watch": 2, "stable": 3}
    current_value = str(current) if current else None
    candidate_value = str(candidate) if candidate else None
    if current_value is None:
        return candidate_value
    if candidate_value is None:
        return current_value
    return current_value if order.get(current_value, 99) <= order.get(candidate_value, 99) else candidate_value


def _review_issue_family(row: dict[str, object]) -> str:
    item_type = str(row.get("item_type") or "")
    payload = row.get("payload") or {}
    if item_type == "rule_trigger":
        return _issue_family_from_signal(str(payload.get("rule_name") or "rule"), signal_type="rule")
    if item_type == "event_signal":
        return _issue_family_from_signal(str(payload.get("event_type") or "event"), signal_type="event")
    if item_type == "persistence_focus":
        return _issue_family_from_persistence_focus(str(payload.get("focus_key") or "focus"))
    if item_type == "persistence_contradiction":
        return _issue_family_from_persistence_contradiction(str(payload.get("contradiction_class") or "contradiction"))
    if item_type.startswith("thesis_"):
        thesis_name = str(payload.get("thesis_name") or payload.get("thesis_id") or "thesis")
        return f"thesis:{thesis_name.lower()}"
    if item_type.startswith("claim_"):
        claim_type = str(payload.get("claim_type") or payload.get("claim_subject") or item_type)
        return f"claim:{claim_type.lower()}"
    if item_type == "revision_change":
        changed_metrics = payload.get("changed_metrics") or []
        if any("debt" in str(metric) or "liabil" in str(metric) for metric in changed_metrics):
            return "revision:balance_sheet"
        if any("cash" in str(metric) for metric in changed_metrics):
            return "revision:cash_flow"
        return "revision:filing_change"
    return item_type or "review"


def _issue_family_from_signal(name: str, *, signal_type: str) -> str:
    normalized = name.lower()
    if any(token in normalized for token in ("debt", "liabil", "refinanc", "leverage", "covenant")):
        return "debt_stress"
    if any(token in normalized for token in ("cash", "cfo", "working_capital", "burn")):
        return "cash_flow"
    if any(token in normalized for token in ("auditor", "governance", "director", "commissioner", "management", "board")):
        return "governance"
    if any(token in normalized for token in ("rights", "placement", "buyback", "capital", "share", "warrant", "dilution")):
        return "capital_actions"
    if any(token in normalized for token in ("lawsuit", "regulator", "regulatory", "restriction", "supervision")):
        return "legal_regulatory"
    if any(token in normalized for token in ("contract",)):
        return "contracts"
    if any(token in normalized for token in ("segment",)):
        return "segment"
    if any(token in normalized for token in ("guidance", "projection", "outlook")):
        return "guidance"
    return f"{signal_type}:{normalized}"


def _issue_family_from_persistence_focus(focus_key: str) -> str:
    normalized = focus_key.lower()
    if normalized == "capital_allocation":
        return "capital_actions"
    if normalized == "legal_regulatory":
        return "legal_regulatory"
    if normalized == "accounting_audit":
        return "governance"
    if normalized == "liquidity_balance_sheet":
        return "debt_stress"
    if normalized == "governance_related_party":
        return "governance"
    if normalized == "segment_or_subsidiary_concentration":
        return "segment"
    if normalized == "claim_credibility":
        return "claim:credibility"
    if normalized == "filing_coverage":
        return "revision:filing_change"
    return f"persistence:{normalized}"


def _issue_family_from_persistence_contradiction(contradiction_class: str) -> str:
    normalized = contradiction_class.lower()
    if normalized in {"claim_vs_outcome", "current_positive_claim_vs_critical_risk"}:
        return "claim:credibility"
    if normalized == "mixed_contract_signals":
        return "contracts"
    return f"contradiction:{normalized}"


def _dedupe_strings(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        normalized = " ".join(str(item).strip().split())
        if not normalized:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _summarize_persistence_group(*, labels: list[str], states: list[str]) -> str | None:
    clean_labels = [item for item in labels if item]
    clean_states = [item for item in states if item]
    if not clean_labels and not clean_states:
        return None
    label_text = ", ".join(clean_labels[:2]) if clean_labels else "persistent issue"
    state_text = " / ".join(clean_states[:2]) if clean_states else "active"
    return f"{label_text} · {state_text}"


def _update_review_status(conn, *, review_item_id: str, status: str, note: str | None) -> ReviewUpdateResult:
    row = conn.execute(
        """
        SELECT review_item_id, ticker, status, payload_json
        FROM review_queue_items
        WHERE review_item_id = ?
        LIMIT 1
        """,
        (review_item_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"review item not found: {review_item_id}")
    previous_status = row["status"]
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        UPDATE review_queue_items
        SET status = ?, updated_at = ?
        WHERE review_item_id = ?
        """,
        (status, now, review_item_id),
    )
    conn.execute(
        """
        INSERT INTO review_audit_log (
            audit_id, review_item_id, ticker, action, from_status, to_status, note, payload_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _stable_id("review-audit", review_item_id, now, status),
            review_item_id,
            row["ticker"],
            "status_update",
            previous_status,
            status,
            note,
            row["payload_json"],
            now,
        ),
    )
    return ReviewUpdateResult(
        review_item_id=review_item_id,
        previous_status=previous_status,
        current_status=status,
        note=note,
    )


def _load_kpis(conn) -> dict[str, object]:
    snapshot_rows = conn.execute(
        """
        SELECT payload_json, as_of_date, updated_at
        FROM company_snapshots
        WHERE snapshot_kind = 'current'
          AND derived_version = ?
        ORDER BY updated_at DESC
        """,
        (DERIVED_VERSION,),
    ).fetchall()
    now = datetime.now(timezone.utc)
    overall_state_counts: dict[str, int] = {}
    thesis_state_counts: dict[str, int] = {}
    credibility_counts: dict[str, int] = {}
    stale_snapshots = 0
    total_peer_anomalies = 0
    complete_snapshots = 0
    late_filing_companies = 0
    missing_recommendations = 0
    for row in snapshot_rows:
        payload = json.loads(row["payload_json"])
        top_card = payload.get("top_card", {})
        overall = str(top_card.get("overall_state") or "unknown")
        thesis = str(top_card.get("thesis_state") or "none")
        credibility = str(top_card.get("management_credibility") or "unknown")
        overall_state_counts[overall] = overall_state_counts.get(overall, 0) + 1
        thesis_state_counts[thesis] = thesis_state_counts.get(thesis, 0) + 1
        credibility_counts[credibility] = credibility_counts.get(credibility, 0) + 1
        total_peer_anomalies += len(payload.get("peer_comparison", {}).get("anomalies", []) or [])
        if any(item.get("state") == "late" for item in (payload.get("filing_expectations") or [])):
            late_filing_companies += 1
        if not (payload.get("recommendation") or {}).get("bucket"):
            missing_recommendations += 1
        updated_at = row["updated_at"] or row["as_of_date"]
        if updated_at:
            dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if now - dt > timedelta(days=3):
                stale_snapshots += 1
        # Completeness per PRD v3 §18: a snapshot is "complete" when every
        # major derived section is populated. Missing sections mean the
        # analyst cannot fully triage from the single card.
        has_top_card = bool(top_card.get("overall_state"))
        has_summary = bool(payload.get("latest_summary"))
        has_metrics = bool(payload.get("metrics"))
        has_peer = isinstance(payload.get("peer_comparison"), dict)
        has_credibility = isinstance(payload.get("management_credibility"), dict)
        if has_top_card and has_summary and has_metrics and has_peer and has_credibility:
            complete_snapshots += 1
    review_counts = conn.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM review_queue_items
        GROUP BY status
        """
    ).fetchall()

    snapshot_count = len(snapshot_rows)
    freshness_pct = (
        round((snapshot_count - stale_snapshots) / snapshot_count * 100.0, 1)
        if snapshot_count
        else None
    )
    completeness_pct = (
        round(complete_snapshots / snapshot_count * 100.0, 1) if snapshot_count else None
    )

    # Digest validity per PRD v3 §18: every company card in the latest digest
    # should ship with at least one narrative "reason" so the digest can be
    # audited end-to-end. We compute this against the most recent digest.
    digest_row = conn.execute(
        """
        SELECT payload_json
        FROM daily_digests
        WHERE derived_version = ?
        ORDER BY digest_date DESC, updated_at DESC
        LIMIT 1
        """,
        (DERIVED_VERSION,),
    ).fetchone()
    digest_validity_pct: float | None = None
    digest_card_count = 0
    digest_card_with_reason = 0
    if digest_row is not None:
        try:
            digest_payload = json.loads(digest_row["payload_json"])
        except (TypeError, ValueError):
            digest_payload = {}
        cards = digest_payload.get("company_cards") or []
        digest_card_count = len(cards)
        for card in cards:
            if card.get("reasons"):
                digest_card_with_reason += 1
        if digest_card_count:
            digest_validity_pct = round(
                digest_card_with_reason / digest_card_count * 100.0, 1
            )

    # PRD v3 §18 targets. Audit-only KPIs (delta precision, thesis correctness,
    # alert relevance, claim linkage precision) are surfaced with a null actual
    # value so the UI can show the target without pretending we computed it.
    kpi_targets = {
        "snapshot_freshness_pct": 95.0,
        "snapshot_completeness_pct": 90.0,
        "digest_validity_pct": 100.0,
        "delta_precision_pct": 95.0,
        "thesis_correctness_pct": 98.0,
        "alert_relevance_pct": 85.0,
        "claim_linkage_pct": 80.0,
    }

    return {
        "snapshot_count": snapshot_count,
        "stale_snapshots": stale_snapshots,
        "complete_snapshots": complete_snapshots,
        "overall_state_counts": overall_state_counts,
        "thesis_state_counts": thesis_state_counts,
        "credibility_counts": credibility_counts,
        "open_review_items": sum(row["count"] for row in review_counts if row["status"] == "open"),
        "review_status_counts": {row["status"]: row["count"] for row in review_counts},
        "peer_anomaly_count": total_peer_anomalies,
        "late_filing_companies": late_filing_companies,
        "missing_recommendations": missing_recommendations,
        "snapshot_freshness_pct": freshness_pct,
        "snapshot_completeness_pct": completeness_pct,
        "digest_validity_pct": digest_validity_pct,
        "digest_card_count": digest_card_count,
        "digest_card_with_reason": digest_card_with_reason,
        "kpi_targets": kpi_targets,
    }


def _build_golden_set_payload(conn, *, ticker: str | None, limit: int) -> dict[str, object]:
    params: list[object] = [DERIVED_VERSION]
    where_sql = ""
    if ticker:
        where_sql = "AND ticker = ?"
        params.append(ticker.upper())
    rows = conn.execute(
        f"""
        SELECT ticker, as_of_document_id, as_of_date, payload_json, updated_at
        FROM company_snapshots
        WHERE snapshot_kind = 'current'
          AND derived_version = ?
          {where_sql}
        ORDER BY updated_at DESC, ticker ASC
        LIMIT ?
        """,
        tuple(params + [limit]),
    ).fetchall()

    cases: list[dict[str, object]] = []
    passed = 0
    failed = 0
    warning = 0
    for row in rows:
        payload = json.loads(row["payload_json"] or "{}")
        ticker_value = str(row["ticker"])
        events = payload.get("events") or []
        segment_summary = payload.get("segment_summary") or []
        narrative_signals = payload.get("narrative_signals") or []
        recommendation = payload.get("recommendation") or {}
        decision_panel = payload.get("decision_panel") or {}
        review_required = payload.get("review_required") or []

        def add_case(name: str, status: str, detail: str) -> None:
            nonlocal passed, failed, warning
            cases.append({"ticker": ticker_value, "case_name": name, "status": status, "detail": detail})
            if status == "pass":
                passed += 1
            elif status == "fail":
                failed += 1
            else:
                warning += 1

        stance = str(decision_panel.get("stance") or "")
        confidence = str(decision_panel.get("confidence") or "")
        if payload.get("top_card", {}).get("overall_state") and stance:
            add_case("decision_surface_present", "pass", f"stance={stance} confidence={confidence or '-'}")
        else:
            add_case("decision_surface_present", "fail", "Snapshot is missing top-card decision fields.")

        if payload.get("filing_expectations"):
            add_case("filing_expectations_materialized", "pass", f"{len(payload.get('filing_expectations') or [])} expectation rows present.")
        else:
            add_case("filing_expectations_materialized", "fail", "No filing expectation rows were materialized.")

        rec_status = "pass"
        rec_detail = f"bucket={recommendation.get('bucket') or 'none'}"
        bucket = recommendation.get("bucket")
        if bucket == "stock_pick_candidate":
            valid = (
                stance == "Buy"
                and confidence == "high"
                and len(review_required) == 0
                and int(recommendation.get("evidence_ref_count") or 0) >= 3
            )
            rec_status = "pass" if valid else "fail"
            rec_detail = f"stock pick gating {'ok' if valid else 'mismatch'}"
        elif bucket == "avoid_candidate":
            valid = (
                stance == "Avoid"
                and confidence in {"high", "medium"}
                and int(recommendation.get("evidence_ref_count") or 0) >= 2
            )
            rec_status = "pass" if valid else "fail"
            rec_detail = f"avoid gating {'ok' if valid else 'mismatch'}"
        else:
            rec_status = "warning"
            rec_detail = "No recommendation bucket was assigned."
        add_case("recommendation_consistency", rec_status, rec_detail)

        if not events:
            add_case("event_signal_quality", "warning", "No event signals in the current snapshot.")
        elif all(
            event.get("impact_direction") is not None
            and event.get("confidence") is not None
            and isinstance(event.get("details"), dict)
            for event in events
        ):
            add_case("event_signal_quality", "pass", f"{len(events)} event rows carry impact/confidence/details.")
        else:
            add_case("event_signal_quality", "fail", "One or more event rows are missing impact/confidence/details.")

        if not segment_summary:
            add_case("segment_signal_quality", "warning", "No segment summary exists for this company.")
        elif all(
            row.get("trend")
            and row.get("concentration_risk")
            and ("revenue_share_pct" in row)
            for row in segment_summary
        ):
            add_case("segment_signal_quality", "pass", f"{len(segment_summary)} segment rows include trend/share/risk fields.")
        else:
            add_case("segment_signal_quality", "fail", "Segment summary rows are missing trend/share/risk fields.")

        deduped = list(dict.fromkeys(str(item) for item in narrative_signals))
        if len(narrative_signals) <= 4 and deduped == list(narrative_signals):
            add_case("narrative_signal_quality", "pass", f"{len(narrative_signals)} deterministic narrative signals.")
        else:
            add_case("narrative_signal_quality", "fail", "Narrative signals exceed cap or are not deduplicated.")

        if not events and not segment_summary:
            add_case(
                "sparse_issuer_handled",
                "pass",
                "Sparse issuer state is explicit: event and segment signal depth are both absent and surfaced as warnings.",
            )
        else:
            add_case(
                "sparse_issuer_handled",
                "warning",
                "Snapshot is not sparse enough for the sparse-issuer handling case.",
            )

    no_case_reason: str | None = None
    summary: str
    if not rows:
        if ticker:
            ticker_upper = ticker.upper()
            analyzed_docs = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM documents
                WHERE ticker = ?
                  AND process_state = 'analyzed'
                """,
                (ticker_upper,),
            ).fetchone()["count"]
            if analyzed_docs:
                no_case_reason = (
                    f"No current {DERIVED_VERSION} snapshot exists for {ticker_upper}. "
                    "Run intelligence-rederive for this issuer."
                )
            else:
                document_count = conn.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM documents
                    WHERE ticker = ?
                    """,
                    (ticker_upper,),
                ).fetchone()["count"]
                if document_count:
                    no_case_reason = (
                        f"{ticker_upper} has local documents, but none are analyzed yet. "
                        "Run parse, extract, and rules before golden checks."
                    )
                else:
                    no_case_reason = (
                        f"{ticker_upper} has no local documents yet. "
                        "Run discovery and download before golden checks."
                    )
            if no_case_reason:
                cases.append(
                    {
                        "ticker": ticker_upper,
                        "case_name": "no_snapshot_issuer_handled",
                        "status": "pass",
                        "detail": no_case_reason,
                    }
                )
                passed += 1
        else:
            no_case_reason = (
                f"No current {DERIVED_VERSION} snapshots exist yet. "
                "Run intelligence-rederive before golden checks."
            )
        summary = no_case_reason
    else:
        summary = f"Audited {len(rows)} snapshot row(s): {passed} passed, {failed} failed, {warning} warning."

    return {
        "scope": "ticker" if ticker else "snapshot_sample",
        "target_ticker": ticker.upper() if ticker else None,
        "sample_size": len(rows),
        "case_count": len(cases),
        "passed_count": passed,
        "failed_count": failed,
        "warning_count": warning,
        "summary": summary,
        "no_case_reason": no_case_reason,
        "cases": cases,
    }


def _persist_golden_set_run(conn, *, payload: dict[str, object], ticker: str | None) -> GoldenSetRunResult:
    now = datetime.now(timezone.utc).isoformat()
    scope = str(payload.get("scope") or ("ticker" if ticker else "snapshot_sample"))
    target_ticker = ticker.upper() if ticker else None
    golden_run_id = _stable_id("golden-run", scope, target_ticker or "-", now, DERIVED_VERSION)
    conn.execute(
        """
        INSERT INTO golden_set_runs (
            golden_run_id, scope, target_ticker, case_count, passed_count, failed_count, warning_count,
            payload_json, derived_version, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            golden_run_id,
            scope,
            target_ticker,
            int(payload.get("case_count") or 0),
            int(payload.get("passed_count") or 0),
            int(payload.get("failed_count") or 0),
            int(payload.get("warning_count") or 0),
            json.dumps(payload, sort_keys=True),
            DERIVED_VERSION,
            now,
        ),
    )
    return GoldenSetRunResult(
        golden_run_id=golden_run_id,
        scope=scope,
        target_ticker=target_ticker,
        case_count=int(payload.get("case_count") or 0),
        passed_count=int(payload.get("passed_count") or 0),
        failed_count=int(payload.get("failed_count") or 0),
        warning_count=int(payload.get("warning_count") or 0),
    )


def _load_observability(conn, cfg: Config) -> dict[str, object]:
    snapshot_rows = conn.execute(
        """
        SELECT ticker, payload_json, updated_at
        FROM company_snapshots
        WHERE snapshot_kind = 'current'
          AND derived_version = ?
        ORDER BY updated_at DESC, ticker ASC
        """,
        (DERIVED_VERSION,),
    ).fetchall()
    low_confidence_events = 0
    missing_recommendations = 0
    late_filing_companies = 0
    recommendation_buckets: dict[str, int] = {}
    for row in snapshot_rows:
        payload = json.loads(row["payload_json"] or "{}")
        recommendation = payload.get("recommendation") or {}
        bucket = str(recommendation.get("bucket") or "none")
        recommendation_buckets[bucket] = recommendation_buckets.get(bucket, 0) + 1
        if not recommendation.get("bucket"):
            missing_recommendations += 1
        if any((event.get("confidence") or 0.0) < 0.8 for event in (payload.get("events") or [])):
            low_confidence_events += 1
        if any(item.get("state") == "late" for item in (payload.get("filing_expectations") or [])):
            late_filing_companies += 1

    latest_golden = conn.execute(
        """
        SELECT golden_run_id, scope, target_ticker, case_count, passed_count, failed_count, warning_count, created_at
        FROM golden_set_runs
        WHERE derived_version = ?
        ORDER BY created_at DESC, golden_run_id DESC
        LIMIT 1
        """,
        (DERIVED_VERSION,),
    ).fetchone()
    overrides = load_fiscal_year_overrides(cfg.company_overrides.definition_path)
    return {
        "summary": {
            "snapshot_count": len(snapshot_rows),
            "missing_recommendations": missing_recommendations,
            "late_filing_companies": late_filing_companies,
            "low_confidence_event_companies": low_confidence_events,
            "fiscal_year_override_count": len(overrides),
        },
        "recommendation_buckets": recommendation_buckets,
        "latest_golden_run": (
            {
                "golden_run_id": latest_golden["golden_run_id"],
                "scope": latest_golden["scope"],
                "target_ticker": latest_golden["target_ticker"],
                "case_count": latest_golden["case_count"],
                "passed_count": latest_golden["passed_count"],
                "failed_count": latest_golden["failed_count"],
                "warning_count": latest_golden["warning_count"],
                "created_at": latest_golden["created_at"],
            }
            if latest_golden
            else None
        ),
    }


def _load_nuance_audit(
    conn,
    *,
    ticker: str | None,
    limit: int,
) -> dict[str, object]:
    target_ticker = ticker.upper() if ticker else None
    range_start, range_end = default_coverage_range()
    params: list[object] = [DERIVED_VERSION]
    snapshot_sql = """
        SELECT ticker, payload_json, updated_at
        FROM company_snapshots
        WHERE snapshot_kind = 'current'
          AND derived_version = ?
    """
    if target_ticker:
        snapshot_sql += " AND ticker = ?"
        params.append(target_ticker)
    snapshot_sql += " ORDER BY updated_at DESC, ticker ASC"
    snapshot_rows = conn.execute(snapshot_sql, tuple(params)).fetchall()

    coverage_params: list[object] = [
        COVERAGE_VERSION,
        range_start.isoformat(),
        range_end.isoformat(),
    ]
    coverage_sql = """
        SELECT
            fc.ticker,
            COUNT(*) AS gap_count,
            SUM(CASE WHEN fc.actual_state = 'blocked' THEN 1 ELSE 0 END) AS blocked_count,
            SUM(CASE WHEN fc.expected_state = 'late' THEN 1 ELSE 0 END) AS late_count,
            GROUP_CONCAT(DISTINCT COALESCE(fc.blocked_reason, fc.actual_state)) AS blocked_reasons
        FROM filing_coverage fc
        WHERE fc.coverage_version = ?
          AND date(fc.deadline_at) BETWEEN date(?) AND date(?)
          AND fc.actual_state != 'analyzed'
    """
    if target_ticker:
        coverage_sql += " AND fc.ticker = ?"
        coverage_params.append(target_ticker)
    coverage_sql += " GROUP BY fc.ticker"
    coverage_rows = conn.execute(coverage_sql, tuple(coverage_params)).fetchall()
    coverage_by_ticker = {
        str(row["ticker"]): {
            "gap_count": int(row["gap_count"] or 0),
            "blocked_count": int(row["blocked_count"] or 0),
            "late_count": int(row["late_count"] or 0),
            "blocked_reasons": [part.strip() for part in str(row["blocked_reasons"] or "").split(",") if part.strip()],
        }
        for row in coverage_rows
    }

    doc_params: list[object] = []
    doc_sql = """
        SELECT
            ticker,
            COUNT(*) AS document_count,
            SUM(CASE WHEN process_state = 'analyzed' THEN 1 ELSE 0 END) AS analyzed_count,
            SUM(CASE WHEN process_state = 'failed' THEN 1 ELSE 0 END) AS failed_count,
            MAX(updated_at) AS latest_document_at
        FROM documents
    """
    if target_ticker:
        doc_sql += " WHERE ticker = ?"
        doc_params.append(target_ticker)
    doc_sql += " GROUP BY ticker"
    document_rows = conn.execute(doc_sql, tuple(doc_params)).fetchall()
    documents_by_ticker = {
        str(row["ticker"]): {
            "document_count": int(row["document_count"] or 0),
            "analyzed_count": int(row["analyzed_count"] or 0),
            "failed_count": int(row["failed_count"] or 0),
            "latest_document_at": row["latest_document_at"],
        }
        for row in document_rows
    }

    issuer_rows = conn.execute(
        "SELECT ticker, issuer_name FROM companies"
        + (" WHERE ticker = ?" if target_ticker else ""),
        ((target_ticker,) if target_ticker else ()),
    ).fetchall()
    issuer_names = {str(row["ticker"]): row["issuer_name"] for row in issuer_rows}

    leaders: list[dict[str, object]] = []
    for row in snapshot_rows:
        payload = json.loads(row["payload_json"] or "{}")
        row_ticker = str(row["ticker"])
        coverage = coverage_by_ticker.get(row_ticker, {})
        entry = _build_snapshot_nuance_audit_row(
            ticker=row_ticker,
            issuer_name=str(payload.get("issuer_name") or issuer_names.get(row_ticker) or ""),
            payload=payload,
            updated_at=str(row["updated_at"] or ""),
            coverage=coverage,
            documents=documents_by_ticker.get(row_ticker, {}),
        )
        if entry["score"] > 0:
            leaders.append(entry)

    leaders.sort(key=lambda item: (-int(item["score"]), str(item["ticker"])))

    bridge_rows: list[dict[str, object]] = []
    covered_tickers = set(coverage_by_ticker)
    covered_tickers.update(str(row["ticker"]) for row in snapshot_rows)
    if target_ticker:
        covered_tickers.add(target_ticker)
    for row_ticker in sorted(covered_tickers):
        coverage = coverage_by_ticker.get(row_ticker, {})
        documents = documents_by_ticker.get(row_ticker, {})
        snapshot_entry = next((item for item in leaders if str(item["ticker"]) == row_ticker), None)
        bridge = _build_coverage_bridge_row(
            ticker=row_ticker,
            issuer_name=str(issuer_names.get(row_ticker) or (snapshot_entry or {}).get("issuer_name") or ""),
            coverage=coverage,
            documents=documents,
            snapshot_entry=snapshot_entry,
        )
        if bridge:
            bridge_rows.append(bridge)
    bridge_rows.sort(key=lambda item: (-int(item["score"]), str(item["ticker"])))

    target = _build_target_nuance_audit(
        ticker=target_ticker,
        leaders=leaders,
        bridge_rows=bridge_rows,
        documents=documents_by_ticker.get(target_ticker or "", {}),
        issuer_name=str(issuer_names.get(target_ticker or "") or ""),
    )

    return {
        "scope": "ticker" if target_ticker else "global",
        "target_ticker": target_ticker,
        "date_from": range_start.isoformat(),
        "date_to": range_end.isoformat(),
        "summary": {
            "snapshot_company_count": len(snapshot_rows),
            "leader_count": len(leaders),
            "coverage_bridge_count": len(bridge_rows),
            "note_scope_company_count": sum(1 for item in leaders if int(item["note_scope_count"]) > 0),
            "contradiction_company_count": sum(1 for item in leaders if int(item["contradiction_count"]) > 0),
            "coverage_gap_company_count": len([1 for item in bridge_rows if int(item["coverage_gap_count"]) > 0]),
        },
        "leaders": leaders[:limit],
        "coverage_bridge": bridge_rows[:limit],
        "target": target,
    }


def _build_snapshot_nuance_audit_row(
    *,
    ticker: str,
    issuer_name: str,
    payload: dict[str, object],
    updated_at: str,
    coverage: dict[str, object],
    documents: dict[str, object],
) -> dict[str, object]:
    nuance_findings = list(payload.get("nuance_findings") or [])
    uncertainty_rows = list(payload.get("uncertainty_rows") or [])
    contradiction_rows = list(payload.get("contradiction_rows") or [])
    next_read_guidance = list(payload.get("next_read_guidance") or [])
    filing_expectations = list(payload.get("filing_expectations") or [])
    top_card = payload.get("top_card") or {}
    unresolved_summary = payload.get("unresolved_nuance_summary") or {}
    focus_rows = list(unresolved_summary.get("focus_rows") or [])
    dominant_focus = focus_rows[0] if focus_rows else None
    persistent_signals = list(payload.get("persistent_risk_signals") or [])
    persistent_top = persistent_signals[0] if persistent_signals else None
    persistent_contradictions = list(payload.get("persistent_contradiction_signals") or [])
    persistent_contradiction_top = persistent_contradictions[0] if persistent_contradictions else None

    contradiction_count = len(contradiction_rows)
    critical_contradictions = sum(1 for row in contradiction_rows if str(row.get("severity") or "") == "critical")
    critical_uncertainty = sum(
        1
        for row in uncertainty_rows
        if bool(row.get("requires_human_review")) and str(row.get("severity") or "") == "critical"
    )
    warning_uncertainty = sum(
        1
        for row in uncertainty_rows
        if bool(row.get("requires_human_review")) and str(row.get("severity") or "") == "warning"
    )
    note_scope_count = sum(
        1 for row in uncertainty_rows if str(row.get("uncertainty_class") or "").endswith("_scope")
    )
    critical_nuance = sum(1 for row in nuance_findings if str(row.get("severity") or "") == "critical")
    warning_nuance = sum(1 for row in nuance_findings if str(row.get("severity") or "") == "warning")
    late_filing_count = sum(1 for item in filing_expectations if str(item.get("state") or "") == "late")
    coverage_gap_count = int(coverage.get("gap_count") or 0)
    blocked_coverage_count = int(coverage.get("blocked_count") or 0)

    score = (
        critical_contradictions * 7
        + critical_uncertainty * 5
        + note_scope_count * 3
        + warning_uncertainty * 2
        + critical_nuance * 2
        + warning_nuance
        + late_filing_count * 2
        + blocked_coverage_count * 2
        + max(0, coverage_gap_count - blocked_coverage_count)
    )
    if persistent_top:
        persistence_weight = 6 if str(persistent_top.get("severity")) == "critical" else 3
        if str(persistent_top.get("persistence_state")) == "reappeared":
            persistence_weight += 1
        if str(persistent_top.get("persistence_state")) == "stale":
            persistence_weight = max(1, persistence_weight - 2)
        score += int(persistent_top.get("repeat_count") or 0) * persistence_weight
    if persistent_contradiction_top:
        contradiction_weight = 7 if str(persistent_contradiction_top.get("severity")) == "critical" else 4
        if str(persistent_contradiction_top.get("persistence_state")) == "reappeared":
            contradiction_weight += 1
        if str(persistent_contradiction_top.get("persistence_state")) == "stale":
            contradiction_weight = max(1, contradiction_weight - 3)
        score += int(persistent_contradiction_top.get("repeat_count") or 0) * contradiction_weight
    reasons: list[str] = []
    if critical_contradictions:
        reasons.append(f"{critical_contradictions} critical contradiction row(s) need review")
    if note_scope_count:
        reasons.append(f"{note_scope_count} note-level scope row(s) still need human read")
    if critical_uncertainty or warning_uncertainty:
        reasons.append(
            f"{critical_uncertainty + warning_uncertainty} unresolved uncertainty row(s) still require analyst judgment"
        )
    if late_filing_count:
        reasons.append(f"{late_filing_count} filing expectation(s) are already late")
    if blocked_coverage_count:
        reasons.append(f"{blocked_coverage_count} blocked coverage obligation(s) limit confidence")
    if persistent_top:
        reasons.append(str(persistent_top.get("summary") or "A risk focus repeats across recent filings"))
    if persistent_contradiction_top:
        reasons.append(
            str(persistent_contradiction_top.get("summary") or "A contradiction pattern repeats across recent filings")
        )
    if not reasons and next_read_guidance:
        reasons.append(f"{len(next_read_guidance)} ranked next-read target(s) remain open")

    return {
        "ticker": ticker,
        "issuer_name": issuer_name,
        "score": score,
        "decision_state": top_card.get("decision_state"),
        "confidence": top_card.get("confidence"),
        "overall_state": top_card.get("overall_state"),
        "contradiction_count": contradiction_count,
        "note_scope_count": note_scope_count,
        "uncertainty_count": len(uncertainty_rows),
        "nuance_count": len(nuance_findings),
        "late_filing_count": late_filing_count,
        "coverage_gap_count": coverage_gap_count,
        "blocked_coverage_count": blocked_coverage_count,
        "document_count": int(documents.get("document_count") or 0),
        "analyzed_document_count": int(documents.get("analyzed_count") or 0),
        "reasons": reasons[:3],
        "dominant_focus_label": dominant_focus.get("label") if dominant_focus else None,
        "dominant_focus_count": dominant_focus.get("count") if dominant_focus else 0,
        "persistent_focus_label": persistent_top.get("label") if persistent_top else None,
        "persistent_focus_repeat_count": persistent_top.get("repeat_count") if persistent_top else 0,
        "persistent_focus_state": persistent_top.get("persistence_state") if persistent_top else None,
        "persistent_contradiction_label": persistent_contradiction_top.get("label") if persistent_contradiction_top else None,
        "persistent_contradiction_repeat_count": persistent_contradiction_top.get("repeat_count") if persistent_contradiction_top else 0,
        "persistent_contradiction_state": persistent_contradiction_top.get("persistence_state") if persistent_contradiction_top else None,
        "next_action": (unresolved_summary.get("next_action") or {}).get("why_this_matters"),
        "updated_at": updated_at,
    }


def _build_coverage_bridge_row(
    *,
    ticker: str,
    issuer_name: str,
    coverage: dict[str, object],
    documents: dict[str, object],
    snapshot_entry: dict[str, object] | None,
) -> dict[str, object] | None:
    coverage_gap_count = int(coverage.get("gap_count") or 0)
    blocked_coverage_count = int(coverage.get("blocked_count") or 0)
    late_count = int(coverage.get("late_count") or 0)
    analyzed_document_count = int(documents.get("analyzed_count") or 0)
    document_count = int(documents.get("document_count") or 0)
    failed_count = int(documents.get("failed_count") or 0)
    snapshot_ready = snapshot_entry is not None
    score = blocked_coverage_count * 4 + late_count * 3 + coverage_gap_count
    if snapshot_entry:
        score += int(snapshot_entry.get("score") or 0)
    elif document_count > 0 and analyzed_document_count == 0:
        score += 3
    elif document_count == 0 and coverage_gap_count > 0:
        score += 2
    if score <= 0:
        return None

    blockers: list[str] = []
    if blocked_coverage_count:
        blockers.append(f"{blocked_coverage_count} blocked coverage obligation(s)")
    if late_count:
        blockers.append(f"{late_count} obligation(s) already late")
    if not snapshot_ready and document_count == 0:
        blockers.append("No local documents yet, so nuance cannot be judged")
    elif not snapshot_ready and analyzed_document_count == 0:
        blockers.append("Local documents exist, but none are analyzed yet")
    elif snapshot_entry and int(snapshot_entry.get("note_scope_count") or 0) > 0:
        blockers.append(f"{snapshot_entry['note_scope_count']} note-level scope row(s) still unresolved")
    if failed_count:
        blockers.append(f"{failed_count} failed document(s) reduce confidence")
    if not blockers and coverage.get("blocked_reasons"):
        blockers.extend(list(coverage["blocked_reasons"])[:2])

    recommended_action = "review-company-signals"
    recommended_command = None
    if document_count == 0:
        recommended_action = "discover-missing"
        recommended_command = f"python -m stock_triage coverage-preview discover-missing --scope ticker-set --ticker {ticker}"
    elif analyzed_document_count == 0:
        recommended_action = "process-missing"
        recommended_command = f"python -m stock_triage coverage-preview process-missing --scope ticker-set --ticker {ticker}"
    elif blocked_coverage_count > 0:
        recommended_action = "rerun-blocked"
        recommended_command = f"python -m stock_triage coverage-preview rerun-blocked --scope ticker-set --ticker {ticker}"
    elif not snapshot_ready:
        recommended_action = "rederive-company"
        recommended_command = f"python -m stock_triage intelligence-rederive --ticker {ticker} --limit 1"

    return {
        "ticker": ticker,
        "issuer_name": issuer_name,
        "score": score,
        "coverage_gap_count": coverage_gap_count,
        "blocked_coverage_count": blocked_coverage_count,
        "late_count": late_count,
        "snapshot_ready": snapshot_ready,
        "document_count": document_count,
        "analyzed_document_count": analyzed_document_count,
        "blockers": blockers[:3],
        "recommended_action": recommended_action,
        "recommended_command": recommended_command,
        "latest_document_at": documents.get("latest_document_at"),
    }


def _build_target_nuance_audit(
    *,
    ticker: str | None,
    leaders: list[dict[str, object]],
    bridge_rows: list[dict[str, object]],
    documents: dict[str, object],
    issuer_name: str,
) -> dict[str, object] | None:
    if not ticker:
        return None
    leader = next((item for item in leaders if str(item.get("ticker")) == ticker), None)
    bridge = next((item for item in bridge_rows if str(item.get("ticker")) == ticker), None)
    if leader:
        return {
            "ticker": ticker,
            "issuer_name": issuer_name or leader.get("issuer_name"),
            "state": "snapshot_ready",
            "leader": leader,
            "bridge": bridge,
            "guidance_command": bridge.get("recommended_command") if bridge else None,
        }
    document_count = int(documents.get("document_count") or 0)
    analyzed_count = int(documents.get("analyzed_count") or 0)
    failed_count = int(documents.get("failed_count") or 0)
    state = "no_local_docs"
    guidance = "Run discovery and download before nuance audit can say anything useful."
    if document_count > 0 and analyzed_count == 0:
        state = "docs_not_analyzed"
        guidance = "Run parse, extract, and rules before nuance audit can score this issuer."
    elif document_count > 0:
        state = "derived_snapshot_missing"
        guidance = "Run intelligence-rederive so nuance audit can rank current unresolved signals."
    if bridge:
        guidance = "Coverage gaps exist; clear the missing or blocked obligations before trusting the nuance read."
    return {
        "ticker": ticker,
        "issuer_name": issuer_name,
        "state": state,
        "document_count": document_count,
        "analyzed_document_count": analyzed_count,
        "failed_document_count": failed_count,
        "guidance": guidance,
        "bridge": bridge,
        "guidance_command": bridge.get("recommended_command") if bridge else None,
    }


def load_latest_digests(cfg: Config, *, limit: int = 10) -> list[dict[str, object]]:
    cfg.ensure_dirs()
    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
    conn = connect(cfg.paths.db_path, read_only=True)
    try:
        configure_pragmas(conn, cfg)
        rows = conn.execute(
            """
            SELECT digest_id, digest_date, scope, payload_json, created_at, updated_at
            FROM daily_digests
            WHERE derived_version = ?
            ORDER BY digest_date DESC, updated_at DESC
            LIMIT ?
            """,
            (DERIVED_VERSION, limit),
        ).fetchall()
        return [
            {
                "digest_id": row["digest_id"],
                "digest_date": row["digest_date"],
                "scope": row["scope"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]
    finally:
        conn.close()


def _digest_sort_key(item: dict[str, object]) -> tuple[int, int, int, int, str]:
    priority_rank = {"high": 0, "medium": 1, "low": 2}
    state_rank = {"critical": 0, "review_required": 1, "watch": 2, "stable": 3}
    return (
        priority_rank.get(str(item.get("watchlist_priority") or "low"), 9),
        state_rank.get(str(item.get("overall_state") or "stable"), 9),
        -int(item.get("review_item_count") or 0),
        -int(item.get("peer_anomaly_count") or 0),
        str(item.get("ticker") or ""),
    )


def _highest_alert_severity(rows) -> str:
    highest = "info"
    best_score = -1
    for row in rows:
        level = str(row["severity"] or "info")
        score = _SEVERITY_ORDER.get(level, 0)
        if score > best_score:
            highest = level
            best_score = score
    return highest


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "|".join("" if part is None else str(part) for part in parts)
    return f"{prefix}-{hashlib.sha1(payload.encode('utf-8')).hexdigest()[:24]}"
