"""Exposure ranking: how fragile each company is if the market breaks.

A deterministic score out of 100 with a plain reason for every point, built
from figures the pipeline already holds (xlsx-first, see
``stock_triage.db.preferred_metric_sql``). Plan: docs/market_risk_plan.md,
component 4.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from ..db import preferred_metric_sql
from .sectors import sector_code

EXPOSURE_VERSION = "exposure-v2"  # v2: active risk themes (component 5) join the score
# Bank liabilities are mostly deposits, so balance-sheet leverage says little
# about fragility there.
FINANCIAL_SECTORS = frozenset({"keuangan", "financials", "finance"})
_EXPOSURE_METRICS = ("total_assets", "total_liabilities", "equity", "operating_cash_flow", "net_profit")
# A watchlist this concentrated in one sector moves with that sector.
_CONCENTRATION_SHARE = 0.4
_CONCENTRATION_MIN_WATCHLIST = 3
_THEME_SECTOR_POINTS = {"high": 10, "medium": 5}
_THEME_SECTOR_CAP = 15
_THEME_NAMED_POINTS = 10


@dataclass(frozen=True)
class ExposureComponent:
    name: str
    points: float
    reason: str


@dataclass(frozen=True)
class ExposureScore:
    ticker: str
    score: float
    components: list[ExposureComponent]
    period: str | None
    sector: str | None
    source: str | None  # "xlsx" or "pdf": where the figures came from


def score_exposure(
    *,
    ticker: str,
    sector: str | None,
    metrics: dict[str, dict[str, object]],
    index_memberships: list[str],
    watchlist_sector_share: float | None,
    active_themes: list[dict[str, object]] = (),
    named_in: dict[str, int] | None = None,
) -> ExposureScore:
    """Score one company from its latest-period figures and the risk themes active now.

    ``named_in`` maps an active theme's title to how many of its recent headlines name the company.
    """
    components: list[ExposureComponent] = []
    value = {name: _number(metrics.get(name, {}).get("value")) for name in _EXPOSURE_METRICS}
    currency = next((str(row.get("currency")) for row in metrics.values() if row.get("currency")), None)
    is_financial = (sector or "").strip().lower() in FINANCIAL_SECTORS

    equity = value["equity"]
    if equity is not None and equity < 0:
        components.append(ExposureComponent("negative_equity", 30, f"Equity is negative ({_money(equity, currency)})"))

    assets, liabilities = value["total_assets"], value["total_liabilities"]
    if assets and liabilities is not None and assets > 0:
        ratio = liabilities / assets
        if is_financial:
            components.append(
                ExposureComponent("leverage", 0, f"Liabilities are {ratio:.0%} of assets; not scored for a financial company")
            )
        else:
            points = 25 if ratio >= 0.9 else 15 if ratio >= 0.7 else 5 if ratio >= 0.5 else 0
            if points:
                components.append(ExposureComponent("leverage", points, f"Liabilities are {ratio:.0%} of assets"))

    cash_flow = value["operating_cash_flow"]
    if cash_flow is not None and cash_flow < 0:
        components.append(
            ExposureComponent("cash_burn", 15, f"Operating cash flow is negative ({_money(cash_flow, currency)})")
        )

    net_profit = value["net_profit"]
    if net_profit is not None and net_profit < 0:
        components.append(ExposureComponent("net_loss", 15, f"Net loss of {_money(-net_profit, currency)}"))

    if currency and currency != "IDR":
        components.append(
            ExposureComponent("reporting_currency", 5, f"Reports in {currency}, so it is exposed to the rupiah rate")
        )

    if index_memberships:
        components.append(
            ExposureComponent(
                "index_membership",
                10,
                f"In {', '.join(index_memberships)}: index funds sell members first when foreigners leave",
            )
        )

    if watchlist_sector_share is not None and watchlist_sector_share > _CONCENTRATION_SHARE:
        components.append(
            ExposureComponent(
                "sector_concentration",
                5,
                f"{watchlist_sector_share:.0%} of the watchlist is in {sector or 'this sector'}",
            )
        )

    code = sector_code(sector)
    hitting = [theme for theme in active_themes if code and code in (theme.get("sectors") or [])]
    points = min(_THEME_SECTOR_CAP, sum(_THEME_SECTOR_POINTS.get(str(theme["severity"]), 0) for theme in hitting))
    if points:
        titles = ", ".join(str(theme["title"]) for theme in hitting[:3])
        components.append(ExposureComponent("theme_sector", points, f"Its sector is hit by active risk themes: {titles}"))
    if named_in:
        title, count = max(named_in.items(), key=lambda pair: pair[1])
        components.append(
            ExposureComponent("theme_named", _THEME_NAMED_POINTS, f"Named in {count} recent headline{'s' if count != 1 else ''} on {title}")
        )

    period_row = metrics.get("total_assets") or metrics.get("equity") or next(iter(metrics.values()), {})
    return ExposureScore(
        ticker=ticker,
        score=min(100.0, float(sum(component.points for component in components))),
        components=components,
        period=_period_label(period_row),
        sector=sector,
        source=str(period_row.get("source")) if period_row.get("source") else None,
    )


def compute_exposure_scores(
    conn: sqlite3.Connection,
    *,
    index_membership_path: Path,
    watchlist: set[str],
    active_themes: list[dict[str, object]] = (),
) -> list[ExposureScore]:
    sectors = {row["ticker"]: row["sector"] for row in conn.execute("SELECT ticker, sector FROM companies")}
    mentions = _theme_mentions(conn, active_themes)
    memberships = load_index_membership(index_membership_path)
    watchlist_sectors = Counter(sectors.get(ticker) for ticker in watchlist if sectors.get(ticker))
    scores: list[ExposureScore] = []
    for ticker, metrics in sorted(_latest_period_metrics(conn).items()):
        sector = sectors.get(ticker)
        share = None
        if ticker in watchlist and sector and len(watchlist) >= _CONCENTRATION_MIN_WATCHLIST:
            share = watchlist_sectors[sector] / len(watchlist)
        scores.append(
            score_exposure(
                ticker=ticker,
                sector=sector,
                metrics=metrics,
                index_memberships=memberships.get(ticker, []),
                watchlist_sector_share=share,
                active_themes=active_themes,
                named_in=mentions.get(ticker),
            )
        )
    return sorted(scores, key=lambda item: (-item.score, item.ticker))


def _theme_mentions(conn: sqlite3.Connection, active_themes: list[dict[str, object]], days: int = 3) -> dict[str, dict[str, int]]:
    """Tickers written in capitals in the recent headlines of each active theme."""
    if not active_themes:
        return {}
    titles = {str(theme["theme_id"]): str(theme["title"]) for theme in active_themes}
    tickers = {row["ticker"] for row in conn.execute("SELECT ticker FROM companies WHERE ticker IS NOT NULL")}
    placeholders = ",".join("?" for _ in titles)
    rows = conn.execute(
        f"""
        SELECT it.theme_id, wi.title
        FROM item_themes it JOIN watch_items wi ON wi.item_id = it.item_id
        WHERE it.theme_id IN ({placeholders})
          AND COALESCE(wi.published_at, wi.first_seen_at) >= datetime('now', ?)
        """,
        (*titles, f"-{days + 1} days"),
    ).fetchall()
    mentions: dict[str, Counter[str]] = {}
    for row in rows:
        for word in set(re.findall(r"\b[A-Z]{4}\b", row["title"])):
            if word in tickers:
                mentions.setdefault(word, Counter())[titles[row["theme_id"]]] += 1
    return {ticker: dict(counter) for ticker, counter in mentions.items()}


def persist_exposure_scores(conn: sqlite3.Connection, *, scores: list[ExposureScore], as_of: date) -> None:
    computed_at = datetime.now(timezone.utc).isoformat()
    conn.execute("DELETE FROM exposure_scores WHERE as_of_date = ?", (as_of.isoformat(),))
    conn.executemany(
        """
        INSERT INTO exposure_scores (ticker, as_of_date, score, components_json, derived_version, computed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                score.ticker,
                as_of.isoformat(),
                score.score,
                json.dumps(
                    {
                        "components": [asdict(component) for component in score.components],
                        "period": score.period,
                        "sector": score.sector,
                        "source": score.source,
                    },
                    sort_keys=True,
                ),
                EXPOSURE_VERSION,
                computed_at,
            )
            for score in scores
        ],
    )


def load_latest_exposure(conn: sqlite3.Connection, *, ticker: str) -> dict[str, object] | None:
    row = conn.execute(
        """
        SELECT ticker, as_of_date, score, components_json, derived_version
        FROM exposure_scores
        WHERE ticker = ?
        ORDER BY as_of_date DESC
        LIMIT 1
        """,
        (ticker,),
    ).fetchone()
    if row is None:
        return None
    return {
        "ticker": row["ticker"],
        "as_of_date": row["as_of_date"],
        "score": row["score"],
        "derived_version": row["derived_version"],
        **json.loads(row["components_json"]),
    }


def load_index_membership(path: Path) -> dict[str, list[str]]:
    """``{ticker: [index names]}`` from a hand-kept file; empty when absent."""
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    indexes = raw.get("indexes", {}) if isinstance(raw, dict) else {}
    memberships: dict[str, list[str]] = {}
    for index_name, tickers in (indexes or {}).items():
        for ticker in tickers or []:
            memberships.setdefault(str(ticker).upper(), []).append(str(index_name))
    return memberships


def _latest_period_metrics(conn: sqlite3.Connection) -> dict[str, dict[str, dict[str, object]]]:
    """Per ticker, the figures of its latest period that has a balance sheet."""
    placeholders = ",".join("?" for _ in _EXPOSURE_METRICS)
    rows = conn.execute(
        f"""
        SELECT
            mi.ticker,
            mi.metric_name,
            mi.value_normalized,
            mi.currency,
            mi.fiscal_year,
            mi.fiscal_period,
            mi.period_end_date,
            CASE WHEN d.parse_strategy = 'xlsx' THEN 'xlsx' ELSE 'pdf' END AS source
        FROM metric_items mi
        JOIN documents d ON d.document_id = mi.document_id
        WHERE mi.ticker IS NOT NULL
          AND mi.period_end_date IS NOT NULL
          AND mi.value_normalized IS NOT NULL
          AND mi.metric_name IN ({placeholders})
          AND COALESCE(mi.consolidation_scope, '') NOT LIKE 'segment:%'
          AND d.process_state IN ('extracted', 'analyzed')
          AND {preferred_metric_sql("mi")}
        ORDER BY mi.ticker, mi.period_end_date DESC, (d.parse_strategy = 'xlsx') DESC, mi.extracted_at DESC
        """,
        _EXPOSURE_METRICS,
    ).fetchall()
    by_period: dict[str, dict[str, dict[str, dict[str, object]]]] = {}
    for row in rows:
        period = by_period.setdefault(row["ticker"], {}).setdefault(row["period_end_date"], {})
        period.setdefault(
            row["metric_name"],
            {
                "value": row["value_normalized"],
                "currency": row["currency"],
                "fiscal_year": row["fiscal_year"],
                "fiscal_period": row["fiscal_period"],
                "source": row["source"],
            },
        )
    latest: dict[str, dict[str, dict[str, object]]] = {}
    for ticker, periods in by_period.items():
        for period_end in sorted(periods, reverse=True):
            if {"total_assets", "equity"} & periods[period_end].keys():
                latest[ticker] = periods[period_end]
                break
    return latest


def _number(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _period_label(row: dict[str, object]) -> str | None:
    year, period = row.get("fiscal_year"), row.get("fiscal_period")
    if not year:
        return None
    return f"FY {year}" if not period or period == "Annual" else f"{period} {year}"


def _money(value: float, currency: str | None) -> str:
    prefix = {"IDR": "Rp ", "USD": "US$ "}.get(currency or "", f"{currency} " if currency else "")
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    for size, suffix in ((1e12, " tn"), (1e9, " bn"), (1e6, " m")):
        if magnitude >= size:
            return f"{sign}{prefix}{magnitude / size:,.1f}{suffix}"
    return f"{sign}{prefix}{magnitude:,.0f}"
