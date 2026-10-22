"""Metrics from IDX FinancialStatement-*.xlsx workbooks, read by exact taxonomy caption.

The parse stage stores each visible sheet as tab-separated rows (see
``stock_triage.parse.workbook``). Line items carry fixed Indonesian and English
captions across issuers, so a figure is located by caption within its statement
rather than by pattern, and is scaled by the rounding declared on the general
information sheet ('1000000').
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from ..fiscal_year import FISCAL_PERIOD_MONTHS_BEFORE_YEAR_END, fiscal_period_end
from ..parse.workbook import split_cells, value_contexts
from .pipeline import (
    ChunkText,
    ExtractDocument,
    ExtractedMetric,
    ExtractionError,
    _derive_financial_metrics,
    _infer_fiscal_period,
    _infer_fiscal_year,
)

# Sheet title fragment -> statement kind. Each industry template numbers its
# sheets differently ('1210000', '4220000', '8220000', ...) but titles them alike.
_STATEMENT_KINDS = (
    ("general information", "general"),
    ("statement of financial position", "financial_position"),
    ("statement of profit or loss", "profit_or_loss"),
    ("statement of cash flows", "cash_flows"),
)

# (metric, statement kind, captions) in priority order; the first row of that
# statement carrying one of the captions and a current-period figure wins.
_METRIC_CAPTIONS: tuple[tuple[str, str, frozenset[str]], ...] = (
    ("total_assets", "financial_position", frozenset({"jumlah aset", "total assets"})),
    ("total_liabilities", "financial_position", frozenset({"jumlah liabilitas", "total liabilities"})),
    ("equity", "financial_position", frozenset({"jumlah ekuitas", "total equity"})),
    ("cash", "financial_position", frozenset({"kas dan setara kas", "cash and cash equivalents"})),
    # Bank balance sheets show only "Kas"; the cash flow statement closes on cash and equivalents.
    (
        "cash",
        "cash_flows",
        frozenset(
            {
                "kas dan setara kas arus kas, akhir periode",
                "cash and cash equivalents cash flows, end of the period",
            }
        ),
    ),
    ("inventory", "financial_position", frozenset({"persediaan lancar", "current inventories"})),
    ("inventory", "financial_position", frozenset({"persediaan", "inventories"})),
    ("revenue", "profit_or_loss", frozenset({"penjualan dan pendapatan usaha", "sales and revenue"})),
    ("gross_profit", "profit_or_loss", frozenset({"jumlah laba bruto", "total gross profit"})),
    # Financial-industry templates only; general-industry ones have no operating subtotal.
    ("operating_profit", "profit_or_loss", frozenset({"jumlah laba operasional", "total profit from operation"})),
    ("net_profit", "profit_or_loss", frozenset({"jumlah laba (rugi)", "total profit (loss)"})),
    ("interest_expense", "profit_or_loss", frozenset({"beban bunga dan keuangan", "interest and finance costs"})),
    (
        "operating_cash_flow",
        "cash_flows",
        frozenset(
            {
                "jumlah arus kas bersih yang diperoleh dari (digunakan untuk) aktivitas operasi",
                "total net cash flows received from (used in) operating activities",
            }
        ),
    ),
    (
        "capex",
        "cash_flows",
        frozenset(
            {
                "pembayaran untuk perolehan aset tetap",
                "payments for acquisition of property, plant and equipment",
                "payments for acquisition of property and equipment",
            }
        ),
    ),
)

# General information fields, by caption prefix (Indonesian, English).
_CURRENCY_FIELD = ("mata uang pelaporan", "description of presentation currency")
_ROUNDING_FIELD = ("pembulatan yang digunakan", "level of rounding used")
_PERIOD_END_FIELD = ("tanggal akhir periode berjalan", "current period end date")
_REPORT_TYPE_FIELD = ("jenis laporan atas laporan keuangan", "type of report on financial statements")
_ENTITY_FIELD = ("apakah merupakan laporan keuangan satu entitas", "whether the financial statements are of an individual")

# Rounding value fragment -> (unit_scale, multiplier), e.g. "Jutaan / In Million".
_ROUNDINGS = (
    ("satuan penuh", "units", 1),
    ("full amount", "units", 1),
    ("ribuan", "thousand", 1_000),
    ("thousand", "thousand", 1_000),
    ("jutaan", "million", 1_000_000),
    ("million", "million", 1_000_000),
    ("miliaran", "billion", 1_000_000_000),
    ("billion", "billion", 1_000_000_000),
)
_CURRENCY_CODE = re.compile(r"\b([A-Z]{3})\b")
_EXCEL_EPOCH = date(1899, 12, 30)


@dataclass(frozen=True)
class _Row:
    kind: str
    captions: frozenset[str]
    value_text: str
    chunk: ChunkText
    line_start: int
    line_end: int
    value_start: int


def extract_workbook_metrics(
    document: ExtractDocument,
    chunks: list[ChunkText],
    *,
    fiscal_year_end_month: int,
) -> list[ExtractedMetric]:
    rows = list(_workbook_rows(chunks))
    general = [row for row in rows if row.kind == "general"]

    rounding = _general_value(general, _ROUNDING_FIELD)
    scale = next(
        ((unit_scale, multiplier) for fragment, unit_scale, multiplier in _ROUNDINGS if fragment in (rounding or "").lower()),
        None,
    )
    if scale is None:
        raise ExtractionError(
            "workbook_unknown_rounding",
            f"Workbook {document.document_id} does not state a recognised rounding: {rounding!r}",
        )
    unit_scale, multiplier = scale
    currency_codes = _CURRENCY_CODE.findall(_general_value(general, _CURRENCY_FIELD) or "")
    currency = currency_codes[-1] if currency_codes else None

    fiscal_period = _infer_fiscal_period(document)
    period_end = _parse_date(_general_value(general, _PERIOD_END_FIELD))
    if period_end is not None:
        fiscal_year: int | None = _fiscal_year_ending_after(period_end, fiscal_period)
        period_end_date: str | None = period_end.isoformat()
    else:
        fiscal_year = _infer_fiscal_year(document, chunks, fiscal_period=fiscal_period)
        period_end_date = (
            fiscal_period_end(fiscal_year, fiscal_period, fiscal_year_end_month).isoformat()
            if fiscal_year is not None
            else None
        )
    audit_status = _audit_status(_general_value(general, _REPORT_TYPE_FIELD)) or (
        "audited" if document.document_type == "annual_report" else None
    )
    entity = (_general_value(general, _ENTITY_FIELD) or "").lower()
    consolidation_scope = "standalone" if ("tunggal" in entity or "single" in entity) else "consolidated"

    out: dict[str, ExtractedMetric] = {}
    for metric_name, kind, captions in _METRIC_CAPTIONS:
        if metric_name in out:
            continue
        for row in rows:
            if row.kind != kind or not (row.captions & captions):
                continue
            value = _parse_decimal(row.value_text)
            if value is None:
                continue
            out[metric_name] = ExtractedMetric(
                metric_name=metric_name,
                value_normalized=float(value * multiplier),
                raw_value_text=row.value_text,
                currency=currency,
                unit_scale=unit_scale,
                fiscal_year=fiscal_year,
                fiscal_period=fiscal_period,
                period_end_date=period_end_date,
                audit_status=audit_status,
                consolidation_scope=consolidation_scope,
                language_variant="bilingual",
                chunk=row.chunk,
                excerpt_text=row.chunk.text[row.line_start : row.line_end],
                char_start=row.chunk.char_start + row.value_start,
                char_end=row.chunk.char_start + row.value_start + len(row.value_text),
            )
            break
    for metric in _derive_financial_metrics(out):
        out.setdefault(metric.metric_name, metric)
    return list(out.values())


def _workbook_rows(chunks: list[ChunkText]) -> Iterator[_Row]:
    """Rows carrying a current-period cell, in workbook order.

    A sheet's title and context row open its first chunk, so the statement kind
    and current-period column carry over to the sheet's later chunks.
    """
    sheet: int | None = None
    kind: str | None = None
    current_column: int | None = None
    for chunk in chunks:
        if chunk.page_start != sheet:
            sheet = chunk.page_start
            title = (chunk.section_label or "").lower()
            kind = next((name for fragment, name in _STATEMENT_KINDS if fragment in title), None)
            current_column = None
        if kind is None:
            continue
        line_start = 0
        for line in chunk.text.split("\n"):
            line_end = line_start + len(line)
            cells = split_cells(line)
            contexts = value_contexts(cells)
            if contexts:
                current_column = _current_column(contexts)
            elif current_column is not None and current_column < len(cells) and cells[current_column]:
                yield _Row(
                    kind=kind,
                    captions=frozenset({_caption(cells[0]), _caption(cells[-1])}),
                    value_text=cells[current_column],
                    chunk=chunk,
                    line_start=line_start,
                    line_end=line_end,
                    value_start=line_start + sum(len(cell) + 1 for cell in cells[:current_column]),
                )
            line_start = line_end + 1


def _current_column(contexts: dict[int, str]) -> int | None:
    # Interim sheets may add quarter-only columns; the year-to-date one comes first.
    for prefix in ("CurrentYear", "Current"):
        for column in sorted(contexts):
            if contexts[column].startswith(prefix):
                return column
    return None


def _caption(text: str) -> str:
    return " ".join(text.lower().split())


def _general_value(rows: list[_Row], prefixes: tuple[str, ...]) -> str | None:
    for row in rows:
        if any(caption.startswith(prefix) for caption in row.captions for prefix in prefixes):
            return row.value_text
    return None


def _parse_decimal(text: str) -> Decimal | None:
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _parse_date(text: str | None) -> date | None:
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    serial = _parse_decimal(text)
    # A date cell left in Excel's serial form (days since 1899-12-30).
    if serial is not None and 20_000 <= serial <= 80_000:
        return _EXCEL_EPOCH + timedelta(days=int(serial))
    return None


def _fiscal_year_ending_after(period_end: date, fiscal_period: str) -> int:
    """Fiscal year (named for the year it ends in) that the period ending ``period_end`` belongs to."""
    month_index = period_end.year * 12 + period_end.month - 1 + FISCAL_PERIOD_MONTHS_BEFORE_YEAR_END[fiscal_period]
    return month_index // 12


def _audit_status(report_type: str | None) -> str | None:
    text = (report_type or "").lower()
    if "tidak diaudit" in text or "unaudit" in text:
        return "unaudited"
    if "diaudit" in text or "audited" in text:
        return "audited"
    if "review" in text:
        return "reviewed"
    return None
