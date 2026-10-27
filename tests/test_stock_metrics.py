from __future__ import annotations

from datetime import date, timedelta

import pytest

from stock_triage.db import WriterQueue, connect, run_migrations
from stock_triage.stocks import build_universe, stock_detail
from stock_triage.stocks.metrics import adjust_closes

from .test_market_data import _weekdays


def test_split_scales_every_earlier_close():
    days = [date(2026, 3, d) for d in (2, 3, 4, 5)]
    # A 1:5 split on the 4th: IDX sets that day's previous price to 5,100 / 5.
    adjusted, adjustments = adjust_closes([5000.0, 5100.0, 1030.0, 1040.0], [0.0, 5000.0, 1020.0, 1030.0], days)

    assert adjusted == pytest.approx([1000.0, 1020.0, 1030.0, 1040.0])
    assert adjustments == [(date(2026, 3, 4), pytest.approx(0.2))]


def test_a_gap_in_the_stored_days_is_not_a_corporate_action():
    days = [date(2024, 3, 1), date(2024, 3, 4), date(2024, 3, 15)]
    calendar = {day: i for i, day in enumerate([date(2024, 3, 1), date(2024, 3, 4), date(2024, 3, 5), date(2024, 3, 15)])}
    # 4 March to 15 March skips the 5th: its previous price (1100) is the missing day's close, not an adjustment.
    adjusted, adjustments = adjust_closes([990.0, 1000.0, 1110.0], [980.0, 990.0, 1100.0], days, position=calendar)

    assert adjusted == [990.0, 1000.0, 1110.0] and adjustments == []


def _seed_universe(cfg) -> list[date]:
    days = _weekdays(date(2025, 1, 6), 300)
    as_of = days[-1]

    def seed(conn):
        conn.executemany(
            "INSERT INTO market_series_daily (series_id, trade_date, value, source_name, retrieved_at) VALUES ('index:COMPOSITE', ?, ?, 'idx_market', 'now')",
            [(day.isoformat(), 7000.0 * 1.0005**i) for i, day in enumerate(days)],
        )
        stock_rows = []
        for i, day in enumerate(days):
            for ticker, close, value in (
                ("AAAA", 1000.0 * 1.001**i, 10e9),  # beats IHSG, liquid
                ("CCCC", 2000.0 * 0.9995**i, 6e9),  # lags it
                ("BBBB", 500.0, 0.4e9),  # flat and thinly traded
            ):
                prev = stock_rows[-3][4] if i else close
                stock_rows.append((ticker, day.isoformat(), prev if ticker != "BBBB" else 500.0, close, close, value / close, value, 1e9, 2e7, 1e7))
        conn.executemany(
            "INSERT INTO stock_daily (ticker, trade_date, previous, open, close, volume, value, listed_shares, foreign_buy, foreign_sell) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            stock_rows,
        )
        ratio_rows = []
        months = [(2024 + (11 + m) // 12, (11 + m) % 12 + 1) for m in range(14)]  # 2024-12 .. 2026-01
        for n, (year, month) in enumerate(months):
            fs = "2024-09-30" if n < 6 else "2025-09-30"
            ratio_rows.append(("AAAA", f"{year}-{month:02d}", fs, 10.0 + n, 50.0 if fs == "2025-09-30" else 40.0, 400.0, 20.0, 12.0, 0.5,
                               600e9 if fs == "2025-09-30" else 500e9, 66e9 if fs == "2025-09-30" else 60e9, 900e9))
            ratio_rows.append(("CCCC", f"{year}-{month:02d}", "2025-09-30", 12.0, 100.0, 1500.0, 8.0, 5.0, 1.5, 800e9, 50e9, 2000e9))
            ratio_rows.append(("BBBB", f"{year}-{month:02d}", "2025-09-30", None, -10.0, 100.0, -5.0, -3.0, 0.8, 100e9, -10e9, 50e9))
        conn.executemany(
            "INSERT INTO company_ratios (ticker, report_month, fs_date, per, eps, book_value, roe, npm, der, sales, profit_attr_owner, equity, "
            "sector, industry, sharia, retrieved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Energy', 'Coal', 1, 'now')",
            ratio_rows,
        )
        conn.execute("INSERT INTO companies (ticker, issuer_name, first_seen_at, last_seen_at) VALUES ('AAAA', 'Alpha Tbk', 'now', 'now')")
        conn.executemany(
            "INSERT INTO market_series_daily (series_id, trade_date, value, source_name, retrieved_at) VALUES ('fx:USDIDR_JISDOR', ?, 16000, 'bi_jisdor', 'now')",
            [(day.isoformat(),) for day in days],
        )
        conn.executemany(
            "INSERT INTO dividends (ticker, ex_date, cash_per_share, currency, report_month, retrieved_at) VALUES (?, ?, ?, ?, '2026-01', 'now')",
            [
                ("AAAA", "2024-06-10", 15.0, "IDR"),  # more than a year before the latest day
                ("AAAA", (as_of - timedelta(days=100)).isoformat(), 20.0, "IDR"),
                ("CCCC", (as_of - timedelta(days=50)).isoformat(), 0.002, "USD"),  # a miner paying in dollars
            ],
        )
        for notice_id, kind, day in (("n1", "suspend", as_of - timedelta(days=5)), ("n2", "suspend", as_of + timedelta(days=5))):
            conn.execute(
                "INSERT INTO exchange_notices (notice_id, kind, notice_date, ticker, title, raw_json, fetched_at) VALUES (?, ?, ?, 'BBBB', 'Suspensi', '{}', 'now')",
                (notice_id, kind, day.isoformat()),
            )

    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
        wq.submit(seed)
    return days


def test_universe_numbers_scores_and_flags(tmp_config):
    days = _seed_universe(tmp_config)
    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        universe = build_universe(conn)
        detail = stock_detail(conn, universe, "aaaa")
    finally:
        conn.close()

    assert universe.as_of == days[-1] and [row["ticker"] for row in universe.rows] == ["CCCC", "AAAA", "BBBB"]
    alpha = universe.by_ticker["AAAA"]
    close = 1000.0 * 1.001**299
    assert alpha["name"] == "Alpha Tbk" and alpha["close"] == pytest.approx(close)
    assert alpha["ret_1y_pct"] == pytest.approx((1.001**252 - 1) * 100)
    assert alpha["rel_1y_pts"] == pytest.approx((1.001**252 - 1.0005**252) * 100)
    assert alpha["pe"] == pytest.approx(close / 50.0) and alpha["pbv"] == pytest.approx(close / 400.0)
    assert alpha["market_cap"] == pytest.approx(close * 1e9)
    # Same nine months a year apart: sales 600 against 500 bn, profit 66 against 60 bn.
    assert alpha["sales_yoy_pct"] == pytest.approx(20.0) and alpha["profit_yoy_pct"] == pytest.approx(10.0)
    assert alpha["pe_history_pct"] == pytest.approx(100.0)  # today's P/E is above every monthly P/E since
    assert alpha["below_high_pct"] == pytest.approx(0.0) and alpha["beta"] is not None
    assert [flag["code"] for flag in alpha["flags"]] == ["pe_high"]
    # Only the dividend in the past year counts; the dollar one converts at JISDOR.
    assert alpha["dividends_12m"] == 20.0 and alpha["dividend_yield_pct"] == pytest.approx(20.0 / close * 100)
    assert alpha["payout_pct"] == pytest.approx(40.0) and alpha["dividend_years"] == 2
    charlie = universe.by_ticker["CCCC"]
    assert charlie["dividends_12m"] == pytest.approx(32.0) and charlie["last_dividend"]["currency"] == "USD"

    bravo = universe.by_ticker["BBBB"]
    assert bravo["loss_making"] and bravo["pe"] is None
    # The suspension five days ago counts; the one dated after the latest day does not.
    assert [flag["code"] for flag in bravo["flags"]][:3] == ["suspended", "loss", "illiquid"]
    assert len(bravo["notices"]) == 1
    assert bravo["dividends_12m"] == 0.0 and bravo["dividend_yield_pct"] == 0.0 and bravo["last_dividend"] is None

    # Ranked within the sector: AAAA beat IHSG most, BBBB stood still, CCCC fell.
    assert alpha["scores"]["momentum"] == 100 and universe.by_ticker["BBBB"]["scores"]["momentum"] == 50
    assert universe.by_ticker["CCCC"]["scores"]["momentum"] == 0
    assert alpha["scores"]["quality"] > universe.by_ticker["CCCC"]["scores"]["quality"]
    assert alpha["peer_count"] == 3 and alpha["peer_group"] == "Energy"

    assert detail["row"]["ticker"] == "AAAA" and len(detail["prices"]["dates"]) == 300
    assert detail["prices"]["ihsg_rebased"][0] == pytest.approx(1000.0)
    assert [s["fs_date"] for s in detail["statements"]] == ["2024-09-30", "2025-09-30"]
    assert [d["cash_per_share"] for d in detail["dividends"]] == [20.0, 15.0]
    assert {peer["ticker"] for peer in detail["peers"]} == {"AAAA", "BBBB", "CCCC"}


def test_statements_drop_idx_duplicates_and_margin_uses_one_period():
    from stock_triage.stocks.metrics import fundamental_metrics, statement_kind, statements_by_date

    def row(month, fs, sales, profit, kind="U", npm=109.8):
        return {"report_month": month, "fs_date": fs, "fs_type": kind, "sales": sales, "profit_attr_owner": profit, "equity": 270e12,
                "eps": 470.0, "book_value": 2200.0, "per": 11.0, "pbv": 2.4, "roe": 21.4, "roa": 3.5, "npm": npm, "der": 5.1, "sharia": 0}

    ratios = [
        row("2025-10", "2025-09-30", 77.1e12, 43.4e12),
        row("2025-11", "2025-10-20", 77.1e12, 43.4e12),  # IDX lists the September figures again under a later date
        row("2026-08", "2026-06-30", 52.9e12, 29.5e12),
        row("2026-09", "2025-06-30", 51.2e12, 29.0e12, kind="Ditelaah secara Terbatas / Limited Review"),
    ]
    statements = statements_by_date(sorted(ratios, key=lambda r: r["report_month"]))
    numbers = fundamental_metrics(ratios[:3] + [ratios[3]], price=5275.0, as_of=date(2026, 10, 21))

    assert list(statements) == ["2025-06-30", "2025-09-30", "2026-06-30"]
    assert [statement_kind(code) for code in ("A", "Diaudit / Audited", "U", "Ù", "LR", "#N/A", None)] == [
        "audited", "audited", "unaudited", "unaudited", "limited review", None, None
    ]
    # IDX's 109.8% mixes a trailing year's profit with half a year's sales.
    latest = fundamental_metrics(ratios[:3], price=5275.0, as_of=date(2026, 10, 21))
    assert latest["npm_pct"] == pytest.approx(29.5 / 52.9 * 100)
    assert numbers["fs_type"] == "limited review"


def test_statement_dates_with_a_time_still_give_growth():
    from stock_triage.stocks.metrics import fundamental_metrics, load_ratios
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE company_ratios (ticker, report_month, fs_date, sales, profit_attr_owner, equity, eps, book_value, per, pbv, roe, roa, npm, der, sharia, fs_type)")
    conn.executemany(
        "INSERT INTO company_ratios VALUES ('AAAA', ?, ?, ?, ?, 900e9, 50, 400, 10, 2, 20, 6, 12, 0.5, 0, 'U')",
        [("2025-11", "2024-09-30T00:00:00", 500e9, 60e9), ("2025-12", "2025-09-30T00:00:00", 600e9, 66e9), ("2026-01", "30/09/2025", 600e9, 66e9)],
    )
    ratios = load_ratios(conn)["AAAA"]

    assert [r["fs_date"] for r in ratios] == ["2024-09-30", "2025-09-30", None]
    numbers = fundamental_metrics(ratios[:2], price=1000.0, as_of=date(2026, 1, 20))
    assert numbers["sales_yoy_pct"] == pytest.approx(20.0)
    assert fundamental_metrics(ratios, price=1000.0, as_of=date(2026, 1, 20))["sales_yoy_pct"] is None


def test_dividends_follow_splits_and_are_counted_once():
    from stock_triage.stocks.metrics import dividend_metrics, load_dividends

    # Rp 200 before a 1:4 split on 1 July is Rp 50 on today's shares.
    numbers = dividend_metrics(
        [{"ex_date": "2026-05-10", "cash_per_share": 200.0, "currency": "IDR"}],
        price=1250.0, eps=100.0, as_of=date(2026, 10, 21), usd={}, adjustments=[(date(2026, 7, 1), 0.25)],
    )
    assert numbers["dividends_12m"] == 50.0 and numbers["dividend_yield_pct"] == pytest.approx(4.0)

    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE dividends (ticker, ex_date, cash_per_share, currency, note, payment_date, record_date, cum_date, report_month, source)"
    )
    conn.executemany(
        "INSERT INTO dividends VALUES (?, ?, ?, 'IDR', NULL, NULL, ?, NULL, ?, ?)",
        [
            ("ADRO", "2026-05-20", 145.0, "2026-05-20", "2026-05", "idx_dividends"),  # listed before its ex-date was known
            ("ADRO", "2026-05-19", 145.0, "2026-05-20", "2026-06", "idx_dividends"),  # the same dividend, revised
            ("TLKM", "2026-06-18", 222.1, "2026-06-19", "2026-10", "idx_profiles"),  # estimated from the total paid
            ("TLKM", "2026-06-18", 222.0, "2026-06-19", "2026-07", "idx_dividends"),
        ],
    )
    paid = load_dividends(conn)
    assert [(d["ex_date"], d["cash_per_share"]) for d in paid["ADRO"]] == [("2026-05-19", 145.0)]
    assert [(d["cash_per_share"], d["source"]) for d in paid["TLKM"]] == [(222.0, "idx_dividends")]


def test_negative_equity_does_not_score_as_quality():
    from stock_triage.stocks.metrics import _score

    rows = [
        {"ticker": "NEG", "sector": "Industrials", "roe_pct": 40.0, "npm_pct": -5.0, "der": -6.0, "equity": -1e9},
        {"ticker": "OKAY", "sector": "Industrials", "roe_pct": 12.0, "npm_pct": 8.0, "der": 0.8, "equity": 5e9},
        {"ticker": "GOOD", "sector": "Industrials", "roe_pct": 20.0, "npm_pct": 15.0, "der": 0.3, "equity": 9e9},
    ]
    _score(rows)

    quality = {row["ticker"]: row["scores"]["quality"] for row in rows}
    assert quality["NEG"] == 0 and quality["GOOD"] > quality["OKAY"]
