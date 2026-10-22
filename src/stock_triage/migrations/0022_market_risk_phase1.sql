-- Market risk layer, phase 1 (docs/market_risk_plan.md): the risk calendar,
-- exposure ranking, fundamental stress breadth, and alerts that are not tied
-- to one document.

CREATE TABLE IF NOT EXISTS market_catalysts (
    catalyst_id     TEXT PRIMARY KEY,
    catalyst_date   TEXT NOT NULL,
    kind            TEXT NOT NULL,
    title           TEXT NOT NULL,
    severity        TEXT NOT NULL,
    region          TEXT,
    source_url      TEXT,
    notes           TEXT,
    synced_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_market_catalysts_date
    ON market_catalysts(catalyst_date);

CREATE TABLE IF NOT EXISTS exposure_scores (
    ticker          TEXT NOT NULL,
    as_of_date      TEXT NOT NULL,
    score           REAL NOT NULL,
    components_json TEXT NOT NULL,
    derived_version TEXT NOT NULL,
    computed_at     TEXT NOT NULL,
    PRIMARY KEY (ticker, as_of_date)
);
CREATE INDEX IF NOT EXISTS idx_exposure_scores_date
    ON exposure_scores(as_of_date, score DESC);

CREATE TABLE IF NOT EXISTS fundamental_breadth (
    as_of_date            TEXT NOT NULL,
    scope                 TEXT NOT NULL,    -- market | sector:<name>
    fiscal_year           INTEGER NOT NULL,
    filer_count           INTEGER NOT NULL,
    negative_ocf_share    REAL,
    negative_equity_share REAL,
    net_loss_share        REAL,
    change_pts            REAL,             -- largest rise in a share versus the prior fiscal year
    level                 TEXT NOT NULL,    -- none | elevated | stressed | crisis | insufficient_data
    details_json          TEXT NOT NULL,
    derived_version       TEXT NOT NULL,
    computed_at           TEXT NOT NULL,
    PRIMARY KEY (as_of_date, scope, fiscal_year)
);

-- Market alerts have no source document, so document_id becomes optional.
-- SQLite cannot drop NOT NULL in place; rebuild the table and keep every row.
CREATE TABLE alert_events_rebuilt (
    alert_event_id        TEXT PRIMARY KEY,
    channel               TEXT NOT NULL,
    source_name           TEXT NOT NULL,
    document_id           TEXT,
    ticker                TEXT,
    signal_kind           TEXT NOT NULL,
    signal_name           TEXT NOT NULL,
    severity              TEXT NOT NULL,
    alert_key             TEXT NOT NULL,
    payload_json          TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    FOREIGN KEY (document_id) REFERENCES documents(document_id)
);
INSERT INTO alert_events_rebuilt (
    alert_event_id, channel, source_name, document_id, ticker,
    signal_kind, signal_name, severity, alert_key, payload_json, created_at
)
SELECT
    alert_event_id, channel, source_name, document_id, ticker,
    signal_kind, signal_name, severity, alert_key, payload_json, created_at
FROM alert_events;
DROP TABLE alert_events;
ALTER TABLE alert_events_rebuilt RENAME TO alert_events;
CREATE UNIQUE INDEX IF NOT EXISTS uq_alert_events_key_channel
    ON alert_events(alert_key, channel);
CREATE INDEX IF NOT EXISTS idx_alert_events_ticker
    ON alert_events(ticker);
CREATE INDEX IF NOT EXISTS idx_alert_events_created_at
    ON alert_events(created_at);
