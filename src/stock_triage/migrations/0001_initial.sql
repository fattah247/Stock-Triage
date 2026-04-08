-- Stock-Triage initial schema (PRD §7.1).
-- All tables favor provenance and reprocessing over one-shot convenience.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Canonical issuer universe.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS companies (
    ticker          TEXT PRIMARY KEY,
    issuer_name     TEXT NOT NULL,
    listing_date    TEXT,
    active_status   TEXT NOT NULL DEFAULT 'unknown',
    sector          TEXT NOT NULL DEFAULT 'unknown',
    subsector       TEXT NOT NULL DEFAULT 'unknown',
    industry        TEXT NOT NULL DEFAULT 'unknown',
    listing_board   TEXT NOT NULL DEFAULT 'unknown',
    source_urls     TEXT,           -- JSON array
    raw_metadata    TEXT,           -- JSON blob
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Raw discovery records (pre-download).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS document_candidates (
    candidate_id        TEXT PRIMARY KEY,
    source_name         TEXT NOT NULL,
    source_record_id    TEXT NOT NULL,
    ticker              TEXT,
    title               TEXT,
    upstream_published_at TEXT,
    attachment_url      TEXT,
    raw_metadata        TEXT NOT NULL,  -- JSON blob
    discovered_at       TEXT NOT NULL,
    queued              INTEGER NOT NULL DEFAULT 0,
    UNIQUE (source_name, source_record_id)
);
CREATE INDEX IF NOT EXISTS idx_candidates_ticker ON document_candidates(ticker);
CREATE INDEX IF NOT EXISTS idx_candidates_discovered_at ON document_candidates(discovered_at);

-- ---------------------------------------------------------------------------
-- Logical version groups (corrections / revisions of the same logical doc).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS document_versions (
    version_group_id    TEXT PRIMARY KEY,
    source_name         TEXT NOT NULL,
    source_record_id    TEXT NOT NULL,
    ticker              TEXT,
    created_at          TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Downloaded attachments + version metadata.
-- Mandatory fields per PRD §7.2.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS documents (
    document_id         TEXT PRIMARY KEY,
    source_name         TEXT NOT NULL,
    source_record_id    TEXT NOT NULL,
    source_file_id      TEXT,
    source_modified_at  TEXT,
    retrieved_at        TEXT NOT NULL,
    original_url        TEXT,
    local_path          TEXT NOT NULL,
    sha256              TEXT NOT NULL,
    mime_type           TEXT,
    document_type       TEXT,            -- financial_statement | annual_report | disclosure | ...
    language_variant    TEXT,            -- id | en | bilingual | unknown
    process_state       TEXT NOT NULL,   -- discovered | downloaded | parsed | extracted | analyzed | failed
    process_error_code  TEXT,
    version_group_id    TEXT,
    etag                TEXT,
    last_modified       TEXT,
    -- parse-stage fields
    page_count          INTEGER,
    parse_strategy      TEXT,            -- native | ocr_partial | ocr_full | parse_failed
    parser_version      TEXT,
    parse_confidence    REAL,
    ocr_used            INTEGER,
    chunk_count         INTEGER,
    -- analysis-stage fields
    pipeline_version    TEXT,
    ruleset_version     TEXT,
    prompt_version      TEXT,
    model_name          TEXT,
    output_schema_version TEXT,
    ticker              TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    FOREIGN KEY (version_group_id) REFERENCES document_versions(version_group_id)
);
CREATE INDEX IF NOT EXISTS idx_documents_sha256 ON documents(sha256);
CREATE INDEX IF NOT EXISTS idx_documents_state  ON documents(process_state);
CREATE INDEX IF NOT EXISTS idx_documents_ticker ON documents(ticker);
CREATE INDEX IF NOT EXISTS idx_documents_version_group ON documents(version_group_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_source_sha
    ON documents(source_name, source_record_id, sha256);

-- ---------------------------------------------------------------------------
-- Stable text chunks with page ranges.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS document_chunks (
    chunk_id        TEXT PRIMARY KEY,
    document_id     TEXT NOT NULL,
    section_label   TEXT,
    page_start      INTEGER NOT NULL,
    page_end        INTEGER NOT NULL,
    char_start      INTEGER NOT NULL,
    char_end        INTEGER NOT NULL,
    text            TEXT NOT NULL,
    ord             INTEGER NOT NULL,
    FOREIGN KEY (document_id) REFERENCES documents(document_id)
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON document_chunks(document_id);

-- ---------------------------------------------------------------------------
-- Fine-grained evidence anchors.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS evidence_spans (
    span_id         TEXT PRIMARY KEY,
    document_id     TEXT NOT NULL,
    chunk_id        TEXT NOT NULL,
    page_start      INTEGER NOT NULL,
    page_end        INTEGER NOT NULL,
    char_start      INTEGER NOT NULL,
    char_end        INTEGER NOT NULL,
    excerpt_text    TEXT NOT NULL,
    display_anchor  TEXT,
    FOREIGN KEY (document_id) REFERENCES documents(document_id),
    FOREIGN KEY (chunk_id)    REFERENCES document_chunks(chunk_id)
);
CREATE INDEX IF NOT EXISTS idx_spans_doc ON evidence_spans(document_id);

-- ---------------------------------------------------------------------------
-- Normalized metric items (PRD §6.6, §6.7, §7.3).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS metric_items (
    metric_id           TEXT PRIMARY KEY,
    document_id         TEXT NOT NULL,
    ticker              TEXT,
    metric_name         TEXT NOT NULL,    -- revenue, gross_profit, ...
    value_normalized    REAL,
    raw_value_text      TEXT,
    currency            TEXT,
    unit_scale          TEXT,             -- units | thousand | million | billion
    fiscal_year         INTEGER,
    fiscal_period       TEXT,             -- Q1|Q2|Q3|Annual|Other
    period_end_date     TEXT,
    audit_status        TEXT,
    consolidation_scope TEXT,
    language_variant    TEXT,
    evidence_span_id    TEXT,
    extracted_at        TEXT NOT NULL,
    FOREIGN KEY (document_id)      REFERENCES documents(document_id),
    FOREIGN KEY (evidence_span_id) REFERENCES evidence_spans(span_id)
);
CREATE INDEX IF NOT EXISTS idx_metric_items_doc    ON metric_items(document_id);
CREATE INDEX IF NOT EXISTS idx_metric_items_ticker ON metric_items(ticker);
CREATE INDEX IF NOT EXISTS idx_metric_items_name   ON metric_items(metric_name);

-- ---------------------------------------------------------------------------
-- Convenience period rollups.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS metric_snapshots (
    snapshot_id         TEXT PRIMARY KEY,
    ticker              TEXT NOT NULL,
    fiscal_year         INTEGER NOT NULL,
    fiscal_period       TEXT NOT NULL,
    consolidation_scope TEXT,
    payload_json        TEXT NOT NULL,
    built_at            TEXT NOT NULL,
    UNIQUE (ticker, fiscal_year, fiscal_period, consolidation_scope)
);

-- ---------------------------------------------------------------------------
-- Detected event records.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    event_id            TEXT PRIMARY KEY,
    document_id         TEXT NOT NULL,
    ticker              TEXT,
    event_type          TEXT NOT NULL,
    matched_pattern     TEXT,
    severity            TEXT,
    rationale           TEXT,
    evidence_span_id    TEXT,
    detected_at         TEXT NOT NULL,
    FOREIGN KEY (document_id)      REFERENCES documents(document_id),
    FOREIGN KEY (evidence_span_id) REFERENCES evidence_spans(span_id)
);
CREATE INDEX IF NOT EXISTS idx_events_doc    ON events(document_id);
CREATE INDEX IF NOT EXISTS idx_events_type   ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_ticker ON events(ticker);

-- ---------------------------------------------------------------------------
-- Deterministic rule outputs.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rule_runs (
    rule_run_id     TEXT PRIMARY KEY,
    document_id     TEXT NOT NULL,
    ticker          TEXT,
    ruleset_version TEXT NOT NULL,
    rule_name       TEXT NOT NULL,
    severity        TEXT NOT NULL,
    threshold       TEXT,
    input_values    TEXT,            -- JSON
    rationale       TEXT,
    triggered       INTEGER NOT NULL,
    evaluated_at    TEXT NOT NULL,
    FOREIGN KEY (document_id) REFERENCES documents(document_id)
);
CREATE INDEX IF NOT EXISTS idx_rule_runs_doc ON rule_runs(document_id);

-- ---------------------------------------------------------------------------
-- Versioned parse/extract execution logs.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS extraction_runs (
    extraction_run_id   TEXT PRIMARY KEY,
    document_id         TEXT NOT NULL,
    parser_version      TEXT,
    extractor_version   TEXT,
    status              TEXT NOT NULL,    -- ok | partial | failed
    error_code          TEXT,
    metrics_extracted   INTEGER,
    events_detected     INTEGER,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    FOREIGN KEY (document_id) REFERENCES documents(document_id)
);

-- ---------------------------------------------------------------------------
-- LLM analysis runs + schema validation state (PRD Appendix C).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS analysis_runs (
    analysis_run_id         TEXT PRIMARY KEY,
    document_id             TEXT NOT NULL,
    model_name              TEXT NOT NULL,
    prompt_version          TEXT NOT NULL,
    output_schema_version   TEXT NOT NULL,
    schema_valid            INTEGER NOT NULL,
    evidence_link_valid     INTEGER NOT NULL,
    output_json             TEXT NOT NULL,
    started_at              TEXT NOT NULL,
    finished_at             TEXT,
    FOREIGN KEY (document_id) REFERENCES documents(document_id)
);
CREATE INDEX IF NOT EXISTS idx_analysis_runs_doc ON analysis_runs(document_id);
