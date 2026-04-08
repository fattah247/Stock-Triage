CREATE TABLE IF NOT EXISTS source_watermarks (
    source_name      TEXT PRIMARY KEY,
    source_class     TEXT NOT NULL,
    window_start     TEXT,
    window_end       TEXT,
    cursor           TEXT,
    raw_state_json   TEXT,
    updated_at       TEXT NOT NULL
);
