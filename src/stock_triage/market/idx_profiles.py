"""Each company's latest dividend from its IDX profile, to fill gaps in the monthly dividend table.

IDX's monthly "Dividend Announcement" table (``idx_dividends``) misses some
recent dividends. ``GetCompanyProfilesDetail`` lists, for each company, its
latest one: book year, cum, ex, record and payment dates, and the cash per
share or, often, only the total paid. A total becomes a per-share amount with
the shares listed on the cum date, from ``stock_daily``.

``market-sync`` fetches every listed company's profile the first time, then a
batch a day, oldest first, so each is refreshed about once a week.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from .idx_dividends import _day
from .idx_market import IDXMarketFetcher

SOURCE_NAME = "idx_profiles"
PROFILE_PATH = "/primary/ListedCompany/GetCompanyProfilesDetail"
BATCH = 150
REFRESH_DAYS = 7


def profile_url(ticker: str) -> str:
    return f"https://www.idx.co.id{PROFILE_PATH}?KodeEmiten={quote(ticker)}&language=id-id"


def parse_profile_dividends(payload: dict[str, Any], ticker: str) -> list[dict[str, Any]]:
    """The profile's cash dividends, each with a per-share amount or the total paid."""
    rows = []
    for item in payload.get("Dividen") or []:
        ex_date = _day(item.get("TanggalExRegulerDanNegosiasi")) or _day(item.get("TanggalDPS")) or _day(item.get("TanggalCum"))
        per_share, total = item.get("CashDividenPerSaham"), item.get("CashDividenTotal")
        if ex_date is None:
            continue
        if isinstance(per_share, (int, float)) and per_share > 0:
            amount = {"cash_per_share": float(per_share), "total": None, "currency": item.get("CashDividenPerSahamMU") or "IDR"}
        elif isinstance(total, (int, float)) and total > 0:
            amount = {"cash_per_share": None, "total": float(total), "currency": item.get("CashDividenTotalMU") or "IDR"}
        else:
            continue
        kind = "I" if str(item.get("Jenis") or "").lower().endswith("i") else "F"
        rows.append(
            {
                "ticker": ticker.upper(),
                "ex_date": ex_date,
                **amount,
                "currency": str(amount["currency"]).strip().upper() or "IDR",
                "cum_date": _day(item.get("TanggalCum")),
                "record_date": _day(item.get("TanggalDPS")),
                "payment_date": _day(item.get("TanggalPembayaran")),
                "note": f"{kind}{item.get('TahunBuku') or ''}",
            }
        )
    return rows


def tickers_due(conn: sqlite3.Connection, *, today: date) -> list[str]:
    """Listed companies whose profile was never fetched or is a week old: all of them the first time."""
    listed = [
        row[0]
        for row in conn.execute(
            "SELECT ticker FROM stock_daily WHERE trade_date = (SELECT MAX(trade_date) FROM stock_daily) ORDER BY ticker"
        )
    ]
    fetched = {row["ticker"]: row["fetched_at"][:10] for row in conn.execute("SELECT ticker, fetched_at FROM company_profile_fetches")}
    if not fetched:
        return listed
    stale = (today - timedelta(days=REFRESH_DAYS)).isoformat()
    due = [ticker for ticker in listed if fetched.get(ticker, "") <= stale]
    return sorted(due, key=lambda ticker: fetched.get(ticker, ""))[:BATCH]


def fetch_profile_dividends(fetcher: IDXMarketFetcher, ticker: str) -> list[dict[str, Any]]:
    return parse_profile_dividends(fetcher.fetch_url(profile_url(ticker)), ticker)


def store_profile_dividends(conn: sqlite3.Connection, *, ticker: str, rows: list[dict[str, Any]]) -> int:
    """Store the profile's dividends, a total turned per share with the shares listed on the cum date."""
    now = datetime.now(timezone.utc).isoformat()
    stored = []
    for row in rows:
        per_share = row["cash_per_share"]
        if per_share is None:
            shares = conn.execute(
                "SELECT listed_shares FROM stock_daily WHERE ticker = ? AND trade_date <= ? AND listed_shares > 0 "
                "ORDER BY trade_date DESC LIMIT 1",
                (row["ticker"], row["cum_date"] or row["ex_date"]),
            ).fetchone()
            if shares is None:
                continue
            per_share = round(row["total"] / shares[0], 4)
        stored.append(
            (
                row["ticker"], row["ex_date"], per_share, row["currency"], row["cum_date"], row["record_date"],
                row["payment_date"], row["note"], now[:7], now, SOURCE_NAME,
            )
        )
    conn.executemany(
        """
        INSERT INTO dividends (
            ticker, ex_date, cash_per_share, currency, cum_date, record_date, payment_date, note, report_month,
            retrieved_at, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, ex_date, cash_per_share) DO UPDATE SET retrieved_at = excluded.retrieved_at
        """,
        stored,
    )
    conn.execute(
        """
        INSERT INTO company_profile_fetches (ticker, fetched_at, dividends) VALUES (?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET fetched_at = excluded.fetched_at, dividends = excluded.dividends
        """,
        (ticker.upper(), now, len(stored)),
    )
    return len(stored)
