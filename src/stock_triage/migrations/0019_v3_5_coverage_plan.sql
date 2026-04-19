CREATE TABLE IF NOT EXISTS coverage_plan_audit_log (
    audit_id      TEXT PRIMARY KEY,
    event_type    TEXT NOT NULL,
    plan_mode     TEXT NOT NULL,
    scope_label   TEXT NOT NULL,
    action        TEXT,
    payload_json  TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_coverage_plan_audit_log_created
    ON coverage_plan_audit_log(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_coverage_plan_audit_log_event
    ON coverage_plan_audit_log(event_type, created_at DESC);
