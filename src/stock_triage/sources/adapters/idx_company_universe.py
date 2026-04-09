from __future__ import annotations

import hashlib
import json
from datetime import date

from ...config import load_config
from ...recon import PlaywrightSession
from ..idx_artifacts import (
    load_artifact_text,
    parse_company_profile_rows,
    parse_company_profiles_html,
)
from ..types import DiscoveryWindow
from ..types import DocumentCandidate
from ..types import SourceClass
from ._base import DEFAULT_BROWSER_ARTIFACTS_ROOT, _IDXStubAdapter

_COMPANY_ROWS_SCRIPT = """
() => {
  const directRows = window.__NUXT__?.fetch?.["data-v-ac7150ac:0"]?.tableData?.data;
  if (Array.isArray(directRows)) {
    return directRows;
  }

  const seen = new Set();
  const walk = (value) => {
    if (!value || typeof value !== "object" || seen.has(value)) {
      return null;
    }
    seen.add(value);

    if (Array.isArray(value)) {
      if (
        value.length > 0 &&
        value[0] &&
        typeof value[0] === "object" &&
        ("KodeEmiten" in value[0] || "NamaEmiten" in value[0])
      ) {
        return value;
      }
      for (const item of value) {
        const found = walk(item);
        if (found) {
          return found;
        }
      }
      return null;
    }

    if ("KodeEmiten" in value || "NamaEmiten" in value) {
      return [value];
    }

    for (const child of Object.values(value)) {
      const found = walk(child);
      if (found) {
        return found;
      }
    }
    return null;
  };

  return walk(window.__NUXT__) || [];
}
"""


class IDXCompanyUniverseAdapter(_IDXStubAdapter):
    name = "idx_company_universe"
    source_class = SourceClass.COMPANY_UNIVERSE
    artifact_source_id = "idx_company_profiles"
    public_url = "https://www.idx.co.id/en/listed-companies/company-profiles"

    def default_window(self) -> DiscoveryWindow:
        today = date.today()
        return DiscoveryWindow(end=today)

    def resume_window(self, watermark: DiscoveryWindow) -> DiscoveryWindow:
        return DiscoveryWindow(start=watermark.start, end=date.today(), cursor=watermark.cursor)

    def watermark_window(
        self,
        *,
        requested_window: DiscoveryWindow,
        candidates: list[DocumentCandidate],
    ) -> DiscoveryWindow:
        del candidates
        return DiscoveryWindow(start=requested_window.start, end=date.today(), cursor=requested_window.cursor)

    def discover(self, window: DiscoveryWindow):
        del window
        if self.artifacts_root != DEFAULT_BROWSER_ARTIFACTS_ROOT and self.artifact_path().exists():
            html = load_artifact_text(artifacts_root=self.artifacts_root, source_id=self.artifact_source_id)
            return parse_company_profiles_html(html)

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
            rows = page.evaluate(_COMPANY_ROWS_SCRIPT)
            if rows:
                return parse_company_profile_rows(rows)
            return parse_company_profiles_html(page.content())

    def healthcheck(self) -> dict[str, object]:
        mode = "live"
        if self.artifacts_root != DEFAULT_BROWSER_ARTIFACTS_ROOT and self.artifact_path().exists():
            mode = "artifact"
            html = load_artifact_text(artifacts_root=self.artifacts_root, source_id=self.artifact_source_id)
            candidates = parse_company_profiles_html(html)
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
                rows = page.evaluate(_COMPANY_ROWS_SCRIPT)
                candidates = parse_company_profile_rows(rows) if rows else parse_company_profiles_html(page.content())

        sample_tickers = [candidate.source_record_id for candidate in candidates[:5]]
        fingerprint = hashlib.sha256(json.dumps(sample_tickers, sort_keys=True).encode("utf-8")).hexdigest()
        return {
            "name": self.name,
            "source_class": self.source_class.value,
            "public_url": self.public_url,
            "status": "ok" if candidates else "empty",
            "accessible": bool(candidates),
            "mode": mode,
            "row_count": len(candidates),
            "sample_tickers": sample_tickers,
            "fingerprint": fingerprint,
        }
