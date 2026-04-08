from __future__ import annotations

from ..idx_artifacts import load_artifact_text, parse_company_profiles_html
from ..types import DiscoveryWindow
from ..types import SourceClass
from ._base import _IDXStubAdapter


class IDXCompanyUniverseAdapter(_IDXStubAdapter):
    name = "idx_company_universe"
    source_class = SourceClass.COMPANY_UNIVERSE
    artifact_source_id = "idx_company_profiles"
    public_url = "https://www.idx.co.id/en/listed-companies/company-profiles"

    def discover(self, window: DiscoveryWindow):
        del window
        html = load_artifact_text(artifacts_root=self.artifacts_root, source_id=self.artifact_source_id)
        return parse_company_profiles_html(html)
