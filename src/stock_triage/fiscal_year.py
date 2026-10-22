from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import yaml

from .config import Config

# Months between the end of each reporting period and the fiscal year end.
FISCAL_PERIOD_MONTHS_BEFORE_YEAR_END = {"Q1": 9, "H1": 6, "Q3": 3, "Annual": 0}
# IDX financial-report ``periode`` codes (TW = triwulan, quarter).
IDX_REPORT_PERIODS = {"tw1": "Q1", "tw2": "H1", "tw3": "Q3", "audit": "Annual"}


def month_end(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def fiscal_period_end(fiscal_year: int, fiscal_period: str, fiscal_year_end_month: int) -> date:
    """Period end for ``fiscal_period`` of the fiscal year ending in ``fiscal_year``."""
    year_end_index = fiscal_year * 12 + (fiscal_year_end_month - 1)
    month_index = year_end_index - FISCAL_PERIOD_MONTHS_BEFORE_YEAR_END[fiscal_period]
    return month_end(month_index // 12, month_index % 12 + 1)


def load_fiscal_year_overrides(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    values = raw.get("fiscal_year_end_months", raw)
    if not isinstance(values, dict):
        return {}
    overrides: dict[str, int] = {}
    for ticker, month in values.items():
        try:
            month_int = int(month)
        except (TypeError, ValueError):
            continue
        if 1 <= month_int <= 12:
            overrides[str(ticker).upper()] = month_int
    return overrides


def save_fiscal_year_override(path: Path, *, ticker: str, month: int | None) -> dict[str, int]:
    overrides = load_fiscal_year_overrides(path)
    ticker_key = ticker.upper().strip()
    if month is None:
        overrides.pop(ticker_key, None)
    else:
        if not 1 <= month <= 12:
            raise ValueError("month must be between 1 and 12")
        overrides[ticker_key] = month
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"fiscal_year_end_months": dict(sorted(overrides.items()))}
    path.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")
    return overrides


def infer_fiscal_year_end_month(conn, *, ticker: str) -> int | None:
    row = conn.execute(
        """
        SELECT mi.period_end_date
        FROM metric_items mi
        JOIN documents d ON d.document_id = mi.document_id
        WHERE d.ticker = ?
          AND mi.period_end_date IS NOT NULL
          AND COALESCE(mi.fiscal_period, '') = 'Annual'
          AND d.document_type IN ('annual_report', 'financial_statement')
        ORDER BY mi.period_end_date DESC
        LIMIT 1
        """,
        (ticker,),
    ).fetchone()
    if row is None or not row["period_end_date"]:
        return None
    try:
        return int(str(row["period_end_date"])[5:7])
    except (TypeError, ValueError):
        return None


def resolve_fiscal_year_end_month(
    conn,
    cfg: Config,
    *,
    ticker: str,
    stored_month: int | None,
) -> tuple[int, str]:
    overrides = load_fiscal_year_overrides(cfg.company_overrides.definition_path)
    ticker_key = ticker.upper()
    if ticker_key in overrides:
        return overrides[ticker_key], "override"

    if stored_month is not None and stored_month != 12:
        return stored_month, "company"

    inferred = infer_fiscal_year_end_month(conn, ticker=ticker_key)
    if inferred is not None:
        return inferred, "inferred"

    return int(stored_month or 12), "default"
