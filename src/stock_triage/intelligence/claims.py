"""Phase 13 management claim extraction and outcome linking."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ClaimSourceChunk:
    chunk_id: str
    text: str
    section_label: str | None
    span_id: str | None


@dataclass(frozen=True)
class ClaimRecord:
    claim_id: str
    ticker: str
    source_document_id: str
    claim_text: str
    claim_type: str
    claim_topic: str
    claim_subject: str | None
    claim_date: str | None
    claim_time_horizon: str | None
    claim_confidence: float
    evidence_span_id: str | None
    status: str
    payload_json: str


@dataclass(frozen=True)
class ClaimOutcomeRecord:
    outcome_id: str
    claim_id: str
    outcome_status: str
    assessment_date: str
    rationale: str
    evidence_span_id: str | None
    payload_json: str


@dataclass(frozen=True)
class ClaimCredibilitySummary:
    total_claims: int
    achieved: int
    partially_supported: int
    missed: int
    contradicted: int
    unresolved: int
    achieved_rate: float
    missed_rate: float
    unresolved_rate: float
    recycled_claim_count: int
    credibility_state: str


_CLAIM_PATTERNS: list[tuple[str, str, str | None, float, bool, tuple[re.Pattern[str], ...]]] = [
    (
        "revenue_growth",
        "growth",
        "revenue",
        0.85,
        False,
        (
            re.compile(r"\b(?:expect|expects|target|targets|aim|plan|plans|will)\b.{0,40}\b(?:revenue|sales)\b.{0,20}\b(?:grow|growth|increase)\b", re.I),
            re.compile(r"\b(?:grow|increase|improve)\b.{0,20}\b(?:revenue|sales)\b", re.I),
        ),
    ),
    (
        "debt_reduction",
        "balance_sheet",
        "debt",
        0.9,
        False,
        (
            re.compile(r"\b(?:reduce|reducing|reduction|lower|decrease|repay|deleverag\w*)\b.{0,30}\b(?:debt|borrowings|liabilities|leverage)\b", re.I),
        ),
    ),
    (
        "margin_improvement",
        "profitability",
        "operating_profit",
        0.8,
        False,
        (
            re.compile(r"\b(?:improve|improving|expand|expanding|restore)\b.{0,30}\b(?:margin|margins|profitability|operating profit)\b", re.I),
            re.compile(r"\b(?:margin|margins|profitability|operating profit)\b.{0,30}\b(?:improve|improving|expand|expanding|restore)\b", re.I),
        ),
    ),
    (
        "capex_expansion",
        "capital_allocation",
        "capex",
        0.75,
        False,
        (
            re.compile(r"\b(?:plan|plans|intend|intends|target|targets|akan)\b.{0,40}\b(?:capex|capital expenditure|belanja modal)\b", re.I),
            re.compile(r"\b(?:capex|capital expenditure|belanja modal)\b.{0,40}\b(?:plan|target|202\d)\b", re.I),
        ),
    ),
    (
        "funding_use",
        "capital_allocation",
        "use_of_proceeds",
        0.7,
        False,
        (
            re.compile(r"\b(?:use of proceeds|penggunaan dana)\b.{0,60}\b(?:for|to|untuk)\b", re.I),
            re.compile(r"\b(?:proceeds|dana hasil penawaran)\b.{0,60}\b(?:used|allocated|dialokasikan)\b", re.I),
        ),
    ),
    (
        "outlook_guidance",
        "outlook",
        "outlook",
        0.65,
        True,
        (
            re.compile(r"\b(?:guidance|outlook|prospek)\b.{0,60}\b(?:202\d|next year|tahun depan)\b", re.I),
            re.compile(r"\b(?:expects|target|aims|memproyeksikan|menargetkan)\b.{0,50}\b(?:202\d|tahun)\b", re.I),
        ),
    ),
    (
        "risk_explanation",
        "risk",
        "risk",
        0.6,
        True,
        (
            re.compile(r"\b(?:risk|risiko)\b.{0,60}\b(?:may|could|dapat|berpotensi)\b", re.I),
            re.compile(r"\b(?:headwind|tantangan|uncertainty|ketidakpastian)\b", re.I),
        ),
    ),
]


def extract_claims_for_documents(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    documents: list[object],
    latest_document_id: str,
) -> list[ClaimRecord]:
    claims: list[ClaimRecord] = []
    seen: set[str] = set()
    for document in documents:
        chunks = _load_claim_source_chunks(conn, document.document_id)
        for chunk in chunks:
            for claim_type, claim_topic, claim_subject, confidence, qualitative_only, patterns in _CLAIM_PATTERNS:
                match = _first_match(patterns, chunk.text)
                if match is None:
                    continue
                claim_text = _extract_sentence(chunk.text, match.start(), match.end())
                claim_id = _stable_id("claim", document.document_id, claim_type, claim_text)
                if claim_id in seen:
                    continue
                seen.add(claim_id)
                claims.append(
                    ClaimRecord(
                        claim_id=claim_id,
                        ticker=ticker,
                        source_document_id=document.document_id,
                        claim_text=claim_text,
                        claim_type=claim_type,
                        claim_topic=claim_topic,
                        claim_subject=claim_subject,
                        claim_date=getattr(document, "source_modified_at", None) or getattr(document, "updated_at", None),
                        claim_time_horizon=_infer_time_horizon(chunk.text),
                        claim_confidence=confidence,
                        evidence_span_id=chunk.span_id,
                        status="not_yet_testable" if document.document_id == latest_document_id else "open",
                        payload_json=json.dumps(
                            {
                                "chunk_id": chunk.chunk_id,
                                "classification_source": "regex",
                                "qualitative_only": qualitative_only,
                                "section_label": chunk.section_label,
                                "source_text": chunk.text[:400],
                            },
                            sort_keys=True,
                        ),
                    )
                )
    return claims


def evaluate_claim_outcomes(
    conn: sqlite3.Connection,
    *,
    claims: list[ClaimRecord],
    latest_document_id: str,
    assessment_date: str,
) -> tuple[list[ClaimOutcomeRecord], ClaimCredibilitySummary]:
    outcomes: list[ClaimOutcomeRecord] = []
    counts = {
        "Achieved": 0,
        "Partially Supported": 0,
        "Missed": 0,
        "Contradicted": 0,
        "Open": 0,
        "Not Yet Testable": 0,
        "Expired": 0,
        "Qualitative": 0,
    }
    signatures: dict[tuple[str, str | None], int] = {}
    evaluable_total = 0
    for claim in claims:
        signatures[(claim.claim_type, claim.claim_subject)] = signatures.get((claim.claim_type, claim.claim_subject), 0) + 1
        qualitative_only = _claim_is_qualitative(claim)
        if qualitative_only:
            status, rationale, evidence_span_id, payload = (
                "Qualitative",
                "Claim is narrative and stored for cross-document pattern tracking, not credibility scoring.",
                claim.evidence_span_id,
                {"support_links": [], "contradiction_links": [], "qualitative_only": True},
            )
        else:
            evaluable_total += 1
            status, rationale, evidence_span_id, payload = _evaluate_claim(
                conn,
                claim=claim,
                latest_document_id=latest_document_id,
            )
        counts[status] = counts.get(status, 0) + 1
        outcomes.append(
            ClaimOutcomeRecord(
                outcome_id=_stable_id("claim-outcome", claim.claim_id, latest_document_id),
                claim_id=claim.claim_id,
                outcome_status=status,
                assessment_date=assessment_date,
                rationale=rationale,
                evidence_span_id=evidence_span_id,
                payload_json=json.dumps(payload, sort_keys=True),
            )
        )
    total = evaluable_total
    achieved = counts["Achieved"]
    partially_supported = counts["Partially Supported"]
    missed = counts["Missed"]
    contradicted = counts["Contradicted"]
    unresolved = counts["Open"] + counts["Not Yet Testable"] + counts["Expired"]
    achieved_rate = ((achieved + partially_supported) / total) if total else 0.0
    missed_rate = ((missed + contradicted) / total) if total else 0.0
    unresolved_rate = (unresolved / total) if total else 0.0
    recycled_claim_count = sum(max(0, count - 1) for count in signatures.values())
    credibility_state = "stable"
    if contradicted or missed:
        credibility_state = "deteriorating"
    elif achieved or partially_supported:
        credibility_state = "improving"
    elif unresolved:
        credibility_state = "uncertain"
    return outcomes, ClaimCredibilitySummary(
        total_claims=total,
        achieved=achieved,
        partially_supported=partially_supported,
        missed=missed,
        contradicted=contradicted,
        unresolved=unresolved,
        achieved_rate=achieved_rate,
        missed_rate=missed_rate,
        unresolved_rate=unresolved_rate,
        recycled_claim_count=recycled_claim_count,
        credibility_state=credibility_state,
    )


def _evaluate_claim(
    conn: sqlite3.Connection,
    *,
    claim: ClaimRecord,
    latest_document_id: str,
) -> tuple[str, str, str | None, dict[str, Any]]:
    if claim.source_document_id == latest_document_id:
        return (
            "Not Yet Testable",
            "Claim originates from the latest document and has no later evidence window yet.",
            None,
            {"support_links": [], "contradiction_links": []},
        )
    mapping = {
        "revenue_growth": ("revenue", "up"),
        "debt_reduction": ("total_liabilities", "down"),
        "margin_improvement": ("operating_profit", "up"),
        "capex_expansion": ("capex", "up"),
    }
    metric_name, favorable_direction = mapping.get(claim.claim_type, (None, None))
    if claim.claim_type == "funding_use":
        return _evaluate_funding_use_claim(conn, claim=claim, latest_document_id=latest_document_id)
    if metric_name is None:
        return (
            "Open",
            "No deterministic evaluator is configured for this claim class yet.",
            None,
            {"support_links": [], "contradiction_links": []},
        )
    return _evaluate_metric_direction_claim(
        conn,
        claim=claim,
        latest_document_id=latest_document_id,
        metric_name=metric_name,
        favorable_direction=favorable_direction,
    )


def _evaluate_metric_direction_claim(
    conn: sqlite3.Connection,
    *,
    claim: ClaimRecord,
    latest_document_id: str,
    metric_name: str,
    favorable_direction: str,
) -> tuple[str, str, str | None, dict[str, Any]]:
    source_metric = _load_metric(conn, claim.source_document_id, metric_name)
    latest_metric = _load_metric(conn, latest_document_id, metric_name)
    if source_metric is None or latest_metric is None:
        return (
            "Not Yet Testable",
            f"Missing comparable {metric_name} evidence for claim evaluation.",
            None,
            {"support_links": [], "contradiction_links": []},
        )
    source_value = source_metric["value_normalized"]
    latest_value = latest_metric["value_normalized"]
    if source_value is None or latest_value is None:
        return (
            "Not Yet Testable",
            f"{metric_name} values are not normalized in both periods.",
            None,
            {"support_links": [], "contradiction_links": []},
        )
    change = latest_value - source_value
    evidence_span_id = latest_metric["evidence_span_id"]
    supports = ((change > 0 and favorable_direction == "up") or (change < 0 and favorable_direction == "down"))
    contradicts = ((change < 0 and favorable_direction == "up") or (change > 0 and favorable_direction == "down"))
    payload = {
        "metric_name": metric_name,
        "source_value": source_value,
        "latest_value": latest_value,
        "support_links": [evidence_span_id] if evidence_span_id and supports else [],
        "contradiction_links": [evidence_span_id] if evidence_span_id and contradicts else [],
    }
    if change == 0:
        return ("Partially Supported", f"{metric_name} is unchanged versus the claim baseline.", evidence_span_id, payload)
    if supports:
        return ("Achieved", f"{metric_name} moved in the claimed direction versus the originating filing.", evidence_span_id, payload)
    return ("Contradicted", f"{metric_name} moved against the claimed direction versus the originating filing.", evidence_span_id, payload)


def _evaluate_funding_use_claim(
    conn: sqlite3.Connection,
    *,
    claim: ClaimRecord,
    latest_document_id: str,
) -> tuple[str, str, str | None, dict[str, Any]]:
    row = conn.execute(
        """
        SELECT evidence_span_id, event_type
        FROM events
        WHERE document_id = ?
          AND event_type IN ('use_of_proceeds', 'use_of_proceeds_update')
        ORDER BY detected_at DESC, event_id DESC
        LIMIT 1
        """,
        (latest_document_id,),
    ).fetchone()
    if row is None:
        return (
            "Open",
            "No later use-of-proceeds disclosure was found yet.",
            None,
            {"support_links": [], "contradiction_links": []},
        )
    return (
        "Partially Supported",
        "A later use-of-proceeds disclosure exists, but the deployment quality still needs human judgment.",
        row["evidence_span_id"],
        {"support_links": [row["evidence_span_id"]] if row["evidence_span_id"] else [], "contradiction_links": []},
    )


def _load_claim_source_chunks(conn: sqlite3.Connection, document_id: str) -> list[ClaimSourceChunk]:
    rows = conn.execute(
        """
        SELECT
            dc.chunk_id,
            dc.text,
            dc.section_label,
            MIN(es.span_id) AS span_id
        FROM document_chunks dc
        LEFT JOIN evidence_spans es
          ON es.chunk_id = dc.chunk_id
         AND es.document_id = dc.document_id
        WHERE dc.document_id = ?
        GROUP BY dc.chunk_id, dc.text, dc.section_label
        ORDER BY dc.ord ASC
        """,
        (document_id,),
    ).fetchall()
    return [
        ClaimSourceChunk(
            chunk_id=row["chunk_id"],
            text=row["text"],
            section_label=row["section_label"],
            span_id=row["span_id"],
        )
        for row in rows
    ]


def _load_metric(conn: sqlite3.Connection, document_id: str, metric_name: str):
    return conn.execute(
        """
        SELECT value_normalized, evidence_span_id
        FROM metric_items
        WHERE document_id = ?
          AND metric_name = ?
        ORDER BY extracted_at DESC, metric_id DESC
        LIMIT 1
        """,
        (document_id, metric_name),
    ).fetchone()


def _claim_is_qualitative(claim: ClaimRecord) -> bool:
    try:
        payload = json.loads(claim.payload_json or "{}")
    except (TypeError, ValueError):
        payload = {}
    return bool(payload.get("qualitative_only")) or claim.claim_type in {"outlook_guidance", "risk_explanation"}


def _first_match(patterns: tuple[re.Pattern[str], ...], text: str):
    for pattern in patterns:
        match = pattern.search(text)
        if match is not None:
            return match
    return None


def _extract_sentence(text: str, start: int, end: int) -> str:
    left = text.rfind(".", 0, start)
    right = text.find(".", end)
    sentence = text[(left + 1 if left != -1 else 0):(right + 1 if right != -1 else len(text))].strip()
    return sentence or text[:240].strip()


def _infer_time_horizon(text: str) -> str | None:
    year_match = re.search(r"\b(20\d{2})\b", text)
    if year_match:
        return year_match.group(1)
    lowered = text.lower()
    for token in ("next year", "next 12 months", "this year", "near term"):
        if token in lowered:
            return token
    return None


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "|".join("" if part is None else str(part) for part in parts)
    return f"{prefix}-{hashlib.sha1(payload.encode('utf-8')).hexdigest()[:24]}"
