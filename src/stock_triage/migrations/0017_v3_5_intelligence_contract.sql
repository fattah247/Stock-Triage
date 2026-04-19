ALTER TABLE nuance_findings
    ADD COLUMN severity_tier TEXT;

ALTER TABLE company_snapshots
    ADD COLUMN issuer_condition TEXT NOT NULL DEFAULT 'watch';

CREATE TABLE IF NOT EXISTS segment_name_aliases (
    alias_id              TEXT PRIMARY KEY,
    canonical_name        TEXT NOT NULL,
    alias_name            TEXT NOT NULL,
    normalized_alias      TEXT NOT NULL,
    scope_type            TEXT NOT NULL DEFAULT 'global',
    scope_value           TEXT NOT NULL DEFAULT '',
    is_default            INTEGER NOT NULL DEFAULT 1,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    UNIQUE (normalized_alias, scope_type, scope_value)
);

CREATE INDEX IF NOT EXISTS idx_segment_name_aliases_canonical
    ON segment_name_aliases(canonical_name, scope_type, scope_value);

CREATE INDEX IF NOT EXISTS idx_segment_name_aliases_normalized
    ON segment_name_aliases(normalized_alias, scope_type, scope_value);

INSERT OR IGNORE INTO segment_name_aliases (
    alias_id, canonical_name, alias_name, normalized_alias, scope_type, scope_value, is_default, created_at, updated_at
) VALUES
    ('segment-alias:global:coal:segmen-batubara', 'coal', 'Segmen Batubara', 'segmen batubara', 'global', '', 1, '2026-04-19T00:00:00+00:00', '2026-04-19T00:00:00+00:00'),
    ('segment-alias:global:coal:coal-mining', 'coal', 'Coal Mining', 'coal mining', 'global', '', 1, '2026-04-19T00:00:00+00:00', '2026-04-19T00:00:00+00:00'),
    ('segment-alias:global:palm-oil:perkebunan-kelapa-sawit', 'palm_oil', 'Perkebunan Kelapa Sawit', 'perkebunan kelapa sawit', 'global', '', 1, '2026-04-19T00:00:00+00:00', '2026-04-19T00:00:00+00:00'),
    ('segment-alias:global:banking:kredit-perbankan', 'banking', 'Kredit Perbankan', 'kredit perbankan', 'global', '', 1, '2026-04-19T00:00:00+00:00', '2026-04-19T00:00:00+00:00'),
    ('segment-alias:global:property:pengembangan-properti', 'property', 'Pengembangan Properti', 'pengembangan properti', 'global', '', 1, '2026-04-19T00:00:00+00:00', '2026-04-19T00:00:00+00:00');
