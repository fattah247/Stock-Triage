from __future__ import annotations

import json
from datetime import datetime, timezone
from math import ceil
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
    default_page_size = 100
    report_types = ("rdf", "rda")
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
        candidates = []
        seen_record_ids: set[str] = set()

        for report_type in self.report_types:
            first_payload = self._fetch_payload(
                session=session,
                api_url=self._build_api_url(
                    year=year,
                    page_size=self.default_page_size,
                    index_from=1,
                    report_type=report_type,
                ),
            )
            payloads = [first_payload]
            total_pages = self._total_pages(
                result_count=int(first_payload.get("ResultCount") or 0),
                page_size=self.default_page_size,
            )
            if total_pages > 1:
                page_urls = [
                    self._build_api_url(
                        year=year,
                        page_size=self.default_page_size,
                        index_from=page_no,
                        report_type=report_type,
                    )
                    for page_no in range(2, total_pages + 1)
                ]
                fetches = session.fetch_json_many(
                    source_id=self.artifact_source_id,
                    page_url=self.public_url,
                    api_urls=page_urls,
                )
                for fetch in fetches:
                    if fetch.status != 200:
                        raise RuntimeError(
                            f"IDX financial report API returned status {fetch.status} for {fetch.api_url}"
                        )
                    payloads.append(json.loads(fetch.body_path.read_text(encoding="utf-8")))

            for payload in payloads:
                for candidate in parse_financial_report_json(payload):
                    if candidate.source_record_id in seen_record_ids:
                        continue
                    seen_record_ids.add(candidate.source_record_id)
                    candidates.append(candidate)

        return candidates

    @staticmethod
    def _resolve_year(window: DiscoveryWindow) -> int:
        if window.end is not None:
            return window.end.year
        if window.start is not None:
            return window.start.year
        now = datetime.now(timezone.utc)
        return now.year if now.month >= 7 else now.year - 1

    @staticmethod
    def _build_api_url(
        *,
        year: int,
        page_size: int = 100,
        index_from: int = 1,
        report_type: str = "rdf",
        period: str = "audit",
    ) -> str:
        params = {
            "indexFrom": index_from,
            "pageSize": page_size,
            "year": year,
            "reportType": report_type,
            "EmitenType": "s",
            "periode": period,
            "kodeEmiten": "",
            "SortColumn": "FileModified",
            "SortOrder": "desc",
        }
        return (
            "https://www.idx.co.id/primary/ListedCompany/GetFinancialReport?"
            + urlencode(params)
        )

    def _fetch_payload(self, *, session: PlaywrightSession, api_url: str) -> dict:
        fetch = session.fetch_json(
            source_id=self.artifact_source_id,
            page_url=self.public_url,
            api_url=api_url,
        )
        if fetch.status != 200:
            raise RuntimeError(f"IDX financial report API returned status {fetch.status} for {api_url}")
        return json.loads(fetch.body_path.read_text(encoding="utf-8"))

    @staticmethod
    def _total_pages(*, result_count: int, page_size: int) -> int:
        if result_count <= 0:
            return 1
        return ceil(result_count / page_size)
