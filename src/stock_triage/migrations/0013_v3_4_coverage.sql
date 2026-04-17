CREATE TABLE IF NOT EXISTS filing_coverage (
    coverage_id            TEXT PRIMARY KEY,
    ticker                 TEXT NOT NULL,
    source_name            TEXT NOT NULL,
    obligation_type        TEXT NOT NULL,
    document_type          TEXT NOT NULL,
    fiscal_year            INTEGER NOT NULL,
    period_label           TEXT NOT NULL,
    period_end_date        TEXT NOT NULL,
    window_start           TEXT NOT NULL,
    deadline_at            TEXT NOT NULL,
    expected_state         TEXT NOT NULL,
    actual_state           TEXT NOT NULL,
    candidate_count        INTEGER NOT NULL DEFAULT 0,
    downloaded_count       INTEGER NOT NULL DEFAULT 0,
    analyzed_count         INTEGER NOT NULL DEFAULT 0,
    blocked_reason         TEXT,
    knowledge_source       TEXT NOT NULL,
    payload_json           TEXT NOT NULL,
    coverage_version       TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    UNIQUE (ticker, obligation_type, period_end_date, coverage_version)
);

CREATE INDEX IF NOT EXISTS idx_filing_coverage_ticker
    ON filing_coverage(ticker, deadline_at DESC);

CREATE INDEX IF NOT EXISTS idx_filing_coverage_state
    ON filing_coverage(actual_state, deadline_at ASC);

CREATE INDEX IF NOT EXISTS idx_filing_coverage_range
    ON filing_coverage(deadline_at, expected_state, coverage_version);
