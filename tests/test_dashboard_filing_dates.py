from __future__ import annotations

from stock_triage.dashboard.snapshot import _load_documents_for_ticker, _load_issuers, _load_revision_history_for_ticker
from stock_triage.db import WriterQueue, connect, run_migrations

_FILED = "2025-01-22T19:52:13.833"
_REPROCESSED = "2026-10-19T08:03:11+00:00"


def _seed_reprocessed_filing(tmp_config) -> None:
    def _seed(conn):
        conn.execute(
            "INSERT INTO companies (ticker, issuer_name, first_seen_at, last_seen_at) VALUES ('ABCD', 'Issuer ABCD', ?, ?)",
            (_FILED, _FILED),
        )
        # Seen by discovery today, but nothing filed yet.
        conn.execute(
            "INSERT INTO companies (ticker, issuer_name, first_seen_at, last_seen_at) VALUES ('NONE', 'Issuer NONE', ?, ?)",
            (_REPROCESSED, _REPROCESSED),
        )
        conn.execute(
            """
            INSERT INTO document_candidates (
                candidate_id, source_name, source_record_id, ticker, title, attachment_url, raw_metadata, discovered_at, queued
            ) VALUES ('idx_financial_reports:rec-1', 'idx_financial_reports', 'rec-1', 'ABCD',
                      'FinancialStatement-2024-Tahunan-ABCD.xlsx', 'https://example.test/a.xlsx', '{}', ?, 0)
            """,
            (_FILED,),
        )
        conn.execute(
            """
            INSERT INTO document_versions (version_group_id, source_name, source_record_id, ticker, created_at, source_file_id)
            VALUES ('version:rec-1', 'idx_financial_reports', 'rec-1', 'ABCD', ?, 'rec-1')
            """,
            (_FILED,),
        )
        # Filed in January 2025; the pipeline touched the row again today.
        conn.execute(
            """
            INSERT INTO documents (
                document_id, source_name, source_record_id, source_file_id, source_modified_at, retrieved_at,
                local_path, sha256, mime_type, document_type, process_state, version_group_id, ticker, created_at, updated_at
            ) VALUES ('doc-1', 'idx_financial_reports', 'rec-1', 'rec-1', ?, '2025-02-01T00:00:00+00:00',
                      '/tmp/a.xlsx', 'sha-1', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                      'financial_statement', 'analyzed', 'version:rec-1', 'ABCD', ?, ?)
            """,
            (_FILED, _FILED, _REPROCESSED),
        )

    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)
        wq.submit(_seed)


def test_dashboard_dates_filings_by_publication_not_reprocessing(tmp_config):
    _seed_reprocessed_filing(tmp_config)

    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        issuers = _load_issuers(conn)
        issuer = next(row for row in issuers if row["ticker"] == "ABCD")
        (document,) = _load_documents_for_ticker(conn, ticker="ABCD", limit=10)
        (revision,) = _load_revision_history_for_ticker(conn, ticker="ABCD", limit=10)
    finally:
        conn.close()

    assert issuer["latest_filing_at"] == _FILED
    # An issuer with a filing lists ahead of one merely seen more recently by discovery.
    assert [row["ticker"] for row in issuers] == ["ABCD", "NONE"]
    assert (document["filed_at"], document["updated_at"]) == (_FILED, _REPROCESSED)
    assert document["title"] == "FinancialStatement-2024-Tahunan-ABCD.xlsx"
    assert revision["latest_revision_at"] == _FILED
