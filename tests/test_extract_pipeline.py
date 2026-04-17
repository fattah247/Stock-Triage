from __future__ import annotations

import json
from datetime import datetime, timezone

from stock_triage.__main__ import main
from stock_triage.db import WriterQueue, connect, configure_pragmas, run_migrations
from stock_triage.extract import load_extract_status, run_extract
from stock_triage.extract.pipeline import _derive_financial_metrics
from stock_triage.parse import load_parse_status


def test_run_extract_persists_financial_metrics_and_evidence(tmp_config):
    document_id = _insert_parsed_document(
        tmp_config,
        source_name="idx_financial_reports",
        source_record_id="fin-1",
        document_type="annual_report",
        ticker="TAMU",
        raw_metadata={"report_year": "2025"},
        title="Annual & Sustainability Report 2024",
        chunks=[
            (
                0,
                15,
                None,
                "Ikhtisar Data Keuangan Financial Highlights\n"
                "Pendapatan 5,082,099 Revenue\n"
                "Laba Bruto (2,145,824) Gross Profit\n"
                "RUGI TAHUN BERJALAN (7,398,080) LOSS FOR THE YEAR\n"
                "JUMLAH ASET 34,565,439 TOTAL ASSETS\n"
                "JUMLAH LIABILITAS 16,011,455 TOTAL LIABILITIES\n"
                "Ekuitas 18,553,984 Equity\n",
            ),
            (
                1,
                87,
                None,
                "Arus Kas Bersih yang Net Cash Provided By (Used in) "
                "Diperoleh dari (Digunakan (1.115.589) Operating Activities untuk) Aktivitas Operasi",
            ),
            (
                2,
                92,
                None,
                "Laba Usaha 1,42 juta USD/Million USD\nOperating Profit",
            ),
        ],
    )

    result = run_extract(tmp_config, source_name="idx_financial_reports", limit=10)

    assert result.selected == 1
    assert result.extracted == 1
    assert result.partial == 0
    assert result.failed == 0
    assert result.metrics == 8
    assert result.events == 0

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        doc = rconn.execute(
            "SELECT process_state, process_error_code, pipeline_version FROM documents WHERE document_id = ?",
            (document_id,),
        ).fetchone()
        assert doc["process_state"] == "extracted"
        assert doc["process_error_code"] is None
        assert doc["pipeline_version"] == "phase5-v2"

        metrics = rconn.execute(
            """
            SELECT metric_name, value_normalized, currency, fiscal_year, period_end_date, evidence_span_id
            FROM metric_items
            WHERE document_id = ?
            ORDER BY metric_name
            """,
            (document_id,),
        ).fetchall()
        assert len(metrics) == 8
        names = {row["metric_name"] for row in metrics}
        assert names == {
            "revenue",
            "gross_profit",
            "operating_profit",
            "net_profit",
            "total_assets",
            "total_liabilities",
            "equity",
            "operating_cash_flow",
        }
        assert all(row["evidence_span_id"] for row in metrics)
        metrics_by_name = {row["metric_name"]: row for row in metrics}
        assert metrics_by_name["revenue"]["fiscal_year"] == 2024
        assert metrics_by_name["revenue"]["period_end_date"] == "2024-12-31"
        assert metrics_by_name["operating_profit"]["fiscal_year"] == 2025
        assert metrics_by_name["operating_profit"]["period_end_date"] == "2025-12-31"

        spans = rconn.execute(
            "SELECT COUNT(*) AS n FROM evidence_spans WHERE document_id = ?",
            (document_id,),
        ).fetchone()
        assert spans["n"] == 8

        run = rconn.execute(
            """
            SELECT status, error_code, metrics_extracted, events_detected
            FROM extraction_runs
            WHERE document_id = ?
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (document_id,),
        ).fetchone()
        assert run["status"] == "ok"
        assert run["error_code"] is None
        assert run["metrics_extracted"] == 8
        assert run["events_detected"] == 0
    finally:
        rconn.close()


def test_run_extract_persists_disclosure_events(tmp_config):
    document_id = _insert_parsed_document(
        tmp_config,
        source_name="idx_disclosures",
        source_record_id="disc-1",
        document_type="disclosure",
        ticker="CNAF",
        title="Treatise Summary/Treatise General Meeting of Shareholder's",
        chunks=[
            (
                0,
                1,
                None,
                "Resume Rapat Umum Pemegang Saham Tahunan PT CIMB Niaga Auto Finance "
                "yang telah diselenggarakan pada tanggal 6 April 2026.",
            ),
        ],
    )

    result = run_extract(tmp_config, source_name="idx_disclosures", limit=10)

    assert result.selected == 1
    assert result.extracted == 1
    assert result.metrics == 0
    assert result.events == 1

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        event = rconn.execute(
            """
            SELECT event_type, severity, impact_direction, confidence, payload_json, evidence_span_id
            FROM events
            WHERE document_id = ?
            """,
            (document_id,),
        ).fetchone()
        assert event["event_type"] == "shareholder_meeting_summary"
        assert event["severity"] == "info"
        assert event["impact_direction"] == "neutral"
        assert event["confidence"] == 0.95
        assert json.loads(event["payload_json"])["matched_regex"] == "rapat umum pemegang saham"
        assert event["evidence_span_id"] is not None
    finally:
        rconn.close()


def test_run_extract_detects_extended_event_taxonomy(tmp_config):
    document_id = _insert_parsed_document(
        tmp_config,
        source_name="idx_disclosures",
        source_record_id="disc-extended-1",
        document_type="disclosure",
        ticker="CNAF",
        title="Ringkasan Risalah RUPS",
        chunks=[
            (
                0,
                1,
                None,
                "Resume Rapat Umum Pemegang Saham Tahunan.\n"
                "X Dividen Tunai Tidak Ada Dividen.\n"
                "Menunjuk kembali Kantor Akuntan Publik Rintis, Jumadi, Rianto & Rekan.",
            ),
            (
                1,
                2,
                None,
                "Agenda 1 Rencana penerbitan Obligasi Perseroan melalui Penawaran Umum Berkelanjutan.\n"
                "Agenda 2 Rencana Perseroan untuk memperoleh fasilitas pembiayaan dari bank luar negeri.\n"
                "Mengangkat kembali anggota Dewan Komisaris.",
            ),
        ],
    )

    result = run_extract(tmp_config, source_name="idx_disclosures", limit=10)

    assert result.selected == 1
    assert result.extracted == 1
    assert result.events == 6

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        events = rconn.execute(
            "SELECT event_type, impact_direction, confidence, payload_json FROM events WHERE document_id = ? ORDER BY event_type",
            (document_id,),
        ).fetchall()
        assert [row["event_type"] for row in events] == [
            "auditor_change",
            "bond_issuance",
            "commissioner_change",
            "debt_facility",
            "dividend_announcement",
            "shareholder_meeting_summary",
        ]
        assert {row["impact_direction"] for row in events} == {"mixed", "negative", "neutral", "positive"}
        assert all(row["confidence"] is not None for row in events)
        assert all("matched_regex" in json.loads(row["payload_json"]) for row in events)
    finally:
        rconn.close()


def test_run_extract_derives_optional_financial_metrics_and_new_event_types(tmp_config):
    document_id = _insert_parsed_document(
        tmp_config,
        source_name="idx_financial_reports",
        source_record_id="fin-optional-1",
        document_type="annual_report",
        ticker="TAMU",
        raw_metadata={"report_year": "2025"},
        title="Annual Report 2024",
        chunks=[
            (
                0,
                1,
                None,
                "Cash and cash equivalents 900,000\n"
                "Pendapatan 5,082,099 Revenue\n"
                "Gross profit 2,145,824\n"
                "Operating profit 1,420,000\n"
                "Net profit 820,000\n"
                "Total liabilities 1,600,000\n"
                "Equity 2,400,000\n"
                "Trade receivables 450,000\n"
                "Inventory 320,000\n"
                "Short-term debt 300,000\n"
                "Long-term debt 500,000\n"
                "Shares outstanding 100,000,000\n"
                "Capital expenditure 200,000\n"
                "EBITDA 1,900,000\n"
                "Interest expense 100,000\n"
                "Arus kas yang digunakan untuk aktivitas operasi pada 2024 tercatat sebesar 1,12 juta USD",
            ),
        ],
    )
    _insert_parsed_document(
        tmp_config,
        source_name="idx_disclosures",
        source_record_id="disc-new-taxonomy-1",
        document_type="disclosure",
        ticker="CNAF",
        title="Keterbukaan Informasi",
        chunks=[
            (
                0,
                1,
                None,
                "Perseroan menyampaikan HMETD / rights issue dan private placement.\n"
                "Perseroan mengumumkan buyback dan stock split.\n"
                "Perseroan memperoleh kontrak baru namun juga menghadapi pemutusan kontrak material.\n"
                "Perseroan menghadapi gugatan hukum dan sanksi regulator.\n"
                "Perseroan merevisi proyeksi serta menyampaikan penggunaan dana hasil penawaran umum.\n"
                "Perseroan menghentikan segmen usaha tertentu.",
            ),
        ],
    )

    result = run_extract(tmp_config, limit=10, include_extracted=True)

    assert result.selected == 2
    assert result.events >= 10

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        metrics = rconn.execute(
            """
            SELECT metric_name, value_normalized
            FROM metric_items
            WHERE document_id = ?
            ORDER BY metric_name
            """,
            (document_id,),
        ).fetchall()
        names = {row["metric_name"] for row in metrics}
        assert {
            "revenue",
            "gross_profit",
            "operating_profit",
            "net_profit",
            "operating_cash_flow",
            "equity",
            "total_liabilities",
            "receivables",
            "inventory",
            "cash",
            "short_term_debt",
            "long_term_debt",
            "share_count",
            "capex",
            "net_debt",
            "free_cash_flow",
            "ebitda",
            "interest_coverage",
        }.issubset(names)
        values = {row["metric_name"]: row["value_normalized"] for row in metrics}
        assert values["receivables"] == 450000.0
        assert values["inventory"] == 320000.0
        assert values["net_debt"] == -100000.0
        assert values["free_cash_flow"] == -1320000.0
        assert values["interest_coverage"] == 14.2

        disclosure_events = rconn.execute(
            """
            SELECT event_type, impact_direction, confidence, payload_json
            FROM events
            WHERE document_id = 'doc-disc-new-taxonomy-1'
            ORDER BY event_type
            """
        ).fetchall()
        assert [row["event_type"] for row in disclosure_events] == [
            "buyback",
            "guidance_change",
            "lawsuit",
            "major_contract_loss",
            "major_contract_win",
            "private_placement",
            "regulatory_action",
            "rights_issue",
            "segment_closure",
            "stock_split",
            "use_of_proceeds",
        ]
        event_map = {row["event_type"]: dict(row) for row in disclosure_events}
        assert event_map["lawsuit"]["impact_direction"] == "negative"
        assert event_map["regulatory_action"]["confidence"] == 0.95
        assert "section_label" in json.loads(event_map["rights_issue"]["payload_json"])
    finally:
        rconn.close()


def test_run_extract_captures_segment_metrics(tmp_config):
    document_id = _insert_parsed_document(
        tmp_config,
        source_name="idx_financial_reports",
        source_record_id="fin-segments-1",
        document_type="annual_report",
        ticker="TAMU",
        raw_metadata={"report_year": "2025"},
        title="Annual Report 2024",
        chunks=[
            (
                0,
                1,
                "Segment Performance",
                "Segment Retail revenue 700,000\n"
                "Segment Retail operating profit 120,000\n"
                "Segmen Distribusi pendapatan 450,000\n"
                "Segmen Distribusi laba 75,000\n",
            ),
        ],
    )

    result = run_extract(tmp_config, limit=10, include_extracted=True)

    assert result.selected == 1

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        metrics = rconn.execute(
            """
            SELECT metric_name, value_normalized, consolidation_scope
            FROM metric_items
            WHERE document_id = ?
              AND metric_name IN ('segment_revenue', 'segment_profit')
            ORDER BY metric_name, consolidation_scope
            """,
            (document_id,),
        ).fetchall()
        assert [dict(row) for row in metrics] == [
            {"metric_name": "segment_profit", "value_normalized": 75000.0, "consolidation_scope": "segment:Distribusi"},
            {"metric_name": "segment_profit", "value_normalized": 120000.0, "consolidation_scope": "segment:Retail"},
            {"metric_name": "segment_revenue", "value_normalized": 450000.0, "consolidation_scope": "segment:Distribusi"},
            {"metric_name": "segment_revenue", "value_normalized": 700000.0, "consolidation_scope": "segment:Retail"},
        ]
    finally:
        rconn.close()


def test_run_extract_detects_financial_note_nuance_events(tmp_config):
    document_id = _insert_parsed_document(
        tmp_config,
        source_name="idx_financial_reports",
        source_record_id="fin-note-nuance-1",
        document_type="annual_report",
        ticker="TAMU",
        raw_metadata={"report_year": "2025"},
        title="Annual Report 2024",
        chunks=[
            (
                0,
                10,
                "Independent Auditor Report",
                "The auditor issued a qualified opinion and noted material uncertainty related to going concern.",
            ),
            (
                1,
                45,
                "Notes to Financial Statements",
                "The Company requested a waiver for covenant compliance. Related party transactions remain material. The notes also describe contingent liabilities and commitments.",
            ),
            (
                2,
                60,
                "Financial Highlights",
                "Revenue 5,082,099\nGross profit 2,145,824\nOperating profit 1,420,000\nNet profit 820,000\nTotal Assets 4,000,000\nTotal Liabilities 1,600,000\nEquity 2,400,000\nOperating cash flow 200,000",
            ),
        ],
    )

    result = run_extract(tmp_config, source_name="idx_financial_reports", limit=10)

    assert result.selected == 1
    assert result.partial == 1
    assert result.events >= 5

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        rows = rconn.execute(
            """
            SELECT event_type, severity, impact_direction, confidence, payload_json
            FROM events
            WHERE document_id = ?
            ORDER BY event_type
            """,
            (document_id,),
        ).fetchall()
        event_types = [row["event_type"] for row in rows]
        assert {
            "qualified_audit_opinion",
            "going_concern_warning",
            "covenant_stress",
            "related_party_transaction",
            "contingent_liability",
        }.issubset(set(event_types))
        event_map = {row["event_type"]: dict(row) for row in rows}
        assert event_map["qualified_audit_opinion"]["severity"] == "critical"
        assert event_map["going_concern_warning"]["impact_direction"] == "negative"
        assert event_map["covenant_stress"]["confidence"] == 0.92
        assert json.loads(event_map["related_party_transaction"]["payload_json"])["section_label"] == "Notes to Financial Statements"
    finally:
        rconn.close()


def test_parse_status_counts_extracted_documents_as_parse_complete(tmp_config):
    _insert_parsed_document(
        tmp_config,
        source_name="idx_financial_reports",
        source_record_id="fin-status-1",
        document_type="annual_report",
        ticker="TAMU",
        raw_metadata={"report_year": "2024"},
        chunks=[
            (
                0,
                15,
                None,
                "Pendapatan 5,082,099 Revenue\n"
                "Laba Bruto (2,145,824) Gross Profit\n"
                "RUGI TAHUN BERJALAN (7,398,080)\n"
                "JUMLAH ASET 34,565,439\nJUMLAH LIABILITAS 16,011,455\nEkuitas 18,553,984",
            ),
            (1, 87, None, "Arus Kas Bersih yang Net Cash Provided By (Used in) Diperoleh dari (Digunakan (1.115.589) Operating Activities"),
            (2, 92, None, "Laba Usaha 1,42 juta USD/Million USD"),
        ],
    )

    run_extract(tmp_config, source_name="idx_financial_reports", limit=10)
    report = load_parse_status(tmp_config, source_name="idx_financial_reports", failure_limit=5)

    assert len(report.rows) == 1
    assert report.rows[0].parsed == 1
    assert report.rows[0].downloaded == 0
    assert report.rows[0].failed == 0


def test_extract_status_reports_completeness_and_failures(tmp_config):
    _insert_parsed_document(
        tmp_config,
        source_name="idx_financial_reports",
        source_record_id="fin-status-1",
        document_type="annual_report",
        ticker="TAMU",
        raw_metadata={"report_year": "2025"},
        title="Annual & Sustainability Report 2024",
        chunks=[
            (
                0,
                15,
                None,
                "Ikhtisar Data Keuangan Financial Highlights\n"
                "Pendapatan 5,082,099 Revenue\n"
                "Laba Bruto (2,145,824) Gross Profit\n"
                "RUGI TAHUN BERJALAN (7,398,080)\n"
                "JUMLAH ASET 34,565,439\nJUMLAH LIABILITAS 16,011,455\nEkuitas 18,553,984",
            ),
            (1, 87, None, "Arus kas yang digunakan untuk aktivitas operasi pada 2024 tercatat sebesar 1,12 juta USD"),
            (2, 92, None, "Laba Usaha 1,42 juta USD/Million USD"),
        ],
    )
    _insert_parsed_document(
        tmp_config,
        source_name="idx_financial_reports",
        source_record_id="fin-status-2",
        document_type="annual_report",
        ticker="TAMU",
        raw_metadata={"report_year": "2025"},
        title="ESG2025-TAMU.pdf",
        chunks=[
            (
                0,
                18,
                None,
                "Attachment 1. AR-SR Tamarin 2024 FINAL_compressed.pdf\n"
                "This is an official document generated electronically.",
            ),
        ],
    )

    run_extract(tmp_config, source_name="idx_financial_reports", limit=10)
    report = load_extract_status(tmp_config, source_name="idx_financial_reports", failure_limit=5)

    assert len(report.rows) == 1
    row = report.rows[0]
    assert row.extracted == 1
    assert row.partial == 0
    assert row.failed == 1
    assert row.metrics == 8
    assert row.completeness_pct == 100.0
    assert report.recent_failures[0].error_code == "unsupported_document_kind:attachment_notice"


def test_extract_cli_uses_run_extract(tmp_config, monkeypatch, capsys):
    monkeypatch.setattr("stock_triage.commands.pipeline.load_config", lambda: tmp_config)
    monkeypatch.setattr(
        "stock_triage.commands.pipeline.run_extract",
        lambda cfg, source_name=None, document_id=None, limit=25, include_extracted=False, retry_failed_only=False: type(
            "R",
            (),
            {
                "selected": 2,
                "extracted": 1,
                "partial": 1,
                "failed": 0,
                "metrics": 8,
                "events": 1,
            },
        )(),
    )

    rc = main(["extract", "--source", "idx_financial_reports", "--limit", "2"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "selected=2" in out
    assert "partial=1" in out
    assert "events=1" in out


def test_run_extract_can_target_single_document(tmp_config):
    target_id = _insert_parsed_document(
        tmp_config,
        source_name="idx_financial_reports",
        source_record_id="extract-target-1",
        document_type="annual_report",
        ticker="TAMU",
        raw_metadata={"report_year": "2025"},
        title="Targeted extract",
        chunks=[
            (
                0,
                1,
                None,
                "Pendapatan 5,082,099 Revenue\nJUMLAH ASET 34,565,439 TOTAL ASSETS\nJUMLAH LIABILITAS 16,011,455",
            ),
        ],
    )
    _insert_parsed_document(
        tmp_config,
        source_name="idx_financial_reports",
        source_record_id="extract-target-2",
        document_type="annual_report",
        ticker="TAMU",
        raw_metadata={"report_year": "2025"},
        title="Untouched extract",
        chunks=[(0, 1, None, "Pendapatan 1 Revenue\nJUMLAH ASET 2 TOTAL ASSETS\nJUMLAH LIABILITAS 1")],
    )

    result = run_extract(tmp_config, document_id=target_id)

    assert result.selected == 1
    assert result.extracted + result.partial == 1

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        target = rconn.execute("SELECT process_state FROM documents WHERE document_id = ?", (target_id,)).fetchone()
        untouched = rconn.execute(
            "SELECT process_state FROM documents WHERE source_record_id = ?",
            ("extract-target-2",),
        ).fetchone()
        assert target["process_state"] in {"extracted", "analyzed"}
        assert untouched["process_state"] == "parsed"
    finally:
        rconn.close()


def test_extract_status_cli_uses_load_extract_status(tmp_config, monkeypatch, capsys):
    monkeypatch.setattr("stock_triage.commands.pipeline.load_config", lambda: tmp_config)
    monkeypatch.setattr(
        "stock_triage.commands.pipeline.load_extract_status",
        lambda cfg, source_name=None, failure_limit=10: type(
            "R",
            (),
            {
                "rows": [
                    type(
                        "Row",
                        (),
                        {
                            "source_name": "idx_financial_reports",
                            "total": 2,
                            "pending": 0,
                            "extracted": 1,
                            "partial": 0,
                            "failed": 1,
                            "metrics": 8,
                            "events": 2,
                            "completeness_pct": 100.0,
                        },
                    )()
                ],
                "recent_failures": [
                    type(
                        "Failure",
                        (),
                        {
                            "source_name": "idx_financial_reports",
                            "document_id": "doc-1",
                            "source_record_id": "fin-1",
                            "status": "failed",
                            "error_code": "unsupported_document_kind:attachment_notice",
                            "updated_at": "2026-04-09T00:00:00+00:00",
                        },
                    )()
                ],
            },
        )(),
    )

    rc = main(["extract-status", "--source", "idx_financial_reports", "--failure-limit", "3"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "completeness=100.00%" in out
    assert "recent_failures:" in out
    assert "unsupported_document_kind:attachment_notice" in out


def _insert_parsed_document(
    tmp_config,
    *,
    source_name: str,
    source_record_id: str,
    document_type: str,
    ticker: str,
    chunks: list[tuple[int, int, str | None, str]],
    raw_metadata: dict | None = None,
    title: str | None = None,
) -> str:
    now = datetime.now(timezone.utc).isoformat()
    document_id = f"doc-{source_record_id}"
    candidate_id = f"{source_name}:{source_record_id}"

    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)

        def _insert(conn):
            conn.execute(
                """
                INSERT INTO document_candidates (
                    candidate_id, source_name, source_record_id, ticker,
                    title, attachment_url, raw_metadata, discovered_at, queued
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    candidate_id,
                    source_name,
                    source_record_id,
                    ticker,
                    title,
                    "https://example.test/file.pdf",
                    json.dumps(raw_metadata or {}, sort_keys=True),
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO documents (
                    document_id, source_name, source_record_id, source_file_id,
                    retrieved_at, original_url, local_path, sha256, mime_type,
                    document_type, language_variant, process_state, parser_version,
                    ticker, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    source_name,
                    source_record_id,
                    source_record_id,
                    now,
                    "https://example.test/file.pdf",
                    "/tmp/example.pdf",
                    f"sha-{source_record_id}",
                    "application/pdf",
                    document_type,
                    "bilingual",
                    "parsed",
                    "phase4-v1",
                    ticker,
                    now,
                    now,
                ),
            )
            char_cursor = 0
            for ord_value, page_start, section_label, text in chunks:
                chunk_id = f"chunk-{source_record_id}-{ord_value}"
                conn.execute(
                    """
                    INSERT INTO document_chunks (
                        chunk_id, document_id, section_label, page_start, page_end,
                        char_start, char_end, text, ord
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk_id,
                        document_id,
                        section_label,
                        page_start,
                        page_start,
                        char_cursor,
                        char_cursor + len(text),
                        text,
                        ord_value,
                    ),
                )
                char_cursor += len(text) + 1

        wq.submit(_insert)
    return document_id


def test_derive_financial_metrics_handles_missing_debt_leg():
    class _Metric:
        def __init__(self, value):
            self.value_normalized = value
            self.raw_value_text = str(value)
            self.currency = "IDR"
            self.unit_scale = "units"
            self.fiscal_year = 2024
            self.fiscal_period = "Annual"
            self.period_end_date = "2024-12-31"
            self.audit_status = "audited"
            self.consolidation_scope = "consolidated"
            self.language_variant = "id"
            self.chunk = None
            self.excerpt_text = ""
            self.char_start = 0
            self.char_end = 0

    derived = _derive_financial_metrics(
        {
            "cash": _Metric(100.0),
            "long_term_debt": _Metric(250.0),
        }
    )

    net_debt = next(metric for metric in derived if metric.metric_name == "net_debt")
    assert net_debt.value_normalized == 150.0
