from __future__ import annotations

import json
import threading
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from stock_triage.dashboard.server import create_dashboard_server

from .test_stock_metrics import _seed_universe


def test_decision_views_are_served(tmp_config):
    _seed_universe(tmp_config)
    handle = create_dashboard_server(tmp_config, host="127.0.0.1", port=0)
    thread = threading.Thread(target=handle.server.serve_forever, daemon=True)
    thread.start()

    def get(path):
        return json.loads(urlopen(f"{handle.url}{path}").read().decode("utf-8"))

    try:
        stocks = get("/api/stocks")
        stock = get("/api/stock?ticker=AAAA")
        with pytest.raises(HTTPError) as missing:
            urlopen(f"{handle.url}/api/stock?ticker=NOPE")
        empty_portfolio = get("/api/portfolio")
        tmp_config.market.portfolio_path.write_text("holdings:\n  - ticker: AAAA\n    lots: 2\n    avg_price: 1000\n")
        portfolio = get("/api/portfolio")
        overview = get("/api/overview")
        script = urlopen(f"{handle.url}/static/decide.js").read().decode("utf-8")
        html = urlopen(handle.url).read().decode("utf-8")
    finally:
        handle.server.shutdown()
        thread.join(timeout=5)

    assert [row["ticker"] for row in stocks["rows"]] == ["CCCC", "AAAA", "BBBB"] and stocks["sectors"] == ["Energy"]
    assert stock["row"]["ticker"] == "AAAA" and stock["prices"]["close"]
    assert missing.value.code == 404
    assert empty_portfolio["portfolio"] is None and empty_portfolio["path"].endswith("portfolio.yaml")
    assert portfolio["portfolio"]["positions"][0]["lots"] == 2
    assert overview["market"]["latest"]["close"] > 0 and overview["portfolio"]["total"] > 0
    assert overview["stats"]["stocks"] == 3 and [row["ticker"] for row in overview["movers"]["up"]] == ["AAAA", "CCCC"]
    assert "window.StockTriageDecide" in script
    assert 'data-view="stocks"' in html and "/decide.js" in html
