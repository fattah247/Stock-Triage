"""Persistence helpers for Phase 2 discovery outputs."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..sources import DiscoveryWindow, DocumentCandidate, SourceClass


@dataclass(frozen=True)
class DiscoveryPersistResult:
    inserted: int
    updated: int
    skipped: int


def persist_companies(conn: sqlite3.Connection, candidates: list[DocumentCandidate]) -> DiscoveryPersistResult:
    inserted = 0
    updated = 0
    now = datetime.now(timezone.utc).isoformat()

    for candidate in candidates:
        if not candidate.ticker:
            continue
        issuer_name = candidate.raw_metadata.get("issuer_name") or candidate.title or candidate.ticker
        listing_date = candidate.raw_metadata.get("listing_date")
        source_urls = json.dumps([candidate.raw_metadata.get("profile_url")] if candidate.raw_metadata.get("profile_url") else [])
        raw_metadata = json.dumps(candidate.raw_metadata, sort_keys=True)
        # IDX company profiles include sector/subsector/industry in raw_metadata.
        sector = candidate.raw_metadata.get("sector") or candidate.raw_metadata.get("sub_sector") or "unknown"
        subsector = candidate.raw_metadata.get("sub_sector") or "unknown"
        industry = candidate.raw_metadata.get("industry") or "unknown"
        listing_board = candidate.raw_metadata.get("listing_board") or ""
        fiscal_year_end_month = int(candidate.raw_metadata.get("fiscal_year_end_month") or 12)

        existing = conn.execute(
            "SELECT ticker FROM companies WHERE ticker = ?",
            (candidate.ticker,),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO companies (
                    ticker, issuer_name, listing_date, source_urls, raw_metadata,
                    sector, subsector, industry, listing_board, fiscal_year_end_month,
                    first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.ticker,
                    issuer_name,
                    listing_date,
                    source_urls,
                    raw_metadata,
                    sector,
                    subsector,
                    industry,
                    listing_board,
                    fiscal_year_end_month,
                    now,
                    now,
                ),
            )
            inserted += 1
        else:
            conn.execute(
                """
                UPDATE companies
                SET issuer_name = ?, listing_date = COALESCE(?, listing_date),
                    source_urls = ?, raw_metadata = ?, last_seen_at = ?,
                    sector = CASE WHEN sector IN ('unknown', '') THEN ? ELSE sector END,
                    subsector = CASE WHEN subsector IN ('unknown', '') THEN ? ELSE subsector END,
                    industry = CASE WHEN industry IN ('unknown', '') THEN ? ELSE industry END,
                    listing_board = CASE WHEN listing_board = '' THEN ? ELSE listing_board END,
                    fiscal_year_end_month = CASE
                        WHEN (? BETWEEN 1 AND 11) AND fiscal_year_end_month = 12 THEN ?
                        ELSE fiscal_year_end_month
                    END
                WHERE ticker = ?
                """,
                (
                    issuer_name,
                    listing_date,
                    source_urls,
                    raw_metadata,
                    now,
                    sector,
                    subsector,
                    industry,
                    listing_board,
                    fiscal_year_end_month,
                    fiscal_year_end_month,
                    candidate.ticker,
                ),
            )
            updated += 1

    return DiscoveryPersistResult(inserted=inserted, updated=updated, skipped=0)


def persist_document_candidates(
    conn: sqlite3.Connection,
    candidates: list[DocumentCandidate],
) -> DiscoveryPersistResult:
    inserted = 0
    updated = 0
    skipped = 0

    for candidate in candidates:
        candidate_id = f"{candidate.source_name}:{candidate.source_record_id}"
        raw_metadata = json.dumps(candidate.raw_metadata, sort_keys=True)
        published_at = candidate.upstream_published_at.isoformat() if candidate.upstream_published_at else None
        discovered_at = candidate.discovered_at.isoformat()

        existing = conn.execute(
            "SELECT candidate_id FROM document_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO document_candidates (
                    candidate_id, source_name, source_record_id, ticker,
                    title, upstream_published_at, attachment_url,
                    raw_metadata, discovered_at, queued
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    candidate_id,
                    candidate.source_name,
                    candidate.source_record_id,
                    candidate.ticker,
                    candidate.title,
                    published_at,
                    candidate.attachment_url,
                    raw_metadata,
                    discovered_at,
                ),
            )
            inserted += 1
            continue

        current = conn.execute(
            """
            SELECT title, upstream_published_at, attachment_url, raw_metadata, ticker
            FROM document_candidates WHERE candidate_id = ?
            """,
            (candidate_id,),
        ).fetchone()
        if current is None:
            skipped += 1
            continue

        changed = (
            current["title"] != candidate.title
            or current["upstream_published_at"] != published_at
            or current["attachment_url"] != candidate.attachment_url
            or current["raw_metadata"] != raw_metadata
            or current["ticker"] != candidate.ticker
        )
        if not changed:
            skipped += 1
            continue

        conn.execute(
            """
            UPDATE document_candidates
            SET ticker = ?, title = ?, upstream_published_at = ?,
                attachment_url = ?, raw_metadata = ?, discovered_at = ?
            WHERE candidate_id = ?
            """,
            (
                candidate.ticker,
                candidate.title,
                published_at,
                candidate.attachment_url,
                raw_metadata,
                discovered_at,
                candidate_id,
            ),
        )
        updated += 1

    return DiscoveryPersistResult(inserted=inserted, updated=updated, skipped=skipped)


def persist_watermark(
    conn: sqlite3.Connection,
    *,
    source_name: str,
    source_class: SourceClass,
    window: DiscoveryWindow,
    raw_state: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO source_watermarks (
            source_name, source_class, window_start, window_end, cursor,
            raw_state_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_name) DO UPDATE SET
            source_class = excluded.source_class,
            window_start = excluded.window_start,
            window_end = excluded.window_end,
            cursor = excluded.cursor,
            raw_state_json = excluded.raw_state_json,
            updated_at = excluded.updated_at
        """,
        (
            source_name,
            source_class.value,
            window.start.isoformat() if window.start else None,
            window.end.isoformat() if window.end else None,
            window.cursor,
            json.dumps(raw_state or {}, sort_keys=True),
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def load_watermark(conn: sqlite3.Connection, source_name: str) -> DiscoveryWindow | None:
    row = conn.execute(
        "SELECT window_start, window_end, cursor FROM source_watermarks WHERE source_name = ?",
        (source_name,),
    ).fetchone()
    if row is None:
        return None
    start = datetime.fromisoformat(row["window_start"]).date() if row["window_start"] else None
    end = datetime.fromisoformat(row["window_end"]).date() if row["window_end"] else None
    return DiscoveryWindow(start=start, end=end, cursor=row["cursor"])
