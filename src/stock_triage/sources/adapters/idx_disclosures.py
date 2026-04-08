from __future__ import annotations

from ..idx_artifacts import load_artifact_text, parse_disclosure_html
from ..types import DiscoveryWindow
from ..types import SourceClass
from ._base import _IDXStubAdapter


class IDXDisclosureAdapter(_IDXStubAdapter):
    name = "idx_disclosures"
    source_class = SourceClass.DISCLOSURE
    artifact_source_id = "idx_disclosure"
    public_url = "https://www.idx.co.id/en/listed-companies/disclosure"

    def discover(self, window: DiscoveryWindow):
        del window
        html = load_artifact_text(artifacts_root=self.artifacts_root, source_id=self.artifact_source_id)
        return parse_disclosure_html(html)
