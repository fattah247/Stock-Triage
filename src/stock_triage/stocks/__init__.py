"""Per-stock numbers for deciding what to buy, hold or avoid (docs/stock_decisions.md).

Everything here is computed from IDX's own data that ``market-sync`` stores:
the daily stock summary (``stock_daily``) and the monthly Financial Data and
Ratio table (``company_ratios``). It describes; it does not recommend.
"""

from .metrics import Universe, build_universe, stock_detail

__all__ = ["Universe", "build_universe", "stock_detail"]
