"""Shared scaffolding for IDX adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from ...config.loader import REPO_ROOT
from ..types import DiscoveryWindow, DocumentCandidate, SourceClass

PHASE_1_RECON_DOC = "docs/phase1_recon.md"
DEFAULT_BROWSER_ARTIFACTS_ROOT = REPO_ROOT / "data" / "recon" / "browser"


class _IDXStubAdapter:
    """Common base for IDX adapters backed by browser artifacts or session fetches."""

    name: str = "idx_stub"
    source_class: SourceClass = SourceClass.COMPANY_UNIVERSE
    public_url: str = ""
    artifact_source_id: str = ""

    def __init__(self, *, artifacts_root: Path | None = None) -> None:
        self.artifacts_root = artifacts_root or DEFAULT_BROWSER_ARTIFACTS_ROOT

    def artifact_path(self, filename: str = "page.html") -> Path:
        return self.artifacts_root / self.artifact_source_id / filename

    def default_window(self) -> DiscoveryWindow:
        return DiscoveryWindow()

    def resume_window(self, watermark: DiscoveryWindow) -> DiscoveryWindow:
        return watermark

    def watermark_window(
        self,
        *,
        requested_window: DiscoveryWindow,
        candidates: list[DocumentCandidate],
    ) -> DiscoveryWindow:
        del candidates
        return requested_window

    def discover(self, window: DiscoveryWindow) -> Iterable[DocumentCandidate]:
        raise NotImplementedError(
            f"{type(self).__name__}.discover() must be implemented by the concrete adapter. "
            f"See {PHASE_1_RECON_DOC}. "
            f"Window={window.to_dict()}"
        )

    def healthcheck(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source_class": self.source_class.value,
            "public_url": self.public_url,
            "status": "browser_backed_discovery_available",
            "blocking_reason": None,
            "next_step_doc": PHASE_1_RECON_DOC,
        }
