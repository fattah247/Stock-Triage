"""Phase 7 summary context builder and validation gate."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
from pydantic import ValidationError

from ..config import Config
from ..db import WriterQueue, connect, configure_pragmas, run_migrations
from .schema import OUTPUT_SCHEMA_VERSION, SummaryDraftOutput, SummaryOutput

PROMPT_VERSION = "phase7-v2"


@dataclass(frozen=True)
class SummaryContext:
    document_id: str
    source_name: str
    source_record_id: str
    ticker: str | None
    metrics: list[dict[str, object]]
    events: list[dict[str, object]]
    rule_flags: list[dict[str, object]]
    evidence_links: list[dict[str, object]]


@dataclass(frozen=True)
class SummaryValidationResult:
    analysis_run_id: str
    schema_valid: bool
    evidence_link_valid: bool
    errors: list[str]


@dataclass(frozen=True)
class SummaryGenerateResult:
    analysis_run_id: str
    schema_valid: bool
    evidence_link_valid: bool
    errors: list[str]
    raw_response: str


def build_summary_context(cfg: Config, *, document_id: str) -> SummaryContext:
    cfg.ensure_dirs()
    conn = connect(cfg.paths.db_path, read_only=True)
    try:
        configure_pragmas(conn, cfg)
        doc = conn.execute(
            """
            SELECT document_id, source_name, source_record_id, ticker
            FROM documents
            WHERE document_id = ?
            """,
            (document_id,),
        ).fetchone()
        if doc is None:
            raise ValueError(f"Unknown document_id: {document_id}")
        metrics = conn.execute(
            """
            SELECT metric_name, value_normalized, currency, fiscal_year, fiscal_period, period_end_date, evidence_span_id
            FROM metric_items
            WHERE document_id = ?
            ORDER BY fiscal_year DESC, metric_name ASC
            """,
            (document_id,),
        ).fetchall()
        events = conn.execute(
            """
            SELECT event_type, severity, rationale, evidence_span_id
            FROM events
            WHERE document_id = ?
            ORDER BY event_type ASC
            """,
            (document_id,),
        ).fetchall()
        rules = conn.execute(
            """
            SELECT rule_name, severity, threshold, rationale, input_values
            FROM rule_runs
            WHERE document_id = ? AND triggered = 1
            ORDER BY severity DESC, rule_name ASC
            """,
            (document_id,),
        ).fetchall()
        spans = conn.execute(
            """
            SELECT span_id, document_id, chunk_id, page_start, page_end, excerpt_text
            FROM evidence_spans
            WHERE document_id = ?
            ORDER BY page_start ASC, chunk_id ASC
            LIMIT 25
            """,
            (document_id,),
        ).fetchall()
        return SummaryContext(
            document_id=doc["document_id"],
            source_name=doc["source_name"],
            source_record_id=doc["source_record_id"],
            ticker=doc["ticker"],
            metrics=[
                {
                    "metric_name": row["metric_name"],
                    "value_normalized": row["value_normalized"],
                    "currency": row["currency"],
                    "fiscal_year": row["fiscal_year"],
                    "fiscal_period": row["fiscal_period"],
                    "period_end_date": row["period_end_date"],
                    "evidence_span_id": row["evidence_span_id"],
                }
                for row in metrics
            ],
            events=[
                {
                    "event_type": row["event_type"],
                    "severity": row["severity"],
                    "rationale": row["rationale"],
                    "evidence_span_id": row["evidence_span_id"],
                }
                for row in events
            ],
            rule_flags=[
                {
                    "rule_name": row["rule_name"],
                    "severity": row["severity"],
                    "threshold": row["threshold"],
                    "rationale": row["rationale"],
                    "input_values": json.loads(row["input_values"]) if row["input_values"] else {},
                }
                for row in rules
            ],
            evidence_links=[
                {
                    "id": row["span_id"],
                    "document_id": row["document_id"],
                    "chunk_id": row["chunk_id"],
                    "page_start": row["page_start"],
                    "page_end": row["page_end"],
                    "excerpt_text": row["excerpt_text"],
                }
                for row in spans
            ],
        )
    finally:
        conn.close()


def validate_summary_file(
    cfg: Config,
    *,
    document_id: str,
    input_path: Path,
    model_name: str,
    prompt_version: str = PROMPT_VERSION,
) -> SummaryValidationResult:
    payload = input_path.read_text(encoding="utf-8")
    return validate_summary_payload(
        cfg,
        document_id=document_id,
        payload=payload,
        model_name=model_name,
        prompt_version=prompt_version,
    )


def validate_summary_payload(
    cfg: Config,
    *,
    document_id: str,
    payload: str,
    model_name: str,
    prompt_version: str = PROMPT_VERSION,
) -> SummaryValidationResult:
    cfg.ensure_dirs()
    analysis_run_id = _stable_id(
        f"analysis:{document_id}:{model_name}:{prompt_version}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"
    )
    schema_valid = False
    evidence_link_valid = False
    errors: list[str] = []
    parsed: SummaryOutput | None = None

    try:
        parsed = SummaryOutput.model_validate_json(payload)
        schema_valid = parsed.document_id == document_id
        if not schema_valid:
            errors.append("document_id_mismatch")
    except ValidationError as exc:
        errors.append("schema_validation_failed")
        errors.extend(error["type"] for error in exc.errors())

    conn = connect(cfg.paths.db_path, read_only=True)
    try:
        configure_pragmas(conn, cfg)
        valid_span_ids = {
            row["span_id"]
            for row in conn.execute(
                "SELECT span_id FROM evidence_spans WHERE document_id = ?",
                (document_id,),
            ).fetchall()
        }
    finally:
        conn.close()

    if parsed is not None and schema_valid:
        declared_ids = {link.id for link in parsed.evidence_links}
        referenced_ids = set()
        referenced_ids.update(link_id for item in parsed.key_changes for link_id in item.evidence_links)
        referenced_ids.update(link_id for item in parsed.risks for link_id in item.evidence_links)
        referenced_ids.update(link_id for item in parsed.events for link_id in item.evidence_links)
        missing_declared = sorted(referenced_ids - declared_ids)
        missing_db = sorted(referenced_ids - valid_span_ids)
        if missing_declared:
            errors.append("undeclared_evidence_links")
        if missing_db:
            errors.append("unknown_evidence_links")
        evidence_link_valid = not missing_declared and not missing_db

    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
        wq.submit(
            lambda conn: _persist_analysis_run(
                conn,
                analysis_run_id=analysis_run_id,
                document_id=document_id,
                model_name=model_name,
                prompt_version=prompt_version,
                schema_valid=schema_valid,
                evidence_link_valid=evidence_link_valid,
                payload=payload,
            )
        )
    return SummaryValidationResult(
        analysis_run_id=analysis_run_id,
        schema_valid=schema_valid,
        evidence_link_valid=evidence_link_valid,
        errors=errors,
    )


def generate_summary(
    cfg: Config,
    *,
    document_id: str,
    prompt_version: str = PROMPT_VERSION,
) -> SummaryGenerateResult:
    cfg.ensure_dirs()
    if not cfg.llm.model_name:
        raise ValueError("llm.model_name is required for summary generation")

    context = build_summary_context(cfg, document_id=document_id)
    alias_catalog = _build_alias_catalog(context)
    payload = {
        "model": cfg.llm.model_name,
        "stream": False,
        "format": SummaryDraftOutput.model_json_schema(),
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a conservative local analyst. Return only valid JSON that matches the schema. "
                    "Every key change, risk, and event must cite evidence_links using only the provided aliases "
                    "like E1, E2, and E3. Never invent aliases, raw span ids, page numbers, or chunk ids."
                ),
            },
            {
                "role": "user",
                "content": _build_summary_prompt(context, alias_catalog),
            },
        ],
        "options": {
            "temperature": cfg.llm.temperature,
        },
    }
    response = httpx.post(
        cfg.llm.base_url.rstrip("/") + "/chat",
        json=payload,
        timeout=cfg.llm.timeout_s,
    )
    response.raise_for_status()
    body = response.json()
    raw_response = body["message"]["content"]
    normalized_payload = _normalize_summary_payload(
        raw_response=raw_response,
        context=context,
        alias_catalog=alias_catalog,
        model_name=cfg.llm.model_name,
    )
    result = validate_summary_payload(
        cfg,
        document_id=document_id,
        payload=normalized_payload,
        model_name=cfg.llm.model_name,
        prompt_version=prompt_version,
    )
    return SummaryGenerateResult(
        analysis_run_id=result.analysis_run_id,
        schema_valid=result.schema_valid,
        evidence_link_valid=result.evidence_link_valid,
        errors=result.errors,
        raw_response=raw_response,
    )


def _persist_analysis_run(
    conn: sqlite3.Connection,
    *,
    analysis_run_id: str,
    document_id: str,
    model_name: str,
    prompt_version: str,
    schema_valid: bool,
    evidence_link_valid: bool,
    payload: str,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT OR REPLACE INTO analysis_runs (
            analysis_run_id, document_id, model_name, prompt_version, output_schema_version,
            schema_valid, evidence_link_valid, output_json, started_at, finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            analysis_run_id,
            document_id,
            model_name,
            prompt_version,
            OUTPUT_SCHEMA_VERSION,
            1 if schema_valid else 0,
            1 if evidence_link_valid else 0,
            payload,
            now,
            now,
        ),
    )
    conn.execute(
        """
        UPDATE documents
        SET model_name = ?, prompt_version = ?, output_schema_version = ?, updated_at = ?
        WHERE document_id = ?
        """,
        (model_name, prompt_version, OUTPUT_SCHEMA_VERSION, now, document_id),
    )
def _build_summary_prompt(context: SummaryContext, alias_catalog: list[dict[str, object]]) -> str:
    alias_by_span_id = {item["span_id"]: item["alias"] for item in alias_catalog}
    return json.dumps(
        {
            "document_id": context.document_id,
            "source_name": context.source_name,
            "source_record_id": context.source_record_id,
            "ticker": context.ticker,
            "rule_flags": context.rule_flags,
            "metrics": [
                {
                    **metric,
                    "evidence_alias": alias_by_span_id.get(metric["evidence_span_id"]),
                }
                for metric in context.metrics
            ],
            "events": [
                {
                    **event,
                    "evidence_alias": alias_by_span_id.get(event["evidence_span_id"]),
                }
                for event in context.events
            ],
            "evidence_catalog": [
                {
                    "alias": item["alias"],
                    "page_start": item["page_start"],
                    "page_end": item["page_end"],
                    "excerpt_text": item["excerpt_text"],
                }
                for item in alias_catalog
            ],
            "instructions": {
                "summary": "short narrative grounded in the supplied facts only",
                "key_changes": "list the most important deterministic changes",
                "risks": "list the principal risks supported by evidence",
                "what_to_verify": "list follow-up checks for the operator",
                "evidence_links": "for each key change, risk, and event, cite aliases from evidence_catalog only",
            },
        },
        sort_keys=True,
    )


def _build_alias_catalog(context: SummaryContext) -> list[dict[str, object]]:
    return [
        {
            "alias": f"E{idx}",
            "span_id": item["id"],
            "document_id": item["document_id"],
            "chunk_id": item["chunk_id"],
            "page_start": item["page_start"],
            "page_end": item["page_end"],
            "excerpt_text": item["excerpt_text"],
        }
        for idx, item in enumerate(context.evidence_links, start=1)
    ]


def _normalize_summary_payload(
    *,
    raw_response: str,
    context: SummaryContext,
    alias_catalog: list[dict[str, object]],
    model_name: str,
) -> str:
    draft = SummaryDraftOutput.model_validate_json(raw_response)
    resolved_by_token = {
        item["alias"]: item
        for item in alias_catalog
    }
    resolved_by_token.update(
        {
            item["span_id"]: item
            for item in alias_catalog
        }
    )
    referenced_items: dict[str, dict[str, object]] = {}

    def _resolve_tokens(items: list[dict[str, object]]) -> list[dict[str, object]]:
        resolved_items: list[dict[str, object]] = []
        for item in items:
            tokens = [token.strip() for token in item["evidence_links"]]
            span_ids: list[str] = []
            for token in tokens:
                resolved = resolved_by_token.get(token)
                if resolved is None:
                    span_ids.append(token)
                    continue
                span_ids.append(resolved["span_id"])
                referenced_items[resolved["span_id"]] = resolved
            resolved_items.append({**item, "evidence_links": span_ids})
        return resolved_items

    payload = {
        "document_id": context.document_id,
        "model_name": model_name,
        "summary": draft.summary,
        "key_changes": _resolve_tokens([item.model_dump() for item in draft.key_changes]),
        "risks": _resolve_tokens([item.model_dump() for item in draft.risks]),
        "events": _resolve_tokens([item.model_dump() for item in draft.events]),
        "what_to_verify": draft.what_to_verify,
        "evidence_links": [
            {
                "id": item["span_id"],
                "document_id": item["document_id"],
                "chunk_id": item["chunk_id"],
                "page_start": item["page_start"],
                "page_end": item["page_end"],
            }
            for item in alias_catalog
            if item["span_id"] in referenced_items
        ],
    }
    return json.dumps(payload, ensure_ascii=True, sort_keys=True)


def _stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
