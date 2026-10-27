-- Cash dividends from IDX's monthly "Dividend Announcement" table, for dividend
-- yield and income (docs/stock_decisions.md). A dividend can appear in more than
-- one monthly table; it is one row per stock, ex-date and amount.

CREATE TABLE IF NOT EXISTS dividends (
    ticker          TEXT NOT NULL,
    ex_date         TEXT NOT NULL,
    cash_per_share  REAL NOT NULL,
    currency        TEXT NOT NULL,   -- IDR, or USD for some miners
    cum_date        TEXT,
    record_date     TEXT,
    payment_date    TEXT,
    note            TEXT,            -- IDX's note, e.g. F (final) or I (interim) and the fiscal year
    report_month    TEXT NOT NULL,   -- the monthly table it was last seen in
    retrieved_at    TEXT NOT NULL,
    PRIMARY KEY (ticker, ex_date, cash_per_share)
);
CREATE INDEX IF NOT EXISTS idx_dividends_ex_date ON dividends(ex_date);
