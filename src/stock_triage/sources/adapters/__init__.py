"""Source adapters for the v1 source classes (PRD §6.2).

All three IDX adapters are stubs in Phase 1: live discovery requires browser
automation to clear Cloudflare's managed challenge (see docs/phase1_recon.md).
The adapter classes exist so the rest of the pipeline can be built and tested
against the SourceAdapter Protocol; their `discover()` methods raise
`NotImplementedError` until Phase 1.5 lands the Playwright-backed session.
"""

from .idx_company_universe import IDXCompanyUniverseAdapter
from .idx_disclosures import IDXDisclosureAdapter
from .idx_financial_reports import IDXFinancialReportAdapter

__all__ = [
    "IDXCompanyUniverseAdapter",
    "IDXDisclosureAdapter",
    "IDXFinancialReportAdapter",
]
