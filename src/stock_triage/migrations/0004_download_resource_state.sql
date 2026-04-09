-- Phase 3: file-level version grouping plus persisted per-resource download state.

UPDATE documents
SET source_file_id = source_record_id
WHERE source_file_id IS NULL;

DROP INDEX IF EXISTS uq_documents_source_sha;

CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_source_file_sha
    ON documents(source_name, source_record_id, source_file_id, sha256);

ALTER TABLE document_versions ADD COLUMN source_file_id TEXT;

INSERT OR IGNORE INTO document_versions (
    version_group_id, source_name, source_record_id, ticker, created_at, source_file_id
)
SELECT
    'version:' || source_name || ':' || source_record_id || ':' || source_file_id,
    source_name,
    source_record_id,
    ticker,
    MIN(created_at),
    source_file_id
FROM documents
GROUP BY source_name, source_record_id, source_file_id, ticker;

UPDATE documents
SET version_group_id = 'version:' || source_name || ':' || source_record_id || ':' || source_file_id
WHERE source_file_id IS NOT NULL;

DELETE FROM document_versions
WHERE source_file_id IS NULL
   OR version_group_id NOT IN (
        SELECT DISTINCT version_group_id
        FROM documents
        WHERE version_group_id IS NOT NULL
    );

CREATE TABLE IF NOT EXISTS download_resources (
    resource_id          TEXT PRIMARY KEY,
    candidate_id         TEXT NOT NULL,
    source_name          TEXT NOT NULL,
    source_record_id     TEXT NOT NULL,
    source_file_id       TEXT NOT NULL,
    attachment_url       TEXT NOT NULL,
    attachment_filename  TEXT,
    status               TEXT NOT NULL,
    document_id          TEXT,
    attempt_count        INTEGER NOT NULL DEFAULT 0,
    failure_count        INTEGER NOT NULL DEFAULT 0,
    last_attempted_at    TEXT,
    next_attempt_at      TEXT,
    last_http_status     INTEGER,
    last_error_code      TEXT,
    last_error_detail    TEXT,
    first_seen_at        TEXT NOT NULL,
    last_seen_at         TEXT NOT NULL,
    FOREIGN KEY (document_id) REFERENCES documents(document_id),
    UNIQUE (source_name, source_record_id, source_file_id)
);

CREATE INDEX IF NOT EXISTS idx_download_resources_status
    ON download_resources(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_download_resources_candidate
    ON download_resources(candidate_id);
