"""Phase 8 canary monitor: live source health snapshots and drift detection."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from ..config import Config
from ..db import WriterQueue, connect, configure_pragmas, run_migrations
from ..sources.adapters import IDXCompanyUniverseAdapter, IDXDisclosureAdapter, IDXFinancialReportAdapter


@dataclass(frozen=True)
class CanarySourceResult:
    source_name: str
    status: str
    accessible: bool
    drifted: bool
    fingerprint: str | None


@dataclass(frozen=True)
class CanaryRunResult:
    rows: list[CanarySourceResult]


@dataclass(frozen=True)
class CanaryStatusRow:
    source_name: str
    status: str
    accessible: bool
    drifted: bool
    finished_at: str


@dataclass(frozen=True)
class CanaryStatusReport:
    rows: list[CanaryStatusRow]


def run_canary(cfg: Config, *, source_name: str | None = None) -> CanaryRunResult:
    cfg.ensure_dirs()
    adapters = _adapters(cfg, source_name=source_name)
    rows: list[CanarySourceResult] = []
    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
        for adapter in adapters:
            started_at = datetime.now(timezone.utc)
            try:
                response = adapter.healthcheck()
            except Exception as exc:  # noqa: BLE001 - canary must persist failures instead of aborting
                response = {
                    "accessible": False,
                    "error": type(exc).__name__,
                    "detail": str(exc),
                }
            finished_at = datetime.now(timezone.utc)
            fingerprint = _fingerprint_response(response)
            previous = wq.submit(lambda conn, name=adapter.name: _load_previous_fingerprint(conn, source_name=name))
            accessible = bool(response.get("accessible", False))
            drifted = previous is not None and previous != fingerprint
            status = "failed" if not accessible else ("warning" if drifted else "ok")
            wq.submit(
                lambda conn, n=adapter.name, s=status, a=accessible, d=drifted, f=fingerprint, r=response, st=started_at, ft=finished_at: _persist_canary_run(
                    conn,
                    source_name=n,
                    status=s,
                    accessible=a,
                    drifted=d,
                    fingerprint=f,
                    response=r,
                    started_at=st.isoformat(),
                    finished_at=ft.isoformat(),
                )
            )
            rows.append(
                CanarySourceResult(
                    source_name=adapter.name,
                    status=status,
                    accessible=accessible,
                    drifted=drifted,
                    fingerprint=fingerprint,
                )
            )
    return CanaryRunResult(rows=rows)


def load_canary_status(cfg: Config) -> CanaryStatusReport:
    cfg.ensure_dirs()
    conn = connect(cfg.paths.db_path, read_only=True)
    try:
        configure_pragmas(conn, cfg)
        rows = conn.execute(
            """
            SELECT source_name, status, accessible, drifted, finished_at
            FROM canary_runs cr
            WHERE finished_at = (
                SELECT MAX(finished_at)
                FROM canary_runs sub
                WHERE sub.source_name = cr.source_name
            )
            ORDER BY source_name
            """
        ).fetchall()
        return CanaryStatusReport(
            rows=[
                CanaryStatusRow(
                    source_name=row["source_name"],
                    status=row["status"],
                    accessible=bool(row["accessible"]),
                    drifted=bool(row["drifted"]),
                    finished_at=row["finished_at"],
                )
                for row in rows
            ]
        )
    finally:
        conn.close()


def _adapters(cfg: Config, *, source_name: str | None) -> list[object]:
    items = [
        IDXCompanyUniverseAdapter(),
        IDXDisclosureAdapter(),
        IDXFinancialReportAdapter(),
    ]
    configured = set(cfg.canary.sources)
    if source_name:
        configured = {source_name}
    if configured:
        items = [adapter for adapter in items if adapter.name in configured]
    return items


def _fingerprint_response(response: dict[str, object]) -> str | None:
    normalized = {key: value for key, value in response.items() if key not in {"status"}}
    if not normalized:
        return None
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _load_previous_fingerprint(conn: sqlite3.Connection, *, source_name: str) -> str | None:
    row = conn.execute(
        """
        SELECT fingerprint
        FROM canary_runs
        WHERE source_name = ?
        ORDER BY finished_at DESC
        LIMIT 1
        """,
        (source_name,),
    ).fetchone()
    if row is None:
        return None
    return row["fingerprint"]


def _persist_canary_run(
    conn: sqlite3.Connection,
    *,
    source_name: str,
    status: str,
    accessible: bool,
    drifted: bool,
    fingerprint: str | None,
    response: dict[str, object],
    started_at: str,
    finished_at: str,
) -> None:
    canary_run_id = hashlib.sha256(f"{source_name}:{finished_at}:{fingerprint}".encode("utf-8")).hexdigest()[:24]
    conn.execute(
        """
        INSERT INTO canary_runs (
            canary_run_id, source_name, status, accessible, drifted, fingerprint,
            response_json, started_at, finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            canary_run_id,
            source_name,
            status,
            1 if accessible else 0,
            1 if drifted else 0,
            fingerprint,
            json.dumps(response, ensure_ascii=True, sort_keys=True, default=str),
            started_at,
            finished_at,
        ),
    )
