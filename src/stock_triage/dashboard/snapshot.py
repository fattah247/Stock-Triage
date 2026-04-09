"""Read-side dashboard snapshot queries."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from ..canary import load_canary_status
from ..config import Config
from ..db import WriterQueue, connect, configure_pragmas, run_migrations
from ..hardening import load_backlog


@dataclass(frozen=True)
class DashboardSnapshot:
    issuers: list[dict[str, object]]
    documents: list[dict[str, object]]
    metrics: list[dict[str, object]]
    findings: list[dict[str, object]]
    revisions: list[dict[str, object]]
    inbox_rows: list[dict[str, object]]
    backlog_rows: list[dict[str, object]]
    canary_rows: list[dict[str, object]]


def load_dashboard_snapshot(
    cfg: Config,
    *,
    document_limit: int = 50,
    finding_limit: int = 50,
) -> DashboardSnapshot:
    cfg.ensure_dirs()

    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)

    conn = connect(cfg.paths.db_path, read_only=True)
    try:
        configure_pragmas(conn, cfg)
        snapshot = DashboardSnapshot(
            issuers=_load_issuers(conn),
            documents=_load_latest_documents(conn, limit=document_limit),
            metrics=_load_metric_snapshot(conn),
            findings=_load_findings(conn, limit=finding_limit),
            revisions=_load_revision_history(conn, limit=50),
            inbox_rows=_load_inbox_rows(conn, limit=60),
            backlog_rows=[],
            canary_rows=[],
        )
    finally:
        conn.close()

    backlog = load_backlog(cfg)
    canary = load_canary_status(cfg)
    return DashboardSnapshot(
        issuers=snapshot.issuers,
        documents=snapshot.documents,
        metrics=snapshot.metrics,
        findings=snapshot.findings,
        revisions=snapshot.revisions,
        inbox_rows=snapshot.inbox_rows,
        backlog_rows=[row.__dict__ for row in backlog.rows],
        canary_rows=[row.__dict__ for row in canary.rows],
    )


def _load_issuers(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT
            c.ticker,
            c.issuer_name,
            c.sector,
            c.subsector,
            c.active_status,
            c.last_seen_at,
            COUNT(d.document_id) AS document_count,
            MAX(d.updated_at) AS latest_document_at
        FROM companies c
        LEFT JOIN documents d ON d.ticker = c.ticker
        GROUP BY c.ticker, c.issuer_name, c.sector, c.subsector, c.active_status, c.last_seen_at
        ORDER BY COALESCE(MAX(d.updated_at), c.last_seen_at) DESC, c.ticker ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _load_latest_documents(conn: sqlite3.Connection, *, limit: int) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT
            d.document_id,
            d.source_name,
            d.source_record_id,
            d.ticker,
            d.document_type,
            d.process_state,
            d.retrieved_at,
            d.updated_at,
            d.local_path,
            d.original_url,
            d.version_group_id,
            COALESCE((
                SELECT COUNT(*)
                FROM rule_runs rr
                WHERE rr.document_id = d.document_id AND rr.triggered = 1
            ), 0) AS triggered_rules
        FROM documents d
        ORDER BY d.updated_at DESC, d.document_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def _load_metric_snapshot(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT
            mi.ticker,
            mi.metric_name,
            mi.value_normalized,
            mi.currency,
            mi.fiscal_year,
            mi.fiscal_period,
            mi.period_end_date,
            mi.document_id
        FROM metric_items mi
        JOIN documents d ON d.document_id = mi.document_id
        WHERE mi.ticker IS NOT NULL
        ORDER BY COALESCE(mi.period_end_date, '') DESC, mi.fiscal_year DESC, d.updated_at DESC
        """
    ).fetchall()
    latest: dict[tuple[str, str], dict[str, object]] = {}
    for row in rows:
        key = (row["ticker"], row["metric_name"])
        latest.setdefault(key, dict(row))
    return sorted(latest.values(), key=lambda item: (str(item["ticker"]), str(item["metric_name"])))


def _load_findings(conn: sqlite3.Connection, *, limit: int) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT
            ar.document_id,
            d.ticker,
            d.source_name,
            d.local_path,
            d.original_url,
            ar.output_json,
            ar.finished_at
        FROM analysis_runs ar
        JOIN documents d ON d.document_id = ar.document_id
        ORDER BY ar.finished_at DESC, ar.analysis_run_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    findings: list[dict[str, object]] = []
    for row in rows:
        payload = json.loads(row["output_json"])
        evidence_map = {
            item["id"]: item
            for item in payload.get("evidence_links", [])
        }
        summary = payload.get("summary", "")
        for item in payload.get("key_changes", []):
            findings.append(
                {
                    "document_id": row["document_id"],
                    "ticker": row["ticker"],
                    "source_name": row["source_name"],
                    "local_path": row["local_path"],
                    "original_url": row["original_url"],
                    "finding_type": "key_change",
                    "name": item.get("severity", "unknown"),
                    "claim": item.get("claim", ""),
                    "summary": summary,
                    "evidence": _evidence_preview(item.get("evidence_links", []), evidence_map),
                    "finished_at": row["finished_at"],
                }
            )
        for item in payload.get("risks", []):
            findings.append(
                {
                    "document_id": row["document_id"],
                    "ticker": row["ticker"],
                    "source_name": row["source_name"],
                    "local_path": row["local_path"],
                    "original_url": row["original_url"],
                    "finding_type": "risk",
                    "name": "risk",
                    "claim": item.get("claim", ""),
                    "summary": summary,
                    "evidence": _evidence_preview(item.get("evidence_links", []), evidence_map),
                    "finished_at": row["finished_at"],
                }
            )
        for item in payload.get("events", []):
            findings.append(
                {
                    "document_id": row["document_id"],
                    "ticker": row["ticker"],
                    "source_name": row["source_name"],
                    "local_path": row["local_path"],
                    "original_url": row["original_url"],
                    "finding_type": "event",
                    "name": item.get("event_type", ""),
                    "claim": item.get("event_type", ""),
                    "summary": summary,
                    "evidence": _evidence_preview(item.get("evidence_links", []), evidence_map),
                    "finished_at": row["finished_at"],
                }
            )
    return findings[:limit]


def _load_revision_history(conn: sqlite3.Connection, *, limit: int) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT
            version_group_id,
            source_name,
            source_record_id,
            ticker,
            COUNT(*) AS revision_count,
            MAX(updated_at) AS latest_revision_at
        FROM documents
        WHERE version_group_id IS NOT NULL
        GROUP BY version_group_id, source_name, source_record_id, ticker
        ORDER BY latest_revision_at DESC, version_group_id ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def _load_inbox_rows(conn: sqlite3.Connection, *, limit: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []

    document_rows = conn.execute(
        """
        SELECT
            d.updated_at AS happened_at,
            d.ticker,
            d.document_id,
            d.source_name,
            d.document_type,
            d.process_state,
            d.local_path,
            d.original_url
        FROM documents d
        ORDER BY d.updated_at DESC, d.document_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    for row in document_rows:
        rows.append(
            {
                "kind": "document",
                "happened_at": row["happened_at"],
                "ticker": row["ticker"],
                "document_id": row["document_id"],
                "source_name": row["source_name"],
                "severity": "ok",
                "title": f"{row['ticker'] or 'Unknown'} {row['document_type'] or 'document'}",
                "summary": f"{row['source_name']} is now {row['process_state']}.",
                "local_path": row["local_path"],
                "original_url": row["original_url"],
            }
        )

    rule_rows = conn.execute(
        """
        SELECT
            rr.evaluated_at AS happened_at,
            rr.ticker,
            rr.document_id,
            rr.rule_name,
            rr.severity,
            rr.rationale,
            d.local_path,
            d.original_url
        FROM rule_runs rr
        JOIN documents d ON d.document_id = rr.document_id
        WHERE rr.triggered = 1
        ORDER BY rr.evaluated_at DESC, rr.rule_run_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    for row in rule_rows:
        rows.append(
            {
                "kind": "rule",
                "happened_at": row["happened_at"],
                "ticker": row["ticker"],
                "document_id": row["document_id"],
                "source_name": "rule_runs",
                "severity": row["severity"] or "warning",
                "title": row["rule_name"],
                "summary": row["rationale"] or "Rule triggered.",
                "local_path": row["local_path"],
                "original_url": row["original_url"],
            }
        )

    analysis_rows = conn.execute(
        """
        SELECT
            ar.finished_at AS happened_at,
            d.ticker,
            ar.document_id,
            d.local_path,
            d.original_url,
            ar.output_json
        FROM analysis_runs ar
        JOIN documents d ON d.document_id = ar.document_id
        ORDER BY ar.finished_at DESC, ar.analysis_run_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    for row in analysis_rows:
        payload = json.loads(row["output_json"])
        rows.append(
            {
                "kind": "summary",
                "happened_at": row["happened_at"],
                "ticker": row["ticker"],
                "document_id": row["document_id"],
                "source_name": "analysis_runs",
                "severity": "info",
                "title": "Summary generated",
                "summary": payload.get("summary", ""),
                "local_path": row["local_path"],
                "original_url": row["original_url"],
            }
        )

    event_rows = conn.execute(
        """
        SELECT
            e.detected_at AS happened_at,
            e.ticker,
            e.document_id,
            e.event_type,
            e.severity,
            e.rationale,
            d.local_path,
            d.original_url
        FROM events e
        JOIN documents d ON d.document_id = e.document_id
        ORDER BY e.detected_at DESC, e.event_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    for row in event_rows:
        rows.append(
            {
                "kind": "event",
                "happened_at": row["happened_at"],
                "ticker": row["ticker"],
                "document_id": row["document_id"],
                "source_name": "events",
                "severity": row["severity"] or "info",
                "title": row["event_type"],
                "summary": row["rationale"] or row["event_type"],
                "local_path": row["local_path"],
                "original_url": row["original_url"],
            }
        )

    alert_rows = conn.execute(
        """
        SELECT
            ae.created_at AS happened_at,
            ae.ticker,
            ae.document_id,
            ae.signal_name,
            ae.severity,
            d.local_path,
            d.original_url
        FROM alert_events ae
        JOIN documents d ON d.document_id = ae.document_id
        ORDER BY ae.created_at DESC, ae.alert_event_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    for row in alert_rows:
        rows.append(
            {
                "kind": "alert",
                "happened_at": row["happened_at"],
                "ticker": row["ticker"],
                "document_id": row["document_id"],
                "source_name": "alert_events",
                "severity": row["severity"] or "warning",
                "title": row["signal_name"],
                "summary": "Watchlist alert emitted.",
                "local_path": row["local_path"],
                "original_url": row["original_url"],
            }
        )

    canary_rows = conn.execute(
        """
        SELECT
            finished_at AS happened_at,
            source_name,
            status,
            accessible,
            drifted
        FROM canary_runs
        ORDER BY finished_at DESC, canary_run_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    for row in canary_rows:
        severity = "critical" if row["status"] == "failed" else "warning" if row["drifted"] else "ok"
        rows.append(
            {
                "kind": "canary",
                "happened_at": row["happened_at"],
                "ticker": None,
                "document_id": None,
                "source_name": row["source_name"],
                "severity": severity,
                "title": f"{row['source_name']} canary",
                "summary": f"status={row['status']} accessible={row['accessible']} drifted={row['drifted']}",
                "local_path": None,
                "original_url": None,
            }
        )

    def _sort_key(item: dict[str, object]) -> tuple[str, str]:
        return (str(item.get("happened_at") or ""), str(item.get("title") or ""))

    rows.sort(key=_sort_key, reverse=True)
    return rows[:limit]


def _evidence_preview(span_ids: list[str], evidence_map: dict[str, dict[str, object]]) -> str:
    previews: list[str] = []
    for span_id in span_ids:
        entry = evidence_map.get(span_id)
        if entry is None:
            previews.append(span_id)
            continue
        previews.append(f"{entry['id']} p{entry['page_start']}: {entry.get('excerpt_text', '')[:120]}")
    return " | ".join(previews)
