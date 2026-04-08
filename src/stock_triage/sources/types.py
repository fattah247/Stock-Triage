"""Source-adapter contract (PRD §6.2).

A `SourceAdapter` returns normalized `DocumentCandidate` objects from one
public source surface (universe, financial reports, disclosures). The
candidate object is the only thing the rest of the pipeline knows about; it
must capture enough provenance for the download stage to fetch the bytes and
for revision logic to detect updates later.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Iterable, Protocol


class SourceClass(str, Enum):
    """Three source classes the v1 adapter interface must support (PRD §6.2)."""

    COMPANY_UNIVERSE = "company_universe"
    FINANCIAL_REPORT = "financial_report"
    DISCLOSURE = "disclosure"


@dataclass(frozen=True)
class DiscoveryWindow:
    """Incremental discovery cursor (PRD §6.2: incremental via cursors or date windows)."""

    start: date | None = None
    end: date | None = None
    cursor: str | None = None  # adapter-specific (e.g., page number, opaque token)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "cursor": self.cursor,
        }


@dataclass
class DocumentCandidate:
    """Normalized pre-download discovery record (PRD §6.2 + §7.1 document_candidates)."""

    source_name: str
    source_class: SourceClass
    source_record_id: str
    title: str | None = None
    ticker: str | None = None
    upstream_published_at: datetime | None = None
    attachment_url: str | None = None
    attachment_filename: str | None = None
    raw_metadata: dict[str, Any] = field(default_factory=dict)
    discovered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not self.source_name:
            raise ValueError("source_name is required")
        if not self.source_record_id:
            raise ValueError("source_record_id is required")
        if isinstance(self.source_class, str):
            self.source_class = SourceClass(self.source_class)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["source_class"] = self.source_class.value
        if self.upstream_published_at is not None:
            d["upstream_published_at"] = self.upstream_published_at.isoformat()
        d["discovered_at"] = self.discovered_at.isoformat()
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, default=str)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DocumentCandidate":
        d = dict(data)
        if isinstance(d.get("upstream_published_at"), str):
            d["upstream_published_at"] = datetime.fromisoformat(d["upstream_published_at"])
        if isinstance(d.get("discovered_at"), str):
            d["discovered_at"] = datetime.fromisoformat(d["discovered_at"])
        d["source_class"] = SourceClass(d["source_class"])
        return cls(**d)


class SourceAdapter(Protocol):
    """Interface every source adapter implements (PRD §6.2).

    Adapters are responsible for one source surface and one source class. They
    must support incremental discovery via DiscoveryWindow and must capture
    upstream identifiers in raw_metadata so downstream stages can detect
    revisions and reproduce queries.
    """

    name: str
    source_class: SourceClass

    def discover(self, window: DiscoveryWindow) -> Iterable[DocumentCandidate]:
        """Yield DocumentCandidate objects for the given window."""
        ...

    def healthcheck(self) -> dict[str, Any]:
        """Return a small dict describing source reachability and shape.

        Used by the canary monitor (PRD §6.11) to detect upstream changes early.
        """
        ...
