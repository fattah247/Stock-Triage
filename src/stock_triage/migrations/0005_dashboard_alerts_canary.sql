CREATE TABLE IF NOT EXISTS alert_events (
    alert_event_id        TEXT PRIMARY KEY,
    channel               TEXT NOT NULL,
    source_name           TEXT NOT NULL,
    document_id           TEXT NOT NULL,
    ticker                TEXT,
    signal_kind           TEXT NOT NULL,
    signal_name           TEXT NOT NULL,
    severity              TEXT NOT NULL,
    alert_key             TEXT NOT NULL,
    payload_json          TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    FOREIGN KEY (document_id) REFERENCES documents(document_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_alert_events_key_channel
    ON alert_events(alert_key, channel);
CREATE INDEX IF NOT EXISTS idx_alert_events_ticker
    ON alert_events(ticker);
CREATE INDEX IF NOT EXISTS idx_alert_events_created_at
    ON alert_events(created_at);

CREATE TABLE IF NOT EXISTS canary_runs (
    canary_run_id         TEXT PRIMARY KEY,
    source_name           TEXT NOT NULL,
    status                TEXT NOT NULL,
    accessible            INTEGER NOT NULL,
    drifted               INTEGER NOT NULL,
    fingerprint           TEXT,
    response_json         TEXT NOT NULL,
    started_at            TEXT NOT NULL,
    finished_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_canary_runs_source_finished_at
    ON canary_runs(source_name, finished_at DESC);
