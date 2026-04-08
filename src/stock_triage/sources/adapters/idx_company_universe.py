from __future__ import annotations

from ..types import SourceClass
from ._base import _IDXStubAdapter


class IDXCompanyUniverseAdapter(_IDXStubAdapter):
    name = "idx_company_universe"
    source_class = SourceClass.COMPANY_UNIVERSE
    public_url = "https://www.idx.co.id/en/listed-companies/company-profiles"
