"""Migration runner. Applies numbered .sql files from src/stock_triage/migrations.

Each migration runs inside a savepoint for atomicity, so it nests inside the
WriterQueue's per-job transaction and still works on a bare autocommit
connection. We avoid sqlite3.Connection.executescript() because it issues an
implicit COMMIT before running, which would defeat the rollback handling.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def _applied_versions(conn: sqlite3.Connection) -> set[int]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {int(r[0]) for r in rows}


def _discover_migrations() -> list[tuple[int, Path]]:
    out: list[tuple[int, Path]] = []
    for p in sorted(MIGRATIONS_DIR.glob("*.sql")):
        stem = p.stem
        version_str = stem.split("_", 1)[0]
        try:
            version = int(version_str)
        except ValueError:
            continue
        out.append((version, p))
    return out


_COMMENT_LINE = re.compile(r"^\s*--.*$", re.MULTILINE)
_ALTER_ADD_COLUMN = re.compile(r"^\s*ALTER\s+TABLE\s+.+\s+ADD\s+COLUMN\s+.+$", re.IGNORECASE | re.DOTALL)


def _split_statements(sql: str) -> list[str]:
    """Split a migration script into executable statements.

    Strips line comments first, then splits on `;`. Migration files must use
    plain DDL only (no triggers/procedures with embedded semicolons).
    """
    cleaned = _COMMENT_LINE.sub("", sql)
    return [stmt.strip() for stmt in cleaned.split(";") if stmt.strip()]


def _is_safe_duplicate_column(stmt: str, exc: Exception) -> bool:
    return bool(_ALTER_ADD_COLUMN.match(stmt)) and "duplicate column name:" in str(exc).lower()


def run_migrations(conn: sqlite3.Connection) -> list[int]:
    """Apply pending migrations. Returns list of applied versions."""
    applied = _applied_versions(conn)
    newly_applied: list[int] = []
    for version, path in _discover_migrations():
        if version in applied:
            continue
        sql = path.read_text(encoding="utf-8")
        statements = _split_statements(sql)
        conn.execute("SAVEPOINT apply_migration")
        try:
            for stmt in statements:
                try:
                    conn.execute(stmt)
                except Exception as exc:
                    if _is_safe_duplicate_column(stmt, exc):
                        continue
                    raise
            cursor = conn.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, datetime.now(timezone.utc).isoformat()),
            )
            conn.execute("RELEASE apply_migration")
        except Exception:
            try:
                conn.execute("ROLLBACK TO apply_migration")
                conn.execute("RELEASE apply_migration")
            except sqlite3.OperationalError:
                pass
            raise
        if cursor.rowcount:
            newly_applied.append(version)
    return newly_applied
