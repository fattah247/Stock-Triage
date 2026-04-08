"""Shared scaffolding for IDX adapters.

These stubs exist so the pipeline can target the SourceAdapter Protocol while
the Cloudflare-cleared session layer is still pending.
"""

from __future__ import annotations

from typing import Any, Iterable

from ..types import DiscoveryWindow, DocumentCandidate, SourceClass

PHASE_1_RECON_DOC = "docs/phase1_recon.md"


class _IDXStubAdapter:
    """Common base — every IDX adapter is blocked on the Phase 1.5 browser session."""

    name: str = "idx_stub"
    source_class: SourceClass = SourceClass.COMPANY_UNIVERSE
    public_url: str = ""

    def discover(self, window: DiscoveryWindow) -> Iterable[DocumentCandidate]:
        raise NotImplementedError(
            f"{type(self).__name__}.discover() requires a Cloudflare-cleared "
            f"browser session. See {PHASE_1_RECON_DOC}. Window={window.to_dict()}"
        )

    def healthcheck(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source_class": self.source_class.value,
            "public_url": self.public_url,
            "status": "blocked_on_browser_session",
            "blocking_reason": "cloudflare_managed_challenge",
            "next_step_doc": PHASE_1_RECON_DOC,
        }
