"""Phase 11 change detection and snapshot assembly over the Phase 10 foundation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import median
from typing import Any

from ..config import Config
from ..db import WriterQueue, connect, configure_pragmas, run_migrations
from .thesis import (
    LocalThesisCondition,
    LocalThesisDefinition,
    ThesisEvaluationRecord,
    load_db_theses,
    load_local_theses,
    sync_local_theses,
)
from .claims import (
    ClaimCredibilitySummary,
    ClaimOutcomeRecord,
    ClaimRecord,
    evaluate_claim_outcomes,
    extract_claims_for_documents,
)

DERIVED_VERSION = "phase14-v1"

HIGHER_IS_BETTER_FIELDS = {
    "revenue_growth_pct",
    "operating_margin_pct",
    "net_margin_pct",
    "cash_flow_margin_pct",
}
LOWER_IS_BETTER_FIELDS = {
    "leverage_ratio_pct",
    "dilution_frequency",
    "governance_change_frequency",
    "event_density",
}
COMPARISON_FIELD_LABELS = {
    "revenue_growth_pct": "Revenue Growth",
    "operating_margin_pct": "Operating Margin",
    "net_margin_pct": "Net Margin",
    "cash_flow_margin_pct": "Cash-Flow Margin",
    "leverage_ratio_pct": "Leverage",
    "dilution_frequency": "Dilution Frequency",
    "governance_change_frequency": "Governance Change Frequency",
    "event_density": "Event Density",
}
DILUTION_EVENT_TYPES = {"capital_raise", "rights_issue", "share_issue", "warrant_issue", "private_placement"}
GOVERNANCE_EVENT_TYPES = {
    "board_change",
    "director_change",
    "commissioner_change",
    "management_change",
    "audit_committee_change",
    "governance_issue",
}


@dataclass(frozen=True)
class CompanyProfile:
    ticker: str
    issuer_name: str
    sector: str | None
    subsector: str | None
    industry: str | None
    listing_board: str | None
    active_status: str | None


@dataclass(frozen=True)
class CompanyDocument:
    document_id: str
    source_name: str
    source_record_id: str
    ticker: str
    title: str | None
    document_type: str | None
    version_group_id: str | None
    source_modified_at: str | None
    updated_at: str
    created_at: str


@dataclass(frozen=True)
class MetricPoint:
    metric_id: str
    document_id: str
    metric_name: str
    value_normalized: float | None
    raw_value_text: str | None
    currency: str | None
    unit_scale: str | None
    fiscal_year: int | None
    fiscal_period: str | None
    period_end_date: str | None
    audit_status: str | None
    consolidation_scope: str | None
    evidence_span_id: str | None


@dataclass(frozen=True)
class EventSignal:
    event_id: str
    event_type: str
    severity: str | None
    rationale: str | None
    evidence_span_id: str | None
    detected_at: str


@dataclass(frozen=True)
class TriggeredRule:
    rule_run_id: str
    rule_name: str
    severity: str
    rationale: str | None
    threshold: str | None
    input_values: dict[str, Any]
    evaluated_at: str


@dataclass(frozen=True)
class AnalysisSummary:
    analysis_run_id: str
    summary: str | None
    what_to_verify: list[str]
    finished_at: str | None


@dataclass(frozen=True)
class DeltaRecord:
    delta_id: str
    delta_key: str
    ticker: str
    from_document_id: str
    to_document_id: str
    delta_kind: str
    metric_name: str
    fiscal_year: int | None
    fiscal_period: str | None
    period_end_date: str | None
    previous_value: float | None
    current_value: float | None
    absolute_change: float | None
    percent_change: float | None
    payload_json: str


@dataclass(frozen=True)
class BaselineChoice:
    kind: str
    document: CompanyDocument
    comparable: bool
    reason: str


@dataclass(frozen=True)
class TimeseriesPoint:
    series_point_id: str
    ticker: str
    series_name: str
    series_key: str
    fiscal_year: int | None
    fiscal_period: str | None
    period_end_date: str | None
    value_normalized: float | None
    document_id: str
    evidence_span_id: str | None


@dataclass(frozen=True)
class ReviewQueueItem:
    review_item_id: str
    item_type: str
    ticker: str
    document_id: str
    source_key: str
    priority: str
    rationale: str | None
    payload_json: str


@dataclass(frozen=True)
class PeerSetRecord:
    peer_set_id: str
    ticker: str
    peer_key: str
    peer_tickers_json: str
    rationale: str


@dataclass(frozen=True)
class SectorRollupRecord:
    sector_rollup_id: str
    sector: str
    subsector: str | None
    rollup_key: str
    payload_json: str


@dataclass(frozen=True)
class ComparableCompanyState:
    ticker: str
    issuer_name: str
    sector: str | None
    subsector: str | None
    latest_document_id: str
    latest_as_of: str | None
    baseline_document_id: str | None
    baseline_kind: str
    historical_comparable: bool
    fiscal_year: int | None
    fiscal_period: str | None
    period_end_date: str | None
    currency: str | None
    unit_scale: str | None
    consolidation_scope: str | None
    metrics: dict[str, float | None]
    warning_signal_count: int
    critical_signal_count: int


@dataclass(frozen=True)
class CompanyIntelligenceBundle:
    profile: CompanyProfile
    latest_document: CompanyDocument
    baseline: BaselineChoice | None
    latest_analysis: AnalysisSummary | None
    deltas: list[DeltaRecord]
    timeseries: list[TimeseriesPoint]
    triggered_rules: list[TriggeredRule]
    events: list[EventSignal]
    claims: list[ClaimRecord]
    claim_outcomes: list[ClaimOutcomeRecord]
    credibility_summary: ClaimCredibilitySummary
    thesis_evaluations: list[ThesisEvaluationRecord]
    snapshot_payload_json: str
    brief_payload_json: str
    review_items: list[ReviewQueueItem]
    peer_set: PeerSetRecord
    peer_view: dict[str, Any]
    sector_rollup: SectorRollupRecord
    sector_view: dict[str, Any]


@dataclass(frozen=True)
class IntelligenceRunResult:
    selected_tickers: int = 0
    deltas: int = 0
    snapshots: int = 0
    timeseries: int = 0
    briefs: int = 0
    review_items: int = 0
    peer_sets: int = 0
    sector_rollups: int = 0
    thesis_evaluations: int = 0
    claims: int = 0
    claim_outcomes: int = 0


def run_intelligence(
    cfg: Config,
    *,
    ticker: str | None = None,
    limit: int = 25,
) -> IntelligenceRunResult:
    cfg.ensure_dirs()

    result = IntelligenceRunResult()
    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
        local_theses = load_local_theses(cfg.theses.definition_path)
        wq.submit(lambda conn, theses=local_theses: sync_local_theses(conn, theses=theses))
        reader = connect(cfg.paths.db_path, read_only=True)
        try:
            configure_pragmas(reader, cfg)
            tickers = _select_tickers(reader, ticker=ticker, limit=limit)
            result = IntelligenceRunResult(selected_tickers=len(tickers))
            for ticker_value in tickers:
                bundle = _build_bundle(reader, ticker_value)
                if bundle is None:
                    continue
                counts = wq.submit(lambda conn, b=bundle: _persist_bundle(conn, b))
                result = IntelligenceRunResult(
                    selected_tickers=result.selected_tickers,
                    deltas=result.deltas + counts["deltas"],
                    snapshots=result.snapshots + counts["snapshots"],
                    timeseries=result.timeseries + counts["timeseries"],
                    briefs=result.briefs + counts["briefs"],
                    review_items=result.review_items + counts["review_items"],
                    peer_sets=result.peer_sets + counts["peer_sets"],
                    sector_rollups=result.sector_rollups + counts["sector_rollups"],
                    thesis_evaluations=result.thesis_evaluations + counts["thesis_evaluations"],
                    claims=result.claims + counts["claims"],
                    claim_outcomes=result.claim_outcomes + counts["claim_outcomes"],
                )
        finally:
            reader.close()
    return result


def _select_tickers(conn: sqlite3.Connection, *, ticker: str | None, limit: int) -> list[str]:
    params: list[object] = []
    where = "WHERE d.process_state = 'analyzed' AND d.ticker IS NOT NULL"
    if ticker:
        where += " AND d.ticker = ?"
        params.append(ticker)
    rows = conn.execute(
        f"""
        SELECT d.ticker, MAX(COALESCE(d.source_modified_at, d.updated_at, d.created_at)) AS latest_seen_at
        FROM documents d
        {where}
        GROUP BY d.ticker
        ORDER BY latest_seen_at DESC, d.ticker ASC
        LIMIT ?
        """,
        tuple(params + [limit]),
    ).fetchall()
    return [row["ticker"] for row in rows]


def _build_bundle(conn: sqlite3.Connection, ticker: str) -> CompanyIntelligenceBundle | None:
    profile = _load_company_profile(conn, ticker)
    if profile is None:
        return None
    documents = _load_company_documents(conn, ticker, limit=25)
    if not documents:
        return None
    latest_document = documents[0]
    baseline = _select_baseline(latest_document, documents[1:])
    latest_metrics = _load_document_metrics(conn, latest_document.document_id)
    if not latest_metrics:
        return None
    previous_metrics = _load_document_metrics(conn, baseline.document.document_id) if baseline is not None else []
    latest_events = _load_document_events(conn, latest_document.document_id)
    previous_events = _load_document_events(conn, baseline.document.document_id) if baseline is not None else []
    latest_rules = _load_triggered_rules(conn, latest_document.document_id)
    previous_rules = _load_triggered_rules(conn, baseline.document.document_id) if baseline is not None else []
    latest_analysis = _load_latest_analysis(conn, latest_document.document_id)
    previous_analysis = _load_latest_analysis(conn, baseline.document.document_id) if baseline is not None else None
    current_company_state = _build_comparable_company_state(
        conn,
        profile=profile,
        latest_document=latest_document,
        latest_metrics=latest_metrics,
        baseline=baseline,
        latest_events=latest_events,
        latest_rules=latest_rules,
    )
    theses = load_db_theses(conn, ticker=ticker)
    deltas = _build_deltas(
        ticker=ticker,
        baseline=baseline,
        to_document=latest_document,
        previous_metrics=previous_metrics,
        current_metrics=latest_metrics,
        previous_events=previous_events,
        current_events=latest_events,
        previous_rules=previous_rules,
        current_rules=latest_rules,
        previous_analysis=previous_analysis,
        current_analysis=latest_analysis,
    )
    timeseries = _build_timeseries(ticker=ticker, metrics=_load_ticker_metrics(conn, ticker))
    claims = extract_claims_for_documents(
        conn,
        ticker=ticker,
        documents=documents,
        latest_document_id=latest_document.document_id,
    )
    claim_outcomes, credibility_summary = evaluate_claim_outcomes(
        conn,
        claims=claims,
        latest_document_id=latest_document.document_id,
        assessment_date=latest_document.source_modified_at or latest_document.updated_at,
    )
    conflict_items = _build_conflict_review_items(
        ticker=ticker,
        latest_document=latest_document,
        baseline=baseline,
        deltas=deltas,
    )
    thesis_evaluations = _evaluate_theses(
        theses=theses,
        ticker=ticker,
        latest_document=latest_document,
        baseline=baseline,
        deltas=deltas,
        events=latest_events,
        triggered_rules=latest_rules,
        review_items=conflict_items,
    )
    thesis_items = _build_thesis_review_items(
        ticker=ticker,
        document_id=latest_document.document_id,
        evaluations=thesis_evaluations,
    )
    claim_items = _build_claim_review_items(
        ticker=ticker,
        document_id=latest_document.document_id,
        claim_outcomes=claim_outcomes,
    )
    peer_set, peer_view = _build_peer_set(conn, profile, current_company_state)
    sector_rollup, sector_view = _build_sector_rollup(conn, profile, current_company_state)
    snapshot_payload_json = _build_snapshot_payload_json(
        profile=profile,
        latest_document=latest_document,
        baseline=baseline,
        latest_analysis=latest_analysis,
        latest_metrics=latest_metrics,
        deltas=deltas,
        events=latest_events,
        triggered_rules=latest_rules,
        conflicts=conflict_items + thesis_items + claim_items,
        claims=claims,
        claim_outcomes=claim_outcomes,
        credibility_summary=credibility_summary,
        thesis_evaluations=thesis_evaluations,
        peer_view=peer_view,
        sector_view=sector_view,
    )
    brief_payload_json = _build_brief_payload_json(
        ticker=ticker,
        latest_analysis=latest_analysis,
        deltas=deltas,
        events=latest_events,
        triggered_rules=latest_rules,
        baseline=baseline,
        credibility_summary=credibility_summary,
        thesis_evaluations=thesis_evaluations,
        peer_view=peer_view,
        sector_view=sector_view,
    )
    review_items = _build_review_items(
        ticker=ticker,
        document_id=latest_document.document_id,
        events=latest_events,
        triggered_rules=latest_rules,
    )
    review_items.extend(conflict_items)
    review_items.extend(thesis_items)
    review_items.extend(claim_items)
    return CompanyIntelligenceBundle(
        profile=profile,
        latest_document=latest_document,
        baseline=baseline,
        latest_analysis=latest_analysis,
        deltas=deltas,
        timeseries=timeseries,
        triggered_rules=latest_rules,
        events=latest_events,
        claims=claims,
        claim_outcomes=claim_outcomes,
        credibility_summary=credibility_summary,
        thesis_evaluations=thesis_evaluations,
        snapshot_payload_json=snapshot_payload_json,
        brief_payload_json=brief_payload_json,
        review_items=review_items,
        peer_set=peer_set,
        peer_view=peer_view,
        sector_rollup=sector_rollup,
        sector_view=sector_view,
    )


def _load_company_profile(conn: sqlite3.Connection, ticker: str) -> CompanyProfile | None:
    row = conn.execute(
        """
        SELECT ticker, issuer_name, sector, subsector, industry, listing_board, active_status
        FROM companies
        WHERE ticker = ?
        LIMIT 1
        """,
        (ticker,),
    ).fetchone()
    if row is None:
        return None
    return CompanyProfile(
        ticker=row["ticker"],
        issuer_name=row["issuer_name"],
        sector=row["sector"],
        subsector=row["subsector"],
        industry=row["industry"],
        listing_board=row["listing_board"],
        active_status=row["active_status"],
    )


def _load_company_documents(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    limit: int,
) -> list[CompanyDocument]:
    rows = conn.execute(
        """
        SELECT
            d.document_id,
            d.source_name,
            d.source_record_id,
            d.ticker,
            dc.title,
            d.document_type,
            d.version_group_id,
            d.source_modified_at,
            d.updated_at,
            d.created_at
        FROM documents d
        LEFT JOIN document_candidates dc
          ON dc.source_name = d.source_name
         AND dc.source_record_id = d.source_record_id
        WHERE d.ticker = ?
          AND d.process_state = 'analyzed'
        ORDER BY COALESCE(d.source_modified_at, d.updated_at, d.created_at) DESC, d.document_id DESC
        LIMIT ?
        """,
        (ticker, limit),
    ).fetchall()
    return [
        CompanyDocument(
            document_id=row["document_id"],
            source_name=row["source_name"],
            source_record_id=row["source_record_id"],
            ticker=row["ticker"],
            title=row["title"],
            document_type=row["document_type"],
            version_group_id=row["version_group_id"],
            source_modified_at=row["source_modified_at"],
            updated_at=row["updated_at"],
            created_at=row["created_at"],
        )
        for row in rows
    ]


def _select_baseline(latest_document: CompanyDocument, prior_documents: list[CompanyDocument]) -> BaselineChoice | None:
    revision_candidates = [
        document
        for document in prior_documents
        if latest_document.version_group_id
        and document.version_group_id == latest_document.version_group_id
    ]
    if revision_candidates:
        return BaselineChoice(
            kind="revision",
            document=revision_candidates[0],
            comparable=True,
            reason="Using the most recent prior revision in the same version group.",
        )

    comparable_candidates = [
        document
        for document in prior_documents
        if document.source_name == latest_document.source_name
        and document.document_type == latest_document.document_type
    ]
    if comparable_candidates:
        return BaselineChoice(
            kind="comparable_filing",
            document=comparable_candidates[0],
            comparable=True,
            reason="Using the most recent prior filing of the same source and document type.",
        )

    source_candidates = [
        document
        for document in prior_documents
        if document.source_name == latest_document.source_name
    ]
    if source_candidates:
        return BaselineChoice(
            kind="source_prior",
            document=source_candidates[0],
            comparable=False,
            reason="Using the most recent prior filing from the same source because no exact comparable baseline was found.",
        )
    return None


def _load_document_metrics(conn: sqlite3.Connection, document_id: str) -> list[MetricPoint]:
    rows = conn.execute(
        """
        SELECT
            metric_id,
            document_id,
            metric_name,
            value_normalized,
            raw_value_text,
            currency,
            unit_scale,
            fiscal_year,
            fiscal_period,
            period_end_date,
            audit_status,
            consolidation_scope,
            evidence_span_id
        FROM metric_items
        WHERE document_id = ?
        ORDER BY metric_name ASC, fiscal_year DESC, fiscal_period ASC
        """,
        (document_id,),
    ).fetchall()
    return [
        MetricPoint(
            metric_id=row["metric_id"],
            document_id=row["document_id"],
            metric_name=row["metric_name"],
            value_normalized=row["value_normalized"],
            raw_value_text=row["raw_value_text"],
            currency=row["currency"],
            unit_scale=row["unit_scale"],
            fiscal_year=row["fiscal_year"],
            fiscal_period=row["fiscal_period"],
            period_end_date=row["period_end_date"],
            audit_status=row["audit_status"],
            consolidation_scope=row["consolidation_scope"],
            evidence_span_id=row["evidence_span_id"],
        )
        for row in rows
    ]


def _load_ticker_metrics(conn: sqlite3.Connection, ticker: str) -> list[MetricPoint]:
    rows = conn.execute(
        """
        SELECT
            mi.metric_id,
            mi.document_id,
            mi.metric_name,
            mi.value_normalized,
            mi.raw_value_text,
            mi.currency,
            mi.unit_scale,
            mi.fiscal_year,
            mi.fiscal_period,
            mi.period_end_date,
            mi.audit_status,
            mi.consolidation_scope,
            mi.evidence_span_id
        FROM metric_items mi
        JOIN documents d ON d.document_id = mi.document_id
        WHERE mi.ticker = ?
          AND d.process_state IN ('extracted', 'analyzed')
        ORDER BY COALESCE(mi.period_end_date, ''), mi.metric_name ASC, mi.document_id ASC
        """,
        (ticker,),
    ).fetchall()
    return [
        MetricPoint(
            metric_id=row["metric_id"],
            document_id=row["document_id"],
            metric_name=row["metric_name"],
            value_normalized=row["value_normalized"],
            raw_value_text=row["raw_value_text"],
            currency=row["currency"],
            unit_scale=row["unit_scale"],
            fiscal_year=row["fiscal_year"],
            fiscal_period=row["fiscal_period"],
            period_end_date=row["period_end_date"],
            audit_status=row["audit_status"],
            consolidation_scope=row["consolidation_scope"],
            evidence_span_id=row["evidence_span_id"],
        )
        for row in rows
    ]


def _load_document_events(conn: sqlite3.Connection, document_id: str) -> list[EventSignal]:
    rows = conn.execute(
        """
        SELECT event_id, event_type, severity, rationale, evidence_span_id, detected_at
        FROM events
        WHERE document_id = ?
        ORDER BY detected_at DESC, event_id ASC
        """,
        (document_id,),
    ).fetchall()
    return [
        EventSignal(
            event_id=row["event_id"],
            event_type=row["event_type"],
            severity=row["severity"],
            rationale=row["rationale"],
            evidence_span_id=row["evidence_span_id"],
            detected_at=row["detected_at"],
        )
        for row in rows
    ]


def _load_triggered_rules(conn: sqlite3.Connection, document_id: str) -> list[TriggeredRule]:
    rows = conn.execute(
        """
        SELECT rule_run_id, rule_name, severity, rationale, threshold, input_values, evaluated_at
        FROM rule_runs
        WHERE document_id = ?
          AND triggered = 1
        ORDER BY evaluated_at DESC, rule_name ASC
        """,
        (document_id,),
    ).fetchall()
    return [
        TriggeredRule(
            rule_run_id=row["rule_run_id"],
            rule_name=row["rule_name"],
            severity=row["severity"],
            rationale=row["rationale"],
            threshold=row["threshold"],
            input_values=json.loads(row["input_values"] or "{}"),
            evaluated_at=row["evaluated_at"],
        )
        for row in rows
    ]


def _load_latest_analysis(conn: sqlite3.Connection, document_id: str) -> AnalysisSummary | None:
    row = conn.execute(
        """
        SELECT analysis_run_id, output_json, finished_at
        FROM analysis_runs
        WHERE document_id = ?
        ORDER BY COALESCE(finished_at, started_at) DESC, analysis_run_id DESC
        LIMIT 1
        """,
        (document_id,),
    ).fetchone()
    if row is None:
        return None
    payload = json.loads(row["output_json"])
    return AnalysisSummary(
        analysis_run_id=row["analysis_run_id"],
        summary=payload.get("summary"),
        what_to_verify=list(payload.get("what_to_verify") or []),
        finished_at=row["finished_at"],
    )


def _build_deltas(
    *,
    ticker: str,
    baseline: BaselineChoice | None,
    to_document: CompanyDocument,
    previous_metrics: list[MetricPoint],
    current_metrics: list[MetricPoint],
    previous_events: list[EventSignal],
    current_events: list[EventSignal],
    previous_rules: list[TriggeredRule],
    current_rules: list[TriggeredRule],
    previous_analysis: AnalysisSummary | None,
    current_analysis: AnalysisSummary | None,
) -> list[DeltaRecord]:
    deltas: list[DeltaRecord] = []
    if baseline is None:
        return deltas

    from_document = baseline.document
    previous_by_key = {_metric_key(metric): metric for metric in previous_metrics}
    current_by_key = {_metric_key(metric): metric for metric in current_metrics}
    for key in sorted(set(previous_by_key) & set(current_by_key)):
        previous_metric = previous_by_key[key]
        current_metric = current_by_key[key]
        if previous_metric.value_normalized is None or current_metric.value_normalized is None:
            continue
        if previous_metric.value_normalized == current_metric.value_normalized:
            continue
        absolute_change = current_metric.value_normalized - previous_metric.value_normalized
        percent_change = None
        if previous_metric.value_normalized != 0:
            percent_change = (absolute_change / abs(previous_metric.value_normalized)) * 100.0
        payload = {
            "ticker": ticker,
            "baseline_kind": baseline.kind,
            "comparable": baseline.comparable,
            "metric_name": current_metric.metric_name,
            "previous_metric_id": previous_metric.metric_id,
            "current_metric_id": current_metric.metric_id,
            "previous_evidence_span_id": previous_metric.evidence_span_id,
            "current_evidence_span_id": current_metric.evidence_span_id,
            "previous_value": previous_metric.value_normalized,
            "current_value": current_metric.value_normalized,
            "absolute_change": absolute_change,
            "percent_change": percent_change,
            "period": {
                "fiscal_year": current_metric.fiscal_year,
                "fiscal_period": current_metric.fiscal_period,
                "period_end_date": current_metric.period_end_date,
            },
        }
        delta_key = "|".join(str(part or "") for part in key)
        deltas.append(
            DeltaRecord(
                delta_id=_stable_id(
                    "delta",
                    ticker,
                    from_document.document_id,
                    to_document.document_id,
                    delta_key,
                    DERIVED_VERSION,
                ),
                delta_key=delta_key,
                ticker=ticker,
                from_document_id=from_document.document_id,
                to_document_id=to_document.document_id,
                delta_kind="metric_change",
                metric_name=current_metric.metric_name,
                fiscal_year=current_metric.fiscal_year,
                fiscal_period=current_metric.fiscal_period,
                period_end_date=current_metric.period_end_date,
                previous_value=previous_metric.value_normalized,
                current_value=current_metric.value_normalized,
                absolute_change=absolute_change,
                percent_change=percent_change,
                payload_json=json.dumps(payload, sort_keys=True),
            )
        )
    deltas.extend(
        _build_event_deltas(
            ticker=ticker,
            baseline=baseline,
            from_document=from_document,
            to_document=to_document,
            previous_events=previous_events,
            current_events=current_events,
        )
    )
    deltas.extend(
        _build_rule_deltas(
            ticker=ticker,
            baseline=baseline,
            from_document=from_document,
            to_document=to_document,
            previous_rules=previous_rules,
            current_rules=current_rules,
        )
    )
    narrative_delta = _build_narrative_delta(
        ticker=ticker,
        baseline=baseline,
        from_document=from_document,
        to_document=to_document,
        previous_analysis=previous_analysis,
        current_analysis=current_analysis,
    )
    if narrative_delta is not None:
        deltas.append(narrative_delta)
    return deltas


def _build_event_deltas(
    *,
    ticker: str,
    baseline: BaselineChoice,
    from_document: CompanyDocument,
    to_document: CompanyDocument,
    previous_events: list[EventSignal],
    current_events: list[EventSignal],
) -> list[DeltaRecord]:
    previous_by_type = {event.event_type: event for event in previous_events}
    current_by_type = {event.event_type: event for event in current_events}
    deltas: list[DeltaRecord] = []
    for event_type in sorted(set(current_by_type) - set(previous_by_type)):
        event = current_by_type[event_type]
        deltas.append(
            DeltaRecord(
                delta_id=_stable_id("delta", ticker, from_document.document_id, to_document.document_id, "event_addition", event_type, DERIVED_VERSION),
                delta_key=f"event_added|{event_type}",
                ticker=ticker,
                from_document_id=from_document.document_id,
                to_document_id=to_document.document_id,
                delta_kind="event_addition",
                metric_name=event_type,
                fiscal_year=None,
                fiscal_period=None,
                period_end_date=None,
                previous_value=None,
                current_value=None,
                absolute_change=None,
                percent_change=None,
                payload_json=json.dumps(
                    {
                        "baseline_kind": baseline.kind,
                        "event_type": event_type,
                        "current_event_id": event.event_id,
                        "current_evidence_span_id": event.evidence_span_id,
                        "severity": event.severity,
                        "rationale": event.rationale,
                    },
                    sort_keys=True,
                ),
            )
        )
    for event_type in sorted(set(previous_by_type) - set(current_by_type)):
        event = previous_by_type[event_type]
        deltas.append(
            DeltaRecord(
                delta_id=_stable_id("delta", ticker, from_document.document_id, to_document.document_id, "event_resolution", event_type, DERIVED_VERSION),
                delta_key=f"event_resolved|{event_type}",
                ticker=ticker,
                from_document_id=from_document.document_id,
                to_document_id=to_document.document_id,
                delta_kind="event_resolution",
                metric_name=event_type,
                fiscal_year=None,
                fiscal_period=None,
                period_end_date=None,
                previous_value=None,
                current_value=None,
                absolute_change=None,
                percent_change=None,
                payload_json=json.dumps(
                    {
                        "baseline_kind": baseline.kind,
                        "event_type": event_type,
                        "previous_event_id": event.event_id,
                        "previous_evidence_span_id": event.evidence_span_id,
                        "severity": event.severity,
                        "rationale": event.rationale,
                    },
                    sort_keys=True,
                ),
            )
        )
    return deltas


def _build_rule_deltas(
    *,
    ticker: str,
    baseline: BaselineChoice,
    from_document: CompanyDocument,
    to_document: CompanyDocument,
    previous_rules: list[TriggeredRule],
    current_rules: list[TriggeredRule],
) -> list[DeltaRecord]:
    previous_by_name = {rule.rule_name: rule for rule in previous_rules}
    current_by_name = {rule.rule_name: rule for rule in current_rules}
    deltas: list[DeltaRecord] = []
    for rule_name in sorted(set(current_by_name) - set(previous_by_name)):
        rule = current_by_name[rule_name]
        deltas.append(
            DeltaRecord(
                delta_id=_stable_id("delta", ticker, from_document.document_id, to_document.document_id, "rule_addition", rule_name, DERIVED_VERSION),
                delta_key=f"rule_added|{rule_name}",
                ticker=ticker,
                from_document_id=from_document.document_id,
                to_document_id=to_document.document_id,
                delta_kind="rule_addition",
                metric_name=rule_name,
                fiscal_year=None,
                fiscal_period=None,
                period_end_date=None,
                previous_value=None,
                current_value=None,
                absolute_change=None,
                percent_change=None,
                payload_json=json.dumps(
                    {
                        "baseline_kind": baseline.kind,
                        "rule_name": rule_name,
                        "current_rule_run_id": rule.rule_run_id,
                        "severity": rule.severity,
                        "threshold": rule.threshold,
                        "rationale": rule.rationale,
                    },
                    sort_keys=True,
                ),
            )
        )
    for rule_name in sorted(set(previous_by_name) - set(current_by_name)):
        rule = previous_by_name[rule_name]
        deltas.append(
            DeltaRecord(
                delta_id=_stable_id("delta", ticker, from_document.document_id, to_document.document_id, "rule_resolution", rule_name, DERIVED_VERSION),
                delta_key=f"rule_resolved|{rule_name}",
                ticker=ticker,
                from_document_id=from_document.document_id,
                to_document_id=to_document.document_id,
                delta_kind="rule_resolution",
                metric_name=rule_name,
                fiscal_year=None,
                fiscal_period=None,
                period_end_date=None,
                previous_value=None,
                current_value=None,
                absolute_change=None,
                percent_change=None,
                payload_json=json.dumps(
                    {
                        "baseline_kind": baseline.kind,
                        "rule_name": rule_name,
                        "previous_rule_run_id": rule.rule_run_id,
                        "severity": rule.severity,
                        "threshold": rule.threshold,
                        "rationale": rule.rationale,
                    },
                    sort_keys=True,
                ),
            )
        )
    return deltas


def _build_narrative_delta(
    *,
    ticker: str,
    baseline: BaselineChoice,
    from_document: CompanyDocument,
    to_document: CompanyDocument,
    previous_analysis: AnalysisSummary | None,
    current_analysis: AnalysisSummary | None,
) -> DeltaRecord | None:
    previous_summary = previous_analysis.summary.strip() if previous_analysis and previous_analysis.summary else None
    current_summary = current_analysis.summary.strip() if current_analysis and current_analysis.summary else None
    if not previous_summary or not current_summary or previous_summary == current_summary:
        return None
    return DeltaRecord(
        delta_id=_stable_id("delta", ticker, from_document.document_id, to_document.document_id, "narrative_change", DERIVED_VERSION),
        delta_key="narrative_change",
        ticker=ticker,
        from_document_id=from_document.document_id,
        to_document_id=to_document.document_id,
        delta_kind="narrative_change",
        metric_name="summary",
        fiscal_year=None,
        fiscal_period=None,
        period_end_date=None,
        previous_value=None,
        current_value=None,
        absolute_change=None,
        percent_change=None,
        payload_json=json.dumps(
            {
                "baseline_kind": baseline.kind,
                "previous_analysis_run_id": previous_analysis.analysis_run_id if previous_analysis else None,
                "current_analysis_run_id": current_analysis.analysis_run_id if current_analysis else None,
                "previous_summary": previous_summary,
                "current_summary": current_summary,
            },
            sort_keys=True,
        ),
    )


def _build_timeseries(*, ticker: str, metrics: list[MetricPoint]) -> list[TimeseriesPoint]:
    points: list[TimeseriesPoint] = []
    for metric in metrics:
        series_key = "|".join(
            str(part or "")
            for part in (metric.metric_name, metric.fiscal_year, metric.fiscal_period, metric.period_end_date)
        )
        points.append(
            TimeseriesPoint(
                series_point_id=_stable_id("series", ticker, metric.document_id, series_key, DERIVED_VERSION),
                ticker=ticker,
                series_name=metric.metric_name,
                series_key=series_key,
                fiscal_year=metric.fiscal_year,
                fiscal_period=metric.fiscal_period,
                period_end_date=metric.period_end_date,
                value_normalized=metric.value_normalized,
                document_id=metric.document_id,
                evidence_span_id=metric.evidence_span_id,
            )
        )
    return points


def _build_snapshot_payload_json(
    *,
    profile: CompanyProfile,
    latest_document: CompanyDocument,
    baseline: BaselineChoice | None,
    latest_analysis: AnalysisSummary | None,
    latest_metrics: list[MetricPoint],
    deltas: list[DeltaRecord],
    events: list[EventSignal],
    triggered_rules: list[TriggeredRule],
    conflicts: list[ReviewQueueItem],
    claims: list[ClaimRecord],
    claim_outcomes: list[ClaimOutcomeRecord],
    credibility_summary: ClaimCredibilitySummary,
    thesis_evaluations: list[ThesisEvaluationRecord],
    peer_view: dict[str, Any],
    sector_view: dict[str, Any],
) -> str:
    health = _build_health_state(
        baseline=baseline,
        deltas=deltas,
        events=events,
        triggered_rules=triggered_rules,
        conflicts=conflicts,
        credibility_summary=credibility_summary,
        thesis_evaluations=thesis_evaluations,
    )
    delta_counts: dict[str, int] = {}
    for delta in deltas:
        delta_counts[delta.delta_kind] = delta_counts.get(delta.delta_kind, 0) + 1
    if peer_view.get("comparability_warnings"):
        warning_count = len(peer_view["comparability_warnings"])
        health["reasons"].append(f"Peer comparison has {warning_count} comparability warning(s).")
    payload = {
        "ticker": profile.ticker,
        "issuer_name": profile.issuer_name,
        "sector": profile.sector,
        "subsector": profile.subsector,
        "industry": profile.industry,
        "listing_board": profile.listing_board,
        "active_status": profile.active_status,
        "latest_document": {
            "document_id": latest_document.document_id,
            "source_name": latest_document.source_name,
            "source_record_id": latest_document.source_record_id,
            "document_type": latest_document.document_type,
            "title": latest_document.title,
            "as_of": latest_document.source_modified_at or latest_document.updated_at,
        },
        "baseline": (
            {
                "kind": baseline.kind,
                "comparable": baseline.comparable,
                "reason": baseline.reason,
                "document_id": baseline.document.document_id,
                "source_name": baseline.document.source_name,
                "source_record_id": baseline.document.source_record_id,
                "document_type": baseline.document.document_type,
                "as_of": baseline.document.source_modified_at or baseline.document.updated_at,
            }
            if baseline is not None
            else {
                "kind": "none",
                "comparable": False,
                "reason": "No prior analyzed filing is available for comparison.",
            }
        ),
        "top_card": health,
        "latest_summary": latest_analysis.summary if latest_analysis else None,
        "what_to_verify": latest_analysis.what_to_verify if latest_analysis else [],
        "management_credibility": {
            "total_claims": credibility_summary.total_claims,
            "achieved": credibility_summary.achieved,
            "partially_supported": credibility_summary.partially_supported,
            "missed": credibility_summary.missed,
            "contradicted": credibility_summary.contradicted,
            "unresolved": credibility_summary.unresolved,
            "achieved_rate": credibility_summary.achieved_rate,
            "missed_rate": credibility_summary.missed_rate,
            "unresolved_rate": credibility_summary.unresolved_rate,
            "recycled_claim_count": credibility_summary.recycled_claim_count,
            "credibility_state": credibility_summary.credibility_state,
        },
        "claims": [
            {
                "claim_id": claim.claim_id,
                "claim_type": claim.claim_type,
                "claim_text": claim.claim_text,
                "claim_topic": claim.claim_topic,
                "claim_subject": claim.claim_subject,
                "claim_time_horizon": claim.claim_time_horizon,
                "claim_confidence": claim.claim_confidence,
                "status": claim.status,
            }
            for claim in claims
        ],
        "claim_outcomes": [
            {
                "claim_id": outcome.claim_id,
                "outcome_status": outcome.outcome_status,
                "rationale": outcome.rationale,
                "details": json.loads(outcome.payload_json),
            }
            for outcome in claim_outcomes
        ],
        "thesis_evaluations": [
            {
                "thesis_id": evaluation.thesis_id,
                "status": evaluation.status,
                "score": evaluation.score,
                "rationale": evaluation.rationale,
                "details": json.loads(evaluation.payload_json),
            }
            for evaluation in thesis_evaluations
        ],
        "delta_count_by_kind": delta_counts,
        "metrics": [
            {
                "metric_name": metric.metric_name,
                "value_normalized": metric.value_normalized,
                "raw_value_text": metric.raw_value_text,
                "currency": metric.currency,
                "unit_scale": metric.unit_scale,
                "fiscal_year": metric.fiscal_year,
                "fiscal_period": metric.fiscal_period,
                "period_end_date": metric.period_end_date,
            }
            for metric in latest_metrics
        ],
        "top_deltas": [
            {
                "delta_kind": delta.delta_kind,
                "metric_name": delta.metric_name,
                "absolute_change": delta.absolute_change,
                "percent_change": delta.percent_change,
                "previous_value": delta.previous_value,
                "current_value": delta.current_value,
                "evidence": json.loads(delta.payload_json),
            }
            for delta in deltas[:10]
        ],
        "events": [
            {
                "event_type": event.event_type,
                "severity": event.severity,
                "rationale": event.rationale,
            }
            for event in events[:10]
        ],
        "triggered_rules": [
            {
                "rule_name": rule.rule_name,
                "severity": rule.severity,
                "rationale": rule.rationale,
                "threshold": rule.threshold,
            }
            for rule in triggered_rules[:10]
        ],
        "review_required": [
            {
                "item_type": item.item_type,
                "priority": item.priority,
                "rationale": item.rationale,
                "payload": json.loads(item.payload_json),
            }
            for item in conflicts
        ],
        "peer_comparison": peer_view,
        "sector_context": sector_view,
    }
    return json.dumps(payload, sort_keys=True)


def _build_brief_payload_json(
    *,
    ticker: str,
    latest_analysis: AnalysisSummary | None,
    deltas: list[DeltaRecord],
    events: list[EventSignal],
    triggered_rules: list[TriggeredRule],
    baseline: BaselineChoice | None,
    credibility_summary: ClaimCredibilitySummary,
    thesis_evaluations: list[ThesisEvaluationRecord],
    peer_view: dict[str, Any],
    sector_view: dict[str, Any],
) -> str:
    headline = latest_analysis.summary if latest_analysis and latest_analysis.summary else "No local summary available yet."
    payload = {
        "ticker": ticker,
        "headline": headline,
        "baseline_kind": baseline.kind if baseline is not None else "none",
        "key_delta_metrics": [delta.metric_name for delta in deltas[:5]],
        "open_risks": [rule.rule_name for rule in triggered_rules[:5]],
        "recent_events": [event.event_type for event in events[:5]],
        "verify_next": latest_analysis.what_to_verify[:5] if latest_analysis else [],
        "management_credibility_state": credibility_summary.credibility_state,
        "thesis_statuses": [evaluation.status for evaluation in thesis_evaluations],
        "peer_highlights": [anomaly["summary"] for anomaly in peer_view.get("anomalies", [])[:3]],
        "sector_heatmap": sector_view.get("heatmap", {}),
    }
    return json.dumps(payload, sort_keys=True)


def _build_review_items(
    *,
    ticker: str,
    document_id: str,
    events: list[EventSignal],
    triggered_rules: list[TriggeredRule],
) -> list[ReviewQueueItem]:
    items: list[ReviewQueueItem] = []
    for rule in triggered_rules:
        items.append(
            ReviewQueueItem(
                review_item_id=_stable_id("review", "rule", rule.rule_run_id, DERIVED_VERSION),
                item_type="rule_trigger",
                ticker=ticker,
                document_id=document_id,
                source_key=rule.rule_run_id,
                priority=_priority_for_severity(rule.severity),
                rationale=rule.rationale,
                payload_json=json.dumps(
                    {
                        "rule_name": rule.rule_name,
                        "severity": rule.severity,
                        "threshold": rule.threshold,
                        "input_values": rule.input_values,
                    },
                    sort_keys=True,
                ),
            )
        )
    for event in events:
        severity = event.severity or "info"
        if severity not in {"warning", "critical"}:
            continue
        items.append(
            ReviewQueueItem(
                review_item_id=_stable_id("review", "event", event.event_id, DERIVED_VERSION),
                item_type="event_signal",
                ticker=ticker,
                document_id=document_id,
                source_key=event.event_id,
                priority=_priority_for_severity(severity),
                rationale=event.rationale,
                payload_json=json.dumps(
                    {
                        "event_type": event.event_type,
                        "severity": severity,
                        "evidence_span_id": event.evidence_span_id,
                    },
                    sort_keys=True,
                ),
            )
        )
    return items


def _build_conflict_review_items(
    *,
    ticker: str,
    latest_document: CompanyDocument,
    baseline: BaselineChoice | None,
    deltas: list[DeltaRecord],
) -> list[ReviewQueueItem]:
    if baseline is None or baseline.kind != "revision":
        return []
    changed_metrics = [
        delta.metric_name
        for delta in deltas
        if delta.delta_kind == "metric_change" and delta.metric_name
    ]
    if not changed_metrics:
        return []
    return [
        ReviewQueueItem(
            review_item_id=_stable_id("review", "revision", latest_document.document_id, DERIVED_VERSION),
            item_type="revision_change",
            ticker=ticker,
            document_id=latest_document.document_id,
            source_key=f"revision:{baseline.document.document_id}->{latest_document.document_id}",
            priority="medium",
            rationale="A revised filing changed one or more tracked metrics relative to the prior revision.",
            payload_json=json.dumps(
                {
                    "baseline_document_id": baseline.document.document_id,
                    "current_document_id": latest_document.document_id,
                    "changed_metrics": changed_metrics,
                },
                sort_keys=True,
            ),
        )
    ]


def _build_health_state(
    *,
    baseline: BaselineChoice | None,
    deltas: list[DeltaRecord],
    events: list[EventSignal],
    triggered_rules: list[TriggeredRule],
    conflicts: list[ReviewQueueItem],
    credibility_summary: ClaimCredibilitySummary,
    thesis_evaluations: list[ThesisEvaluationRecord],
) -> dict[str, Any]:
    reasons: list[str] = []
    overall_state = "stable"
    comparability_state = "none"
    confidence = "medium"

    if baseline is None:
        comparability_state = "not_comparable"
        confidence = "low"
        reasons.append("No prior analyzed baseline exists yet.")
    else:
        comparability_state = baseline.kind
        if baseline.kind == "revision":
            reasons.append("Snapshot is anchored to the most recent prior revision.")
            confidence = "high"
        elif baseline.comparable:
            reasons.append("Snapshot is anchored to a comparable prior filing.")
            confidence = "high"
        else:
            reasons.append("Snapshot used a weaker source-level baseline.")
            confidence = "medium"

    if conflicts:
        overall_state = "review_required"
        confidence = "medium" if confidence == "high" else confidence
        reasons.append("A revised filing changed tracked metrics and should be reviewed.")

    thesis_statuses = {evaluation.status for evaluation in thesis_evaluations}
    if "broken" in thesis_statuses:
        overall_state = "critical"
        reasons.append("At least one configured thesis is broken.")
    elif overall_state not in {"critical", "review_required"} and "weakening" in thesis_statuses:
        overall_state = "watch"
        reasons.append("At least one configured thesis is weakening.")
    elif overall_state == "stable" and "improving" in thesis_statuses:
        reasons.append("A configured thesis is improving.")

    if any(rule.severity == "critical" for rule in triggered_rules) or any((event.severity or "") == "critical" for event in events):
        overall_state = "critical"
        reasons.append("At least one critical rule or event is active.")
    elif overall_state == "stable" and (
        any(rule.severity == "warning" for rule in triggered_rules)
        or any((event.severity or "") == "warning" for event in events)
    ):
        overall_state = "watch"
        reasons.append("Warning-level rules or events are active.")

    positive_factors: list[str] = []
    negative_factors: list[str] = []
    for delta in deltas:
        if delta.delta_kind == "metric_change" and delta.absolute_change is not None:
            if delta.absolute_change > 0:
                positive_factors.append(f"{delta.metric_name} increased")
            elif delta.absolute_change < 0:
                negative_factors.append(f"{delta.metric_name} decreased")
        elif delta.delta_kind in {"rule_addition", "event_addition"} and delta.metric_name:
            negative_factors.append(f"{delta.metric_name} appeared")
        elif delta.delta_kind in {"rule_resolution", "event_resolution"} and delta.metric_name:
            positive_factors.append(f"{delta.metric_name} cleared")
        elif delta.delta_kind == "narrative_change":
            reasons.append("Management narrative changed versus the baseline.")

    leverage_state = "stable"
    if any("debt" in rule.rule_name for rule in triggered_rules) or any("debt" in event.event_type for event in events):
        leverage_state = "warning"
        reasons.append("Debt-related signals remain active.")

    disclosure_quality = "stable"
    if baseline is None:
        disclosure_quality = "low_context"
    elif conflicts:
        disclosure_quality = "review"

    management_credibility = credibility_summary.credibility_state
    if management_credibility == "deteriorating":
        reasons.append("Management claim delivery deteriorated versus prior expectations.")

    return {
        "overall_state": overall_state,
        "comparability_state": comparability_state,
        "confidence": confidence,
        "leverage_state": leverage_state,
        "disclosure_quality": disclosure_quality,
        "management_credibility": management_credibility,
        "thesis_state": _aggregate_thesis_state(thesis_evaluations),
        "decision_state": _decision_state_for(thesis_evaluations, overall_state, confidence),
        "watchlist_priority": _watchlist_priority_for(thesis_evaluations, overall_state, conflicts),
        "positive_factors": positive_factors[:5],
        "negative_factors": negative_factors[:5],
        "reasons": reasons[:8],
    }


def _evaluate_theses(
    *,
    theses: list[LocalThesisDefinition],
    ticker: str,
    latest_document: CompanyDocument,
    baseline: BaselineChoice | None,
    deltas: list[DeltaRecord],
    events: list[EventSignal],
    triggered_rules: list[TriggeredRule],
    review_items: list[ReviewQueueItem],
) -> list[ThesisEvaluationRecord]:
    evaluations: list[ThesisEvaluationRecord] = []
    for thesis in theses:
        matched_positive: list[dict[str, Any]] = []
        matched_warning: list[dict[str, Any]] = []
        matched_stop: list[dict[str, Any]] = []
        unsupported: list[dict[str, Any]] = []
        for condition in thesis.conditions:
            matched = _match_condition(
                condition,
                baseline=baseline,
                deltas=deltas,
                events=events,
                triggered_rules=triggered_rules,
                review_items=review_items,
            )
            record = {
                "severity": condition.severity,
                "kind": condition.kind,
                "payload": condition.payload,
            }
            if matched is None:
                unsupported.append(record)
            elif matched:
                if condition.severity == "positive":
                    matched_positive.append(record)
                elif condition.severity == "stop":
                    matched_stop.append(record)
                else:
                    matched_warning.append(record)
        status, score, rationale = _status_for_thesis(
            matched_positive=matched_positive,
            matched_warning=matched_warning,
            matched_stop=matched_stop,
            unsupported=unsupported,
        )
        payload = {
            "ticker": ticker,
            "baseline_kind": baseline.kind if baseline is not None else "none",
            "matched_positive": matched_positive,
            "matched_warning": matched_warning,
            "matched_stop": matched_stop,
            "unsupported": unsupported,
            "review_cadence": thesis.review_cadence,
            "notes": thesis.notes,
        }
        evaluations.append(
            ThesisEvaluationRecord(
                thesis_evaluation_id=_stable_id("thesis-eval", thesis.thesis_id, ticker, latest_document.document_id, DERIVED_VERSION),
                thesis_id=thesis.thesis_id,
                ticker=ticker,
                document_id=latest_document.document_id,
                status=status,
                score=score,
                rationale=rationale,
                payload_json=json.dumps(payload, sort_keys=True),
            )
        )
    return evaluations


def _match_condition(
    condition: LocalThesisCondition,
    *,
    baseline: BaselineChoice | None,
    deltas: list[DeltaRecord],
    events: list[EventSignal],
    triggered_rules: list[TriggeredRule],
    review_items: list[ReviewQueueItem],
) -> bool | None:
    kind = condition.kind
    payload = condition.payload
    if kind == "metric_delta":
        metric_name = payload.get("metric_name")
        operator = payload.get("operator") or "gte"
        threshold = float(payload.get("value", 0))
        metric_deltas = [
            delta for delta in deltas
            if delta.delta_kind == "metric_change" and delta.metric_name == metric_name and delta.percent_change is not None
        ]
        if not metric_deltas or baseline is None:
            return None
        value = metric_deltas[0].percent_change or 0.0
        return _compare_numeric(value, operator, threshold)
    if kind == "rule_present":
        rule_name = payload.get("rule_name")
        return any(rule.rule_name == rule_name for rule in triggered_rules)
    if kind == "rule_absent":
        rule_name = payload.get("rule_name")
        return not any(rule.rule_name == rule_name for rule in triggered_rules)
    if kind == "event_present":
        event_type = payload.get("event_type")
        return any(event.event_type == event_type for event in events)
    if kind == "event_absent":
        event_type = payload.get("event_type")
        return not any(event.event_type == event_type for event in events)
    if kind == "review_item_present":
        item_type = payload.get("item_type")
        return any(item.item_type == item_type for item in review_items)
    return None


def _status_for_thesis(
    *,
    matched_positive: list[dict[str, Any]],
    matched_warning: list[dict[str, Any]],
    matched_stop: list[dict[str, Any]],
    unsupported: list[dict[str, Any]],
) -> tuple[str, float, str]:
    if matched_stop:
        return "broken", 0.0, "One or more stop-rule conditions were triggered."
    if matched_warning:
        return "weakening", 0.35, "Warning conditions are active against the thesis."
    if matched_positive:
        return "improving", 0.8, "Positive thesis conditions are currently satisfied."
    if unsupported and not (matched_positive or matched_warning or matched_stop):
        return "unsupported", 0.2, "The thesis cannot be fully evaluated from current derived inputs."
    return "intact", 0.6, "No warning or stop conditions are currently active."


def _build_thesis_review_items(
    *,
    ticker: str,
    document_id: str,
    evaluations: list[ThesisEvaluationRecord],
) -> list[ReviewQueueItem]:
    items: list[ReviewQueueItem] = []
    for evaluation in evaluations:
        if evaluation.status not in {"broken", "weakening"}:
            continue
        items.append(
            ReviewQueueItem(
                review_item_id=_stable_id("review", "thesis", evaluation.thesis_id, document_id, DERIVED_VERSION),
                item_type="thesis_broken" if evaluation.status == "broken" else "thesis_warning",
                ticker=ticker,
                document_id=document_id,
                source_key=evaluation.thesis_id,
                priority="high" if evaluation.status == "broken" else "medium",
                rationale=evaluation.rationale,
                payload_json=evaluation.payload_json,
            )
        )
    return items


def _build_claim_review_items(
    *,
    ticker: str,
    document_id: str,
    claim_outcomes: list[ClaimOutcomeRecord],
) -> list[ReviewQueueItem]:
    items: list[ReviewQueueItem] = []
    for outcome in claim_outcomes:
        if outcome.outcome_status not in {"Contradicted", "Open", "Not Yet Testable"}:
            continue
        items.append(
            ReviewQueueItem(
                review_item_id=_stable_id("review", "claim", outcome.claim_id, document_id, DERIVED_VERSION),
                item_type="claim_contradicted" if outcome.outcome_status == "Contradicted" else "claim_unresolved",
                ticker=ticker,
                document_id=document_id,
                source_key=outcome.claim_id,
                priority="high" if outcome.outcome_status == "Contradicted" else "medium",
                rationale=outcome.rationale,
                payload_json=outcome.payload_json,
            )
        )
    return items


def _aggregate_thesis_state(evaluations: list[ThesisEvaluationRecord]) -> str:
    statuses = {evaluation.status for evaluation in evaluations}
    if not statuses:
        return "none"
    if "broken" in statuses:
        return "broken"
    if "weakening" in statuses:
        return "weakening"
    if "improving" in statuses:
        return "improving"
    if "unsupported" in statuses:
        return "unsupported"
    return "intact"


def _decision_state_for(
    thesis_evaluations: list[ThesisEvaluationRecord],
    overall_state: str,
    confidence: str,
) -> str:
    thesis_state = _aggregate_thesis_state(thesis_evaluations)
    if thesis_state == "broken":
        return "Avoid Candidate"
    if overall_state in {"critical", "review_required"}:
        return "Review"
    if thesis_state == "improving" and confidence != "low":
        return "Buy Candidate"
    if thesis_state in {"weakening", "unsupported"}:
        return "Watch"
    if confidence == "low":
        return "Insufficient Signal"
    return "Watch"


def _watchlist_priority_for(
    thesis_evaluations: list[ThesisEvaluationRecord],
    overall_state: str,
    conflicts: list[ReviewQueueItem],
) -> str:
    thesis_state = _aggregate_thesis_state(thesis_evaluations)
    if thesis_state == "broken" or overall_state == "critical":
        return "high"
    if conflicts or thesis_state == "weakening" or overall_state == "review_required":
        return "medium"
    if thesis_state == "improving":
        return "medium"
    return "low"


def _compare_numeric(value: float, operator: str, threshold: float) -> bool:
    if operator == "gt":
        return value > threshold
    if operator == "gte":
        return value >= threshold
    if operator == "lt":
        return value < threshold
    if operator == "lte":
        return value <= threshold
    if operator == "eq":
        return value == threshold
    return False


def _build_comparable_company_state(
    conn: sqlite3.Connection,
    *,
    profile: CompanyProfile,
    latest_document: CompanyDocument | None = None,
    latest_metrics: list[MetricPoint] | None = None,
    baseline: BaselineChoice | None = None,
    latest_events: list[EventSignal] | None = None,
    latest_rules: list[TriggeredRule] | None = None,
) -> ComparableCompanyState | None:
    documents = _load_company_documents(conn, profile.ticker, limit=4)
    if not documents:
        return None
    latest_document = latest_document or documents[0]
    if baseline is None:
        baseline = _select_baseline(
            latest_document,
            [document for document in documents if document.document_id != latest_document.document_id],
        )
    latest_metrics = latest_metrics or _load_document_metrics(conn, latest_document.document_id)
    if not latest_metrics:
        return None
    latest_events = latest_events or _load_document_events(conn, latest_document.document_id)
    latest_rules = latest_rules or _load_triggered_rules(conn, latest_document.document_id)
    metric_by_name = _metric_map(latest_metrics)
    anchor_metric = _anchor_metric(latest_metrics)
    baseline_metrics = _load_document_metrics(conn, baseline.document.document_id) if baseline is not None else []
    baseline_metric_by_name = _metric_map(baseline_metrics)
    revenue_growth_pct = _safe_percent_change(
        baseline_metric_by_name.get("revenue"),
        metric_by_name.get("revenue"),
    ) if baseline and baseline.comparable else None
    recent_documents = documents[:3]
    recent_events = [event for document in recent_documents for event in _load_document_events(conn, document.document_id)]
    recent_rules = [rule for document in recent_documents for rule in _load_triggered_rules(conn, document.document_id)]
    warning_signal_count = sum(1 for event in recent_events if event.severity == "warning") + sum(
        1 for rule in recent_rules if rule.severity == "warning"
    )
    critical_signal_count = sum(1 for event in recent_events if event.severity == "critical") + sum(
        1 for rule in recent_rules if rule.severity == "critical"
    )
    revenue = metric_by_name.get("revenue")
    operating_profit = metric_by_name.get("operating_profit")
    net_income = metric_by_name.get("net_income")
    operating_cash_flow = metric_by_name.get("operating_cash_flow")
    total_liabilities = metric_by_name.get("total_liabilities")
    total_assets = metric_by_name.get("total_assets")
    total_equity = metric_by_name.get("total_equity")
    total_debt = metric_by_name.get("total_debt")
    leverage_ratio_pct = None
    if total_liabilities is not None and total_assets not in {None, 0}:
        leverage_ratio_pct = (total_liabilities / total_assets) * 100.0
    elif total_debt is not None and total_equity not in {None, 0}:
        leverage_ratio_pct = (total_debt / total_equity) * 100.0
    elif total_liabilities is not None and revenue not in {None, 0}:
        leverage_ratio_pct = (total_liabilities / revenue) * 100.0
    return ComparableCompanyState(
        ticker=profile.ticker,
        issuer_name=profile.issuer_name,
        sector=profile.sector,
        subsector=profile.subsector,
        latest_document_id=latest_document.document_id,
        latest_as_of=latest_document.source_modified_at or latest_document.updated_at,
        baseline_document_id=baseline.document.document_id if baseline is not None else None,
        baseline_kind=baseline.kind if baseline is not None else "none",
        historical_comparable=bool(baseline and baseline.comparable),
        fiscal_year=anchor_metric.fiscal_year if anchor_metric is not None else None,
        fiscal_period=anchor_metric.fiscal_period if anchor_metric is not None else None,
        period_end_date=anchor_metric.period_end_date if anchor_metric is not None else None,
        currency=anchor_metric.currency if anchor_metric is not None else None,
        unit_scale=anchor_metric.unit_scale if anchor_metric is not None else None,
        consolidation_scope=anchor_metric.consolidation_scope if anchor_metric is not None else None,
        metrics={
            "revenue_growth_pct": revenue_growth_pct,
            "operating_margin_pct": _prefer_metric_or_ratio(metric_by_name, "operating_margin", operating_profit, revenue),
            "net_margin_pct": _prefer_metric_or_ratio(metric_by_name, "net_margin", net_income, revenue),
            "cash_flow_margin_pct": _prefer_metric_or_ratio(metric_by_name, "cash_flow_margin", operating_cash_flow, revenue),
            "leverage_ratio_pct": leverage_ratio_pct,
            "dilution_frequency": float(sum(1 for event in recent_events if event.event_type in DILUTION_EVENT_TYPES)),
            "governance_change_frequency": float(sum(1 for event in recent_events if event.event_type in GOVERNANCE_EVENT_TYPES)),
            "event_density": round(len(recent_events) / max(len(recent_documents), 1), 4),
        },
        warning_signal_count=warning_signal_count,
        critical_signal_count=critical_signal_count,
    )


def _build_peer_set(
    conn: sqlite3.Connection,
    profile: CompanyProfile,
    current_state: ComparableCompanyState,
) -> tuple[PeerSetRecord, dict[str, Any]]:
    candidate_profiles = _load_sector_profiles(conn, sector=profile.sector, exclude_ticker=profile.ticker)
    candidate_states: list[tuple[ComparableCompanyState, bool]] = []
    for candidate in candidate_profiles:
        state = _build_comparable_company_state(conn, profile=candidate)
        if state is None:
            continue
        candidate_states.append((state, bool(profile.subsector and candidate.subsector == profile.subsector)))

    same_subsector_states = [state for state, same_subsector in candidate_states if same_subsector]
    if profile.subsector and len(same_subsector_states) >= 3:
        selected_states = same_subsector_states
        peer_key = f"subsector:{profile.subsector}"
        rationale = f"Peers selected from the same subsector ({profile.subsector}) because enough comparable issuers exist."
    else:
        selected_states = [state for state, _ in candidate_states]
        peer_key = f"sector:{profile.sector or 'unknown'}"
        rationale = f"Peers selected from the same sector ({profile.sector or 'unknown'}) due to limited subsector coverage."

    rows: list[dict[str, Any]] = []
    comparable_states: list[ComparableCompanyState] = []
    comparability_warnings: list[str] = []
    for state in selected_states[:12]:
        warnings = _peer_comparability_warnings(current_state, state)
        rows.append(
            {
                "ticker": state.ticker,
                "issuer_name": state.issuer_name,
                "latest_as_of": state.latest_as_of,
                "baseline_kind": state.baseline_kind,
                "historical_comparable": state.historical_comparable,
                "metrics": state.metrics,
                "warning_signal_count": state.warning_signal_count,
                "critical_signal_count": state.critical_signal_count,
                "comparability_warnings": warnings,
            }
        )
        if warnings:
            comparability_warnings.append(f"{state.ticker}: {'; '.join(warnings)}")
            continue
        comparable_states.append(state)

    metric_ranks, anomalies = _build_comparison_views(current_state, comparable_states)
    peer_view = {
        "peer_key": peer_key,
        "selection_rationale": rationale,
        "peer_count": len(rows),
        "comparable_peer_count": len(comparable_states),
        "comparability_warnings": comparability_warnings,
        "metric_ranks": metric_ranks,
        "anomalies": anomalies,
        "peer_rows": rows,
    }
    peer_tickers = [state.ticker for state in comparable_states]
    return (
        PeerSetRecord(
            peer_set_id=_stable_id("peers", profile.ticker, peer_key, DERIVED_VERSION),
            ticker=profile.ticker,
            peer_key=peer_key,
            peer_tickers_json=json.dumps(peer_tickers, sort_keys=True),
            rationale=rationale,
        ),
        peer_view,
    )


def _build_sector_rollup(
    conn: sqlite3.Connection,
    profile: CompanyProfile,
    current_state: ComparableCompanyState,
) -> tuple[SectorRollupRecord, dict[str, Any]]:
    sector_profiles = _load_sector_profiles(conn, sector=profile.sector)
    states = [state for candidate in sector_profiles if (state := _build_comparable_company_state(conn, profile=candidate)) is not None]
    metric_summaries = {
        field_name: _summarize_metric_distribution(states, field_name)
        for field_name in COMPARISON_FIELD_LABELS
    }
    sector_ranks, _ = _build_comparison_views(
        current_state,
        [state for state in states if state.ticker != current_state.ticker and not _peer_comparability_warnings(current_state, state)],
    )
    red_flag_companies = sum(1 for state in states if state.warning_signal_count or state.critical_signal_count)
    heatmap = {
        "disclosure_intensity": round(median([state.metrics["event_density"] for state in states if state.metrics["event_density"] is not None]) if states else 0.0, 4),
        "red_flag_density": round(red_flag_companies / max(len(states), 1), 4),
        "governance_change_density": round(median([state.metrics["governance_change_frequency"] for state in states]) if states else 0.0, 4),
        "dilution_frequency": round(median([state.metrics["dilution_frequency"] for state in states]) if states else 0.0, 4),
    }
    payload = {
        "sector": profile.sector or "unknown",
        "subsector": profile.subsector,
        "company_count": len(sector_profiles),
        "analyzed_tickers": len(states),
        "metric_summaries": metric_summaries,
        "heatmap": heatmap,
        "subject_ranks": sector_ranks,
        "top_anomalies": _collect_sector_anomalies(states)[:10],
    }
    return (
        SectorRollupRecord(
            sector_rollup_id=_stable_id("sector", profile.sector or "unknown", profile.subsector or "", "peer_intelligence", DERIVED_VERSION),
            sector=profile.sector or "unknown",
            subsector=profile.subsector,
            rollup_key="peer_intelligence",
            payload_json=json.dumps(payload, sort_keys=True),
        ),
        payload,
    )


def _load_sector_profiles(
    conn: sqlite3.Connection,
    *,
    sector: str | None,
    exclude_ticker: str | None = None,
) -> list[CompanyProfile]:
    params: list[object]
    if sector is None:
        where = "WHERE sector IS NULL"
        params = []
    else:
        where = "WHERE sector = ?"
        params = [sector]
    if exclude_ticker is not None:
        where += " AND ticker != ?"
        params.append(exclude_ticker)
    rows = conn.execute(
        f"""
        SELECT ticker, issuer_name, sector, subsector, industry, listing_board, active_status
        FROM companies
        {where}
        ORDER BY CASE WHEN active_status = 'listed' THEN 0 ELSE 1 END, ticker ASC
        """,
        tuple(params),
    ).fetchall()
    return [
        CompanyProfile(
            ticker=row["ticker"],
            issuer_name=row["issuer_name"],
            sector=row["sector"],
            subsector=row["subsector"],
            industry=row["industry"],
            listing_board=row["listing_board"],
            active_status=row["active_status"],
        )
        for row in rows
    ]


def _metric_map(metrics: list[MetricPoint]) -> dict[str, float | None]:
    result: dict[str, float | None] = {}
    for metric in metrics:
        result.setdefault(metric.metric_name, metric.value_normalized)
    return result


def _anchor_metric(metrics: list[MetricPoint]) -> MetricPoint | None:
    for preferred in ("revenue", "operating_profit", "net_income"):
        for metric in metrics:
            if metric.metric_name == preferred:
                return metric
    return metrics[0] if metrics else None


def _prefer_metric_or_ratio(
    metrics_by_name: dict[str, float | None],
    metric_name: str,
    numerator: float | None,
    denominator: float | None,
) -> float | None:
    direct = metrics_by_name.get(metric_name)
    if direct is not None:
        return direct
    if numerator is None or denominator in {None, 0}:
        return None
    return (numerator / denominator) * 100.0


def _safe_percent_change(previous_value: float | None, current_value: float | None) -> float | None:
    if previous_value in {None, 0} or current_value is None:
        return None
    return ((current_value - previous_value) / abs(previous_value)) * 100.0


def _peer_comparability_warnings(subject: ComparableCompanyState, candidate: ComparableCompanyState) -> list[str]:
    warnings: list[str] = []
    if subject.fiscal_year is not None and candidate.fiscal_year is not None and subject.fiscal_year != candidate.fiscal_year:
        warnings.append("fiscal year mismatch")
    if subject.fiscal_period and candidate.fiscal_period and subject.fiscal_period != candidate.fiscal_period:
        warnings.append("fiscal period mismatch")
    if subject.currency and candidate.currency and subject.currency != candidate.currency:
        warnings.append("currency mismatch")
    if subject.unit_scale and candidate.unit_scale and subject.unit_scale != candidate.unit_scale:
        warnings.append("unit scale mismatch")
    if subject.consolidation_scope and candidate.consolidation_scope and subject.consolidation_scope != candidate.consolidation_scope:
        warnings.append("consolidation scope mismatch")
    return warnings


def _build_comparison_views(
    subject: ComparableCompanyState,
    peer_states: list[ComparableCompanyState],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    metric_ranks: dict[str, Any] = {}
    anomalies: list[dict[str, Any]] = []
    for field_name in COMPARISON_FIELD_LABELS:
        rank = _rank_metric_against_group(subject, peer_states, field_name)
        if rank is None:
            continue
        metric_ranks[field_name] = rank
        if rank["anomaly"] is not None:
            anomalies.append(
                {
                    "metric_name": field_name,
                    "label": COMPARISON_FIELD_LABELS[field_name],
                    "anomaly": rank["anomaly"],
                    "severity": rank["severity"],
                    "summary": f"{subject.ticker} is a {rank['anomaly'].replace('_', ' ')} on {COMPARISON_FIELD_LABELS[field_name].lower()} versus peers.",
                    "subject_value": rank["subject_value"],
                    "peer_median": rank["peer_median"],
                    "rank": rank["rank"],
                    "sample_size": rank["sample_size"],
                }
            )
    anomalies.sort(key=lambda item: (0 if item["severity"] == "critical" else 1, item["metric_name"]))
    return metric_ranks, anomalies


def _rank_metric_against_group(
    subject: ComparableCompanyState,
    peer_states: list[ComparableCompanyState],
    field_name: str,
) -> dict[str, Any] | None:
    subject_value = subject.metrics.get(field_name)
    if subject_value is None:
        return None
    values: list[tuple[str, float]] = [(subject.ticker, subject_value)]
    for state in peer_states:
        value = state.metrics.get(field_name)
        if value is not None:
            values.append((state.ticker, value))
    if len(values) < 2:
        return None
    higher_is_better = field_name in HIGHER_IS_BETTER_FIELDS
    ordered = sorted(values, key=lambda item: item[1], reverse=higher_is_better)
    rank = next(index for index, item in enumerate(ordered, start=1) if item[0] == subject.ticker)
    peer_values = [value for ticker, value in values if ticker != subject.ticker]
    peer_median = float(median(peer_values)) if peer_values else subject_value
    directional_gap = (subject_value - peer_median) if higher_is_better else (peer_median - subject_value)
    if peer_median == 0:
        if directional_gap > 0:
            relative_gap_pct = 100.0
        elif directional_gap < 0:
            relative_gap_pct = -100.0
        else:
            relative_gap_pct = 0.0
    else:
        relative_gap_pct = (directional_gap / abs(peer_median)) * 100.0
    anomaly = None
    severity = None
    if len(values) >= 4:
        if rank == 1 and relative_gap_pct >= 25:
            anomaly = "positive_outlier"
        elif rank == len(values) and relative_gap_pct <= -25:
            anomaly = "negative_outlier"
        if anomaly is not None:
            severity = "critical" if abs(relative_gap_pct) >= 50 else "warning"
    return {
        "label": COMPARISON_FIELD_LABELS[field_name],
        "direction": "higher_better" if higher_is_better else "lower_better",
        "subject_value": subject_value,
        "peer_median": peer_median,
        "rank": rank,
        "sample_size": len(values),
        "relative_gap_pct": relative_gap_pct,
        "anomaly": anomaly,
        "severity": severity,
    }


def _summarize_metric_distribution(
    states: list[ComparableCompanyState],
    field_name: str,
) -> dict[str, Any]:
    values = [(state.ticker, state.metrics.get(field_name)) for state in states if state.metrics.get(field_name) is not None]
    if not values:
        return {
            "label": COMPARISON_FIELD_LABELS[field_name],
            "sample_size": 0,
            "median": None,
            "leaders": [],
            "laggards": [],
        }
    higher_is_better = field_name in HIGHER_IS_BETTER_FIELDS
    ordered = sorted(values, key=lambda item: item[1], reverse=higher_is_better)
    laggards = list(reversed(ordered)) if higher_is_better else ordered
    numeric_values = [value for _, value in values]
    return {
        "label": COMPARISON_FIELD_LABELS[field_name],
        "sample_size": len(values),
        "median": float(median(numeric_values)),
        "leaders": [{"ticker": ticker, "value": value} for ticker, value in ordered[:3]],
        "laggards": [{"ticker": ticker, "value": value} for ticker, value in laggards[:3]],
    }


def _collect_sector_anomalies(states: list[ComparableCompanyState]) -> list[dict[str, Any]]:
    anomalies: list[dict[str, Any]] = []
    for state in states:
        peers = [peer for peer in states if peer.ticker != state.ticker and not _peer_comparability_warnings(state, peer)]
        _, peer_anomalies = _build_comparison_views(state, peers)
        for anomaly in peer_anomalies:
            anomalies.append(
                {
                    **anomaly,
                    "ticker": state.ticker,
                    "issuer_name": state.issuer_name,
                }
            )
    anomalies.sort(key=lambda item: (0 if item["severity"] == "critical" else 1, item["ticker"], item["metric_name"]))
    return anomalies


def _persist_bundle(conn: sqlite3.Connection, bundle: CompanyIntelligenceBundle) -> dict[str, int]:
    now = datetime.now(timezone.utc).isoformat()
    claim_status_by_id = {
        outcome.claim_id: outcome.outcome_status.lower().replace(" ", "_")
        for outcome in bundle.claim_outcomes
    }
    for delta in bundle.deltas:
        conn.execute(
            """
            INSERT INTO document_deltas (
                delta_id, ticker, from_document_id, to_document_id, delta_kind, delta_key,
                metric_name, fiscal_year, fiscal_period, period_end_date, previous_value,
                current_value, absolute_change, percent_change, payload_json, derived_version, derived_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, from_document_id, to_document_id, delta_kind, delta_key, derived_version)
            DO UPDATE SET
                metric_name = excluded.metric_name,
                fiscal_year = excluded.fiscal_year,
                fiscal_period = excluded.fiscal_period,
                period_end_date = excluded.period_end_date,
                previous_value = excluded.previous_value,
                current_value = excluded.current_value,
                absolute_change = excluded.absolute_change,
                percent_change = excluded.percent_change,
                payload_json = excluded.payload_json,
                derived_at = excluded.derived_at
            """,
            (
                delta.delta_id,
                delta.ticker,
                delta.from_document_id,
                delta.to_document_id,
                delta.delta_kind,
                delta.delta_key,
                delta.metric_name,
                delta.fiscal_year,
                delta.fiscal_period,
                delta.period_end_date,
                delta.previous_value,
                delta.current_value,
                delta.absolute_change,
                delta.percent_change,
                delta.payload_json,
                DERIVED_VERSION,
                now,
            ),
        )

    snapshot_id = _stable_id("snapshot", bundle.profile.ticker, "current", DERIVED_VERSION)
    conn.execute(
        """
        INSERT INTO company_snapshots (
            snapshot_id, ticker, snapshot_kind, as_of_document_id, as_of_date, latest_analysis_run_id,
            payload_json, derived_version, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, snapshot_kind, derived_version)
        DO UPDATE SET
            snapshot_id = excluded.snapshot_id,
            as_of_document_id = excluded.as_of_document_id,
            as_of_date = excluded.as_of_date,
            latest_analysis_run_id = excluded.latest_analysis_run_id,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        (
            snapshot_id,
            bundle.profile.ticker,
            "current",
            bundle.latest_document.document_id,
            bundle.latest_document.source_modified_at or bundle.latest_document.updated_at,
            bundle.latest_analysis.analysis_run_id if bundle.latest_analysis else None,
            bundle.snapshot_payload_json,
            DERIVED_VERSION,
            now,
            now,
        ),
    )

    for point in bundle.timeseries:
        conn.execute(
            """
            INSERT INTO company_timeseries (
                series_point_id, ticker, series_name, series_key, fiscal_year, fiscal_period,
                period_end_date, value_normalized, document_id, evidence_span_id, derived_version, derived_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, series_name, series_key, document_id, derived_version)
            DO UPDATE SET
                series_point_id = excluded.series_point_id,
                fiscal_year = excluded.fiscal_year,
                fiscal_period = excluded.fiscal_period,
                period_end_date = excluded.period_end_date,
                value_normalized = excluded.value_normalized,
                evidence_span_id = excluded.evidence_span_id,
                derived_at = excluded.derived_at
            """,
            (
                point.series_point_id,
                point.ticker,
                point.series_name,
                point.series_key,
                point.fiscal_year,
                point.fiscal_period,
                point.period_end_date,
                point.value_normalized,
                point.document_id,
                point.evidence_span_id,
                DERIVED_VERSION,
                now,
            ),
        )

    for evaluation in bundle.thesis_evaluations:
        conn.execute(
            """
            INSERT OR REPLACE INTO thesis_evaluations (
                thesis_evaluation_id, thesis_id, ticker, document_id, status, score, rationale,
                payload_json, derived_version, evaluated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evaluation.thesis_evaluation_id,
                evaluation.thesis_id,
                evaluation.ticker,
                evaluation.document_id,
                evaluation.status,
                evaluation.score,
                evaluation.rationale,
                evaluation.payload_json,
                DERIVED_VERSION,
                now,
            ),
        )

    for claim in bundle.claims:
        conn.execute(
            """
            INSERT INTO management_claims (
                claim_id, ticker, source_document_id, claim_text, claim_topic, claim_date,
                evidence_span_id, status, derived_version, created_at, updated_at,
                claim_type, claim_subject, claim_time_horizon, claim_confidence, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(claim_id)
            DO UPDATE SET
                ticker = excluded.ticker,
                source_document_id = excluded.source_document_id,
                claim_text = excluded.claim_text,
                claim_topic = excluded.claim_topic,
                claim_date = excluded.claim_date,
                evidence_span_id = excluded.evidence_span_id,
                status = excluded.status,
                updated_at = excluded.updated_at,
                claim_type = excluded.claim_type,
                claim_subject = excluded.claim_subject,
                claim_time_horizon = excluded.claim_time_horizon,
                claim_confidence = excluded.claim_confidence,
                payload_json = excluded.payload_json
            """,
            (
                claim.claim_id,
                claim.ticker,
                claim.source_document_id,
                claim.claim_text,
                claim.claim_topic,
                claim.claim_date,
                claim.evidence_span_id,
                claim_status_by_id.get(claim.claim_id, claim.status),
                DERIVED_VERSION,
                now,
                now,
                claim.claim_type,
                claim.claim_subject,
                claim.claim_time_horizon,
                claim.claim_confidence,
                claim.payload_json,
            ),
        )

    for outcome in bundle.claim_outcomes:
        conn.execute(
            """
            INSERT OR REPLACE INTO claim_outcomes (
                outcome_id, claim_id, outcome_status, assessment_date, rationale, evidence_span_id,
                payload_json, derived_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                outcome.outcome_id,
                outcome.claim_id,
                outcome.outcome_status,
                outcome.assessment_date,
                outcome.rationale,
                outcome.evidence_span_id,
                outcome.payload_json,
                DERIVED_VERSION,
                now,
            ),
        )

    conn.execute(
        """
        INSERT INTO decision_briefs (
            brief_id, ticker, source_document_id, snapshot_id, brief_kind, payload_json,
            derived_version, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, brief_kind, derived_version)
        DO UPDATE SET
            brief_id = excluded.brief_id,
            source_document_id = excluded.source_document_id,
            snapshot_id = excluded.snapshot_id,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        (
            _stable_id("brief", bundle.profile.ticker, "current", DERIVED_VERSION),
            bundle.profile.ticker,
            bundle.latest_document.document_id,
            snapshot_id,
            "current",
            bundle.brief_payload_json,
            DERIVED_VERSION,
            now,
            now,
        ),
    )

    for item in bundle.review_items:
        conn.execute(
            """
            INSERT INTO review_queue_items (
                review_item_id, item_type, ticker, document_id, source_key, priority, status,
                rationale, payload_json, derived_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_type, source_key, derived_version)
            DO UPDATE SET
                review_item_id = excluded.review_item_id,
                ticker = excluded.ticker,
                document_id = excluded.document_id,
                priority = excluded.priority,
                rationale = excluded.rationale,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (
                item.review_item_id,
                item.item_type,
                item.ticker,
                item.document_id,
                item.source_key,
                item.priority,
                "open",
                item.rationale,
                item.payload_json,
                DERIVED_VERSION,
                now,
                now,
            ),
        )

    conn.execute(
        """
        INSERT INTO peer_sets (
            peer_set_id, ticker, peer_key, peer_tickers_json, rationale, derived_version, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, peer_key, derived_version)
        DO UPDATE SET
            peer_set_id = excluded.peer_set_id,
            peer_tickers_json = excluded.peer_tickers_json,
            rationale = excluded.rationale,
            updated_at = excluded.updated_at
        """,
        (
            bundle.peer_set.peer_set_id,
            bundle.peer_set.ticker,
            bundle.peer_set.peer_key,
            bundle.peer_set.peer_tickers_json,
            bundle.peer_set.rationale,
            DERIVED_VERSION,
            now,
            now,
        ),
    )

    conn.execute(
        """
        INSERT INTO sector_rollups (
            sector_rollup_id, sector, subsector, rollup_key, payload_json, derived_version, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sector, subsector, rollup_key, derived_version)
        DO UPDATE SET
            sector_rollup_id = excluded.sector_rollup_id,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        (
            bundle.sector_rollup.sector_rollup_id,
            bundle.sector_rollup.sector,
            bundle.sector_rollup.subsector,
            bundle.sector_rollup.rollup_key,
            bundle.sector_rollup.payload_json,
            DERIVED_VERSION,
            now,
            now,
        ),
    )

    return {
        "deltas": len(bundle.deltas),
        "snapshots": 1,
        "timeseries": len(bundle.timeseries),
        "briefs": 1,
        "review_items": len(bundle.review_items),
        "peer_sets": 1,
        "sector_rollups": 1,
        "thesis_evaluations": len(bundle.thesis_evaluations),
        "claims": len(bundle.claims),
        "claim_outcomes": len(bundle.claim_outcomes),
    }


def _metric_key(metric: MetricPoint) -> tuple[str, int | None, str | None, str | None]:
    return (
        metric.metric_name,
        metric.fiscal_year,
        metric.fiscal_period,
        metric.period_end_date,
    )


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "|".join("" if part is None else str(part) for part in parts)
    return f"{prefix}-{hashlib.sha1(payload.encode('utf-8')).hexdigest()[:24]}"


def _priority_for_severity(severity: str | None) -> str:
    if severity == "critical":
        return "high"
    if severity == "warning":
        return "medium"
    return "low"
