"""SQLite connection helpers honoring single-writer + WAL constraints (PRD §7, §10)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ..config import Config


def connect(db_path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if read_only:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    else:
        conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def configure_pragmas(conn: sqlite3.Connection, cfg: Config) -> None:
    conn.execute(f"PRAGMA journal_mode = {cfg.database.journal_mode}")
    conn.execute(f"PRAGMA synchronous = {cfg.database.synchronous}")
    conn.execute(f"PRAGMA busy_timeout = {cfg.database.busy_timeout_ms}")
