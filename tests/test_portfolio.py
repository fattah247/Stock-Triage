from __future__ import annotations

import pytest

from stock_triage.db import connect
from stock_triage.stocks import HoldingsFileError, build_universe, load_holdings, portfolio_view

from .test_stock_metrics import _seed_universe


def test_portfolio_values_positions_and_reads_the_holdings_file(tmp_config, tmp_path):
    _seed_universe(tmp_config)
    path = tmp_path / "portfolio.yaml"
    path.write_text("cash: 1000000\nholdings:\n  - ticker: aaaa\n    lots: 10\n    avg_price: 900\n  - ticker: ZZZZ\n    lots: 1\n    avg_price: 100\n")
    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        universe = build_universe(conn)
    finally:
        conn.close()

    holdings = load_holdings(path)
    view = portfolio_view(universe, holdings)

    close = universe.by_ticker["AAAA"]["close"]
    alpha = next(p for p in view["positions"] if p["ticker"] == "AAAA")
    assert alpha["value"] == pytest.approx(1000 * close) and alpha["gain"] == pytest.approx(1000 * (close - 900))
    assert view["total"] == pytest.approx(1000 * close + 1e6) and view["cash_pct"] == pytest.approx(1e6 / view["total"] * 100)
    assert next(p for p in view["positions"] if p["ticker"] == "ZZZZ")["missing"] is True
    assert view["beta"] == pytest.approx(alpha["beta"]) and view["largest"]["ticker"] == "AAAA"
    assert view["dividend_income"] == pytest.approx(1000 * 20.0) and view["yield_on_cost_pct"] == pytest.approx(20.0 / 900 * 100)
    assert load_holdings(tmp_path / "absent.yaml") is None
    path.write_text("holdings:\n  - ticker: AAAA\n    lots: ten\n    avg_price: 900\n")
    with pytest.raises(HoldingsFileError, match="holding 1 needs"):
        load_holdings(path)


def test_a_malformed_holdings_file_is_reported_not_raised(tmp_path):
    path = tmp_path / "portfolio.yaml"
    for text in ("holdings:\n  - BBCA\n", "holdings:\n  -\n", "- ticker: BBCA\n", "holdings: BBCA\n"):
        path.write_text(text)
        with pytest.raises(HoldingsFileError):
            load_holdings(path)
