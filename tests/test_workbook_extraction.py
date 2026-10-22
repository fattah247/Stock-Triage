from __future__ import annotations

import io
import json
import zipfile
from datetime import date, datetime, timezone
from xml.sax.saxutils import escape

import pytest

from stock_triage.dashboard.snapshot import _load_metrics_for_ticker
from stock_triage.db import WriterQueue, connect, run_migrations
from stock_triage.extract import run_extract
from stock_triage.intelligence.pipeline import (
    CompanyDocument,
    _build_bundle,
    _load_document_metrics,
    _load_ticker_metrics,
    _prefer_workbook_anchor,
)
from stock_triage.parse import run_parse
from stock_triage.parse.pipeline import CHUNK_TARGET_CHARS
from stock_triage.storage import FileStore

from .test_extract_pipeline import _insert_parsed_document

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class _Number(str):
    """A numeric cell stored with this exact text, e.g. Excel's '5.0005E3'."""


class _Inline(str):
    """A string cell stored inline rather than in the shared-strings table."""


def _xlsx_bytes(sheets: list[tuple[str, str | None, list[list[object]]]]) -> bytes:
    """A minimal workbook: (sheet name, state, rows of cell values) per sheet."""
    shared: list[str] = []

    def cell(reference: str, value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, _Number) or isinstance(value, (int, float)):
            return f'<c r="{reference}"><v>{value}</v></c>'
        if isinstance(value, _Inline):
            return f'<c r="{reference}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
        shared.append(str(value))
        return f'<c r="{reference}" t="s"><v>{len(shared) - 1}</v></c>'

    worksheets = []
    for _, _, rows in sheets:
        body = "".join(
            f'<row r="{row_number}">'
            + "".join(cell(f"{'ABCDEFGH'[column]}{row_number}", value) for column, value in enumerate(row))
            + "</row>"
            for row_number, row in enumerate(rows, start=1)
        )
        worksheets.append(
            f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{body}</sheetData></worksheet>'
        )
    sheet_entries = "".join(
        f'<sheet name="{name}" sheetId="{index}"{f" state={chr(34)}{state}{chr(34)}" if state else ""} r:id="rId{index}"/>'
        for index, (name, state, _) in enumerate(sheets, start=1)
    )
    relationship = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    rels = "".join(
        f'<Relationship Id="rId{index}" Type="{relationship}/worksheet" Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, len(sheets) + 1)
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            f'xmlns:r="{relationship}"><sheets>{sheet_entries}</sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'{rels}<Relationship Id="rIdStrings" Type="{relationship}/sharedStrings" Target="sharedStrings.xml"/>'
            "</Relationships>",
        )
        for index, worksheet in enumerate(worksheets, start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", worksheet)
        archive.writestr(
            "xl/sharedStrings.xml",
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            + "".join(f"<si><t>{escape(text)}</t></si>" for text in shared)
            + "</sst>",
        )
    return buffer.getvalue()


def _statement_workbook(
    *,
    period_end: str = "2025-12-31",
    rounding: str | None = "Jutaan / In Million",
    currency: str = "Dolar Amerika Serikat / USD",
    policy_text: str = "Laporan keuangan disusun sesuai SAK.",
) -> bytes:
    general = [
        ["[1000000] General information"],
        [],
        ["Informasi umum", "General information"],
        [None, "CurrentYearInstant"],
        ["Informasi umum", None, "General information"],
        ["Kode entitas", "ABCD", "Entity code"],
        [
            "Apakah merupakan laporan keuangan satu entitas atau suatu kelompok entitas",
            "Entitas grup / Group entity",
            "Whether the financial statements are of an individual entity or a group of entities",
        ],
        ["Tanggal akhir periode berjalan", period_end, "Current period end date"],
        ["Mata uang pelaporan", currency, "Description of presentation currency"],
        ["Jenis laporan atas laporan keuangan", "Diaudit / Audited", "Type of report on financial statements"],
    ]
    if rounding is not None:
        general.append(
            [
                "Pembulatan yang digunakan dalam penyajian jumlah dalam laporan keuangan",
                rounding,
                "Level of rounding used in financial statements",
            ]
        )
    return _xlsx_bytes(
        [
            ("Context", "veryHidden", [["Context"], ["identifier", "entityCode"]]),
            ("1000000", None, general),
            (
                "1210000",
                None,
                [
                    ["[1210000] Statement of financial position presented using current and non-current - General Industry"],
                    [],
                    ["Laporan posisi keuangan", None, None, "Statement of financial position"],
                    [None, "CurrentYearInstant", "PriorEndYearInstant"],
                    ["Aset", None, None, "Assets"],
                    ["Kas dan setara kas", _Number("1.2E3"), 900, "Cash and cash equivalents"],
                    ["Persediaan lancar", None, None, "Current inventories"],
                    ["Persediaan lancar", 300, 250, "Current inventories"],
                    ["Jumlah aset", _Number("5.0005E3"), 4000, "Total assets"],
                    [_Inline("Jumlah liabilitas"), 5500.25, 4300, "Total liabilities"],
                    [
                        "Jumlah ekuitas yang diatribusikan kepada pemilik entitas induk",
                        -510,
                        -310,
                        "Total equity attributable to equity owners of parent entity",
                    ],
                    ["Kepentingan non-pengendali", 10.25, 10, "Non-controlling interests"],
                    ["Jumlah ekuitas", -499.75, -300, "Total equity"],
                    ["Jumlah liabilitas dan ekuitas", _Number("5.0005E3"), 4000, "Total liabilities and equity"],
                ],
            ),
            # The template's unused alternative: hidden, ahead of the visible one, and never read.
            (
                "1312000",
                "veryHidden",
                [
                    ["[1312000] Statement of profit or loss and other comprehensive income, by nature - General Industry"],
                    [None, "CurrentYearDuration", "PriorYearDuration"],
                    ["Penjualan dan pendapatan usaha", 999999, 999999, "Sales and revenue"],
                ],
            ),
            (
                "1311000",
                None,
                [
                    ["[1311000] Statement of profit or loss and other comprehensive income, by function - General Industry"],
                    [None, "CurrentYearDuration", "PriorYearDuration"],
                    ["Penjualan dan pendapatan usaha", 2000, 1800, "Sales and revenue"],
                    ["Beban pokok penjualan dan pendapatan", 1500, 1400, "Cost of sales and revenue"],
                    ["Jumlah laba bruto", 500, 400, "Total gross profit"],
                    ["Beban bunga dan keuangan", 40, 35, "Interest and finance costs"],
                    ["Jumlah laba (rugi)", -120, 15, "Total profit (loss)"],
                    ["Laba (rugi) yang dapat diatribusikan ke entitas induk", -125, 12, "Profit (loss) attributable to parent entity"],
                ],
            ),
            (
                "1510000",
                None,
                [
                    ["[1510000] Statement of cash flows, direct method - General Industry"],
                    [None, "CurrentYearDuration", "PriorYearDuration"],
                    [
                        "Jumlah arus kas bersih yang diperoleh dari (digunakan untuk) aktivitas operasi",
                        150,
                        90,
                        "Total net cash flows received from (used in) operating activities",
                    ],
                    ["Pembayaran untuk perolehan aset tetap", 60, 20, "Payments for acquisition of property, plant and equipment"],
                    ["Kas dan setara kas arus kas, akhir periode", 1200, 900, "Cash and cash equivalents cash flows, end of the period"],
                ],
            ),
            (
                "1610000",
                None,
                [
                    ["[1610000] Explanation for Significant Accounting Policy - General Industry"],
                    [None, "CurrentYearDuration"],
                    ["Dasar penyusunan laporan keuangan", policy_text, "Basis of preparation of financial statements"],
                ],
            ),
            ("hidden", "hidden", [["Entitas tunggal / Single entity", "Ya / Yes"]]),
        ]
    )


def _insert_downloaded_workbook(
    tmp_config,
    *,
    source_record_id: str,
    payload: bytes,
    raw_metadata: dict | None = None,
    title: str = "FinancialStatement-2025-Tahunan-ABCD.xlsx",
    ticker: str = "ABCD",
) -> str:
    stored = FileStore(tmp_config.paths.files_dir).put_bytes(payload)
    now = datetime.now(timezone.utc).isoformat()
    document_id = f"doc-{source_record_id}"
    metadata = raw_metadata if raw_metadata is not None else {"report_year": "2025", "report_period": "Audit"}

    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)

        def _insert(conn):
            conn.execute(
                """
                INSERT INTO document_candidates (
                    candidate_id, source_name, source_record_id, ticker,
                    title, attachment_url, raw_metadata, discovered_at, queued
                ) VALUES (?, 'idx_financial_reports', ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    f"idx_financial_reports:{source_record_id}",
                    source_record_id,
                    ticker,
                    title,
                    f"https://example.test/{title}",
                    json.dumps(metadata, sort_keys=True),
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO documents (
                    document_id, source_name, source_record_id, source_file_id,
                    retrieved_at, original_url, local_path, sha256, mime_type,
                    document_type, language_variant, process_state, ticker,
                    created_at, updated_at
                ) VALUES (?, 'idx_financial_reports', ?, ?, ?, ?, ?, ?, ?, 'financial_statement',
                          'unknown', 'downloaded', ?, ?, ?)
                """,
                (
                    document_id,
                    source_record_id,
                    source_record_id,
                    now,
                    f"https://example.test/{title}",
                    str(stored.local_path),
                    stored.sha256,
                    _XLSX_MIME,
                    ticker,
                    now,
                    now,
                ),
            )

        wq.submit(_insert)
    return document_id


def _document_row(tmp_config, document_id: str):
    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        return conn.execute("SELECT * FROM documents WHERE document_id = ?", (document_id,)).fetchone()
    finally:
        conn.close()


def _metrics(tmp_config, document_id: str) -> dict[str, dict]:
    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        rows = conn.execute(
            """
            SELECT mi.*, es.excerpt_text, es.char_start AS span_start, es.char_end AS span_end,
                   dc.text AS chunk_text, dc.char_start AS chunk_start, dc.section_label
            FROM metric_items mi
            JOIN evidence_spans es ON es.span_id = mi.evidence_span_id
            JOIN document_chunks dc ON dc.chunk_id = es.chunk_id
            WHERE mi.document_id = ?
            """,
            (document_id,),
        ).fetchall()
    finally:
        conn.close()
    return {row["metric_name"]: dict(row) for row in rows}


def test_run_parse_renders_visible_workbook_sheets_as_row_chunks(tmp_config):
    long_policy = " ".join(f"kalimat{index}" for index in range(600))
    document_id = _insert_downloaded_workbook(
        tmp_config, source_record_id="wb-parse", payload=_statement_workbook(policy_text=long_policy)
    )

    result = run_parse(tmp_config, source_name="idx_financial_reports", limit=10)

    assert (result.parsed, result.failed) == (1, 0)
    document = _document_row(tmp_config, document_id)
    assert document["process_state"] == "parsed"
    assert document["parse_strategy"] == "xlsx"
    assert document["parser_version"] == "phase4-xlsx-v1"
    # Context, the hidden template alternative and 'hidden' are not rendered.
    assert document["page_count"] == 5

    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        chunks = conn.execute(
            "SELECT page_start, section_label, char_start, char_end, text FROM document_chunks WHERE document_id = ? ORDER BY ord",
            (document_id,),
        ).fetchall()
    finally:
        conn.close()
    joined = "\n".join(chunk["text"] for chunk in chunks)
    assert "Jumlah aset\t5000.5\t4000\tTotal assets" in joined
    assert "Jumlah liabilitas\t5500.25\t4300\tTotal liabilities" in joined
    assert "999999" not in joined
    # Captions without figures (abstract headings) are dropped below the context row.
    assert "Aset\t\t\tAssets" not in joined
    balance_sheet = [chunk for chunk in chunks if chunk["page_start"] == 2]
    assert {chunk["section_label"] for chunk in balance_sheet} == {
        "[1210000] Statement of financial position presented using current and non-current - General Industry"
    }
    assert all(len(chunk["text"]) == chunk["char_end"] - chunk["char_start"] for chunk in chunks)
    # A note text longer than a chunk is split at spaces, like page text.
    policy_chunks = [chunk["text"] for chunk in chunks if chunk["page_start"] == 5]
    assert len(policy_chunks) > 2
    assert max(len(text) for text in policy_chunks) <= CHUNK_TARGET_CHARS
    assert "kalimat599" in policy_chunks[-1]


def test_run_extract_reads_workbook_figures_in_full_units(tmp_config):
    document_id = _insert_downloaded_workbook(
        tmp_config,
        source_record_id="wb-extract",
        payload=_statement_workbook(),
        raw_metadata={"report_year": "2025", "report_period": "Audit"},
    )
    run_parse(tmp_config, source_name="idx_financial_reports", limit=10)

    result = run_extract(tmp_config, source_name="idx_financial_reports", limit=10)

    assert (result.partial, result.failed, result.events) == (1, 0, 0)
    document = _document_row(tmp_config, document_id)
    assert document["process_state"] == "extracted"
    # General-industry templates have no operating subtotal; nothing else is missing.
    assert document["process_error_code"] == "missing_metrics:operating_profit"

    metrics = _metrics(tmp_config, document_id)
    values = {name: metric["value_normalized"] for name, metric in metrics.items()}
    assert values == {
        "total_assets": 5_000_500_000.0,
        "total_liabilities": 5_500_250_000.0,
        # Total equity, not the parent-attributable subtotal above it.
        "equity": -499_750_000.0,
        "cash": 1_200_000_000.0,
        "inventory": 300_000_000.0,
        # The hidden by-nature alternative (999999) is never read.
        "revenue": 2_000_000_000.0,
        "gross_profit": 500_000_000.0,
        "interest_expense": 40_000_000.0,
        "net_profit": -120_000_000.0,
        "operating_cash_flow": 150_000_000.0,
        "capex": 60_000_000.0,
        "free_cash_flow": 90_000_000.0,
    }
    assert values["total_assets"] == values["total_liabilities"] + values["equity"]

    total_assets = metrics["total_assets"]
    assert total_assets["raw_value_text"] == "5000.5"
    assert (total_assets["currency"], total_assets["unit_scale"]) == ("USD", "million")
    assert (total_assets["fiscal_year"], total_assets["fiscal_period"], total_assets["period_end_date"]) == (
        2025,
        "Annual",
        "2025-12-31",
    )
    assert (total_assets["audit_status"], total_assets["consolidation_scope"]) == ("audited", "consolidated")
    assert total_assets["language_variant"] == "bilingual"
    assert total_assets["excerpt_text"] == "Jumlah aset\t5000.5\t4000\tTotal assets"
    assert total_assets["section_label"].startswith("[1210000] Statement of financial position")
    for name, metric in metrics.items():
        if name == "free_cash_flow":
            continue
        offset = metric["span_start"] - metric["chunk_start"]
        assert metric["chunk_text"][offset : offset + len(metric["raw_value_text"])] == metric["raw_value_text"], name
        assert metric["span_end"] - metric["span_start"] == len(metric["raw_value_text"]), name


def test_run_extract_dates_workbook_periods_from_the_general_information_sheet(tmp_config):
    document_id = _insert_downloaded_workbook(
        tmp_config,
        source_record_id="wb-h1",
        payload=_statement_workbook(period_end="2026-06-30", rounding="Ribuan / In Thousand", currency="Rupiah / IDR"),
        raw_metadata={"report_year": "2026", "requested_period": "tw2", "report_period": "TW2"},
        title="FinancialStatement-2026-II-ABCD.xlsx",
    )
    run_parse(tmp_config, source_name="idx_financial_reports", limit=10)
    run_extract(tmp_config, source_name="idx_financial_reports", limit=10)

    revenue = _metrics(tmp_config, document_id)["revenue"]
    assert (revenue["fiscal_year"], revenue["fiscal_period"], revenue["period_end_date"]) == (2026, "H1", "2026-06-30")
    assert (revenue["value_normalized"], revenue["currency"], revenue["unit_scale"]) == (2_000_000.0, "IDR", "thousand")


@pytest.mark.parametrize(
    ("payload", "stage", "error_code"),
    [
        (b"not a workbook", "parse", "xlsx_open_failed"),
        (_statement_workbook(rounding=None), "extract", "workbook_unknown_rounding"),
    ],
)
def test_unreadable_workbooks_fail_with_a_specific_code(tmp_config, payload, stage, error_code):
    document_id = _insert_downloaded_workbook(tmp_config, source_record_id=f"wb-{stage}", payload=payload)

    parsed = run_parse(tmp_config, source_name="idx_financial_reports", limit=10)
    extracted = run_extract(tmp_config, source_name="idx_financial_reports", limit=10)

    assert (parsed.failed, extracted.failed) == ((1, 0) if stage == "parse" else (0, 1))
    document = _document_row(tmp_config, document_id)
    assert (document["process_state"], document["process_error_code"]) == ("failed", error_code)
    assert _metrics(tmp_config, document_id) == {}


def _extract_workbook_and_template_pdf(tmp_config) -> tuple[str, str]:
    pdf_id = _insert_parsed_document(
        tmp_config,
        source_name="idx_financial_reports",
        source_record_id="pdf-fy25",
        document_type="financial_statement",
        ticker="ABCD",
        raw_metadata={"report_year": "2025", "report_period": "Audit"},
        title="FinancialStatement-2025-Tahunan-ABCD.pdf",
        # A table-of-contents page number read as the total, plus a figure the workbook lacks.
        chunks=[(0, 1, None, "Total Assets 97\nShort-term debt 5")],
    )
    workbook_id = _insert_downloaded_workbook(tmp_config, source_record_id="wb-fy25", payload=_statement_workbook())
    run_parse(tmp_config, source_name="idx_financial_reports", limit=10)
    run_extract(tmp_config, source_name="idx_financial_reports", limit=10)
    return pdf_id, workbook_id


def test_workbook_metrics_supersede_text_metrics_for_the_same_period(tmp_config):
    pdf_id, workbook_id = _extract_workbook_and_template_pdf(tmp_config)

    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        ticker_metrics = _load_ticker_metrics(conn, "ABCD")
        pdf_metrics = _load_document_metrics(conn, pdf_id)
        dashboard_metrics = {row["metric_name"]: row for row in _load_metrics_for_ticker(conn, ticker="ABCD")}
        stored_pdf_assets = conn.execute(
            "SELECT value_normalized FROM metric_items WHERE document_id = ? AND metric_name = 'total_assets'",
            (pdf_id,),
        ).fetchone()
    finally:
        conn.close()

    assets = [metric for metric in ticker_metrics if metric.metric_name == "total_assets"]
    assert [(metric.document_id, metric.value_normalized) for metric in assets] == [(workbook_id, 5_000_500_000.0)]
    # Figures only the text reports are kept, and the text row itself is not rewritten.
    assert {metric.metric_name for metric in pdf_metrics} == {"short_term_debt"}
    assert any(metric.metric_name == "short_term_debt" and metric.document_id == pdf_id for metric in ticker_metrics)
    assert stored_pdf_assets["value_normalized"] == 97.0
    assert dashboard_metrics["total_assets"]["document_id"] == workbook_id
    assert dashboard_metrics["total_assets"]["value_normalized"] == 5_000_500_000.0


def test_filing_anchor_moves_to_its_workbook_sibling(tmp_config):
    pdf_id, workbook_id = _extract_workbook_and_template_pdf(tmp_config)

    def document(document_id: str, as_of: str, parse_strategy: str | None) -> CompanyDocument:
        return CompanyDocument(
            document_id=document_id,
            source_name="idx_financial_reports",
            source_record_id=document_id,
            ticker="ABCD",
            title=None,
            document_type="financial_statement",
            version_group_id=None,
            source_modified_at=as_of,
            updated_at=as_of,
            created_at=as_of,
            parse_strategy=parse_strategy,
        )

    filing = "2026-03-30T10:00:00"
    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        siblings = [document(pdf_id, filing, "native"), document(workbook_id, filing, "xlsx")]
        assert _prefer_workbook_anchor(conn, siblings, 0) == 1
        # A workbook from another (older) filing does not take over the anchor.
        separate = [document(pdf_id, filing, "native"), document(workbook_id, "2025-03-30T10:00:00", "xlsx")]
        assert _prefer_workbook_anchor(conn, separate, 0) == 0
    finally:
        conn.close()


def test_workbook_anchor_folds_in_the_rest_of_its_filing(tmp_config):
    pdf_id, workbook_id = _extract_workbook_and_template_pdf(tmp_config)
    filing = "2026-03-30T10:00:00"

    def _seed(conn):
        conn.execute(
            "INSERT INTO companies (ticker, issuer_name, first_seen_at, last_seen_at) VALUES ('ABCD', 'Issuer ABCD', ?, ?)",
            (filing, filing),
        )
        # Two files of one filing; the text PDF sorts behind the workbook.
        conn.execute(
            "UPDATE documents SET process_state = 'analyzed', source_modified_at = ? WHERE document_id IN (?, ?)",
            (filing, pdf_id, workbook_id),
        )
        conn.execute(
            """
            INSERT INTO events (
                event_id, document_id, ticker, event_type, matched_pattern, severity, rationale, detected_at
            ) VALUES ('event-going-concern', ?, 'ABCD', 'going_concern_warning', 'going concern', 'critical',
                      'Auditor flags material uncertainty.', ?)
            """,
            (pdf_id, filing),
        )

    with WriterQueue(tmp_config) as wq:
        wq.submit(_seed)

    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        bundle = _build_bundle(conn, tmp_config, "ABCD", as_of=date(2026, 4, 1))
    finally:
        conn.close()

    assert bundle is not None
    assert bundle.latest_document.document_id == workbook_id
    # The workbook carries no events; the filing's PDF does.
    assert "event-going-concern" in {event.event_id for event in bundle.events}
    # A sibling file is part of the latest filing, not a baseline to compare against.
    assert bundle.baseline is None
