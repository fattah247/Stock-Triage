from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from stock_triage.__main__ import main
from stock_triage.db import WriterQueue, connect, configure_pragmas, run_migrations
from stock_triage.hardening import load_backlog, run_backfill, run_sqlite_benchmark
from stock_triage.sources import DocumentCandidate, SourceClass


def test_run_backfill_orchestrates_bounded_pipeline(tmp_config, monkeypatch):
    class FakeAdapter:
        name = "idx_financial_reports"
        source_class = SourceClass.FINANCIAL_REPORT

        def discover(self, window):
            return [
                DocumentCandidate(
                    source_name="idx_financial_reports",
                    source_class=SourceClass.FINANCIAL_REPORT,
                    source_record_id=f"record-{window.start.year}",
                    ticker="TAMU",
                )
            ]

    monkeypatch.setattr("stock_triage.hardening.pipeline._adapters_for_source", lambda source: [FakeAdapter()])
    monkeypatch.setattr(
        "stock_triage.hardening.pipeline.persist_document_candidates",
        lambda conn, c: SimpleNamespace(inserted=len(c), updated=0),
    )
    monkeypatch.setattr("stock_triage.hardening.pipeline.record_historical_discovery", lambda conn, **kwargs: None)
    monkeypatch.setattr(
        "stock_triage.hardening.pipeline.run_downloads",
        lambda cfg, source_name, limit: SimpleNamespace(inserted=2),
    )
    monkeypatch.setattr(
        "stock_triage.hardening.pipeline.run_parse",
        lambda cfg, source_name, limit: SimpleNamespace(parsed=2),
    )
    monkeypatch.setattr(
        "stock_triage.hardening.pipeline.run_extract",
        lambda cfg, source_name, limit: SimpleNamespace(extracted=1, partial=1),
    )
    monkeypatch.setattr(
        "stock_triage.hardening.pipeline.run_rules",
        lambda cfg, source_name, limit: SimpleNamespace(analyzed=2),
    )

    result = run_backfill(
        tmp_config,
        source="financial_report",
        start_year=2024,
        end_year=2025,
        discover_limit=1,
        download_limit=5,
        parse_limit=5,
        extract_limit=5,
        rules_limit=5,
    )

    assert [row.year for row in result.rows] == [2024, 2025]
    assert result.rows[0].discovered == 1
    assert result.rows[0].inserted == 1
    assert result.rows[0].downloaded == 2
    assert result.rows[0].analyzed == 2


def test_load_backlog_aggregates_process_state_counts(tmp_config):
    _seed_backlog_docs(tmp_config)

    report = load_backlog(tmp_config)

    assert report.rows[0].source_name == "idx_financial_reports"
    assert report.rows[0].discovered == 1
    assert report.rows[0].downloaded == 1
    assert report.rows[0].analyzed == 1


def test_run_sqlite_benchmark_returns_throughput_metrics(tmp_config):
    result = run_sqlite_benchmark(tmp_config, batches=4, rows_per_batch=25)

    assert result.total_rows == 100
    assert result.rows_per_second > 0
    assert result.avg_submit_ms >= 0


def test_phase9_cli_uses_hardening_commands(tmp_config, monkeypatch, capsys):
    monkeypatch.setattr("stock_triage.commands.hardening.load_config", lambda: tmp_config)
    monkeypatch.setattr(
        "stock_triage.commands.hardening.run_sqlite_benchmark",
        lambda cfg, batches, rows_per_batch: SimpleNamespace(
            batches=batches,
            rows_per_batch=rows_per_batch,
            total_rows=batches * rows_per_batch,
            total_seconds=0.5,
            rows_per_second=200.0,
            avg_submit_ms=2.0,
            p95_submit_ms=3.0,
        ),
    )
    monkeypatch.setattr(
        "stock_triage.commands.hardening.load_backlog",
        lambda cfg: SimpleNamespace(
            rows=[
                SimpleNamespace(
                    source_name="idx_financial_reports",
                    discovered=1,
                    downloaded=2,
                    parsed=3,
                    extracted=4,
                    analyzed=5,
                    failed=0,
                )
            ]
        ),
    )

    rc = main(["benchmark-sqlite", "--batches", "2", "--rows-per-batch", "10"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "benchmark-sqlite:" in out

    rc = main(["backlog-status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "idx_financial_reports: discovered=1" in out


def _seed_backlog_docs(tmp_config) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)

        def _insert(conn):
            conn.execute(
                """
                INSERT INTO document_candidates (
                    candidate_id, source_name, source_record_id, ticker, title, upstream_published_at,
                    attachment_url, raw_metadata, discovered_at, queued
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    "idx_financial_reports:candidate-only",
                    "idx_financial_reports",
                    "candidate-only",
                    "TAMU",
                    "Candidate only",
                    now,
                    "https://example.test/candidate.pdf",
                    "{}",
                    now,
                ),
            )
            for doc_id, state in (
                ("doc-downloaded", "downloaded"),
                ("doc-analyzed", "analyzed"),
            ):
                conn.execute(
                    """
                    INSERT INTO documents (
                        document_id, source_name, source_record_id, source_file_id, retrieved_at, original_url,
                        local_path, sha256, mime_type, document_type, language_variant, process_state,
                        ticker, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        doc_id,
                        "idx_financial_reports",
                        doc_id,
                        doc_id,
                        now,
                        "https://example.test/file.pdf",
                        "/tmp/example.pdf",
                        f"sha-{doc_id}",
                        "application/pdf",
                        "annual_report",
                        "bilingual",
                        state,
                        "TAMU",
                        now,
                        now,
                    ),
                )

        wq.submit(_insert)

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        row = rconn.execute("SELECT COUNT(*) AS cnt FROM documents").fetchone()
        assert row["cnt"] == 2
    finally:
        rconn.close()
