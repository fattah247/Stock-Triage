-- Market risk layer, phase 4 (docs/market_risk_plan.md, component 5): watched
-- news and official sources, theme tags, theme activity, and emerging findings.

-- Headlines and short excerpts only, never full articles.
CREATE TABLE IF NOT EXISTS watch_items (
    item_id        TEXT PRIMARY KEY,   -- hash of the source and the item's link or id
    source_id      TEXT NOT NULL,
    trust          TEXT NOT NULL,      -- high | medium | low, from the registry when fetched
    published_at   TEXT,               -- UTC, as the source states it
    first_seen_at  TEXT NOT NULL,
    title          TEXT NOT NULL,
    url            TEXT NOT NULL,
    excerpt        TEXT,
    language       TEXT,
    raw_json       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_watch_items_published ON watch_items(published_at);
CREATE INDEX IF NOT EXISTS idx_watch_items_source ON watch_items(source_id, published_at);

CREATE TABLE IF NOT EXISTS watch_source_runs (
    source_id    TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,
    status       TEXT NOT NULL,   -- ok | error
    item_count   INTEGER NOT NULL,
    new_count    INTEGER NOT NULL,
    error        TEXT,
    PRIMARY KEY (source_id, fetched_at)
);

-- Themes promoted from findings. Themes from the themes file are not copied here.
CREATE TABLE IF NOT EXISTS risk_themes (
    theme_id       TEXT PRIMARY KEY,
    category       TEXT,
    title          TEXT NOT NULL,
    severity       TEXT NOT NULL,   -- low | medium | high
    keywords_json  TEXT NOT NULL,
    sectors_json   TEXT NOT NULL,
    origin         TEXT NOT NULL,   -- promoted
    finding_id     TEXT,
    created_at     TEXT NOT NULL
);

-- Every tag cites the exact text in the item that supports it.
CREATE TABLE IF NOT EXISTS item_themes (
    item_id          TEXT NOT NULL,
    theme_id         TEXT NOT NULL,
    method           TEXT NOT NULL,   -- keyword | llm
    evidence_field   TEXT NOT NULL,   -- title | excerpt
    evidence_start   INTEGER NOT NULL,
    evidence_text    TEXT NOT NULL,
    tagger_version   TEXT NOT NULL,
    tagged_at        TEXT NOT NULL,
    PRIMARY KEY (item_id, theme_id, method)
);
CREATE INDEX IF NOT EXISTS idx_item_themes_theme ON item_themes(theme_id);

CREATE TABLE IF NOT EXISTS theme_activity_daily (
    theme_id           TEXT NOT NULL,
    day                TEXT NOT NULL,
    item_count         INTEGER NOT NULL,
    severity_weighted  REAL NOT NULL,
    PRIMARY KEY (theme_id, day)
);

CREATE TABLE IF NOT EXISTS emerging_findings (
    finding_id        TEXT PRIMARY KEY,
    kind              TEXT NOT NULL,   -- unclassified | burst | new_pairing | anomaly
    term_or_entity    TEXT NOT NULL,
    first_seen        TEXT NOT NULL,
    detected_on       TEXT NOT NULL,
    burst_ratio       REAL,
    item_ids_json     TEXT NOT NULL,
    details_json      TEXT NOT NULL,
    status            TEXT NOT NULL,   -- open | dismissed | promoted
    theme_id          TEXT,
    review_item_id    TEXT,
    detector_version  TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_emerging_findings_status ON emerging_findings(status, detected_on);
