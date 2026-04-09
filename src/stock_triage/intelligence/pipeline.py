"""Phase 11 change detection and snapshot assembly over the Phase 10 foundation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..config import Config
from ..db import WriterQueue, connect, configure_pragmas, run_migrations

DERIVED_VERSION = "phase11-v1"


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
class CompanyIntelligenceBundle:
    profile: CompanyProfile
    latest_document: CompanyDocument
    baseline: BaselineChoice | None
    latest_analysis: AnalysisSummary | None
    deltas: list[DeltaRecord]
    timeseries: list[TimeseriesPoint]
    triggered_rules: list[TriggeredRule]
    events: list[EventSignal]
    snapshot_payload_json: str
    brief_payload_json: str
    review_items: list[ReviewQueueItem]
    peer_set: PeerSetRecord
    sector_rollup: SectorRollupRecord


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
    conflict_items = _build_conflict_review_items(
        ticker=ticker,
        latest_document=latest_document,
        baseline=baseline,
        deltas=deltas,
    )
    snapshot_payload_json = _build_snapshot_payload_json(
        profile=profile,
        latest_document=latest_document,
        baseline=baseline,
        latest_analysis=latest_analysis,
        latest_metrics=latest_metrics,
        deltas=deltas,
        events=latest_events,
        triggered_rules=latest_rules,
        conflicts=conflict_items,
    )
    brief_payload_json = _build_brief_payload_json(
        ticker=ticker,
        latest_analysis=latest_analysis,
        deltas=deltas,
        events=latest_events,
        triggered_rules=latest_rules,
        baseline=baseline,
    )
    review_items = _build_review_items(
        ticker=ticker,
        document_id=latest_document.document_id,
        events=latest_events,
        triggered_rules=latest_rules,
    )
    review_items.extend(conflict_items)
    peer_set = _build_peer_set(conn, profile)
    sector_rollup = _build_sector_rollup(conn, profile)
    return CompanyIntelligenceBundle(
        profile=profile,
        latest_document=latest_document,
        baseline=baseline,
        latest_analysis=latest_analysis,
        deltas=deltas,
        timeseries=timeseries,
        triggered_rules=latest_rules,
        events=latest_events,
        snapshot_payload_json=snapshot_payload_json,
        brief_payload_json=brief_payload_json,
        review_items=review_items,
        peer_set=peer_set,
        sector_rollup=sector_rollup,
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
) -> str:
    health = _build_health_state(
        baseline=baseline,
        deltas=deltas,
        events=events,
        triggered_rules=triggered_rules,
        conflicts=conflicts,
    )
    delta_counts: dict[str, int] = {}
    for delta in deltas:
        delta_counts[delta.delta_kind] = delta_counts.get(delta.delta_kind, 0) + 1
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

    return {
        "overall_state": overall_state,
        "comparability_state": comparability_state,
        "confidence": confidence,
        "leverage_state": leverage_state,
        "disclosure_quality": disclosure_quality,
        "positive_factors": positive_factors[:5],
        "negative_factors": negative_factors[:5],
        "reasons": reasons[:8],
    }


def _build_peer_set(conn: sqlite3.Connection, profile: CompanyProfile) -> PeerSetRecord:
    sector = profile.sector or "unknown"
    peer_rows = conn.execute(
        """
        SELECT ticker
        FROM companies
        WHERE sector = ?
          AND ticker != ?
        ORDER BY ticker ASC
        LIMIT 10
        """,
        (sector, profile.ticker),
    ).fetchall()
    peer_tickers = [row["ticker"] for row in peer_rows]
    peer_key = f"sector:{sector}"
    rationale = f"Peers selected from the same sector ({sector})."
    return PeerSetRecord(
        peer_set_id=_stable_id("peers", profile.ticker, peer_key, DERIVED_VERSION),
        ticker=profile.ticker,
        peer_key=peer_key,
        peer_tickers_json=json.dumps(peer_tickers, sort_keys=True),
        rationale=rationale,
    )


def _build_sector_rollup(conn: sqlite3.Connection, profile: CompanyProfile) -> SectorRollupRecord:
    sector = profile.sector or "unknown"
    subsector = profile.subsector
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS company_count,
            COUNT(DISTINCT CASE WHEN d.process_state = 'analyzed' THEN c.ticker END) AS analyzed_tickers
        FROM companies c
        LEFT JOIN documents d ON d.ticker = c.ticker
        WHERE c.sector = ?
        """,
        (sector,),
    ).fetchone()
    payload = {
        "sector": sector,
        "subsector": subsector,
        "company_count": row["company_count"] or 0,
        "analyzed_tickers": row["analyzed_tickers"] or 0,
    }
    return SectorRollupRecord(
        sector_rollup_id=_stable_id("sector", sector, subsector or "", "coverage", DERIVED_VERSION),
        sector=sector,
        subsector=subsector,
        rollup_key="coverage",
        payload_json=json.dumps(payload, sort_keys=True),
    )


def _persist_bundle(conn: sqlite3.Connection, bundle: CompanyIntelligenceBundle) -> dict[str, int]:
    now = datetime.now(timezone.utc).isoformat()
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
