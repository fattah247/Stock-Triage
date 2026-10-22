"""``market-backtest``: replay the state and the estimate over history (component 9).

Every number comes from the same point-in-time computation the daily run uses,
so the report shows what the layer would have said on each day, not what it
says looking back. It is the basis for tuning thresholds, and every tuning
change should be judged against it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ..config import Config
from ..db import WriterQueue, configure_pragmas, connect, run_migrations
from .forecast import FEATURES, CrashRiskResult, Episode, compute_crash_risk, is_trustworthy
from .signals import LEVELS, MarketDay, market_days_from_db

# Sessions before a trough in which a signal reaching elevated counts as a warning.
LEAD_WINDOW = 126
_REGIME_RANK = {"calm": 0, "elevated": 1, "stressed": 2, "crisis": 3}


@dataclass(frozen=True)
class GoldenDate:
    day: date
    why: str
    at_least: str | None = None
    at_most: str | None = None


# Known days with the state they should read, checked by market-backtest and the
# golden set (intelligence-golden). Each is described from IDX's own closes.
GOLDEN_DATES = (
    GoldenDate(date(2020, 3, 24), "COVID low: IHSG at its lowest close of 2020", at_least="crisis"),
    GoldenDate(date(2024, 9, 19), "IHSG at its record close before the 2025 sell-off", at_most="elevated"),
    GoldenDate(date(2025, 4, 8), "first session after the 2025 Lebaran holiday, when IHSG fell about 8% and trading halted", at_least="crisis"),
    GoldenDate(date(2026, 10, 19), "October 2026 decline", at_least="crisis"),
)


@dataclass(frozen=True)
class GoldenResult:
    golden: GoldenDate
    trade_date: date | None
    regime: str | None
    passed: bool | None  # None: no data for that day

    @property
    def expectation(self) -> str:
        if self.golden.at_least:
            return f"at least {self.golden.at_least}"
        return f"at most {self.golden.at_most}"


@dataclass(frozen=True)
class EpisodeReport:
    episode: Episode
    state_lead: int | None  # sessions from the first elevated state to the trough
    signal_leads: dict[str, int | None]
    estimate_at_open: float | None
    estimate_peak_before_trough: float | None
    warned_sessions_before_trough: int | None  # first trustworthy warning, in sessions
    false_alarm: bool  # no further crash-sized fall followed the opening


@dataclass(frozen=True)
class BacktestReport:
    start: date
    end: date
    days: int
    regime_counts: dict[str, int]
    episodes: list[EpisodeReport]
    risk: CrashRiskResult
    golden: list[GoldenResult]
    missed_crashes: list[tuple[date, date]]  # crash stretches with no warning before them
    drawdown_pct: float
    horizon: int
    alert_probability: float
    min_skill: float


def run_market_backtest(cfg: Config, *, start: date | None = None, end: date | None = None) -> BacktestReport:
    cfg.ensure_dirs()
    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
    reader = connect(cfg.paths.db_path, read_only=True)
    try:
        configure_pragmas(reader, cfg)
        series, days = market_days_from_db(reader, cfg)
    finally:
        reader.close()
    # The state and the estimate need the history before ``start``; only the report is cut.
    if end is not None:
        days = [day for day in days if day.trade_date <= end]
    composite = series.get("index:COMPOSITE", {})
    return build_backtest(
        days,
        [composite[day.trade_date] for day in days],
        start=start,
        drawdown_pct=cfg.market.crash_risk_drawdown_pct,
        horizon=cfg.market.crash_risk_horizon_sessions,
        alert_probability=cfg.market.crash_risk_alert_probability,
        min_skill=cfg.market.crash_risk_min_skill,
        close_after_calm=cfg.market.episode_close_after_calm_sessions,
    )


def build_backtest(
    days: list[MarketDay],
    closes: list[float],
    *,
    start: date | None,
    drawdown_pct: float,
    horizon: int,
    alert_probability: float,
    min_skill: float,
    close_after_calm: int,
) -> BacktestReport:
    risk = compute_crash_risk(
        days,
        closes,
        horizon=horizon,
        drawdown_pct=drawdown_pct,
        alert_probability=alert_probability,
        min_skill=min_skill,
        close_after_calm=close_after_calm,
    )
    first = 0 if start is None else next((i for i, day in enumerate(days) if day.trade_date >= start), len(days))
    window = days[first:]
    regime_counts = {regime: sum(1 for day in window if day.regime == regime) for regime in _REGIME_RANK}
    reports = [
        _episode_report(episode, days, risk, closes, drawdown_pct=drawdown_pct, horizon=horizon, alert_probability=alert_probability)
        for episode in risk.episodes
        if episode.open_index >= first
    ]
    return BacktestReport(
        start=window[0].trade_date if window else (start or date.today()),
        end=window[-1].trade_date if window else (start or date.today()),
        days=len(window),
        regime_counts=regime_counts,
        episodes=reports,
        risk=risk,
        golden=golden_results(days),
        missed_crashes=_missed_crashes(risk, alert_probability=alert_probability, first=first, horizon=horizon),
        drawdown_pct=drawdown_pct,
        horizon=horizon,
        alert_probability=alert_probability,
        min_skill=min_skill,
    )


def golden_results(days: list[MarketDay]) -> list[GoldenResult]:
    by_date = {day.trade_date: day for day in days}
    ordered = sorted(by_date)
    results = []
    for golden in GOLDEN_DATES:
        # A golden date on a holiday reads the session before it.
        candidates = [d for d in ordered if d <= golden.day and (golden.day - d).days <= 5]
        if not candidates:
            results.append(GoldenResult(golden, None, None, None))
            continue
        day = by_date[candidates[-1]]
        rank = _REGIME_RANK[day.regime]
        if golden.at_least:
            passed = rank >= _REGIME_RANK[golden.at_least]
        else:
            passed = rank <= _REGIME_RANK[golden.at_most or "crisis"]
        results.append(GoldenResult(golden, day.trade_date, day.regime, passed))
    return results


def golden_cases_from_db(conn: sqlite3.Connection) -> list[dict[str, str]]:
    """The golden dates against the stored state, as golden-set cases."""
    rows = {row["trade_date"]: row["regime"] for row in conn.execute("SELECT trade_date, regime FROM market_regime_days")}
    cases = []
    for golden in GOLDEN_DATES:
        known = [d for d in rows if d <= golden.day.isoformat() and (golden.day - date.fromisoformat(d)).days <= 5]
        name = f"market_state_{golden.day.isoformat()}"
        if not known:
            cases.append({"case_name": name, "status": "warning", "detail": f"no market data for {golden.day}; run market-sync"})
            continue
        trade_date = max(known)
        regime = rows[trade_date]
        rank = _REGIME_RANK[regime]
        expected = f"at least {golden.at_least}" if golden.at_least else f"at most {golden.at_most}"
        passed = rank >= _REGIME_RANK[golden.at_least] if golden.at_least else rank <= _REGIME_RANK[golden.at_most or "crisis"]
        cases.append(
            {
                "case_name": name,
                "status": "pass" if passed else "fail",
                "detail": f"{golden.why}: state {regime} on {trade_date}, expected {expected}",
            }
        )
    return cases


def _episode_report(
    episode: Episode,
    days: list[MarketDay],
    risk: CrashRiskResult,
    closes: list[float],
    *,
    drawdown_pct: float,
    horizon: int,
    alert_probability: float,
) -> EpisodeReport:
    trough = episode.trough_index if episode.trough_index is not None else episode.open_index
    lookback = range(max(0, trough - LEAD_WINDOW), trough + 1)
    state_first = next((i for i in lookback if _REGIME_RANK[days[i].regime] >= 1), None)
    leads: dict[str, int | None] = {}
    for name in [reading.signal_name for reading in days[episode.open_index].readings]:
        first_index = next(
            (
                i
                for i in lookback
                for reading in days[i].readings
                if reading.signal_name == name and reading.level in LEVELS[1:]
            ),
            None,
        )
        leads[name] = None if first_index is None else trough - first_index
    probabilities = [risk.days[i].probability for i in lookback]
    known = [p for p in probabilities if p is not None]
    warned = next(
        (i for i in lookback if risk.days[i].trustworthy and (risk.days[i].probability or 0.0) >= alert_probability),
        None,
    )
    later = closes[episode.open_index + 1 : episode.open_index + horizon + 1]
    false_alarm = not later or min(later) > episode.open_close * (1.0 - drawdown_pct / 100.0)
    return EpisodeReport(
        episode=episode,
        state_lead=None if state_first is None else trough - state_first,
        signal_leads=leads,
        estimate_at_open=risk.days[episode.open_index].probability,
        estimate_peak_before_trough=max(known) if known else None,
        warned_sessions_before_trough=None if warned is None else trough - warned,
        false_alarm=false_alarm,
    )


def _missed_crashes(
    risk: CrashRiskResult, *, alert_probability: float, first: int, horizon: int
) -> list[tuple[date, date]]:
    """Stretches of days a crash followed, with no warning in the horizon before."""
    stretches: list[tuple[int, int]] = []
    for i, label in enumerate(risk.labels):
        if i < first or label != 1:
            continue
        if stretches and stretches[-1][1] == i - 1:
            stretches[-1] = (stretches[-1][0], i)
        else:
            stretches.append((i, i))
    missed = []
    for begin, finish in stretches:
        before = range(max(0, begin - horizon), finish + 1)
        if not any((risk.days[i].probability or 0.0) >= alert_probability for i in before):
            missed.append((risk.days[begin].trade_date, risk.days[finish].trade_date))
    return missed


def render_report(report: BacktestReport) -> str:
    """The backtest as Markdown, for ``data/reports/market_backtest.md``."""
    record = report.risk.record
    trusted = is_trustworthy(record, min_skill=report.min_skill)
    lines = [
        "# Market backtest",
        "",
        f"{report.start} to {report.end}: {report.days} trading days.",
        f"A crash is a fall of {report.drawdown_pct:g}% or more from a day's close within {report.horizon} sessions.",
        "",
        "## State",
        "",
        "| State | Days | Share |",
        "|---|---:|---:|",
    ]
    for regime, count in report.regime_counts.items():
        share = count / report.days if report.days else 0.0
        lines.append(f"| {regime} | {count} | {share:.0%} |")
    lines += ["", "## Golden dates", "", "| Date | Expected | State | Result | Why |", "|---|---|---|---|---|"]
    for result in report.golden:
        outcome = "no data" if result.passed is None else ("pass" if result.passed else "**fail**")
        lines.append(
            f"| {result.golden.day} | {result.expectation} | {result.regime or '-'}"
            f"{f' ({result.trade_date})' if result.trade_date and result.trade_date != result.golden.day else ''} | {outcome} | {result.golden.why} |"
        )
    lines += [
        "",
        "## Crash-risk estimate (walk-forward)",
        "",
        f"- Days scored: {record.scored_days}, of which {record.crash_days} were followed by a crash.",
        f"- Brier score: {_fmt(record.brier, 4)} against {_fmt(record.base_rate_brier, 4)} for the base rate"
        f" (skill {_pct(record.skill)}; needs {report.min_skill:.0%} to count).",
        f"- Warnings at {report.alert_probability:.0%} or more: hit rate {_pct(record.hit_rate)},"
        f" false alarms {_pct(record.false_alarm_rate)} of warnings.",
        f"- Verdict: {'shown as a forecast' if trusted else 'not trustworthy, so only analogs are shown'}.",
        "",
        "## Episodes",
        "",
        "| Opened | Closed | Worst | Max drawdown | Trough | Sessions to trough | State lead | Estimate at open | Warned before trough | False alarm |",
        "|---|---|---|---:|---|---:|---:|---:|---:|---|",
    ]
    for item in report.episodes:
        episode = item.episode
        lines.append(
            f"| {episode.opened_on} | {episode.closed_on or 'open'} | {episode.worst_regime} | {_fmt(episode.max_drawdown_pct, 1)}% "
            f"| {episode.trough_date} | {(episode.trough_index or episode.open_index) - episode.open_index} "
            f"| {_sessions(item.state_lead)} | {_pct(item.estimate_at_open)} | {_sessions(item.warned_sessions_before_trough)} "
            f"| {'yes' if item.false_alarm else 'no'} |"
        )
    if report.episodes:
        names = list(report.episodes[0].signal_leads)
        lines += [
            "",
            f"### How early each signal fired (sessions before the trough, within {LEAD_WINDOW})",
            "",
            "| Opened | " + " | ".join(names) + " |",
            "|---|" + "---:|" * len(names),
        ]
        for item in report.episodes:
            lines.append(f"| {item.episode.opened_on} | " + " | ".join(_sessions(item.signal_leads.get(name)) for name in names) + " |")
    lines += [
        "",
        "## Crash stretches the estimate did not warn of",
        "",
        f"Days a crash followed, with no estimate of {report.alert_probability:.0%} or more in the {report.horizon} sessions before.",
        "",
    ]
    if report.missed_crashes:
        lines += [f"- {begin} to {finish}" for begin, finish in report.missed_crashes]
    else:
        lines.append("- None.")
    lines += ["", f"Features: {', '.join(FEATURES)}.", ""]
    return "\n".join(lines)


def write_report(report: BacktestReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report(report), encoding="utf-8")
    return path


def _fmt(value: float | None, digits: int) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _sessions(value: int | None) -> str:
    return "-" if value is None else str(value)
