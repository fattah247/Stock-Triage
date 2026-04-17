ALTER TABLE nuance_findings
    ADD COLUMN tier TEXT NOT NULL DEFAULT 'derived';

ALTER TABLE nuance_findings
    ADD COLUMN source_label TEXT NOT NULL DEFAULT 'unknown';

ALTER TABLE uncertainty_rows
    ADD COLUMN tier TEXT NOT NULL DEFAULT 'derived';

ALTER TABLE uncertainty_rows
    ADD COLUMN source_label TEXT NOT NULL DEFAULT 'unknown';

ALTER TABLE next_read_guidance
    ADD COLUMN tier TEXT NOT NULL DEFAULT 'guidance';

ALTER TABLE next_read_guidance
    ADD COLUMN source_label TEXT NOT NULL DEFAULT 'unknown';

ALTER TABLE contradiction_rows
    ADD COLUMN tier TEXT NOT NULL DEFAULT 'derived';

ALTER TABLE contradiction_rows
    ADD COLUMN source_label TEXT NOT NULL DEFAULT 'unknown';

CREATE TABLE IF NOT EXISTS segment_history (
    segment_history_id     TEXT PRIMARY KEY,
    ticker                 TEXT NOT NULL,
    document_id            TEXT NOT NULL,
    normalized_key         TEXT NOT NULL,
    segment_name           TEXT NOT NULL,
    fiscal_year            INTEGER,
    fiscal_period          TEXT,
    period_end_date        TEXT,
    revenue                REAL,
    profit                 REAL,
    revenue_share_pct      REAL,
    profit_margin_pct      REAL,
    revenue_change_pct     REAL,
    profit_change_pct      REAL,
    trend                  TEXT,
    dominant               INTEGER NOT NULL DEFAULT 0,
    concentration_risk     TEXT,
    derived_version        TEXT NOT NULL,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    FOREIGN KEY (document_id) REFERENCES documents(document_id),
    UNIQUE (ticker, document_id, normalized_key, derived_version)
);

CREATE INDEX IF NOT EXISTS idx_segment_history_ticker
    ON segment_history(ticker, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_segment_history_key
    ON segment_history(normalized_key, period_end_date DESC);
