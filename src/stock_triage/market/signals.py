"""Market signals and the daily market state (docs/market_risk_plan.md, component 2).

Deterministic and versioned, like rules: each trading day gets a level per
signal, and a state from those levels. Every driver carries the numbers and a
plain sentence, so alerts and the dashboard can say why.
"""

from __future__ import annotations

import json
import math
import sqlite3
from bisect import bisect_right
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from statistics import median, pstdev

from ..config import Config
from ..db import WriterQueue, configure_pragmas, connect, run_migrations
from .catalysts import Catalyst
from .idx_market import FOREIGN_NET_SERIES
from .jisdor import SERIES_ID as USDIDR_SERIES

# v2: active themes and exchange notices join the signals; the sector signal counts
# the share of sectors in drawdown; a two-signal crisis needs IHSG itself down 10%.
RULESET_VERSION = "market-state-v2"
LEVELS = ("none", "elevated", "stressed", "crisis")
REGIME_OF_LEVEL = {"none": "calm", "elevated": "elevated", "stressed": "stressed", "crisis": "crisis"}
_REGIME_RANK = {"calm": 0, "elevated": 1, "stressed": 2, "crisis": 3}
_YEAR_SESSIONS = 252
_MIN_HISTORY = 20
# The notices start in October 2023; their usual level needs this much history first.
_NOTICE_MIN_HISTORY = 120
# Signal name -> threshold key in ``market.thresholds``.
SIGNAL_THRESHOLDS = {
    "drawdown": "drawdown_pct",
    "vol_ratio": "vol_ratio",
    "foreign_sell_streak": "foreign_sell_streak",
    "usdidr_change_20d": "usdidr_change_20d_pct",
    "decline_share": "decline_share_pct",
    "sector_drawdown": "sector_drawdown_pct",
    "sector_breadth": "sector_breadth_pct",
    "active_themes": "active_themes",
    "exchange_notices": "exchange_notices_ratio",
}


@dataclass(frozen=True)
class SignalReading:
    signal_name: str
    value: float | None
    level: str  # none | elevated | stressed | crisis | no_data
    inputs: dict[str, object] = field(default_factory=dict)
    text: str = ""


@dataclass(frozen=True)
class MarketDay:
    trade_date: date
    regime: str
    raw_regime: str
    readings: list[SignalReading]
    event_risk: list[dict[str, object]]

    @property
    def drivers(self) -> list[SignalReading]:
        active = [reading for reading in self.readings if reading.level in LEVELS[1:]]
        return sorted(active, key=lambda reading: (-LEVELS.index(reading.level), reading.signal_name))


@dataclass(frozen=True)
class MarketSignalsResult:
    days: int
    latest: MarketDay | None
    theses: list = field(default_factory=list)  # market thesis evaluations for the latest day


def compute_market_days(
    series: dict[str, dict[date, float]],
    *,
    thresholds: dict[str, dict[str, float]],
    sectors: list[str],
    min_signals: int,
    step_down_after: int,
    fundamental: dict[str, object] | None = None,
    catalysts: list[Catalyst] = (),
    lead_days: int = 3,
    active_themes: dict[date, list[str]] | None = None,
    sector_mode: str = "worst",
    sector_cutoff_pct: float = 15.0,
    crisis_needs_index: str | None = None,
) -> list[MarketDay]:
    """Each day's signals and state.

    ``sector_mode`` picks the sector signal: ``worst`` (the worst sector's drawdown)
    or ``share`` (the share of sectors at least ``sector_cutoff_pct`` below their
    high). ``crisis_needs_index`` set to a level makes a two-signal crisis also need
    IHSG's own drawdown at that level.
    """
    composite = series.get("index:COMPOSITE", {})
    trade_dates = sorted(composite)
    closes = [composite[day] for day in trade_dates]
    returns = [math.log(closes[i] / closes[i - 1]) if i and closes[i - 1] > 0 else 0.0 for i in range(len(closes))]
    rolling_vol: list[float | None] = [
        pstdev(returns[i - 19 : i + 1]) if i >= 20 else None for i in range(len(closes))
    ]
    foreign = series.get(FOREIGN_NET_SERIES, {})
    usdidr = series.get(USDIDR_SERIES, {})
    usdidr_dates = sorted(usdidr)
    advancers, decliners = series.get("breadth:advancers", {}), series.get("breadth:decliners", {})
    sector_history = {
        sector: (sorted(series[f"index:{sector}"]), series[f"index:{sector}"])
        for sector in sectors
        if f"index:{sector}" in series
    }
    # Annual figures are known once that year's reports are due, not before.
    fundamental_from = (
        date(int(fundamental["fiscal_year"]) + 1, 3, 31) if fundamental is not None else None  # type: ignore[arg-type]
    )

    suspend, uma = series.get("notices:suspend", {}), series.get("notices:uma", {})
    notice_counts = {day: suspend.get(day, 0.0) + uma.get(day, 0.0) for day in suspend.keys() | uma.keys()}
    # Theme activity exists only from the first watched item, not before.
    themes_from = min(active_themes) if active_themes else None

    days: list[MarketDay] = []
    state, below = "calm", []
    for i, day in enumerate(trade_dates):
        readings = [
            _drawdown(closes, i),
            _vol_ratio(rolling_vol, i),
            _foreign_streak(foreign, trade_dates, i),
            _usdidr_change(usdidr, usdidr_dates, trade_dates, i),
            _decline_share(advancers, decliners, trade_dates, i),
            _sector_drawdown(sector_history, day)
            if sector_mode == "worst"
            else _sector_breadth(sector_history, day, cutoff_pct=sector_cutoff_pct),
            _exchange_notices(notice_counts, trade_dates, i),
        ]
        readings = [_with_level(reading, thresholds) for reading in readings]
        if fundamental is not None and fundamental_from is not None and day >= fundamental_from:
            readings.append(_fundamental_reading(fundamental))
        if active_themes and day >= themes_from:  # type: ignore[operator]
            readings.append(_with_level(_themes_reading(active_themes.get(day, [])), thresholds))

        raw = "calm"
        for level in ("crisis", "stressed", "elevated"):
            reached = sum(1 for reading in readings if reading.level in LEVELS and LEVELS.index(reading.level) >= LEVELS.index(level))
            if reached >= min_signals:
                raw = REGIME_OF_LEVEL[level]
                break
        if raw == "crisis" and crisis_needs_index and not _at_least(readings[0].level, crisis_needs_index):
            raw = "stressed"  # a crisis needs the index itself to have fallen that far
        if readings[0].level == "crisis":
            raw = "crisis"  # a crash in the index itself is enough on its own

        if _REGIME_RANK[raw] >= _REGIME_RANK[state]:
            state, below = raw, []
        else:
            below.append(raw)
            if len(below) >= step_down_after:
                state, below = max(below[-step_down_after:], key=_REGIME_RANK.__getitem__), []

        window_end = day + timedelta(days=lead_days)
        event_risk = [
            {"date": item.catalyst_date.isoformat(), "kind": item.kind, "title": item.title}
            for item in catalysts
            if day <= item.catalyst_date <= window_end and item.severity == "high"
        ]
        days.append(MarketDay(trade_date=day, regime=state, raw_regime=raw, readings=readings, event_risk=event_risk))
    return days


def run_market_signals(cfg: Config) -> MarketSignalsResult:
    cfg.ensure_dirs()
    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
        reader = connect(cfg.paths.db_path, read_only=True)
        try:
            configure_pragmas(reader, cfg)
            series, days = market_days_from_db(reader, cfg)
        finally:
            reader.close()
        wq.submit(lambda conn: _persist_days(conn, days))
        theses = wq.submit(lambda conn: evaluate_and_store_market_theses(conn, cfg))
    return MarketSignalsResult(days=len(days), latest=days[-1] if days else None, theses=theses)


def evaluate_and_store_market_theses(conn: sqlite3.Connection, cfg: Config) -> list:
    """Re-check market theses against the newest stored state (component 7)."""
    from ..intelligence.thesis import load_local_theses, sync_local_theses
    from .theses import run_market_thesis_evaluation

    # The same file mirror intelligence-run applies, so an edited thesis takes effect here too.
    sync_local_theses(conn, theses=load_local_theses(cfg.theses.definition_path))
    return run_market_thesis_evaluation(conn, watchlist={ticker.upper() for ticker in cfg.alerts.watchlist})


def market_days_from_db(
    conn: sqlite3.Connection, cfg: Config
) -> tuple[dict[str, dict[date, float]], list[MarketDay]]:
    """The stored series and every day's signals and state, as ``market-signals`` computes them."""
    from .discovery import active_high_themes_by_day
    from .themes import load_themes

    series = load_market_series(conn)
    themes = active_high_themes_by_day(
        conn, themes=load_themes(conn, cfg.market.risk_themes_path), min_items=cfg.market.theme_active_min_items
    )
    days = compute_market_days(
        series,
        thresholds=cfg.market.thresholds,
        sectors=cfg.market.sectors,
        min_signals=cfg.market.regime_min_signals,
        step_down_after=cfg.market.regime_step_down_after_sessions,
        fundamental=_latest_fundamental_breadth(conn),
        catalysts=_all_catalysts(conn),
        lead_days=cfg.market.catalyst_lead_days,
        active_themes=themes,
        sector_mode="share" if cfg.market.sector_signal == "share" else "worst",
        sector_cutoff_pct=cfg.market.sector_breadth_cutoff_pct,
        crisis_needs_index=cfg.market.regime_crisis_needs_index,
    )
    return series, days


def load_market_series(conn: sqlite3.Connection) -> dict[str, dict[date, float]]:
    series: dict[str, dict[date, float]] = {}
    for row in conn.execute("SELECT series_id, trade_date, value FROM market_series_daily"):
        series.setdefault(row["series_id"], {})[date.fromisoformat(row["trade_date"])] = float(row["value"])
    return series


def load_market_state(conn: sqlite3.Connection, *, history: int = 30) -> dict[str, object] | None:
    """The latest market state with its drivers, plus recent states for context."""
    rows = conn.execute(
        """
        SELECT trade_date, regime, raw_regime, drivers_json
        FROM market_regime_days
        ORDER BY trade_date DESC
        LIMIT ?
        """,
        (history,),
    ).fetchall()
    if not rows:
        return None
    latest = rows[0]
    payload = json.loads(latest["drivers_json"])
    composite = conn.execute(
        "SELECT value FROM market_series_daily WHERE series_id = 'index:COMPOSITE' AND trade_date = ?",
        (latest["trade_date"],),
    ).fetchone()
    return {
        "trade_date": latest["trade_date"],
        "regime": latest["regime"],
        "raw_regime": latest["raw_regime"],
        "composite_close": composite["value"] if composite else None,
        "drivers": payload.get("drivers", []),
        "readings": payload.get("readings", []),
        "event_risk": payload.get("event_risk", []),
        "ruleset_version": RULESET_VERSION,
        "recent": [{"trade_date": row["trade_date"], "regime": row["regime"]} for row in reversed(rows)],
    }


# --- signals ---------------------------------------------------------------


def _drawdown(closes: list[float], i: int) -> SignalReading:
    if i + 1 < _MIN_HISTORY:
        return SignalReading("drawdown", None, "no_data", {"sessions": i + 1})
    window = closes[max(0, i - _YEAR_SESSIONS + 1) : i + 1]
    peak = max(window)
    value = (peak - closes[i]) / peak * 100.0
    return SignalReading(
        "drawdown",
        round(value, 2),
        "none",
        {"close": closes[i], "peak": peak, "sessions": len(window)},
        f"IHSG {value:.1f}% below its {_high_label(len(window))} ({closes[i]:,.0f} vs {peak:,.0f})",
    )


def _vol_ratio(rolling_vol: list[float | None], i: int) -> SignalReading:
    history = [value for value in rolling_vol[max(0, i - _YEAR_SESSIONS + 1) : i + 1] if value is not None]
    current = rolling_vol[i]
    if current is None or len(history) < 60:
        return SignalReading("vol_ratio", None, "no_data", {"history": len(history)})
    usual = median(history)
    value = current / usual if usual > 0 else 0.0
    return SignalReading(
        "vol_ratio",
        round(value, 2),
        "none",
        {"vol_20d": current, "median_vol_20d": usual},
        f"Volatility {value:.1f}x its usual level (20-session)",
    )


def _foreign_streak(foreign: dict[date, float], trade_dates: list[date], i: int) -> SignalReading:
    if trade_dates[i] not in foreign:
        return SignalReading("foreign_sell_streak", None, "no_data")
    streak, total = 0, 0.0
    for day in reversed(trade_dates[: i + 1]):
        value = foreign.get(day)
        if value is None or value >= 0:
            break
        streak += 1
        total += value
    return SignalReading(
        "foreign_sell_streak",
        float(streak),
        "none",
        {"net_value_est": foreign[trade_dates[i]], "streak_net_value_est": total},
        f"Foreign investors net sellers {streak} sessions running (about Rp {abs(total) / 1e12:,.1f} tn, estimated)"
        if streak
        else "Foreign investors net buyers today (estimated)",
    )


def _usdidr_change(
    usdidr: dict[date, float], usdidr_dates: list[date], trade_dates: list[date], i: int
) -> SignalReading:
    if i < 20 or not usdidr_dates:
        return SignalReading("usdidr_change_20d", None, "no_data")
    now, then = _as_of(usdidr, usdidr_dates, trade_dates[i]), _as_of(usdidr, usdidr_dates, trade_dates[i - 20])
    if now is None or then is None or then <= 0:
        return SignalReading("usdidr_change_20d", None, "no_data")
    value = (now / then - 1.0) * 100.0
    direction = "weaker" if value > 0 else "stronger"
    return SignalReading(
        "usdidr_change_20d",
        round(value, 2),
        "none",
        {"rate": now, "rate_20_sessions_ago": then},
        f"Rupiah {direction}: USD/IDR {value:+.1f}% in 20 sessions (Rp {now:,.0f})",
    )


def _decline_share(
    advancers: dict[date, float], decliners: dict[date, float], trade_dates: list[date], i: int
) -> SignalReading:
    shares = []
    for day in trade_dates[max(0, i - 4) : i + 1]:
        up, down = advancers.get(day), decliners.get(day)
        if up is not None and down is not None and up + down > 0:
            shares.append(down / (up + down) * 100.0)
    if len(shares) < 3:
        return SignalReading("decline_share", None, "no_data")
    value = sum(shares) / len(shares)
    return SignalReading(
        "decline_share",
        round(value, 1),
        "none",
        {"days": len(shares)},
        f"{value:.0f}% of moving stocks fell (5-session average)",
    )


def _sector_drawdown(
    sector_history: dict[str, tuple[list[date], dict[date, float]]], day: date
) -> SignalReading:
    worst: tuple[float, str, float, float, int] | None = None
    for sector, (dates, values) in sector_history.items():
        if day not in values:
            continue
        end = bisect_right(dates, day)
        history = [values[d] for d in dates[max(0, end - _YEAR_SESSIONS) : end]]
        if len(history) < _MIN_HISTORY:
            continue
        peak = max(history)
        drawdown = (peak - history[-1]) / peak * 100.0
        if worst is None or drawdown > worst[0]:
            worst = (drawdown, sector, history[-1], peak, len(history))
    if worst is None:
        return SignalReading("sector_drawdown", None, "no_data")
    drawdown, sector, close, peak, sessions = worst
    return SignalReading(
        "sector_drawdown",
        round(drawdown, 2),
        "none",
        {"sector": sector, "close": close, "peak": peak},
        f"{sector} {drawdown:.1f}% below its {_high_label(sessions)} (worst sector)",
    )


def _high_label(sessions: int) -> str:
    # Until a year is synced, the high is only over the sessions there are.
    return "52-week high" if sessions >= _YEAR_SESSIONS else f"{sessions}-session high"


def _exchange_notices(counts: dict[date, float], trade_dates: list[date], i: int) -> SignalReading:
    """Suspensions and UMA notices over five sessions, against the past year's usual five."""
    covered = [day for day in trade_dates[max(0, i - _YEAR_SESSIONS) : i + 1] if day in counts]
    if len(covered) < _NOTICE_MIN_HISTORY or trade_dates[i] not in counts:
        return SignalReading("exchange_notices", None, "no_data", {"sessions": len(covered)})
    sums = [sum(counts[day] for day in covered[k - 4 : k + 1]) for k in range(4, len(covered))]
    current, usual = sums[-1], median(sums[:-1])
    ratio = current / max(usual, 1.0)
    return SignalReading(
        "exchange_notices",
        round(ratio, 2),
        "none",
        {"five_sessions": current, "usual": usual},
        f"IDX suspended or flagged {current:.0f} stocks in 5 sessions ({ratio:.1f}x usual)",
    )


def _sector_breadth(
    sector_history: dict[str, tuple[list[date], dict[date, float]]], day: date, *, cutoff_pct: float
) -> SignalReading:
    """The share of tracked sectors at least ``cutoff_pct`` below their 52-week high."""
    falling: list[str] = []
    counted = 0
    for sector, (dates, values) in sector_history.items():
        if day not in values:
            continue
        end = bisect_right(dates, day)
        history = [values[d] for d in dates[max(0, end - _YEAR_SESSIONS) : end]]
        if len(history) < _MIN_HISTORY:
            continue
        counted += 1
        peak = max(history)
        if peak and (peak - history[-1]) / peak * 100.0 >= cutoff_pct:
            falling.append(sector)
    if counted < 5:
        return SignalReading("sector_breadth", None, "no_data", {"sectors": counted})
    share = len(falling) / counted * 100.0
    return SignalReading(
        "sector_breadth",
        round(share, 1),
        "none",
        {"falling": falling, "sectors": counted, "cutoff_pct": cutoff_pct},
        f"{len(falling)} of {counted} sectors at least {cutoff_pct:g}% below their 52-week high",
    )


def _at_least(level: str, target: str) -> bool:
    return level in LEVELS and target in LEVELS and LEVELS.index(level) >= LEVELS.index(target)


def _themes_reading(titles: list[str]) -> SignalReading:
    count = len(titles)
    text = (
        f"{count} high-severity risk theme{'s' if count != 1 else ''} active: {', '.join(titles[:3])}"
        if count
        else "No high-severity risk theme active"
    )
    return SignalReading("active_themes", float(count), "none", {"themes": titles}, text)


def _fundamental_reading(fundamental: dict[str, object]) -> SignalReading:
    level = str(fundamental["level"])
    return SignalReading(
        "fundamental_breadth",
        fundamental.get("change_pts"),  # type: ignore[arg-type]
        level if level in LEVELS else "no_data",
        {"fiscal_year": fundamental["fiscal_year"], "as_of_date": fundamental["as_of_date"]},
        f"Stress spreading across filers: {fundamental['change_pts']:+.1f} pts in FY{fundamental['fiscal_year']}",
    )


def _with_level(reading: SignalReading, thresholds: dict[str, dict[str, float]]) -> SignalReading:
    if reading.value is None:
        return reading
    limits = thresholds.get(SIGNAL_THRESHOLDS[reading.signal_name], {})
    level = "none"
    for candidate in ("crisis", "stressed", "elevated"):
        if reading.value >= limits.get(candidate, math.inf):
            level = candidate
            break
    return SignalReading(reading.signal_name, reading.value, level, reading.inputs, reading.text)


def _as_of(values: dict[date, float], sorted_dates: list[date], day: date) -> float | None:
    """The latest value on or before ``day`` (rates are not published every trading day)."""
    position = bisect_right(sorted_dates, day)
    return values[sorted_dates[position - 1]] if position else None


# --- storage -----------------------------------------------------------------


def _latest_fundamental_breadth(conn: sqlite3.Connection) -> dict[str, object] | None:
    row = conn.execute(
        """
        SELECT fiscal_year, change_pts, level, as_of_date
        FROM fundamental_breadth
        WHERE scope = 'market'
          AND level != 'insufficient_data'
          AND as_of_date = (SELECT MAX(as_of_date) FROM fundamental_breadth)
        ORDER BY fiscal_year DESC
        LIMIT 1
        """
    ).fetchone()
    return dict(row) if row is not None else None


def _all_catalysts(conn: sqlite3.Connection) -> list[Catalyst]:
    return [
        Catalyst(
            catalyst_id=row["catalyst_id"],
            catalyst_date=date.fromisoformat(row["catalyst_date"]),
            kind=row["kind"],
            title=row["title"],
            severity=row["severity"],
        )
        for row in conn.execute("SELECT catalyst_id, catalyst_date, kind, title, severity FROM market_catalysts")
    ]


def _persist_days(conn: sqlite3.Connection, days: list[MarketDay]) -> None:
    evaluated_at = datetime.now(timezone.utc).isoformat()
    conn.execute("DELETE FROM market_signal_runs")
    conn.execute("DELETE FROM market_regime_days")
    conn.executemany(
        """
        INSERT INTO market_signal_runs (trade_date, signal_name, value, level, inputs_json, ruleset_version, evaluated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                day.trade_date.isoformat(),
                reading.signal_name,
                reading.value,
                reading.level,
                json.dumps(reading.inputs, sort_keys=True, default=str),
                RULESET_VERSION,
                evaluated_at,
            )
            for day in days
            for reading in day.readings
        ],
    )
    conn.executemany(
        """
        INSERT INTO market_regime_days (trade_date, regime, raw_regime, drivers_json, ruleset_version, evaluated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                day.trade_date.isoformat(),
                day.regime,
                day.raw_regime,
                json.dumps(
                    {
                        "drivers": [_reading_payload(reading) for reading in day.drivers],
                        "readings": [_reading_payload(reading) for reading in day.readings],
                        "event_risk": day.event_risk,
                    },
                    sort_keys=True,
                    default=str,
                ),
                RULESET_VERSION,
                evaluated_at,
            )
            for day in days
        ],
    )


def _reading_payload(reading: SignalReading) -> dict[str, object]:
    payload = asdict(reading)
    payload.pop("inputs", None)
    return payload
