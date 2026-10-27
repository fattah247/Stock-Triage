from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from urllib.parse import parse_qs, urlsplit

import pytest

from stock_triage.db import WriterQueue, connect, run_migrations
from stock_triage.market import run_market_sync
from stock_triage.market.idx_dividends import parse_dividends
from stock_triage.market.idx_market import INDEX_SUMMARY_PATH, STOCK_SUMMARY_PATH, parse_stock_rows
from stock_triage.market.idx_ratios import RATIOS_PATH, months_due, parse_ratios

from .test_market_data import _FakeIDX, _weekdays


def _ratio(code: str, *, fs: str, eps: float, per: float | None = None, **extra) -> dict:
    # Shaped like IDX's LINK_FINANCIAL_DATA_RATIO rows: amounts in billions of rupiah.
    row = {
        "code": code, "stockName": f"{code} Tbk.", "sector": "Energy", "subSector": "Oil, Gas & Coal", "industry": "Coal",
        "subIndustry": "Coal Production", "subIndustryCode": "A121", "sharia": "S", "fsDate": fs, "fiscalYearEnd": "Dec",
        "audit": "LR", "opini": "-", "assets": 1000.0, "liabilities": 400.0, "equity": 600.0, "sales": 500.0, "ebt": 80.0,
        "profitPeriod": 60.0, "profitAttrOwner": 55.0, "eps": eps, "bookValue": 400.0, "per": per, "priceBV": 2.5,
        "deRatio": 0.67, "roa": 6.0, "roe": 20.0, "npm": 12.0,
    }
    row.update(extra)
    return row


def test_stock_and_ratio_rows_parse_into_columns():
    stocks = parse_stock_rows(
        {
            "data": [
                {"StockCode": "bbca", "Previous": 9000, "OpenPrice": 9010, "High": 9100, "Low": 8950, "Close": 9075, "Volume": 1000,
                 "Value": 9075000, "Frequency": 12, "ListedShares": 123, "TradebleShares": 100, "ForeignBuy": 10, "ForeignSell": 30},
                {"StockCode": ""},
            ]
        }
    )
    ratios = parse_ratios({"data": [_ratio("ANTM", fs="2026-06-30", eps=370.4, per=8.64)]})

    assert stocks == [
        {"ticker": "BBCA", "previous": 9000.0, "open": 9010.0, "high": 9100.0, "low": 8950.0, "close": 9075.0, "volume": 1000.0,
         "value": 9075000.0, "frequency": 12.0, "listed_shares": 123.0, "tradable_shares": 100.0, "foreign_buy": 10.0, "foreign_sell": 30.0}
    ]
    antm = ratios[0]
    assert (antm["ticker"], antm["sales"], antm["eps"], antm["per"], antm["sharia"]) == ("ANTM", 500e9, 370.4, 8.64, 1)
    assert antm["auditor_opinion"] is None and antm["fs_date"] == "2026-06-30" and antm["industry"] == "Coal"


def test_dividend_rows_parse_with_their_currency_and_dates():
    rows = parse_dividends(
        {
            "data": [
                {"code": "bbca", "cashDividend": 25, "currency": "IDR", "exDividend": "2026-08-31", "recordDate": "2026-09-01", "paymentDate": "2026-09-19", "note": "I2026"},
                {"code": "ADRO", "cashDividend": 0.0123, "currency": "USD", "exDividend": None, "recordDate": "2026-05-20T00:00:00", "note": ""},
                {"code": "ZERO", "cashDividend": 0, "currency": "IDR", "exDividend": "2026-05-20"},
            ]
        }
    )

    assert [(r["ticker"], r["ex_date"], r["cash_per_share"], r["currency"], r["note"]) for r in rows] == [
        ("BBCA", "2026-08-31", 25.0, "IDR", "I2026"),
        ("ADRO", "2026-05-20", 0.0123, "USD", None),  # no ex-date given: the record date stands in
    ]


def test_months_due_fills_gaps_and_refreshes_the_latest_once_a_day():
    months = [date(2021 + (m // 12), m % 12 + 1, 1) for m in range(70)]  # 2021-01 .. 2026-10
    fetched = {month: date(2026, 10, 21) for month in months}

    assert months_due(fetched, today=date(2026, 10, 22)) == [date(2026, 9, 1), date(2026, 10, 1)]
    assert months_due(fetched, today=date(2026, 10, 21)) == []
    del fetched[date(2023, 5, 1)]
    assert months_due(fetched, today=date(2026, 10, 21)) == [date(2023, 5, 1)]
    assert months_due({}, today=date(2021, 3, 5)) == [date(2021, 1, 1), date(2021, 2, 1), date(2021, 3, 1)]


class _RatiosIDX(_FakeIDX):
    """IDX's monthly tables: ratios (LINK_FINANCIAL_DATA_RATIO) and dividends (LINK_DIVIDEND)."""

    def __init__(self, closes, ratios_by_month, dividends_by_month=None):
        super().__init__(closes)
        self.ratios_by_month, self.dividends_by_month = ratios_by_month, dividends_by_month or {}
        self.months, self.dividend_months = [], []

    def fetch_url(self, url):
        if urlsplit(url).path != RATIOS_PATH:
            return super().fetch_url(url)
        query = parse_qs(urlsplit(url).query)
        month = date(int(query["periodYear"][0]), int(query["periodMonth"][0]), 1)
        if query["urlName"] == ["LINK_DIVIDEND"]:
            self.dividend_months.append(month)
            return {"data": self.dividends_by_month.get(month, [])}
        self.months.append(month)
        return {"data": self.ratios_by_month.get(month, [])}


def test_market_sync_keeps_each_stock_and_fetches_older_days_for_stocks_alone(tmp_config, monkeypatch):
    monkeypatch.setattr("stock_triage.market.jisdor._PAUSE_S", 0)
    from .test_market_data import _jisdor_client

    fake = _FakeIDX({date(2026, 10, 15): 5863.78, date(2026, 10, 16): 5185.1})

    @contextmanager
    def factory():
        yield fake

    def sync():
        return run_market_sync(
            tmp_config, start=date(2026, 10, 14), end=date(2026, 10, 16), history=False, monthly=False,
            idx_fetcher_factory=factory, jisdor_client=_jisdor_client([]),
        )

    first = sync()

    assert (first.idx_days_fetched, first.idx_days_no_data, first.stock_days) == (2, 1, 2)
    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        rows = conn.execute("SELECT ticker, trade_date, close, listed_shares FROM stock_daily ORDER BY trade_date, ticker").fetchall()
    finally:
        conn.close()
    assert [(r["ticker"], r["trade_date"]) for r in rows] == [
        ("BBCA", "2026-10-15"), ("FLAT", "2026-10-15"), ("UPPP", "2026-10-15"),
        ("BBCA", "2026-10-16"), ("FLAT", "2026-10-16"), ("UPPP", "2026-10-16"),
    ]

    # Days synced before per-stock rows were kept: only their stock summary is fetched again.
    def forget_stocks(conn):
        conn.execute("DELETE FROM stock_daily")
        conn.execute("DELETE FROM market_sync_days WHERE source_name = 'idx_stocks'")

    with WriterQueue(tmp_config) as wq:
        wq.submit(forget_stocks)
    fake.calls = []
    again = sync()

    assert (again.idx_days_fetched, again.stock_days, again.idx_days_skipped) == (0, 2, 1)
    assert fake.calls == [(STOCK_SUMMARY_PATH, date(2026, 10, 15)), (STOCK_SUMMARY_PATH, date(2026, 10, 16))]
    assert INDEX_SUMMARY_PATH not in {path for path, _ in fake.calls}
    fake.calls = []
    assert sync().stock_days == 0 and not [call for call in fake.calls if call[1]]


def test_market_sync_fetches_every_ratio_month_then_only_the_latest_each_day(tmp_config, monkeypatch):
    monkeypatch.setattr("stock_triage.market.jisdor._PAUSE_S", 0)
    from .test_market_data import _jisdor_client

    september = [_ratio("AAAA", fs="2026-06-30", eps=50.0, per=10.0)]
    october = [_ratio("AAAA", fs="2026-06-30", eps=50.0, per=11.0), _ratio("BBBB", fs="2026-06-30", eps=-5.0)]
    dividend = {"code": "AAAA", "cashDividend": 25, "currency": "IDR", "cumDividend": "2026-08-28", "exDividend": "2026-08-31",
                "recordDate": "2026-09-01", "paymentDate": "2026-09-19", "note": "F"}
    fake = _RatiosIDX(
        {date(2026, 10, 16): 5185.1},
        {date(2026, 9, 1): september, date(2026, 10, 1): october},
        # The same dividend in two monthly tables is stored once.
        {date(2026, 9, 1): [dividend], date(2026, 10, 1): [dividend]},
    )

    @contextmanager
    def factory():
        yield fake

    def sync(end):
        return run_market_sync(
            tmp_config, start=end, end=end, history=False, idx_fetcher_factory=factory, jisdor_client=_jisdor_client([])
        )

    first = sync(date(2026, 10, 16))

    assert (first.ratio_months, first.dividend_months, first.errors) == (2, 2, [])
    assert fake.months[0] == date(2021, 1, 1) and fake.months[-1] == date(2026, 10, 1) and len(fake.months) == 70
    assert fake.dividend_months[0] == date(2019, 1, 1) and len(fake.dividend_months) == 94
    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        stored = conn.execute("SELECT ticker, report_month, per, sales FROM company_ratios ORDER BY report_month, ticker").fetchall()
    finally:
        conn.close()
    assert [tuple(row) for row in stored] == [
        ("AAAA", "2026-09", 10.0, 500e9), ("AAAA", "2026-10", 11.0, 500e9), ("BBBB", "2026-10", None, 500e9)
    ]
    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        paid = conn.execute("SELECT ticker, ex_date, cash_per_share, report_month FROM dividends").fetchall()
    finally:
        conn.close()
    assert [tuple(row) for row in paid] == [("AAAA", "2026-08-31", 25.0, "2026-10")]

    fake.months, fake.dividend_months = [], []
    sync(date(2026, 10, 16))
    assert fake.months == fake.dividend_months == []

    def fetched_yesterday(conn):
        conn.execute(
            "UPDATE market_sync_days SET fetched_at = '2026-10-15T09:00:00+00:00' WHERE source_name IN ('idx_ratios', 'idx_dividends')"
        )

    with WriterQueue(tmp_config) as wq:
        wq.submit(fetched_yesterday)
    sync(date(2026, 10, 16))
    assert fake.months == fake.dividend_months == [date(2026, 9, 1), date(2026, 10, 1)]


def test_profile_dividends_fill_the_monthly_table_and_turn_totals_per_share(tmp_config):
    from stock_triage.market.idx_profiles import parse_profile_dividends, store_profile_dividends, tickers_due

    payload = {
        "Dividen": [
            {"CashDividenPerSaham": 0, "CashDividenTotal": 21999902180685, "CashDividenTotalMU": "IDR", "Jenis": "dt", "TahunBuku": "2025",
             "TanggalCum": "2026-06-17T00:00:00", "TanggalDPS": "2026-06-19T16:00:00", "TanggalExRegulerDanNegosiasi": "2026-06-18T00:00:00",
             "TanggalPembayaran": "2026-07-10T00:00:00"},
            {"CashDividenPerSaham": 25, "CashDividenPerSahamMU": "IDR", "CashDividenTotal": 0, "Jenis": "dti", "TahunBuku": "2026",
             "TanggalExRegulerDanNegosiasi": "2026-08-31T00:00:00", "TanggalDPS": "2026-09-01T00:00:00"},
        ]
    }
    rows = parse_profile_dividends(payload, "tlkm")
    assert [(r["ex_date"], r["cash_per_share"], r["total"], r["note"]) for r in rows] == [
        ("2026-06-18", None, 21999902180685.0, "F2025"),
        ("2026-08-31", 25.0, None, "I2026"),
    ]

    def seed(conn):
        conn.executemany(
            "INSERT INTO stock_daily (ticker, trade_date, close, listed_shares) VALUES (?, ?, 3000, ?)",
            [("TLKM", "2026-06-16", 99062216600.0), ("TLKM", "2026-10-21", 99062216600.0), ("BBCA", "2026-10-21", 1.2e11)],
        )

    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)
        wq.submit(seed)
        assert wq.submit(lambda conn: tickers_due(conn, today=date(2026, 10, 22))) == ["BBCA", "TLKM"]  # every company the first time
        assert wq.submit(lambda conn: store_profile_dividends(conn, ticker="TLKM", rows=rows)) == 2
        assert wq.submit(lambda conn: tickers_due(conn, today=date(2026, 10, 22))) == ["BBCA"]
    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        stored = conn.execute("SELECT ex_date, cash_per_share, source FROM dividends ORDER BY ex_date").fetchall()
    finally:
        conn.close()
    # Rp 22.0 tn over 99.06 bn shares listed on the cum date.
    assert [tuple(row) for row in stored] == [("2026-06-18", pytest.approx(222.08, abs=0.01), "idx_profiles"), ("2026-08-31", 25.0, "idx_profiles")]


def test_sync_resumes_after_a_break_and_retries_an_empty_stock_summary(tmp_config, monkeypatch):
    monkeypatch.setattr("stock_triage.market.jisdor._PAUSE_S", 0)
    from .test_market_data import _jisdor_client

    days = _weekdays(date(2026, 9, 1), 33)  # 1 Sep .. 15 Oct
    fake = _FakeIDX({day: 6000.0 for day in days})

    @contextmanager
    def factory():
        yield fake

    run_market_sync(tmp_config, start=days[0], end=days[0], history=False, monthly=False, idx_fetcher_factory=factory, jisdor_client=_jisdor_client([]))
    fake.calls = []
    original = fake.fetch

    def empty_on_the_8th(path, trade_date):
        if path == STOCK_SUMMARY_PATH and trade_date == date(2026, 10, 8):
            return {"data": []}
        return original(path, trade_date)

    monkeypatch.setattr(fake, "fetch", empty_on_the_8th)
    # No start given, six weeks later: the sync picks up the day after the last one synced.
    result = run_market_sync(tmp_config, end=days[-1], history=False, monthly=False, idx_fetcher_factory=factory, jisdor_client=_jisdor_client([]))

    assert result.start == date(2026, 9, 2)
    assert min(day for _, day in fake.calls if day) == date(2026, 9, 2)
    assert result.errors == ["idx_market 2026-10-08: IDX returned an empty stock summary for a trading day"]
    monkeypatch.setattr(fake, "fetch", original)
    fake.calls = []
    again = run_market_sync(tmp_config, end=days[-1], history=False, monthly=False, idx_fetcher_factory=factory, jisdor_client=_jisdor_client([]))
    assert date(2026, 10, 8) in {day for _, day in fake.calls if day} and again.errors == []
