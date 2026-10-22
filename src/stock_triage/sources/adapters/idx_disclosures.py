from __future__ import annotations

import hashlib
import json
from datetime import date

from ...config import load_config
from ...recon import PlaywrightSession
from ..idx_artifacts import load_artifact_text, parse_disclosure_html
from ..types import DiscoveryWindow
from ..types import DocumentCandidate
from ..types import SourceClass
from ._base import DEFAULT_BROWSER_ARTIFACTS_ROOT, _IDXStubAdapter


class IDXDisclosureAdapter(_IDXStubAdapter):
    name = "idx_disclosures"
    source_class = SourceClass.DISCLOSURE
    artifact_source_id = "idx_disclosure"
    public_url = "https://www.idx.co.id/en/listed-companies/disclosure"
    bootstrap_cursor = "bootstrap:first-page"

    def default_window(self) -> DiscoveryWindow:
        return DiscoveryWindow(cursor=self.bootstrap_cursor)

    def resume_window(self, watermark: DiscoveryWindow) -> DiscoveryWindow:
        if watermark.start is None and watermark.end is None and not watermark.cursor:
            return self.default_window()
        # The saved ``end`` is the newest date already seen: a floor for the next
        # scan, not an upper bound (that would exclude everything published since).
        # A historical window may end in the future; never floor past today.
        floor = watermark.end or watermark.start
        if floor is not None:
            floor = min(floor, date.today())
        return DiscoveryWindow(start=floor, end=None, cursor=watermark.cursor)

    def watermark_window(
        self,
        *,
        requested_window: DiscoveryWindow,
        candidates: list[DocumentCandidate],
    ) -> DiscoveryWindow:
        if candidates:
            newest = candidates[0]
            newest_date = newest.upstream_published_at.date() if newest.upstream_published_at else requested_window.end
            return DiscoveryWindow(start=requested_window.start, end=newest_date, cursor=newest.source_record_id)
        if requested_window.cursor == self.bootstrap_cursor:
            return self.default_window()
        return requested_window

    def discover(self, window: DiscoveryWindow):
        if self.artifacts_root != DEFAULT_BROWSER_ARTIFACTS_ROOT and self.artifact_path().exists():
            html = load_artifact_text(artifacts_root=self.artifacts_root, source_id=self.artifact_source_id)
            return parse_disclosure_html(html)

        cfg = load_config()
        session = PlaywrightSession(
            profile_dir=cfg.browser.profile_dir,
            artifacts_dir=cfg.browser.artifacts_dir,
            headless=cfg.browser.headless,
            challenge_timeout_s=cfg.browser.challenge_timeout_s,
            page_settle_ms=cfg.browser.page_settle_ms,
            channel=cfg.browser.channel,
        )

        resume_cursor = None if window.cursor == self.bootstrap_cursor else window.cursor
        bootstrap_only = window.cursor == self.bootstrap_cursor and window.start is None and window.end is None
        candidates: list[DocumentCandidate] = []
        seen_record_ids: set[str] = set()

        with session.open_page(page_url=self.public_url) as page:
            while True:
                page_candidates = parse_disclosure_html(page.content())
                stop_paging = False

                for candidate in page_candidates:
                    if resume_cursor and candidate.source_record_id == resume_cursor:
                        stop_paging = True
                        break
                    if not self._within_window(candidate, window):
                        if self._is_older_than_start(candidate, window.start):
                            stop_paging = True
                            break
                        continue
                    if candidate.source_record_id in seen_record_ids:
                        continue
                    seen_record_ids.add(candidate.source_record_id)
                    candidates.append(candidate)

                if bootstrap_only or stop_paging:
                    break

                next_button = page.locator(".btn-arrow.--next")
                if next_button.count() == 0 or next_button.is_disabled():
                    break
                next_button.click()
                page.wait_for_timeout(1500)

        return candidates

    @staticmethod
    def _within_window(candidate: DocumentCandidate, window: DiscoveryWindow) -> bool:
        if candidate.upstream_published_at is None:
            return True
        published_date = candidate.upstream_published_at.date()
        if window.start is not None and published_date < window.start:
            return False
        if window.end is not None and published_date > window.end:
            return False
        return True

    @staticmethod
    def _is_older_than_start(candidate: DocumentCandidate, start: date | None) -> bool:
        if start is None or candidate.upstream_published_at is None:
            return False
        return candidate.upstream_published_at.date() < start

    def healthcheck(self) -> dict[str, object]:
        mode = "live"
        if self.artifacts_root != DEFAULT_BROWSER_ARTIFACTS_ROOT and self.artifact_path().exists():
            mode = "artifact"
            html = load_artifact_text(artifacts_root=self.artifacts_root, source_id=self.artifact_source_id)
            candidates = parse_disclosure_html(html)
        else:
            cfg = load_config()
            session = PlaywrightSession(
                profile_dir=cfg.browser.profile_dir,
                artifacts_dir=cfg.browser.artifacts_dir,
                headless=cfg.browser.headless,
                challenge_timeout_s=cfg.browser.challenge_timeout_s,
                page_settle_ms=cfg.browser.page_settle_ms,
                channel=cfg.browser.channel,
            )
            with session.open_page(page_url=self.public_url) as page:
                candidates = parse_disclosure_html(page.content())

        sample_ids = [candidate.source_record_id for candidate in candidates[:5]]
        fingerprint = hashlib.sha256(json.dumps(sample_ids, sort_keys=True).encode("utf-8")).hexdigest()
        return {
            "name": self.name,
            "source_class": self.source_class.value,
            "public_url": self.public_url,
            "status": "ok" if candidates else "empty",
            "accessible": bool(candidates),
            "mode": mode,
            "row_count": len(candidates),
            "sample_ids": sample_ids,
            "fingerprint": fingerprint,
        }
