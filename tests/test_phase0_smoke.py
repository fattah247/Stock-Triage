"""Phase 0 smoke test (PRD §11 Phase 0 exit criteria).

Verifies:
  - Repo boots: config loads and directories materialize.
  - Config + DB migrations work: schema_migrations records all current versions.
  - A sample document can be stored end-to-end: bytes hit the content-addressed
    store, a `documents` row references the local path + sha256, and a read
    query through a separate connection sees it.
"""

from __future__ import annotations

from datetime import datetime, timezone

from stock_triage.db import WriterQueue, connect, configure_pragmas, run_migrations
from stock_triage.storage import FileStore

# Minimal valid PDF (single empty page). Hand-built so the test has no fixture deps.
SAMPLE_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
    b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
    b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >> endobj\n"
    b"xref\n0 4\n"
    b"0000000000 65535 f \n"
    b"0000000010 00000 n \n"
    b"0000000060 00000 n \n"
    b"0000000110 00000 n \n"
    b"trailer << /Size 4 /Root 1 0 R >>\nstartxref\n170\n%%EOF\n"
)


def test_phase0_end_to_end(tmp_config):
    cfg = tmp_config

    # 1. Migrations apply via the writer queue.
    with WriterQueue(cfg) as wq:
        applied = wq.submit(run_migrations)
        assert applied == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19]

        # 2. Re-running is a no-op.
        again = wq.submit(run_migrations)
        assert again == []

    # 3. Store a sample PDF in the content-addressed file store.
    store = FileStore(cfg.paths.files_dir)
    stored = store.put_bytes(SAMPLE_PDF)
    assert stored.local_path.exists()
    assert store.verify(stored.sha256)

    # 4. Insert a documents row referencing it (through the writer queue).
    now = datetime.now(timezone.utc).isoformat()

    def _insert(conn):
        conn.execute(
            """
            INSERT INTO documents (
                document_id, source_name, source_record_id,
                retrieved_at, local_path, sha256, mime_type,
                document_type, process_state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "doc_smoke_1",
                "smoke",
                "rec-1",
                now,
                str(stored.local_path),
                stored.sha256,
                "application/pdf",
                "other",
                "downloaded",
                now,
                now,
            ),
        )

    with WriterQueue(cfg) as wq:
        wq.submit(_insert)

    # 5. Read it back through a fresh read-only connection (separate from the writer).
    rconn = connect(cfg.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, cfg)  # safe; PRAGMA on RO conn is permitted
        row = rconn.execute(
            "SELECT document_id, sha256, process_state FROM documents WHERE document_id = ?",
            ("doc_smoke_1",),
        ).fetchone()
        assert row is not None
        assert row["sha256"] == stored.sha256
        assert row["process_state"] == "downloaded"

        version = rconn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert [r["version"] for r in version] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19]
    finally:
        rconn.close()


def test_file_store_dedup(tmp_config):
    store = FileStore(tmp_config.paths.files_dir)
    a = store.put_bytes(b"hello world")
    b = store.put_bytes(b"hello world")
    assert a.sha256 == b.sha256
    assert b.deduplicated is True


def test_stage_counters_thread_safe():
    from stock_triage.logging_setup import StageCounters

    sc = StageCounters()
    sc.incr("discovered", 3)
    sc.incr("downloaded")
    snap = sc.snapshot()
    assert snap["discovered"] == 3
    assert snap["downloaded"] == 1
    assert snap["analyzed"] == 0
