CREATE TABLE IF NOT EXISTS filing_expectations (
    expectation_id        TEXT PRIMARY KEY,
    ticker                TEXT NOT NULL,
    obligation_type       TEXT NOT NULL,
    fiscal_year           INTEGER NOT NULL,
    period_label          TEXT NOT NULL,
    period_end_date       TEXT NOT NULL,
    window_start          TEXT NOT NULL,
    deadline_at           TEXT NOT NULL,
    satisfied_at          TEXT,
    state                 TEXT NOT NULL,
    knowledge_source      TEXT NOT NULL,
    satisfied_document_id TEXT,
    payload_json          TEXT NOT NULL,
    derived_version       TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    FOREIGN KEY (satisfied_document_id) REFERENCES documents(document_id),
    UNIQUE (ticker, obligation_type, period_end_date, derived_version)
);

CREATE INDEX IF NOT EXISTS idx_filing_expectations_ticker
    ON filing_expectations(ticker, deadline_at DESC);

CREATE INDEX IF NOT EXISTS idx_filing_expectations_state
    ON filing_expectations(state, deadline_at ASC);
