"""``market-sync``: daily IDX summaries and Bank Indonesia's JISDOR into ``market_series_daily``.

The first sync also fetches the history before IDX's summaries begin in 2020:
the composite's daily closes from 2009 out of IDX's statistics reports
(``idx_history``), and JISDOR from its first fixing in 2013.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import httpx

from ..config import Config
from ..db import WriterQueue, configure_pragmas, connect, run_migrations
from .idx_market import (
    INDEX_SUMMARY_PATH,
    STOCK_SUMMARY_PATH,
    IDXMarketFetcher,
    open_idx_fetcher,
    parse_index_summary,
    parse_stock_summary,
    rows_of,
)
from .idx_market import SOURCE_NAME as IDX_SOURCE
from .jisdor import SERIES_ID as JISDOR_SERIES
from .jisdor import SOURCE_NAME as JISDOR_SOURCE
from .exchange_notices import SOURCE_NAME as NOTICES_SOURCE
from .exchange_notices import fetch_exchange_notices, persist_notices, write_notice_series
from .idx_history import SOURCE_NAME as HISTORY_SOURCE
from .idx_history import fetch_history, store_history
from .jisdor import FIRST_DAY as JISDOR_FIRST_DAY
from .jisdor import fetch_jisdor_export, parse_jisdor_export

# Without a backfill, a sync looks back this far for missing days.
DEFAULT_LOOKBACK_DAYS = 10
# History fills the years before IDX's summaries start (2020-01-02); from then
# on, the sync's own range covers both sources.
HISTORY_END_YEAR = 2019
# This many failed days in a row means the source is down or the browser is gone,
# not that a few days are bad: stop and leave the rest for the next run.
MAX_CONSECUTIVE_FAILURES = 3
_JISDOR_CONNECT_RETRIES = 3


@dataclass
class MarketSyncResult:
    start: date
    end: date
    idx_days_fetched: int = 0
    idx_days_no_data: int = 0
    idx_days_skipped: int = 0
    idx_days_left: int = 0
    jisdor_rates: int = 0
    notices_new: int = 0
    history_days: int = 0
    history_first: date | None = None
    history_last: date | None = None
    errors: list[str] = field(default_factory=list)


def run_market_sync(
    cfg: Config,
    *,
    start: date | None = None,
    end: date | None = None,
    refresh: bool = False,
    history: bool = True,
    idx_fetcher_factory: Callable[[], AbstractContextManager[IDXMarketFetcher]] | None = None,
    jisdor_client: httpx.Client | None = None,
) -> MarketSyncResult:
    """Fetch every weekday in ``start``..``end`` not yet synced (all of them with ``refresh``).

    With ``history``, also fetch the pre-2020 history still missing: IDX's
    statistics reports once (again with ``refresh``), and each JISDOR year
    from 2013 that holds no rate yet.
    """
    cfg.ensure_dirs()
    end = end or datetime.now(timezone.utc).date()
    start = start or end - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    result = MarketSyncResult(start=start, end=end)

    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
        reader = connect(cfg.paths.db_path, read_only=True)
        try:
            configure_pragmas(reader, cfg)
            done = set() if refresh else _synced_days(reader, IDX_SOURCE)
            # Notices are fetched once a day, even when every trading day is already synced.
            notices_due = refresh or end not in _synced_days(reader, NOTICES_SOURCE)
            history_due = history and (refresh or not _synced_days(reader, HISTORY_SOURCE))
            jisdor_history = _missing_jisdor_years(reader) if history else []
        finally:
            reader.close()

        pending = [day for day in _weekdays(start, end) if day not in done]
        result.idx_days_skipped = len(list(_weekdays(start, end))) - len(pending)
        if pending or notices_due or history_due:
            factory = idx_fetcher_factory or (lambda: open_idx_fetcher(cfg))
            with factory() as fetcher:
                failures = 0
                for position, day in enumerate(pending):
                    try:
                        series, row_count = _fetch_idx_day(fetcher, day)
                    except Exception as exc:  # noqa: BLE001 - one bad day must not lose the others
                        result.errors.append(f"{IDX_SOURCE} {day.isoformat()}: {exc}")
                        failures += 1
                        if failures >= MAX_CONSECUTIVE_FAILURES:
                            result.idx_days_left = len(pending) - position - 1
                            break
                        continue
                    failures = 0
                    wq.submit(
                        lambda conn, d=day, s=series, n=row_count: _persist_day(
                            conn, source_name=IDX_SOURCE, trade_date=d, series=s, row_count=n
                        )
                    )
                    if series:
                        result.idx_days_fetched += 1
                    else:
                        result.idx_days_no_data += 1
                if notices_due and not result.idx_days_left:
                    # Suspensions and UMA notices (phase 2b), through the same page.
                    try:
                        notices = fetch_exchange_notices(fetcher)
                    except Exception as exc:  # noqa: BLE001
                        result.errors.append(f"{NOTICES_SOURCE}: {exc}")
                    else:
                        result.notices_new = wq.submit(lambda conn, n=notices: persist_notices(conn, notices=n))
                        wq.submit(lambda conn, n=len(notices): _record_ledger(conn, source_name=NOTICES_SOURCE, trade_date=end, row_count=n))
                if history_due and not result.idx_days_left:
                    try:
                        closes = fetch_history(fetcher)
                        stored = wq.submit(lambda conn, c=closes: store_history(conn, closes=c, fetched_on=end))
                    except Exception as exc:  # noqa: BLE001 - the daily sync must not fail with it
                        result.errors.append(f"{HISTORY_SOURCE}: {exc}")
                    else:
                        result.history_days = stored.stored
                        result.history_first, result.history_last = stored.first, stored.last
        wq.submit(write_notice_series)

        ranges = list(_year_ranges(start, end))
        covered = {year_start.year for year_start, _ in ranges}
        ranges += [_full_year(year) for year in jisdor_history if year not in covered]
        client_context = nullcontext(jisdor_client) if jisdor_client is not None else _jisdor_client(cfg)
        with client_context as client:
            for year_start, year_end in sorted(ranges):
                try:
                    rates = parse_jisdor_export(
                        fetch_jisdor_export(client, url=cfg.market.jisdor_url, start=year_start, end=year_end)
                    )
                except Exception as exc:  # noqa: BLE001
                    result.errors.append(f"{JISDOR_SOURCE} {year_start.year}: {exc}")
                    continue
                wq.submit(lambda conn, r=rates: _persist_rates(conn, rates=r))
                result.jisdor_rates += len(rates)
    return result


def _fetch_idx_day(fetcher: IDXMarketFetcher, day: date) -> tuple[dict[str, float], int]:
    index_payload = fetcher.fetch(INDEX_SUMMARY_PATH, day)
    if not rows_of(index_payload):
        return {}, 0  # a holiday or weekend: IDX returns no rows
    stock_payload = fetcher.fetch(STOCK_SUMMARY_PATH, day)
    series = {**parse_index_summary(index_payload), **parse_stock_summary(stock_payload)}
    return series, len(rows_of(index_payload)) + len(rows_of(stock_payload))


def _persist_day(
    conn: sqlite3.Connection,
    *,
    source_name: str,
    trade_date: date,
    series: dict[str, float],
    row_count: int,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        """
        INSERT INTO market_series_daily (series_id, trade_date, value, source_name, retrieved_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(series_id, trade_date) DO UPDATE SET
            value = excluded.value, source_name = excluded.source_name, retrieved_at = excluded.retrieved_at
        """,
        [(series_id, trade_date.isoformat(), value, source_name, now) for series_id, value in series.items()],
    )
    conn.execute(
        """
        INSERT INTO market_sync_days (source_name, trade_date, status, row_count, fetched_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source_name, trade_date) DO UPDATE SET
            status = excluded.status, row_count = excluded.row_count, fetched_at = excluded.fetched_at
        """,
        (source_name, trade_date.isoformat(), "ok" if series else "no_data", row_count, now),
    )


def _record_ledger(conn: sqlite3.Connection, *, source_name: str, trade_date: date, row_count: int) -> None:
    conn.execute(
        """
        INSERT INTO market_sync_days (source_name, trade_date, status, row_count, fetched_at)
        VALUES (?, ?, 'ok', ?, ?)
        ON CONFLICT(source_name, trade_date) DO UPDATE SET row_count = excluded.row_count, fetched_at = excluded.fetched_at
        """,
        (source_name, trade_date.isoformat(), row_count, datetime.now(timezone.utc).isoformat()),
    )


def _persist_rates(conn: sqlite3.Connection, *, rates: dict[date, float]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        """
        INSERT INTO market_series_daily (series_id, trade_date, value, source_name, retrieved_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(series_id, trade_date) DO UPDATE SET
            value = excluded.value, retrieved_at = excluded.retrieved_at
        """,
        [(JISDOR_SERIES, day.isoformat(), rate, JISDOR_SOURCE, now) for day, rate in sorted(rates.items())],
    )


def _synced_days(conn: sqlite3.Connection, source_name: str) -> set[date]:
    return {
        date.fromisoformat(row["trade_date"])
        for row in conn.execute("SELECT trade_date FROM market_sync_days WHERE source_name = ?", (source_name,))
    }


def _weekdays(start: date, end: date) -> Iterator[date]:
    day = start
    while day <= end:
        if day.weekday() < 5:
            yield day
        day += timedelta(days=1)


def _year_ranges(start: date, end: date) -> Iterator[tuple[date, date]]:
    for year in range(start.year, end.year + 1):
        yield max(start, date(year, 1, 1)), min(end, date(year, 12, 31))


def _full_year(year: int) -> tuple[date, date]:
    return max(JISDOR_FIRST_DAY, date(year, 1, 1)), date(year, 12, 31)


def _missing_jisdor_years(conn: sqlite3.Connection) -> list[int]:
    """Years from JISDOR's first fixing up to ``HISTORY_END_YEAR`` that hold no rate yet."""
    present = {
        int(row["year"])
        for row in conn.execute(
            "SELECT DISTINCT substr(trade_date, 1, 4) AS year FROM market_series_daily WHERE series_id = ?",
            (JISDOR_SERIES,),
        )
    }
    return [year for year in range(JISDOR_FIRST_DAY.year, HISTORY_END_YEAR + 1) if year not in present]


def _jisdor_client(cfg: Config) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": cfg.discovery.user_agent},
        timeout=cfg.discovery.request_timeout_s,
        follow_redirects=True,
        # Bank Indonesia's server now and then resets a new connection; retrying it succeeds.
        transport=httpx.HTTPTransport(retries=_JISDOR_CONNECT_RETRIES),
    )
