"""The dashboard's Market view: IHSG charts, signals, crash risk, episodes and themes.

Loaded on its own (``/api/market``) when the Market tab opens, so the main
snapshot stays light.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from ..config import Config
from ..db import WriterQueue, configure_pragmas, connect, run_migrations
from ..market.catalysts import load_upcoming_catalysts
from ..market.discovery import active_themes, load_open_findings, load_theme_headlines
from ..market.exchange_notices import load_recent_notices
from ..market.forecast import FEATURE_LABELS, TrackRecord, is_trustworthy, load_crash_risk, load_episodes
from ..market.idx_market import FOREIGN_NET_SERIES
from ..market.jisdor import SERIES_ID as USDIDR_SERIES
from ..market.sectors import SECTOR_LABELS
from ..market.signals import load_market_series
from ..market.themes import ThemeFileError, load_themes

_YEAR = 252


def load_market_view(cfg: Config) -> dict[str, object]:
    cfg.ensure_dirs()
    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
    conn = connect(cfg.paths.db_path, read_only=True)
    try:
        configure_pragmas(conn, cfg)
        return _build(conn, cfg)
    finally:
        conn.close()


def _build(conn, cfg: Config) -> dict[str, object]:
    series = load_market_series(conn)
    composite = series.get("index:COMPOSITE", {})
    dates = sorted(composite)
    today = (datetime.now(timezone.utc) + timedelta(hours=7)).date()
    if not dates:
        return {"has_data": False, "calendar": _calendar(conn, cfg, today=today)}
    closes = [composite[day] for day in dates]
    regimes = dict(conn.execute("SELECT trade_date, regime FROM market_regime_days").fetchall())
    foreign = series.get(FOREIGN_NET_SERIES, {})
    usdidr = series.get(USDIDR_SERIES, {})
    crash_rows = {
        row["trade_date"]: row
        for row in conn.execute("SELECT trade_date, probability, base_rate, trustworthy FROM crash_risk_days")
    }

    peaks = []
    for i in range(len(closes)):
        peaks.append(max(closes[max(0, i - _YEAR + 1) : i + 1]))
    daily = {
        "dates": [day.isoformat() for day in dates],
        "close": closes,
        "ma50": _moving_average(closes, 50),
        "ma200": _moving_average(closes, 200),
        "drawdown": [round((peak - close) / peak * 100.0, 2) if peak else None for close, peak in zip(closes, peaks)],
        "regime": [regimes.get(day.isoformat()) for day in dates],
        "foreign_net": [round(foreign[day] / 1e9, 1) if day in foreign else None for day in dates],  # Rp bn
        "crash_probability": [_crash_value(crash_rows.get(day.isoformat()), "probability") for day in dates],
        "crash_base_rate": [_crash_value(crash_rows.get(day.isoformat()), "base_rate") for day in dates],
    }
    usd_dates = sorted(usdidr)
    rupiah = {"dates": [day.isoformat() for day in usd_dates], "rate": [usdidr[day] for day in usd_dates]}

    latest = dates[-1]
    state_row = conn.execute(
        "SELECT trade_date, regime, drivers_json FROM market_regime_days ORDER BY trade_date DESC LIMIT 1"
    ).fetchone()
    state = None
    if state_row is not None:
        payload = json.loads(state_row["drivers_json"])
        state = {
            "trade_date": state_row["trade_date"],
            "regime": state_row["regime"],
            "drivers": payload.get("drivers", []),
            "readings": payload.get("readings", []),
            "event_risk": payload.get("event_risk", []),
            "since": _regime_since(conn, state_row["regime"]),
        }

    risk = load_crash_risk(conn, history=1)
    if risk is not None:
        record = risk.get("track_record") or {}
        track = TrackRecord(
            brier=record.get("brier"),
            base_rate_brier=record.get("base_rate_brier"),
            hit_rate=record.get("hit_rate"),
            false_alarm_rate=record.get("false_alarm_rate"),
            scored_days=int(record.get("scored_days") or 0),
            crash_days=int(record.get("crash_days") or 0),
            evaluated_through=None,
        )
        risk = {
            **{key: value for key, value in risk.items() if key != "history"},
            "skill": track.skill,
            "record_trustworthy": is_trustworthy(track, min_skill=cfg.market.crash_risk_min_skill),
            "feature_labels": FEATURE_LABELS,
            "definition": {
                "drawdown_pct": cfg.market.crash_risk_drawdown_pct,
                "horizon_sessions": cfg.market.crash_risk_horizon_sessions,
                "alert_probability": cfg.market.crash_risk_alert_probability,
                "min_skill": cfg.market.crash_risk_min_skill,
            },
        }

    try:
        themes = load_themes(conn, cfg.market.risk_themes_path)
        theme_rows = active_themes(conn, themes=themes, as_of=today, min_items=cfg.market.theme_active_min_items)
    except ThemeFileError:
        themes, theme_rows = [], []
    for item in theme_rows:
        item["headlines"] = load_theme_headlines(conn, theme_id=str(item["theme_id"]), limit=3)

    return {
        "has_data": True,
        "latest": _latest_stats(daily, rupiah, foreign, dates),
        "daily": daily,
        "rupiah": rupiah,
        "state": state,
        "crash_risk": risk,
        "sectors": _sectors(series, cfg.market.sectors, latest),
        "episodes": load_episodes(conn),
        "themes": {"tracked": len(themes), "active": theme_rows},
        "findings": load_open_findings(conn, limit=15),
        "calendar": _calendar(conn, cfg, today=today),
        "notices": _notices(conn, series, latest),
    }


def _latest_stats(daily: dict[str, list], rupiah: dict[str, list], foreign: dict[date, float], dates: list[date]) -> dict[str, object]:
    closes = daily["close"]
    change = (closes[-1] / closes[-2] - 1.0) * 100.0 if len(closes) > 1 and closes[-2] else None
    rate = rupiah["rate"]
    rate_change = (rate[-1] / rate[-21] - 1.0) * 100.0 if len(rate) > 21 and rate[-21] else None
    recent = [foreign[day] for day in dates[-5:] if day in foreign]
    return {
        "trade_date": daily["dates"][-1],
        "close": closes[-1],
        "change_pct": change,
        "drawdown_pct": daily["drawdown"][-1],
        "peak": max(closes[-_YEAR:]),
        "ma200": daily["ma200"][-1],
        "usdidr": rate[-1] if rate else None,
        "usdidr_date": rupiah["dates"][-1] if rate else None,
        "usdidr_change_20_pct": rate_change,
        "foreign_net_5d_bn": round(sum(recent) / 1e9, 1) if recent else None,
    }


def _sectors(series: dict[str, dict[date, float]], sectors: list[str], latest: date) -> list[dict[str, object]]:
    rows = []
    for code in sectors:
        values = series.get(f"index:{code}", {})
        days = sorted(day for day in values if day <= latest)
        if len(days) < 2:
            continue
        closes = [values[day] for day in days]
        peak = max(closes[-_YEAR:])
        rows.append(
            {
                "code": code,
                "label": SECTOR_LABELS.get(code, code),
                "close": closes[-1],
                "change_1d_pct": (closes[-1] / closes[-2] - 1.0) * 100.0,
                "change_20d_pct": (closes[-1] / closes[-21] - 1.0) * 100.0 if len(closes) > 20 else None,
                "drawdown_pct": (peak - closes[-1]) / peak * 100.0 if peak else None,
                "spark": closes[-60:],
                "as_of": days[-1].isoformat(),
            }
        )
    return sorted(rows, key=lambda row: -(row["drawdown_pct"] or 0.0))


def _calendar(conn, cfg: Config, *, today: date) -> list[dict[str, object]]:
    watchlist = {ticker.upper() for ticker in cfg.alerts.watchlist}
    return [
        {"date": item.catalyst_date.isoformat(), "kind": item.kind, "title": item.title, "severity": item.severity, "ticker": item.ticker}
        for item in load_upcoming_catalysts(conn, today=today, days=90, watchlist=watchlist)[:20]
    ]


def _notices(conn, series: dict[str, dict[date, float]], latest: date) -> dict[str, object]:
    suspend, uma = series.get("notices:suspend", {}), series.get("notices:uma", {})
    days = sorted(day for day in suspend if day <= latest)[-20:]
    return {
        "recent": load_recent_notices(conn, days=10, today=latest)[:30],
        "daily": [{"date": day.isoformat(), "suspend": suspend.get(day, 0.0), "uma": uma.get(day, 0.0)} for day in days],
    }


def _regime_since(conn, regime: str) -> str | None:
    row = conn.execute(
        "SELECT MIN(trade_date) FROM market_regime_days WHERE trade_date > COALESCE("
        "(SELECT MAX(trade_date) FROM market_regime_days WHERE regime != ?), '')",
        (regime,),
    ).fetchone()
    return row[0] if row else None


def _crash_value(row, key: str) -> float | None:
    if row is None or row[key] is None:
        return None
    return round(float(row[key]), 4)


def _moving_average(values: list[float], window: int) -> list[float | None]:
    out: list[float | None] = []
    total = 0.0
    for i, value in enumerate(values):
        total += value
        if i >= window:
            total -= values[i - window]
        out.append(round(total / window, 2) if i >= window - 1 else None)
    return out
