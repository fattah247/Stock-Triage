"""Single-writer queue for SQLite (PRD §7, §10).

SQLite allows only one concurrent writer. Routing all writes through a single
background thread keeps lock contention bounded and lets callers fire writes
from any thread without juggling busy_timeouts.
"""

from __future__ import annotations

import queue
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..config import Config
from .connection import configure_pragmas, connect


@dataclass
class _Job:
    fn: Callable[[sqlite3.Connection], Any]
    result: queue.Queue


_SHUTDOWN = object()


class WriterQueue:
    """A single background writer thread that owns the write connection.

    Each submitted job runs in its own transaction, so a multi-statement job
    either lands completely or not at all.
    """

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._startup_error: BaseException | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="sqlite-writer", daemon=True)
        self._thread.start()
        self._started.wait()
        if self._startup_error is not None:
            self._thread.join()
            self._thread = None
            raise self._startup_error

    def stop(self) -> None:
        if self._thread is None:
            return
        self._q.put(_SHUTDOWN)
        self._thread.join()
        self._thread = None

    def submit(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        if self._thread is None:
            raise RuntimeError("WriterQueue not started")
        result: queue.Queue = queue.Queue(maxsize=1)
        self._q.put(_Job(fn=fn, result=result))
        ok, payload = result.get()
        if not ok:
            raise payload
        return payload

    def _run(self) -> None:
        try:
            conn = connect(self._cfg.paths.db_path)
            configure_pragmas(conn, self._cfg)
        except BaseException as exc:  # noqa: BLE001 - re-raised by start()
            self._startup_error = exc
            self._started.set()
            return
        self._started.set()
        try:
            while True:
                item = self._q.get()
                if item is _SHUTDOWN:
                    return
                job: _Job = item
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    payload = job.fn(conn)
                    conn.execute("COMMIT")
                    job.result.put((True, payload))
                except Exception as exc:  # noqa: BLE001 - propagated to caller
                    try:
                        if conn.in_transaction:
                            conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    job.result.put((False, exc))
        finally:
            conn.close()

    def __enter__(self) -> "WriterQueue":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()
