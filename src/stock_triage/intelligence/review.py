"""Phase 15 daily digest, review queue, and KPI utilities."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from ..alerts.pipeline import _SEVERITY_ORDER
from ..config import Config
from ..db import WriterQueue, connect, configure_pragmas, run_migrations
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
        ranking_inputs = {
            "high_priority_count": high_count,
            "medium_priority_count": medium_count,
            "low_priority_count": low_count,
            "member_count": len(members),
            "decision_state_penalty": _DECISION_STATE_PENALTY.get(str(entry.get("decision_state") or "Watch"), 0),
        }
        rank_score = (
            high_count * 100
            + medium_count * 40
            + low_count * 10
            + len(members) * 8
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
        "snapshot_freshness_pct": freshness_pct,
        "snapshot_completeness_pct": completeness_pct,
        "digest_validity_pct": digest_validity_pct,
        "digest_card_count": digest_card_count,
        "digest_card_with_reason": digest_card_with_reason,
        "kpi_targets": kpi_targets,
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
