CREATE TABLE IF NOT EXISTS golden_set_runs (
    golden_run_id         TEXT PRIMARY KEY,
    scope                 TEXT NOT NULL,
    target_ticker         TEXT,
    case_count            INTEGER NOT NULL,
    passed_count          INTEGER NOT NULL,
    failed_count          INTEGER NOT NULL,
    warning_count         INTEGER NOT NULL,
    payload_json          TEXT NOT NULL,
    derived_version       TEXT NOT NULL,
    created_at            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_golden_set_runs_scope_created
    ON golden_set_runs(scope, created_at DESC);
