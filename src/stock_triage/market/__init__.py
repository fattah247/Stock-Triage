"""Market risk layer (docs/market_risk_plan.md).

Phase 1: risk calendar, exposure ranking, and fundamental stress breadth.
Phase 2: daily market data (``market-sync``), signals, and the market state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

from ..config import Config
from ..db import WriterQueue, configure_pragmas, connect, run_migrations
from .breadth import BreadthRow, compute_breadth, load_annual_facts, persist_breadth
from .catalysts import Catalyst, CatalystFileError, CatalystSyncResult, load_upcoming_catalysts, sync_catalysts
from .discovery import WatchSyncResult, active_themes, run_watch_sync
from .exposure import ExposureScore, compute_exposure_scores, load_latest_exposure, persist_exposure_scores
from .signals import MarketDay, MarketSignalsResult, load_market_state, run_market_signals
from .sync import MarketSyncResult, run_market_sync
from .themes import load_themes


@dataclass(frozen=True)
class ExposureRunResult:
    as_of: date
    scores: list[ExposureScore]
    breadth: list[BreadthRow]


def run_exposure(cfg: Config, *, as_of: date | None = None) -> ExposureRunResult:
    """Score every company with figures and measure stress breadth, as of ``as_of``."""
    cfg.ensure_dirs()
    as_of = as_of or datetime.now(timezone.utc).date()
    watchlist = {ticker.upper() for ticker in cfg.alerts.watchlist}
    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
        reader = connect(cfg.paths.db_path, read_only=True)
        try:
            configure_pragmas(reader, cfg)
            themes = active_themes(
                reader,
                themes=load_themes(reader, cfg.market.risk_themes_path),
                as_of=as_of,
                min_items=cfg.market.theme_active_min_items,
            )
            scores = compute_exposure_scores(
                reader,
                index_membership_path=cfg.market.index_membership_path,
                watchlist=watchlist,
                active_themes=themes,
            )
            sectors = {row["ticker"]: row["sector"] for row in reader.execute("SELECT ticker, sector FROM companies")}
            breadth = compute_breadth(
                load_annual_facts(reader),
                sectors=sectors,
                thresholds_pts=cfg.market.breadth_thresholds_pts,
                min_filers=cfg.market.breadth_min_filers,
            )
        finally:
            reader.close()
        wq.submit(lambda conn: persist_exposure_scores(conn, scores=scores, as_of=as_of))
        wq.submit(lambda conn: persist_breadth(conn, rows=breadth, as_of=as_of))
    return ExposureRunResult(as_of=as_of, scores=scores, breadth=breadth)


__all__ = [
    "BreadthRow",
    "Catalyst",
    "CatalystFileError",
    "CatalystSyncResult",
    "ExposureRunResult",
    "ExposureScore",
    "MarketDay",
    "MarketSignalsResult",
    "MarketSyncResult",
    "WatchSyncResult",
    "load_latest_exposure",
    "load_market_state",
    "load_upcoming_catalysts",
    "run_exposure",
    "run_market_signals",
    "run_market_sync",
    "run_watch_sync",
    "sync_catalysts",
]
