CREATE TABLE IF NOT EXISTS contradiction_rows (
    contradiction_id       TEXT PRIMARY KEY,
    ticker                 TEXT NOT NULL,
    document_id            TEXT NOT NULL,
    contradiction_class    TEXT NOT NULL,
    severity               TEXT NOT NULL,
    summary                TEXT NOT NULL,
    source_claim_id        TEXT,
    section_label          TEXT,
    evidence_span_id       TEXT,
    payload_json           TEXT NOT NULL,
    derived_version        TEXT NOT NULL,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    FOREIGN KEY (document_id) REFERENCES documents(document_id),
    FOREIGN KEY (source_claim_id) REFERENCES management_claims(claim_id),
    FOREIGN KEY (evidence_span_id) REFERENCES evidence_spans(span_id),
    UNIQUE (ticker, document_id, contradiction_class, summary, derived_version)
);

CREATE INDEX IF NOT EXISTS idx_contradiction_rows_ticker
    ON contradiction_rows(ticker, updated_at DESC);
