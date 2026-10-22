"""Phase 8 alerts: watchlist-triggered local notifications."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..config import Config
from ..db import WriterQueue, connect, configure_pragmas, run_migrations

_SEVERITY_ORDER = {"info": 1, "warning": 2, "high": 3, "critical": 4}


@dataclass(frozen=True)
class AlertSignal:
    document_id: str
    source_name: str
    ticker: str | None
    signal_kind: str
    signal_name: str
    severity: str
    claim: str
    local_path: str | None
    original_url: str | None
    updated_at: str
    suggested_next_action: str | None = None
    details: dict[str, object] | None = None


@dataclass(frozen=True)
class AlertRunResult:
    selected: int
    emitted: int
    skipped_existing: int


def run_alerts(cfg: Config) -> AlertRunResult:
    cfg.ensure_dirs()
    watchlist = {ticker.upper() for ticker in cfg.alerts.watchlist}
    if not watchlist:
        return AlertRunResult(selected=0, emitted=0, skipped_existing=0)

    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
        reader = connect(cfg.paths.db_path, read_only=True)
        try:
            configure_pragmas(reader, cfg)
            signals = _load_signals(
                reader,
                watchlist=watchlist,
                min_severity=cfg.alerts.min_severity,
                lookback_days=cfg.alerts.lookback_days,
            )
        finally:
            reader.close()

        emitted = 0
        skipped_existing = 0
        for signal in signals:
            for channel in cfg.alerts.channels:
                payload = _signal_payload(signal)
                inserted = wq.submit(
                    lambda conn, s=signal, c=channel, p=payload: _persist_alert(conn, signal=s, channel=c, payload=p)
                )
                if inserted:
                    emitted += 1
                    if channel == "jsonl":
                        cfg.alerts.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
                        with cfg.alerts.jsonl_path.open("a", encoding="utf-8") as handle:
                            handle.write(json.dumps(payload, ensure_ascii=True) + "\n")
                else:
                    skipped_existing += 1
    return AlertRunResult(selected=len(signals), emitted=emitted, skipped_existing=skipped_existing)


def _load_signals(
    conn: sqlite3.Connection,
    *,
    watchlist: set[str],
    min_severity: str,
    lookback_days: int,
) -> list[AlertSignal]:
    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    severity_floor = _SEVERITY_ORDER.get(min_severity, _SEVERITY_ORDER["warning"])

    rows = conn.execute(
        """
        SELECT
            d.document_id,
            d.source_name,
            d.ticker,
            'rule' AS signal_kind,
            rr.rule_name AS signal_name,
            rr.severity,
            rr.rationale AS claim,
            d.local_path,
            d.original_url,
            rr.evaluated_at AS updated_at,
            'review now' AS suggested_next_action,
            NULL AS details_json
        FROM rule_runs rr
        JOIN documents d ON d.document_id = rr.document_id
        WHERE rr.triggered = 1
          AND d.ticker IS NOT NULL
          AND rr.evaluated_at >= ?
        UNION ALL
        SELECT
            d.document_id,
            d.source_name,
            d.ticker,
            'event' AS signal_kind,
            ev.event_type AS signal_name,
            ev.severity,
            ev.rationale AS claim,
            d.local_path,
            d.original_url,
            ev.detected_at AS updated_at,
            'review now' AS suggested_next_action,
            NULL AS details_json
        FROM events ev
        JOIN documents d ON d.document_id = ev.document_id
        WHERE d.ticker IS NOT NULL
          AND ev.detected_at >= ?
        UNION ALL
        SELECT
            d.document_id,
            d.source_name,
            te.ticker,
            'thesis' AS signal_kind,
            -- improving and weakening share a severity; the status keeps their dedupe keys apart.
            td.thesis_name || ' (' || te.status || ')' AS signal_name,
            CASE te.status
                WHEN 'broken' THEN 'critical'
                WHEN 'improving' THEN 'warning'
                WHEN 'weakening' THEN 'warning'
                ELSE 'info'
            END AS severity,
            te.rationale AS claim,
            d.local_path,
            d.original_url,
            te.evaluated_at AS updated_at,
            CASE te.status
                WHEN 'broken' THEN 'review now'
                WHEN 'improving' THEN 'monitor thesis'
                WHEN 'weakening' THEN 'review thesis'
                ELSE 'monitor'
            END AS suggested_next_action,
            te.payload_json AS details_json
        FROM thesis_evaluations te
        JOIN thesis_definitions td ON td.thesis_id = te.thesis_id
        LEFT JOIN documents d ON d.document_id = te.document_id
        WHERE te.ticker IS NOT NULL
          AND te.evaluated_at >= ?
          AND te.status IN ('broken', 'improving', 'weakening')
        ORDER BY updated_at DESC
        """,
        (since, since, since),
    ).fetchall()
    signals: list[AlertSignal] = []
    for row in rows:
        ticker = (row["ticker"] or "").upper()
        if ticker not in watchlist:
            continue
        if _SEVERITY_ORDER.get(row["severity"] or "info", 1) < severity_floor:
            continue
        signals.append(
            AlertSignal(
                document_id=row["document_id"],
                source_name=row["source_name"],
                ticker=row["ticker"],
                signal_kind=row["signal_kind"],
                signal_name=row["signal_name"],
                severity=row["severity"],
                claim=row["claim"],
                local_path=row["local_path"],
                original_url=row["original_url"],
                updated_at=row["updated_at"],
                suggested_next_action=row["suggested_next_action"],
                details=json.loads(row["details_json"]) if row["details_json"] else None,
            )
        )
    return signals


def _signal_payload(signal: AlertSignal) -> dict[str, object]:
    return {
        "document_id": signal.document_id,
        "source_name": signal.source_name,
        "ticker": signal.ticker,
        "signal_kind": signal.signal_kind,
        "signal_name": signal.signal_name,
        "severity": signal.severity,
        "claim": signal.claim,
        "local_path": signal.local_path,
        "original_url": signal.original_url,
        "updated_at": signal.updated_at,
        "suggested_next_action": signal.suggested_next_action,
        "details": signal.details or {},
    }


def _persist_alert(
    conn: sqlite3.Connection,
    *,
    signal: AlertSignal,
    channel: str,
    payload: dict[str, object],
) -> bool:
    alert_key = f"{signal.document_id}:{signal.signal_kind}:{signal.signal_name}:{signal.severity}"
    exists = conn.execute(
        "SELECT 1 FROM alert_events WHERE alert_key = ? AND channel = ?",
        (alert_key, channel),
    ).fetchone()
    if exists is not None:
        return False
    alert_event_id = hashlib.sha256(f"{channel}:{alert_key}".encode("utf-8")).hexdigest()[:24]
    conn.execute(
        """
        INSERT INTO alert_events (
            alert_event_id, channel, source_name, document_id, ticker,
            signal_kind, signal_name, severity, alert_key, payload_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            alert_event_id,
            channel,
            signal.source_name,
            signal.document_id,
            signal.ticker,
            signal.signal_kind,
            signal.signal_name,
            signal.severity,
            alert_key,
            json.dumps(payload, ensure_ascii=True, sort_keys=True),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    return True
