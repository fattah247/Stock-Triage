-- A second source of dividends (docs/stock_decisions.md): IDX's monthly
-- dividend table misses some recent dividends, and each company's IDX profile
-- lists its latest one. Profiles are fetched a batch at a time; this records when.

ALTER TABLE dividends ADD COLUMN source TEXT NOT NULL DEFAULT 'idx_dividends';  -- idx_dividends | idx_profiles

CREATE TABLE IF NOT EXISTS company_profile_fetches (
    ticker      TEXT PRIMARY KEY,
    fetched_at  TEXT NOT NULL,
    dividends   INTEGER NOT NULL   -- dividends the profile listed
);
