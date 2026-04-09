"""Appendix C output schema for local-summary validation."""

from __future__ import annotations

from pydantic import BaseModel, Field

OUTPUT_SCHEMA_VERSION = "appendix-c-v1"


class EvidenceLink(BaseModel):
    id: str
    document_id: str
    chunk_id: str
    page_start: int
    page_end: int


class KeyChange(BaseModel):
    claim: str
    severity: str
    evidence_links: list[str] = Field(min_length=1)


class RiskItem(BaseModel):
    claim: str
    evidence_links: list[str] = Field(min_length=1)


class EventItem(BaseModel):
    event_type: str
    evidence_links: list[str] = Field(min_length=1)


class SummaryOutput(BaseModel):
    document_id: str
    model_name: str
    summary: str
    key_changes: list[KeyChange]
    risks: list[RiskItem]
    events: list[EventItem]
    what_to_verify: list[str]
    evidence_links: list[EvidenceLink]
