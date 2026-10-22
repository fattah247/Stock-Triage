-- Market risk layer, phase 5 (docs/market_risk_plan.md, components 6 and 9):
-- the crash-risk estimate, its walk-forward track record, and market episodes.

-- One row per trading day. probability is the walk-forward estimate: each day's
-- model is fitted only on days whose outcome was already known on that day.
CREATE TABLE IF NOT EXISTS crash_risk_days (
    trade_date      TEXT PRIMARY KEY,
    probability     REAL,             -- NULL until there is enough history to fit
    base_rate       REAL,             -- share of known past days followed by a crash
    trustworthy     INTEGER NOT NULL, -- 1 when the track record beats the base rate
    features_json   TEXT NOT NULL,
    analogs_json    TEXT NOT NULL,
    model_version   TEXT NOT NULL,
    evaluated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS forecast_track_record (
    model_version      TEXT NOT NULL,
    horizon_days       INTEGER NOT NULL,
    drawdown_pct       REAL NOT NULL,
    brier              REAL,
    base_rate_brier    REAL,
    hit_rate           REAL,   -- crashes that had a warning beforehand
    false_alarm_rate   REAL,   -- warnings that no crash followed
    alert_probability  REAL,   -- the probability that counts as a warning
    scored_days        INTEGER NOT NULL,
    crash_days         INTEGER NOT NULL,
    evaluated_through  TEXT,
    evaluated_at       TEXT NOT NULL,
    PRIMARY KEY (model_version, horizon_days, drawdown_pct)
);

-- An episode opens when the state reaches stressed and closes after a run of
-- calm sessions. Figures are recomputed from the state; cause, lessons and
-- themes are the user's notes and survive recomputation.
CREATE TABLE IF NOT EXISTS market_episodes (
    episode_id          TEXT PRIMARY KEY,   -- episode-<opened_on>
    opened_on           TEXT NOT NULL UNIQUE,
    closed_on           TEXT,
    worst_regime        TEXT NOT NULL,
    open_close          REAL,
    peak_close          REAL,               -- the 52-week high when it opened
    trough_date         TEXT,
    trough_close        REAL,
    max_drawdown_pct    REAL,               -- trough versus the 52-week high
    recovered_on        TEXT,               -- first close back at peak_close
    open_snapshot_json  TEXT NOT NULL,      -- signals and features on the opening day
    cause               TEXT,
    lessons             TEXT,
    theme_ids_json      TEXT,
    ruleset_version     TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
