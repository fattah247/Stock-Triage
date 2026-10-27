"""Price behaviour, valuation, quality, growth, scores and flags for every listed stock.

- **Prices** are adjusted for splits, reverse splits, rights and bonus shares:
  on the day of such an action IDX sets the stock's "previous" price to the
  adjusted one, and every earlier close is scaled by that ratio. Cash
  dividends are not added back, so returns are price returns.
- **Valuation** uses IDX's trailing EPS and book value per share from the
  latest monthly ratio table, at the latest close.
- **Growth** compares the latest statement's year-to-date sales and profit with
  the same period a year before.
- **Dividends** come from IDX's dividend announcements: the cash paid per share
  with an ex-date in the past year, US-dollar dividends at that day's JISDOR rate.
- **Scores** (0-100) rank a stock within its IDX-IC sector on each theme; 50 is
  the sector's middle. They describe; they are not a recommendation.
"""

from __future__ import annotations

import math
import sqlite3
import statistics
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

SESSIONS = {"1m": 21, "3m": 63, "6m": 126, "1y": 252}
YEAR = 252
COMPOSITE = "index:COMPOSITE"
USDIDR = "fx:USDIDR_JISDOR"
FINANCIALS = "Financials"
# A previous price this far from the last close means IDX adjusted it for a corporate action.
ADJUSTMENT_TOLERANCE = 0.005
# Below this average daily value a position is hard to enter or leave.
ILLIQUID_VALUE = 1e9
MIN_HISTORY_MONTHS = 12
# Fewer sessions than this give no 52-week range; under a year, it is the range since listing.
MIN_RANGE_SESSIONS = 60
MAX_UNTRADED_DAYS = 5
STALE_STATEMENT_DAYS = 270
NOTICE_DAYS = 30

SCORE_THEMES = {
    "value": "Cheap against its sector: high earnings and book value per rupiah of price.",
    "quality": "Profitable with a sound balance sheet: ROE, net margin and (outside financials) low debt.",
    "growth": "Sales and profit growing against the same period a year before.",
    "momentum": "Price beating IHSG over six months and a year.",
    "stability": "Calm price: low volatility and close to its 52-week high.",
}


@dataclass
class PriceHistory:
    dates: list[date]
    close: list[float]  # adjusted
    raw_close: list[float]
    volume: list[float]
    value: list[float]
    listed_shares: list[float | None]
    foreign_net_value: list[float | None]  # rupiah, estimated at the day's average price
    adjustments: list[tuple[date, float]] = field(default_factory=list)


@dataclass
class Universe:
    as_of: date | None
    rows: list[dict[str, Any]]
    by_ticker: dict[str, dict[str, Any]]
    sectors: list[str]


def build_universe(conn: sqlite3.Connection) -> Universe:
    """Every stock IDX listed on the latest synced day, with its numbers, scores and flags."""
    latest = conn.execute("SELECT MAX(trade_date) FROM stock_daily").fetchone()[0]
    if not latest:
        return Universe(as_of=None, rows=[], by_ticker={}, sectors=[])
    as_of = date.fromisoformat(latest)
    histories = load_price_histories(conn, since=as_of - timedelta(days=420))
    ihsg = _composite(conn)
    ratios = load_ratios(conn)
    names = {row["ticker"]: row["issuer_name"] for row in conn.execute("SELECT ticker, issuer_name FROM companies")}
    notices = _recent_notices(conn, since=as_of - timedelta(days=NOTICE_DAYS), until=as_of)
    suspended = _suspended_since(conn, until=as_of)
    dividends = load_dividends(conn)
    usd = _series(conn, USDIDR)
    have_dividends = bool(dividends)

    rows = []
    for ticker, history in histories.items():
        if not history.dates or history.dates[-1] != as_of:
            continue  # delisted, or missing from the latest summary
        row = {"ticker": ticker, "name": names.get(ticker) or ticker, "as_of": as_of.isoformat()}
        row.update(price_metrics(history, ihsg))
        latest_ratio = ratios.get(ticker, [])
        row.update(_classification(latest_ratio[-1] if latest_ratio else None))
        row.update(fundamental_metrics(latest_ratio, price=row["close"], as_of=as_of))
        if have_dividends:
            row.update(
                dividend_metrics(
                    dividends.get(ticker, []), price=row["close"], eps=row.get("eps"), as_of=as_of, usd=usd,
                    adjustments=history.adjustments,
                )
            )
        row["notices"] = notices.get(ticker, [])
        row["suspended_since"] = suspended.get(ticker)
        rows.append(row)
    _score(rows)
    _peer_medians(rows)
    for row in rows:
        row["flags"] = flags_for(row)
    rows.sort(key=lambda row: -(row.get("market_cap") or 0.0))
    sectors = sorted({row["sector"] for row in rows if row.get("sector")})
    return Universe(as_of=as_of, rows=rows, by_ticker={row["ticker"]: row for row in rows}, sectors=sectors)


def stock_detail(conn: sqlite3.Connection, universe: Universe, ticker: str) -> dict[str, Any] | None:
    """One stock's numbers with its full price history, ratio history, statements and peers."""
    ticker = ticker.upper()
    row = universe.by_ticker.get(ticker)
    if row is None:
        return None
    history = load_price_histories(conn, tickers=[ticker]).get(ticker)
    ihsg = _composite(conn)
    ratio_rows = load_ratios(conn, tickers=[ticker]).get(ticker, [])
    start = history.dates[0] if history and history.dates else None
    base = ihsg.get(start) if start else None
    prices = {
        "dates": [day.isoformat() for day in history.dates] if history else [],
        "close": [round(value, 2) for value in history.close] if history else [],
        "volume": history.volume if history else [],
        "value": history.value if history else [],
        "foreign_net_value": history.foreign_net_value if history else [],
        # IHSG on the same days, scaled to start at the stock's first close.
        "ihsg_rebased": [
            round(ihsg[day] / base * history.close[0], 2) if base and day in ihsg else None for day in history.dates
        ]
        if history
        else [],
        "adjustments": [{"date": day.isoformat(), "factor": round(factor, 6)} for day, factor in history.adjustments]
        if history
        else [],
    }
    statements = statements_by_date(ratio_rows)
    peers = _peers(universe, row)
    usd = _series(conn, USDIDR)
    paid = [
        {**dividend, "idr_per_share": _in_rupiah(dividend, usd)}
        for dividend in reversed(load_dividends(conn, tickers=[ticker]).get(ticker, []))
    ]
    return {
        "dividends": paid[:12],
        "row": row,
        "prices": prices,
        "ratio_history": [
            {key: ratio[key] for key in ("report_month", "fs_date", "per", "pbv", "roe", "roa", "npm", "der", "eps", "book_value")}
            for ratio in ratio_rows
        ],
        "statements": [
            {
                **{
                    key: statements[fs][key]
                    for key in ("fs_date", "sales", "profit_attr_owner", "profit_period", "assets", "liabilities", "equity", "eps", "book_value")
                },
                "fs_type": statement_kind(statements[fs]["fs_type"]),
            }
            for fs in sorted(statements)
        ],
        "peers": peers,
        "score_themes": SCORE_THEMES,
    }


# --- loading -----------------------------------------------------------------


def load_price_histories(
    conn: sqlite3.Connection, *, since: date | None = None, tickers: list[str] | None = None
) -> dict[str, PriceHistory]:
    clauses, params = [], []
    if since is not None:
        clauses.append("trade_date >= ?")
        params.append(since.isoformat())
    if tickers:
        clauses.append(f"ticker IN ({', '.join('?' for _ in tickers)})")
        params.extend(tickers)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    raw: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute(
        "SELECT ticker, trade_date, previous, close, volume, value, listed_shares, foreign_buy, foreign_sell "
        f"FROM stock_daily {where} ORDER BY ticker, trade_date",
        params,
    ):
        raw.setdefault(row["ticker"], []).append(row)
    position = {day: i for i, day in enumerate(trading_calendar(conn))}
    return {ticker: _history(rows, position) for ticker, rows in raw.items()}


def trading_calendar(conn: sqlite3.Connection) -> list[date]:
    """Every day IDX traded that this database knows of: IHSG closes, stock rows and the sync ledger."""
    days = set()
    for sql in (
        "SELECT trade_date FROM market_series_daily WHERE series_id = 'index:COMPOSITE'",
        "SELECT DISTINCT trade_date FROM stock_daily",
        "SELECT trade_date FROM market_sync_days WHERE source_name = 'idx_market' AND status = 'ok'",
    ):
        days.update(row[0] for row in conn.execute(sql))
    return sorted(date.fromisoformat(day) for day in days)


def load_ratios(conn: sqlite3.Connection, *, tickers: list[str] | None = None) -> dict[str, list[dict[str, Any]]]:
    where, params = "", []
    if tickers:
        where = f"WHERE ticker IN ({', '.join('?' for _ in tickers)})"
        params = list(tickers)
    out: dict[str, list[dict[str, Any]]] = {}
    for row in conn.execute(f"SELECT * FROM company_ratios {where} ORDER BY ticker, report_month", params):
        ratio = dict(row)
        ratio["fs_date"] = iso_day(ratio["fs_date"])
        out.setdefault(row["ticker"], []).append(ratio)
    return out


def iso_day(value: Any) -> str | None:
    """``YYYY-MM-DD`` from IDX's date text (with or without a time), or ``None``."""
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text).isoformat() if text else None
    except ValueError:
        return None


def _history(rows: list[sqlite3.Row], position: dict[date, int] | None = None) -> PriceHistory:
    rows = [row for row in rows if (row["close"] or 0) > 0]
    raw_close = [float(row["close"]) for row in rows]
    previous = [float(row["previous"] or 0) for row in rows]
    dates = [date.fromisoformat(row["trade_date"]) for row in rows]
    close, adjustments = adjust_closes(raw_close, previous, dates, position=position)
    foreign = []
    for row in rows:
        volume, value = row["volume"] or 0.0, row["value"] or 0.0
        if row["foreign_buy"] is None or row["foreign_sell"] is None or not volume:
            foreign.append(None if row["foreign_buy"] is None else 0.0)
            continue
        foreign.append((row["foreign_buy"] - row["foreign_sell"]) * value / volume)
    return PriceHistory(
        dates=[date.fromisoformat(row["trade_date"]) for row in rows],
        close=close,
        raw_close=raw_close,
        volume=[float(row["volume"] or 0) for row in rows],
        value=[float(row["value"] or 0) for row in rows],
        listed_shares=[row["listed_shares"] for row in rows],
        foreign_net_value=foreign,
        adjustments=adjustments,
    )


def adjust_closes(
    raw_close: list[float], previous: list[float], dates: list[date], *, position: dict[date, int] | None = None
) -> tuple[list[float], list[tuple[date, float]]]:
    """Closes scaled for every corporate action IDX adjusted the previous price for, newest unchanged.

    The previous price is compared with the stored close of the trading day just
    before; across a gap in the stored days (``position`` numbers the trading
    calendar) a difference is a missing day, not a corporate action.
    """
    adjusted = [0.0] * len(raw_close)
    adjustments: list[tuple[date, float]] = []
    multiplier = 1.0
    for i in range(len(raw_close) - 1, -1, -1):
        adjusted[i] = raw_close[i] * multiplier
        consecutive = position is None or (
            dates[i] in position and dates[i - 1] in position and position[dates[i]] - position[dates[i - 1]] == 1
        )
        if i > 0 and consecutive and raw_close[i - 1] > 0 and previous[i] > 0:
            factor = previous[i] / raw_close[i - 1]
            if abs(factor - 1.0) > ADJUSTMENT_TOLERANCE:
                multiplier *= factor
                adjustments.append((dates[i], factor))
    return adjusted, sorted(adjustments)


def load_dividends(conn: sqlite3.Connection, *, tickers: list[str] | None = None) -> dict[str, list[dict[str, Any]]]:
    where, params = "", []
    if tickers:
        where = f"WHERE ticker IN ({', '.join('?' for _ in tickers)})"
        params = list(tickers)
    # One dividend can arrive twice: listed under its record date before its
    # ex-date was known, revised in a later month, or from both of IDX's sources.
    # It is kept once, by record date, preferring the monthly table and its latest month.
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT ticker, ex_date, cash_per_share, currency, note, payment_date, record_date, cum_date, report_month, source "
        f"FROM dividends {where} ORDER BY ticker, ex_date",
        params,
    ):
        dividend = dict(row)
        key = (dividend["ticker"], dividend["record_date"] or dividend["ex_date"])
        rank = (dividend["source"] == "idx_dividends", dividend["report_month"] or "")
        if key not in best or rank > best[key]["_rank"]:
            best[key] = {**dividend, "_rank": rank}
    out: dict[str, list[dict[str, Any]]] = {}
    for dividend in sorted(best.values(), key=lambda d: (d["ticker"], d["ex_date"])):
        dividend.pop("_rank")
        out.setdefault(dividend["ticker"], []).append(dividend)
    return out


def _composite(conn: sqlite3.Connection) -> dict[date, float]:
    return _series(conn, COMPOSITE)


def _series(conn: sqlite3.Connection, series_id: str) -> dict[date, float]:
    return {
        date.fromisoformat(row["trade_date"]): float(row["value"])
        for row in conn.execute("SELECT trade_date, value FROM market_series_daily WHERE series_id = ?", (series_id,))
    }


def _suspended_since(conn: sqlite3.Connection, *, until: date) -> dict[str, str]:
    """Stocks whose latest IDX suspension has not been lifted, with its date."""
    latest: dict[str, dict[str, str]] = {}
    for row in conn.execute(
        "SELECT ticker, kind, MAX(notice_date) AS day FROM exchange_notices "
        "WHERE kind IN ('suspend', 'unsuspend') AND ticker IS NOT NULL AND notice_date <= ? GROUP BY ticker, kind",
        (until.isoformat(),),
    ):
        latest.setdefault(row["ticker"], {})[row["kind"]] = row["day"]
    return {
        ticker: kinds["suspend"]
        for ticker, kinds in latest.items()
        if "suspend" in kinds and kinds.get("unsuspend", "") < kinds["suspend"]
    }


def _recent_notices(conn: sqlite3.Connection, *, since: date, until: date) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for row in conn.execute(
        "SELECT ticker, kind, notice_date, title FROM exchange_notices "
        "WHERE notice_date >= ? AND notice_date <= ? AND ticker IS NOT NULL ORDER BY notice_date DESC",
        (since.isoformat(), until.isoformat()),
    ):
        out.setdefault(row["ticker"], []).append({"kind": row["kind"], "date": row["notice_date"], "title": row["title"]})
    return out


# --- per-stock numbers ---------------------------------------------------------


def price_metrics(history: PriceHistory, ihsg: dict[date, float]) -> dict[str, Any]:
    close, dates = history.close, history.dates
    n = len(close)
    last = close[-1]
    out: dict[str, Any] = {
        "close": history.raw_close[-1],
        "change_1d_pct": _pct(close[-1], close[-2]) if n > 1 else None,
    }
    for label, sessions in SESSIONS.items():
        stock = _pct(close[-1], close[-1 - sessions]) if n > sessions else None
        out[f"ret_{label}_pct"] = stock
        index = _index_return(ihsg, dates[-1 - sessions], dates[-1]) if n > sessions else None
        out[f"rel_{label}_pts"] = stock - index if stock is not None and index is not None else None
    year_end = bisect_right(dates, date(dates[-1].year - 1, 12, 31)) - 1
    out["ret_ytd_pct"] = _pct(last, close[year_end]) if year_end >= 0 else None

    window = close[-YEAR:]
    out["range_sessions"] = len(window)
    if len(window) >= MIN_RANGE_SESSIONS:
        high, low = max(window), min(window)
        # Adjusted closes keep the latest one as it is, so these are in today's terms.
        out["high_52w"], out["low_52w"] = high, low
        out["below_high_pct"] = (high - last) / high * 100.0 if high else None
        out["range_position_pct"] = (last - low) / (high - low) * 100.0 if high > low else None
    else:
        out["high_52w"] = out["low_52w"] = out["below_high_pct"] = out["range_position_pct"] = None
    out["above_ma200"] = last > statistics.fmean(close[-200:]) if n >= 200 else None

    out["untraded_days_20d"] = sum(1 for volume in history.volume[-20:] if not volume)
    # A stock that barely trades has a flat price: its volatility and beta would
    # read as the calmest in the market, so they are left empty.
    rarely_traded = out["untraded_days_20d"] > MAX_UNTRADED_DAYS
    returns = [close[i] / close[i - 1] - 1.0 for i in range(max(1, n - 60), n) if close[i - 1] > 0]
    out["volatility_pct"] = (
        statistics.pstdev(returns) * math.sqrt(YEAR) * 100.0 if len(returns) >= 20 and not rarely_traded else None
    )
    out["beta"] = None if rarely_traded else _beta(history, ihsg)

    recent_value = history.value[-20:]
    out["avg_value_20d"] = statistics.fmean(recent_value) if recent_value else None
    shares = history.listed_shares[-1]
    out["listed_shares"] = shares
    out["market_cap"] = history.raw_close[-1] * shares if shares else None
    foreign = [value for value in history.foreign_net_value[-20:] if value is not None]
    out["foreign_net_20d"] = sum(foreign) if foreign else None
    out["first_date"] = dates[0].isoformat()
    return out


def fundamental_metrics(ratios: list[dict[str, Any]], *, price: float, as_of: date) -> dict[str, Any]:
    if not ratios:
        return {"has_fundamentals": False}
    latest = ratios[-1]
    eps, book = latest["eps"], latest["book_value"]
    out: dict[str, Any] = {
        "has_fundamentals": True,
        "report_month": latest["report_month"],
        "fs_date": latest["fs_date"],
        "fs_type": statement_kind(latest["fs_type"]),
        "eps": eps,
        "book_value": book,
        "pe": price / eps if eps and eps > 0 else None,
        "pbv": price / book if book and book > 0 else None,
        "earnings_yield_pct": eps / price * 100.0 if eps is not None and price else None,
        "book_yield_pct": book / price * 100.0 if book is not None and price else None,
        "roe_pct": latest["roe"],
        "roa_pct": latest["roa"],
        # Profit over sales of the same period. IDX's own net margin divides a
        # trailing year's profit by year-to-date sales, which overstates it mid-year.
        "npm_pct": latest["profit_attr_owner"] / latest["sales"] * 100.0
        if latest["profit_attr_owner"] is not None and latest["sales"]
        else None,
        "der": latest["der"],
        "equity": latest["equity"],
        "sales": latest["sales"],
        "profit": latest["profit_attr_owner"],
        "loss_making": (eps is not None and eps < 0) or (latest["profit_attr_owner"] or 0) < 0,
        "sharia": bool(latest["sharia"]),
    }
    fs_date = _date(latest["fs_date"])
    out["statement_age_days"] = (as_of - fs_date).days if fs_date else None
    year_before = _year_before(latest["fs_date"]) if latest["fs_date"] else None
    prior = _statement_near(statements_by_date(ratios), year_before) if year_before else None
    out["sales_yoy_pct"] = _growth(latest["sales"], prior["sales"] if prior else None)
    out["profit_yoy_pct"] = _growth(latest["profit_attr_owner"], prior["profit_attr_owner"] if prior else None)
    out["pe_history_pct"] = _history_percentile(out["pe"], [row["per"] for row in ratios])
    out["pbv_history_pct"] = _history_percentile(out["pbv"], [row["pbv"] for row in ratios])
    return out


def dividend_metrics(
    paid: list[dict[str, Any]],
    *,
    price: float,
    eps: float | None,
    as_of: date,
    usd: dict[date, float],
    adjustments: list[tuple[date, float]] = (),
) -> dict[str, Any]:
    """Cash paid per share in the past year, its yield and payout, and how regularly it pays.

    A dividend paid before a split is scaled to today's shares, like the prices.
    """
    year_ago = (as_of - timedelta(days=365)).isoformat()
    recent = [d for d in paid if year_ago < d["ex_date"] <= as_of.isoformat()]

    def per_today_share(dividend: dict[str, Any]) -> float:
        scale = 1.0
        for day, factor in adjustments:
            if day.isoformat() > dividend["ex_date"]:
                scale *= factor
        return (_in_rupiah(dividend, usd) or 0.0) * scale

    per_share = sum(per_today_share(d) for d in recent)
    last = paid[-1] if paid else None
    return {
        "dividends_12m": per_share,
        "dividend_yield_pct": per_share / price * 100.0 if price else None,
        "payout_pct": per_share / eps * 100.0 if eps and eps > 0 and per_share else None,
        "dividend_years": len({d["ex_date"][:4] for d in paid if d["ex_date"] <= as_of.isoformat()}),
        "last_dividend": {"ex_date": last["ex_date"], "cash": last["cash_per_share"], "currency": last["currency"]} if last else None,
    }


def _in_rupiah(dividend: dict[str, Any], usd: dict[date, float]) -> float | None:
    if dividend["currency"] == "IDR":
        return dividend["cash_per_share"]
    if dividend["currency"] != "USD" or not usd:
        return None
    days = sorted(usd)
    i = bisect_right(days, date.fromisoformat(dividend["ex_date"])) - 1
    return dividend["cash_per_share"] * usd[days[max(i, 0)]]


def flags_for(row: dict[str, Any]) -> list[dict[str, str]]:
    """Plain-language warnings, most serious first: ``risk``, then ``watch``, then ``note``."""
    flags: list[dict[str, str]] = []

    def add(level: str, code: str, text: str, short: str) -> None:
        flags.append({"level": level, "code": code, "text": text, "short": short})

    kinds = {notice["kind"] for notice in row.get("notices", [])}
    if row.get("suspended_since"):
        add("risk", "suspended", f"Suspended by IDX since {row['suspended_since']}", "Suspended")
    elif "suspend" in kinds:
        add("watch", "was_suspended", "IDX suspended trading in the last 30 days, since lifted", "Was suspended")
    if "uma" in kinds:
        add("watch", "uma", "IDX flagged unusual market activity in the last 30 days", "Unusual activity")
    if row.get("loss_making"):
        add("risk", "loss", "Losing money on the latest statement", "Losing money")
    if row.get("equity") is not None and row["equity"] < 0:
        add("risk", "negative_equity", "Liabilities exceed assets (negative equity)", "Negative equity")
    if row.get("avg_value_20d") is not None and row["avg_value_20d"] < ILLIQUID_VALUE:
        add("risk", "illiquid", f"Thinly traded: about Rp {_short(row['avg_value_20d'])} a day", "Thinly traded")
    if row.get("der") is not None and row.get("sector") != FINANCIALS and row["der"] > 2.0:
        add("watch", "debt", f"High debt: {row['der']:.1f}x equity", "High debt")
    if row.get("profit_yoy_pct") is not None and row["profit_yoy_pct"] < -20:
        add("watch", "profit_down", f"Profit down {abs(row['profit_yoy_pct']):.0f}% on a year before", f"Profit −{abs(row['profit_yoy_pct']):.0f}%")
    if row.get("sales_yoy_pct") is not None and row["sales_yoy_pct"] < -10:
        add("watch", "sales_down", f"Sales down {abs(row['sales_yoy_pct']):.0f}% on a year before", f"Sales −{abs(row['sales_yoy_pct']):.0f}%")
    if row.get("below_high_pct") is not None and row["below_high_pct"] >= 30:
        span = "52-week" if (row.get("range_sessions") or 0) >= YEAR - 2 else "since-listing"
        add("watch", "drawdown", f"{row['below_high_pct']:.0f}% below its {span} high", f"−{row['below_high_pct']:.0f}% from high")
    if row.get("volatility_pct") is not None and row["volatility_pct"] >= 70:
        add("watch", "volatile", f"Very volatile: {row['volatility_pct']:.0f}% a year", "Very volatile")
    if row.get("statement_age_days") is not None and row["statement_age_days"] > STALE_STATEMENT_DAYS:
        add("watch", "stale", f"Latest statement is {row['statement_age_days'] // 30} months old", "Old statement")
    foreign, traded = row.get("foreign_net_20d"), row.get("avg_value_20d")
    if foreign is not None and traded and foreign < -0.1 * traded * 20:
        add("note", "foreign_selling", f"Foreigners sold a net Rp {_short(-foreign)} in 20 sessions", "Foreign selling")
    if row.get("pe_history_pct") is not None:
        if row["pe_history_pct"] >= 90:
            add("note", "pe_high", "P/E near the top of its range since 2021", "P/E near its high")
        elif row["pe_history_pct"] <= 10:
            add("note", "pe_low", "P/E near the bottom of its range since 2021", "P/E near its low")
    order = {"risk": 0, "watch": 1, "note": 2}
    return sorted(flags, key=lambda flag: order[flag["level"]])


# --- across stocks ---------------------------------------------------------------


def _score(rows: list[dict[str, Any]]) -> None:
    """0-100 ranks within each sector; a theme with no inputs for a stock stays empty."""
    themes = {
        "value": (("earnings_yield_pct", 1), ("book_yield_pct", 1)),
        # ROE and debt to equity mean nothing with negative equity (a loss over a
        # negative number reads as a return): those stocks rank on margin alone.
        "quality": (("roe_pct", 1), ("npm_pct", 1), ("der", -1)),
        "growth": (("sales_yoy_pct", 1), ("profit_yoy_pct", 1)),
        "momentum": (("rel_6m_pts", 1), ("rel_1y_pts", 1)),
        "stability": (("volatility_pct", -1), ("below_high_pct", -1)),
    }
    by_sector: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        row["scores"] = {}
        by_sector.setdefault(row.get("sector") or "Unclassified", []).append(row)
    for sector, members in by_sector.items():
        for theme, inputs in themes.items():
            parts: dict[str, list[float]] = {}
            for key, direction in inputs:
                if key == "der" and sector == FINANCIALS:
                    continue  # leverage is a bank's business, not a warning
                values = {
                    row["ticker"]: row[key] * direction
                    for row in members
                    if row.get(key) is not None
                    and math.isfinite(row[key])
                    and not (key in ("roe_pct", "der") and (row.get("equity") is None or row["equity"] <= 0))
                }
                for ticker, rank in _ranks(values).items():
                    parts.setdefault(ticker, []).append(rank)
            for row in members:
                ranks = parts.get(row["ticker"])
                row["scores"][theme] = round(statistics.fmean(ranks)) if ranks else None


def _peer_medians(rows: list[dict[str, Any]]) -> None:
    by_industry: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("industry"):
            by_industry.setdefault(row["industry"], []).append(row)
    by_sector: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("sector"):
            by_sector.setdefault(row["sector"], []).append(row)
    for row in rows:
        group = by_industry.get(row.get("industry") or "", [])
        label = row.get("industry")
        if len(group) < 6:
            group, label = by_sector.get(row.get("sector") or "", []), row.get("sector")
        row["peer_group"] = label
        row["peer_count"] = len(group)
        for key in ("pe", "pbv", "roe_pct", "npm_pct", "der", "ret_1y_pct", "dividend_yield_pct"):
            values = [peer[key] for peer in group if peer.get(key) is not None]
            row[f"peer_{key}"] = statistics.median(values) if len(values) >= 3 else None


def _peers(universe: Universe, row: dict[str, Any]) -> list[dict[str, Any]]:
    group_key = "industry" if row.get("peer_group") == row.get("industry") else "sector"
    peers = [other for other in universe.rows if other.get(group_key) and other.get(group_key) == row.get(group_key)]
    peers.sort(key=lambda other: -(other.get("market_cap") or 0.0))
    keys = (
        "ticker", "name", "close", "market_cap", "pe", "pbv", "roe_pct", "npm_pct", "der", "sales_yoy_pct",
        "profit_yoy_pct", "dividend_yield_pct", "ret_1y_pct", "scores", "loss_making",
    )
    top = peers[:12]
    if row not in top:
        top.append(row)
    return [{key: peer.get(key) for key in keys} for peer in top]


# --- helpers -------------------------------------------------------------------


def _ranks(values: dict[str, float]) -> dict[str, float]:
    """Percentile ranks 0-100 (ties share the average rank); one value ranks 50."""
    if not values:
        return {}
    ordered = sorted(values.items(), key=lambda item: item[1])
    n = len(ordered)
    if n == 1:
        return {ordered[0][0]: 50.0}
    ranks: dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        rank = (i + j) / 2 / (n - 1) * 100.0
        for k in range(i, j + 1):
            ranks[ordered[k][0]] = rank
        i = j + 1
    return ranks


def _classification(ratio: dict[str, Any] | None) -> dict[str, Any]:
    keys = ("sector", "sub_sector", "industry", "sub_industry")
    return {key: (ratio or {}).get(key) for key in keys}


def _beta(history: PriceHistory, ihsg: dict[date, float]) -> float | None:
    pairs = []
    dates, close = history.dates[-YEAR - 1 :], history.close[-YEAR - 1 :]
    for i in range(1, len(dates)):
        before, after = ihsg.get(dates[i - 1]), ihsg.get(dates[i])
        if before and after and close[i - 1] > 0:
            pairs.append((close[i] / close[i - 1] - 1.0, after / before - 1.0))
    if len(pairs) < 120:
        return None
    stock_mean = statistics.fmean(p[0] for p in pairs)
    index_mean = statistics.fmean(p[1] for p in pairs)
    variance = sum((p[1] - index_mean) ** 2 for p in pairs)
    if not variance:
        return None
    return sum((p[0] - stock_mean) * (p[1] - index_mean) for p in pairs) / variance


def _index_return(ihsg: dict[date, float], start: date, end: date) -> float | None:
    before, after = ihsg.get(start), ihsg.get(end)
    return _pct(after, before) if before and after else None


def _history_percentile(current: float | None, history: list[float | None]) -> float | None:
    values = [value for value in history if value is not None and value > 0]
    if current is None or len(values) < MIN_HISTORY_MONTHS:
        return None
    return sum(1 for value in values if value <= current) / len(values) * 100.0


def _growth(now: float | None, before: float | None) -> float | None:
    if now is None or before is None or before <= 0:
        return None
    return (now / before - 1.0) * 100.0


def statements_by_date(ratios: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Each statement once, by its date, from its latest monthly table.

    IDX sometimes lists one statement under two dates, its period end and a
    later one, with the same figures; the period end (a quarter's last day) is kept.
    """
    by_date: dict[str, dict[str, Any]] = {}
    for ratio in ratios:
        if ratio["fs_date"]:
            by_date[ratio["fs_date"][:10]] = ratio
    kept: dict[tuple, str] = {}
    for fs_date in sorted(by_date):
        row = by_date[fs_date]
        figures = (row["sales"], row["profit_attr_owner"], row["equity"])
        if None in figures:
            kept[(fs_date,)] = fs_date
            continue
        other = kept.get(figures)
        if other is None or (not _quarter_end(other) and _quarter_end(fs_date)):
            kept[figures] = fs_date
    return {fs_date: by_date[fs_date] for fs_date in sorted(kept.values())}


def statement_kind(code: str | None) -> str | None:
    """IDX's statement type, in words: audited, unaudited or limited review."""
    text = (code or "").strip().lower()
    if text in ("a", "audit") or "audited" in text and "un" not in text:
        return "audited"
    if text in ("u", "ù") or "unaudit" in text:
        return "unaudited"
    if text == "lr" or "limited" in text or "terbatas" in text:
        return "limited review"
    return None


def _statement_near(statements: dict[str, dict[str, Any]], target: str, *, days: int = 20) -> dict[str, Any] | None:
    """The statement dated within ``days`` of ``target``, the closest one."""
    wanted = date.fromisoformat(target)
    best = None
    for fs_date, row in statements.items():
        gap = abs((date.fromisoformat(fs_date) - wanted).days)
        if gap <= days and (best is None or gap < best[0]):
            best = (gap, row)
    return best[1] if best else None


def _quarter_end(text: str) -> bool:
    return text[5:] in ("03-31", "06-30", "09-30", "12-31")


def _year_before(text: str) -> str | None:
    try:
        day = date.fromisoformat(text[:10])
    except ValueError:
        return None
    try:
        return day.replace(year=day.year - 1).isoformat()
    except ValueError:  # 29 February
        return day.replace(year=day.year - 1, day=28).isoformat()


def _pct(now: float | None, before: float | None) -> float | None:
    if not now or not before:
        return None
    return (now / before - 1.0) * 100.0


def _date(text: str | None) -> date | None:
    try:
        return date.fromisoformat(str(text)[:10]) if text else None
    except ValueError:
        return None


def _short(value: float) -> str:
    for size, suffix in ((1e12, "tn"), (1e9, "bn"), (1e6, "m")):
        if abs(value) >= size:
            return f"{value / size:.1f} {suffix}"
    return f"{value:,.0f}"
