"""Your holdings, valued at the latest IDX close (``market.portfolio_path``).

The file is yours and stays on this machine; see config/portfolio.example.yaml.
One lot is 100 shares. Profit and loss is against your average price and
leaves out dividends and fees; dividend income is shown on its own, as the
cash paid per share over the past year times your shares.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .metrics import Universe

LOT = 100


class HoldingsFileError(ValueError):
    """The holdings file could not be read."""


@dataclass(frozen=True)
class Position:
    ticker: str
    lots: float
    avg_price: float
    note: str | None = None


@dataclass(frozen=True)
class Holdings:
    path: Path
    cash: float
    positions: tuple[Position, ...]


def load_holdings(path: Path) -> Holdings | None:
    """The holdings in ``path``, or ``None`` when there is no file."""
    if not path.exists():
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise HoldingsFileError(f"{path}: not valid YAML: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("holdings") or [], list):
        raise HoldingsFileError(f"{path}: expected 'cash' and a 'holdings' list (see config/portfolio.example.yaml)")
    positions = []
    for index, entry in enumerate(raw.get("holdings") or []):
        if not isinstance(entry, dict):
            raise HoldingsFileError(f"{path}: holding {index + 1} needs ticker, lots and avg_price on their own lines")
        ticker = str(entry.get("ticker") or "").strip().upper()
        try:
            lots = float(entry.get("lots"))
            avg_price = float(entry.get("avg_price"))
        except (TypeError, ValueError):
            raise HoldingsFileError(f"{path}: holding {index + 1} needs a ticker, lots and avg_price as numbers") from None
        if not ticker or lots <= 0 or avg_price <= 0:
            raise HoldingsFileError(f"{path}: holding {index + 1} needs a ticker and positive lots and avg_price")
        positions.append(Position(ticker=ticker, lots=lots, avg_price=avg_price, note=entry.get("note")))
    try:
        cash = float(raw.get("cash") or 0)
    except (TypeError, ValueError):
        raise HoldingsFileError(f"{path}: cash must be a number of rupiah") from None
    return Holdings(path=path, cash=cash, positions=tuple(positions))


def portfolio_view(universe: Universe, holdings: Holdings) -> dict[str, Any]:
    """Each position's value, gain and flags, and the whole portfolio's mix and risk."""
    positions = []
    for position in holdings.positions:
        row = universe.by_ticker.get(position.ticker)
        shares = position.lots * LOT
        cost = shares * position.avg_price
        if row is None:
            positions.append({"ticker": position.ticker, "lots": position.lots, "avg_price": position.avg_price, "cost": cost, "missing": True})
            continue
        value = shares * row["close"]
        change = row.get("change_1d_pct")
        per_share = row.get("dividends_12m")
        positions.append(
            {
                "ticker": position.ticker,
                "name": row["name"],
                "sector": row.get("sector"),
                "lots": position.lots,
                "avg_price": position.avg_price,
                "close": row["close"],
                "cost": cost,
                "value": value,
                "gain": value - cost,
                "gain_pct": (value / cost - 1.0) * 100.0 if cost else None,
                "day_change": value - value / (1.0 + change / 100.0) if change is not None else None,
                "change_1d_pct": change,
                "beta": row.get("beta"),
                "volatility_pct": row.get("volatility_pct"),
                "below_high_pct": row.get("below_high_pct"),
                "pe": row.get("pe"),
                "dividend_yield_pct": row.get("dividend_yield_pct"),
                "dividend_income": shares * per_share if per_share is not None else None,
                "scores": row.get("scores"),
                "flags": row.get("flags", []),
                "note": position.note,
            }
        )
    priced = [p for p in positions if not p.get("missing")]
    invested = sum(p["value"] for p in priced)
    total = invested + holdings.cash
    for p in priced:
        p["weight_pct"] = p["value"] / total * 100.0 if total else None
    sectors: dict[str, float] = {}
    for p in priced:
        sectors[p.get("sector") or "Unclassified"] = sectors.get(p.get("sector") or "Unclassified", 0.0) + p["value"]
    betas = [(p["value"], p["beta"]) for p in priced if p.get("beta") is not None]
    cost = sum(p["cost"] for p in priced)
    day = [p["day_change"] for p in priced if p.get("day_change") is not None]
    largest = max(priced, key=lambda p: p["value"], default=None)
    income = [p["dividend_income"] for p in priced if p.get("dividend_income") is not None]
    return {
        "path": str(holdings.path),
        "positions": sorted(positions, key=lambda p: -(p.get("value") or 0.0)),
        "cash": holdings.cash,
        "invested": invested,
        "total": total,
        "cost": cost,
        "gain": invested - cost,
        "gain_pct": (invested / cost - 1.0) * 100.0 if cost else None,
        "day_change": sum(day) if day else None,
        "day_change_pct": sum(day) / (invested - sum(day)) * 100.0 if day and invested - sum(day) else None,
        "cash_pct": holdings.cash / total * 100.0 if total else None,
        "sectors": [
            {"sector": name, "value": value, "weight_pct": value / total * 100.0 if total else None}
            for name, value in sorted(sectors.items(), key=lambda item: -item[1])
        ],
        # Weighted by value: how much the stocks tend to move when IHSG moves 1%.
        "beta": sum(value * beta for value, beta in betas) / sum(value for value, _ in betas) if betas else None,
        "largest": {"ticker": largest["ticker"], "weight_pct": largest["weight_pct"]} if largest else None,
        "flag_count": sum(1 for p in priced for flag in p["flags"] if flag["level"] == "risk"),
        # Cash the holdings paid per share over the past year, times your shares.
        "dividend_income": sum(income) if income else None,
        "dividend_yield_pct": sum(income) / invested * 100.0 if income and invested else None,
        "yield_on_cost_pct": sum(income) / cost * 100.0 if income and cost else None,
        "median_gain_pct": statistics.median([p["gain_pct"] for p in priced if p["gain_pct"] is not None]) if priced else None,
    }
