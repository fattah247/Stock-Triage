"""Exchange-issued notices: suspensions and unusual market activity (plan phase 2b).

IDX publishes them at two endpoints, each a rolling three years in one call:

- ``GetSuspension``: ``Info_Type`` SPT (suspend) and UPT (lift), one row per notice;
- ``GetUMA``: unusual market activity announcements, one per stock and day.

Both are fetched inside the IDX page ``market-sync`` already opens. Daily counts
become the ``exchange_notices`` signal, read against its own past year.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any

SUSPENSION_PATH = "/primary/NewsAnnouncement/GetSuspension"
UMA_PATH = "/primary/NewsAnnouncement/GetUMA"
SOURCE_NAME = "idx_notices"
SUSPEND_SERIES = "notices:suspend"
UMA_SERIES = "notices:uma"
# One call returns the whole rolling window; IDX holds about 2,000 of each.
PAGE_SIZE = 5000
_KINDS = {"SPT": "suspend", "UPT": "unsuspend"}


def notices_url(path: str) -> str:
    return f"https://www.idx.co.id{path}?indexFrom=0&pageSize={PAGE_SIZE}&lang=id"


def parse_suspensions(payload: dict[str, Any]) -> list[dict[str, object]]:
    notices = []
    for row in payload.get("Results") or []:
        kind = _KINDS.get(str(row.get("Info_Type") or ""))
        stamp = _parse_stamp(row.get("Date"))
        if kind is None or stamp is None:
            continue
        key = f"{kind}|{row.get('Kode')}|{row.get('Date')}|{row.get('Data_Download')}"
        notices.append(
            {
                "notice_id": _id(key),
                "kind": kind,
                "notice_date": stamp.date().isoformat(),
                "published_at": stamp.isoformat(),
                "ticker": (row.get("Kode") or None),
                "title": str(row.get("Judul") or "").strip(),
                "announcement_no": None,
                "url": _absolute(row.get("Data_Download")),
                "raw": row,
            }
        )
    return notices


def parse_uma(payload: dict[str, Any]) -> list[dict[str, object]]:
    notices = []
    for row in payload.get("Results") or []:
        stamp = _parse_stamp(row.get("UMADate"))
        if stamp is None:
            continue
        notices.append(
            {
                "notice_id": _id(f"uma|{row.get('UMAID')}|{row.get('CompanyID')}"),
                "kind": "uma",
                "notice_date": stamp.date().isoformat(),
                "published_at": stamp.isoformat(),
                "ticker": row.get("CompanyID") or None,
                "title": str(row.get("Judul") or "").strip(),
                "announcement_no": row.get("AnnouncementNo"),
                "url": _absolute(row.get("Attachment")),
                "raw": row,
            }
        )
    return notices


def fetch_exchange_notices(fetcher) -> list[dict[str, object]]:
    """Both lists, through an IDX page (``BrowserIDXFetcher.fetch_url``)."""
    return parse_suspensions(fetcher.fetch_url(notices_url(SUSPENSION_PATH))) + parse_uma(fetcher.fetch_url(notices_url(UMA_PATH)))


def persist_notices(conn: sqlite3.Connection, *, notices: list[dict[str, object]]) -> int:
    now = datetime.now(timezone.utc).isoformat()
    before = conn.execute("SELECT COUNT(*) FROM exchange_notices").fetchone()[0]
    conn.executemany(
        """
        INSERT OR IGNORE INTO exchange_notices (
            notice_id, kind, notice_date, published_at, ticker, title, announcement_no, url, raw_json, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                notice["notice_id"],
                notice["kind"],
                notice["notice_date"],
                notice["published_at"],
                notice["ticker"],
                notice["title"],
                notice["announcement_no"],
                notice["url"],
                json.dumps(notice["raw"], sort_keys=True, default=str),
                now,
            )
            for notice in notices
        ],
    )
    return conn.execute("SELECT COUNT(*) FROM exchange_notices").fetchone()[0] - before


def write_notice_series(conn: sqlite3.Connection) -> int:
    """Daily suspension and UMA counts on every trading day the notices cover, zeros included."""
    first = conn.execute("SELECT MIN(notice_date) FROM exchange_notices").fetchone()[0]
    if first is None:
        return 0
    trade_dates = [
        row[0]
        for row in conn.execute(
            "SELECT trade_date FROM market_series_daily WHERE series_id = 'index:COMPOSITE' AND trade_date >= ? ORDER BY trade_date",
            (first,),
        )
    ]
    counts: Counter[tuple[str, str]] = Counter(
        (row["kind"], row["notice_date"]) for row in conn.execute("SELECT kind, notice_date FROM exchange_notices")
    )
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for day in trade_dates:
        rows.append((SUSPEND_SERIES, day, float(counts[("suspend", day)]), SOURCE_NAME, now))
        rows.append((UMA_SERIES, day, float(counts[("uma", day)]), SOURCE_NAME, now))
    conn.executemany(
        """
        INSERT INTO market_series_daily (series_id, trade_date, value, source_name, retrieved_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(series_id, trade_date) DO UPDATE SET value = excluded.value, retrieved_at = excluded.retrieved_at
        """,
        rows,
    )
    return len(trade_dates)


def load_recent_notices(conn: sqlite3.Connection, *, days: int = 7, today: date | None = None) -> list[dict[str, object]]:
    since = ((today or date.today()) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT kind, notice_date, ticker, title, url FROM exchange_notices WHERE notice_date >= ? ORDER BY notice_date DESC, kind",
        (since,),
    ).fetchall()
    return [dict(row) for row in rows]


def _parse_stamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _absolute(path: object) -> str | None:
    if not path:
        return None
    text = str(path)
    return text if text.startswith("http") else f"https://www.idx.co.id{text}"


def _id(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
