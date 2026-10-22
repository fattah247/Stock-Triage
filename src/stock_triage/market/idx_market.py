"""IDX daily market summaries (docs/phase2_market_recon.md).

Two endpoints, both taking ``date=YYYYMMDD``:

- ``GetIndexSummary``: every headline and IDX-IC sector index's close;
- ``GetStockSummary``: one row per stock, for breadth, turnover, and foreign
  buy and sell volumes.

They are fetched from inside an opened IDX page, like the discovery adapters,
because IDX sits behind a managed challenge. Bodies are not saved: a backfill
would otherwise leave hundreds of megabytes of artifacts.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from typing import Any, Protocol

SOURCE_NAME = "idx_market"
INDEX_SUMMARY_PATH = "/primary/TradingSummary/GetIndexSummary"
STOCK_SUMMARY_PATH = "/primary/TradingSummary/GetStockSummary"
FOREIGN_NET_SERIES = "flow:foreign_net_value_est"


class IDXMarketFetcher(Protocol):
    def fetch(self, path: str, trade_date: date) -> dict[str, Any]:
        """The JSON payload of ``path`` for ``trade_date``."""

    def fetch_url(self, url: str) -> dict[str, Any]:
        """The JSON payload of any IDX URL, fetched the same way."""


def summary_url(path: str, trade_date: date) -> str:
    return f"https://www.idx.co.id{path}?date={trade_date.strftime('%Y%m%d')}&length=9999&start=0"


def rows_of(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("data")
    if rows is None:
        rows = payload.get("Items")
    return list(rows or [])


def parse_index_summary(payload: dict[str, Any]) -> dict[str, float]:
    """``index:<code>`` closes, plus the composite's traded value."""
    series: dict[str, float] = {}
    for row in rows_of(payload):
        code, close = row.get("IndexCode"), row.get("Close")
        if not code or close is None:
            continue
        series[f"index:{code}"] = float(close)
        if code == "COMPOSITE" and row.get("Value") is not None:
            series["turnover:value"] = float(row["Value"])
    return series


def parse_stock_summary(payload: dict[str, Any]) -> dict[str, float]:
    """Breadth and the estimated foreign net value for one day.

    IDX reports foreign trades as share volumes. Each stock's net is valued at
    its average traded price (value over volume), so the total is an estimate,
    not IDX's own foreign-flow figure.
    """
    advancers = decliners = unchanged = 0
    foreign_net = 0.0
    priced = 0
    for row in rows_of(payload):
        previous, close = row.get("Previous"), row.get("Close")
        if previous and close:
            if close > previous:
                advancers += 1
            elif close < previous:
                decliners += 1
            else:
                unchanged += 1
        volume, value = row.get("Volume") or 0, row.get("Value") or 0
        net_shares = (row.get("ForeignBuy") or 0) - (row.get("ForeignSell") or 0)
        if net_shares and volume:
            foreign_net += net_shares * (value / volume)
            priced += 1
    series = {
        "breadth:advancers": float(advancers),
        "breadth:decliners": float(decliners),
        "breadth:unchanged": float(unchanged),
    }
    if priced:
        series[FOREIGN_NET_SERIES] = foreign_net
    return series


class BrowserIDXFetcher:
    """Fetches from inside one opened IDX page, at most ``rate_per_second`` calls."""

    def __init__(self, page: Any, *, rate_per_second: float) -> None:
        self._page = page
        self._interval = 1.0 / rate_per_second if rate_per_second > 0 else 0.0
        self._last = 0.0

    def fetch(self, path: str, trade_date: date) -> dict[str, Any]:
        try:
            return self.fetch_url(summary_url(path, trade_date))
        except RuntimeError as exc:
            raise RuntimeError(f"{exc} for {path} on {trade_date.isoformat()}") from None

    def fetch_url(self, url: str) -> dict[str, Any]:
        wait = self._interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        result = self._page.evaluate(
            """async (targetUrl) => {
                const res = await fetch(targetUrl, { credentials: 'include' });
                return { status: res.status, text: await res.text() };
            }""",
            url,
        )
        if int(result["status"]) != 200:
            raise RuntimeError(f"IDX returned {result['status']}")
        return json.loads(result["text"] or "{}")


@contextmanager
def open_idx_fetcher(cfg) -> Iterator[BrowserIDXFetcher]:
    """An IDX page in the pipeline's persistent browser profile (see ``recon.browser``)."""
    from ..recon.browser import PlaywrightSession

    session = PlaywrightSession(
        profile_dir=cfg.browser.profile_dir,
        artifacts_dir=cfg.browser.artifacts_dir,
        headless=cfg.browser.headless,
        challenge_timeout_s=cfg.browser.challenge_timeout_s,
        page_settle_ms=cfg.browser.page_settle_ms,
        channel=cfg.browser.channel,
    )
    with session.open_page(page_url=cfg.market.idx_page_url) as page:
        yield BrowserIDXFetcher(page, rate_per_second=cfg.discovery.rate_limit_per_second)
