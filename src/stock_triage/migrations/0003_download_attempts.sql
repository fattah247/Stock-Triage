CREATE TABLE IF NOT EXISTS download_attempts (
    attempt_id        TEXT PRIMARY KEY,
    candidate_id      TEXT NOT NULL,
    source_name       TEXT NOT NULL,
    source_record_id  TEXT NOT NULL,
    attachment_url    TEXT NOT NULL,
    status            TEXT NOT NULL,    -- downloaded | duplicate | not_modified | failed
    http_status       INTEGER,
    error_code        TEXT,
    error_detail      TEXT,
    document_id       TEXT,
    started_at        TEXT NOT NULL,
    finished_at       TEXT NOT NULL,
    FOREIGN KEY (document_id) REFERENCES documents(document_id)
);

CREATE INDEX IF NOT EXISTS idx_download_attempts_candidate ON download_attempts(candidate_id);
CREATE INDEX IF NOT EXISTS idx_download_attempts_status ON download_attempts(status);
