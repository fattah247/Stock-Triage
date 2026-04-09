from __future__ import annotations

from datetime import datetime, timezone

import fitz

from stock_triage.__main__ import main
from stock_triage.db import WriterQueue, connect, configure_pragmas, run_migrations
from stock_triage.parse import load_parse_status, run_parse
from stock_triage.storage import FileStore


def test_run_parse_persists_native_chunks_and_document_state(tmp_config):
    document_id = _insert_downloaded_document(
        tmp_config,
        source_name="idx_financial_reports",
        source_record_id="native-1",
        pdf_bytes=_pdf_bytes_with_pages(
            [
                "Revenue increased materially in 2025.\n\nGross profit also improved.",
                "Cash flow from operations remained positive throughout the year.",
            ]
        ),
    )

    result = run_parse(tmp_config, source_name="idx_financial_reports", limit=10)

    assert result.selected == 1
    assert result.parsed == 1
    assert result.failed == 0
    assert result.ocr_partial == 0
    assert result.ocr_full == 0
    assert result.chunks >= 2

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        doc = rconn.execute(
            """
            SELECT process_state, parse_strategy, parser_version, page_count, ocr_used, chunk_count
            FROM documents
            WHERE document_id = ?
            """,
            (document_id,),
        ).fetchone()
        assert doc is not None
        assert doc["process_state"] == "parsed"
        assert doc["parse_strategy"] == "native"
        assert doc["parser_version"] == "phase4-v1"
        assert doc["page_count"] == 2
        assert doc["ocr_used"] == 0
        assert doc["chunk_count"] >= 2

        chunks = rconn.execute(
            """
            SELECT page_start, page_end, char_start, char_end, text
            FROM document_chunks
            WHERE document_id = ?
            ORDER BY ord
            """,
            (document_id,),
        ).fetchall()
        assert len(chunks) >= 2
        assert chunks[0]["page_start"] == 1
        assert chunks[0]["page_end"] == 1
        assert chunks[0]["char_end"] > chunks[0]["char_start"]
        assert "Revenue increased" in chunks[0]["text"]
    finally:
        rconn.close()


def test_run_parse_uses_ocr_partial_when_some_pages_are_sparse(tmp_config, monkeypatch):
    document_id = _insert_downloaded_document(
        tmp_config,
        source_name="idx_disclosures",
        source_record_id="ocr-partial-1",
        pdf_bytes=_pdf_bytes_with_pages(
            [
                "This page already contains enough native text to skip OCR and retain a native parse strategy for this page only.",
                "",
            ]
        ),
    )

    monkeypatch.setattr("stock_triage.parse.pipeline._ocr_available", lambda: True)
    monkeypatch.setattr(
        "stock_triage.parse.pipeline._ocr_page_text",
        lambda cfg, page: "Scanned disclosure text recovered by OCR.",
    )

    result = run_parse(tmp_config, source_name="idx_disclosures", limit=10)

    assert result.selected == 1
    assert result.parsed == 1
    assert result.ocr_partial == 1
    assert result.ocr_full == 0

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        doc = rconn.execute(
            """
            SELECT process_state, parse_strategy, ocr_used
            FROM documents
            WHERE document_id = ?
            """,
            (document_id,),
        ).fetchone()
        assert doc["process_state"] == "parsed"
        assert doc["parse_strategy"] == "ocr_partial"
        assert doc["ocr_used"] == 1

        chunks = rconn.execute(
            "SELECT text FROM document_chunks WHERE document_id = ? ORDER BY ord",
            (document_id,),
        ).fetchall()
        joined = "\n".join(row["text"] for row in chunks)
        assert "Scanned disclosure text recovered by OCR." in joined
    finally:
        rconn.close()


def test_run_parse_marks_document_failed_when_ocr_is_unavailable(tmp_config, monkeypatch):
    document_id = _insert_downloaded_document(
        tmp_config,
        source_name="idx_disclosures",
        source_record_id="ocr-fail-1",
        pdf_bytes=_pdf_bytes_with_pages([""]),
    )

    monkeypatch.setattr("stock_triage.parse.pipeline._ocr_available", lambda: False)

    result = run_parse(tmp_config, source_name="idx_disclosures", limit=10)

    assert result.selected == 1
    assert result.parsed == 0
    assert result.failed == 1

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        doc = rconn.execute(
            """
            SELECT process_state, process_error_code, parse_strategy, chunk_count
            FROM documents
            WHERE document_id = ?
            """,
            (document_id,),
        ).fetchone()
        assert doc["process_state"] == "failed"
        assert doc["process_error_code"] == "ocr_unavailable"
        assert doc["parse_strategy"] == "parse_failed"
        assert doc["chunk_count"] == 0
    finally:
        rconn.close()


def test_run_parse_tolerates_blank_ocr_pages_when_other_pages_have_text(tmp_config, monkeypatch):
    document_id = _insert_downloaded_document(
        tmp_config,
        source_name="idx_financial_reports",
        source_record_id="ocr-blank-page-1",
        pdf_bytes=_pdf_bytes_with_pages(
            [
                "This page stays native and contains enough financial text to remain parseable without OCR.",
                "",
            ]
        ),
    )

    monkeypatch.setattr("stock_triage.parse.pipeline._ocr_available", lambda: True)
    monkeypatch.setattr("stock_triage.parse.pipeline._ocr_page_text", lambda cfg, page: "")

    result = run_parse(tmp_config, source_name="idx_financial_reports", limit=10)

    assert result.selected == 1
    assert result.parsed == 1
    assert result.failed == 0
    assert result.ocr_partial == 1

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        doc = rconn.execute(
            """
            SELECT process_state, parse_strategy, chunk_count
            FROM documents
            WHERE document_id = ?
            """,
            (document_id,),
        ).fetchone()
        assert doc["process_state"] == "parsed"
        assert doc["parse_strategy"] == "ocr_partial"
        assert doc["chunk_count"] >= 1
    finally:
        rconn.close()


def test_run_parse_persists_section_labels_from_heading_blocks(tmp_config):
    document_id = _insert_downloaded_document(
        tmp_config,
        source_name="idx_financial_reports",
        source_record_id="sections-1",
        pdf_bytes=_pdf_bytes_with_pages(
            [
                "MANAGEMENT DISCUSSION AND ANALYSIS\n\nRevenue increased materially in 2025 and margins expanded.\n\nRISK FACTORS\n\nLiquidity pressure remains a key risk to monitor.",
            ]
        ),
    )

    result = run_parse(tmp_config, source_name="idx_financial_reports", limit=10)

    assert result.selected == 1
    assert result.parsed == 1

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        rows = rconn.execute(
            """
            SELECT section_label, text
            FROM document_chunks
            WHERE document_id = ?
            ORDER BY ord
            """,
            (document_id,),
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["section_label"] == "MANAGEMENT DISCUSSION AND ANALYSIS"
        assert "Revenue increased materially" in rows[0]["text"]
        assert rows[1]["section_label"] == "RISK FACTORS"
        assert "Liquidity pressure remains" in rows[1]["text"]
    finally:
        rconn.close()


def test_parse_cli_uses_run_parse(tmp_config, monkeypatch, capsys):
    monkeypatch.setattr("stock_triage.__main__.load_config", lambda: tmp_config)
    monkeypatch.setattr(
        "stock_triage.__main__.run_parse",
        lambda cfg, source_name=None, limit=25, include_parsed=False, retry_failed_only=False: type(
            "R",
            (),
            {
                "selected": 2,
                "parsed": 1,
                "failed": 0,
                "ocr_partial": 1,
                "ocr_full": 0,
                "chunks": 6,
            },
        )(),
    )

    rc = main(["parse", "--source", "idx_disclosures", "--limit", "2"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "selected=2" in out
    assert "ocr_partial=1" in out
    assert "chunks=6" in out


def test_run_parse_retry_failed_only_targets_failed_documents(tmp_config, monkeypatch):
    failed_id = _insert_downloaded_document(
        tmp_config,
        source_name="idx_financial_reports",
        source_record_id="retry-failed-1",
        pdf_bytes=_pdf_bytes_with_pages([""]),
    )
    parsed_id = _insert_downloaded_document(
        tmp_config,
        source_name="idx_financial_reports",
        source_record_id="already-parsed-1",
        pdf_bytes=_pdf_bytes_with_pages(["Revenue remains positive and stable over the reporting period."]),
    )

    monkeypatch.setattr("stock_triage.parse.pipeline._ocr_available", lambda: False)
    first = run_parse(tmp_config, source_name="idx_financial_reports", limit=10)
    assert first.parsed == 1
    assert first.failed == 1

    monkeypatch.setattr("stock_triage.parse.pipeline._ocr_available", lambda: True)
    monkeypatch.setattr("stock_triage.parse.pipeline._ocr_page_text", lambda cfg, page: "Recovered OCR text")
    second = run_parse(tmp_config, source_name="idx_financial_reports", limit=10, retry_failed_only=True)
    assert second.selected == 1
    assert second.parsed == 1
    assert second.failed == 0

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        failed_row = rconn.execute(
            "SELECT process_state, parse_strategy FROM documents WHERE document_id = ?",
            (failed_id,),
        ).fetchone()
        parsed_row = rconn.execute(
            "SELECT process_state, parse_strategy FROM documents WHERE document_id = ?",
            (parsed_id,),
        ).fetchone()
        assert failed_row["process_state"] == "parsed"
        assert failed_row["parse_strategy"] == "ocr_full"
        assert parsed_row["process_state"] == "parsed"
        assert parsed_row["parse_strategy"] == "native"
    finally:
        rconn.close()


def test_load_parse_status_reports_recent_failures(tmp_config, monkeypatch):
    _insert_downloaded_document(
        tmp_config,
        source_name="idx_financial_reports",
        source_record_id="status-fail-1",
        pdf_bytes=_pdf_bytes_with_pages([""]),
    )
    _insert_downloaded_document(
        tmp_config,
        source_name="idx_disclosures",
        source_record_id="status-parsed-1",
        pdf_bytes=_pdf_bytes_with_pages(
            ["Disclosure text remains available for native parsing and comfortably exceeds the OCR threshold for this fixture."]
        ),
    )

    monkeypatch.setattr("stock_triage.parse.pipeline._ocr_available", lambda: False)
    run_parse(tmp_config, source_name=None, limit=10)

    report = load_parse_status(tmp_config, failure_limit=5)

    assert len(report.rows) == 2
    financial = next(row for row in report.rows if row.source_name == "idx_financial_reports")
    disclosure = next(row for row in report.rows if row.source_name == "idx_disclosures")
    assert financial.failed == 1
    assert disclosure.parsed == 1
    assert len(report.recent_failures) == 1
    assert report.recent_failures[0].source_record_id == "status-fail-1"
    assert report.recent_failures[0].process_error_code == "ocr_unavailable"


def test_parse_status_cli_uses_status_loader(tmp_config, monkeypatch, capsys):
    monkeypatch.setattr("stock_triage.__main__.load_config", lambda: tmp_config)
    monkeypatch.setattr(
        "stock_triage.__main__.load_parse_status",
        lambda cfg, source_name=None, failure_limit=10: type(
            "Report",
            (),
            {
                "rows": [
                    type(
                        "Row",
                        (),
                        {
                            "source_name": "idx_financial_reports",
                            "total": 2,
                            "downloaded": 0,
                            "parsed": 2,
                            "failed": 0,
                            "ocr_partial": 1,
                            "ocr_full": 0,
                        },
                    )()
                ],
                "recent_failures": [
                    type(
                        "Failure",
                        (),
                        {
                            "source_name": "idx_disclosures",
                            "document_id": "doc-fail",
                            "source_record_id": "rec-1",
                            "process_error_code": "ocr_unavailable",
                            "parse_strategy": "parse_failed",
                            "updated_at": "2026-04-09T00:00:00+00:00",
                        },
                    )()
                ],
            },
        )(),
    )

    rc = main(["parse-status", "--source", "all", "--failure-limit", "5"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "idx_financial_reports: total=2" in out
    assert "recent_failures:" in out
    assert "error=ocr_unavailable" in out


def _insert_downloaded_document(tmp_config, *, source_name: str, source_record_id: str, pdf_bytes: bytes) -> str:
    store = FileStore(tmp_config.paths.files_dir)
    stored = store.put_bytes(pdf_bytes)
    now = datetime.now(timezone.utc).isoformat()
    document_id = f"doc-{source_record_id}"

    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)

        def _insert(conn):
            conn.execute(
                """
                INSERT INTO documents (
                    document_id, source_name, source_record_id, source_file_id,
                    retrieved_at, original_url, local_path, sha256, mime_type,
                    document_type, language_variant, process_state, ticker,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    source_name,
                    source_record_id,
                    source_record_id,
                    now,
                    "https://example.test/file.pdf",
                    str(stored.local_path),
                    stored.sha256,
                    "application/pdf",
                    "disclosure",
                    "unknown",
                    "downloaded",
                    "TEST",
                    now,
                    now,
                ),
            )

        wq.submit(_insert)

    return document_id


def _pdf_bytes_with_pages(page_texts: list[str]) -> bytes:
    doc = fitz.open()
    try:
        for text in page_texts:
            page = doc.new_page()
            if text:
                page.insert_text((72, 72), text)
        return doc.tobytes()
    finally:
        doc.close()
