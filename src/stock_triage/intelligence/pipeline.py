"""Phase 11 change detection and snapshot assembly over the Phase 10 foundation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Any

from ..config import Config
from ..db import WriterQueue, connect, configure_pragmas, run_migrations
from ..fiscal_year import resolve_fiscal_year_end_month
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

DERIVED_VERSION = "v3_2-v1"

CORE_FINANCIAL_METRICS = {
    "revenue",
    "gross_profit",
    "operating_profit",
    "net_profit",
    "operating_cash_flow",
    "capex",
    "cash",
    "short_term_debt",
    "long_term_debt",
    "equity",
    "share_count",
    "net_debt",
    "free_cash_flow",
    "total_liabilities",
}
STANCE_LEVEL = {
    "Avoid": 0,
    "Review manually": 1,
    "Watch": 2,
    "Buy": 3,
}

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
    fiscal_year_end_month: int
    fiscal_year_end_source: str


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
class FilingDocument:
    document_id: str
    document_type: str | None
    as_of: str
    period_end_dates: tuple[str, ...]
    fiscal_periods: tuple[str, ...]


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
class FilingExpectationRecord:
    expectation_id: str
    ticker: str
    obligation_type: str
    fiscal_year: int
    period_label: str
    period_end_date: str
    window_start: str
    deadline_at: str
    satisfied_at: str | None
    state: str
    knowledge_source: str
    satisfied_document_id: str | None
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
    filing_expectations: list[FilingExpectationRecord]
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
                bundle = _build_bundle(reader, cfg, ticker_value)
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


def rederive_intelligence(
    cfg: Config,
    *,
    ticker: str | None = None,
    limit: int = 1000,
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
            tickers = _select_snapshot_tickers(reader, ticker=ticker, limit=limit)
            result = IntelligenceRunResult(selected_tickers=len(tickers))
            for ticker_value in tickers:
                bundle = _build_bundle(reader, cfg, ticker_value)
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


def _select_snapshot_tickers(conn: sqlite3.Connection, *, ticker: str | None, limit: int) -> list[str]:
    if ticker:
        return [ticker]
    rows = conn.execute(
        """
        SELECT ticker
        FROM company_snapshots
        WHERE snapshot_kind = 'current'
        GROUP BY ticker
        ORDER BY MAX(updated_at) DESC, ticker ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [row["ticker"] for row in rows]


def _build_bundle(conn: sqlite3.Connection, cfg: Config, ticker: str) -> CompanyIntelligenceBundle | None:
    profile = _load_company_profile(conn, cfg, ticker)
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
    filing_expectations = _build_filing_expectations(
        profile=profile,
        filing_documents=_load_filing_documents(conn, ticker),
        now=date.today(),
    )
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
    persisted_review_statuses = _load_review_status_map(conn, ticker=ticker)
    active_review_items = _filter_active_review_items(
        conflict_items + thesis_items + claim_items,
        review_statuses=persisted_review_statuses,
    )
    peer_set, peer_view = _build_peer_set(conn, cfg, profile, current_company_state)
    sector_rollup, sector_view = _build_sector_rollup(conn, cfg, profile, current_company_state)
    snapshot_payload_json = _build_snapshot_payload_json(
        profile=profile,
        latest_document=latest_document,
        baseline=baseline,
        filing_expectations=filing_expectations,
        latest_analysis=latest_analysis,
        latest_metrics=latest_metrics,
        deltas=deltas,
        events=latest_events,
        triggered_rules=latest_rules,
        review_items=active_review_items,
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
        filing_expectations=filing_expectations,
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


def _load_company_profile(conn: sqlite3.Connection, cfg: Config, ticker: str) -> CompanyProfile | None:
    row = conn.execute(
        """
        SELECT ticker, issuer_name, sector, subsector, industry, listing_board, active_status, fiscal_year_end_month
        FROM companies
        WHERE ticker = ?
        LIMIT 1
        """,
        (ticker,),
    ).fetchone()
    if row is None:
        return None
    fiscal_year_end_month, fiscal_year_end_source = resolve_fiscal_year_end_month(
        conn,
        cfg,
        ticker=ticker,
        stored_month=int(row["fiscal_year_end_month"] or 12),
    )
    return CompanyProfile(
        ticker=row["ticker"],
        issuer_name=row["issuer_name"],
        sector=row["sector"],
        subsector=row["subsector"],
        industry=row["industry"],
        listing_board=row["listing_board"],
        active_status=row["active_status"],
        fiscal_year_end_month=fiscal_year_end_month,
        fiscal_year_end_source=fiscal_year_end_source,
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


def _load_filing_documents(conn: sqlite3.Connection, ticker: str) -> list[FilingDocument]:
    rows = conn.execute(
        """
        SELECT
            d.document_id,
            d.document_type,
            COALESCE(d.source_modified_at, d.updated_at, d.created_at) AS as_of,
            mi.period_end_date,
            mi.fiscal_period
        FROM documents d
        LEFT JOIN metric_items mi ON mi.document_id = d.document_id
        WHERE d.ticker = ?
          AND d.process_state = 'analyzed'
          AND d.document_type IN ('annual_report', 'financial_statement')
        ORDER BY COALESCE(d.source_modified_at, d.updated_at, d.created_at) DESC, d.document_id DESC
        """,
        (ticker,),
    ).fetchall()
    grouped: dict[str, dict[str, object]] = {}
    for row in rows:
        record = grouped.setdefault(
            row["document_id"],
            {
                "document_id": row["document_id"],
                "document_type": row["document_type"],
                "as_of": row["as_of"],
                "period_end_dates": set(),
                "fiscal_periods": set(),
            },
        )
        if row["period_end_date"]:
            record["period_end_dates"].add(str(row["period_end_date"]))
        if row["fiscal_period"]:
            record["fiscal_periods"].add(str(row["fiscal_period"]))
    return [
        FilingDocument(
            document_id=str(item["document_id"]),
            document_type=item["document_type"],
            as_of=str(item["as_of"]),
            period_end_dates=tuple(sorted(item["period_end_dates"])),
            fiscal_periods=tuple(sorted(item["fiscal_periods"])),
        )
        for item in grouped.values()
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
            for part in (
                metric.metric_name,
                metric.consolidation_scope,
                metric.fiscal_year,
                metric.fiscal_period,
                metric.period_end_date,
            )
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
    filing_expectations: list[FilingExpectationRecord],
    latest_analysis: AnalysisSummary | None,
    latest_metrics: list[MetricPoint],
    deltas: list[DeltaRecord],
    events: list[EventSignal],
    triggered_rules: list[TriggeredRule],
    review_items: list[ReviewQueueItem],
    claims: list[ClaimRecord],
    claim_outcomes: list[ClaimOutcomeRecord],
    credibility_summary: ClaimCredibilitySummary,
    thesis_evaluations: list[ThesisEvaluationRecord],
    peer_view: dict[str, Any],
    sector_view: dict[str, Any],
) -> str:
    health = _build_health_state(
        baseline=baseline,
        latest_document=latest_document,
        latest_metrics=latest_metrics,
        latest_analysis=latest_analysis,
        peer_view=peer_view,
        deltas=deltas,
        events=events,
        triggered_rules=triggered_rules,
        review_items=review_items,
        credibility_summary=credibility_summary,
        thesis_evaluations=thesis_evaluations,
    )
    delta_counts: dict[str, int] = {}
    for delta in deltas:
        delta_counts[delta.delta_kind] = delta_counts.get(delta.delta_kind, 0) + 1
    if peer_view.get("comparability_warnings"):
        warning_count = len(peer_view["comparability_warnings"])
        health["reasons"].append(f"Peer comparison has {warning_count} comparability warning(s).")
    decision_panel = _build_decision_panel(
        latest_document=latest_document,
        filing_expectations=filing_expectations,
        latest_metrics=latest_metrics,
        latest_analysis=latest_analysis,
        deltas=deltas,
        events=events,
        triggered_rules=triggered_rules,
        thesis_evaluations=thesis_evaluations,
        review_items=review_items,
        profile=profile,
        top_card=health,
    )
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
        "decision_panel": decision_panel,
        "fiscal_year_end_month": profile.fiscal_year_end_month,
        "fiscal_year_end_source": profile.fiscal_year_end_source,
        "filing_expectations": [
            {
                "obligation_type": expectation.obligation_type,
                "fiscal_year": expectation.fiscal_year,
                "period_label": expectation.period_label,
                "period_end_date": expectation.period_end_date,
                "window_start": expectation.window_start,
                "deadline_at": expectation.deadline_at,
                "satisfied_at": expectation.satisfied_at,
                "state": expectation.state,
                "knowledge_source": expectation.knowledge_source,
                "satisfied_document_id": expectation.satisfied_document_id,
            }
            for expectation in filing_expectations[:10]
        ],
        "latest_summary": latest_analysis.summary if latest_analysis else None,
        "segment_summary": _build_segment_summary(latest_metrics),
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
            for item in review_items
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
    # Deduplicate per ticker+rule_name so that only the latest trigger for a
    # given rule survives across documents (e.g. annual reports from different
    # years that fire the same rule). The ON CONFLICT upsert in _persist will
    # update the existing row without resetting operator status.
    seen_rules: set[str] = set()
    for rule in triggered_rules:
        dedup_key = f"{ticker}:{rule.rule_name}"
        if dedup_key in seen_rules:
            continue
        seen_rules.add(dedup_key)
        source_key = _stable_id("review-src", "rule", ticker, rule.rule_name, DERIVED_VERSION)
        items.append(
            ReviewQueueItem(
                review_item_id=_stable_id("review", "rule", source_key, DERIVED_VERSION),
                item_type="rule_trigger",
                ticker=ticker,
                document_id=document_id,
                source_key=source_key,
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
    seen_events: set[str] = set()
    for event in events:
        severity = event.severity or "info"
        if severity not in {"warning", "critical"}:
            continue
        dedup_key = f"{ticker}:{event.event_type}"
        if dedup_key in seen_events:
            continue
        seen_events.add(dedup_key)
        source_key = _stable_id("review-src", "event", ticker, event.event_type, DERIVED_VERSION)
        items.append(
            ReviewQueueItem(
                review_item_id=_stable_id("review", "event", source_key, DERIVED_VERSION),
                item_type="event_signal",
                ticker=ticker,
                document_id=document_id,
                source_key=source_key,
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
    latest_document: CompanyDocument,
    latest_metrics: list[MetricPoint],
    latest_analysis: AnalysisSummary | None,
    peer_view: dict[str, Any],
    deltas: list[DeltaRecord],
    events: list[EventSignal],
    triggered_rules: list[TriggeredRule],
    review_items: list[ReviewQueueItem],
    credibility_summary: ClaimCredibilitySummary,
    thesis_evaluations: list[ThesisEvaluationRecord],
) -> dict[str, Any]:
    reasons: list[str] = []
    overall_state = "stable"
    comparability_state = "none"
    confidence = "medium"
    thesis_state = _aggregate_thesis_state(thesis_evaluations)
    has_critical_rules = any(rule.severity == "critical" for rule in triggered_rules) or any(
        (event.severity or "") == "critical" for event in events
    )
    data_completeness_pct = _data_completeness_pct(
        baseline=baseline,
        latest_document=latest_document,
        latest_metrics=latest_metrics,
        latest_analysis=latest_analysis,
        peer_view=peer_view,
        credibility_summary=credibility_summary,
    )
    data_completeness = data_completeness_pct >= 70.0

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

    if review_items:
        overall_state = "review_required"
        confidence = "medium" if confidence == "high" else confidence
        reasons.append("One or more review items still require analyst judgment.")

    thesis_statuses = {evaluation.status for evaluation in thesis_evaluations}
    if "broken" in thesis_statuses:
        overall_state = "critical"
        reasons.append("At least one configured thesis is broken.")
    elif overall_state not in {"critical", "review_required"} and "weakening" in thesis_statuses:
        overall_state = "watch"
        reasons.append("At least one configured thesis is weakening.")
    elif overall_state == "stable" and "improving" in thesis_statuses:
        reasons.append("A configured thesis is improving.")

    if has_critical_rules:
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
    elif review_items:
        disclosure_quality = "review"

    management_credibility = credibility_summary.credibility_state
    if management_credibility == "deteriorating":
        reasons.append("Management claim delivery deteriorated versus prior expectations.")

    credibility_band = _credibility_band(credibility_summary)
    stance, stance_confidence = _derive_stance(
        overall_state=overall_state,
        thesis_state=thesis_state,
        credibility_state=credibility_band,
        has_critical_rules=has_critical_rules,
        has_open_reviews=bool(review_items),
        data_completeness=data_completeness,
    )

    return {
        "overall_state": overall_state,
        "comparability_state": comparability_state,
        "confidence": confidence,
        "data_completeness_pct": data_completeness_pct,
        "data_completeness": data_completeness,
        "leverage_state": leverage_state,
        "disclosure_quality": disclosure_quality,
        "management_credibility": management_credibility,
        "credibility_band": credibility_band,
        "thesis_state": thesis_state,
        "decision_state": stance,
        "stance_confidence": stance_confidence,
        "watchlist_priority": _watchlist_priority_for(thesis_state, overall_state, review_items, stance),
        "positive_factors": positive_factors[:5],
        "negative_factors": negative_factors[:5],
        "reasons": reasons[:8],
    }


def _build_decision_panel(
    *,
    profile: CompanyProfile,
    latest_document: CompanyDocument,
    filing_expectations: list[FilingExpectationRecord],
    latest_metrics: list[MetricPoint],
    latest_analysis: AnalysisSummary | None,
    deltas: list[DeltaRecord],
    events: list[EventSignal],
    triggered_rules: list[TriggeredRule],
    thesis_evaluations: list[ThesisEvaluationRecord],
    review_items: list[ReviewQueueItem],
    top_card: dict[str, Any],
) -> dict[str, Any]:
    return {
        "stance": top_card.get("decision_state"),
        "confidence": top_card.get("stance_confidence") or top_card.get("confidence"),
        "why": list(top_card.get("reasons") or [])[:3],
        "what_changed": _summarize_deltas(deltas, limit=3),
        "what_to_watch": _build_watch_items(
            latest_analysis=latest_analysis,
            events=events,
            triggered_rules=triggered_rules,
            review_items=review_items,
            top_card=top_card,
        ),
        "must_improve": _build_improvement_items(
            thesis_evaluations=thesis_evaluations,
            review_items=review_items,
            top_card=top_card,
        ),
        "thesis_breakers": _build_thesis_breakers(thesis_evaluations),
        "quality_flags": _build_quality_flags(deltas=deltas, events=events, triggered_rules=triggered_rules),
        "next_catalyst": _estimate_next_catalyst(
            filing_expectations=filing_expectations,
            profile=profile,
            latest_document=latest_document,
            latest_metrics=latest_metrics,
        ),
    }


def _data_completeness_pct(
    *,
    baseline: BaselineChoice | None,
    latest_document: CompanyDocument,
    latest_metrics: list[MetricPoint],
    latest_analysis: AnalysisSummary | None,
    peer_view: dict[str, Any],
    credibility_summary: ClaimCredibilitySummary,
) -> float:
    checks = [
        baseline is not None,
        latest_analysis is not None and bool(latest_analysis.summary),
        bool(peer_view.get("peer_key")),
        credibility_summary.total_claims > 0 or credibility_summary.unresolved >= 0,
    ]
    base_score = sum(1 for item in checks if item) / len(checks) * 60.0
    metric_score = 0.0
    if latest_document.source_name == "idx_financial_reports":
        present = {metric.metric_name for metric in latest_metrics}
        metric_score = (len(present & CORE_FINANCIAL_METRICS) / len(CORE_FINANCIAL_METRICS)) * 40.0
    else:
        metric_score = 40.0 if latest_metrics else 0.0
    return round(base_score + metric_score, 1)


def _credibility_band(summary: ClaimCredibilitySummary) -> str:
    if summary.total_claims == 0:
        return "unknown"
    if summary.contradicted or summary.missed:
        return "poor"
    if summary.achieved_rate >= 0.6 and summary.contradicted == 0:
        return "high"
    if summary.achieved or summary.partially_supported:
        return "acceptable"
    return "unknown"


def _derive_stance(
    *,
    overall_state: str,
    thesis_state: str,
    credibility_state: str,
    has_critical_rules: bool,
    has_open_reviews: bool,
    data_completeness: bool,
) -> tuple[str, str]:
    if thesis_state == "broken":
        return "Avoid", "high"
    if overall_state == "critical" and has_critical_rules:
        return "Review manually", "high"
    if overall_state == "critical" and thesis_state != "broken":
        return "Review manually", "medium"
    if credibility_state == "poor" and has_open_reviews:
        return "Avoid", "medium"
    if thesis_state == "weakening" and has_critical_rules:
        return "Review manually", "medium"
    if thesis_state == "weakening" and not has_critical_rules:
        return "Watch", "medium"
    if thesis_state == "improving" and overall_state == "stable" and not has_critical_rules:
        return "Buy", "high"
    if thesis_state == "holding" and overall_state == "stable" and not has_critical_rules:
        return "Watch", "high"
    if thesis_state == "holding" and overall_state == "watch":
        return "Watch", "medium"
    if thesis_state in {"none", "unsupported"}:
        return "Watch", "low"
    if not data_completeness:
        return "Review manually", "low"
    return "Watch", "low"


def _summarize_deltas(deltas: list[DeltaRecord], *, limit: int) -> list[str]:
    summaries: list[str] = []
    for delta in deltas:
        if delta.delta_kind == "metric_change" and delta.metric_name:
            metric_label = delta.metric_name.replace("_", " ")
            if delta.percent_change is not None:
                summaries.append(f"{metric_label} changed {delta.percent_change:+.1f}% versus baseline.")
            elif delta.absolute_change is not None:
                summaries.append(f"{metric_label} changed by {delta.absolute_change:+,.0f} versus baseline.")
        elif delta.delta_kind == "event_addition" and delta.metric_name:
            summaries.append(f"New event detected: {delta.metric_name.replace('_', ' ')}.")
        elif delta.delta_kind == "event_resolution" and delta.metric_name:
            summaries.append(f"Prior event cleared: {delta.metric_name.replace('_', ' ')}.")
        elif delta.delta_kind == "rule_addition" and delta.metric_name:
            summaries.append(f"New rule trigger: {delta.metric_name.replace('_', ' ')}.")
        elif delta.delta_kind == "rule_resolution" and delta.metric_name:
            summaries.append(f"Rule cleared: {delta.metric_name.replace('_', ' ')}.")
        elif delta.delta_kind == "narrative_change":
            summaries.append("Management narrative changed versus the prior filing.")
        if len(summaries) >= limit:
            break
    return summaries


def _build_watch_items(
    *,
    latest_analysis: AnalysisSummary | None,
    events: list[EventSignal],
    triggered_rules: list[TriggeredRule],
    review_items: list[ReviewQueueItem],
    top_card: dict[str, Any],
) -> list[str]:
    items: list[str] = []
    if latest_analysis is not None:
        items.extend(str(item) for item in latest_analysis.what_to_verify[:3])
    for review_item in review_items:
        if review_item.rationale:
            items.append(review_item.rationale)
    for rule in triggered_rules:
        if rule.severity in {"warning", "critical"}:
            items.append(rule.rationale or f"Watch active rule {rule.rule_name}.")
    for event in events:
        if (event.severity or "") in {"warning", "critical"}:
            items.append(event.rationale or f"Watch event {event.event_type}.")
    if top_card.get("comparability_state") == "not_comparable":
        items.append("Wait for a cleaner comparable baseline before upgrading conviction.")
    return _dedupe_keep_order(items)[:3]


def _build_improvement_items(
    *,
    thesis_evaluations: list[ThesisEvaluationRecord],
    review_items: list[ReviewQueueItem],
    top_card: dict[str, Any],
) -> list[str]:
    items: list[str] = []
    for evaluation in thesis_evaluations:
        details = json.loads(evaluation.payload_json or "{}")
        matched_positive = {_condition_key(record) for record in details.get("matched_positive", [])}
        for record in details.get("positive_conditions", []):
            if _condition_key(record) in matched_positive:
                continue
            items.append(_condition_to_improvement_text(record))
        for record in details.get("matched_warning", []):
            items.append(_condition_to_resolution_text(record))
    for item in review_items:
        if item.priority == "high":
            items.append(item.rationale or f"Resolve {item.item_type.replace('_', ' ')} before upgrading.")
    if top_card.get("overall_state") == "critical":
        items.append("Clear critical rules or events before upgrading the stance.")
    return _dedupe_keep_order(items)[:4]


def _build_thesis_breakers(thesis_evaluations: list[ThesisEvaluationRecord]) -> list[str]:
    items: list[str] = []
    for evaluation in thesis_evaluations:
        details = json.loads(evaluation.payload_json or "{}")
        for record in details.get("stop_conditions", []):
            items.append(_condition_to_breaker_text(record))
    return _dedupe_keep_order(items)[:4]


def _build_quality_flags(
    *,
    deltas: list[DeltaRecord],
    events: list[EventSignal],
    triggered_rules: list[TriggeredRule],
) -> list[str]:
    metric_changes = {delta.metric_name: delta for delta in deltas if delta.delta_kind == "metric_change" and delta.metric_name}
    flags: list[str] = []
    net_profit = metric_changes.get("net_profit")
    cash_flow = metric_changes.get("operating_cash_flow")
    liabilities = metric_changes.get("total_liabilities")
    revenue = metric_changes.get("revenue")
    if net_profit and cash_flow and (net_profit.absolute_change or 0) > 0 and (cash_flow.absolute_change or 0) < 0:
        flags.append("Profit improved while operating cash flow deteriorated.")
    if liabilities and revenue and (liabilities.percent_change or 0) > (revenue.percent_change or 0):
        flags.append("Debt grew faster than revenue.")
    event_types = {event.event_type for event in events}
    if event_types & {"auditor_change"}:
        flags.append("Auditor changed.")
    if event_types & {"rights_issue", "private_placement", "capital_raise", "share_issue", "warrant_issue"}:
        flags.append("Equity issuance or dilution signal remains active.")
    if event_types & {"lawsuit", "regulatory_action", "regulator_supervision", "special_supervision", "business_restriction"}:
        flags.append("Legal or regulatory risk is active.")
    if any(rule.rule_name == "cash_flow_deterioration" and rule.severity in {"warning", "critical"} for rule in triggered_rules):
        flags.append("Cash-flow quality remains weak.")
    return _dedupe_keep_order(flags)[:5]


def _estimate_next_catalyst(
    *,
    filing_expectations: list[FilingExpectationRecord],
    profile: CompanyProfile,
    latest_document: CompanyDocument,
    latest_metrics: list[MetricPoint],
) -> dict[str, Any]:
    del profile, latest_document, latest_metrics
    schedule = next(
        (
            expectation
            for expectation in filing_expectations
            if expectation.state in {"upcoming", "due"}
        ),
        None,
    )
    if schedule is not None:
        return {
            "label": f"{schedule.period_label} due",
            "window_start": schedule.window_start,
            "window_end": schedule.deadline_at,
            "reason": json.loads(schedule.payload_json).get("reason", "Derived from filing expectation model."),
            "report_type": schedule.obligation_type,
        }
    return {
        "label": "No catalyst estimate yet",
        "window_start": None,
        "window_end": None,
        "reason": "Insufficient timing metadata exists for this issuer.",
        "report_type": None,
    }


_EXPECTATION_WINDOW_DAYS = {
    "quarterly_fs": 14,
    "interim_fs": 21,
    "annual_fs": 30,
    "annual_report": 30,
}


def _build_filing_expectations(
    *,
    profile: CompanyProfile,
    filing_documents: list[FilingDocument],
    now: date,
) -> list[FilingExpectationRecord]:
    expectations: list[FilingExpectationRecord] = []
    for year in range(now.year - 2, now.year + 2):
        for template in _filing_deadline_templates(profile.fiscal_year_end_month, around_year=year):
            satisfied = _match_filing_expectation(template, filing_documents)
            if satisfied is not None:
                state = "satisfied"
            elif now < template["window_start"]:
                state = "upcoming"
            elif now <= template["deadline_at"]:
                state = "due"
            else:
                state = "late"
            expectations.append(
                FilingExpectationRecord(
                    expectation_id=_stable_id(
                        "expectation",
                        profile.ticker,
                        template["obligation_type"],
                        template["period_end_date"],
                        DERIVED_VERSION,
                    ),
                    ticker=profile.ticker,
                    obligation_type=str(template["obligation_type"]),
                    fiscal_year=int(template["fiscal_year"]),
                    period_label=str(template["period_label"]),
                    period_end_date=str(template["period_end_date"]),
                    window_start=str(template["window_start"].isoformat()),
                    deadline_at=str(template["deadline_at"].isoformat()),
                    satisfied_at=satisfied["as_of"] if satisfied is not None else None,
                    state=state,
                    knowledge_source=profile.fiscal_year_end_source,
                    satisfied_document_id=satisfied["document_id"] if satisfied is not None else None,
                    payload_json=json.dumps(
                        {
                            "reason": template["reason"],
                            "report_type": template["obligation_type"],
                            "fiscal_year_end_month": profile.fiscal_year_end_month,
                        },
                        sort_keys=True,
                    ),
                )
            )
    expectations.sort(key=lambda item: (item.deadline_at, item.obligation_type))
    return expectations


def _filing_deadline_templates(fiscal_year_end_month: int, *, around_year: int) -> list[dict[str, Any]]:
    fiscal_year_end = _month_end(around_year, fiscal_year_end_month)
    q1_end = _shift_months(fiscal_year_end, -9)
    h1_end = _shift_months(fiscal_year_end, -6)
    q3_end = _shift_months(fiscal_year_end, -3)
    templates = [
        ("quarterly_fs", "Q1 filing", q1_end, q1_end + timedelta(days=30)),
        ("interim_fs", "H1 filing", h1_end, h1_end + timedelta(days=60)),
        ("quarterly_fs", "Q3 filing", q3_end, q3_end + timedelta(days=30)),
        ("annual_fs", "Annual FS", fiscal_year_end, fiscal_year_end + timedelta(days=120)),
        ("annual_report", "Annual report", fiscal_year_end, fiscal_year_end + timedelta(days=120)),
    ]
    rows: list[dict[str, Any]] = []
    for obligation_type, period_label, period_end, deadline_at in templates:
        window_days = _EXPECTATION_WINDOW_DAYS[obligation_type]
        rows.append(
            {
                "obligation_type": obligation_type,
                "period_label": period_label,
                "period_end_date": period_end.isoformat(),
                "fiscal_year": period_end.year,
                "window_start": deadline_at - timedelta(days=window_days),
                "deadline_at": deadline_at,
                "reason": f"Derived from fiscal year end month {fiscal_year_end_month:02d} using the v3.3 filing expectation model.",
            }
        )
    return rows


def _match_filing_expectation(
    template: dict[str, Any],
    filing_documents: list[FilingDocument],
) -> dict[str, str] | None:
    expected_period_end = str(template["period_end_date"])
    obligation_type = str(template["obligation_type"])
    for document in filing_documents:
        if obligation_type == "annual_report":
            if document.document_type != "annual_report":
                continue
        else:
            if document.document_type != "financial_statement":
                continue
        if expected_period_end in document.period_end_dates:
            return {"document_id": document.document_id, "as_of": document.as_of}
    return None


def _month_end(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def _shift_months(anchor: date, months: int) -> date:
    month_index = (anchor.year * 12 + (anchor.month - 1)) + months
    year = month_index // 12
    month = (month_index % 12) + 1
    return _month_end(year, month)


def _filing_deadlines(fiscal_year_end_month: int, *, around_year: int) -> list[tuple[date, str, str, int, str]]:
    return [
        (
            row["deadline_at"],
            f"{row['period_label']} due",
            row["period_end_date"],
            index,
            row["obligation_type"],
        )
        for index, row in enumerate(_filing_deadline_templates(fiscal_year_end_month, around_year=around_year), start=1)
    ]


def _next_expected_filing_window(
    *,
    fiscal_year_end_month: int,
    now: date,
    latest_period_end: date | None,
    latest_document_type: str | None,
) -> dict[str, str] | None:
    candidates: list[tuple[date, str, str, int, str]] = []
    for year in range(now.year - 1, now.year + 3):
        candidates.extend(_filing_deadlines(fiscal_year_end_month, around_year=year))

    def _candidate_priority(item: tuple[date, str, str, int, str]) -> tuple[date, int, int, str]:
        due_date, _label, period_end_iso, order, report_type = item
        period_end = _parse_iso_date(period_end_iso)
        special_rank = 0
        if (
            latest_document_type == "financial_statement"
            and latest_period_end is not None
            and period_end == latest_period_end
            and report_type == "annual_report"
        ):
            special_rank = -1
        return (due_date, special_rank, order, report_type)

    candidates.sort(key=_candidate_priority)
    for due_date, label, period_end_iso, _order, report_type in candidates:
        period_end = _parse_iso_date(period_end_iso)
        if due_date <= now:
            continue
        if latest_period_end is not None and period_end is not None and period_end < latest_period_end:
            continue
        if (
            latest_document_type == "financial_statement"
            and report_type == "annual_fs"
            and latest_period_end is not None
            and period_end == latest_period_end
        ):
            continue
        if (
            latest_document_type == "annual_report"
            and report_type == "annual_report"
            and latest_period_end is not None
            and period_end == latest_period_end
        ):
            continue
        window_days = _EXPECTATION_WINDOW_DAYS[report_type]
        return {
            "label": label,
            "window_start": (due_date - timedelta(days=window_days)).isoformat(),
            "window_end": due_date.isoformat(),
            "reason": f"Estimated from fiscal year end month {fiscal_year_end_month:02d} using the v3.3 filing expectation model.",
            "report_type": report_type,
        }
    return None


def _build_segment_summary(latest_metrics: list[MetricPoint]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for metric in latest_metrics:
        if metric.metric_name not in {"segment_revenue", "segment_profit"}:
            continue
        segment_name = (metric.consolidation_scope or "").removeprefix("segment:").strip() or "Unknown"
        record = grouped.setdefault(
            segment_name,
            {
                "segment_name": segment_name,
                "revenue": None,
                "profit": None,
                "period_end_date": metric.period_end_date,
                "fiscal_year": metric.fiscal_year,
            },
        )
        if metric.metric_name == "segment_revenue":
            record["revenue"] = metric.value_normalized
        else:
            record["profit"] = metric.value_normalized
    rows = list(grouped.values())
    rows.sort(key=lambda item: (item["revenue"] is None, -(item["revenue"] or 0), item["segment_name"]))
    return rows[:8]


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
        positive_conditions: list[dict[str, Any]] = []
        warning_conditions: list[dict[str, Any]] = []
        stop_conditions: list[dict[str, Any]] = []
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
            if condition.severity == "positive":
                positive_conditions.append(record)
            elif condition.severity == "stop":
                stop_conditions.append(record)
            else:
                warning_conditions.append(record)
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
            "positive_conditions": positive_conditions,
            "warning_conditions": warning_conditions,
            "stop_conditions": stop_conditions,
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
    return "holding", 0.6, "No warning or stop conditions are currently active."


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
    return "holding"


def _watchlist_priority_for(
    thesis_state: str,
    overall_state: str,
    review_items: list[ReviewQueueItem],
    decision_state: str,
) -> str:
    if decision_state in {"Avoid", "Review manually"} or thesis_state == "broken" or overall_state == "critical":
        return "high"
    if review_items or thesis_state in {"weakening", "improving"} or overall_state in {"review_required", "watch"}:
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


def _condition_key(record: dict[str, Any]) -> str:
    return json.dumps(record, sort_keys=True)


def _condition_to_improvement_text(record: dict[str, Any]) -> str:
    kind = str(record.get("kind") or "")
    payload = record.get("payload") or {}
    if kind == "metric_delta":
        metric_name = str(payload.get("metric_name") or "metric").replace("_", " ")
        operator = str(payload.get("operator") or "gte")
        value = payload.get("value")
        operator_text = {
            "gt": ">",
            "gte": ">=",
            "lt": "<",
            "lte": "<=",
            "eq": "=",
        }.get(operator, operator)
        return f"Need {metric_name} change {operator_text} {value}."
    if kind == "rule_absent":
        return f"Need rule {str(payload.get('rule_name') or 'unknown').replace('_', ' ')} to stay clear."
    if kind == "event_absent":
        return f"Need event {str(payload.get('event_type') or 'unknown').replace('_', ' ')} to stay absent."
    if kind == "event_present":
        return f"Need confirming event {str(payload.get('event_type') or 'unknown').replace('_', ' ')}."
    if kind == "review_item_present":
        return f"Need human review on {str(payload.get('item_type') or 'unknown').replace('_', ' ')}."
    return "Need more evidence to improve the thesis."


def _condition_to_resolution_text(record: dict[str, Any]) -> str:
    kind = str(record.get("kind") or "")
    payload = record.get("payload") or {}
    if kind == "rule_present":
        return f"Resolve active rule {str(payload.get('rule_name') or 'unknown').replace('_', ' ')}."
    if kind == "event_present":
        return f"Resolve active event {str(payload.get('event_type') or 'unknown').replace('_', ' ')}."
    if kind == "review_item_present":
        return f"Close review item {str(payload.get('item_type') or 'unknown').replace('_', ' ')}."
    return "Resolve the active warning condition."


def _condition_to_breaker_text(record: dict[str, Any]) -> str:
    kind = str(record.get("kind") or "")
    payload = record.get("payload") or {}
    if kind == "rule_present":
        return f"Thesis breaks if {str(payload.get('rule_name') or 'unknown').replace('_', ' ')} triggers."
    if kind == "event_present":
        return f"Thesis breaks if {str(payload.get('event_type') or 'unknown').replace('_', ' ')} appears."
    if kind == "metric_delta":
        return _condition_to_improvement_text(record).replace("Need", "Thesis breaks if")
    return "Thesis has a stop-rule condition."


def _dedupe_keep_order(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


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
    cfg: Config,
    profile: CompanyProfile,
    current_state: ComparableCompanyState,
) -> tuple[PeerSetRecord, dict[str, Any]]:
    candidate_profiles = _load_sector_profiles(conn, cfg, sector=profile.sector, exclude_ticker=profile.ticker)
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
    cfg: Config,
    profile: CompanyProfile,
    current_state: ComparableCompanyState,
) -> tuple[SectorRollupRecord, dict[str, Any]]:
    sector_profiles = _load_sector_profiles(conn, cfg, sector=profile.sector)
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
    cfg: Config,
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
        SELECT ticker, issuer_name, sector, subsector, industry, listing_board, active_status, fiscal_year_end_month
        FROM companies
        {where}
        ORDER BY CASE WHEN active_status = 'listed' THEN 0 ELSE 1 END, ticker ASC
        """,
        tuple(params),
    ).fetchall()
    profiles: list[CompanyProfile] = []
    for row in rows:
        fiscal_year_end_month, fiscal_year_end_source = resolve_fiscal_year_end_month(
            conn,
            cfg,
            ticker=row["ticker"],
            stored_month=int(row["fiscal_year_end_month"] or 12),
        )
        profiles.append(
            CompanyProfile(
                ticker=row["ticker"],
                issuer_name=row["issuer_name"],
                sector=row["sector"],
                subsector=row["subsector"],
                industry=row["industry"],
                listing_board=row["listing_board"],
                active_status=row["active_status"],
                fiscal_year_end_month=fiscal_year_end_month,
                fiscal_year_end_source=fiscal_year_end_source,
            )
        )
    return profiles


def _load_review_status_map(conn: sqlite3.Connection, *, ticker: str) -> dict[str, str]:
    rows = conn.execute(
        """
        SELECT source_key, status
        FROM review_queue_items
        WHERE ticker = ?
        """,
        (ticker,),
    ).fetchall()
    return {str(row["source_key"]): str(row["status"]) for row in rows}


def _filter_active_review_items(
    items: list[ReviewQueueItem],
    *,
    review_statuses: dict[str, str],
) -> list[ReviewQueueItem]:
    return [
        item
        for item in items
        if review_statuses.get(item.source_key, "open") == "open"
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

    for expectation in bundle.filing_expectations:
        conn.execute(
            """
            INSERT INTO filing_expectations (
                expectation_id, ticker, obligation_type, fiscal_year, period_label, period_end_date,
                window_start, deadline_at, satisfied_at, state, knowledge_source, satisfied_document_id,
                payload_json, derived_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, obligation_type, period_end_date, derived_version)
            DO UPDATE SET
                expectation_id = excluded.expectation_id,
                fiscal_year = excluded.fiscal_year,
                period_label = excluded.period_label,
                period_end_date = excluded.period_end_date,
                window_start = excluded.window_start,
                deadline_at = excluded.deadline_at,
                satisfied_at = excluded.satisfied_at,
                state = excluded.state,
                knowledge_source = excluded.knowledge_source,
                satisfied_document_id = excluded.satisfied_document_id,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (
                expectation.expectation_id,
                expectation.ticker,
                expectation.obligation_type,
                expectation.fiscal_year,
                expectation.period_label,
                expectation.period_end_date,
                expectation.window_start,
                expectation.deadline_at,
                expectation.satisfied_at,
                expectation.state,
                expectation.knowledge_source,
                expectation.satisfied_document_id,
                expectation.payload_json,
                DERIVED_VERSION,
                now,
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
