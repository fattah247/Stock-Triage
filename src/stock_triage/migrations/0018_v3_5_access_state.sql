CREATE TABLE IF NOT EXISTS artifact_access_state (
    artifact_id                    TEXT PRIMARY KEY,
    candidate_id                   TEXT NOT NULL,
    source_name                    TEXT NOT NULL,
    ticker                         TEXT,
    endpoint_family                TEXT NOT NULL,
    document_type                  TEXT,
    first_blocked_at               TEXT,
    last_blocked_at                TEXT,
    last_success_at                TEXT,
    retry_count                    INTEGER NOT NULL DEFAULT 0,
    block_class                    TEXT,
    retry_eligible_until           TEXT,
    needs_alternative_source_check INTEGER NOT NULL DEFAULT 0,
    last_http_status               INTEGER,
    last_error_code                TEXT,
    created_at                     TEXT NOT NULL,
    updated_at                     TEXT NOT NULL,
    UNIQUE (candidate_id)
);

CREATE INDEX IF NOT EXISTS idx_artifact_access_state_block_class
    ON artifact_access_state(block_class, retry_eligible_until);

CREATE INDEX IF NOT EXISTS idx_artifact_access_state_ticker
    ON artifact_access_state(ticker, updated_at DESC);
