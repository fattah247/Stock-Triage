"""Phase 12 thesis definitions, local sync, and deterministic evaluation helpers."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class LocalThesisCondition:
    severity: str
    kind: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class LocalThesisDefinition:
    thesis_id: str
    ticker: str | None
    thesis_name: str
    thesis_text: str
    owner_scope: str
    status: str
    review_cadence: str | None
    notes: str | None
    conditions: list[LocalThesisCondition]


@dataclass(frozen=True)
class ThesisEvaluationRecord:
    thesis_evaluation_id: str
    thesis_id: str
    ticker: str | None
    document_id: str | None
    status: str
    score: float
    rationale: str
    payload_json: str


def load_local_theses(path: Path) -> list[LocalThesisDefinition]:
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    theses_raw = raw.get("theses") or []
    definitions: list[LocalThesisDefinition] = []
    for idx, item in enumerate(theses_raw):
        thesis_name = item["thesis_name"]
        ticker = item.get("ticker")
        thesis_id = item.get("thesis_id") or _slugify(f"{ticker or 'global'}-{thesis_name}-{idx}")
        conditions = [
            LocalThesisCondition(
                severity=(condition.get("severity") or "warning"),
                kind=condition["kind"],
                payload={key: value for key, value in condition.items() if key not in {"severity", "kind"}},
            )
            for condition in (item.get("conditions") or [])
        ]
        definitions.append(
            LocalThesisDefinition(
                thesis_id=thesis_id,
                ticker=ticker,
                thesis_name=thesis_name,
                thesis_text=item["thesis_text"],
                owner_scope=item.get("owner_scope") or "local",
                status=item.get("status") or "active",
                review_cadence=item.get("review_cadence"),
                notes=item.get("notes"),
                conditions=conditions,
            )
        )
    return definitions


def sync_local_theses(conn: sqlite3.Connection, *, theses: list[LocalThesisDefinition]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    for thesis in theses:
        conn.execute(
            """
            INSERT INTO thesis_definitions (
                thesis_id, ticker, thesis_name, thesis_text, owner_scope, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(thesis_id)
            DO UPDATE SET
                ticker = excluded.ticker,
                thesis_name = excluded.thesis_name,
                thesis_text = excluded.thesis_text,
                owner_scope = excluded.owner_scope,
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (
                thesis.thesis_id,
                thesis.ticker,
                thesis.thesis_name,
                thesis.thesis_text,
                thesis.owner_scope,
                thesis.status,
                now,
                now,
            ),
        )
        conn.execute("DELETE FROM thesis_conditions WHERE thesis_id = ?", (thesis.thesis_id,))
        for ord_idx, condition in enumerate(thesis.conditions):
            payload = dict(condition.payload)
            payload.setdefault("severity", condition.severity)
            if thesis.review_cadence:
                payload.setdefault("review_cadence", thesis.review_cadence)
            if thesis.notes:
                payload.setdefault("notes", thesis.notes)
            conn.execute(
                """
                INSERT INTO thesis_conditions (
                    thesis_condition_id, thesis_id, ord, condition_kind, condition_payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    f"{thesis.thesis_id}:{ord_idx}",
                    thesis.thesis_id,
                    ord_idx,
                    condition.kind,
                    json.dumps(payload, sort_keys=True),
                    now,
                ),
            )


def load_db_theses(conn: sqlite3.Connection, *, ticker: str) -> list[LocalThesisDefinition]:
    rows = conn.execute(
        """
        SELECT thesis_id, ticker, thesis_name, thesis_text, owner_scope, status
        FROM thesis_definitions
        WHERE status = 'active'
          AND (ticker = ? OR ticker IS NULL OR ticker = '')
        ORDER BY CASE WHEN ticker = ? THEN 0 ELSE 1 END, thesis_name ASC
        """,
        (ticker, ticker),
    ).fetchall()
    out: list[LocalThesisDefinition] = []
    for row in rows:
        condition_rows = conn.execute(
            """
            SELECT condition_kind, condition_payload
            FROM thesis_conditions
            WHERE thesis_id = ?
            ORDER BY ord ASC
            """,
            (row["thesis_id"],),
        ).fetchall()
        conditions: list[LocalThesisCondition] = []
        review_cadence = None
        notes = None
        for condition_row in condition_rows:
            payload = json.loads(condition_row["condition_payload"] or "{}")
            review_cadence = review_cadence or payload.get("review_cadence")
            notes = notes or payload.get("notes")
            conditions.append(
                LocalThesisCondition(
                    severity=payload.get("severity") or "warning",
                    kind=condition_row["condition_kind"],
                    payload={k: v for k, v in payload.items() if k not in {"severity", "review_cadence", "notes"}},
                )
            )
        out.append(
            LocalThesisDefinition(
                thesis_id=row["thesis_id"],
                ticker=row["ticker"],
                thesis_name=row["thesis_name"],
                thesis_text=row["thesis_text"],
                owner_scope=row["owner_scope"],
                status=row["status"],
                review_cadence=review_cadence,
                notes=notes,
                conditions=conditions,
            )
        )
    return out


def _slugify(text: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in text).strip("-")
