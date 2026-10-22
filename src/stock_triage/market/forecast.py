"""Crash-risk estimate, market episodes and historical analogs (components 6 and 9).

Everything here is point-in-time: the estimate for a day is fitted only on days
whose outcome was already known on that day, its track record on a day counts
only outcomes known by then, and a day's analogs are episodes that had already
closed. The model is deliberately small, a regularised logistic regression on
a handful of signals, because IDX history (from 2020) holds only a few crashes.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from ..config import Config
from ..db import WriterQueue, configure_pragmas, connect, run_migrations
from .signals import RULESET_VERSION, MarketDay, evaluate_and_store_market_theses, market_days_from_db

MODEL_VERSION = "crash-risk-v1"
FEATURES = (
    "drawdown",
    "vol_ratio",
    "foreign_sell_streak",
    "usdidr_change_20d",
    "decline_share",
    "sector_breadth",
    "exchange_notices",
    "trend_200d",  # close versus its 200-session average, in percent
    "momentum_20d",  # 20-session change, in percent
)
FEATURE_LABELS = {
    "drawdown": "IHSG below its high (%)",
    "vol_ratio": "volatility ratio",
    "foreign_sell_streak": "foreign net-sell sessions",
    "usdidr_change_20d": "USD/IDR 20-session change (%)",
    "decline_share": "decliners (%)",
    "sector_breadth": "sectors well below their high (%)",
    "exchange_notices": "IDX suspensions and UMA notices versus usual",
    "trend_200d": "IHSG versus its 200-session average (%)",
    "momentum_20d": "IHSG 20-session change (%)",
}
_MIN_TRAIN_DAYS = 252
_MIN_TRAIN_CRASH_DAYS = 10
_REFIT_EVERY = 21
_L2 = 1.0
_MIN_SCORED_DAYS = 250
_MIN_SHARED_FEATURES = 4
_ANALOG_COUNT = 3
_REGIME_RANK = {"calm": 0, "elevated": 1, "stressed": 2, "crisis": 3}


@dataclass(frozen=True)
class DayFeatures:
    trade_date: date
    close: float
    values: dict[str, float | None]


@dataclass(frozen=True)
class TrackRecord:
    brier: float | None
    base_rate_brier: float | None
    hit_rate: float | None
    false_alarm_rate: float | None
    scored_days: int
    crash_days: int
    evaluated_through: date | None

    @property
    def skill(self) -> float | None:
        if self.brier is None or not self.base_rate_brier:
            return None
        return 1.0 - self.brier / self.base_rate_brier


@dataclass
class Episode:
    opened_on: date
    open_index: int
    open_close: float
    peak_close: float
    worst_regime: str
    open_snapshot: dict[str, object]
    closed_on: date | None = None
    close_index: int | None = None
    trough_date: date | None = None
    trough_index: int | None = None
    trough_close: float | None = None
    recovered_on: date | None = None
    recovered_index: int | None = None

    @property
    def episode_id(self) -> str:
        return f"episode-{self.opened_on.isoformat()}"

    @property
    def max_drawdown_pct(self) -> float | None:
        if self.trough_close is None or not self.peak_close:
            return None
        return (self.peak_close - self.trough_close) / self.peak_close * 100.0

    @property
    def further_fall_pct(self) -> float | None:
        if self.trough_close is None or not self.open_close:
            return None
        return (self.open_close - self.trough_close) / self.open_close * 100.0


@dataclass(frozen=True)
class CrashRiskDay:
    trade_date: date
    probability: float | None
    base_rate: float | None
    trustworthy: bool
    features: dict[str, float | None]
    analogs: list[dict[str, object]]
    contributions: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class CrashRiskResult:
    days: list[CrashRiskDay]
    episodes: list[Episode]
    record: TrackRecord
    labels: list[int | None]
    theses: list = field(default_factory=list)

    @property
    def latest(self) -> CrashRiskDay | None:
        return self.days[-1] if self.days else None


# --- features and labels -------------------------------------------------------


def build_features(days: list[MarketDay], closes: list[float]) -> list[DayFeatures]:
    """Each day's feature values, from that day's signal readings and closes up to it."""
    out: list[DayFeatures] = []
    running = 0.0
    for i, day in enumerate(days):
        running += closes[i]
        if i >= 200:
            running -= closes[i - 200]
        values: dict[str, float | None] = {name: None for name in FEATURES}
        for reading in day.readings:
            if reading.signal_name in values and reading.level != "no_data":
                values[reading.signal_name] = reading.value
        if i >= 199 and "trend_200d" in values:
            values["trend_200d"] = (closes[i] / (running / 200.0) - 1.0) * 100.0
        if i >= 20 and closes[i - 20] > 0 and "momentum_20d" in values:
            values["momentum_20d"] = (closes[i] / closes[i - 20] - 1.0) * 100.0
        out.append(DayFeatures(trade_date=day.trade_date, close=closes[i], values=values))
    return out


def crash_labels(closes: list[float], *, horizon: int, drawdown_pct: float) -> list[int | None]:
    """1 when the close falls ``drawdown_pct`` below the day's close within ``horizon`` sessions.

    None while the horizon is not yet over, so the outcome is unknown.
    """
    n = len(closes)
    labels: list[int | None] = []
    for i in range(n):
        if i + horizon >= n:
            labels.append(None)
            continue
        floor = closes[i] * (1.0 - drawdown_pct / 100.0)
        labels.append(1 if min(closes[i + 1 : i + horizon + 1]) <= floor else 0)
    return labels


# --- the model -----------------------------------------------------------------


@dataclass(frozen=True)
class _Model:
    weights: list[float]  # intercept first
    means: list[float]
    scales: list[float]

    def row(self, values: dict[str, float | None]) -> list[float]:
        return [
            0.0 if values[name] is None else (float(values[name]) - self.means[k]) / self.scales[k]  # type: ignore[arg-type]
            for k, name in enumerate(FEATURES)
        ]

    def predict(self, values: dict[str, float | None]) -> float:
        row = self.row(values)
        return _sigmoid(self.weights[0] + sum(w * x for w, x in zip(self.weights[1:], row)))

    def contributions(self, values: dict[str, float | None]) -> dict[str, float]:
        """Each feature's push on the log-odds, for explaining the number."""
        row = self.row(values)
        return {name: round(self.weights[k + 1] * row[k], 3) for k, name in enumerate(FEATURES) if values[name] is not None}


def fit_model(
    rows: list[dict[str, float | None]], labels: list[int], *, start: list[float] | None = None
) -> _Model:
    """Ridge-penalised logistic regression by Newton's method; missing values sit at the mean.

    ``start`` warm-starts from the previous refit, which is close, so few steps are needed.
    """
    means, scales = [], []
    for name in FEATURES:
        present = [float(row[name]) for row in rows if row[name] is not None]  # type: ignore[arg-type]
        mean = sum(present) / len(present) if present else 0.0
        variance = sum((value - mean) ** 2 for value in present) / len(present) if present else 0.0
        means.append(mean)
        scales.append(math.sqrt(variance) or 1.0)
    model = _Model(weights=[0.0] * (len(FEATURES) + 1), means=means, scales=scales)
    matrix = [[1.0, *model.row(row)] for row in rows]
    weights = list(start) if start is not None else [0.0] * (len(FEATURES) + 1)
    for _ in range(25):
        size = len(weights)
        gradient = [0.0] * size
        hessian = [[0.0] * size for _ in range(size)]
        for x, y in zip(matrix, labels):
            p = _sigmoid(sum(w * v for w, v in zip(weights, x)))
            error, weight = p - y, p * (1.0 - p)
            for a in range(size):
                gradient[a] += error * x[a]
                for b in range(a, size):
                    hessian[a][b] += weight * x[a] * x[b]
        for a in range(size):
            for b in range(a):
                hessian[a][b] = hessian[b][a]
            if a:  # the intercept is not penalised
                gradient[a] += _L2 * weights[a]
                hessian[a][a] += _L2
        step = _solve(hessian, gradient)
        weights = [w - s for w, s in zip(weights, step)]
        if max(abs(s) for s in step) < 1e-4:
            break
    return _Model(weights=weights, means=means, scales=scales)


def walk_forward(
    features: list[DayFeatures], labels: list[int | None], *, horizon: int
) -> tuple[list[float | None], list[float | None], list[_Model | None]]:
    """Each day's probability and base rate, from days whose outcome was known that day."""
    n = len(features)
    probabilities: list[float | None] = [None] * n
    base_rates: list[float | None] = [None] * n
    models: list[_Model | None] = [None] * n
    model: _Model | None = None
    base: float | None = None
    next_refit = 0
    for t in range(n):
        if t >= next_refit:
            known = [i for i in range(0, max(t - horizon + 1, 0)) if labels[i] is not None and features[i].values["drawdown"] is not None]
            crashes = sum(labels[i] for i in known)  # type: ignore[misc]
            if len(known) >= _MIN_TRAIN_DAYS and crashes >= _MIN_TRAIN_CRASH_DAYS and crashes < len(known):
                model = fit_model(
                    [features[i].values for i in known],
                    [labels[i] for i in known],  # type: ignore[misc]
                    start=model.weights if model is not None else None,
                )
                base = crashes / len(known)
                next_refit = t + _REFIT_EVERY
            else:
                next_refit = t + 1
        if model is not None and features[t].values["drawdown"] is not None:
            probabilities[t] = model.predict(features[t].values)
            base_rates[t] = base
            models[t] = model
    return probabilities, base_rates, models


def track_record(
    probabilities: list[float | None],
    base_rates: list[float | None],
    labels: list[int | None],
    trade_dates: list[date],
    *,
    alert_probability: float,
    through: int | None = None,
) -> TrackRecord:
    """Brier scores and warning hit and false-alarm rates over days with a known outcome."""
    last = len(labels) - 1 if through is None else through
    brier = base_brier = 0.0
    scored = crashes = hits = warnings = false_alarms = 0
    latest: date | None = None
    for i in range(min(last + 1, len(labels))):
        p, b, y = probabilities[i], base_rates[i], labels[i]
        if p is None or b is None or y is None:
            continue
        scored += 1
        crashes += y
        brier += (p - y) ** 2
        base_brier += (b - y) ** 2
        warned = p >= alert_probability
        warnings += warned
        hits += warned and y == 1
        false_alarms += warned and y == 0
        latest = trade_dates[i]
    return TrackRecord(
        brier=brier / scored if scored else None,
        base_rate_brier=base_brier / scored if scored else None,
        hit_rate=hits / crashes if crashes else None,
        false_alarm_rate=false_alarms / warnings if warnings else None,
        scored_days=scored,
        crash_days=crashes,
        evaluated_through=latest,
    )


def is_trustworthy(record: TrackRecord, *, min_skill: float) -> bool:
    skill = record.skill
    return record.scored_days >= _MIN_SCORED_DAYS and skill is not None and skill >= min_skill


def running_trust(
    probabilities: list[float | None],
    base_rates: list[float | None],
    labels: list[int | None],
    *,
    horizon: int,
    min_skill: float,
) -> list[bool]:
    """Whether the estimate had beaten its base rate on outcomes known by each day."""
    n = len(labels)
    known_on: list[list[int]] = [[] for _ in range(n)]
    for i in range(n):
        if probabilities[i] is not None and base_rates[i] is not None and labels[i] is not None and i + horizon < n:
            known_on[i + horizon].append(i)
    trust, brier, base_brier, scored = [], 0.0, 0.0, 0
    for t in range(n):
        for i in known_on[t]:
            brier += (probabilities[i] - labels[i]) ** 2  # type: ignore[operator]
            base_brier += (base_rates[i] - labels[i]) ** 2  # type: ignore[operator]
            scored += 1
        skill = 1.0 - brier / base_brier if base_brier else None
        trust.append(scored >= _MIN_SCORED_DAYS and skill is not None and skill >= min_skill)
    return trust


# --- episodes and analogs ----------------------------------------------------


def detect_episodes(
    days: list[MarketDay], features: list[DayFeatures], *, close_after_calm: int
) -> list[Episode]:
    """Episodes open when the state reaches stressed and close after a run of calm sessions."""
    closes = [item.close for item in features]
    episodes: list[Episode] = []
    current: Episode | None = None
    calm_run = 0
    for i, day in enumerate(days):
        rank = _REGIME_RANK[day.regime]
        if current is None:
            if rank >= _REGIME_RANK["stressed"]:
                drawdown = next((r for r in day.readings if r.signal_name == "drawdown"), None)
                peak = float(drawdown.inputs.get("peak", closes[i])) if drawdown and drawdown.inputs else closes[i]
                current = Episode(
                    opened_on=day.trade_date,
                    open_index=i,
                    open_close=closes[i],
                    peak_close=peak,
                    worst_regime=day.regime,
                    open_snapshot={
                        "regime": day.regime,
                        "features": features[i].values,
                        "signals": {r.signal_name: [r.value, r.level] for r in day.readings},
                    },
                )
                calm_run = 0
            continue
        if rank > _REGIME_RANK[current.worst_regime]:
            current.worst_regime = day.regime
        calm_run = calm_run + 1 if day.regime == "calm" else 0
        if calm_run >= close_after_calm:
            current.closed_on, current.close_index = day.trade_date, i
            episodes.append(current)
            current = None
    if current is not None:
        episodes.append(current)
    for episode in episodes:
        end = episode.close_index if episode.close_index is not None else len(closes) - 1
        trough = min(range(episode.open_index, end + 1), key=lambda k: closes[k])
        episode.trough_index, episode.trough_date, episode.trough_close = trough, days[trough].trade_date, closes[trough]
        recovered = next((k for k in range(trough + 1, len(closes)) if closes[k] >= episode.peak_close), None)
        if recovered is not None:
            episode.recovered_index, episode.recovered_on = recovered, days[recovered].trade_date
    return episodes


def find_analogs(
    today: dict[str, float | None],
    episodes: list[Episode],
    *,
    as_of_index: int,
    scales: dict[str, float],
    count: int = _ANALOG_COUNT,
) -> list[dict[str, object]]:
    """The closed episodes whose opening day looked most like today, with what followed."""
    ranked = []
    for episode in episodes:
        if episode.close_index is None or episode.close_index >= as_of_index:
            continue  # its outcome was not complete yet
        then = episode.open_snapshot["features"]
        shared = [name for name in FEATURES if today.get(name) is not None and then.get(name) is not None and scales.get(name)]  # type: ignore[union-attr]
        if len(shared) < _MIN_SHARED_FEATURES:
            continue
        distance = math.sqrt(
            sum(((float(today[name]) - float(then[name])) / scales[name]) ** 2 for name in shared) / len(shared)  # type: ignore[index,arg-type]
        )
        recovered = episode.recovered_index is not None and episode.recovered_index <= as_of_index
        ranked.append(
            (
                distance,
                {
                    "episode_id": episode.episode_id,
                    "opened_on": episode.opened_on.isoformat(),
                    "distance": round(distance, 3),
                    "worst_regime": episode.worst_regime,
                    "further_fall_pct": _round(episode.further_fall_pct),
                    "max_drawdown_pct": _round(episode.max_drawdown_pct),
                    "sessions_to_trough": (episode.trough_index or episode.open_index) - episode.open_index,
                    "sessions_to_recover": (episode.recovered_index - episode.trough_index) if recovered else None,  # type: ignore[operator]
                },
            )
        )
    ranked.sort(key=lambda pair: pair[0])
    return [item for _, item in ranked[:count]]


# --- the run -----------------------------------------------------------------


def compute_crash_risk(
    days: list[MarketDay],
    closes: list[float],
    *,
    horizon: int,
    drawdown_pct: float,
    alert_probability: float,
    min_skill: float,
    close_after_calm: int,
) -> CrashRiskResult:
    features = build_features(days, closes)
    labels = crash_labels(closes, horizon=horizon, drawdown_pct=drawdown_pct)
    probabilities, base_rates, models = walk_forward(features, labels, horizon=horizon)
    trust = running_trust(probabilities, base_rates, labels, horizon=horizon, min_skill=min_skill)
    episodes = detect_episodes(days, features, close_after_calm=close_after_calm)
    trade_dates = [item.trade_date for item in features]

    out: list[CrashRiskDay] = []
    sums = {name: [0, 0.0, 0.0] for name in FEATURES}  # count, sum, sum of squares, up to each day
    for t, item in enumerate(features):
        for name, value in item.values.items():
            if value is not None:
                stats = sums[name]
                stats[0] += 1
                stats[1] += value
                stats[2] += value * value
        scales = {}
        for name, (count, total, squares) in sums.items():
            if count > 1:
                variance = squares / count - (total / count) ** 2
                scales[name] = math.sqrt(variance) if variance > 0 else 0.0
        model = models[t]
        out.append(
            CrashRiskDay(
                trade_date=item.trade_date,
                probability=probabilities[t],
                base_rate=base_rates[t],
                trustworthy=trust[t],
                features=item.values,
                analogs=find_analogs(item.values, episodes, as_of_index=t, scales=scales),
                contributions=model.contributions(item.values) if model is not None else {},
            )
        )
    record = track_record(probabilities, base_rates, labels, trade_dates, alert_probability=alert_probability)
    return CrashRiskResult(days=out, episodes=episodes, record=record, labels=labels)


def run_crash_risk(cfg: Config) -> CrashRiskResult:
    """Recompute the estimate, episodes and analogs over all synced history, then store them."""
    cfg.ensure_dirs()
    market = cfg.market
    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
        reader = connect(cfg.paths.db_path, read_only=True)
        try:
            configure_pragmas(reader, cfg)
            series, days = market_days_from_db(reader, cfg)
        finally:
            reader.close()
        composite = series.get("index:COMPOSITE", {})
        result = compute_crash_risk(
            days,
            [composite[day.trade_date] for day in days],
            horizon=market.crash_risk_horizon_sessions,
            drawdown_pct=market.crash_risk_drawdown_pct,
            alert_probability=market.crash_risk_alert_probability,
            min_skill=market.crash_risk_min_skill,
            close_after_calm=market.episode_close_after_calm_sessions,
        )
        wq.submit(lambda conn: _persist(conn, result, cfg=cfg))
        theses = wq.submit(lambda conn: evaluate_and_store_market_theses(conn, cfg))
    return CrashRiskResult(
        days=result.days, episodes=result.episodes, record=result.record, labels=result.labels, theses=theses
    )


def _persist(conn: sqlite3.Connection, result: CrashRiskResult, *, cfg: Config) -> None:
    now = datetime.now(timezone.utc).isoformat()
    market = cfg.market
    conn.execute("DELETE FROM crash_risk_days")
    conn.executemany(
        """
        INSERT INTO crash_risk_days (
            trade_date, probability, base_rate, trustworthy, features_json, analogs_json, model_version, evaluated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                day.trade_date.isoformat(),
                day.probability,
                day.base_rate,
                int(day.trustworthy),
                json.dumps({"values": day.features, "contributions": day.contributions}, sort_keys=True),
                json.dumps(day.analogs),
                MODEL_VERSION,
                now,
            )
            for day in result.days
        ],
    )
    record = result.record
    conn.execute(
        """
        INSERT OR REPLACE INTO forecast_track_record (
            model_version, horizon_days, drawdown_pct, brier, base_rate_brier, hit_rate, false_alarm_rate,
            alert_probability, scored_days, crash_days, evaluated_through, evaluated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            MODEL_VERSION,
            market.crash_risk_horizon_sessions,
            market.crash_risk_drawdown_pct,
            record.brier,
            record.base_rate_brier,
            record.hit_rate,
            record.false_alarm_rate,
            market.crash_risk_alert_probability,
            record.scored_days,
            record.crash_days,
            record.evaluated_through.isoformat() if record.evaluated_through else None,
            now,
        ),
    )
    detected = {episode.episode_id for episode in result.episodes}
    # Episodes the rules no longer find go, unless the user has written notes on them.
    for row in conn.execute("SELECT episode_id, cause, lessons FROM market_episodes").fetchall():
        if row["episode_id"] not in detected and not (row["cause"] or row["lessons"]):
            conn.execute("DELETE FROM market_episodes WHERE episode_id = ?", (row["episode_id"],))
    for episode in result.episodes:
        conn.execute(
            """
            INSERT INTO market_episodes (
                episode_id, opened_on, closed_on, worst_regime, open_close, peak_close, trough_date, trough_close,
                max_drawdown_pct, recovered_on, open_snapshot_json, ruleset_version, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(episode_id) DO UPDATE SET
                closed_on = excluded.closed_on, worst_regime = excluded.worst_regime,
                open_close = excluded.open_close, peak_close = excluded.peak_close,
                trough_date = excluded.trough_date, trough_close = excluded.trough_close,
                max_drawdown_pct = excluded.max_drawdown_pct, recovered_on = excluded.recovered_on,
                open_snapshot_json = excluded.open_snapshot_json, ruleset_version = excluded.ruleset_version,
                updated_at = excluded.updated_at
            """,
            (
                episode.episode_id,
                episode.opened_on.isoformat(),
                episode.closed_on.isoformat() if episode.closed_on else None,
                episode.worst_regime,
                episode.open_close,
                episode.peak_close,
                episode.trough_date.isoformat() if episode.trough_date else None,
                episode.trough_close,
                _round(episode.max_drawdown_pct),
                episode.recovered_on.isoformat() if episode.recovered_on else None,
                json.dumps(episode.open_snapshot, sort_keys=True),
                RULESET_VERSION,
                now,
            ),
        )


def record_episode_note(
    conn: sqlite3.Connection,
    *,
    episode_id: str,
    cause: str | None,
    lessons: str | None,
    theme_ids: list[str] | None,
) -> None:
    """Store the user's account of an episode; recomputation keeps it."""
    row = conn.execute("SELECT cause, lessons, theme_ids_json FROM market_episodes WHERE episode_id = ?", (episode_id,)).fetchone()
    if row is None:
        raise ValueError(f"episode not found: {episode_id}")
    conn.execute(
        "UPDATE market_episodes SET cause = ?, lessons = ?, theme_ids_json = ? WHERE episode_id = ?",
        (
            cause if cause is not None else row["cause"],
            lessons if lessons is not None else row["lessons"],
            json.dumps(sorted(set(theme_ids))) if theme_ids is not None else row["theme_ids_json"],
            episode_id,
        ),
    )


def load_crash_risk(conn: sqlite3.Connection, *, history: int = 260) -> dict[str, object] | None:
    """The latest estimate with its track record and analogs, for the dashboard and commands."""
    rows = conn.execute(
        "SELECT trade_date, probability, base_rate, trustworthy, features_json, analogs_json, model_version "
        "FROM crash_risk_days ORDER BY trade_date DESC LIMIT ?",
        (history,),
    ).fetchall()
    if not rows:
        return None
    latest = rows[0]
    record = conn.execute(
        "SELECT * FROM forecast_track_record WHERE model_version = ? ORDER BY evaluated_at DESC LIMIT 1",
        (latest["model_version"],),
    ).fetchone()
    features = json.loads(latest["features_json"])
    return {
        "trade_date": latest["trade_date"],
        "probability": latest["probability"],
        "base_rate": latest["base_rate"],
        "trustworthy": bool(latest["trustworthy"]),
        "features": features.get("values", {}),
        "contributions": features.get("contributions", {}),
        "analogs": json.loads(latest["analogs_json"]),
        "model_version": latest["model_version"],
        "track_record": dict(record) if record is not None else None,
        "history": [
            {"trade_date": row["trade_date"], "probability": row["probability"], "base_rate": row["base_rate"]}
            for row in reversed(rows)
        ],
    }


def load_episodes(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT episode_id, opened_on, closed_on, worst_regime, open_close, peak_close, trough_date, trough_close,
               max_drawdown_pct, recovered_on, cause, lessons, theme_ids_json
        FROM market_episodes ORDER BY opened_on DESC
        """
    ).fetchall()
    return [
        {**dict(row), "theme_ids": json.loads(row["theme_ids_json"]) if row["theme_ids_json"] else []}
        for row in rows
    ]


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exp = math.exp(value)
    return exp / (1.0 + exp)


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting; the ridge keeps the system well posed."""
    size = len(vector)
    rows = [list(matrix[i]) + [vector[i]] for i in range(size)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda r: abs(rows[r][col]))
        rows[col], rows[pivot] = rows[pivot], rows[col]
        if abs(rows[col][col]) < 1e-12:
            continue
        for r in range(col + 1, size):
            factor = rows[r][col] / rows[col][col]
            if factor:
                for c in range(col, size + 1):
                    rows[r][c] -= factor * rows[col][c]
    solution = [0.0] * size
    for r in range(size - 1, -1, -1):
        if abs(rows[r][r]) < 1e-12:
            continue
        solution[r] = (rows[r][size] - sum(rows[r][c] * solution[c] for c in range(r + 1, size))) / rows[r][r]
    return solution


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)

