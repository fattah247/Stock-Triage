-- Market risk layer, phase 2b (docs/market_risk_plan.md): exchange-issued notices,
-- trading suspensions and unusual market activity (UMA) announcements. IDX
-- serves a rolling three years of each; daily counts feed a market signal.

CREATE TABLE IF NOT EXISTS exchange_notices (
    notice_id        TEXT PRIMARY KEY,   -- hash of the kind and the exchange's own key
    kind             TEXT NOT NULL,      -- suspend | unsuspend | uma
    notice_date      TEXT NOT NULL,      -- the day it applies to, in Jakarta
    published_at     TEXT,
    ticker           TEXT,
    title            TEXT NOT NULL,
    announcement_no  TEXT,
    url              TEXT,
    raw_json         TEXT NOT NULL,
    fetched_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exchange_notices_date ON exchange_notices(notice_date, kind);
