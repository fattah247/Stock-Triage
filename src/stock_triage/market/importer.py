"""``market-import``: daily index closes from a CSV the user downloaded.

``market-sync`` fetches IHSG closes from 2009 on IDX's own statistics reports
(``idx_history``); IDX has nothing earlier. Longer history is on Yahoo Finance,
Stooq and Investing.com, but all three forbid automated access, so the user
downloads the file with the site's own button and this only reads it. Nothing
here touches the network.

Closes go into ``market_series_daily`` under ``import:<file name>``. A day IDX
already supplied is never overwritten, and ``market-sync`` overwrites an
imported day when IDX later supplies it: IDX wins both ways. Imported days
carry only the close, so breadth, foreign-flow and sector signals read
``no_data`` on them.
"""

from __future__ import annotations

import csv
import io
import math
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from ..config import Config
from ..db import WriterQueue, run_migrations
from .idx_history import AGREEMENT_PCT
from .idx_history import SOURCE_NAME as HISTORY_SOURCE
from .idx_market import SOURCE_NAME as IDX_SOURCE

SOURCE_PREFIX = "import:"
DEFAULT_SERIES = "index:COMPOSITE"
# Days from either IDX source are checked, and kept, over the file's.
IDX_SOURCES = (IDX_SOURCE, HISTORY_SOURCE)


class MarketImportError(ValueError):
    pass


@dataclass
class ParsedCloses:
    closes: dict[date, float] = field(default_factory=dict)
    rows_read: int = 0
    unparseable: int = 0
    duplicate_dates: int = 0


@dataclass
class MarketImportResult:
    path: Path
    series_id: str
    source_name: str
    rows_read: int = 0
    inserted: int = 0
    skipped_present: int = 0
    skipped_unparseable: int = 0
    duplicate_dates: int = 0
    first_inserted: date | None = None
    last_inserted: date | None = None
    idx_overlap: int = 0  # days the file and IDX both hold, compared
    idx_disagreeing: int = 0


def parse_closes_csv(text: str, *, date_column: str = "Date", close_column: str = "Close") -> ParsedCloses:
    """Closes by day from a Yahoo-, Stooq- or similarly laid-out CSV.

    Dates must be ISO (``2008-01-02``, optionally with a time). Numbers may use
    Indonesian separators (``7.123,45``) or English ones (``7,123.45``); the
    file's own numbers decide which. Missing, ``null`` and non-positive closes
    count as unparseable.
    """
    text = text.lstrip("﻿")
    lines = text.splitlines()
    if not lines:
        raise MarketImportError("the file is empty")
    rows = list(csv.reader(io.StringIO(text), delimiter=max(",;\t", key=lines[0].count)))
    header = [name.strip().lower() for name in rows[0]]
    date_index = _column(header, rows[0], date_column)
    close_index = _column(header, rows[0], close_column)
    body = [row for row in rows[1:] if any(cell.strip() for cell in row)]
    decimal = _decimal_mark(row[close_index].strip() for row in body if len(row) > close_index)

    parsed = ParsedCloses(rows_read=len(body))
    for row in body:
        if len(row) <= max(date_index, close_index):
            parsed.unparseable += 1
            continue
        day, close = _parse_date(row[date_index]), _parse_number(row[close_index], decimal)
        if day is None or close is None:
            parsed.unparseable += 1
        elif day in parsed.closes:
            parsed.duplicate_dates += 1  # the first row for a day stands
        else:
            parsed.closes[day] = close
    return parsed


def run_market_import(
    cfg: Config,
    *,
    csv_path: Path,
    series_id: str = DEFAULT_SERIES,
    date_column: str = "Date",
    close_column: str = "Close",
) -> MarketImportResult:
    """Store every day in ``csv_path`` that ``market_series_daily`` does not hold yet."""
    cfg.ensure_dirs()
    if not csv_path.is_file():
        raise MarketImportError(f"no file at {csv_path}")
    try:
        text = csv_path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise MarketImportError(f"{csv_path.name} is not UTF-8 text; save it as CSV (UTF-8) and retry") from None
    parsed = parse_closes_csv(text, date_column=date_column, close_column=close_column)
    result = MarketImportResult(
        path=csv_path,
        series_id=series_id,
        source_name=f"{SOURCE_PREFIX}{csv_path.name}",
        rows_read=parsed.rows_read,
        skipped_unparseable=parsed.unparseable,
        duplicate_dates=parsed.duplicate_dates,
    )
    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
        # One transaction: the check against IDX and the inserts see the same rows.
        wq.submit(lambda conn: _store(conn, closes=parsed.closes, result=result))
    return result


def _store(conn: sqlite3.Connection, *, closes: dict[date, float], result: MarketImportResult) -> None:
    existing = {
        date.fromisoformat(row["trade_date"]): (float(row["value"]), row["source_name"])
        for row in conn.execute(
            "SELECT trade_date, value, source_name FROM market_series_daily WHERE series_id = ?", (result.series_id,)
        )
    }
    shared = [
        (close, existing[day][0]) for day, close in closes.items() if day in existing and existing[day][1] in IDX_SOURCES
    ]
    result.idx_overlap = len(shared)
    result.idx_disagreeing = sum(1 for mine, idx in shared if abs(mine - idx) > abs(idx) * AGREEMENT_PCT / 100.0)
    if result.idx_disagreeing * 2 > result.idx_overlap:
        raise MarketImportError(
            f"{result.idx_disagreeing} of the {result.idx_overlap} days IDX also has differ from IDX's "
            f"{result.series_id} close by more than {AGREEMENT_PCT:g}%; is this the right index, and the right "
            "close column? Nothing was written"
        )

    new = sorted((day, close) for day, close in closes.items() if day not in existing)
    now = datetime.now(timezone.utc).isoformat()
    before = conn.total_changes
    conn.executemany(
        """
        INSERT INTO market_series_daily (series_id, trade_date, value, source_name, retrieved_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(series_id, trade_date) DO NOTHING
        """,
        [(result.series_id, day.isoformat(), close, result.source_name, now) for day, close in new],
    )
    result.inserted = conn.total_changes - before
    result.skipped_present = len(closes) - result.inserted
    if new:
        result.first_inserted, result.last_inserted = new[0][0], new[-1][0]


def _column(header: list[str], raw: list[str], name: str) -> int:
    try:
        return header.index(name.strip().lower())
    except ValueError:
        columns = ", ".join(cell.strip() for cell in raw) or "none"
        raise MarketImportError(f"no {name!r} column (columns: {columns})") from None


def _decimal_mark(values: Iterable[str]) -> str:
    """``,`` for Indonesian numbers (``7.123,45``), ``.`` otherwise.

    The first value that settles it decides: both marks present (the last is the
    decimal), one mark repeated (it groups thousands), or one mark not followed by
    exactly three digits (it is the decimal). A file where every number reads like
    ``7.123`` stays ambiguous and is read with ``.`` as the decimal point, as Yahoo
    and Stooq write it.
    """
    for text in values:
        dot, comma = text.rfind("."), text.rfind(",")
        if dot >= 0 and comma >= 0:
            return "." if dot > comma else ","
        for mark, other in ((".", ","), (",", ".")):
            if text.count(mark) > 1:
                return other
            if text.count(mark) == 1 and len(text) - text.index(mark) - 1 != 3:
                return mark
    return "."


def _parse_number(text: str, decimal: str) -> float | None:
    thousands = "," if decimal == "." else "."
    cleaned = text.strip().replace(" ", "").replace(" ", "").replace(thousands, "").replace(decimal, ".")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if math.isfinite(value) and value > 0 else None


def _parse_date(text: str) -> date | None:
    head = text.strip().replace("T", " ").split(" ", 1)[0]
    try:
        return date.fromisoformat(head) if len(head) == 10 else None
    except ValueError:
        return None
