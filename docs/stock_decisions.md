# Stock Decisions

**Added:** 2026-10-22
**Code:** `market/idx_ratios.py`, `market/idx_dividends.py`, `market/idx_profiles.py`, the per-stock part of `market/sync.py`, `stocks/metrics.py`, `stocks/portfolio.py`, `dashboard/stocks.py`, `dashboard/static/decide.js`.
**Schema:** migration `0027_stock_data.sql` adds `stock_daily` and `company_ratios`; `0028_dividends.sql` adds `dividends`; `0029_dividend_sources.sql` adds its `source` and `company_profile_fetches`.

The dashboard opens on four views for deciding: **Overview**, **Market**,
**Stocks** and **Portfolio**. The filing views (Filings, Company, Review, Ops)
sit after them. Every number comes from IDX's own data, for every listed
company. The views describe; the decision stays with the reader.

## Data

| Table | Source | Coverage | Fetched by |
|---|---|---|---|
| `stock_daily` | IDX `GetStockSummary`, one row per stock and day: previous, open, high, low, close, volume, value, frequency, listed and tradable shares, foreign buy and sell volumes | 2020-01-02 onward, about 960 stocks a day | `market-sync`, in the same call that feeds breadth and foreign flow |
| `dividends` | IDX's monthly "Dividend Announcement" table (`urlName=LINK_DIVIDEND`): cash per share, currency, cum, ex, record and payment dates | 2019-01 onward | `market-sync`, like the ratio table |
| `dividends` (`source = 'idx_profiles'`) | Each company's IDX profile (`/primary/ListedCompany/GetCompanyProfilesDetail`): its latest dividend, often as a total, turned per share with the shares listed on the cum date | the latest dividend per company | `market-sync`: every company the first time, then 150 a day, so each is refreshed about weekly |
| `company_ratios` | IDX's monthly "Financial Data and Ratio" table (`/primary/DigitalStatistic/GetApiDataPaginated`, `urlName=LINK_FINANCIAL_DATA_RATIO`): assets, liabilities, equity, sales, EBT, profit, profit to owners, EPS, book value, P/E, P/BV, D/E, ROA, ROE, net margin, with the IDX-IC sector and industry | 2021-01 onward, about 960 stocks a month | `market-sync`: each month not stored yet, and the latest two again once a day |

- **Amounts are rupiah.** IDX publishes billions; `company_ratios` stores them multiplied out.
- **Why two dividend sources.** In October 2026 the monthly table held 142 dividends for 2025 against about 250 in earlier years, and nothing after 2023 for TLKM or ANTM. The profiles fill the latest dividend. One dividend is counted once: by record date, the monthly table winning over a profile's estimate.
- **Sales and profit are year to date** at the statement date (`fs_date`). EPS and the ratios are IDX's trailing figures at that month's price.
- **Days synced before** per-stock rows were kept are fetched again for their stock summary alone (the `idx_stocks` ledger). The first sync after this change does so for every day already synced, about an hour.
- **No gaps.** Without `--backfill-from`, a sync starts at the day after the last one synced when that is more than ten days back. The current day is fetched only after 18:00 in Jakarta, when IDX's summary is final. An empty stock summary on a day the index traded is an error to retry, not a holiday.

## Per-stock numbers (`stocks/metrics.py`)

- **Prices are adjusted** for splits, reverse splits, rights and bonus shares. On such a day IDX sets the stock's "previous" price to the adjusted one, and every earlier close is scaled by that ratio. Only consecutive trading days are compared, so a missing day is never taken for a split. Cash dividends are not added back, so returns are price returns.
- **Returns:** 1 month, 3 and 6 months and 1 year (21, 63, 126 and 252 sessions), and year to date. **vs IHSG** is the stock's return minus IHSG's over the same days, in percentage points.
- **52-week range:** the high and low close of the last 252 sessions, how far below the high the stock is, and where it sits in the range. A stock listed for under a year has its range since listing (at least 60 sessions; fewer give none).
- **Volatility:** the standard deviation of daily returns over 60 sessions, annualised. Left empty, like beta, for a stock with more than 5 days without a trade in the last 20: its flat price would read as the calmest in the market.
- **Beta:** the stock's daily returns against IHSG's over the past year (at least 120 shared days).
- **Liquidity:** the average value traded over 20 sessions, and how many of them saw no trade.
- **Foreign flow:** foreign net share volumes valued at each day's average price, summed over 20 sessions, so an estimate.
- **Market cap:** the last close times listed shares.
- **Valuation:** P/E is the latest close over IDX's trailing EPS (empty when losing money), P/BV the close over book value per share, and the earnings yield is EPS over price. **P/E vs its past** is where today's P/E sits among the monthly P/Es since 2021 (at least 12 months): 0% is its cheapest.
- **Growth:** the latest statement's year-to-date sales and profit to owners against the statement a year earlier.
- **Dividends:** the cash paid per share with an ex-date in the past year (US-dollar dividends at that day's JISDOR rate, dividends before a split scaled to today's shares), its **yield** at today's price, its **payout** of trailing EPS, and the number of years since 2019 with a dividend. A stock with no dividend on record in the past year has a 0% yield.
- **Net margin** is profit over sales of the same period. IDX's own figure divides a trailing year's profit by year-to-date sales, which overstates it mid-year (109.8% for BBCA in June 2026 against 55.8%).
- **Peers:** the same IDX-IC industry, or the sector when the industry has fewer than six stocks. Their medians sit beside each number.

### Scores

Each score ranks a stock within its sector from 0 to 100; 50 is the middle.
A stock with none of a score's inputs has no score for it.

| Score | Inputs (higher is better unless noted) |
|---|---|
| Value | earnings yield, book value per rupiah of price |
| Quality | ROE, net margin, debt to equity (lower is better; left out for financials). With negative equity, ROE and debt to equity are left out |
| Growth | sales and profit against a year before |
| Momentum | 6-month and 1-year return against IHSG |
| Stability | volatility and distance below the 52-week high (both lower is better) |

### Warnings

| Level | Warning | Rule |
|---|---|---|
| Serious | Suspended | an IDX suspension not yet lifted (notices start in October 2023) |
| Watch | Was suspended | a suspension in the last 30 days, since lifted |
| Serious | Losing money | negative EPS or profit to owners |
| Serious | Negative equity | equity below zero |
| Serious | Thinly traded | under Rp 1 bn traded a day |
| Watch | Unusual activity | an IDX UMA notice in the last 30 days |
| Watch | High debt | debt over 2x equity, outside financials |
| Watch | Profit or sales down | profit down over 20%, or sales over 10%, on a year before |
| Watch | Far from its high | 30% or more below the 52-week high |
| Watch | Very volatile | volatility of 70% a year or more |
| Watch | Old statement | the latest statement is over nine months old |
| Note | Foreign selling | foreigners sold more than a tenth of 20 sessions' trading |
| Note | P/E near its high or low | in the top or bottom 10% of its range since 2021 |

## Screens (the Stocks view)

Presets narrow the list; the filters (sector, minimum traded value, Sharia
only, hide loss-makers, ticker or name) apply on top. Each preset says its
rule on screen:

- **Quality at a fair price:** ROE at least 15%, profitable, P/E at or below its peers' median, debt at most 1x equity outside financials, at least Rp 5 bn traded a day.
- **Cheap against its own past:** P/E in the bottom 20% of its range since 2021, profitable, at least Rp 1 bn traded a day.
- **Dividend payers:** a yield of 4% or more over the past year, dividends in three or more years since 2019, profitable, payout at most 100%, at least Rp 1 bn a day.
- **Strong trend:** beat IHSG over a year, above its 200-day average, within 15% of its 52-week high, at least Rp 5 bn a day.
- **Fallen but profitable:** at least 30% below its 52-week high, ROE of 10% or more, at least Rp 5 bn a day.
- **Serious warnings** and **Watchlist**.

The table shows one column set at a time (Summary, Valuation, Profit and
growth, Price and risk) so it stays readable; any column sorts.

## Portfolio

Holdings live in a YAML file on this machine, `market.portfolio_path`
(default `<paths.data_dir>/portfolio.yaml`; see `config/portfolio.example.yaml`).
One lot is 100 shares. The view values each position at the latest close and
shows its gain against the average price (before fees and dividends), its
weight, today's change, the dividends its shares received over the past year,
the sector mix, and a rough guide to a market fall:
the stocks' value-weighted beta times 10%, scaled down by the cash share.

There is no broker connection. Stockbit publishes no API, and its unofficial
endpoints need a login token that can trade the account, so the file is the
safe way in.

## Not covered yet

- **Total return.** Price returns leave dividends out; the dividend yield is shown beside them.
- **Statements before 2021** in the ratio table, dividends before 2019, and per-stock prices before 2020.
- **Intraday prices.** Everything is as of the latest close `market-sync` stored.
