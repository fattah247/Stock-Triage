from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from stock_triage.alerts import run_alerts
from stock_triage.db import WriterQueue, connect, run_migrations
from stock_triage.intelligence.review import run_golden_set
from stock_triage.market import run_market_signals
from stock_triage.market.backtest import GOLDEN_DATES, build_backtest, golden_results, render_report
from stock_triage.market.forecast import (
    FEATURES,
    DayFeatures,
    Episode,
    build_features,
    crash_labels,
    detect_episodes,
    find_analogs,
    fit_model,
    load_crash_risk,
    record_episode_note,
    run_crash_risk,
    walk_forward,
)
from stock_triage.market.signals import MarketDay, SignalReading, compute_market_days

from .test_market_data import _THRESHOLDS, _seed_series, _weekdays


def _cyclic_market(sessions: int = 1100) -> dict[str, dict[date, float]]:
    """A market that crashes every 180 sessions, each crash preceded by 15 sessions of foreign selling."""
    days = _weekdays(date(2010, 1, 4), sessions)  # clear of every golden date
    composite, foreign = {}, {}
    level = 5000.0
    for i, day in enumerate(days):
        phase = i % 180
        if 120 <= phase < 135:
            foreign[day] = -2e11  # the warning: foreigners sell for 15 sessions
            level *= 1.0005
        elif 135 <= phase < 150:
            foreign[day] = -1e11
            level *= 0.985  # about 20% down over 15 sessions
        else:
            foreign[day] = 1e11
            level *= 1.0025 if phase >= 150 else 1.0006
        composite[day] = round(level, 2)
    return {"index:COMPOSITE": composite, "flow:foreign_net_value_est": foreign}


def _market_days(series: dict[str, dict[date, float]]) -> list[MarketDay]:
    return compute_market_days(series, thresholds=_THRESHOLDS, sectors=[], min_signals=2, step_down_after=5)


def test_a_crash_label_looks_only_inside_the_horizon():
    closes = [100.0, 99.0, 95.0, 89.0, 91.0, 92.0]

    # Day 0 falls to 89 within three sessions, 11% down; day 2 never gets 10% below 95.
    assert crash_labels(closes, horizon=3, drawdown_pct=10.0) == [1, 1, 0, None, None, None]
    # With two sessions, day 0 only sees 99 and 95; the last two days are not known yet.
    assert crash_labels(closes, horizon=2, drawdown_pct=10.0) == [0, 1, 0, 0, None, None]


def test_the_model_learns_a_clear_relation():
    rows, labels = [], []
    for k in range(300):
        streak = float(k % 20)
        rows.append({name: (streak if name == "foreign_sell_streak" else 1.0) for name in FEATURES})
        labels.append(1 if streak >= 12 else 0)

    model = fit_model(rows, labels)

    low = model.predict({name: (2.0 if name == "foreign_sell_streak" else 1.0) for name in FEATURES})
    high = model.predict({name: (18.0 if name == "foreign_sell_streak" else 1.0) for name in FEATURES})
    assert low < 0.1 < 0.9 < high
    assert model.contributions({name: (18.0 if name == "foreign_sell_streak" else 1.0) for name in FEATURES})["foreign_sell_streak"] > 0


def test_walk_forward_never_sees_an_outcome_before_it_is_known():
    series = _cyclic_market(700)
    days = _market_days(series)
    closes = [series["index:COMPOSITE"][day.trade_date] for day in days]
    features = build_features(days, closes)
    labels = crash_labels(closes, horizon=63, drawdown_pct=10.0)
    probabilities, _, _ = walk_forward(features, labels, horizon=63)

    t = 600
    # Rewriting every outcome not yet known on day t leaves the estimates up to t unchanged.
    tampered = [label if (label is None or i + 63 <= t) else 1 - label for i, label in enumerate(labels)]
    again, _, _ = walk_forward(features, tampered, horizon=63)

    assert probabilities[: t + 1] == again[: t + 1]
    assert probabilities[:252] == [None] * 252  # not enough known history to fit yet
    assert any(p is not None for p in probabilities)


def _day(i: int, regime: str, close: float) -> tuple[MarketDay, DayFeatures]:
    day = date(2025, 1, 1).toordinal() + i
    reading = SignalReading("drawdown", 0.0, "none", {"peak": 100.0, "close": close})
    values = {name: float(i) for name in FEATURES}
    return (
        MarketDay(trade_date=date.fromordinal(day), regime=regime, raw_regime=regime, readings=[reading], event_risk=[]),
        DayFeatures(trade_date=date.fromordinal(day), close=close, values=values),
    )


def test_episodes_open_at_stressed_and_close_after_a_calm_run():
    plan = ["calm"] * 3 + ["stressed", "crisis", "crisis", "stressed"] + ["calm"] * 4 + ["elevated"] + ["calm"] * 5 + ["calm"] * 3
    closes = [100.0] * 3 + [90.0, 80.0, 70.0, 75.0] + [80.0, 85.0, 90.0, 95.0] + [96.0] + [97.0, 98.0, 99.0, 100.0, 101.0, 102, 103, 104]
    pairs = [_day(i, regime, close) for i, (regime, close) in enumerate(zip(plan, closes))]

    [episode] = detect_episodes([p[0] for p in pairs], [p[1] for p in pairs], close_after_calm=5)

    assert episode.opened_on == pairs[3][0].trade_date
    assert episode.worst_regime == "crisis"
    # An elevated day resets the calm run, so it closes five calm sessions after it.
    assert episode.closed_on == pairs[16][0].trade_date
    assert (episode.trough_close, episode.trough_index) == (70.0, 5)
    assert episode.max_drawdown_pct == pytest.approx(30.0)
    assert episode.further_fall_pct == pytest.approx(100 * (90 - 70) / 90)
    assert episode.recovered_on == pairs[15][0].trade_date  # first close back at the 100 peak


def test_analogs_only_use_episodes_that_had_closed():
    def episode(opened: int, closed: int | None, value: float) -> Episode:
        return Episode(
            opened_on=date(2024, 1, 1).fromordinal(date(2024, 1, 1).toordinal() + opened),
            open_index=opened,
            open_close=100.0,
            peak_close=110.0,
            worst_regime="crisis",
            open_snapshot={"features": {name: value for name in FEATURES}},
            closed_on=None if closed is None else date.fromordinal(date(2024, 1, 1).toordinal() + closed),
            close_index=closed,
            trough_index=opened + 5,
            trough_close=80.0,
        )

    episodes = [episode(10, 40, 1.0), episode(60, 90, 5.0), episode(100, None, 1.0)]
    scales = {name: 1.0 for name in FEATURES}
    today = {name: 1.2 for name in FEATURES}

    analogs = find_analogs(today, episodes, as_of_index=95, scales=scales)

    assert [item["opened_on"] for item in analogs] == [episodes[0].opened_on.isoformat(), episodes[1].opened_on.isoformat()]
    assert analogs[0]["further_fall_pct"] == 20.0
    assert analogs[0]["sessions_to_recover"] is None  # never got back to the peak
    assert find_analogs(today, episodes, as_of_index=50, scales=scales)[0]["opened_on"] == episodes[0].opened_on.isoformat()
    assert len(find_analogs(today, episodes, as_of_index=50, scales=scales)) == 1


def test_crash_risk_beats_the_base_rate_when_a_signal_leads_and_keeps_notes(tmp_config):
    _seed_series(tmp_config, _cyclic_market())
    run_market_signals(tmp_config)

    result = run_crash_risk(tmp_config)

    record = result.record
    assert record.scored_days > 500 and record.crash_days > 0
    assert record.brier < record.base_rate_brier * 0.95
    assert result.latest.trustworthy
    assert len(result.episodes) >= 4
    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        stored = load_crash_risk(conn)
        episode_id = conn.execute("SELECT episode_id FROM market_episodes ORDER BY opened_on LIMIT 1").fetchone()[0]
    finally:
        conn.close()
    assert stored["trustworthy"] is True
    assert stored["track_record"]["scored_days"] == record.scored_days
    assert stored["analogs"] and set(stored["analogs"][0]) >= {"opened_on", "further_fall_pct", "sessions_to_trough"}

    with WriterQueue(tmp_config) as wq:
        wq.submit(lambda conn: record_episode_note(conn, episode_id=episode_id, cause="synthetic selling", lessons=None, theme_ids=["msci"]))
    run_crash_risk(tmp_config)
    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        row = conn.execute("SELECT cause, theme_ids_json FROM market_episodes WHERE episode_id = ?", (episode_id,)).fetchone()
    finally:
        conn.close()
    assert (row["cause"], json.loads(row["theme_ids_json"])) == ("synthetic selling", ["msci"])


def test_the_backtest_reports_leads_false_alarms_and_golden_dates():
    series = _cyclic_market()
    days = _market_days(series)
    closes = [series["index:COMPOSITE"][day.trade_date] for day in days]

    report = build_backtest(
        days, closes, start=None, drawdown_pct=10.0, horizon=63, alert_probability=0.4, min_skill=0.05, close_after_calm=20
    )

    assert report.days == len(days)
    assert report.episodes
    # Foreign selling starts 15 sessions before each fall, so it leads the trough by more than that.
    assert all((item.signal_leads["foreign_sell_streak"] or 0) >= 15 for item in report.episodes[1:])
    # A trusted warning needs a track record first: fitted history, then 250 scored days.
    trusted = [item for item in report.episodes if item.episode.open_index > 700]
    assert trusted and all(item.warned_sessions_before_trough is not None for item in trusted)
    text = render_report(report)
    assert "## Golden dates" in text and "## Episodes" in text and "How early each signal fired" in text
    assert [result.passed for result in report.golden] == [None] * len(GOLDEN_DATES)  # no data on those days


def test_golden_dates_read_the_session_before_a_holiday():
    reading = SignalReading("drawdown", 30.0, "crisis")
    days = [
        MarketDay(trade_date=date(2020, 3, 23), regime="crisis", raw_regime="crisis", readings=[reading], event_risk=[]),
        MarketDay(trade_date=date(2024, 9, 18), regime="calm", raw_regime="calm", readings=[reading], event_risk=[]),
        MarketDay(trade_date=date(2025, 4, 8), regime="stressed", raw_regime="stressed", readings=[reading], event_risk=[]),
    ]

    results = {result.golden.day: result for result in golden_results(days)}

    assert results[date(2020, 3, 24)].trade_date == date(2020, 3, 23) and results[date(2020, 3, 24)].passed
    assert results[date(2024, 9, 19)].passed
    assert results[date(2025, 4, 8)].passed is False  # stressed is short of crisis
    assert results[date(2026, 10, 19)].passed is None


def test_a_trustworthy_warning_alerts_once_per_stretch(tmp_config):
    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)

        def seed(conn):
            for day, probability in (("2026-10-15", 0.2), ("2026-10-16", 0.45), ("2026-10-19", 0.52)):
                conn.execute(
                    "INSERT INTO crash_risk_days VALUES (?, ?, 0.18, 1, '{}', ?, 'crash-risk-v1', 'now')",
                    (day, probability, json.dumps([{"opened_on": "2025-02-27", "further_fall_pct": 14.2}])),
                )
            conn.execute(
                "INSERT INTO forecast_track_record VALUES ('crash-risk-v1', 63, 10, 0.12, 0.15, 0.6, 0.35, 0.4, 900, 200, '2026-07-01', 'now')"
            )

        wq.submit(seed)

    run_alerts(tmp_config, now=datetime(2026, 10, 19, 12, tzinfo=timezone.utc))
    again = run_alerts(tmp_config, now=datetime(2026, 10, 19, 13, tzinfo=timezone.utc))

    alerts = [json.loads(line) for line in tmp_config.alerts.jsonl_path.read_text().splitlines()]
    [alert] = [item for item in alerts if item["signal_kind"] == "crash_risk"]
    assert alert["claim"].startswith("Crash risk 52% (base rate 18%) as of 2026-10-19; past warnings caught 60%")
    assert "most like 2025-02-27, which fell a further 14.2%" in alert["claim"]
    assert alert["details"]["since"] == "2026-10-16"
    assert again.emitted == 0


def test_golden_dates_join_the_golden_set_once_market_data_exists(tmp_config):
    days = _weekdays(date(2026, 9, 1), 35)
    _seed_series(tmp_config, {"index:COMPOSITE": dict(zip(days, [6000.0] * 30 + [4500.0] * 5))})
    run_market_signals(tmp_config)

    result = run_golden_set(tmp_config)

    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        payload = json.loads(conn.execute("SELECT payload_json FROM golden_set_runs").fetchone()["payload_json"])
    finally:
        conn.close()
    market = {case["case_name"]: case for case in payload["cases"] if case["ticker"] == "MARKET"}
    assert market["market_state_2026-10-19"]["status"] == "pass"
    assert market["market_state_2020-03-24"]["status"] == "warning"
    assert result is not None
