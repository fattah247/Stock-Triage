"""Phase 6 rule engine: deterministic flags over normalized metrics and events."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from ..config import Config
from ..db import WriterQueue, connect, configure_pragmas, run_migrations

RULESET_VERSION = "phase6-v1"


@dataclass(frozen=True)
class RuleDocument:
    document_id: str
    source_name: str
    source_record_id: str
    ticker: str | None
    process_state: str


@dataclass(frozen=True)
class MetricValue:
    metric_name: str
    value_normalized: float | None
    fiscal_year: int | None
    fiscal_period: str | None
    period_end_date: str | None
    raw_value_text: str | None


@dataclass(frozen=True)
class EventSignal:
    event_type: str
    severity: str | None
    matched_pattern: str | None


@dataclass(frozen=True)
class RuleOutcome:
    rule_name: str
    severity: str
    threshold: str
    input_values: dict[str, object]
    rationale: str
    triggered: bool


@dataclass(frozen=True)
class RuleRunResult:
    selected: int = 0
    analyzed: int = 0
    triggered: int = 0


@dataclass(frozen=True)
class RuleStatusRow:
    source_name: str
    total: int
    pending: int
    analyzed: int
    triggered: int


@dataclass(frozen=True)
class RuleTriggerRow:
    source_name: str
    document_id: str
    source_record_id: str
    rule_name: str
    severity: str
    evaluated_at: str


@dataclass(frozen=True)
class RuleStatusReport:
    rows: list[RuleStatusRow]
    recent_triggers: list[RuleTriggerRow]


def run_rules(
    cfg: Config,
    *,
    source_name: str | None = None,
    limit: int = 25,
    include_analyzed: bool = False,
) -> RuleRunResult:
    cfg.ensure_dirs()

    result = RuleRunResult()
    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
        reader = connect(cfg.paths.db_path, read_only=True)
        try:
            configure_pragmas(reader, cfg)
            documents = _select_documents(
                reader,
                source_name=source_name,
                limit=limit,
                include_analyzed=include_analyzed,
            )
            result = RuleRunResult(selected=len(documents))
            for document in documents:
                metrics = _load_metrics(reader, document.document_id)
                events = _load_events(reader, document.document_id)
                outcomes = _evaluate_document(document, metrics, events)
                triggered = sum(1 for outcome in outcomes if outcome.triggered)
                wq.submit(
                    lambda conn, d=document, o=outcomes: _persist_rule_outcomes(conn, document=d, outcomes=o)
                )
                result = RuleRunResult(
                    selected=result.selected,
                    analyzed=result.analyzed + 1,
                    triggered=result.triggered + triggered,
                )
        finally:
            reader.close()
    return result


def load_rule_status(
    cfg: Config,
    *,
    source_name: str | None = None,
    trigger_limit: int = 10,
) -> RuleStatusReport:
    cfg.ensure_dirs()
    conn = connect(cfg.paths.db_path, read_only=True)
    try:
        configure_pragmas(conn, cfg)
        params: list[object] = []
        where = ""
        if source_name:
            where = "WHERE d.source_name = ?"
            params.append(source_name)
        rows = conn.execute(
            f"""
            SELECT
                d.source_name,
                COUNT(*) AS total,
                SUM(CASE WHEN d.process_state = 'extracted' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN d.process_state = 'analyzed' THEN 1 ELSE 0 END) AS analyzed,
                COALESCE((
                    SELECT COUNT(*)
                    FROM rule_runs rr
                    JOIN documents rd ON rd.document_id = rr.document_id
                    WHERE rd.source_name = d.source_name AND rr.triggered = 1
                ), 0) AS triggered
            FROM documents d
            {where}
            GROUP BY d.source_name
            ORDER BY d.source_name
            """,
            tuple(params),
        ).fetchall()
        triggers = conn.execute(
            f"""
            SELECT
                d.source_name,
                d.document_id,
                d.source_record_id,
                rr.rule_name,
                rr.severity,
                rr.evaluated_at
            FROM rule_runs rr
            JOIN documents d ON d.document_id = rr.document_id
            {where.replace('d.', 'd.')}
            {"AND" if where else "WHERE"} rr.triggered = 1
            ORDER BY rr.evaluated_at DESC, rr.rule_name ASC
            LIMIT ?
            """,
            tuple(params + [trigger_limit]),
        ).fetchall()
        return RuleStatusReport(
            rows=[
                RuleStatusRow(
                    source_name=row["source_name"],
                    total=row["total"],
                    pending=row["pending"] or 0,
                    analyzed=row["analyzed"] or 0,
                    triggered=row["triggered"] or 0,
                )
                for row in rows
            ],
            recent_triggers=[
                RuleTriggerRow(
                    source_name=row["source_name"],
                    document_id=row["document_id"],
                    source_record_id=row["source_record_id"],
                    rule_name=row["rule_name"],
                    severity=row["severity"],
                    evaluated_at=row["evaluated_at"],
                )
                for row in triggers
            ],
        )
    finally:
        conn.close()


def _select_documents(
    conn: sqlite3.Connection,
    *,
    source_name: str | None,
    limit: int,
    include_analyzed: bool,
) -> list[RuleDocument]:
    states = ["extracted"]
    if include_analyzed:
        states.append("analyzed")
    placeholders = ",".join("?" for _ in states)
    sql = f"""
        SELECT document_id, source_name, source_record_id, ticker, process_state
        FROM documents
        WHERE process_state IN ({placeholders})
    """
    params: list[object] = list(states)
    if source_name:
        sql += " AND source_name = ?"
        params.append(source_name)
    sql += " ORDER BY updated_at ASC, document_id ASC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, tuple(params)).fetchall()
    return [
        RuleDocument(
            document_id=row["document_id"],
            source_name=row["source_name"],
            source_record_id=row["source_record_id"],
            ticker=row["ticker"],
            process_state=row["process_state"],
        )
        for row in rows
    ]


def _load_metrics(conn: sqlite3.Connection, document_id: str) -> list[MetricValue]:
    rows = conn.execute(
        """
        SELECT metric_name, value_normalized, fiscal_year, fiscal_period, period_end_date, raw_value_text
        FROM metric_items
        WHERE document_id = ?
        ORDER BY fiscal_year DESC, metric_name ASC
        """,
        (document_id,),
    ).fetchall()
    return [
        MetricValue(
            metric_name=row["metric_name"],
            value_normalized=row["value_normalized"],
            fiscal_year=row["fiscal_year"],
            fiscal_period=row["fiscal_period"],
            period_end_date=row["period_end_date"],
            raw_value_text=row["raw_value_text"],
        )
        for row in rows
    ]


def _load_events(conn: sqlite3.Connection, document_id: str) -> list[EventSignal]:
    rows = conn.execute(
        """
        SELECT event_type, severity, matched_pattern
        FROM events
        WHERE document_id = ?
        ORDER BY event_type ASC
        """,
        (document_id,),
    ).fetchall()
    return [
        EventSignal(
            event_type=row["event_type"],
            severity=row["severity"],
            matched_pattern=row["matched_pattern"],
        )
        for row in rows
    ]


def _evaluate_document(
    document: RuleDocument,
    metrics: list[MetricValue],
    events: list[EventSignal],
) -> list[RuleOutcome]:
    latest_metrics: dict[str, MetricValue] = {}
    for metric in metrics:
        latest_metrics.setdefault(metric.metric_name, metric)
    event_types = {event.event_type for event in events}

    outcomes = [
        _metric_lt_zero_rule(
            "profit_drop",
            severity="critical",
            threshold="net_profit < 0",
            metric=latest_metrics.get("net_profit"),
            rationale_hit="Net profit is negative for the latest extracted annual period.",
            rationale_miss="Net profit is not negative or is unavailable.",
        ),
        _metric_lt_zero_rule(
            "cash_flow_deterioration",
            severity="warning",
            threshold="operating_cash_flow < 0",
            metric=latest_metrics.get("operating_cash_flow"),
            rationale_hit="Operating cash flow is negative for the latest extracted annual period.",
            rationale_miss="Operating cash flow is not negative or is unavailable.",
        ),
        _ratio_rule(
            "debt_surge",
            severity="warning",
            threshold="total_liabilities / equity > 0.75",
            numerator=latest_metrics.get("total_liabilities"),
            denominator=latest_metrics.get("equity"),
            rationale_hit="Liabilities exceed the leverage threshold relative to equity.",
            rationale_miss="Leverage threshold not breached or required metrics are unavailable.",
        ),
        _event_rule(
            "auditor_change",
            severity="warning",
            threshold="event_type == auditor_change",
            event_types=event_types,
            watched={"auditor_change"},
            rationale_hit="The filing includes an auditor appointment or change event.",
            rationale_miss="No auditor-change event detected for this document.",
        ),
        _event_rule(
            "management_change",
            severity="warning",
            threshold="event_type == governance_appointment",
            event_types=event_types,
            watched={"governance_appointment"},
            rationale_hit="The filing includes a management or governance appointment event.",
            rationale_miss="No management-change event detected for this document.",
        ),
        _event_rule(
            "funding_event",
            severity="warning",
            threshold="event_type in {debt_facility, debt_refinancing, bond_issuance}",
            event_types=event_types,
            watched={"debt_facility", "debt_refinancing", "bond_issuance"},
            rationale_hit="The filing includes a financing-related event.",
            rationale_miss="No financing-related event detected for this document.",
        ),
        _event_rule(
            "suspension_notice",
            severity="warning",
            threshold="event_type == suspension_related_notice",
            event_types=event_types,
            watched={"suspension_related_notice"},
            rationale_hit="The filing references a trading suspension or relisting notice.",
            rationale_miss="No suspension-related event detected for this document.",
        ),
        _event_rule(
            "material_transaction",
            severity="info",
            threshold="event_type == material_transaction",
            event_types=event_types,
            watched={"material_transaction"},
            rationale_hit="The filing includes a material transaction signal.",
            rationale_miss="No material transaction event detected for this document.",
        ),
    ]
    del document
    return outcomes


def _metric_lt_zero_rule(
    rule_name: str,
    *,
    severity: str,
    threshold: str,
    metric: MetricValue | None,
    rationale_hit: str,
    rationale_miss: str,
) -> RuleOutcome:
    value = metric.value_normalized if metric is not None else None
    triggered = value is not None and value < 0
    return RuleOutcome(
        rule_name=rule_name,
        severity=severity,
        threshold=threshold,
        input_values={
            "metric_name": metric.metric_name if metric is not None else None,
            "value_normalized": value,
            "fiscal_year": metric.fiscal_year if metric is not None else None,
            "period_end_date": metric.period_end_date if metric is not None else None,
        },
        rationale=rationale_hit if triggered else rationale_miss,
        triggered=triggered,
    )


def _ratio_rule(
    rule_name: str,
    *,
    severity: str,
    threshold: str,
    numerator: MetricValue | None,
    denominator: MetricValue | None,
    rationale_hit: str,
    rationale_miss: str,
) -> RuleOutcome:
    ratio = None
    triggered = False
    if numerator is not None and denominator is not None and denominator.value_normalized not in (None, 0):
        ratio = (numerator.value_normalized or 0.0) / denominator.value_normalized
        triggered = ratio > 0.75
    return RuleOutcome(
        rule_name=rule_name,
        severity=severity,
        threshold=threshold,
        input_values={
            "numerator_metric": numerator.metric_name if numerator is not None else None,
            "numerator_value": numerator.value_normalized if numerator is not None else None,
            "denominator_metric": denominator.metric_name if denominator is not None else None,
            "denominator_value": denominator.value_normalized if denominator is not None else None,
            "ratio": ratio,
        },
        rationale=rationale_hit if triggered else rationale_miss,
        triggered=triggered,
    )


def _event_rule(
    rule_name: str,
    *,
    severity: str,
    threshold: str,
    event_types: set[str],
    watched: set[str],
    rationale_hit: str,
    rationale_miss: str,
) -> RuleOutcome:
    matched = sorted(event_types & watched)
    return RuleOutcome(
        rule_name=rule_name,
        severity=severity,
        threshold=threshold,
        input_values={"matched_event_types": matched},
        rationale=rationale_hit if matched else rationale_miss,
        triggered=bool(matched),
    )


def _persist_rule_outcomes(
    conn: sqlite3.Connection,
    *,
    document: RuleDocument,
    outcomes: list[RuleOutcome],
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("DELETE FROM rule_runs WHERE document_id = ?", (document.document_id,))
    for outcome in outcomes:
        conn.execute(
            """
            INSERT INTO rule_runs (
                rule_run_id, document_id, ticker, ruleset_version, rule_name,
                severity, threshold, input_values, rationale, triggered, evaluated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _stable_id(f"rule:{document.document_id}:{outcome.rule_name}:{RULESET_VERSION}"),
                document.document_id,
                document.ticker,
                RULESET_VERSION,
                outcome.rule_name,
                outcome.severity,
                outcome.threshold,
                json.dumps(outcome.input_values, sort_keys=True),
                outcome.rationale,
                1 if outcome.triggered else 0,
                now,
            ),
        )
    conn.execute(
        """
        UPDATE documents
        SET process_state = 'analyzed', ruleset_version = ?, updated_at = ?
        WHERE document_id = ?
        """,
        (RULESET_VERSION, now, document.document_id),
    )


def _stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
