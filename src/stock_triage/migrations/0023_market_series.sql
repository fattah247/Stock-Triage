-- Market risk layer, phase 2 (docs/market_risk_plan.md, docs/phase2_market_recon.md):
-- daily market series, a per-day sync ledger, and the signals and market state
-- derived from them.

CREATE TABLE IF NOT EXISTS market_series_daily (
    series_id     TEXT NOT NULL,   -- e.g. index:COMPOSITE, breadth:decliners, fx:USDIDR_JISDOR
    trade_date    TEXT NOT NULL,
    value         REAL NOT NULL,
    source_name   TEXT NOT NULL,
    retrieved_at  TEXT NOT NULL,
    PRIMARY KEY (series_id, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_market_series_daily_date
    ON market_series_daily(trade_date);

-- One row per source and day fetched, so re-runs and backfills skip finished
-- days, including holidays that returned nothing.
CREATE TABLE IF NOT EXISTS market_sync_days (
    source_name   TEXT NOT NULL,
    trade_date    TEXT NOT NULL,
    status        TEXT NOT NULL,   -- ok | no_data
    row_count     INTEGER NOT NULL,
    fetched_at    TEXT NOT NULL,
    PRIMARY KEY (source_name, trade_date)
);

CREATE TABLE IF NOT EXISTS market_signal_runs (
    trade_date       TEXT NOT NULL,
    signal_name      TEXT NOT NULL,
    value            REAL,
    level            TEXT NOT NULL,   -- none | elevated | stressed | crisis | no_data
    inputs_json      TEXT NOT NULL,
    ruleset_version  TEXT NOT NULL,
    evaluated_at     TEXT NOT NULL,
    PRIMARY KEY (trade_date, signal_name)
);

CREATE TABLE IF NOT EXISTS market_regime_days (
    trade_date       TEXT PRIMARY KEY,
    regime           TEXT NOT NULL,   -- calm | elevated | stressed | crisis
    raw_regime       TEXT NOT NULL,   -- before the step-down rule
    drivers_json     TEXT NOT NULL,
    ruleset_version  TEXT NOT NULL,
    evaluated_at     TEXT NOT NULL
);
