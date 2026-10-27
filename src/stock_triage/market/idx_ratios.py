"""IDX's monthly "Financial Data and Ratio" table, for every listed company.

IDX's digital statistics publish, each month, one row per stock: the latest
statement's assets, liabilities, equity, sales, profit, EPS and book value, and
IDX's trailing P/E, price to book, debt to equity, ROA, ROE and net margin at
that month's price, with the IDX-IC sector and industry. It goes back to 2021.
``market-sync`` fetches it through the IDX page it already opens: every month
not stored yet, and the latest two again, since IDX fills them in as
statements arrive.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date, datetime, timezone
from typing import Any

from .idx_market import IDXMarketFetcher

SOURCE_NAME = "idx_ratios"
RATIOS_PATH = "/primary/DigitalStatistic/GetApiDataPaginated"
# Earlier months come back empty.
FIRST_MONTH = date(2021, 1, 1)
REFETCH_LATEST_MONTHS = 2
_BILLION = 1e9

# company_ratios column, IDX field, and the factor to rupiah for amounts.
_TEXT_FIELDS = (
    ("fs_date", "fsDate"),
    ("fiscal_year_end", "fiscalYearEnd"),
    ("fs_type", "audit"),
    ("auditor_opinion", "opini"),
    ("sector", "sector"),
    ("sub_sector", "subSector"),
    ("industry", "industry"),
    ("sub_industry", "subIndustry"),
    ("sub_industry_code", "subIndustryCode"),
)
_NUMBER_FIELDS = (
    ("assets", "assets", _BILLION),
    ("liabilities", "liabilities", _BILLION),
    ("equity", "equity", _BILLION),
    ("sales", "sales", _BILLION),
    ("ebt", "ebt", _BILLION),
    ("profit_period", "profitPeriod", _BILLION),
    ("profit_attr_owner", "profitAttrOwner", _BILLION),
    ("eps", "eps", 1.0),
    ("book_value", "bookValue", 1.0),
    ("per", "per", 1.0),
    ("pbv", "priceBV", 1.0),
    ("der", "deRatio", 1.0),
    ("roa", "roa", 1.0),
    ("roe", "roe", 1.0),
    ("npm", "npm", 1.0),
)
COLUMNS = ("ticker",) + tuple(c for c, _ in _TEXT_FIELDS) + ("sharia",) + tuple(c for c, _, _ in _NUMBER_FIELDS)


def ratios_url(month: date) -> str:
    return (
        f"https://www.idx.co.id{RATIOS_PATH}?urlName=LINK_FINANCIAL_DATA_RATIO&periodYear={month.year}"
        f"&periodMonth={month.month}&periodType=monthly&isPrint=False&cumulative=false&pageSize=2000&pageNumber=1&orderBy="
    )


def parse_ratios(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for row in payload.get("data") or []:
        ticker = str(row.get("code") or "").strip().upper()
        if not ticker:
            continue
        parsed: dict[str, Any] = {"ticker": ticker}
        for column, field in _TEXT_FIELDS:
            text = str(row.get(field) or "").strip()
            parsed[column] = text if text and text != "-" else None
        parsed["sharia"] = 1 if str(row.get("sharia") or "").strip().upper() == "S" else 0
        for column, field, factor in _NUMBER_FIELDS:
            value = row.get(field)
            parsed[column] = float(value) * factor if isinstance(value, (int, float)) else None
        rows.append(parsed)
    return rows


def months_due(fetched_on: dict[date, date], *, today: date, first: date = FIRST_MONTH) -> list[date]:
    """Months from ``first`` never fetched, plus the latest ones not fetched today, oldest first."""
    months = list(_months(first, today.replace(day=1)))
    latest = set(months[-REFETCH_LATEST_MONTHS:])
    return [month for month in months if month not in fetched_on or (month in latest and fetched_on[month] < today)]


def fetch_ratios(fetcher: IDXMarketFetcher, month: date) -> list[dict[str, Any]]:
    return parse_ratios(fetcher.fetch_url(ratios_url(month)))


def record_month(conn: sqlite3.Connection, *, source_name: str, month: date, row_count: int, fetched_at: str) -> None:
    conn.execute(
        """
        INSERT INTO market_sync_days (source_name, trade_date, status, row_count, fetched_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source_name, trade_date) DO UPDATE SET
            status = excluded.status, row_count = excluded.row_count, fetched_at = excluded.fetched_at
        """,
        (source_name, month.isoformat(), "ok" if row_count else "no_data", row_count, fetched_at),
    )


def store_ratios(conn: sqlite3.Connection, *, month: date, rows: list[dict[str, Any]]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    key = month.strftime("%Y-%m")
    columns = COLUMNS + ("report_month", "retrieved_at")
    updates = ", ".join(f"{column} = excluded.{column}" for column in columns if column not in ("ticker", "report_month"))
    conn.executemany(
        f"""
        INSERT INTO company_ratios ({", ".join(columns)}) VALUES ({", ".join("?" for _ in columns)})
        ON CONFLICT(ticker, report_month) DO UPDATE SET {updates}
        """,
        [tuple(row[column] for column in COLUMNS) + (key, now) for row in rows],
    )
    record_month(conn, source_name=SOURCE_NAME, month=month, row_count=len(rows), fetched_at=now)


def _months(start: date, end: date) -> Iterator[date]:
    month = start
    while month <= end:
        yield month
        month = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
