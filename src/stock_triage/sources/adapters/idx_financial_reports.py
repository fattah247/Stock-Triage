from __future__ import annotations

import json
from datetime import datetime, timezone
from math import ceil
from urllib.parse import urlencode

from ...recon import PlaywrightSession
from ...config import load_config
from ..idx_artifacts import parse_financial_report_json
from ..types import DiscoveryWindow
from ..types import DocumentCandidate
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
        session = PlaywrightSession(
            profile_dir=cfg.browser.profile_dir,
            artifacts_dir=cfg.browser.artifacts_dir,
            headless=cfg.browser.headless,
            challenge_timeout_s=cfg.browser.challenge_timeout_s,
            page_settle_ms=cfg.browser.page_settle_ms,
            channel=cfg.browser.channel,
        )
        resume_cursors = self._resume_cursors(window)
        candidates = []
        seen_record_ids: set[str] = set()

        with session.open_page(page_url=self.public_url) as page:
            for report_type in self.report_types:
                resume_cursor = resume_cursors.get(report_type)
                page_no = 1
                total_pages: int | None = None

                while total_pages is None or page_no <= total_pages:
                    payload = self._fetch_payload(
                        session=session,
                        page=page,
                        api_url=self._build_api_url(
                            year=year,
                            page_size=self.default_page_size,
                            index_from=page_no,
                            report_type=report_type,
                        ),
                    )
                    if total_pages is None:
                        total_pages = self._total_pages(
                            result_count=int(payload.get("ResultCount") or 0),
                            page_size=self.default_page_size,
                        )

                    stop_report = False
                    for candidate in parse_financial_report_json(payload):
                        if resume_cursor and candidate.source_record_id == resume_cursor:
                            stop_report = True
                            break
                        if candidate.source_record_id in seen_record_ids:
                            continue
                        seen_record_ids.add(candidate.source_record_id)
                        candidates.append(candidate)

                    if stop_report:
                        break
                    page_no += 1

        return candidates

    def watermark_window(
        self,
        *,
        requested_window: DiscoveryWindow,
        candidates: list[DocumentCandidate],
    ) -> DiscoveryWindow:
        if not candidates:
            return requested_window

        merged_cursors = self._resume_cursors(requested_window)
        updated_types: set[str] = set()
        newest_date = requested_window.end
        for candidate in candidates:
            report_type = candidate.raw_metadata.get("report_type")
            if report_type and report_type not in updated_types:
                merged_cursors[report_type] = candidate.source_record_id
                updated_types.add(report_type)
            if candidate.upstream_published_at is not None:
                candidate_date = candidate.upstream_published_at.date()
                if newest_date is None or candidate_date > newest_date:
                    newest_date = candidate_date

        cursor = json.dumps(merged_cursors, sort_keys=True) if merged_cursors else requested_window.cursor
        return DiscoveryWindow(
            start=requested_window.start,
            end=newest_date,
            cursor=cursor,
        )

    @staticmethod
    def _resume_cursors(window: DiscoveryWindow) -> dict[str, str]:
        if not window.cursor:
            return {}
        try:
            raw = json.loads(window.cursor)
        except json.JSONDecodeError:
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            str(report_type): str(record_id)
            for report_type, record_id in raw.items()
            if record_id
        }

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

    def _fetch_payload(self, *, session: PlaywrightSession, page, api_url: str) -> dict:
        fetch = session.fetch_json_from_page(
            source_id=self.artifact_source_id,
            page=page,
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
