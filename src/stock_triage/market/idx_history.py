"""IHSG daily closes before 2020, from IDX's own digital statistics.

``GetIndexSummary`` serves nothing before 2020-01-02. IDX's quarterly digital
statistics still chart the composite's daily close ("Composite Stock Price
Index and Stock Trading Volume"): each Q4 report covers the year it names and
the two before it, back to 2009-01-05. Reports for earlier years come back
with an empty series. IDX's robots.txt is empty, and the call goes through the
IDX page ``market-sync`` already opens, like every other IDX fetch.

Closes go into ``market_series_daily`` under ``idx_statistics``. A day
``GetIndexSummary`` supplied is never overwritten, and the reports are checked
against those days first: when most differ by more than 2%, the series was
misread and nothing is written. A day imported from a CSV is replaced, since
IDX's own figure is the better one.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import quote

from .idx_market import SOURCE_NAME as IDX_SOURCE
from .idx_market import IDXMarketFetcher

SOURCE_NAME = "idx_statistics"
# Two sources agree on a day when their closes are this close. When most shared
# days disagree, one of them holds another index or was misread.
AGREEMENT_PCT = 2.0
SERIES_ID = "index:COMPOSITE"
HISTORY_PATH = "/primary/DigitalStatistic/GetApiData"
# The newest report fetched (2018-2020) overlaps GetIndexSummary's first year,
# which is what the agreement check compares.
LAST_REPORT_YEAR = 2020
REPORT_YEARS = 3
# A floor for the walk back; in practice it stops at the first empty report.
OLDEST_REPORT_YEAR = 1990


@dataclass
class HistoryStored:
    stored: int = 0
    idx_overlap: int = 0
    idx_disagreeing: int = 0
    first: date | None = None
    last: date | None = None


def history_url(year: int) -> str:
    """The Q4 ``year`` report: daily closes for ``year - 2`` through ``year``."""
    query = json.dumps({"year": str(year), "month": "0", "quarter": 4, "type": "quarterly"}, separators=(",", ":"))
    encoded = quote(base64.b64encode(query.encode()).decode(), safe="")
    return (
        f"https://www.idx.co.id{HISTORY_PATH}?urlName=LINK_DPS_COMPOSITE_STOCK_PRICE&cumulative=false&query={encoded}"
    )


def parse_history(payload: dict[str, Any]) -> dict[date, float]:
    """The composite's daily closes in one report; an error when the report has no composite series."""
    for series in payload.get("series") or []:
        if str(series.get("seriesName") or "").lower().startswith("composite index"):
            closes: dict[date, float] = {}
            for point in series.get("seriesData") or []:
                day, value = _parse_day(point.get("x")), point.get("y")
                if day is not None and isinstance(value, (int, float)) and value > 0:
                    closes[day] = float(value)
            return closes
    raise ValueError("no Composite Index series in IDX's statistics report")


def fetch_history(fetcher: IDXMarketFetcher) -> dict[date, float]:
    """Every daily close the reports hold, walking back three years at a time until one is empty."""
    closes: dict[date, float] = {}
    for year in range(LAST_REPORT_YEAR, OLDEST_REPORT_YEAR - 1, -REPORT_YEARS):
        report = parse_history(fetcher.fetch_url(history_url(year)))
        if not report:
            break
        closes.update(report)
    return closes


def store_history(conn: sqlite3.Connection, *, closes: dict[date, float], fetched_on: date) -> HistoryStored:
    """Store the closes GetIndexSummary did not supply, after checking them against the ones it did."""
    idx_days = {
        date.fromisoformat(row["trade_date"]): float(row["value"])
        for row in conn.execute(
            "SELECT trade_date, value FROM market_series_daily WHERE series_id = ? AND source_name = ?",
            (SERIES_ID, IDX_SOURCE),
        )
    }
    result = HistoryStored()
    shared = [(close, idx_days[day]) for day, close in closes.items() if day in idx_days]
    result.idx_overlap = len(shared)
    result.idx_disagreeing = sum(1 for mine, idx in shared if abs(mine - idx) > abs(idx) * AGREEMENT_PCT / 100.0)
    if result.idx_disagreeing * 2 > result.idx_overlap:
        raise ValueError(
            f"{result.idx_disagreeing} of the {result.idx_overlap} days both sources hold differ by more than "
            f"{AGREEMENT_PCT:g}%; the statistics report was misread, so nothing was written"
        )

    new = sorted((day, close) for day, close in closes.items() if day not in idx_days)
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        """
        INSERT INTO market_series_daily (series_id, trade_date, value, source_name, retrieved_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(series_id, trade_date) DO UPDATE SET
            value = excluded.value, source_name = excluded.source_name, retrieved_at = excluded.retrieved_at
        """,
        [(SERIES_ID, day.isoformat(), close, SOURCE_NAME, now) for day, close in new],
    )
    conn.execute(
        """
        INSERT INTO market_sync_days (source_name, trade_date, status, row_count, fetched_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source_name, trade_date) DO UPDATE SET
            status = excluded.status, row_count = excluded.row_count, fetched_at = excluded.fetched_at
        """,
        (SOURCE_NAME, fetched_on.isoformat(), "ok" if new else "no_data", len(new), now),
    )
    result.stored = len(new)
    if new:
        result.first, result.last = new[0][0], new[-1][0]
    return result


def _parse_day(text: Any) -> date | None:
    try:
        return date.fromisoformat(str(text)[:10])
    except ValueError:
        return None
