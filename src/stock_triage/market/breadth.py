"""Fundamental stress breadth: how widely stress is spreading across filers.

For each fiscal year, the share of companies with negative operating cash
flow, negative equity, or a net loss, market-wide and per sector, and how
much those shares rose versus the prior year. Plan: docs/market_risk_plan.md,
component 2 (catalog L).

The rise compares only companies that report the figure in both years.
Otherwise a year with more filers, or different ones, would look like a
change in stress.

Going-concern warnings are left out for now: the event is keyword-matched and
also fires on standard auditor's-report wording, so its share would be noise.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone

from ..db import preferred_metric_sql

BREADTH_VERSION = "breadth-v1"
LEVELS = ("none", "elevated", "stressed", "crisis")
_BREADTH_METRICS = ("operating_cash_flow", "equity", "net_profit")
_SHARES = (
    ("negative_ocf_share", "operating_cash_flow"),
    ("negative_equity_share", "equity"),
    ("net_loss_share", "net_profit"),
)


@dataclass(frozen=True)
class BreadthRow:
    scope: str
    fiscal_year: int
    filer_count: int
    shares: dict[str, float | None]
    change_pts: float | None
    level: str
    details: dict[str, object]


def compute_breadth(
    facts: dict[tuple[str, int], dict[str, float]],
    *,
    sectors: dict[str, str | None],
    thresholds_pts: dict[str, float],
    min_filers: int,
) -> list[BreadthRow]:
    """``facts`` maps (ticker, fiscal year) to that year's annual figures."""
    scoped: dict[tuple[str, int], dict[str, dict[str, float]]] = {}
    for (ticker, fiscal_year), figures in facts.items():
        scoped.setdefault(("market", fiscal_year), {})[ticker] = figures
        if sectors.get(ticker):
            scoped.setdefault((f"sector:{sectors[ticker]}", fiscal_year), {})[ticker] = figures

    rows: list[BreadthRow] = []
    for (scope, fiscal_year), by_ticker in sorted(scoped.items()):
        previous = scoped.get((scope, fiscal_year - 1), {})
        rises: dict[str, float] = {}
        matched: dict[str, int] = {}
        for share_name, metric_name in _SHARES:
            common = [
                ticker
                for ticker, figures in by_ticker.items()
                if metric_name in figures and metric_name in previous.get(ticker, {})
            ]
            matched[share_name] = len(common)
            if len(common) >= min_filers:
                now = sum(1 for ticker in common if by_ticker[ticker][metric_name] < 0) / len(common)
                before = sum(1 for ticker in common if previous[ticker][metric_name] < 0) / len(common)
                rises[share_name] = round((now - before) * 100.0, 1)
        change_pts = max(rises.values()) if rises else None
        rows.append(
            BreadthRow(
                scope=scope,
                fiscal_year=fiscal_year,
                filer_count=len(by_ticker),
                shares=_shares(list(by_ticker.values())),
                change_pts=change_pts,
                level=_level(change_pts, thresholds_pts) if rises else "insufficient_data",
                details={"rise_pts": rises, "matched_filers": matched, "min_filers": min_filers},
            )
        )
    return rows


def load_annual_facts(conn: sqlite3.Connection) -> dict[tuple[str, int], dict[str, float]]:
    placeholders = ",".join("?" for _ in _BREADTH_METRICS)
    rows = conn.execute(
        f"""
        SELECT mi.ticker, mi.fiscal_year, mi.metric_name, mi.value_normalized
        FROM metric_items mi
        JOIN documents d ON d.document_id = mi.document_id
        WHERE mi.ticker IS NOT NULL
          AND mi.fiscal_year IS NOT NULL
          AND mi.fiscal_period = 'Annual'
          AND mi.value_normalized IS NOT NULL
          AND mi.metric_name IN ({placeholders})
          AND COALESCE(mi.consolidation_scope, '') NOT LIKE 'segment:%'
          AND d.process_state IN ('extracted', 'analyzed')
          AND {preferred_metric_sql("mi")}
        ORDER BY mi.ticker, mi.fiscal_year, mi.metric_name, (d.parse_strategy = 'xlsx') DESC, mi.extracted_at DESC
        """,
        _BREADTH_METRICS,
    ).fetchall()
    facts: dict[tuple[str, int], dict[str, float]] = {}
    for row in rows:
        facts.setdefault((row["ticker"], int(row["fiscal_year"])), {}).setdefault(
            row["metric_name"], float(row["value_normalized"])
        )
    return facts


def persist_breadth(conn: sqlite3.Connection, *, rows: list[BreadthRow], as_of: date) -> None:
    computed_at = datetime.now(timezone.utc).isoformat()
    conn.execute("DELETE FROM fundamental_breadth WHERE as_of_date = ?", (as_of.isoformat(),))
    conn.executemany(
        """
        INSERT INTO fundamental_breadth (
            as_of_date, scope, fiscal_year, filer_count, negative_ocf_share, negative_equity_share,
            net_loss_share, change_pts, level, details_json, derived_version, computed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                as_of.isoformat(),
                row.scope,
                row.fiscal_year,
                row.filer_count,
                row.shares["negative_ocf_share"],
                row.shares["negative_equity_share"],
                row.shares["net_loss_share"],
                row.change_pts,
                row.level,
                json.dumps(row.details, sort_keys=True),
                BREADTH_VERSION,
                computed_at,
            )
            for row in rows
        ],
    )


def level_at_least(level: str, floor: str) -> bool:
    if level not in LEVELS or floor not in LEVELS:
        return False
    return LEVELS.index(level) >= LEVELS.index(floor)


def _shares(figures: list[dict[str, float]]) -> dict[str, float | None]:
    """Shares among all of the year's filers, for display; the rise uses matched filers."""
    shares: dict[str, float | None] = {}
    for share_name, metric_name in _SHARES:
        values = [item[metric_name] for item in figures if metric_name in item]
        shares[share_name] = (sum(1 for value in values if value < 0) / len(values)) if values else None
    return shares


def _level(change_pts: float | None, thresholds_pts: dict[str, float]) -> str:
    if change_pts is None:
        return "none"
    for level in ("crisis", "stressed", "elevated"):
        if change_pts >= thresholds_pts.get(level, float("inf")):
            return level
    return "none"
