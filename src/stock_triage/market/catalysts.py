"""Risk calendar: dated market events, plus filing deadlines for the watchlist.

Market events come from ``market.catalysts_path`` (default
``<data_dir>/catalysts.yaml``; see ``config/catalysts.example.yaml``), which is
the source of truth: each sync replaces the stored rows. Filing deadlines are
read live from ``filing_expectations`` so they never go stale.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from ..config import Config
from ..db import WriterQueue, run_migrations

CATALYST_KINDS = frozenset(
    {
        "fomc",
        "bi_rate",
        "msci_review",
        "msci_classification",
        "ftse_classification",
        "sovereign_rating",
        "us_cpi",
        "bps_release",
        "election",
        "lockup_expiry",
        "enso_outlook",
        "policy",
        "policy_speech",
        "custom",
    }
)
CATALYST_SEVERITIES = ("low", "medium", "high")
FILING_DEADLINE_KIND = "filing_deadline"


class CatalystFileError(ValueError):
    """The catalyst file cannot be read as a list of dated events."""


@dataclass(frozen=True)
class Catalyst:
    catalyst_id: str
    catalyst_date: date
    kind: str
    title: str
    severity: str
    region: str | None = None
    source_url: str | None = None
    notes: str | None = None
    ticker: str | None = None  # set for filing deadlines


@dataclass(frozen=True)
class CatalystSyncResult:
    path: Path
    file_found: bool
    synced: int


def load_catalyst_file(path: Path) -> list[Catalyst]:
    if not path.exists():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise CatalystFileError(f"{path}: not valid YAML: {exc}") from exc
    entries = raw.get("catalysts", []) if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise CatalystFileError(f"{path}: expected a top-level 'catalysts' list")
    catalysts: list[Catalyst] = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise CatalystFileError(f"{path}: entry {index} is not a mapping")
        catalyst_date = _as_date(entry.get("date"))
        if catalyst_date is None:
            raise CatalystFileError(f"{path}: entry {index} has no valid 'date' (YYYY-MM-DD)")
        kind = str(entry.get("kind") or "").strip()
        if kind not in CATALYST_KINDS:
            raise CatalystFileError(
                f"{path}: entry {index} has unknown kind {kind!r}; expected one of {', '.join(sorted(CATALYST_KINDS))}"
            )
        title = str(entry.get("title") or "").strip()
        if not title:
            raise CatalystFileError(f"{path}: entry {index} has no 'title'")
        severity = str(entry.get("severity") or "medium").strip().lower()
        if severity not in CATALYST_SEVERITIES:
            raise CatalystFileError(
                f"{path}: entry {index} has severity {severity!r}; expected one of {', '.join(CATALYST_SEVERITIES)}"
            )
        catalysts.append(
            Catalyst(
                catalyst_id=hashlib.sha256(f"{catalyst_date.isoformat()}:{kind}:{title}".encode("utf-8")).hexdigest()[:24],
                catalyst_date=catalyst_date,
                kind=kind,
                title=title,
                severity=severity,
                region=_optional_text(entry.get("region")),
                source_url=_optional_text(entry.get("source_url")),
                notes=_optional_text(entry.get("notes")),
            )
        )
    return catalysts


def sync_catalysts(cfg: Config) -> CatalystSyncResult:
    path = cfg.market.catalysts_path
    catalysts = load_catalyst_file(path)
    synced_at = datetime.now(timezone.utc).isoformat()

    def _replace(conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM market_catalysts")
        conn.executemany(
            """
            INSERT INTO market_catalysts (
                catalyst_id, catalyst_date, kind, title, severity, region, source_url, notes, synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    catalyst.catalyst_id,
                    catalyst.catalyst_date.isoformat(),
                    catalyst.kind,
                    catalyst.title,
                    catalyst.severity,
                    catalyst.region,
                    catalyst.source_url,
                    catalyst.notes,
                    synced_at,
                )
                for catalyst in catalysts
            ],
        )

    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
        wq.submit(_replace)
    return CatalystSyncResult(path=path, file_found=path.exists(), synced=len(catalysts))


def load_upcoming_catalysts(
    conn: sqlite3.Connection,
    *,
    today: date,
    days: int,
    watchlist: set[str],
) -> list[Catalyst]:
    """Risk dates from ``today`` through ``today + days``, soonest first."""
    until = today + timedelta(days=days)
    rows = conn.execute(
        """
        SELECT catalyst_id, catalyst_date, kind, title, severity, region, source_url, notes
        FROM market_catalysts
        WHERE catalyst_date BETWEEN ? AND ?
        """,
        (today.isoformat(), until.isoformat()),
    ).fetchall()
    upcoming = [
        Catalyst(
            catalyst_id=row["catalyst_id"],
            catalyst_date=date.fromisoformat(row["catalyst_date"]),
            kind=row["kind"],
            title=row["title"],
            severity=row["severity"],
            region=row["region"],
            source_url=row["source_url"],
            notes=row["notes"],
        )
        for row in rows
    ]
    if watchlist:
        placeholders = ",".join("?" for _ in watchlist)
        deadlines = conn.execute(
            f"""
            SELECT DISTINCT ticker, period_label, period_end_date, date(deadline_at) AS deadline
            FROM filing_expectations
            WHERE ticker IN ({placeholders})
              AND satisfied_at IS NULL
              AND state != 'satisfied'
              AND date(deadline_at) BETWEEN ? AND ?
            """,
            (*sorted(watchlist), today.isoformat(), until.isoformat()),
        ).fetchall()
        upcoming.extend(
            Catalyst(
                catalyst_id=f"filing:{row['ticker']}:{row['period_label']}:{row['period_end_date']}",
                catalyst_date=date.fromisoformat(row["deadline"]),
                kind=FILING_DEADLINE_KIND,
                title=f"{row['ticker']} {row['period_label']} due (period ending {row['period_end_date']})",
                severity="medium",
                ticker=row["ticker"],
            )
            for row in deadlines
        )
    return sorted(upcoming, key=lambda item: (item.catalyst_date, item.kind, item.title))


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        return None


def _optional_text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None
