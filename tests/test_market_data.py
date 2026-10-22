from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from stock_triage.alerts import run_alerts
from stock_triage.dashboard.snapshot import _load_market_risk
from stock_triage.db import WriterQueue, connect, run_migrations
from stock_triage.market import load_market_state, run_market_signals, run_market_sync
from stock_triage.market.idx_market import INDEX_SUMMARY_PATH, STOCK_SUMMARY_PATH, parse_index_summary, parse_stock_summary
from stock_triage.market.jisdor import parse_jisdor_export
from stock_triage.market.signals import compute_market_days

from .test_workbook_extraction import _xlsx_bytes

_THRESHOLDS = {
    "drawdown_pct": {"elevated": 5.0, "stressed": 10.0, "crisis": 20.0},
    "vol_ratio": {"elevated": 1.5, "stressed": 2.0, "crisis": 3.0},
    "foreign_sell_streak": {"elevated": 5.0, "stressed": 10.0, "crisis": 15.0},
    "usdidr_change_20d_pct": {"elevated": 2.0, "stressed": 3.0, "crisis": 5.0},
    "decline_share_pct": {"elevated": 60.0, "stressed": 70.0, "crisis": 80.0},
    "sector_drawdown_pct": {"elevated": 7.0, "stressed": 12.0, "crisis": 25.0},
}


def _index_payload(day: date, composite: float, finance: float | None = None) -> dict:
    rows = [{"IndexCode": "COMPOSITE", "Date": f"{day}T00:00:00", "Previous": composite + 10, "Close": composite, "Value": 25e12}]
    if finance is not None:
        rows.append({"IndexCode": "IDXFINANCE", "Date": f"{day}T00:00:00", "Previous": finance + 1, "Close": finance, "Value": 9e12})
    return {"draw": 0, "recordsTotal": len(rows), "recordsFiltered": len(rows), "data": rows}


def _stock_payload() -> dict:
    # Shaped like IDX's GetStockSummary (docs/phase2_market_recon.md); BBCA's row is from 2026-10-19.
    rows = [
        {"StockCode": "BBCA", "Previous": 5175, "Close": 4820, "Volume": 382924400, "Value": 1846577521000, "ForeignBuy": 76717400, "ForeignSell": 281481600},
        {"StockCode": "UPPP", "Previous": 100, "Close": 110, "Volume": 1000, "Value": 105000, "ForeignBuy": 500, "ForeignSell": 0},
        {"StockCode": "FLAT", "Previous": 50, "Close": 50, "Volume": 0, "Value": 0, "ForeignBuy": 0, "ForeignSell": 0},
    ]
    return {"draw": 0, "recordsTotal": 3, "recordsFiltered": 3, "data": rows}


def test_idx_summaries_parse_into_daily_series():
    index = parse_index_summary(_index_payload(date(2026, 10, 19), 4996.609, 1150.502))
    stocks = parse_stock_summary(_stock_payload())

    assert index == {"index:COMPOSITE": 4996.609, "turnover:value": 25e12, "index:IDXFINANCE": 1150.502}
    assert (stocks["breadth:advancers"], stocks["breadth:decliners"], stocks["breadth:unchanged"]) == (1, 1, 1)
    # Net foreign shares valued at each stock's average traded price: BBCA's ~Rp 0.99 tn of selling dominates.
    bbca = (76717400 - 281481600) * (1846577521000 / 382924400)
    upppp = 500 * (105000 / 1000)
    assert stocks["flow:foreign_net_value_est"] == pytest.approx(bbca + upppp)
    assert stocks["flow:foreign_net_value_est"] == pytest.approx(-0.987e12, rel=0.01)


def test_jisdor_export_parses_us_dates_and_indonesian_numbers():
    content = _xlsx_bytes(
        [
            (
                "Informasi Kurs Jisdor",
                None,
                [["Informasi Kurs Jisdor"], ["NO", "Tanggal", "Kurs"], ["1", "1/2/2020 12:00:00 AM", "13.895,00"], ["2", "10/19/2026 12:00:00 AM", "18.050,00"]],
            )
        ]
    )

    assert parse_jisdor_export(content) == {date(2020, 1, 2): 13895.0, date(2026, 10, 19): 18050.0}


class _FakeIDX:
    def __init__(self, closes: dict[date, float], failing: set[date] = frozenset(), notices: dict | None = None) -> None:
        self.closes, self.failing, self.calls = closes, failing, []
        self.notices = notices or {}

    def fetch_url(self, url):
        self.calls.append((url.split("?")[0], None))
        return self.notices.get(url.split("?")[0].removeprefix("https://www.idx.co.id"), {"Results": []})

    def fetch(self, path, trade_date):
        self.calls.append((path, trade_date))
        if trade_date in self.failing:
            raise RuntimeError("IDX returned 503")
        if path == INDEX_SUMMARY_PATH:
            close = self.closes.get(trade_date)
            return _index_payload(trade_date, close) if close else {"data": []}
        assert path == STOCK_SUMMARY_PATH
        return _stock_payload()


_JISDOR_PAGE = """
<form method="post"><input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="state-1" />
<input type="hidden" name="__EVENTVALIDATION" id="__EVENTVALIDATION" value="valid&#43;1" />
<input name="ctl00$ctl50$g_x$ctl00$TextBoxFrom" type="text" id="TextBoxFrom" />
<input name="ctl00$ctl50$g_x$ctl00$TextBoxDateTo" type="text" id="TextBoxDateTo" />
<input type="submit" name="ctl00$ctl50$g_x$ctl00$ButtonCari" value="Cari" />
<a id="ButtonExport" class="btn-unduh" href="javascript:__doPostBack(&#39;ctl00$ctl50$g_x$ctl00$ButtonExport&#39;,&#39;&#39;)">Unduh</a></form>
"""


def _jisdor_client(requests: list[dict]) -> httpx.Client:
    export = _xlsx_bytes(
        [("Informasi Kurs Jisdor", None, [["NO", "Tanggal", "Kurs"], ["1", "10/14/2026 12:00:00 AM", "17.898,00"], ["2", "10/15/2026 12:00:00 AM", "17.948,00"]])]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=_JISDOR_PAGE)
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        requests.append(form)
        if form.get("__EVENTTARGET", "").endswith("ButtonExport"):
            return httpx.Response(
                200,
                content=export,
                headers={"content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
            )
        return httpx.Response(200, text=_JISDOR_PAGE.replace("state-1", "state-2"))

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_market_sync_fetches_weekdays_records_holidays_and_resumes(tmp_config, monkeypatch):
    monkeypatch.setattr("stock_triage.market.jisdor._PAUSE_S", 0)
    # Mon 12 Oct .. Fri 16 Oct 2026, with Wednesday a holiday and the weekend around it.
    closes = {date(2026, 10, 12): 5936.14, date(2026, 10, 13): 5875.0, date(2026, 10, 15): 5863.78, date(2026, 10, 16): 5185.105}
    fake = _FakeIDX(closes, failing={date(2026, 10, 15)})
    posted: list[dict] = []

    @contextmanager
    def factory():
        yield fake

    result = run_market_sync(
        tmp_config,
        start=date(2026, 10, 10),
        end=date(2026, 10, 18),
        idx_fetcher_factory=factory,
        jisdor_client=_jisdor_client(posted),
    )

    assert (result.idx_days_fetched, result.idx_days_no_data, result.jisdor_rates) == (3, 1, 2)
    assert result.errors == ["idx_market 2026-10-15: IDX returned 503"]
    # Weekends are never requested; the holiday needs no stock summary.
    assert {day for _, day in fake.calls if day} == {date(2026, 10, d) for d in (12, 13, 14, 15, 16)}
    assert (STOCK_SUMMARY_PATH, date(2026, 10, 14)) not in fake.calls
    # The export reuses the search's view state and the requested dates.
    search, export = posted
    assert search["ctl00$ctl50$g_x$ctl00$TextBoxFrom"] == "10/10/2026"
    assert export["__VIEWSTATE"] == "state-2"
    assert export["__EVENTVALIDATION"] == "valid+1"
    assert export["__EVENTTARGET"] == "ctl00$ctl50$g_x$ctl00$ButtonExport"

    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        composite = {
            row["trade_date"]: row["value"]
            for row in conn.execute("SELECT trade_date, value FROM market_series_daily WHERE series_id = 'index:COMPOSITE'")
        }
        statuses = dict(conn.execute("SELECT trade_date, status FROM market_sync_days WHERE source_name = 'idx_market'").fetchall())
        rate = conn.execute(
            "SELECT value FROM market_series_daily WHERE series_id = 'fx:USDIDR_JISDOR' AND trade_date = '2026-10-15'"
        ).fetchone()["value"]
    finally:
        conn.close()
    assert composite == {"2026-10-12": 5936.14, "2026-10-13": 5875.0, "2026-10-16": 5185.105}
    assert statuses == {"2026-10-12": "ok", "2026-10-13": "ok", "2026-10-14": "no_data", "2026-10-16": "ok"}
    assert rate == 17948.0

    # A second run only retries the failed day.
    fake.failing, fake.calls = set(), []
    again = run_market_sync(
        tmp_config, start=date(2026, 10, 10), end=date(2026, 10, 18), idx_fetcher_factory=factory, jisdor_client=_jisdor_client([])
    )
    assert again.idx_days_skipped == 4
    assert {day for _, day in fake.calls if day} == {date(2026, 10, 15)}


def test_market_sync_stops_when_every_day_fails_and_resumes_later(tmp_config, monkeypatch):
    monkeypatch.setattr("stock_triage.market.jisdor._PAUSE_S", 0)
    # The browser closed after the first day: every later fetch fails the same way.
    days = _weekdays(date(2026, 2, 9), 10)
    fake = _FakeIDX({days[0]: 8100.0}, failing=set(days[1:]))

    @contextmanager
    def factory():
        yield fake

    result = run_market_sync(
        tmp_config, start=days[0], end=days[-1], idx_fetcher_factory=factory, jisdor_client=_jisdor_client([])
    )

    assert result.idx_days_fetched == 1
    assert len(result.errors) == 3
    assert result.idx_days_left == 6
    assert {day for _, day in fake.calls if day} == set(days[:4])
    # JISDOR is a separate source and still syncs.
    assert result.jisdor_rates == 2

    fake.failing, fake.calls = set(), []
    again = run_market_sync(
        tmp_config, start=days[0], end=days[-1], idx_fetcher_factory=factory, jisdor_client=_jisdor_client([])
    )
    assert (again.idx_days_skipped, again.idx_days_left, again.errors) == (1, 0, [])


def _weekdays(start: date, count: int) -> list[date]:
    days, day = [], start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def _days(series, **overrides):
    options = {
        "thresholds": _THRESHOLDS,
        "sectors": ["IDXFINANCE"],
        "min_signals": 2,
        "step_down_after": 5,
    }
    options.update(overrides)
    return compute_market_days(series, **options)


def test_a_crash_alone_is_a_crisis_and_the_state_steps_down_slowly():
    days = _weekdays(date(2025, 1, 6), 37)
    closes = [100.0] * 30 + [75.0] + [100.0] * 6
    result = _days({"index:COMPOSITE": dict(zip(days, closes))})

    regimes = [day.regime for day in result]
    assert regimes[29] == "calm"
    assert regimes[30] == "crisis"  # 25% below the high, on its own
    assert result[30].drivers[0].text == "IHSG 25.0% below its 31-session high (75 vs 100)"
    # Recovered the next session, but the state waits five calm sessions.
    assert [day.raw_regime for day in result[31:]] == ["calm"] * 6
    assert regimes[31:] == ["crisis", "crisis", "crisis", "crisis", "calm", "calm"]


def test_the_high_is_called_52_week_only_once_a_year_is_synced():
    days = _weekdays(date(2025, 1, 6), 260)
    closes = [100.0] * 259 + [80.0]
    result = _days({"index:COMPOSITE": dict(zip(days, closes))})

    assert result[-1].drivers[0].text == "IHSG 20.0% below its 52-week high (80 vs 100)"


def test_the_state_needs_two_signals_unless_the_index_crashes():
    days = _weekdays(date(2025, 1, 6), 31)
    closes = dict(zip(days, [100.0] * 30 + [88.0]))  # 12% drawdown: stressed on its own
    selling = {day: (-1e11 if index >= 21 else 1e11) for index, day in enumerate(days)}  # 10 sessions of net selling

    one_signal = _days({"index:COMPOSITE": closes})[-1]
    two_signals = _days({"index:COMPOSITE": closes, "flow:foreign_net_value_est": selling})[-1]

    assert (one_signal.readings[0].level, one_signal.regime) == ("stressed", "calm")
    assert two_signals.regime == "stressed"
    assert [reading.signal_name for reading in two_signals.drivers] == ["drawdown", "foreign_sell_streak"]
    assert two_signals.drivers[1].text == "Foreign investors net sellers 10 sessions running (about Rp 1.0 tn, estimated)"


def test_rupiah_change_uses_the_latest_published_rate():
    days = _weekdays(date(2025, 1, 6), 25)
    closes = {day: 100.0 for day in days}
    # JISDOR is missing on the last trading day; the previous fixing stands in.
    rates = {days[3]: 15000.0, days[22]: 15500.0}

    last = _days({"index:COMPOSITE": closes, "fx:USDIDR_JISDOR": rates})[-1]

    rupiah = next(reading for reading in last.readings if reading.signal_name == "usdidr_change_20d")
    assert (rupiah.value, rupiah.level) == (3.33, "stressed")
    assert rupiah.text == "Rupiah weaker: USD/IDR +3.3% in 20 sessions (Rp 15,500)"


def test_fundamental_breadth_counts_only_once_annual_reports_are_due():
    days = [date(2026, 3, 30), date(2026, 3, 31)]
    fundamental = {"fiscal_year": 2025, "change_pts": 21.1, "level": "crisis", "as_of_date": "2026-10-20"}

    result = _days({"index:COMPOSITE": {day: 100.0 for day in days}}, fundamental=fundamental)

    assert [reading.signal_name for reading in result[0].readings][-1] != "fundamental_breadth"
    assert result[1].readings[-1].signal_name == "fundamental_breadth"


def _seed_series(tmp_config, series: dict[str, dict[date, float]]) -> None:
    def _insert(conn):
        conn.executemany(
            "INSERT INTO market_series_daily (series_id, trade_date, value, source_name, retrieved_at) VALUES (?, ?, ?, 'test', 'now')",
            [(series_id, day.isoformat(), value) for series_id, values in series.items() for day, value in values.items()],
        )

    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)
        wq.submit(_insert)


def test_market_state_is_stored_explained_and_alerted_once(tmp_config):
    days = _weekdays(date(2026, 9, 1), 31)
    _seed_series(tmp_config, {"index:COMPOSITE": dict(zip(days, [6000.0] * 30 + [4500.0]))})

    result = run_market_signals(tmp_config)

    assert (result.days, result.latest.regime) == (31, "crisis")
    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        state = load_market_state(conn)
        risk = _load_market_risk(conn, tmp_config, today=days[-1])
    finally:
        conn.close()
    assert state["regime"] == "crisis"
    assert state["composite_close"] == 4500.0
    assert state["drivers"][0]["text"] == "IHSG 25.0% below its 31-session high (4,500 vs 6,000)"
    assert risk["state"]["trade_date"] == days[-1].isoformat()

    first = run_alerts(tmp_config, now=datetime(2026, 10, 14, 10, tzinfo=timezone.utc))
    second = run_alerts(tmp_config, now=datetime(2026, 10, 14, 11, tzinfo=timezone.utc))

    alerts = [json.loads(line) for line in tmp_config.alerts.jsonl_path.read_text().splitlines()]
    regime_alerts = [alert for alert in alerts if alert["signal_kind"] == "market_regime"]
    assert len(regime_alerts) == 1
    assert regime_alerts[0]["severity"] == "critical"
    assert regime_alerts[0]["claim"].startswith(f"Market now crisis (was calm) on {days[-1].isoformat()}: IHSG 25.0% below")
    assert second.emitted == 0
    assert first.emitted >= 1


def test_a_state_change_is_alerted_even_when_alerts_run_days_later(tmp_config):
    # The crash is on day 31; alerts-run first runs four sessions later, still in crisis.
    days = _weekdays(date(2026, 9, 1), 35)
    _seed_series(tmp_config, {"index:COMPOSITE": dict(zip(days, [6000.0] * 30 + [4500.0] * 5))})
    run_market_signals(tmp_config)

    run_alerts(tmp_config, now=datetime(2026, 10, 20, 10, tzinfo=timezone.utc))
    # Another crisis session arrives; it is the same stretch, so no second alert.
    _seed_series(tmp_config, {"index:COMPOSITE": {_weekdays(days[-1], 2)[1]: 4400.0}})
    run_market_signals(tmp_config)
    again = run_alerts(tmp_config, now=datetime(2026, 10, 21, 10, tzinfo=timezone.utc))

    alerts = [json.loads(line) for line in tmp_config.alerts.jsonl_path.read_text().splitlines()]
    regime_alerts = [alert for alert in alerts if alert["signal_kind"] == "market_regime"]
    assert len(regime_alerts) == 1
    assert regime_alerts[0]["claim"].startswith(
        f"Market now crisis (was calm) since {days[30].isoformat()}, as of {days[-1].isoformat()}: IHSG"
    )
    assert again.emitted == 0
