CREATE TABLE IF NOT EXISTS nuance_findings (
    nuance_id              TEXT PRIMARY KEY,
    ticker                 TEXT NOT NULL,
    document_id            TEXT NOT NULL,
    nuance_class           TEXT NOT NULL,
    severity               TEXT NOT NULL,
    confidence             REAL,
    section_label          TEXT,
    summary                TEXT NOT NULL,
    evidence_span_id       TEXT,
    requires_human_review  INTEGER NOT NULL DEFAULT 0,
    payload_json           TEXT NOT NULL,
    derived_version        TEXT NOT NULL,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    FOREIGN KEY (document_id) REFERENCES documents(document_id),
    FOREIGN KEY (evidence_span_id) REFERENCES evidence_spans(span_id),
    UNIQUE (ticker, document_id, nuance_class, summary, derived_version)
);

CREATE INDEX IF NOT EXISTS idx_nuance_findings_ticker
    ON nuance_findings(ticker, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_nuance_findings_class
    ON nuance_findings(nuance_class, severity);

CREATE TABLE IF NOT EXISTS uncertainty_rows (
    uncertainty_id         TEXT PRIMARY KEY,
    ticker                 TEXT NOT NULL,
    document_id            TEXT NOT NULL,
    uncertainty_class      TEXT NOT NULL,
    severity               TEXT NOT NULL,
    summary                TEXT NOT NULL,
    confidence             REAL,
    section_label          TEXT,
    evidence_span_id       TEXT,
    requires_human_review  INTEGER NOT NULL DEFAULT 0,
    payload_json           TEXT NOT NULL,
    derived_version        TEXT NOT NULL,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    FOREIGN KEY (document_id) REFERENCES documents(document_id),
    FOREIGN KEY (evidence_span_id) REFERENCES evidence_spans(span_id),
    UNIQUE (ticker, document_id, uncertainty_class, summary, derived_version)
);

CREATE INDEX IF NOT EXISTS idx_uncertainty_rows_ticker
    ON uncertainty_rows(ticker, updated_at DESC);

CREATE TABLE IF NOT EXISTS next_read_guidance (
    guidance_id             TEXT PRIMARY KEY,
    ticker                  TEXT NOT NULL,
    document_id             TEXT NOT NULL,
    priority_rank           INTEGER NOT NULL,
    target_document_type    TEXT,
    target_period_label     TEXT,
    target_section_label    TEXT,
    reason_class            TEXT NOT NULL,
    why_this_matters        TEXT NOT NULL,
    evidence_reference_count INTEGER NOT NULL DEFAULT 0,
    blocked_by              TEXT,
    payload_json            TEXT NOT NULL,
    derived_version         TEXT NOT NULL,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    FOREIGN KEY (document_id) REFERENCES documents(document_id),
    UNIQUE (ticker, priority_rank, derived_version)
);

CREATE INDEX IF NOT EXISTS idx_next_read_guidance_ticker
    ON next_read_guidance(ticker, priority_rank ASC);
