from __future__ import annotations

import json
from datetime import date, datetime, timezone

import yaml

from stock_triage.alerts import run_alerts
from stock_triage.db import WriterQueue, connect, run_migrations
from stock_triage.intelligence.pipeline import _match_condition
from stock_triage.intelligence.thesis import LocalThesisCondition, LocalThesisDefinition
from stock_triage.market import run_market_signals
from stock_triage.market.theses import MarketContext, is_market_thesis, match_market_condition

from .test_market_data import _seed_series, _weekdays

_GUARD = {
    "thesis_id": "watchlist-market-guard",
    "ticker": None,
    "thesis_name": "Review the watchlist when the market breaks",
    "thesis_text": "Rules written in calm times.",
    "owner_scope": "portfolio",
    "status": "active",
    "conditions": [
        {"severity": "stop", "kind": "market_regime", "at_least": "stressed"},
        {"severity": "stop", "kind": "crash_risk", "operator": "gte", "value": 0.4},
        {"severity": "warning", "kind": "market_signal", "signal": "decline_share", "operator": "gte", "value": 70},
        {"severity": "warning", "kind": "exposure_score", "operator": "gte", "value": 70},
    ],
}


def _write_theses(tmp_config, theses: list[dict]) -> None:
    tmp_config.theses.definition_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_config.theses.definition_path.write_text(yaml.safe_dump({"theses": theses}))


def _seed_market(tmp_config, *, stressed: bool) -> list[date]:
    # 40 sessions. When stressed, the last six close 12% below the high while three
    # in four moving stocks fall: two signals at stressed.
    days = _weekdays(date(2026, 8, 3), 40)
    fall = 0.88 if stressed else 1.0
    closes = [6000.0] * 34 + [6000.0 * fall] * 6
    decliners = [200.0] * 34 + [300.0 if stressed else 200.0] * 6
    advancers = [200.0] * 34 + [100.0 if stressed else 200.0] * 6
    _seed_series(
        tmp_config,
        {
            "index:COMPOSITE": dict(zip(days, closes)),
            "breadth:advancers": dict(zip(days, advancers)),
            "breadth:decliners": dict(zip(days, decliners)),
        },
    )
    return days


def test_a_portfolio_thesis_breaks_when_the_state_reaches_stressed(tmp_config):
    _write_theses(tmp_config, [_GUARD])
    days = _seed_market(tmp_config, stressed=True)

    result = run_market_signals(tmp_config)

    assert result.latest.regime == "stressed"
    [evaluation] = result.theses
    assert evaluation.status == "broken"
    assert evaluation.ticker is None and evaluation.document_id is None
    payload = json.loads(evaluation.payload_json)
    assert [item["kind"] for item in payload["matched_stop"]] == ["market_regime"]
    assert [item["kind"] for item in payload["matched_warning"]] == ["market_signal"]
    # No estimate and no exposure scores yet: those conditions cannot be judged.
    assert {item["kind"] for item in payload["unsupported"]} == {"crash_risk", "exposure_score"}
    assert payload["since"] == days[-1].isoformat()
    assert "state at least stressed (market state stressed on" in evaluation.rationale

    first = run_alerts(tmp_config, now=datetime(2026, 10, 20, 10, tzinfo=timezone.utc))
    run_market_signals(tmp_config)  # the same day again: same stretch, no new alert
    second = run_alerts(tmp_config, now=datetime(2026, 10, 20, 11, tzinfo=timezone.utc))

    alerts = [json.loads(line) for line in tmp_config.alerts.jsonl_path.read_text().splitlines()]
    thesis_alerts = [alert for alert in alerts if alert["signal_kind"] == "thesis"]
    assert len(thesis_alerts) == 1
    assert thesis_alerts[0]["severity"] == "critical"
    assert thesis_alerts[0]["claim"].startswith(
        f"Review the watchlist when the market breaks: broken since {days[-1].isoformat()}."
    )
    assert first.emitted >= 1 and second.emitted == 0


def test_a_calm_market_leaves_the_thesis_holding(tmp_config):
    _write_theses(tmp_config, [{**_GUARD, "conditions": _GUARD["conditions"][:1]}])
    _seed_market(tmp_config, stressed=False)

    [evaluation] = run_market_signals(tmp_config).theses

    assert evaluation.status == "holding"


def test_market_theses_are_left_out_of_per_company_evaluation():
    market = LocalThesisDefinition(
        thesis_id="m", ticker=None, thesis_name="m", thesis_text="", owner_scope="portfolio", status="active",
        review_cadence=None, notes=None,
        conditions=[LocalThesisCondition(severity="stop", kind="market_regime", payload={"at_least": "crisis"})],
    )
    mixed = LocalThesisDefinition(
        thesis_id="c", ticker="BBCA", thesis_name="c", thesis_text="", owner_scope="local", status="active",
        review_cadence=None, notes=None,
        conditions=[
            LocalThesisCondition(severity="stop", kind="rule_present", payload={"rule_name": "x"}),
            LocalThesisCondition(severity="stop", kind="exposure_score", payload={"operator": "gte", "value": 60}),
        ],
    )

    assert is_market_thesis(market) and not is_market_thesis(mixed)

    # A company thesis's market condition reads the company's own exposure.
    context = MarketContext(trade_date="2026-10-19", regime="crisis", exposure={"BBCA": 65.0, "ABBA": 90.0})
    matched = _match_condition(
        mixed.conditions[1], baseline=None, deltas=[], events=[], triggered_rules=[], review_items=[],
        ticker="BBCA", market_context=context,
    )
    assert matched is True
    # Without market context, the condition stays unsupported as before.
    assert _match_condition(
        mixed.conditions[1], baseline=None, deltas=[], events=[], triggered_rules=[], review_items=[]
    ) is None


def test_crash_risk_counts_only_while_the_estimate_beats_its_base_rate():
    condition = LocalThesisCondition(severity="stop", kind="crash_risk", payload={"operator": "gte", "value": 0.4})
    untrusted = MarketContext(trade_date="2026-10-19", regime="crisis", crash_trade_date="2026-10-19")
    trusted = MarketContext(
        trade_date="2026-10-19", regime="crisis", crash_trade_date="2026-10-19", crash_probability=0.55
    )

    assert match_market_condition(condition, untrusted, ticker=None).matched is None
    assert "does not beat its base rate" in match_market_condition(condition, untrusted, ticker=None).observed
    assert match_market_condition(condition, trusted, ticker=None).matched is True


def test_exposure_without_a_ticker_checks_the_watchlist_only():
    condition = LocalThesisCondition(severity="warning", kind="exposure_score", payload={"operator": "gte", "value": 70})
    context = MarketContext(trade_date="2026-10-19", regime="calm", exposure={"ABBA": 85.0, "BBCA": 20.0})

    assert match_market_condition(condition, context, ticker=None, scope={"BBCA"}).matched is False
    hit = match_market_condition(condition, context, ticker=None, scope=None)
    assert hit.matched is True and hit.observed == "ABBA 85/100"


def test_market_thesis_rows_need_no_document(tmp_config):
    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)
    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        columns = {row["name"]: row["notnull"] for row in conn.execute("PRAGMA table_info(thesis_evaluations)")}
    finally:
        conn.close()
    assert columns["document_id"] == 0
