"""Choosing among ``metric_items`` rows that report the same figure."""

from __future__ import annotations

# ``documents.parse_strategy`` of IDX's FinancialStatement-*.xlsx workbooks.
WORKBOOK_PARSE_STRATEGY = "xlsx"


def preferred_metric_sql(alias: str) -> str:
    """SQL condition keeping the ``metric_items`` row ``alias`` unless a workbook reports it.

    IDX's workbooks carry the statements as structured data, so their figure
    wins over a text-extracted one for the same ticker, fiscal year, fiscal
    period and metric. Workbook rows themselves are always kept.
    """
    return f"""(
        EXISTS (
            SELECT 1 FROM documents own
            WHERE own.document_id = {alias}.document_id
              AND own.parse_strategy = '{WORKBOOK_PARSE_STRATEGY}'
        )
        OR NOT EXISTS (
            SELECT 1
            FROM metric_items preferred
            JOIN documents preferred_doc ON preferred_doc.document_id = preferred.document_id
            WHERE preferred.ticker = {alias}.ticker
              AND preferred.fiscal_year = {alias}.fiscal_year
              AND preferred.fiscal_period = {alias}.fiscal_period
              AND preferred.metric_name = {alias}.metric_name
              AND preferred_doc.parse_strategy = '{WORKBOOK_PARSE_STRATEGY}'
              AND preferred_doc.process_state IN ('extracted', 'analyzed')
        )
    )"""
