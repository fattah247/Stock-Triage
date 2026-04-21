CREATE TABLE IF NOT EXISTS coverage_request_telemetry (
    telemetry_id        TEXT PRIMARY KEY,
    action              TEXT NOT NULL,
    source_name         TEXT,
    endpoint_family     TEXT,
    transport_mode      TEXT,
    ticker              TEXT,
    document_type       TEXT,
    candidate_id        TEXT,
    document_id         TEXT,
    result_class        TEXT NOT NULL,
    http_status         INTEGER,
    latency_ms          INTEGER,
    delay_applied_ms    INTEGER,
    controller_mode     TEXT NOT NULL,
    source_health       TEXT NOT NULL,
    payload_json        TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_coverage_request_telemetry_created
    ON coverage_request_telemetry(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_coverage_request_telemetry_source_created
    ON coverage_request_telemetry(source_name, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_coverage_request_telemetry_result_created
    ON coverage_request_telemetry(result_class, created_at DESC);
