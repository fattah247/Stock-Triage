"""Source adapters for the v1 source classes (PRD §6.2).

The repository now includes browser-backed proof-of-access tooling for IDX
surfaces (see `docs/phase1_recon.md` and `scripts/run_browser_recon.py`).
Phase 2 bootstrap discovery is available:

- company universe parses from saved browser artifacts
- disclosures parse from saved browser artifacts
- financial reports fetch JSON through the warmed browser session
"""

from .idx_company_universe import IDXCompanyUniverseAdapter
from .idx_disclosures import IDXDisclosureAdapter
from .idx_financial_reports import IDXFinancialReportAdapter

__all__ = [
    "IDXCompanyUniverseAdapter",
    "IDXDisclosureAdapter",
    "IDXFinancialReportAdapter",
]
