ALTER TABLE events ADD COLUMN impact_direction TEXT;
ALTER TABLE events ADD COLUMN confidence REAL;
ALTER TABLE events ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}';
