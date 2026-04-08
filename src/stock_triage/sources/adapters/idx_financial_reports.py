from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import urlencode

from ...recon import PlaywrightSession
from ...config import load_config
from ..idx_artifacts import parse_financial_report_json
from ..types import DiscoveryWindow
from ..types import SourceClass
from ._base import _IDXStubAdapter


class IDXFinancialReportAdapter(_IDXStubAdapter):
    name = "idx_financial_reports"
    source_class = SourceClass.FINANCIAL_REPORT
    artifact_source_id = "idx_financial_statements_annual_report"
    public_url = (
        "https://www.idx.co.id/en/listed-companies/financial-statements-and-annual-report"
    )

    def discover(self, window: DiscoveryWindow):
        cfg = load_config()
        year = self._resolve_year(window)
        api_url = self._build_api_url(year=year)
        session = PlaywrightSession(
            profile_dir=cfg.browser.profile_dir,
            artifacts_dir=cfg.browser.artifacts_dir,
            headless=cfg.browser.headless,
            challenge_timeout_s=cfg.browser.challenge_timeout_s,
            page_settle_ms=cfg.browser.page_settle_ms,
            channel=cfg.browser.channel,
        )
        fetch = session.fetch_json(
            source_id=self.artifact_source_id,
            page_url=self.public_url,
            api_url=api_url,
        )
        if fetch.status != 200:
            raise RuntimeError(f"IDX financial report API returned status {fetch.status} for {api_url}")
        payload = json.loads(fetch.body_path.read_text(encoding="utf-8"))
        return parse_financial_report_json(payload)

    @staticmethod
    def _resolve_year(window: DiscoveryWindow) -> int:
        if window.end is not None:
            return window.end.year
        if window.start is not None:
            return window.start.year
        now = datetime.now(timezone.utc)
        return now.year if now.month >= 7 else now.year - 1

    @staticmethod
    def _build_api_url(*, year: int, page_size: int = 100) -> str:
        params = {
            "indexFrom": 1,
            "pageSize": page_size,
            "year": year,
            "reportType": "rdf",
            "EmitenType": "s",
            "periode": "audit",
            "kodeEmiten": "",
            "SortColumn": "FileModified",
            "SortOrder": "desc",
        }
        return (
            "https://www.idx.co.id/primary/ListedCompany/GetFinancialReport?"
            + urlencode(params)
        )
