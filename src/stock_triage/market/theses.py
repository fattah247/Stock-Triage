"""Market conditions for theses (docs/market_risk_plan.md, component 7).

Four condition kinds read the market layer's stored results:

- ``market_regime``: ``at_least: stressed`` (or ``equals: crisis``);
- ``market_signal``: ``signal: foreign_sell_streak`` with ``operator``/``value``,
  or ``level_at_least: stressed``;
- ``crash_risk``: ``operator``/``value`` against the estimate's probability. It
  counts only while the estimate beats its base rate; otherwise the condition
  is unsupported rather than matched on an untrustworthy number;
- ``exposure_score``: ``operator``/``value`` against the thesis company's score,
  or, without a ticker, against every company in scope (the watchlist when one
  is set).

A thesis whose conditions are all market kinds is a market thesis: it is
evaluated once per run here, after ``market-signals`` and ``crash-risk``, not
once per company by ``intelligence-run``. Company theses may mix in market
kinds too; those are checked whenever the company is derived.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..intelligence.thesis import (
    LocalThesisCondition,
    LocalThesisDefinition,
    ThesisEvaluationRecord,
    compare_numeric,
    load_active_theses,
    status_for_thesis,
)

MARKET_CONDITION_KINDS = frozenset({"market_regime", "market_signal", "crash_risk", "exposure_score"})
MARKET_THESIS_VERSION = "market-thesis-v1"
_REGIME_RANK = {"calm": 0, "elevated": 1, "stressed": 2, "crisis": 3}
_LEVEL_RANK = {"none": 0, "elevated": 1, "stressed": 2, "crisis": 3}
_OPERATOR_TEXT = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "eq": "="}


@dataclass(frozen=True)
class MarketContext:
    trade_date: str | None = None
    regime: str | None = None
    signals: dict[str, tuple[float | None, str]] = field(default_factory=dict)
    crash_probability: float | None = None  # only while the estimate is trustworthy
    crash_trade_date: str | None = None
    exposure: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class ConditionResult:
    matched: bool | None  # None: the input is missing, so the condition is unsupported
    observed: str


def is_market_thesis(thesis: LocalThesisDefinition) -> bool:
    return bool(thesis.conditions) and all(condition.kind in MARKET_CONDITION_KINDS for condition in thesis.conditions)


def load_market_context(conn: sqlite3.Connection) -> MarketContext:
    regime = conn.execute("SELECT trade_date, regime FROM market_regime_days ORDER BY trade_date DESC LIMIT 1").fetchone()
    signals: dict[str, tuple[float | None, str]] = {}
    if regime is not None:
        for row in conn.execute(
            "SELECT signal_name, value, level FROM market_signal_runs WHERE trade_date = ?", (regime["trade_date"],)
        ):
            signals[row["signal_name"]] = (row["value"], row["level"])
    crash = conn.execute(
        "SELECT trade_date, probability, trustworthy FROM crash_risk_days ORDER BY trade_date DESC LIMIT 1"
    ).fetchone()
    exposure = {
        row["ticker"]: float(row["score"])
        for row in conn.execute(
            """
            SELECT e.ticker, e.score
            FROM exposure_scores e
            WHERE e.as_of_date = (SELECT MAX(e2.as_of_date) FROM exposure_scores e2 WHERE e2.ticker = e.ticker)
            """
        )
    }
    trustworthy = crash is not None and bool(crash["trustworthy"]) and crash["probability"] is not None
    return MarketContext(
        trade_date=regime["trade_date"] if regime is not None else None,
        regime=regime["regime"] if regime is not None else None,
        signals=signals,
        crash_probability=float(crash["probability"]) if trustworthy else None,
        crash_trade_date=crash["trade_date"] if crash is not None else None,
        exposure=exposure,
    )


def match_market_condition(
    condition: LocalThesisCondition,
    context: MarketContext,
    *,
    ticker: str | None,
    scope: set[str] | None = None,
) -> ConditionResult:
    """Whether one market condition holds; ``matched`` is None when its input is missing."""
    payload = condition.payload
    kind = condition.kind
    if kind == "market_regime":
        if context.regime is None:
            return ConditionResult(None, "no market state yet")
        observed = f"market state {context.regime} on {context.trade_date}"
        if payload.get("equals"):
            return ConditionResult(context.regime == str(payload["equals"]), observed)
        target = str(payload.get("at_least") or "stressed")
        return ConditionResult(_REGIME_RANK.get(context.regime, 0) >= _REGIME_RANK.get(target, 99), observed)
    if kind == "market_signal":
        name = str(payload.get("signal") or "")
        value, level = context.signals.get(name, (None, "no_data"))
        if level == "no_data":
            return ConditionResult(None, f"{name} has no reading")
        observed = f"{name} {_format(value)} ({level})"
        if payload.get("level_at_least"):
            target = str(payload["level_at_least"])
            return ConditionResult(_LEVEL_RANK.get(level, 0) >= _LEVEL_RANK.get(target, 99), observed)
        if value is None:
            return ConditionResult(None, f"{name} has no value")
        return ConditionResult(_compare(value, payload), observed)
    if kind == "crash_risk":
        if context.crash_probability is None:
            reason = "no crash-risk estimate yet" if context.crash_trade_date is None else "the crash-risk estimate does not beat its base rate"
            return ConditionResult(None, reason)
        observed = f"crash risk {context.crash_probability:.0%} on {context.crash_trade_date}"
        return ConditionResult(_compare(context.crash_probability, payload), observed)
    if kind == "exposure_score":
        target = (payload.get("ticker") or ticker or "").upper()
        if target:
            score = context.exposure.get(target)
            if score is None:
                return ConditionResult(None, f"{target} has no exposure score")
            return ConditionResult(_compare(score, payload), f"{target} exposure {score:.0f}/100")
        candidates = {name: score for name, score in context.exposure.items() if scope is None or name in scope}
        if not candidates:
            return ConditionResult(None, "no exposure scores in scope")
        matched = sorted((name for name, score in candidates.items() if _compare(score, payload)), key=lambda n: -candidates[n])
        if matched:
            listed = ", ".join(f"{name} {candidates[name]:.0f}/100" for name in matched[:5])
            return ConditionResult(True, listed)
        top = max(candidates, key=candidates.get)
        return ConditionResult(False, f"highest exposure {top} {candidates[top]:.0f}/100")
    return ConditionResult(None, f"{kind} is not a market condition")


def evaluate_market_theses(
    conn: sqlite3.Connection,
    *,
    watchlist: set[str],
    now: datetime | None = None,
) -> list[ThesisEvaluationRecord]:
    """One evaluation per market thesis for the latest market day."""
    now = now or datetime.now(timezone.utc)
    context = load_market_context(conn)
    if context.trade_date is None:
        return []
    scope = watchlist or None
    records: list[ThesisEvaluationRecord] = []
    for thesis in load_active_theses(conn):
        if not is_market_thesis(thesis):
            continue
        groups: dict[str, list[dict[str, Any]]] = {"positive": [], "warning": [], "stop": []}
        matched: dict[str, list[dict[str, Any]]] = {"positive": [], "warning": [], "stop": []}
        unsupported: list[dict[str, Any]] = []
        for condition in thesis.conditions:
            result = match_market_condition(condition, context, ticker=thesis.ticker, scope=scope)
            severity = condition.severity if condition.severity in groups else "warning"
            record = {
                "severity": condition.severity,
                "kind": condition.kind,
                "payload": condition.payload,
                "observed": result.observed,
            }
            groups[severity].append(record)
            if result.matched is None:
                unsupported.append(record)
            elif result.matched:
                matched[severity].append(record)
        status, score, rationale = status_for_thesis(
            matched_positive=matched["positive"],
            matched_warning=matched["warning"],
            matched_stop=matched["stop"],
            unsupported=unsupported,
        )
        active = matched["stop"] + matched["warning"] + matched["positive"]
        if active:
            rationale = f"{rationale} {'; '.join(_describe(record) for record in active)}."
        payload = {
            "ticker": thesis.ticker,
            "trade_date": context.trade_date,
            "since": _status_since(conn, thesis.thesis_id, status=status, trade_date=context.trade_date),
            "positive_conditions": groups["positive"],
            "warning_conditions": groups["warning"],
            "stop_conditions": groups["stop"],
            "matched_positive": matched["positive"],
            "matched_warning": matched["warning"],
            "matched_stop": matched["stop"],
            "unsupported": unsupported,
            "review_cadence": thesis.review_cadence,
            "notes": thesis.notes,
        }
        records.append(
            ThesisEvaluationRecord(
                thesis_evaluation_id=_stable_id("market-thesis-eval", thesis.thesis_id, thesis.ticker, context.trade_date),
                thesis_id=thesis.thesis_id,
                ticker=thesis.ticker,
                document_id=None,
                status=status,
                score=score,
                rationale=rationale,
                payload_json=json.dumps(payload, sort_keys=True),
            )
        )
    return records


def persist_market_thesis_evaluations(
    conn: sqlite3.Connection, *, records: list[ThesisEvaluationRecord], now: datetime | None = None
) -> None:
    evaluated_at = (now or datetime.now(timezone.utc)).isoformat()
    conn.executemany(
        """
        INSERT OR REPLACE INTO thesis_evaluations (
            thesis_evaluation_id, thesis_id, ticker, document_id, status, score, rationale,
            payload_json, derived_version, evaluated_at
        ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                record.thesis_evaluation_id,
                record.thesis_id,
                record.ticker,
                record.status,
                record.score,
                record.rationale,
                record.payload_json,
                MARKET_THESIS_VERSION,
                evaluated_at,
            )
            for record in records
        ],
    )


def run_market_thesis_evaluation(conn: sqlite3.Connection, *, watchlist: set[str]) -> list[ThesisEvaluationRecord]:
    """Evaluate and store market theses on a writer connection."""
    records = evaluate_market_theses(conn, watchlist=watchlist)
    persist_market_thesis_evaluations(conn, records=records)
    return records


def _status_since(conn: sqlite3.Connection, thesis_id: str, *, status: str, trade_date: str) -> str:
    # The first day of the current run of this status, so alerts fire once per run.
    row = conn.execute(
        """
        SELECT status, payload_json FROM thesis_evaluations
        WHERE thesis_id = ? AND derived_version = ? AND json_extract(payload_json, '$.trade_date') < ?
        ORDER BY json_extract(payload_json, '$.trade_date') DESC
        LIMIT 1
        """,
        (thesis_id, MARKET_THESIS_VERSION, trade_date),
    ).fetchone()
    if row is not None and row["status"] == status:
        return str(json.loads(row["payload_json"]).get("since") or trade_date)
    return trade_date


def _compare(value: float, payload: dict[str, Any]) -> bool:
    return compare_numeric(float(value), str(payload.get("operator") or "gte"), float(payload.get("value", 0)))


def _operator_text(payload: dict[str, Any]) -> str:
    operator = str(payload.get("operator") or "gte")
    return _OPERATOR_TEXT.get(operator, operator)


def _describe(record: dict[str, Any]) -> str:
    payload = record["payload"]
    kind = record["kind"]
    if kind == "market_regime":
        rule = f"state {payload['equals']}" if payload.get("equals") else f"state at least {payload.get('at_least') or 'stressed'}"
    elif kind == "market_signal" and payload.get("level_at_least"):
        rule = f"{payload.get('signal')} at least {payload['level_at_least']}"
    elif kind == "market_signal":
        rule = f"{payload.get('signal')} {_operator_text(payload)} {payload.get('value')}"
    elif kind == "crash_risk":
        rule = f"crash risk {_operator_text(payload)} {payload.get('value')}"
    else:
        rule = f"exposure {_operator_text(payload)} {payload.get('value')}"
    return f"{rule} ({record['observed']})"


def _format(value: float | None) -> str:
    return "n/a" if value is None else f"{value:g}"


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "|".join("" if part is None else str(part) for part in parts)
    return f"{prefix}-{hashlib.sha1(payload.encode('utf-8')).hexdigest()[:24]}"
