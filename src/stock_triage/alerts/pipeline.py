"""Phase 8 alerts: watchlist-triggered local notifications, plus market alerts."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..config import Config
from ..db import WriterQueue, connect, configure_pragmas, run_migrations
from ..logging_setup import get_logger
from ..market.breadth import level_at_least
from ..market.catalysts import load_upcoming_catalysts
from ..market.theses import MARKET_THESIS_VERSION

_SEVERITY_ORDER = {"info": 1, "warning": 2, "high": 3, "critical": 4}
_CATALYST_SEVERITY = {"low": "info", "medium": "warning", "high": "high"}
_BREADTH_SEVERITY = {"elevated": "warning", "stressed": "high", "crisis": "critical"}
_REGIME_SEVERITY = {"calm": "info", "elevated": "warning", "stressed": "high", "crisis": "critical"}
_THESIS_SEVERITY = {"broken": "critical", "weakening": "warning", "improving": "warning"}
_REGIME_RANK = {"calm": 0, "elevated": 1, "stressed": 2, "crisis": 3}
# Channels that deliver; any other name is recorded in alert_events only.
DELIVERY_CHANNELS = frozenset({"jsonl", "macos"})
# Channels that interrupt the user. A backlog must not arrive as dozens of popups,
# so each run shows at most alerts.push_max_per_run, most severe first.
PUSH_CHANNELS = frozenset({"macos"})
_MACOS_MESSAGE_CHARS = 220

logger = get_logger(__name__)


@dataclass(frozen=True)
class AlertSignal:
    document_id: str | None  # None for market alerts, which no single document raises
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
    # Dedupe key; defaults to one built from the document and signal.
    alert_key: str | None = None


@dataclass(frozen=True)
class AlertRunResult:
    selected: int
    emitted: int
    skipped_existing: int


def run_alerts(cfg: Config, *, now: datetime | None = None) -> AlertRunResult:
    """Emit new alerts: company signals for the watchlist, and market alerts for everyone."""
    cfg.ensure_dirs()
    now = now or datetime.now(timezone.utc)
    watchlist = {ticker.upper() for ticker in cfg.alerts.watchlist}

    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
        reader = connect(cfg.paths.db_path, read_only=True)
        try:
            configure_pragmas(reader, cfg)
            signals = (
                _load_signals(
                    reader,
                    watchlist=watchlist,
                    min_severity=cfg.alerts.min_severity,
                    lookback_days=cfg.alerts.lookback_days,
                    now=now,
                )
                if watchlist
                else []
            )
            signals.extend(_load_market_signals(reader, cfg=cfg, watchlist=watchlist, now=now))
        finally:
            reader.close()

        emitted = 0
        skipped_existing = 0
        pending_push: dict[str, list[dict[str, object]]] = {}
        for signal in signals:
            for channel in cfg.alerts.channels:
                payload = _signal_payload(signal)
                inserted = wq.submit(
                    lambda conn, s=signal, c=channel, p=payload: _persist_alert(conn, signal=s, channel=c, payload=p)
                )
                if inserted:
                    emitted += 1
                    if channel in PUSH_CHANNELS:
                        pending_push.setdefault(channel, []).append(payload)
                    else:
                        _deliver(cfg, channel=channel, payload=payload)
                else:
                    skipped_existing += 1
        for channel, payloads in pending_push.items():
            for payload in _cap_push(payloads, limit=cfg.alerts.push_max_per_run):
                _deliver(cfg, channel=channel, payload=payload)
    return AlertRunResult(selected=len(signals), emitted=emitted, skipped_existing=skipped_existing)


def _fragile_watchlist_signal(
    conn: sqlite3.Connection, *, watchlist: set[str], regime: str, since: str, now: datetime
) -> list[AlertSignal]:
    """In a stressed market, the five most fragile watchlist companies, once per stretch."""
    placeholders = ",".join("?" for _ in watchlist)
    rows = conn.execute(
        f"""
        SELECT es.ticker, es.score, es.components_json
        FROM exposure_scores es
        WHERE es.ticker IN ({placeholders})
          AND es.as_of_date = (SELECT MAX(as_of_date) FROM exposure_scores)
        ORDER BY es.score DESC, es.ticker ASC
        LIMIT 5
        """,
        tuple(sorted(watchlist)),
    ).fetchall()
    if not rows:
        return []
    parts = []
    for row in rows:
        components = json.loads(row["components_json"]).get("components", [])
        top = max(components, key=lambda item: item["points"], default=None)
        reason = f" ({top['reason']})" if top and top["points"] else ""
        parts.append(f"{row['ticker']} {row['score']:.0f}{reason}")
    return [
        AlertSignal(
            document_id=None,
            source_name="market_risk",
            ticker=None,
            signal_kind="exposure",
            signal_name=f"exposure:most_fragile:{regime}",
            severity=_REGIME_SEVERITY[regime],
            claim=f"Market {regime} since {since}; most fragile on the watchlist: " + "; ".join(parts),
            local_path=None,
            original_url=None,
            updated_at=now.isoformat(),
            suggested_next_action="review the watchlist's exposure",
            details={"regime": regime, "since": since, "tickers": [row["ticker"] for row in rows]},
            alert_key=f"market:exposure:most_fragile:{since}:{regime}",
        )
    ]


def _discovery_signals(conn: sqlite3.Connection, *, now: datetime, days: int = 2) -> list[AlertSignal]:
    """High-trust bursts and new pairings, unexplained IHSG moves, and themes promoted to high severity.

    Unclassified items wait in the review queue: official sources publish many
    releases no theme matches, and each one alerting would bury the rest.
    """
    since = (now - timedelta(days=days)).date().isoformat()
    signals: list[AlertSignal] = []
    rows = conn.execute(
        """
        SELECT finding_id, kind, term_or_entity, detected_on, burst_ratio, item_ids_json, details_json
        FROM emerging_findings
        WHERE status = 'open' AND detected_on >= ?
        ORDER BY detected_on DESC
        """,
        (since,),
    ).fetchall()
    for row in rows:
        details = json.loads(row["details_json"])
        item_ids = json.loads(row["item_ids_json"])
        if row["kind"] == "anomaly":
            if details.get("series_id") != "index:COMPOSITE":
                continue  # sector and flow anomalies wait in the review queue
            signals.append(
                AlertSignal(
                    document_id=None,
                    source_name="market_risk",
                    ticker=None,
                    signal_kind="market_anomaly",
                    signal_name=f"anomaly:{row['term_or_entity']}",
                    severity="high",
                    claim=str(details.get("note")),
                    local_path=None,
                    original_url=None,
                    updated_at=now.isoformat(),
                    suggested_next_action="read the research item in the review queue",
                    details={"finding_id": row["finding_id"], **details},
                    alert_key=f"market:anomaly:{row['finding_id']}",
                )
            )
            continue
        if row["kind"] == "unclassified":
            continue
        placeholders = ",".join("?" for _ in item_ids) or "''"
        trusted = conn.execute(
            f"SELECT title, url FROM watch_items WHERE trust = 'high' AND item_id IN ({placeholders}) LIMIT 1",
            tuple(item_ids),
        ).fetchone()
        if trusted is None:
            continue
        label = {"burst": "burst", "new_pairing": "new pairing"}.get(row["kind"], row["kind"])
        signals.append(
            AlertSignal(
                document_id=None,
                source_name="market_risk",
                ticker=None,
                signal_kind="emerging_theme",
                signal_name=f"{row['kind']}:{row['term_or_entity'][:60]}",
                severity="high",
                claim=f"Emerging {label}: '{row['term_or_entity']}' in {len(item_ids)} watched items, e.g. \"{trusted['title']}\"",
                local_path=None,
                original_url=trusted["url"],
                updated_at=now.isoformat(),
                suggested_next_action="review the finding: promote it to a theme or dismiss it",
                details={"finding_id": row["finding_id"], "kind": row["kind"], "burst_ratio": row["burst_ratio"]},
                alert_key=f"market:finding:{row['finding_id']}",
            )
        )
    for row in conn.execute(
        "SELECT theme_id, title, keywords_json, sectors_json FROM risk_themes WHERE severity = 'high' AND created_at >= ?",
        ((now - timedelta(days=7)).isoformat(),),
    ):
        signals.append(
            AlertSignal(
                document_id=None,
                source_name="market_risk",
                ticker=None,
                signal_kind="emerging_theme",
                signal_name=f"promoted:{row['theme_id']}",
                severity="high",
                claim=f"New high-severity theme tracked: {row['title']} (keywords: {', '.join(json.loads(row['keywords_json'])[:5])})",
                local_path=None,
                original_url=None,
                updated_at=now.isoformat(),
                suggested_next_action="check the watchlist's exposure to it",
                details={"theme_id": row["theme_id"], "sectors": json.loads(row["sectors_json"])},
                alert_key=f"market:theme_promoted:{row['theme_id']}",
            )
        )
    return signals


def _crash_risk_signals(conn: sqlite3.Connection, *, threshold: float, now: datetime) -> list[AlertSignal]:
    """The estimate crossing its warning level, while its track record makes it worth showing."""
    rows = conn.execute(
        "SELECT trade_date, probability, base_rate, trustworthy, analogs_json, model_version "
        "FROM crash_risk_days ORDER BY trade_date DESC LIMIT 400"
    ).fetchall()

    def warning(row: sqlite3.Row) -> bool:
        return bool(row["trustworthy"]) and row["probability"] is not None and row["probability"] >= threshold

    if not rows or not warning(rows[0]):
        return []
    since = rows[0]["trade_date"]
    for row in rows[1:]:
        if not warning(row):
            break
        since = row["trade_date"]
    latest = rows[0]
    record = conn.execute(
        "SELECT hit_rate, false_alarm_rate, scored_days FROM forecast_track_record WHERE model_version = ? "
        "ORDER BY evaluated_at DESC LIMIT 1",
        (latest["model_version"],),
    ).fetchone()
    analogs = json.loads(latest["analogs_json"])
    claim = f"Crash risk {latest['probability']:.0%} (base rate {latest['base_rate']:.0%}) as of {latest['trade_date']}"
    if record is not None and record["hit_rate"] is not None:
        false_alarms = record["false_alarm_rate"]
        claim += (
            f"; past warnings caught {record['hit_rate']:.0%} of crash days"
            + (f", and {false_alarms:.0%} of warnings were false alarms" if false_alarms is not None else "")
        )
    if analogs:
        nearest = analogs[0]
        claim += f"; most like {nearest['opened_on']}, which fell a further {nearest['further_fall_pct']}%"
    return [
        AlertSignal(
            document_id=None,
            source_name="market_risk",
            ticker=None,
            signal_kind="crash_risk",
            signal_name=f"crash_risk:{threshold:g}",
            severity="critical" if latest["probability"] >= max(threshold * 1.5, 0.6) else "high",
            claim=claim,
            local_path=None,
            original_url=None,
            updated_at=now.isoformat(),
            suggested_next_action="review the watchlist's exposure",
            details={
                "trade_date": latest["trade_date"],
                "since": since,
                "probability": latest["probability"],
                "base_rate": latest["base_rate"],
                "analogs": analogs,
            },
            alert_key=f"market:crash_risk:{since}",
        )
    ]


def _market_thesis_signals(conn: sqlite3.Connection, *, watchlist: set[str], now: datetime) -> list[AlertSignal]:
    """The latest evaluation of each market thesis, once per stretch of the same status."""
    rows = conn.execute(
        """
        SELECT te.thesis_id, te.ticker, te.status, te.rationale, te.payload_json, td.thesis_name
        FROM thesis_evaluations te
        JOIN thesis_definitions td ON td.thesis_id = te.thesis_id
        WHERE te.derived_version = ?
          AND td.status = 'active'
          AND json_extract(te.payload_json, '$.trade_date') = (
              SELECT MAX(json_extract(t2.payload_json, '$.trade_date'))
              FROM thesis_evaluations t2
              WHERE t2.thesis_id = te.thesis_id AND t2.derived_version = te.derived_version
          )
        """,
        (MARKET_THESIS_VERSION,),
    ).fetchall()
    signals: list[AlertSignal] = []
    for row in rows:
        if row["status"] not in _THESIS_SEVERITY:
            continue
        ticker = (row["ticker"] or "").upper() or None
        if ticker and watchlist and ticker not in watchlist:
            continue
        payload = json.loads(row["payload_json"])
        since = payload.get("since") or payload.get("trade_date")
        signals.append(
            AlertSignal(
                document_id=None,
                source_name="market_risk",
                ticker=ticker,
                signal_kind="thesis",
                signal_name=f"{row['thesis_name']} ({row['status']})",
                severity=_THESIS_SEVERITY[row["status"]],
                claim=f"{row['thesis_name']}: {row['status']} since {since}. {row['rationale']}",
                local_path=None,
                original_url=None,
                updated_at=now.isoformat(),
                suggested_next_action="review now" if row["status"] == "broken" else "review thesis",
                details=payload,
                alert_key=f"market:thesis:{row['thesis_id']}:{row['status']}:{since}",
            )
        )
    return signals


def _cap_push(payloads: list[dict[str, object]], *, limit: int) -> list[dict[str, object]]:
    """The most severe alerts, then one summary for the rest when there are too many."""
    ranked = sorted(payloads, key=lambda item: -_SEVERITY_ORDER.get(str(item.get("severity")), 1))
    if len(ranked) <= limit:
        return ranked
    shown, rest = ranked[: max(limit - 1, 0)], ranked[max(limit - 1, 0) :]
    kinds: dict[str, int] = {}
    for item in rest:
        kind = str(item.get("signal_kind") or "alert")
        kinds[kind] = kinds.get(kind, 0) + 1
    breakdown = ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items(), key=lambda pair: -pair[1]))
    summary = {
        "signal_kind": "summary",
        "ticker": None,
        "severity": rest[0].get("severity") or "warning",
        "claim": f"{len(rest)} more alerts ({breakdown}); see the dashboard or alerts.jsonl",
    }
    return [*shown, summary]


def _deliver(cfg: Config, *, channel: str, payload: dict[str, object]) -> None:
    if channel == "jsonl":
        cfg.alerts.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with cfg.alerts.jsonl_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=True) + "\n")
    elif channel == "macos":
        if not _notify_macos(payload):
            logger.warning("alerts: macOS notification not delivered (needs macOS with osascript)")
    elif channel not in DELIVERY_CHANNELS:
        logger.warning("alerts: channel %r has no delivery; the alert is recorded only", channel)


def _notify_macos(payload: dict[str, object]) -> bool:
    """Show a desktop notification. Alert text can come from filings or news,
    so it travels as script arguments, never spliced into the script."""
    if sys.platform != "darwin" or shutil.which("osascript") is None:
        return False
    severity = str(payload.get("severity") or "info")
    subtitle = str(payload.get("ticker") or str(payload.get("signal_kind") or "").replace("_", " "))
    message = " ".join(str(payload.get("claim") or "").split())[:_MACOS_MESSAGE_CHARS]
    try:
        completed = subprocess.run(
            [
                "osascript",
                "-e",
                "on run argv",
                "-e",
                "display notification (item 1 of argv) with title (item 2 of argv) subtitle (item 3 of argv)",
                "-e",
                "end run",
                message,
                f"Stock-Triage · {severity}",
                subtitle,
            ],
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _load_market_signals(
    conn: sqlite3.Connection,
    *,
    cfg: Config,
    watchlist: set[str],
    now: datetime,
) -> list[AlertSignal]:
    """Risk-date reminders, stress spreading across filers, and fragile watchlist names."""
    signals: list[AlertSignal] = []
    today = now.date()
    for catalyst in load_upcoming_catalysts(
        conn, today=today, days=cfg.market.catalyst_lead_days, watchlist=watchlist
    ):
        days_left = (catalyst.catalyst_date - today).days
        when = "today" if days_left == 0 else "tomorrow" if days_left == 1 else f"in {days_left} days"
        signals.append(
            AlertSignal(
                document_id=None,
                source_name="market_risk",
                ticker=catalyst.ticker,
                signal_kind="catalyst",
                signal_name=f"{catalyst.kind}:{catalyst.catalyst_date.isoformat()}",
                severity=_CATALYST_SEVERITY.get(catalyst.severity, "warning"),
                claim=f"{catalyst.title} on {catalyst.catalyst_date.isoformat()} ({when})",
                local_path=None,
                original_url=catalyst.source_url,
                updated_at=now.isoformat(),
                suggested_next_action="review exposure before the event",
                details={"kind": catalyst.kind, "date": catalyst.catalyst_date.isoformat(), "notes": catalyst.notes},
                alert_key=f"market:catalyst:{catalyst.catalyst_id}",
            )
        )

    latest = conn.execute(
        "SELECT trade_date, regime, drivers_json FROM market_regime_days ORDER BY trade_date DESC LIMIT 1"
    ).fetchone()
    before = (
        conn.execute(
            "SELECT trade_date, regime FROM market_regime_days WHERE regime != ? ORDER BY trade_date DESC LIMIT 1",
            (latest["regime"],),
        ).fetchone()
        if latest is not None
        else None
    )
    # One alert per stretch of the same state, keyed on the day it began, so a run days
    # after the change (or after a backfill) still sends it. A history that was never
    # anything but calm sends none.
    if latest is not None and (before is not None or latest["regime"] != "calm"):
        since = conn.execute(
            "SELECT MIN(trade_date) FROM market_regime_days WHERE trade_date > ?",
            (before["trade_date"] if before is not None else "",),
        ).fetchone()[0]
        previous = before["regime"] if before is not None else None
        drivers = json.loads(latest["drivers_json"]).get("drivers", [])
        reasons = "; ".join(str(item.get("text") or item.get("signal_name")) for item in drivers[:3])
        change = f"now {latest['regime']}" + (f" (was {previous})" if previous else "")
        when = f"on {since}" if since == latest["trade_date"] else f"since {since}, as of {latest['trade_date']}"
        signals.append(
            AlertSignal(
                document_id=None,
                source_name="market_risk",
                ticker=None,
                signal_kind="market_regime",
                signal_name=f"market_regime:{latest['regime']}",
                severity=_REGIME_SEVERITY.get(latest["regime"], "warning"),
                claim=f"Market {change} {when}" + (f": {reasons}" if reasons else ""),
                local_path=None,
                original_url=None,
                updated_at=now.isoformat(),
                suggested_next_action="review the watchlist's exposure",
                details={
                    "trade_date": latest["trade_date"],
                    "since": since,
                    "regime": latest["regime"],
                    "previous": previous,
                    "drivers": drivers,
                },
                alert_key=f"market:regime:{since}:{latest['regime']}",
            )
        )
        if watchlist and _REGIME_RANK.get(latest["regime"], 0) >= _REGIME_RANK["stressed"]:
            signals.extend(_fragile_watchlist_signal(conn, watchlist=watchlist, regime=latest["regime"], since=since, now=now))

    signals.extend(_market_thesis_signals(conn, watchlist=watchlist, now=now))
    signals.extend(_crash_risk_signals(conn, threshold=cfg.market.crash_risk_alert_probability, now=now))
    signals.extend(_discovery_signals(conn, now=now))

    breadth = conn.execute(
        """
        SELECT fiscal_year, filer_count, negative_ocf_share, negative_equity_share, net_loss_share,
               change_pts, level, as_of_date
        FROM fundamental_breadth
        WHERE scope = 'market'
          AND level != 'insufficient_data'
          AND as_of_date = (SELECT MAX(as_of_date) FROM fundamental_breadth)
        ORDER BY fiscal_year DESC
        LIMIT 1
        """
    ).fetchone()
    if breadth is not None and level_at_least(breadth["level"], cfg.market.breadth_alert_level):
        shares = ", ".join(
            f"{label} {breadth[column]:.0%}"
            for label, column in (
                ("negative cash flow", "negative_ocf_share"),
                ("negative equity", "negative_equity_share"),
                ("net loss", "net_loss_share"),
            )
            if breadth[column] is not None
        )
        signals.append(
            AlertSignal(
                document_id=None,
                source_name="market_risk",
                ticker=None,
                signal_kind="breadth",
                signal_name=f"fundamental_breadth:FY{breadth['fiscal_year']}:{breadth['level']}",
                severity=_BREADTH_SEVERITY.get(breadth["level"], "warning"),
                claim=(
                    f"Stress is spreading across {breadth['filer_count']} filers in FY{breadth['fiscal_year']}: "
                    f"up to {breadth['change_pts']:+.1f} pts versus the prior year ({shares})"
                ),
                local_path=None,
                original_url=None,
                updated_at=now.isoformat(),
                suggested_next_action="check which watchlist names share the stress",
                details={key: breadth[key] for key in breadth.keys()},
                alert_key=f"market:breadth:{breadth['fiscal_year']}:{breadth['level']}",
            )
        )

    if watchlist:
        placeholders = ",".join("?" for _ in watchlist)
        rows = conn.execute(
            f"""
            SELECT es.ticker, es.score, es.components_json, es.as_of_date
            FROM exposure_scores es
            WHERE es.ticker IN ({placeholders})
              AND es.as_of_date = (SELECT MAX(as_of_date) FROM exposure_scores)
            ORDER BY es.score DESC
            """,
            tuple(sorted(watchlist)),
        ).fetchall()
        threshold = cfg.market.exposure_alert_score
        for row in rows:
            if row["score"] < threshold:
                continue
            components = json.loads(row["components_json"]).get("components", [])
            reasons = [item["reason"] for item in sorted(components, key=lambda item: -item["points"]) if item["points"]]
            # One alert per band: a score drifting within its band stays quiet.
            band = "85+" if row["score"] >= 85 else f"{threshold:g}+"
            signals.append(
                AlertSignal(
                    document_id=None,
                    source_name="market_risk",
                    ticker=row["ticker"],
                    signal_kind="exposure",
                    signal_name=f"exposure:{band}",
                    severity="high" if row["score"] >= 85 else "warning",
                    claim=f"Exposure {row['score']:.0f}/100: " + "; ".join(reasons[:3]),
                    local_path=None,
                    original_url=None,
                    updated_at=now.isoformat(),
                    suggested_next_action="review the position's risk",
                    details={"score": row["score"], "as_of_date": row["as_of_date"], "components": components},
                    alert_key=f"market:exposure:{row['ticker']}:{band}",
                )
            )

    floor = _SEVERITY_ORDER.get(cfg.alerts.min_severity, _SEVERITY_ORDER["warning"])
    return [signal for signal in signals if _SEVERITY_ORDER.get(signal.severity, 1) >= floor]


def _load_signals(
    conn: sqlite3.Connection,
    *,
    watchlist: set[str],
    min_severity: str,
    lookback_days: int,
    now: datetime,
) -> list[AlertSignal]:
    since = (now - timedelta(days=lookback_days)).isoformat()
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
          AND te.document_id IS NOT NULL  -- market theses alert from _load_market_signals
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
    alert_key = signal.alert_key or f"{signal.document_id}:{signal.signal_kind}:{signal.signal_name}:{signal.severity}"
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
