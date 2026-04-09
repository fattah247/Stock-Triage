CREATE TABLE IF NOT EXISTS document_deltas (
    delta_id             TEXT PRIMARY KEY,
    ticker               TEXT NOT NULL,
    from_document_id     TEXT NOT NULL,
    to_document_id       TEXT NOT NULL,
    delta_kind           TEXT NOT NULL,
    delta_key            TEXT NOT NULL,
    metric_name          TEXT,
    fiscal_year          INTEGER,
    fiscal_period        TEXT,
    period_end_date      TEXT,
    previous_value       REAL,
    current_value        REAL,
    absolute_change      REAL,
    percent_change       REAL,
    payload_json         TEXT NOT NULL,
    derived_version      TEXT NOT NULL,
    derived_at           TEXT NOT NULL,
    FOREIGN KEY (from_document_id) REFERENCES documents(document_id),
    FOREIGN KEY (to_document_id) REFERENCES documents(document_id),
    UNIQUE (ticker, from_document_id, to_document_id, delta_kind, delta_key, derived_version)
);
CREATE INDEX IF NOT EXISTS idx_document_deltas_ticker
    ON document_deltas(ticker, derived_at DESC);

CREATE TABLE IF NOT EXISTS company_snapshots (
    snapshot_id           TEXT PRIMARY KEY,
    ticker                TEXT NOT NULL,
    snapshot_kind         TEXT NOT NULL,
    as_of_document_id     TEXT NOT NULL,
    as_of_date            TEXT,
    latest_analysis_run_id TEXT,
    payload_json          TEXT NOT NULL,
    derived_version       TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    FOREIGN KEY (as_of_document_id) REFERENCES documents(document_id),
    FOREIGN KEY (latest_analysis_run_id) REFERENCES analysis_runs(analysis_run_id),
    UNIQUE (ticker, snapshot_kind, derived_version)
);
CREATE INDEX IF NOT EXISTS idx_company_snapshots_ticker
    ON company_snapshots(ticker, updated_at DESC);

CREATE TABLE IF NOT EXISTS company_timeseries (
    series_point_id       TEXT PRIMARY KEY,
    ticker                TEXT NOT NULL,
    series_name           TEXT NOT NULL,
    series_key            TEXT NOT NULL,
    fiscal_year           INTEGER,
    fiscal_period         TEXT,
    period_end_date       TEXT,
    value_normalized      REAL,
    document_id           TEXT NOT NULL,
    evidence_span_id      TEXT,
    derived_version       TEXT NOT NULL,
    derived_at            TEXT NOT NULL,
    FOREIGN KEY (document_id) REFERENCES documents(document_id),
    FOREIGN KEY (evidence_span_id) REFERENCES evidence_spans(span_id),
    UNIQUE (ticker, series_name, series_key, document_id, derived_version)
);
CREATE INDEX IF NOT EXISTS idx_company_timeseries_ticker
    ON company_timeseries(ticker, period_end_date DESC);

CREATE TABLE IF NOT EXISTS management_claims (
    claim_id              TEXT PRIMARY KEY,
    ticker                TEXT NOT NULL,
    source_document_id    TEXT,
    claim_text            TEXT NOT NULL,
    claim_topic           TEXT,
    claim_date            TEXT,
    evidence_span_id      TEXT,
    status                TEXT NOT NULL DEFAULT 'open',
    derived_version       TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    FOREIGN KEY (source_document_id) REFERENCES documents(document_id),
    FOREIGN KEY (evidence_span_id) REFERENCES evidence_spans(span_id)
);
CREATE INDEX IF NOT EXISTS idx_management_claims_ticker
    ON management_claims(ticker, updated_at DESC);

CREATE TABLE IF NOT EXISTS claim_outcomes (
    outcome_id            TEXT PRIMARY KEY,
    claim_id              TEXT NOT NULL,
    outcome_status        TEXT NOT NULL,
    assessment_date       TEXT NOT NULL,
    rationale             TEXT,
    evidence_span_id      TEXT,
    payload_json          TEXT,
    derived_version       TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    FOREIGN KEY (claim_id) REFERENCES management_claims(claim_id),
    FOREIGN KEY (evidence_span_id) REFERENCES evidence_spans(span_id)
);
CREATE INDEX IF NOT EXISTS idx_claim_outcomes_claim
    ON claim_outcomes(claim_id, assessment_date DESC);

CREATE TABLE IF NOT EXISTS thesis_definitions (
    thesis_id             TEXT PRIMARY KEY,
    ticker                TEXT,
    thesis_name           TEXT NOT NULL,
    thesis_text           TEXT NOT NULL,
    owner_scope           TEXT NOT NULL DEFAULT 'system',
    status                TEXT NOT NULL DEFAULT 'active',
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_thesis_definitions_ticker
    ON thesis_definitions(ticker, updated_at DESC);

CREATE TABLE IF NOT EXISTS thesis_conditions (
    thesis_condition_id   TEXT PRIMARY KEY,
    thesis_id             TEXT NOT NULL,
    ord                   INTEGER NOT NULL,
    condition_kind        TEXT NOT NULL,
    condition_payload     TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    FOREIGN KEY (thesis_id) REFERENCES thesis_definitions(thesis_id),
    UNIQUE (thesis_id, ord)
);

CREATE TABLE IF NOT EXISTS thesis_evaluations (
    thesis_evaluation_id  TEXT PRIMARY KEY,
    thesis_id             TEXT NOT NULL,
    ticker                TEXT,
    document_id           TEXT,
    status                TEXT NOT NULL,
    score                 REAL,
    rationale             TEXT,
    payload_json          TEXT,
    derived_version       TEXT NOT NULL,
    evaluated_at          TEXT NOT NULL,
    FOREIGN KEY (thesis_id) REFERENCES thesis_definitions(thesis_id),
    FOREIGN KEY (document_id) REFERENCES documents(document_id)
);
CREATE INDEX IF NOT EXISTS idx_thesis_evaluations_thesis
    ON thesis_evaluations(thesis_id, evaluated_at DESC);

CREATE TABLE IF NOT EXISTS peer_sets (
    peer_set_id           TEXT PRIMARY KEY,
    ticker                TEXT NOT NULL,
    peer_key              TEXT NOT NULL,
    peer_tickers_json     TEXT NOT NULL,
    rationale             TEXT,
    derived_version       TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    UNIQUE (ticker, peer_key, derived_version)
);
CREATE INDEX IF NOT EXISTS idx_peer_sets_ticker
    ON peer_sets(ticker, updated_at DESC);

CREATE TABLE IF NOT EXISTS sector_rollups (
    sector_rollup_id      TEXT PRIMARY KEY,
    sector                TEXT NOT NULL,
    subsector             TEXT,
    rollup_key            TEXT NOT NULL,
    payload_json          TEXT NOT NULL,
    derived_version       TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    UNIQUE (sector, subsector, rollup_key, derived_version)
);
CREATE INDEX IF NOT EXISTS idx_sector_rollups_sector
    ON sector_rollups(sector, updated_at DESC);

CREATE TABLE IF NOT EXISTS decision_briefs (
    brief_id              TEXT PRIMARY KEY,
    ticker                TEXT NOT NULL,
    source_document_id    TEXT,
    snapshot_id           TEXT,
    brief_kind            TEXT NOT NULL,
    payload_json          TEXT NOT NULL,
    derived_version       TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    FOREIGN KEY (source_document_id) REFERENCES documents(document_id),
    FOREIGN KEY (snapshot_id) REFERENCES company_snapshots(snapshot_id),
    UNIQUE (ticker, brief_kind, derived_version)
);
CREATE INDEX IF NOT EXISTS idx_decision_briefs_ticker
    ON decision_briefs(ticker, updated_at DESC);

CREATE TABLE IF NOT EXISTS review_queue_items (
    review_item_id        TEXT PRIMARY KEY,
    item_type             TEXT NOT NULL,
    ticker                TEXT,
    document_id           TEXT,
    source_key            TEXT NOT NULL,
    priority              TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'open',
    rationale             TEXT,
    payload_json          TEXT NOT NULL,
    derived_version       TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    FOREIGN KEY (document_id) REFERENCES documents(document_id),
    UNIQUE (item_type, source_key, derived_version)
);
CREATE INDEX IF NOT EXISTS idx_review_queue_items_ticker
    ON review_queue_items(ticker, status, updated_at DESC);
