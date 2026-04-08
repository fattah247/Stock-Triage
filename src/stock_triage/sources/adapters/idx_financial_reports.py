from __future__ import annotations

from ..types import SourceClass
from ._base import _IDXStubAdapter


class IDXFinancialReportAdapter(_IDXStubAdapter):
    name = "idx_financial_reports"
    source_class = SourceClass.FINANCIAL_REPORT
    public_url = (
        "https://www.idx.co.id/en/listed-companies/financial-statements-and-annual-report"
    )
