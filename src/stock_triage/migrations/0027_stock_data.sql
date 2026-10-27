-- Per-stock market data and IDX's monthly financial ratios, for the stock
-- screener, the stock page and the portfolio (docs/market_risk_plan.md,
-- "Stock decisions"). Both come from IDX for every listed company; no filing
-- has to be read for them.

-- One row per stock and trading day, from GetStockSummary, which market-sync
-- already fetches for breadth and foreign flow.
CREATE TABLE IF NOT EXISTS stock_daily (
    ticker           TEXT NOT NULL,
    trade_date       TEXT NOT NULL,
    previous         REAL,
    open             REAL,
    high             REAL,
    low              REAL,
    close            REAL,
    volume           REAL,               -- shares
    value            REAL,               -- rupiah traded in the regular market
    frequency        REAL,
    listed_shares    REAL,
    tradable_shares  REAL,
    foreign_buy      REAL,               -- shares
    foreign_sell     REAL,               -- shares
    PRIMARY KEY (ticker, trade_date)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_stock_daily_date ON stock_daily(trade_date);

-- IDX's "Financial Data and Ratio" table, one row per stock and monthly report.
-- Amounts are rupiah (IDX publishes billions); profit and sales cover the
-- statement's period (year to date), the ratios are IDX's trailing figures at
-- that month's price.
CREATE TABLE IF NOT EXISTS company_ratios (
    ticker             TEXT NOT NULL,
    report_month       TEXT NOT NULL,    -- YYYY-MM of IDX's monthly table
    fs_date            TEXT,             -- the statement the figures come from
    fiscal_year_end    TEXT,
    fs_type            TEXT,
    auditor_opinion    TEXT,
    sector             TEXT,
    sub_sector         TEXT,
    industry           TEXT,
    sub_industry       TEXT,
    sub_industry_code  TEXT,
    sharia             INTEGER,
    assets             REAL,
    liabilities        REAL,
    equity             REAL,
    sales              REAL,
    ebt                REAL,
    profit_period      REAL,
    profit_attr_owner  REAL,
    eps                REAL,
    book_value         REAL,             -- per share
    per                REAL,
    pbv                REAL,
    der                REAL,
    roa                REAL,             -- percent
    roe                REAL,             -- percent
    npm                REAL,             -- percent
    retrieved_at       TEXT NOT NULL,
    PRIMARY KEY (ticker, report_month)
);
CREATE INDEX IF NOT EXISTS idx_company_ratios_month ON company_ratios(report_month);
