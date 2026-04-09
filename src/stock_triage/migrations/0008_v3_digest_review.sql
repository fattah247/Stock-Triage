CREATE TABLE IF NOT EXISTS daily_digests (
    digest_id            TEXT PRIMARY KEY,
    digest_date          TEXT NOT NULL,
    scope                TEXT NOT NULL,
    payload_json         TEXT NOT NULL,
    derived_version      TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    UNIQUE (digest_date, scope, derived_version)
);
CREATE INDEX IF NOT EXISTS idx_daily_digests_date
    ON daily_digests(digest_date DESC, updated_at DESC);

CREATE TABLE IF NOT EXISTS review_audit_log (
    audit_id             TEXT PRIMARY KEY,
    review_item_id       TEXT NOT NULL,
    ticker               TEXT,
    action               TEXT NOT NULL,
    from_status          TEXT,
    to_status            TEXT NOT NULL,
    note                 TEXT,
    payload_json         TEXT,
    created_at           TEXT NOT NULL,
    FOREIGN KEY (review_item_id) REFERENCES review_queue_items(review_item_id)
);
CREATE INDEX IF NOT EXISTS idx_review_audit_log_item
    ON review_audit_log(review_item_id, created_at DESC);
