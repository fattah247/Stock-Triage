"""The dashboard's decision views: Overview, Stocks (screener and stock page) and Portfolio.

Each loads on its own (``/api/overview``, ``/api/stocks``, ``/api/stock``,
``/api/portfolio``). The per-stock numbers are computed once per data update
and kept in memory until ``market-sync`` stores a newer day or ratio table.
"""

from __future__ import annotations

import math
import threading
from typing import Any

from ..config import Config
from ..db import WriterQueue, configure_pragmas, connect, run_migrations
from ..stocks import HoldingsFileError, Universe, build_universe, load_holdings, portfolio_view, stock_detail
from ..stocks.metrics import SCORE_THEMES
from .market import _build as build_market

_CACHE: dict[str, tuple[tuple, Universe]] = {}
_LOCK = threading.Lock()

# The screener's columns, in the order the table shows them.
SCREEN_KEYS = (
    "ticker", "name", "sector", "industry", "close", "change_1d_pct", "market_cap", "avg_value_20d",
    "pe", "pbv", "pe_history_pct", "roe_pct", "npm_pct", "der", "sales_yoy_pct", "profit_yoy_pct",
    "ret_1m_pct", "ret_3m_pct", "ret_1y_pct", "ret_ytd_pct", "rel_1y_pts", "below_high_pct", "range_position_pct",
    "volatility_pct", "beta", "foreign_net_20d", "above_ma200", "earnings_yield_pct", "has_fundamentals",
    "loss_making", "sharia", "scores", "flags", "peer_pe", "peer_pbv", "peer_roe_pct", "fs_date",
    "dividend_yield_pct", "payout_pct", "dividend_years", "peer_dividend_yield_pct",
)


def load_stocks_view(cfg: Config, watchlist: list[str]) -> dict[str, Any]:
    with _reader(cfg) as conn:
        universe = _universe(conn, cfg)
    watchlist = sorted({ticker.upper() for ticker in watchlist})
    return _finite(
        {
            "as_of": universe.as_of.isoformat() if universe.as_of else None,
            "rows": [{key: row.get(key) for key in SCREEN_KEYS} for row in universe.rows],
            "sectors": universe.sectors,
            "watchlist": watchlist,
            "score_themes": SCORE_THEMES,
        }
    )


def load_stock_view(cfg: Config, ticker: str, watchlist: list[str]) -> dict[str, Any] | None:
    with _reader(cfg) as conn:
        universe = _universe(conn, cfg)
        detail = stock_detail(conn, universe, ticker)
        if detail is None:
            return None
        market = build_market(conn, cfg)
    detail["market_state"] = (market.get("state") or {}).get("regime") if market.get("has_data") else None
    detail["watchlisted"] = ticker.upper() in {t.upper() for t in watchlist}
    return _finite(detail)


def load_portfolio_view(cfg: Config, watchlist: list[str]) -> dict[str, Any]:
    with _reader(cfg) as conn:
        universe = _universe(conn, cfg)
    out: dict[str, Any] = {
        "as_of": universe.as_of.isoformat() if universe.as_of else None,
        "path": str(cfg.market.portfolio_path),
        "watchlist": _watchlist_rows(universe, watchlist),
    }
    try:
        holdings = load_holdings(cfg.market.portfolio_path)
    except HoldingsFileError as exc:
        out["error"] = str(exc)
        return _finite(out)
    out["portfolio"] = portfolio_view(universe, holdings) if holdings else None
    return _finite(out)


def load_overview(cfg: Config, watchlist: list[str]) -> dict[str, Any]:
    with _reader(cfg) as conn:
        universe = _universe(conn, cfg)
        market = build_market(conn, cfg)
    out: dict[str, Any] = {"as_of": universe.as_of.isoformat() if universe.as_of else None, "market": _market_summary(market)}
    try:
        holdings = load_holdings(cfg.market.portfolio_path)
        out["portfolio"] = portfolio_view(universe, holdings) if holdings else None
    except HoldingsFileError as exc:
        out["portfolio_error"] = str(exc)
    out["portfolio_path"] = str(cfg.market.portfolio_path)
    out["watchlist"] = _watchlist_rows(universe, watchlist)
    liquid = [row for row in universe.rows if (row.get("avg_value_20d") or 0) >= 5e9 and row.get("change_1d_pct") is not None]
    out["movers"] = {
        "up": [_brief(row) for row in sorted(liquid, key=lambda row: -row["change_1d_pct"])[:6]],
        "down": [_brief(row) for row in sorted(liquid, key=lambda row: row["change_1d_pct"])[:6]],
    }
    out["sectors"] = _sector_board(universe)
    out["stats"] = {
        "stocks": len(universe.rows),
        "with_fundamentals": sum(1 for row in universe.rows if row.get("has_fundamentals")),
        "up": sum(1 for row in universe.rows if (row.get("change_1d_pct") or 0) > 0),
        "down": sum(1 for row in universe.rows if (row.get("change_1d_pct") or 0) < 0),
    }
    return _finite(out)


def _finite(value: Any) -> Any:
    """The payload with NaN and infinities as null, which JSON cannot carry."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _finite(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(item) for item in value]
    return value


def _universe(conn, cfg: Config) -> Universe:
    key = tuple(
        conn.execute(sql).fetchone()[0]
        for sql in (
            "SELECT MAX(trade_date) FROM stock_daily",
            "SELECT COUNT(*) FROM stock_daily WHERE trade_date = (SELECT MAX(trade_date) FROM stock_daily)",
            "SELECT MAX(retrieved_at) FROM company_ratios",
            "SELECT MAX(fetched_at) FROM exchange_notices",
            "SELECT MAX(last_seen_at) FROM companies",
            # Every stock, ratio, dividend, profile and notice fetch is in this ledger.
            "SELECT MAX(fetched_at) FROM market_sync_days",
            "SELECT MAX(fetched_at) FROM company_profile_fetches",
        )
    )
    with _LOCK:
        cached = _CACHE.get(str(cfg.paths.db_path))
        if cached and cached[0] == key:
            return cached[1]
        universe = build_universe(conn)
        _CACHE[str(cfg.paths.db_path)] = (key, universe)
        return universe


class _reader:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def __enter__(self):
        self.cfg.ensure_dirs()
        with WriterQueue(self.cfg) as wq:
            wq.submit(run_migrations)
        self.conn = connect(self.cfg.paths.db_path, read_only=True)
        configure_pragmas(self.conn, self.cfg)
        return self.conn

    def __exit__(self, *exc) -> None:
        self.conn.close()


def _brief(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "ticker", "name", "sector", "close", "change_1d_pct", "ret_1m_pct", "ret_1y_pct", "below_high_pct", "pe",
        "dividend_yield_pct", "scores", "flags", "market_cap", "avg_value_20d",
    )
    return {key: row.get(key) for key in keys}


def _watchlist_rows(universe: Universe, watchlist: list[str]) -> list[dict[str, Any]]:
    tickers = sorted({ticker.upper() for ticker in watchlist})
    return [_brief(universe.by_ticker[ticker]) if ticker in universe.by_ticker else {"ticker": ticker, "missing": True} for ticker in tickers]


def _sector_board(universe: Universe) -> list[dict[str, Any]]:
    """Each sector's median moves and valuation, from its stocks."""
    import statistics

    groups: dict[str, list[dict[str, Any]]] = {}
    for row in universe.rows:
        if row.get("sector"):
            groups.setdefault(row["sector"], []).append(row)

    def median(rows, key):
        values = [row[key] for row in rows if row.get(key) is not None]
        return statistics.median(values) if values else None

    board = [
        {
            "sector": sector,
            "stocks": len(rows),
            "change_1d_pct": median(rows, "change_1d_pct"),
            "ret_1m_pct": median(rows, "ret_1m_pct"),
            "ret_1y_pct": median(rows, "ret_1y_pct"),
            "pe": median([row for row in rows if not row.get("loss_making")], "pe"),
            "pbv": median(rows, "pbv"),
            "roe_pct": median(rows, "roe_pct"),
            "market_cap": sum(row.get("market_cap") or 0.0 for row in rows),
        }
        for sector, rows in groups.items()
    ]
    return sorted(board, key=lambda row: -(row["market_cap"] or 0.0))


def _market_summary(market: dict[str, Any]) -> dict[str, Any] | None:
    if not market.get("has_data"):
        return None
    daily = market["daily"]
    risk = market.get("crash_risk") or {}
    return {
        "latest": market["latest"],
        "state": market.get("state"),
        "spark": {"dates": daily["dates"][-130:], "close": daily["close"][-130:], "regime": daily["regime"][-130:]},
        "crash_risk": {
            "trustworthy": risk.get("record_trustworthy"),
            "probability": risk.get("probability"),
            "base_rate": risk.get("base_rate"),
            "skill": risk.get("skill"),
            "analogs": (risk.get("analogs") or [])[:3],
        },
        "calendar": market.get("calendar", [])[:8],
        "themes": [{"title": item.get("title"), "severity": item.get("severity"), "items": item.get("items")} for item in (market.get("themes") or {}).get("active", [])[:6]],
    }
