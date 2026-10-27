"""``market-sync``: daily IDX summaries and Bank Indonesia's JISDOR into ``market_series_daily``.

Each trading day's stock summary is also kept per stock (``stock_daily``), and
IDX's monthly financial-ratio and dividend tables for every company
(``idx_ratios``, ``idx_dividends``) are fetched through the same page.

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
    parse_stock_rows,
    parse_stock_summary,
    rows_of,
)
from .idx_market import SOURCE_NAME as IDX_SOURCE
from .idx_market import STOCK_FIELDS
from .jisdor import SERIES_ID as JISDOR_SERIES
from .jisdor import SOURCE_NAME as JISDOR_SOURCE
from .exchange_notices import SOURCE_NAME as NOTICES_SOURCE
from .exchange_notices import fetch_exchange_notices, persist_notices, write_notice_series
from .idx_history import SOURCE_NAME as HISTORY_SOURCE
from .idx_history import fetch_history, store_history
from .idx_dividends import FIRST_MONTH as DIVIDENDS_FIRST_MONTH
from .idx_dividends import SOURCE_NAME as DIVIDENDS_SOURCE
from .idx_dividends import fetch_dividends, store_dividends
from .idx_ratios import FIRST_MONTH as RATIOS_FIRST_MONTH
from .idx_ratios import SOURCE_NAME as RATIOS_SOURCE
from .idx_ratios import fetch_ratios, months_due, store_ratios
from .idx_profiles import SOURCE_NAME as PROFILES_SOURCE
from .idx_profiles import fetch_profile_dividends, store_profile_dividends, tickers_due
from .jisdor import FIRST_DAY as JISDOR_FIRST_DAY
from .jisdor import fetch_jisdor_export, parse_jisdor_export

# Without a backfill, a sync looks back this far for missing days, or to the
# day after the last one synced when that is earlier.
DEFAULT_LOOKBACK_DAYS = 10
# IDX's summaries for a day are final after the close; earlier in the day the
# current day is left for a later sync.
JAKARTA = timezone(timedelta(hours=7))
DAY_FINAL_HOUR = 18
# History fills the years before IDX's summaries start (2020-01-02); from then
# on, the sync's own range covers both sources.
HISTORY_END_YEAR = 2019
# The per-stock rows of a day's stock summary, in their own ledger so a day
# synced before they were kept is fetched again for them alone.
STOCKS_SOURCE = "idx_stocks"
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
    stock_days: int = 0
    ratio_months: int = 0
    dividend_months: int = 0
    profiles: int = 0
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
    monthly: bool = True,
    idx_fetcher_factory: Callable[[], AbstractContextManager[IDXMarketFetcher]] | None = None,
    jisdor_client: httpx.Client | None = None,
) -> MarketSyncResult:
    """Fetch every weekday in ``start``..``end`` not yet synced (all of them with ``refresh``).

    With ``history``, also fetch the pre-2020 history still missing: IDX's
    statistics reports once (again with ``refresh``), and each JISDOR year
    from 2013 that holds no rate yet. With ``monthly``, fetch IDX's monthly
    ratio and dividend tables for each month not stored yet, and the latest
    ones once a day.
    """
    cfg.ensure_dirs()
    end = end or datetime.now(timezone.utc).date()
    explicit_start = start is not None

    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
        reader = connect(cfg.paths.db_path, read_only=True)
        try:
            configure_pragmas(reader, cfg)
            ledger = _ledger(reader, IDX_SOURCE)
            stocks_ledger = _synced_days(reader, STOCKS_SOURCE)
            if not explicit_start:
                start = end - timedelta(days=DEFAULT_LOOKBACK_DAYS)
                synced = sorted(day for day, status in ledger.items() if day <= end)
                if synced and synced[-1] + timedelta(days=1) < start:
                    start = synced[-1] + timedelta(days=1)  # resume after a break
                ok_days = [day for day, status in ledger.items() if status == "ok"]
                if ok_days and not stocks_ledger:
                    start = min(start, min(ok_days))  # keep each stock's rows for every day already synced
            index_days = {} if refresh else ledger
            stock_days = set() if refresh else stocks_ledger
            # Notices are fetched once a day, even when every trading day is already synced.
            notices_due = refresh or end not in _synced_days(reader, NOTICES_SOURCE)
            history_due = history and (refresh or not _synced_days(reader, HISTORY_SOURCE))
            jisdor_history = _missing_jisdor_years(reader) if history else []
            # IDX's monthly tables: (ledger source, months due, fetch, store, result counter).
            tables = [
                (source, months_due({} if refresh else _fetched_on(reader, source), today=end, first=first), fetch, store, counter)
                for source, first, fetch, store, counter in (
                    (RATIOS_SOURCE, RATIOS_FIRST_MONTH, fetch_ratios, store_ratios, "ratio_months"),
                    (DIVIDENDS_SOURCE, DIVIDENDS_FIRST_MONTH, fetch_dividends, store_dividends, "dividend_months"),
                )
            ] if monthly else []
            profiles_due = tickers_due(reader, today=end) if monthly else []
        finally:
            reader.close()

        result = MarketSyncResult(start=start, end=end)
        jakarta_now = datetime.now(JAKARTA)
        weekdays = [
            day
            for day in _weekdays(start, end)
            if not (day == jakarta_now.date() and jakarta_now.hour < DAY_FINAL_HOUR)
        ]
        # A trading day synced before per-stock rows were kept needs its stock summary alone.
        pending = [
            day
            for day in weekdays
            if day not in index_days or (index_days[day] == "ok" and day not in stock_days)
        ]
        result.idx_days_skipped = len(weekdays) - len(pending)
        if pending or notices_due or history_due or profiles_due or any(months for _, months, *_ in tables):
            factory = idx_fetcher_factory or (lambda: open_idx_fetcher(cfg))
            with factory() as fetcher:
                failures = 0
                for position, day in enumerate(pending):
                    try:
                        if day in index_days:
                            series, row_count = None, 0
                            stocks = parse_stock_rows(fetcher.fetch(STOCK_SUMMARY_PATH, day))
                            if not stocks:
                                raise RuntimeError("IDX returned an empty stock summary for a trading day")
                        else:
                            series, row_count, stocks = _fetch_idx_day(fetcher, day)
                    except Exception as exc:  # noqa: BLE001 - one bad day must not lose the others
                        result.errors.append(f"{IDX_SOURCE} {day.isoformat()}: {exc}")
                        failures += 1
                        if failures >= MAX_CONSECUTIVE_FAILURES:
                            result.idx_days_left = len(pending) - position - 1
                            break
                        continue
                    failures = 0
                    if series is not None:
                        wq.submit(
                            lambda conn, d=day, s=series, n=row_count: _persist_day(
                                conn, source_name=IDX_SOURCE, trade_date=d, series=s, row_count=n
                            )
                        )
                        if series:
                            result.idx_days_fetched += 1
                        else:
                            result.idx_days_no_data += 1
                    if series is None or series:
                        wq.submit(lambda conn, d=day, r=stocks: _persist_stocks(conn, trade_date=d, rows=r))
                        result.stock_days += bool(stocks)
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
                for source, months, fetch, store, counter in tables:
                    if result.idx_days_left:
                        break
                    failures = 0
                    for month in months:
                        try:
                            rows = fetch(fetcher, month)
                        except Exception as exc:  # noqa: BLE001
                            result.errors.append(f"{source} {month:%Y-%m}: {exc}")
                            failures += 1
                            if failures >= MAX_CONSECUTIVE_FAILURES:
                                break
                            continue
                        failures = 0
                        wq.submit(lambda conn, m=month, r=rows, f=store: f(conn, month=m, rows=r))
                        setattr(result, counter, getattr(result, counter) + bool(rows))
                # Each company's latest dividend, from its profile (fills the monthly table's gaps).
                failures = 0
                for ticker in profiles_due if not result.idx_days_left else []:
                    try:
                        rows = fetch_profile_dividends(fetcher, ticker)
                    except Exception as exc:  # noqa: BLE001
                        result.errors.append(f"{PROFILES_SOURCE} {ticker}: {exc}")
                        failures += 1
                        if failures >= MAX_CONSECUTIVE_FAILURES:
                            break
                        continue
                    failures = 0
                    wq.submit(lambda conn, t=ticker, r=rows: store_profile_dividends(conn, ticker=t, rows=r))
                    result.profiles += 1
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


def _fetch_idx_day(fetcher: IDXMarketFetcher, day: date) -> tuple[dict[str, float], int, list[dict]]:
    index_payload = fetcher.fetch(INDEX_SUMMARY_PATH, day)
    if not rows_of(index_payload):
        return {}, 0, []  # a holiday or weekend: IDX returns no rows
    stock_payload = fetcher.fetch(STOCK_SUMMARY_PATH, day)
    stocks = parse_stock_rows(stock_payload)
    if not stocks:
        # The index traded, so an empty stock summary is a fault to retry, not a holiday.
        raise RuntimeError("IDX returned an empty stock summary for a trading day")
    series = {**parse_index_summary(index_payload), **parse_stock_summary(stock_payload)}
    return series, len(rows_of(index_payload)) + len(rows_of(stock_payload)), stocks


def _persist_stocks(conn: sqlite3.Connection, *, trade_date: date, rows: list[dict]) -> None:
    columns = ("ticker", "trade_date") + tuple(column for column, _ in STOCK_FIELDS)
    updates = ", ".join(f"{column} = excluded.{column}" for column in columns[2:])
    conn.executemany(
        f"""
        INSERT INTO stock_daily ({", ".join(columns)}) VALUES ({", ".join("?" for _ in columns)})
        ON CONFLICT(ticker, trade_date) DO UPDATE SET {updates}
        """,
        [(row["ticker"], trade_date.isoformat()) + tuple(row[column] for column, _ in STOCK_FIELDS) for row in rows],
    )
    conn.execute(
        """
        INSERT INTO market_sync_days (source_name, trade_date, status, row_count, fetched_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source_name, trade_date) DO UPDATE SET
            status = excluded.status, row_count = excluded.row_count, fetched_at = excluded.fetched_at
        """,
        (STOCKS_SOURCE, trade_date.isoformat(), "ok" if rows else "no_data", len(rows), datetime.now(timezone.utc).isoformat()),
    )


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


def _ledger(conn: sqlite3.Connection, source_name: str) -> dict[date, str]:
    """Each day's status (``ok`` or ``no_data``) for ``source_name``."""
    return {
        date.fromisoformat(row["trade_date"]): row["status"]
        for row in conn.execute("SELECT trade_date, status FROM market_sync_days WHERE source_name = ?", (source_name,))
    }


def _fetched_on(conn: sqlite3.Connection, source_name: str) -> dict[date, date]:
    """The day each ledger entry for ``source_name`` was last fetched."""
    return {
        date.fromisoformat(row["trade_date"]): date.fromisoformat(row["fetched_at"][:10])
        for row in conn.execute("SELECT trade_date, fetched_at FROM market_sync_days WHERE source_name = ?", (source_name,))
    }


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
