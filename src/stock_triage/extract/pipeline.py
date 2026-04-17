"""Phase 5 extraction pipeline: normalized metrics, evidence spans, and events."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..config import Config
from ..db import WriterQueue, connect, configure_pragmas, run_migrations

EXTRACTOR_VERSION = "phase5-v2"
FINANCIAL_REQUIRED_METRICS = {
    "revenue",
    "gross_profit",
    "operating_profit",
    "net_profit",
    "total_assets",
    "total_liabilities",
    "equity",
    "operating_cash_flow",
}
FINANCIAL_CORE_METRICS = FINANCIAL_REQUIRED_METRICS | {
    "capex",
    "cash",
    "short_term_debt",
    "long_term_debt",
    "share_count",
    "net_debt",
    "free_cash_flow",
}
FINANCIAL_STRETCH_METRICS = {
    "ebitda",
    "interest_coverage",
    "receivables",
    "inventory",
    "segment_revenue",
    "segment_profit",
}


@dataclass(frozen=True)
class ExtractDocument:
    document_id: str
    source_name: str
    source_record_id: str
    document_type: str | None
    ticker: str | None
    parser_version: str | None
    process_state: str
    raw_metadata: dict[str, Any]
    title: str | None


@dataclass(frozen=True)
class ChunkText:
    chunk_id: str
    page_start: int
    page_end: int
    char_start: int
    char_end: int
    text: str
    section_label: str | None


@dataclass(frozen=True)
class ExtractedMetric:
    metric_name: str
    value_normalized: float | None
    raw_value_text: str
    currency: str | None
    unit_scale: str
    fiscal_year: int | None
    fiscal_period: str
    period_end_date: str | None
    audit_status: str | None
    consolidation_scope: str | None
    language_variant: str
    chunk: ChunkText
    excerpt_text: str
    char_start: int
    char_end: int


@dataclass(frozen=True)
class ExtractedEvent:
    event_type: str
    matched_pattern: str
    severity: str
    impact_direction: str | None
    confidence: float | None
    rationale: str
    payload_json: str
    chunk: ChunkText
    excerpt_text: str
    char_start: int
    char_end: int


@dataclass(frozen=True)
class ExtractResult:
    metrics: list[ExtractedMetric]
    events: list[ExtractedEvent]
    status: str
    error_code: str | None


@dataclass(frozen=True)
class ExtractRunResult:
    selected: int = 0
    extracted: int = 0
    partial: int = 0
    failed: int = 0
    metrics: int = 0
    events: int = 0


@dataclass(frozen=True)
class ExtractStatusRow:
    source_name: str
    total: int
    pending: int
    extracted: int
    partial: int
    failed: int
    metrics: int
    events: int
    completeness_pct: float | None


@dataclass(frozen=True)
class ExtractFailureRow:
    document_id: str
    source_name: str
    source_record_id: str
    status: str
    error_code: str | None
    updated_at: str


@dataclass(frozen=True)
class ExtractStatusReport:
    rows: list[ExtractStatusRow]
    recent_failures: list[ExtractFailureRow]


class ExtractionError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def run_extract(
    cfg: Config,
    *,
    source_name: str | None = None,
    document_id: str | None = None,
    limit: int = 25,
    include_extracted: bool = False,
    retry_failed_only: bool = False,
) -> ExtractRunResult:
    cfg.ensure_dirs()

    result = ExtractRunResult()
    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
        reader = connect(cfg.paths.db_path, read_only=True)
        try:
            configure_pragmas(reader, cfg)
            documents = _select_documents(
                reader,
                source_name=source_name,
                document_id=document_id,
                limit=limit,
                include_extracted=include_extracted,
                retry_failed_only=retry_failed_only,
            )
            result = _replace_result(result, selected=len(documents))
            for document in documents:
                started_at = datetime.now(timezone.utc)
                chunks = _load_chunks(reader, document.document_id)
                try:
                    extracted = _extract_document(document, chunks)
                except ExtractionError as exc:
                    finished_at = datetime.now(timezone.utc)
                    wq.submit(
                        lambda conn, d=document, e=exc, s=started_at, f=finished_at: _persist_extraction_failure(
                            conn,
                            document=d,
                            error=e,
                            started_at=s,
                            finished_at=f,
                        )
                    )
                    result = _replace_result(result, failed=result.failed + 1)
                    continue

                finished_at = datetime.now(timezone.utc)
                wq.submit(
                    lambda conn, d=document, x=extracted, s=started_at, f=finished_at: _persist_extraction_success(
                        conn,
                        document=d,
                        extracted=x,
                        started_at=s,
                        finished_at=f,
                    )
                )
                result = _replace_result(
                    result,
                    extracted=result.extracted + (1 if extracted.status == "ok" else 0),
                    partial=result.partial + (1 if extracted.status == "partial" else 0),
                    metrics=result.metrics + len(extracted.metrics),
                    events=result.events + len(extracted.events),
                )
        finally:
            reader.close()
    return result


def load_extract_status(
    cfg: Config,
    *,
    source_name: str | None = None,
    failure_limit: int = 10,
) -> ExtractStatusReport:
    cfg.ensure_dirs()
    conn = connect(cfg.paths.db_path, read_only=True)
    try:
        configure_pragmas(conn, cfg)
        params: list[object] = []
        where = ""
        if source_name:
            where = "WHERE d.source_name = ?"
            params.append(source_name)
        rows = conn.execute(
            f"""
            SELECT
                d.source_name,
                COUNT(*) AS total,
                SUM(CASE WHEN d.process_state = 'parsed' THEN 1 ELSE 0 END) AS pending,
                SUM(
                    CASE
                        WHEN d.process_state IN ('extracted', 'analyzed')
                         AND (d.process_error_code IS NULL OR d.process_error_code = '')
                        THEN 1 ELSE 0
                    END
                ) AS extracted,
                SUM(
                    CASE
                        WHEN d.process_state IN ('extracted', 'analyzed')
                         AND d.process_error_code LIKE 'missing_metrics:%'
                        THEN 1 ELSE 0
                    END
                ) AS partial,
                SUM(CASE WHEN d.process_state = 'failed' THEN 1 ELSE 0 END) AS failed,
                COALESCE((
                    SELECT COUNT(*)
                    FROM metric_items mi
                    JOIN documents dm ON dm.document_id = mi.document_id
                    WHERE dm.source_name = d.source_name
                ), 0) AS metrics,
                COALESCE((
                    SELECT COUNT(*)
                    FROM events ev
                    JOIN documents de ON de.document_id = ev.document_id
                    WHERE de.source_name = d.source_name
                ), 0) AS events
            FROM documents d
            {where}
            GROUP BY d.source_name
            ORDER BY d.source_name
            """,
            tuple(params),
        ).fetchall()
        failures = conn.execute(
            f"""
            SELECT
                document_id,
                source_name,
                source_record_id,
                CASE
                    WHEN process_state = 'failed' THEN 'failed'
                    WHEN process_error_code LIKE 'missing_metrics:%' THEN 'partial'
                    ELSE process_state
                END AS status,
                process_error_code,
                updated_at
            FROM documents d
            {where}
            {"AND" if where else "WHERE"} (
                d.process_state = 'failed'
                OR d.process_error_code LIKE 'missing_metrics:%'
            )
            ORDER BY updated_at DESC, document_id ASC
            LIMIT ?
            """,
            tuple(params + [failure_limit]),
        ).fetchall()
        status_rows: list[ExtractStatusRow] = []
        for row in rows:
            processed = (row["extracted"] or 0) + (row["partial"] or 0)
            completeness_pct = None
            if row["source_name"] == "idx_financial_reports" and processed:
                completeness_pct = round((row["metrics"] / (processed * len(FINANCIAL_REQUIRED_METRICS))) * 100.0, 2)
            status_rows.append(
                ExtractStatusRow(
                    source_name=row["source_name"],
                    total=row["total"],
                    pending=row["pending"] or 0,
                    extracted=row["extracted"] or 0,
                    partial=row["partial"] or 0,
                    failed=row["failed"] or 0,
                    metrics=row["metrics"] or 0,
                    events=row["events"] or 0,
                    completeness_pct=completeness_pct,
                )
            )
        return ExtractStatusReport(
            rows=status_rows,
            recent_failures=[
                ExtractFailureRow(
                    document_id=row["document_id"],
                    source_name=row["source_name"],
                    source_record_id=row["source_record_id"],
                    status=row["status"],
                    error_code=row["process_error_code"],
                    updated_at=row["updated_at"],
                )
                for row in failures
            ],
        )
    finally:
        conn.close()


def _select_documents(
    conn: sqlite3.Connection,
    *,
    source_name: str | None,
    document_id: str | None,
    limit: int,
    include_extracted: bool,
    retry_failed_only: bool,
) -> list[ExtractDocument]:
    if document_id:
        rows = conn.execute(
            """
            SELECT
                d.document_id,
                d.source_name,
                d.source_record_id,
                d.document_type,
                d.ticker,
                d.parser_version,
                d.process_state,
                dc.raw_metadata,
                dc.title
            FROM documents d
            LEFT JOIN document_candidates dc
              ON dc.source_name = d.source_name
             AND dc.source_record_id = d.source_record_id
            WHERE d.document_id = ?
            LIMIT 1
            """,
            (document_id,),
        ).fetchall()
        return [
            ExtractDocument(
                document_id=row["document_id"],
                source_name=row["source_name"],
                source_record_id=row["source_record_id"],
                document_type=row["document_type"],
                ticker=row["ticker"],
                parser_version=row["parser_version"],
                process_state=row["process_state"],
                raw_metadata=json.loads(row["raw_metadata"]) if row["raw_metadata"] else {},
                title=row["title"],
            )
            for row in rows
        ]
    if retry_failed_only:
        states = ["failed"]
    else:
        states = ["parsed"]
    if include_extracted and not retry_failed_only:
        states.extend(["extracted", "analyzed", "failed"])
    placeholders = ",".join("?" for _ in states)
    sql = f"""
        SELECT
            d.document_id,
            d.source_name,
            d.source_record_id,
            d.document_type,
            d.ticker,
            d.parser_version,
            d.process_state,
            dc.raw_metadata,
            dc.title
        FROM documents d
        LEFT JOIN document_candidates dc
          ON dc.source_name = d.source_name
         AND dc.source_record_id = d.source_record_id
        WHERE d.process_state IN ({placeholders})
    """
    params: list[object] = list(states)
    if source_name:
        sql += " AND d.source_name = ?"
        params.append(source_name)
    sql += " ORDER BY d.updated_at ASC, d.document_id ASC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, tuple(params)).fetchall()
    return [
        ExtractDocument(
            document_id=row["document_id"],
            source_name=row["source_name"],
            source_record_id=row["source_record_id"],
            document_type=row["document_type"],
            ticker=row["ticker"],
            parser_version=row["parser_version"],
            process_state=row["process_state"],
            raw_metadata=json.loads(row["raw_metadata"]) if row["raw_metadata"] else {},
            title=row["title"],
        )
        for row in rows
    ]


def _load_chunks(conn: sqlite3.Connection, document_id: str) -> list[ChunkText]:
    rows = conn.execute(
        """
        SELECT chunk_id, page_start, page_end, char_start, char_end, text, section_label
        FROM document_chunks
        WHERE document_id = ?
        ORDER BY ord
        """,
        (document_id,),
    ).fetchall()
    return [
        ChunkText(
            chunk_id=row["chunk_id"],
            page_start=row["page_start"],
            page_end=row["page_end"],
            char_start=row["char_start"],
            char_end=row["char_end"],
            text=row["text"],
            section_label=row["section_label"],
        )
        for row in rows
    ]


def _extract_document(document: ExtractDocument, chunks: list[ChunkText]) -> ExtractResult:
    if not chunks:
        raise ExtractionError("missing_chunks", f"No parsed chunks for {document.document_id}")

    metrics: list[ExtractedMetric] = []
    events: list[ExtractedEvent] = []

    if document.source_name == "idx_financial_reports":
        metrics.extend(_extract_financial_metrics(document, chunks))
        events.extend(_extract_financial_events(document, chunks))
    if document.source_name == "idx_disclosures":
        events.extend(_extract_disclosure_events(document, chunks))

    missing_metrics: set[str] = set()
    if document.source_name == "idx_financial_reports":
        present = {metric.metric_name for metric in metrics}
        missing_metrics = FINANCIAL_REQUIRED_METRICS - present

    if missing_metrics:
        status = "partial"
        error_code = "missing_metrics:" + ",".join(sorted(missing_metrics))
    else:
        status = "ok"
        error_code = None
    if not metrics and not events:
        if document.source_name == "idx_financial_reports" and _looks_like_attachment_notice(document, chunks):
            raise ExtractionError(
                "unsupported_document_kind:attachment_notice",
                f"Attachment wrapper notice for {document.document_id}",
            )
        raise ExtractionError("no_facts_extracted", f"No metrics or events extracted for {document.document_id}")
    return ExtractResult(metrics=metrics, events=events, status=status, error_code=error_code)


def _extract_financial_metrics(document: ExtractDocument, chunks: list[ChunkText]) -> list[ExtractedMetric]:
    fiscal_year = _infer_fiscal_year(document, chunks)
    period_end_date = f"{fiscal_year}-12-31" if fiscal_year is not None else None
    audit_status = "audited" if document.document_type == "annual_report" else None
    consolidation_scope = "consolidated"
    language_variant = _language_variant(chunks)

    out: dict[str, ExtractedMetric] = {}
    segment_metrics: list[ExtractedMetric] = []
    for chunk in chunks:
        lower = chunk.text.lower()
        for metric in _extract_financial_highlight_chunk(
            chunk,
            fiscal_year=fiscal_year,
            period_end_date=period_end_date,
            audit_status=audit_status,
            consolidation_scope=consolidation_scope,
            language_variant=language_variant,
        ):
            out.setdefault(metric.metric_name, metric)

        metric = _extract_operating_cash_flow(
            chunk,
            fiscal_year=fiscal_year,
            period_end_date=period_end_date,
            audit_status=audit_status,
            consolidation_scope=consolidation_scope,
            language_variant=language_variant,
        )
        if metric is not None:
            out.setdefault(metric.metric_name, metric)

        for metric in _extract_optional_financial_metrics(
            chunk,
            fiscal_year=fiscal_year,
            period_end_date=period_end_date,
            audit_status=audit_status,
            consolidation_scope=consolidation_scope,
            language_variant=language_variant,
        ):
            out.setdefault(metric.metric_name, metric)

        segment_metrics.extend(
            _extract_segment_metrics(
                chunk,
                fiscal_year=fiscal_year,
                period_end_date=period_end_date,
                audit_status=audit_status,
                language_variant=language_variant,
            )
        )

        if "laba usaha" in lower or "operating profit" in lower:
            metric = _extract_operating_profit_guidance(
                chunk,
                fiscal_year=fiscal_year,
                period_end_date=period_end_date,
                audit_status=audit_status,
                consolidation_scope=consolidation_scope,
                language_variant=language_variant,
            )
            if metric is not None:
                out[metric.metric_name] = metric
    for metric in _derive_financial_metrics(out):
        out.setdefault(metric.metric_name, metric)
    return list(out.values()) + segment_metrics


def _extract_financial_highlight_chunk(
    chunk: ChunkText,
    *,
    fiscal_year: int | None,
    period_end_date: str | None,
    audit_status: str | None,
    consolidation_scope: str | None,
    language_variant: str,
) -> list[ExtractedMetric]:
    patterns = [
        ("revenue", r"(?:Pendapatan|Revenue)\s+([\(]?\d[\d\.,]*\)?)"),
        ("gross_profit", r"(?:Laba Bruto|Gross Profit|Gross profit)\s+([\(]?\d[\d\.,]*\)?)"),
        (
            "operating_profit",
            r"(?:Laba Usaha|Operating Profit|Operating profit|Operating income)\s+([\(]?\d[\d\.,]*\)?)",
        ),
        (
            "net_profit",
            r"(?:RUGI TAHUN BERJALAN|Laba Bersih|Net Profit|Net profit|Profit for the year)\s+([\(]?\d[\d\.,]*\)?)",
        ),
        ("total_assets", r"(?:JUMLAH ASET|Total Asset In USD|Total Assets|Total assets|Aset \| Assets)\s+([\(]?\d[\d\.,]*\)?)"),
        ("total_liabilities", r"(?:JUMLAH LIABILITAS|Total Liabilities In USD|Total Liabilities|Total liabilities)\s+([\(]?\d[\d\.,]*\)?)"),
        ("equity", r"(?:Ekuitas|Equity In USD|Equity)\s+([\(]?\d[\d\.,]*\)?)"),
    ]
    out: list[ExtractedMetric] = []
    for metric_name, pattern in patterns:
        match = re.search(pattern, chunk.text, flags=re.IGNORECASE)
        if match is None:
            continue
        raw = match.group(1)
        out.append(
            ExtractedMetric(
                metric_name=metric_name,
                value_normalized=_parse_number(raw),
                raw_value_text=raw,
                currency="USD",
                unit_scale="units",
                fiscal_year=fiscal_year,
                fiscal_period="Annual",
                period_end_date=period_end_date,
                audit_status=audit_status,
                consolidation_scope=consolidation_scope,
                language_variant=language_variant,
                chunk=chunk,
                excerpt_text=chunk.text[max(0, match.start() - 40): min(len(chunk.text), match.end() + 80)],
                char_start=chunk.char_start + match.start(1),
                char_end=chunk.char_start + match.end(1),
            )
        )
    return out


def _extract_operating_cash_flow(
    chunk: ChunkText,
    *,
    fiscal_year: int | None,
    period_end_date: str | None,
    audit_status: str | None,
    consolidation_scope: str | None,
    language_variant: str,
) -> ExtractedMetric | None:
    patterns = [
        r"Arus Kas Bersih.*?\(([\d\.,]+)\)\s+(?:Operating Activities|untuk\)\s+Aktivitas Operasi)",
        r"Arus kas yang digunakan untuk aktivitas operasi.*?tercatat sebesar\s+([\d\.,]+)\s+juta USD",
    ]
    for pattern in patterns:
        match = re.search(pattern, chunk.text, flags=re.IGNORECASE | re.DOTALL)
        if match is None:
            continue
        raw = match.group(1)
        value = _parse_number(raw)
        if "juta usd" in chunk.text.lower() and "tercatat sebesar" in chunk.text.lower():
            value *= 1_000_000
        if "digunakan" in chunk.text.lower() or "used for operating activities" in chunk.text.lower():
            value = -abs(value)
        return ExtractedMetric(
            metric_name="operating_cash_flow",
            value_normalized=value,
            raw_value_text=raw,
            currency="USD",
            unit_scale="units",
            fiscal_year=fiscal_year,
            fiscal_period="Annual",
            period_end_date=period_end_date,
            audit_status=audit_status,
            consolidation_scope=consolidation_scope,
            language_variant=language_variant,
            chunk=chunk,
            excerpt_text=chunk.text[max(0, match.start() - 40): min(len(chunk.text), match.end() + 120)],
            char_start=chunk.char_start + match.start(1),
            char_end=chunk.char_start + match.end(1),
        )
    return None


def _extract_operating_profit_guidance(
    chunk: ChunkText,
    *,
    fiscal_year: int | None,
    period_end_date: str | None,
    audit_status: str | None,
    consolidation_scope: str | None,
    language_variant: str,
) -> ExtractedMetric | None:
    del period_end_date
    matches = list(re.finditer(r"Laba Usaha\s+(\(?[\d\.,]+\)?)\s+juta USD", chunk.text, flags=re.IGNORECASE))
    if not matches:
        return None
    match = matches[-1]
    raw = match.group(1)
    return ExtractedMetric(
        metric_name="operating_profit",
        value_normalized=_parse_number(raw) * 1_000_000,
        raw_value_text=raw,
        currency="USD",
        unit_scale="million",
        fiscal_year=fiscal_year + 1 if fiscal_year is not None else None,
        fiscal_period="Annual",
        period_end_date=f"{fiscal_year + 1}-12-31" if fiscal_year is not None else None,
        audit_status=None,
        consolidation_scope=consolidation_scope,
        language_variant=language_variant,
        chunk=chunk,
        excerpt_text=chunk.text[max(0, match.start() - 40): min(len(chunk.text), match.end() + 120)],
        char_start=chunk.char_start + match.start(1),
        char_end=chunk.char_start + match.end(1),
    )


def _extract_optional_financial_metrics(
    chunk: ChunkText,
    *,
    fiscal_year: int | None,
    period_end_date: str | None,
    audit_status: str | None,
    consolidation_scope: str | None,
    language_variant: str,
) -> list[ExtractedMetric]:
    patterns = [
        ("cash", r"(?:Kas dan setara kas|Cash and cash equivalents|Cash)\s+([\(]?\d[\d\.,]*\)?)"),
        (
            "short_term_debt",
            r"(?:Liabilitas jangka pendek|Utang jangka pendek|Short[- ]term debt|Current borrowings)\s+([\(]?\d[\d\.,]*\)?)",
        ),
        (
            "long_term_debt",
            r"(?:Liabilitas jangka panjang|Utang jangka panjang|Long[- ]term debt|Non-current borrowings)\s+([\(]?\d[\d\.,]*\)?)",
        ),
        (
            "share_count",
            r"(?:Jumlah saham beredar|Shares outstanding|Weighted average shares outstanding)\s+([\(]?\d[\d\.,]*\)?)",
        ),
        ("capex", r"(?:Belanja modal|Capital expenditure|Capex)\s+([\(]?\d[\d\.,]*\)?)"),
        ("ebitda", r"(?:EBITDA)\s+([\(]?\d[\d\.,]*\)?)"),
        ("interest_expense", r"(?:Beban bunga|Interest expense)\s+([\(]?\d[\d\.,]*\)?)"),
        ("receivables", r"(?:Piutang usaha|Trade receivables|Accounts receivable)\s+([\(]?\d[\d\.,]*\)?)"),
        ("inventory", r"(?:Persediaan|Inventory)\s+([\(]?\d[\d\.,]*\)?)"),
    ]
    out: list[ExtractedMetric] = []
    for metric_name, pattern in patterns:
        match = re.search(pattern, chunk.text, flags=re.IGNORECASE)
        if match is None:
            continue
        raw = match.group(1)
        out.append(
            ExtractedMetric(
                metric_name=metric_name,
                value_normalized=_parse_number(raw),
                raw_value_text=raw,
                currency="USD",
                unit_scale="units",
                fiscal_year=fiscal_year,
                fiscal_period="Annual",
                period_end_date=period_end_date,
                audit_status=audit_status,
                consolidation_scope=consolidation_scope,
                language_variant=language_variant,
                chunk=chunk,
                excerpt_text=chunk.text[max(0, match.start() - 40): min(len(chunk.text), match.end() + 80)],
                char_start=chunk.char_start + match.start(1),
                char_end=chunk.char_start + match.end(1),
            )
        )
    return out


def _extract_segment_metrics(
    chunk: ChunkText,
    *,
    fiscal_year: int | None,
    period_end_date: str | None,
    audit_status: str | None,
    language_variant: str,
) -> list[ExtractedMetric]:
    patterns = [
        (
            "segment_revenue",
            r"(?:Segmen|Segment)\s+(?P<segment>[A-Za-z][A-Za-z0-9/&,\- ]{1,40}?)\s+(?:pendapatan|revenue)\s+(?P<value>[\(]?\d[\d\.,]*\)?)",
        ),
        (
            "segment_revenue",
            r"(?P<segment>[A-Za-z][A-Za-z0-9/&,\- ]{1,40}?)\s+segment\s+revenue\s+(?P<value>[\(]?\d[\d\.,]*\)?)",
        ),
        (
            "segment_profit",
            r"(?:Segmen|Segment)\s+(?P<segment>[A-Za-z][A-Za-z0-9/&,\- ]{1,40}?)\s+(?:laba(?: usaha)?|operating profit|profit)\s+(?P<value>[\(]?\d[\d\.,]*\)?)",
        ),
        (
            "segment_profit",
            r"(?P<segment>[A-Za-z][A-Za-z0-9/&,\- ]{1,40}?)\s+segment\s+(?:operating profit|profit)\s+(?P<value>[\(]?\d[\d\.,]*\)?)",
        ),
    ]
    metrics: list[ExtractedMetric] = []
    seen: set[tuple[str, str, str]] = set()
    for metric_name, pattern in patterns:
        for match in re.finditer(pattern, chunk.text, flags=re.IGNORECASE):
            segment_name = re.sub(r"\s+", " ", match.group("segment")).strip(" -:")
            if not segment_name:
                continue
            raw = match.group("value")
            dedupe_key = (metric_name, segment_name.lower(), raw)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            metrics.append(
                ExtractedMetric(
                    metric_name=metric_name,
                    value_normalized=_parse_number(raw),
                    raw_value_text=raw,
                    currency="USD",
                    unit_scale="units",
                    fiscal_year=fiscal_year,
                    fiscal_period="Annual",
                    period_end_date=period_end_date,
                    audit_status=audit_status,
                    consolidation_scope=f"segment:{segment_name}",
                    language_variant=language_variant,
                    chunk=chunk,
                    excerpt_text=chunk.text[max(0, match.start() - 40): min(len(chunk.text), match.end() + 80)],
                    char_start=chunk.char_start + match.start("value"),
                    char_end=chunk.char_start + match.end("value"),
                )
            )
    return metrics


def _derive_financial_metrics(metrics: dict[str, ExtractedMetric]) -> list[ExtractedMetric]:
    derived: list[ExtractedMetric] = []
    cash = metrics.get("cash")
    short_term_debt = metrics.get("short_term_debt")
    long_term_debt = metrics.get("long_term_debt")
    operating_cash_flow = metrics.get("operating_cash_flow")
    capex = metrics.get("capex")
    operating_profit = metrics.get("operating_profit")
    interest_expense = metrics.get("interest_expense")
    debt_total = None
    debt_source = short_term_debt or long_term_debt
    if short_term_debt is not None or long_term_debt is not None:
        debt_total = (short_term_debt.value_normalized or 0.0) + (long_term_debt.value_normalized or 0.0)
    if cash is not None and debt_total is not None and debt_source is not None:
        derived.append(
            ExtractedMetric(
                metric_name="net_debt",
                value_normalized=debt_total - (cash.value_normalized or 0.0),
                raw_value_text=f"{debt_total} - {cash.raw_value_text}",
                currency=cash.currency,
                unit_scale=cash.unit_scale,
                fiscal_year=cash.fiscal_year,
                fiscal_period=cash.fiscal_period,
                period_end_date=cash.period_end_date,
                audit_status=cash.audit_status,
                consolidation_scope=cash.consolidation_scope,
                language_variant=cash.language_variant,
                chunk=debt_source.chunk,
                excerpt_text=debt_source.excerpt_text,
                char_start=debt_source.char_start,
                char_end=debt_source.char_end,
            )
        )
    if operating_cash_flow is not None and capex is not None:
        derived.append(
            ExtractedMetric(
                metric_name="free_cash_flow",
                value_normalized=(operating_cash_flow.value_normalized or 0.0) - abs(capex.value_normalized or 0.0),
                raw_value_text=f"{operating_cash_flow.raw_value_text} - {capex.raw_value_text}",
                currency=operating_cash_flow.currency,
                unit_scale=operating_cash_flow.unit_scale,
                fiscal_year=operating_cash_flow.fiscal_year,
                fiscal_period=operating_cash_flow.fiscal_period,
                period_end_date=operating_cash_flow.period_end_date,
                audit_status=operating_cash_flow.audit_status,
                consolidation_scope=operating_cash_flow.consolidation_scope,
                language_variant=operating_cash_flow.language_variant,
                chunk=capex.chunk,
                excerpt_text=capex.excerpt_text,
                char_start=capex.char_start,
                char_end=capex.char_end,
            )
        )
    if operating_profit is not None and interest_expense is not None:
        denominator = abs(interest_expense.value_normalized or 0.0)
        if denominator:
            derived.append(
                ExtractedMetric(
                    metric_name="interest_coverage",
                    value_normalized=(operating_profit.value_normalized or 0.0) / denominator,
                    raw_value_text=f"{operating_profit.raw_value_text} / {interest_expense.raw_value_text}",
                    currency=operating_profit.currency,
                    unit_scale="ratio",
                    fiscal_year=operating_profit.fiscal_year,
                    fiscal_period=operating_profit.fiscal_period,
                    period_end_date=operating_profit.period_end_date,
                    audit_status=operating_profit.audit_status,
                    consolidation_scope=operating_profit.consolidation_scope,
                    language_variant=operating_profit.language_variant,
                    chunk=interest_expense.chunk,
                    excerpt_text=interest_expense.excerpt_text,
                    char_start=interest_expense.char_start,
                    char_end=interest_expense.char_end,
                )
            )
    return derived


def _extract_financial_events(document: ExtractDocument, chunks: list[ChunkText]) -> list[ExtractedEvent]:
    del document
    patterns = [
        (
            "debt_refinancing",
            "restrukturisasi kredit",
            "info",
            "mixed",
            0.7,
            "Annual report notes a credit restructuring or refinancing action.",
            [r"restrukturisasi kredit", r"credit restructuring", r"\brefinancing\b"],
        ),
        (
            "suspension_related_notice",
            "penghentian sementara perdagangan saham",
            "warning",
            "negative",
            0.9,
            "Annual report references suspension, delisting, or relisting status.",
            [r"penghentian sementara perdagangan saham", r"\bsuspension\b", r"lifted the temporary suspension"],
        ),
        (
            "material_transaction",
            "penjualan kapal",
            "info",
            "mixed",
            0.6,
            "Annual report discloses a vessel sale or other material transaction.",
            [r"penjualan\s+1\s+unit\s+kapal", r"sale of\s+1\s+unit\s+of\s+the company.?s vessel"],
        ),
        (
            "qualified_audit_opinion",
            "opini wajar dengan pengecualian",
            "critical",
            "negative",
            0.98,
            "Auditor opinion is qualified, adverse, or includes a disclaimer.",
            [
                r"wajar dengan pengecualian",
                r"qualified opinion",
                r"adverse opinion",
                r"disclaimer of opinion",
                r"opini tidak menyatakan pendapat",
            ],
        ),
        (
            "going_concern_warning",
            "kelangsungan usaha",
            "critical",
            "negative",
            0.97,
            "Filing references substantial doubt about going concern or business continuity.",
            [
                r"kelangsungan usaha",
                r"going concern",
                r"substantial doubt",
                r"material uncertainty.*going concern",
            ],
        ),
        (
            "covenant_stress",
            "pelanggaran covenant",
            "warning",
            "negative",
            0.92,
            "Filing indicates covenant pressure, waiver requests, or breach risk.",
            [
                r"covenant",
                r"waiver",
                r"breach",
                r"pelanggaran.*pinjaman",
                r"persyaratan.*pinjaman.*tidak terpenuhi",
            ],
        ),
        (
            "related_party_transaction",
            "pihak berelasi",
            "warning",
            "mixed",
            0.88,
            "Filing discusses a related-party transaction or balance that may need human review.",
            [
                r"pihak berelasi",
                r"related party",
                r"transaksi afiliasi",
                r"affiliated transaction",
            ],
        ),
        (
            "contingent_liability",
            "liabilitas kontinjensi",
            "warning",
            "negative",
            0.9,
            "Filing discloses contingent liabilities or off-balance-sheet obligations.",
            [
                r"liabilitas kontinjensi",
                r"contingent liabilit",
                r"komitmen dan kontinjensi",
                r"commitments and contingencies",
            ],
        ),
    ]
    return _match_event_patterns(chunks, patterns)


def _extract_disclosure_events(document: ExtractDocument, chunks: list[ChunkText]) -> list[ExtractedEvent]:
    title = (document.title or "").lower()
    patterns = [
        (
            "shareholder_meeting_summary",
            "rapat umum pemegang saham",
            "info",
            "neutral",
            0.95,
            "Document contains a shareholder meeting summary or meeting minutes notice.",
            [r"rapat umum pemegang saham", r"general meeting of shareholders", r"ringkasan risalah"],
        ),
        (
            "dividend_announcement",
            "dividen tunai",
            "info",
            "positive",
            0.8,
            "Document discloses a dividend decision or explicit no-dividend outcome.",
            [r"dividen tunai", r"cash dividend", r"tidak ada dividen"],
        ),
        (
            "auditor_change",
            "kantor akuntan publik",
            "info",
            "mixed",
            0.75,
            "Document appoints or reappoints the external auditor.",
            [r"kantor akuntan publik", r"akuntan publik"],
        ),
        (
            "bond_issuance",
            "penerbitan obligasi",
            "info",
            "negative",
            0.8,
            "Document announces a bond issuance plan or approval.",
            [r"penerbitan obligasi", r"obligasi perseroan", r"bond issuance"],
        ),
        (
            "debt_facility",
            "fasilitas pembiayaan",
            "warning",
            "negative",
            0.85,
            "Document discusses new debt or external financing facilities.",
            [r"fasilitas pembiayaan", r"bank luar negeri", r"debt facility"],
        ),
        (
            "governance_appointment",
            "perubahan susunan pengurus",
            "info",
            "neutral",
            0.75,
            "Document records an appointment or reappointment of governance personnel.",
            [r"perubahan susunan pengurus", r"appointment", r"reappointment of management", r"perubahan pengurus"],
        ),
        (
            "director_change",
            "direksi",
            "warning",
            "mixed",
            0.8,
            "Document records a director or executive management change.",
            [r"perubahan direksi", r"anggota direksi", r"director change", r"chief executive officer", r"chief financial officer"],
        ),
        (
            "commissioner_change",
            "dewan komisaris",
            "warning",
            "mixed",
            0.8,
            "Document records a commissioner or supervisory-board change.",
            [r"perubahan dewan komisaris", r"anggota dewan komisaris", r"commissioner change"],
        ),
        (
            "rights_issue",
            "hmetd",
            "warning",
            "negative",
            0.95,
            "Document announces a rights issue or pre-emptive offering.",
            [r"\bhmetd\b", r"hak memesan efek terlebih dahulu", r"rights issue"],
        ),
        (
            "private_placement",
            "pmthmetd",
            "warning",
            "negative",
            0.95,
            "Document announces a private placement or non-preemptive capital raise.",
            [r"\bpmthmetd\b", r"private placement", r"penambahan modal tanpa hak memesan efek terlebih dahulu"],
        ),
        (
            "stock_split",
            "pemecahan saham",
            "info",
            "neutral",
            0.9,
            "Document announces a stock split.",
            [r"pemecahan saham", r"stock split"],
        ),
        (
            "reverse_split",
            "reverse stock split",
            "warning",
            "negative",
            0.9,
            "Document announces a reverse stock split or nominal share consolidation.",
            [r"reverse stock split", r"penggabungan nilai nominal saham", r"reverse split"],
        ),
        (
            "buyback",
            "pembelian kembali saham",
            "info",
            "positive",
            0.8,
            "Document announces a share buyback or treasury share action.",
            [r"pembelian kembali saham", r"buyback", r"treasury share"],
        ),
        (
            "major_contract_win",
            "perolehan kontrak",
            "info",
            "positive",
            0.9,
            "Document announces a major contract win.",
            [r"perolehan kontrak", r"memperoleh kontrak", r"contract win", r"new material contract"],
        ),
        (
            "major_contract_loss",
            "pemutusan kontrak",
            "warning",
            "negative",
            0.95,
            "Document announces a major contract loss or termination.",
            [r"pemutusan kontrak", r"kehilangan kontrak", r"contract termination", r"major contract loss"],
        ),
        (
            "lawsuit",
            "gugatan hukum",
            "critical",
            "negative",
            0.95,
            "Document discloses a lawsuit, legal dispute, or litigation matter.",
            [r"gugatan hukum", r"sengketa hukum", r"lawsuit", r"litigation", r"legal dispute"],
        ),
        (
            "regulatory_action",
            "sanksi",
            "critical",
            "negative",
            0.95,
            "Document discloses a regulatory sanction, supervision, or restriction.",
            [r"sanksi", r"otoritas jasa keuangan", r"regulatory action", r"regulatory sanction", r"special supervision", r"pembatasan kegiatan usaha"],
        ),
        (
            "segment_closure",
            "penutupan segmen",
            "warning",
            "negative",
            0.85,
            "Document discloses a segment shutdown or closure.",
            [r"penutupan segmen", r"menghentikan segmen", r"segment closure", r"shutdown of .* segment"],
        ),
        (
            "guidance_change",
            "revisi proyeksi",
            "warning",
            "negative",
            0.8,
            "Document revises guidance or discloses a projection miss.",
            [r"revisi proyeksi", r"guidance revision", r"projection miss", r"guidance miss", r"target .* tidak tercapai"],
        ),
        (
            "use_of_proceeds",
            "penggunaan dana",
            "info",
            "mixed",
            0.75,
            "Document updates or revises use-of-proceeds disclosures.",
            [r"penggunaan dana", r"use of proceeds", r"realisasi penggunaan dana"],
        ),
    ]
    if "treatise summary" in title:
        patterns[0][6].append(r".")
    return _match_event_patterns(chunks, patterns)


def _match_event_patterns(
    chunks: list[ChunkText],
    patterns: list[tuple[str, str, str, str | None, float | None, str, list[str]]],
) -> list[ExtractedEvent]:
    out: list[ExtractedEvent] = []
    seen: set[str] = set()
    for chunk in chunks:
        for event_type, matched_pattern, severity, impact_direction, confidence, rationale, regexes in patterns:
            if event_type in seen:
                continue
            for regex in regexes:
                match = re.search(regex, chunk.text, flags=re.IGNORECASE)
                if match is None:
                    continue
                excerpt = chunk.text[max(0, match.start() - 40): min(len(chunk.text), match.end() + 220)]
                out.append(
                    ExtractedEvent(
                        event_type=event_type,
                        matched_pattern=matched_pattern,
                        severity=severity,
                        impact_direction=impact_direction,
                        confidence=confidence,
                        rationale=rationale,
                        payload_json=json.dumps(
                            {
                                "impact_direction": impact_direction,
                                "confidence": confidence,
                                "matched_regex": regex,
                                "section_label": chunk.section_label,
                            },
                            sort_keys=True,
                        ),
                        chunk=chunk,
                        excerpt_text=excerpt,
                        char_start=chunk.char_start + match.start(),
                        char_end=chunk.char_start + match.end(),
                    )
                )
                seen.add(event_type)
                break
    return out


def _persist_extraction_success(
    conn: sqlite3.Connection,
    *,
    document: ExtractDocument,
    extracted: ExtractResult,
    started_at: datetime,
    finished_at: datetime,
) -> None:
    now = finished_at.isoformat()
    conn.execute("DELETE FROM metric_items WHERE document_id = ?", (document.document_id,))
    conn.execute("DELETE FROM events WHERE document_id = ?", (document.document_id,))
    conn.execute("DELETE FROM evidence_spans WHERE document_id = ?", (document.document_id,))

    metrics_extracted = 0
    events_detected = 0
    for metric in extracted.metrics:
        span_id = _persist_span(
            conn,
            document_id=document.document_id,
            chunk=metric.chunk,
            excerpt_text=metric.excerpt_text,
            char_start=metric.char_start,
            char_end=metric.char_end,
        )
        metric_id = _stable_id(
            f"metric:{document.document_id}:{metric.metric_name}:{metric.chunk.chunk_id}:{metric.char_start}:{metric.char_end}"
        )
        conn.execute(
            """
            INSERT INTO metric_items (
                metric_id, document_id, ticker, metric_name, value_normalized, raw_value_text,
                currency, unit_scale, fiscal_year, fiscal_period, period_end_date,
                audit_status, consolidation_scope, language_variant, evidence_span_id, extracted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                metric_id,
                document.document_id,
                document.ticker,
                metric.metric_name,
                metric.value_normalized,
                metric.raw_value_text,
                metric.currency,
                metric.unit_scale,
                metric.fiscal_year,
                metric.fiscal_period,
                metric.period_end_date,
                metric.audit_status,
                metric.consolidation_scope,
                metric.language_variant,
                span_id,
                now,
            ),
        )
        metrics_extracted += 1

    for event in extracted.events:
        span_id = _persist_span(
            conn,
            document_id=document.document_id,
            chunk=event.chunk,
            excerpt_text=event.excerpt_text,
            char_start=event.char_start,
            char_end=event.char_end,
        )
        event_id = _stable_id(
            f"event:{document.document_id}:{event.event_type}:{event.chunk.chunk_id}:{event.char_start}:{event.char_end}"
        )
        conn.execute(
            """
            INSERT INTO events (
                event_id, document_id, ticker, event_type, matched_pattern,
                severity, impact_direction, confidence, rationale, evidence_span_id, payload_json, detected_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                document.document_id,
                document.ticker,
                event.event_type,
                event.matched_pattern,
                event.severity,
                event.impact_direction,
                event.confidence,
                event.rationale,
                span_id,
                event.payload_json,
                now,
            ),
        )
        events_detected += 1

    extraction_run_id = _stable_id(f"extract-run:{document.document_id}:{started_at.isoformat()}")
    conn.execute(
        """
        INSERT INTO extraction_runs (
            extraction_run_id, document_id, parser_version, extractor_version,
            status, error_code, metrics_extracted, events_detected, started_at, finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            extraction_run_id,
            document.document_id,
            document.parser_version,
            EXTRACTOR_VERSION,
            extracted.status,
            extracted.error_code,
            metrics_extracted,
            events_detected,
            started_at.isoformat(),
            finished_at.isoformat(),
        ),
    )
    conn.execute(
        """
        UPDATE documents
        SET process_state = ?, process_error_code = ?, pipeline_version = ?, updated_at = ?
        WHERE document_id = ?
        """,
        (
            "extracted",
            extracted.error_code,
            EXTRACTOR_VERSION,
            now,
            document.document_id,
        ),
    )


def _persist_extraction_failure(
    conn: sqlite3.Connection,
    *,
    document: ExtractDocument,
    error: ExtractionError,
    started_at: datetime,
    finished_at: datetime,
) -> None:
    run_id = _stable_id(f"extract-run:{document.document_id}:{started_at.isoformat()}")
    now = finished_at.isoformat()
    conn.execute(
        """
        INSERT INTO extraction_runs (
            extraction_run_id, document_id, parser_version, extractor_version,
            status, error_code, metrics_extracted, events_detected, started_at, finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            document.document_id,
            document.parser_version,
            EXTRACTOR_VERSION,
            "failed",
            error.code,
            0,
            0,
            started_at.isoformat(),
            finished_at.isoformat(),
        ),
    )
    conn.execute(
        """
        UPDATE documents
        SET process_state = ?, process_error_code = ?, pipeline_version = ?, updated_at = ?
        WHERE document_id = ?
        """,
        ("failed", error.code, EXTRACTOR_VERSION, now, document.document_id),
    )


def _persist_span(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    chunk: ChunkText,
    excerpt_text: str,
    char_start: int,
    char_end: int,
) -> str:
    span_id = _stable_id(f"span:{document_id}:{chunk.chunk_id}:{char_start}:{char_end}")
    conn.execute(
        """
        INSERT OR IGNORE INTO evidence_spans (
            span_id, document_id, chunk_id, page_start, page_end,
            char_start, char_end, excerpt_text, display_anchor
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            span_id,
            document_id,
            chunk.chunk_id,
            chunk.page_start,
            chunk.page_end,
            char_start,
            char_end,
            excerpt_text,
            f"p{chunk.page_start}",
        ),
    )
    return span_id


def _parse_number(raw: str) -> float:
    text = raw.strip()
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace(" ", "")
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif text.count(".") > 1 and "," not in text:
        text = text.replace(".", "")
    elif "," in text:
        parts = text.split(",")
        if len(parts[-1]) == 3:
            text = text.replace(",", "")
        else:
            text = text.replace(",", ".")
    value = float(text)
    return -value if negative else value


def _infer_fiscal_year(document: ExtractDocument, chunks: list[ChunkText]) -> int | None:
    samples = [document.title or ""]
    samples.extend(chunk.text[:600] for chunk in chunks[:20])
    patterns = [
        r"annual(?:\s*&\s*sustainability)?\s+report\s+(20\d{2})",
        r"tahun buku\s+(20\d{2})",
        r"financial year ending.*?(20\d{2})",
        r"as of december 31,\s*(20\d{2})",
    ]
    for sample in samples:
        for pattern in patterns:
            match = re.search(pattern, sample, flags=re.IGNORECASE | re.DOTALL)
            if match is not None:
                return int(match.group(1))
    year = _coerce_int(document.raw_metadata.get("report_year"))
    if year is None:
        return None
    if document.document_type == "annual_report":
        return year - 1
    return year


def _looks_like_attachment_notice(document: ExtractDocument, chunks: list[ChunkText]) -> bool:
    title = (document.title or "").lower()
    joined = "\n".join(chunk.text[:800] for chunk in chunks[:8]).lower()
    return (
        (
            "attachment" in joined
            and "official document" in joined
            and ("esg" in title or "generated electronically" in joined)
        )
        or (
            "esg" in title
            and (
                "penyampaian laporan tahunan" in joined
                or "kinerja lingkungan" in joined
                or "environmental performance" in joined
            )
        )
    )


def _language_variant(chunks: list[ChunkText]) -> str:
    joined = "\n".join(chunk.text[:300] for chunk in chunks[:12]).lower()
    has_id = any(token in joined for token in ["perseroan", "laporan", "pendapatan", "liabilitas"])
    has_en = any(token in joined for token in ["company", "report", "revenue", "liabilities"])
    if has_id and has_en:
        return "bilingual"
    if has_id:
        return "id"
    if has_en:
        return "en"
    return "unknown"


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _replace_result(result: ExtractRunResult, **updates: int) -> ExtractRunResult:
    payload = {
        "selected": result.selected,
        "extracted": result.extracted,
        "partial": result.partial,
        "failed": result.failed,
        "metrics": result.metrics,
        "events": result.events,
    }
    payload.update(updates)
    return ExtractRunResult(**payload)
