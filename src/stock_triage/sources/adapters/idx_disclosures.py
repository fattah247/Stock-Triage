from __future__ import annotations

from ..types import SourceClass
from ._base import _IDXStubAdapter


class IDXDisclosureAdapter(_IDXStubAdapter):
    name = "idx_disclosures"
    source_class = SourceClass.DISCLOSURE
    public_url = "https://www.idx.co.id/en/listed-companies/disclosure"
