# Phase 2 — Market Data Reconnaissance

**Date:** 2026-10-20
**Plan:** `docs/market_risk_plan.md`, component 1 (market data)
**Method:** pages opened in a desktop browser session, network calls read, a
handful of single-date probes spaced at least 1.5 s apart, and three plain-HTTP
requests to Bank Indonesia with the project's identifying user agent
(`stock-triage/0.1 (+local research)`). `robots.txt` was read on both hosts
first: IDX's is empty, and Bank Indonesia's does not exclude `/id/statistik/`.

## Summary

| Need | Source | Access | History |
|---|---|---|---|
| IHSG and every headline and sector index, daily close | IDX `GetIndexSummary` | IDX JSON via the existing browser session | From January 2020 |
| Breadth (stocks up vs down) and turnover | IDX `GetStockSummary` | Same | From January 2020 |
| Foreign net flow | IDX `GetStockSummary` (foreign buy and sell volumes per stock) | Same | From January 2020; value is an estimate |
| USD/IDR reference rate | Bank Indonesia JISDOR page | Plain HTTP (ASP.NET form, export to xlsx) | At least January 2020; earlier untested |

The plan hoped for ten years. IDX's endpoints start in 2020, which covers the
COVID crash, the 2025 selloffs and October 2026, but not 2015 or 2018.

## IDX

IDX's site sits behind a Cloudflare managed challenge (`docs/phase1_recon.md`).
No challenge appeared during this recon. The pipeline's existing access path
(the persistent Playwright profile in `recon/browser.py`, calling `/primary/...`
JSON endpoints from inside an opened IDX page) applies unchanged.

### Index summary, by date

`GET /primary/TradingSummary/GetIndexSummary?date=YYYYMMDD&length=9999&start=0`

Page: `/en/market-data/trading-summary/index-summary`, "Historical" tab. The
date picker sends `date=YYYYMMDD`; omitting it returns the latest day.

The response is DataTables-shaped: `{draw, recordsTotal, recordsFiltered, data: [...]}`.
It returned 45 rows on 2026-10-19: 34 headline indices (`COMPOSITE`, `LQ45`,
`IDX30`, ...) and the 11 IDX-IC sector indices (`IDXENERGY`, `IDXBASIC`,
`IDXINDUST`, `IDXNONCYC`, `IDXCYCLIC`, `IDXHEALTH`, `IDXFINANCE`,
`IDXPROPERT`, `IDXTECHNO`, `IDXINFRA`, `IDXTRANS`).

Fields per row: `IndexCode`, `Date`, `Previous`, `Highest`, `Lowest`, `Close`,
`Change`, `Volume`, `Value`, `Frequency`, `MarketCapital`, `NumberOfStock`.

Example, 2026-10-19:

- COMPOSITE: close 4,996.609, previous 5,185.105 (−3.64%), 918 stocks, value Rp 25.1 tn.
- IDXFINANCE: close 1,150.502, previous 1,176.087.

History probes:

| Date | Rows | Note |
|---|---|---|
| 2011-08-01, 2013-06-03, 2015-08-24, 2018-01-02, 2019-01-02, 2019-07-01 | 0 | Before the endpoint's history |
| 2020-01-02 | 34 | COMPOSITE 6,283.581 |
| 2020-03-23 | 34 | COMPOSITE 3,989.517 (COVID low); no IDX-IC sector indices yet |
| 2026-10-17 (Saturday) | 0 | Non-trading days return no rows |

The exact first date, and when the IDX-IC sector indices begin, will be found
by the backfill itself.

### Stock summary, by date

`GET /primary/TradingSummary/GetStockSummary?date=YYYYMMDD&length=9999&start=0`

Page: `/en/market-data/trading-summary/stock-summary`. Same response shape and
date parameter. It returned 963 rows on 2026-10-19 and 687 on 2020-03-23.

Each response is about 400 KB per day. Fields used:

- `Previous`, `Close` — breadth;
- `Volume`, `Value` — turnover, and an average price `Value / Volume`;
- `ForeignBuy`, `ForeignSell` — foreign trades as **share volumes**.

Derived daily figures:

- **Advancers and decliners:** 103 up and 691 down on 2026-10-19; 68 and 332 on 2020-03-23.
- **Foreign net flow (estimated):** the sum over stocks of `(ForeignBuy − ForeignSell) × Value / Volume`. IDX's average price includes all regular-market trades, so this is an estimate, not IDX's official foreign-flow figure. For example, BBCA on 2026-10-19 had 76.7 m shares bought and 281.5 m sold by foreigners, about Rp 1 tn of net selling.

### Live endpoints (not needed for daily closes)

- `/primary/StockData/GetConstituent` — the 34 headline indices, live, `Items`-shaped.
- `/primary/StockData/GetIndexIC` — the 11 sector indices, live, `data`-shaped.
- `/primary/StockData/GetLastTimeLivefeed` — the live-feed timestamp.

### Exchange notices (phase 2b, recon of 2026-10-21)

Exchange-issued notices are not in the listed-company disclosure feed the
pipeline reads. Two endpoints serve them, both readable with `fetch()` from
inside an opened IDX page (plain HTTP gets the challenge page):

- **`/primary/NewsAnnouncement/GetSuspension?indexFrom=0&pageSize=N&lang=id`:** trading suspensions and their lifting, one row per notice, with fields `Date`, `Info_Type` (`SPT` suspend, `UPT` lift), `Kode` (ticker), `Judul` (title) and `Data_Download` (PDF path). The default window is the last three years, and `dateFrom`/`dateTo` (`YYYYMMDD`) narrow it. In October 2026 it held 1,923 rows from 2023-10-23; March 2020 returns none.
- **`/primary/NewsAnnouncement/GetUMA?indexFrom=0&pageSize=N&lang=id`:** unusual market activity announcements, with fields `UMADate`, `UMAID`, `AnnouncementNo`, `CompanyID`, `CompanyName`, `Judul`, `Status` and `Attachment`. It held 1,194 rows, also from 2023-10-23.
- **Page size:** one call with `pageSize` above the count returns everything, about 0.5 MB.
- **What is missing:** market-wide trading halts (for example 18 March and 8 April 2025) appear in neither list. They are covered by the `exchange_rules_halts` news theme instead.
- **Neighbouring paths:** `GetSuspend` and `TradingSummary/GetSuspendedStock` return 503, and `GetAllAnnouncement` returns a challenge.

`market-sync` fetches both lists once a day through the page it already opens,
stores them in `exchange_notices`, and writes daily counts as the series
`notices:suspend` and `notices:uma`.

## Bank Indonesia JISDOR

Page: `https://www.bi.go.id/id/statistik/informasi-kurs/jisdor/default.aspx`
(SharePoint, ASP.NET WebForms). No bot protection was observed: a plain GET
with the project's user agent returned the page and its latest 10 rates.

- **Search:** POST to the same URL with the page's hidden fields (`__VIEWSTATE`, `__VIEWSTATEGENERATOR`, `__EVENTVALIDATION`, `__REQUESTDIGEST`), plus `…$TextBoxFrom` and `…$TextBoxDateTo` (dd/mm/yyyy) and `…$ButtonCari=Cari`. Results are paged server-side at 10 rows per page.
- **Export:** a second POST after the search, with `__EVENTTARGET=…$ButtonExport` and the same dates. It returns `Informasi Kurs Jisdor.xlsx`: one sheet with header `NO, Tanggal, Kurs`, dates as `M/D/YYYY 12:00:00 AM`, and rates as `13.895,00`. January 2020 came back as 22 rows in about 3.4 KB. The stdlib workbook reader in `parse/workbook.py` reads it.
- **Field names:** the `…` prefix is a SharePoint web-part ID (`ctl00$ctl50$g_8e393016_…$ctl00$`) that can change on redeploy. The adapter finds fields by their suffix.
- **Observed rates:** Rp 13,895 on 2020-01-02; Rp 17,910 on 2026-10-06; Rp 18,052 on 2026-10-16; Rp 18,050 on 2026-10-19.
- **Notes BI publishes on the page:**
  - the fixings for 26 November 2021 and 17 December 2021 reused the previous day's data;
  - the rate for 2 April 2024 was revised.

The cookie banner was left untouched; reading the page does not need it.

## Consequences for the build

1. **Backfill size:** two IDX calls per trading day (index summary, stock summary) give about 3,400 calls since 2020. At the configured `discovery.rate_limit_per_second` (1.0), plus page settling, expect roughly an hour, run once. Weekends are skipped; holidays come back empty.
2. **No saved bodies:** the shared fetch helpers save every response body under the browser artifacts directory, which is right for discovery but would mean about 0.7 GB of files for a backfill. Market calls fetch inside the page without saving, and only daily totals are stored.
3. **Labels:** foreign net flow is stored as `flow:foreign_net_value_est` and labelled an estimate everywhere it appears.
4. **Sector signals:** these start when the IDX-IC sector indices start. Earlier days have headline indices only.
5. **JISDOR:** fetched by calendar-year ranges with plain HTTP, three requests per year.
6. **The ten-year goal** is not met by these sources. Backtests (phase 5) cover 2020 onward unless a longer free source is found later.
