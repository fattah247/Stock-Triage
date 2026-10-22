from __future__ import annotations

import sqlite3
import threading

import pytest

from stock_triage.db import WriterQueue, connect, run_migrations


def test_writer_job_is_atomic_when_it_fails_midway(tmp_config) -> None:
    def setup(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE ledger (id INTEGER PRIMARY KEY, note TEXT NOT NULL)")
        conn.execute("INSERT INTO ledger (id, note) VALUES (1, 'kept')")

    def replace_then_fail(conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM ledger")
        conn.execute("INSERT INTO ledger (id, note) VALUES (2, 'partial')")
        raise RuntimeError("interrupted between statements")

    with WriterQueue(tmp_config) as wq:
        wq.submit(setup)
        with pytest.raises(RuntimeError):
            wq.submit(replace_then_fail)
        rows = wq.submit(lambda conn: conn.execute("SELECT id, note FROM ledger").fetchall())

    assert [tuple(row) for row in rows] == [(1, "kept")]


def test_writer_start_raises_instead_of_hanging_when_connect_fails(tmp_config, monkeypatch) -> None:
    def broken_connect(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("stock_triage.db.writer.connect", broken_connect)
    outcome: list[BaseException] = []

    def start() -> None:
        try:
            WriterQueue(tmp_config).start()
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion
            outcome.append(exc)

    thread = threading.Thread(target=start, daemon=True)
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive(), "WriterQueue.start() hung on a failed connection"
    assert isinstance(outcome[0], sqlite3.OperationalError)


def test_migrations_nest_inside_writer_transaction_and_on_bare_connections(tmp_config) -> None:
    with WriterQueue(tmp_config) as wq:
        applied = wq.submit(run_migrations)
        assert applied, "fresh database should apply migrations"
        assert wq.submit(run_migrations) == []

    bare = connect(tmp_config.paths.db_path)
    try:
        assert run_migrations(bare) == []
        assert not bare.in_transaction
    finally:
        bare.close()
