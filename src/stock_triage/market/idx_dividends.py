"""IDX's monthly "Dividend Announcement" table: cash dividends for every listed company.

Each row is one dividend: the cash per share, its currency (a few miners pay
in US dollars), and the cum, ex, record and payment dates. The table goes back
to 2019. ``market-sync`` fetches it like the ratio table (``idx_ratios``): each
month not stored yet, and the latest two again once a day. A dividend listed in
more than one month is stored once.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from typing import Any

from .idx_market import IDXMarketFetcher
from .idx_ratios import RATIOS_PATH, record_month

SOURCE_NAME = "idx_dividends"
FIRST_MONTH = date(2019, 1, 1)


def dividends_url(month: date) -> str:
    return (
        f"https://www.idx.co.id{RATIOS_PATH}?urlName=LINK_DIVIDEND&periodYear={month.year}"
        f"&periodMonth={month.month}&periodType=monthly&isPrint=False&cumulative=false&pageSize=2000&pageNumber=1&orderBy="
    )


def parse_dividends(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for row in payload.get("data") or []:
        ticker = str(row.get("code") or "").strip().upper()
        cash = row.get("cashDividend")
        ex_date = _day(row.get("exDividend")) or _day(row.get("recordDate"))
        if not ticker or not isinstance(cash, (int, float)) or cash <= 0 or ex_date is None:
            continue
        rows.append(
            {
                "ticker": ticker,
                "ex_date": ex_date,
                "cash_per_share": float(cash),
                "currency": str(row.get("currency") or "IDR").strip().upper(),
                "cum_date": _day(row.get("cumDividend")),
                "record_date": _day(row.get("recordDate")),
                "payment_date": _day(row.get("paymentDate")),
                "note": (str(row.get("note")).strip() or None) if row.get("note") else None,
            }
        )
    return rows


def fetch_dividends(fetcher: IDXMarketFetcher, month: date) -> list[dict[str, Any]]:
    return parse_dividends(fetcher.fetch_url(dividends_url(month)))


def store_dividends(conn: sqlite3.Connection, *, month: date, rows: list[dict[str, Any]]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        """
        INSERT INTO dividends (
            ticker, ex_date, cash_per_share, currency, cum_date, record_date, payment_date, note, report_month, retrieved_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, ex_date, cash_per_share) DO UPDATE SET
            currency = excluded.currency, cum_date = excluded.cum_date, record_date = excluded.record_date,
            payment_date = excluded.payment_date, note = excluded.note, report_month = excluded.report_month,
            retrieved_at = excluded.retrieved_at, source = 'idx_dividends'
        """,
        [
            (
                row["ticker"], row["ex_date"], row["cash_per_share"], row["currency"], row["cum_date"],
                row["record_date"], row["payment_date"], row["note"], month.strftime("%Y-%m"), now,
            )
            for row in rows
        ],
    )
    record_month(conn, source_name=SOURCE_NAME, month=month, row_count=len(rows), fetched_at=now)


def _day(value: Any) -> str | None:
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text).isoformat() if text else None
    except ValueError:
        return None
