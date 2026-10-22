# Market Risk Layer — Plan

**Date:** 2026-10-21 (revision 4)
**Status:** Phases 1–5 and 2b implemented. Calibration: `docs/market_calibration.md`.

Revision 2 changes:

- **Crash-risk estimation is now a goal**, through historical analogs and a calibrated estimate whose track record is always shown (component 6).
- **Discovery of drivers nobody is tracking yet** is added (component 5).

Revision 3 changes:

- **The risk-factor catalog** (`docs/market_risk_factors.md`) defines what to watch. It covers 13 categories, from US rates and commodities to war, pandemics, natural disasters and cyber attacks, and states the detector for each factor.
- **Fundamental stress breadth from filings** joins phase 1 (component 2).
- **Scenario playbooks and a completeness review** are placed in phases 4–5.

## Why

Stock-Triage reads company filings. Everything it knows about risk arrives
through `idx_financial_reports` and `idx_disclosures` documents, which land
weeks to months after the period they describe. It holds no index levels,
prices, foreign flows, exchange rates, news, or market calendar, so it could
not have flagged the October 2026 IHSG decline.

The press attributes that decline to forces that are all outside company
filings ([CNBC Indonesia][cnbc], [Suara][suara], [BSI News][bsi]):

- MSCI reportedly consulting on moving Indonesia from Emerging to Standalone
  Market status (a roughly six-month consultation, not a decision);
- Middle East tension and oil;
- hot US inflation ahead of the Fed decision on 2026-10-28;
- a weaker rupiah, foreign selling, and financials leading the fall.

Several of these, the MSCI consultation especially, are not variables anyone
would have picked in advance. The aim is to be ready for the next shock
whatever its cause: war, pandemic, disaster, policy, or something new. So the
layer works from a catalog covering every category of driver
(`docs/market_risk_factors.md`), plus detectors for whatever the catalog
misses. Reported magnitudes vary
between outlets and should be checked against IDX's own data before use.

## Goals and non-goals

The layer exists to:

1. **Anticipate crash risk.** Estimate the chance of a large IHSG drawdown, show the past episodes today most resembles and what followed them, and publish how well the estimate has done historically. History rhymes; the layer makes that testable instead of a feeling.
2. **See stress building** in market data, with every flag showing its numbers.
3. **Discover unknown drivers:** watch official sources and news broadly, and surface themes nobody is tracking yet.
4. **Show exposure:** which watchlist companies are most fragile.
5. **Remind ahead** of known risk dates.
6. **Prevent being caught unprepared.** Rules written in calm times fire through the existing thesis and alert machinery.
7. **Learn from each episode** so signals and estimates improve.

Non-goals:

- **Certainty.** Every forecast is a probability with its record next to it. If the estimate doesn't beat a naive base rate in backtests, it isn't shown as a forecast; only the analogs are.
- **Stopping the market.** No tool can keep IHSG from falling. The aim is that the user isn't surprised by it.
- **Trading.** The layer alerts; the user decides. It is research tooling, not investment advice.
- **An LLM-scored state.** The market state and the estimate stay deterministic and versioned, like rules (README: "Rules vs. LLM separation"). An LLM only tags and summarizes text, with citations.

## Where it plugs in

| Existing piece | How the layer uses it |
|---|---|
| `metric_items` + `preferred_metric_sql` (xlsx-first figures) | Exposure inputs: leverage, operating cash flow, equity, reporting currency |
| Document pipeline (discover → download → parse → extract → summarize), `SourceClass` enum | News and official notices enter as a new source class and get evidence spans and citations like filings |
| Local LLM (`llm.model_name` via Ollama; `qwen3.5:9b`, chosen by `llm-eval`) and the summary validation (`schema_valid`, `evidence_link_valid`) | Theme tagging and "what happened" notes; only output that passes validation is used |
| Review queue (`review-queue`, `review-update`) | Emerging themes wait there for a human decision |
| Theses (`data/theses.yaml`; kinds `metric_delta`, `event_present`, `rule_present`; a matched `stop` condition marks the thesis `broken`) | New condition kinds `market_regime`, `market_signal`, `crash_risk`, `exposure_score` |
| `alerts-run` (`alerts/pipeline.py`) | New document-less alert kinds; a push channel |
| `runlock.py` (one pipeline command at a time) | New fetching commands take the lock |
| Filing expectations (intelligence) | Merged into the risk calendar |
| `recon/` browser access to IDX | Reused to fetch IDX market summaries |
| Golden checks, review digest | Golden dates for the state; market and theme sections in the digest |
| Dashboard (`dashboard/`) | Market strip on Home, exposure card in company Summary |

## Components

### 1. Market data (`src/stock_triage/market/`)

Daily time series, kept out of the document pipeline. Each value records its
source and retrieval time, so backtests can use point-in-time data.

```text
market_series_daily(series_id, trade_date, value, source_name, retrieved_at)
  PRIMARY KEY (series_id, trade_date)
```

Proposed series:

- `index:COMPOSITE` — IHSG close
- `index:<sector>` — sector indices, financials first
- `flow:foreign_net_value` — daily foreign net buy/sell value
- `breadth:advancers`, `breadth:decliners`
- `turnover:value`
- `fx:USDIDR_JISDOR` — Bank Indonesia's reference rate

Later: the catalog's other "Signal" factors, for example:

- US yields, the dollar, VIX and credit spreads (catalog A);
- coal, nickel, palm oil, oil and gold (catalog C);
- reserves, inflation, bond yields and foreign ownership (catalog D).

Each extra source (e.g. FRED, the World Bank Pink Sheet, BI and BPS
statistics) is its own small adapter writing into the same table.

Sources: IDX daily index and trading summaries through the existing browser
access (IDX sits behind a managed challenge; see `docs/phase1_recon.md`), and
Bank Indonesia for JISDOR. The exact endpoints are unverified; phase 2 opens
with a recon spike written up the way `phase1_recon.md` is.

Commands: `market-sync` (daily) and `market-sync --backfill-from YYYY-MM-DD`.

### 2. Signals and market state (`market/signals.py`)

Each signal maps to a level: `none`, `elevated`, `stressed` or `crisis`.
Thresholds live in config and are starting points to be tuned by the
backtest (component 9), not settled values.

| Signal | Elevated | Stressed | Crisis |
|---|---|---|---|
| IHSG drawdown from 52-week high | 5% | 10% | 20% |
| 20-day volatility ÷ 1-year median | 1.5× | 2× | 3× |
| Consecutive foreign net-sell sessions | 5 | 10 | 15 |
| USD/IDR 20-day change | +2% | +3% | +5% |
| Share of stocks declining (5-day average) | 60% | 70% | 80% |
| Sector index drawdown (per tracked sector) | 7% | 12% | 25% |
| Active high-severity themes (component 5) | 1 | 2 | 3 |
| Fundamental stress breadth (catalog L): rise in the share of filers with negative operating cash flow, negative equity or going-concern warnings, versus a year earlier | +5 pts | +10 pts | +20 pts |

**State rule:**

- The day's state is the highest level reached by at least two signals.
- Drawdown alone can raise it to `crisis`.
- It steps down only after a configurable number of calmer sessions, so it doesn't flap.
- A risk date within the lead window adds an `event_risk` flag without changing the state.

```text
market_signal_runs(trade_date, signal_name, value, level, ruleset_version, inputs_json, evaluated_at)
market_regime_days(trade_date PK, regime, drivers_json, ruleset_version, evaluated_at)
```

`drivers_json` names the signals and numbers behind the state, so the
dashboard and alerts can say why.

Proposed config:

```yaml
market:
  series: [index:COMPOSITE, index:IDXFINANCE, flow:foreign_net_value, fx:USDIDR_JISDOR]
  thresholds:
    drawdown_pct: {elevated: 5, stressed: 10, crisis: 20}
    vol_ratio: {elevated: 1.5, stressed: 2.0, crisis: 3.0}
    foreign_sell_streak: {elevated: 5, stressed: 10, crisis: 15}
    usdidr_change_20d_pct: {elevated: 2, stressed: 3, crisis: 5}
  regime:
    min_signals: 2
    step_down_after_sessions: 5
  catalyst_lead_days: 3
  crash_risk:
    drawdown_pct: 10        # what counts as a crash; see "Decisions for review"
    horizon_days: 63        # about three months of sessions
```

### 3. Risk calendar

`data/catalysts.yaml`, synced into `market_catalysts` the way theses sync
today:

```yaml
catalysts:
  - date: 2026-10-28
    kind: fomc
    title: FOMC rate decision
    severity: high
  - date: 2026-11-10
    kind: msci_review
    title: MSCI index review announcement (illustrative date)
    severity: high
```

- **Kinds:** `fomc`, `bi_rate`, `msci_review`, `msci_classification`, `ftse_classification`, `sovereign_rating`, `us_cpi`, `bps_release`, `election`, `lockup_expiry`, `enso_outlook`, `policy`, `custom`. These cover every "Calendar" factor in the catalog.
- **Filing deadlines:** taken from filing expectations, so one calendar holds both.
- **Reminders:** fire `catalyst_lead_days` ahead of each date.
- **Growth:** entries are added by hand, and also proposed by component 5 when a watched source announces a dated event.

### 4. Exposure ranking (`market/exposure.py`)

A deterministic fragility score per watchlist company, with reasons. It
starts with data the pipeline already has:

| Component | Input |
|---|---|
| Leverage | `total_liabilities / total_assets`, latest period, xlsx-first |
| Negative equity | `equity < 0` |
| Cash burn | `operating_cash_flow < 0` |
| Net loss | `net_profit < 0` |
| Reporting currency | `currency` other than IDR |
| Index membership | `data/index_membership.yaml` (LQ45, IDX30, MSCI Indonesia), maintained by hand |
| Sector concentration | share of the watchlist in one sector |
| Theme exposure | sectors or companies named by active themes (component 5) |

Later inputs, once component 1 provides prices: liquidity (average daily
value) and sensitivity to IHSG.

True FX risk needs USD-denominated debt from the notes, which the extractor
doesn't read yet. Until then, "reporting currency" is only a weak stand-in.

```text
exposure_scores(ticker, as_of_date, score, components_json, derived_version)
```

### 5. Risk discovery: known and unknown drivers

Fixed variables only catch risks someone already thought of. This component
casts a wide net over text sources and flags what is new.

**5a. Watch registry.** `data/watch_sources.yaml` lists sources with their
fetch method (RSS, page, or API) and cadence. Candidate groups:

- **Index providers:** MSCI (index reviews, market classification reviews and consultations) and FTSE Russell (country classification).
- **Rating agencies:** sovereign rating actions on Indonesia by S&P, Moody's and Fitch.
- **Domestic authorities:** Bank Indonesia, OJK, the Ministry of Finance, BPS statistics, IDX and KSEI notices.
- **News:** Indonesian business outlets with public feeds, and global wires where licensing allows.
- **Wide net:** a global news index queried for Indonesia, e.g. GDELT, to be evaluated in the phase 4 spike.

Adding a source is a YAML edit, not code, so the net widens over time.

**5b. Items through the existing pipeline.** Each item enters as a new
`SourceClass` (for example `news` and `official_notice`). It then goes
through discover → download → parse → extract like a filing, so it gets
evidence spans, dedupe and revisions for free. Where licensing requires,
only the headline, a short excerpt and the link are kept.

**5c. Known themes.** `data/risk_themes.yaml` is seeded from every "Theme"
factor in the catalog's 13 categories:

- A–D: global money, China and trade, commodities, the domestic economy and the rupiah;
- E–G: market structure and index providers, politics and governance, banks and credit;
- H–K: war and geopolitics, pandemics and disease, natural disasters and climate, technology and operations;
- M: sentiment.

Each theme has keywords in Indonesian and English, a default severity, and
the sectors it tends to hit. Items are tagged by keyword rules first, and
the local LLM tags the rest.

- An LLM tag must cite an evidence span from the item. Output that fails the existing validation (`schema_valid`, `evidence_link_valid`) is discarded.
- That check proves the cited span exists, not that the tag is right. This is why promotions stay with a human.

**5d. Novelty: what nobody is tracking yet.** Three detectors, all simple and
explainable:

- **Unclassified items:** an item no theme matches, from a high-trust source, is queued for review.
- **Term bursts:** words, phrases or names whose weekly frequency jumps well above their own baseline. For example, "standalone market" appearing suddenly next to "MSCI". This is plain counting, no model.
- **New pairings:** an organization or term appearing alongside "Indonesia" or "IHSG" for the first time, or far more often than before.

Findings go to the existing review queue. A reviewer can dismiss one, or
promote it to a tracked theme with keywords, a severity, affected sectors,
and optionally a calendar entry or a market signal. Every promotion makes
the registry smarter.

**5e. Anomaly first, explanation second.** Prices often move before anyone
has named the cause. When the market moves abnormally without a known
catalyst, the layer opens a research item. Examples:

- IHSG or a sector beyond about 2.5 standard deviations;
- a sudden jump in foreign selling;
- a sector breaking from its usual relation to IHSG.

The research item gathers watched items from that window, clusters them, and
writes a cited "what likely happened" note. Its causes are proposed as new
themes when they don't match existing ones.

```text
watch_items(item_id, source_id, published_at, title, url, excerpt, document_id)
item_themes(item_id, theme_id, method, evidence_span_id)   -- method: keyword | llm
theme_activity_daily(theme_id, trade_date, item_count, severity_weighted)
emerging_findings(finding_id, kind, term_or_entity, first_seen, burst_ratio, review_item_id)
```

`theme_activity_daily` feeds back into component 2 (active high-severity
themes), component 4 (theme exposure) and component 6 (as an input feature).

### 6. Crash-risk estimate and historical analogs (`market/forecast.py`)

This is the "history repeats" part, made testable.

**6a. Analogs.**

- Build today's feature vector: drawdown, volatility ratio, foreign-flow streak, rupiah change, breadth, sector stress, and active theme counts.
- Compare it with the vector at the start of every past episode.
- Show the three nearest, and what followed each: the further drawdown, the time to the bottom, and the time to recover.

This needs no model and is easy to inspect.

**6b. Estimate.**

- A small calibrated model, e.g. logistic regression on a handful of leading features, gives the probability of a crash within the horizon. A crash is an IHSG drawdown of at least `crash_risk.drawdown_pct` within `crash_risk.horizon_days`.
- It is trained only on point-in-time data (values as known on each day), validated walk-forward, and versioned like rules.

**6c. Track record next to every number.**

- The forecast always appears with its walk-forward hit rate, false-alarm rate, and Brier score against the naive base rate.
- If the estimate doesn't beat the base rate, the dashboard shows the analogs only.

```text
crash_risk_days(trade_date PK, probability, features_json, analogs_json, model_version, evaluated_at)
forecast_track_record(model_version, horizon_days, drawdown_pct, brier, base_rate_brier, hit_rate, false_alarm_rate, evaluated_through)
```

### 7. Pre-commitment through theses

New condition kinds, evaluated alongside the existing ones:

```yaml
theses:
  - thesis_id: watchlist-market-guard
    ticker: null
    thesis_name: Review the watchlist when the market breaks
    owner_scope: portfolio
    status: active
    conditions:
      - severity: stop
        kind: market_regime
        at_least: stressed
      - severity: stop
        kind: crash_risk
        operator: gte
        value: 0.4
      - severity: warning
        kind: market_signal
        signal: foreign_sell_streak
        operator: gte
        value: 10
      - severity: warning
        kind: exposure_score
        operator: gte
        value: 70
```

A tripped `stop` condition marks the thesis `broken`, and the existing
alerts already report broken theses as critical.

A ticker-less thesis is loaded for every company (`load_db_theses` matches
`ticker = ? OR ticker IS NULL`) and evaluated once per company. So a market
stop rule breaks it for each company at once, and alerts (watchlist-filtered)
send one critical alert per watchlist company. There are two ways to handle
this:

- **Keep per-company evaluation** and group the resulting alerts into one message per run.
- **Evaluate `owner_scope: portfolio` theses once per run** instead.

See "Decisions for review".

### 8. Alerts and delivery

- **New alert kinds:**
  - `market_regime` — a state change;
  - `crash_risk` — the estimate crossing a configured level, with its analogs and track record;
  - `emerging_theme` — a novelty finding from a high-trust source, or a theme promoted to high severity;
  - `catalyst` — a risk date entering the lead window;
  - `exposure` — when the state is `stressed` or worse, the five most fragile watchlist companies with their reasons.
- **Document-less alerts:** `AlertSignal.document_id` is required today. Market alerts need it optional, or a synthetic key such as `market:<trade_date>`. They keep the existing per-channel dedupe, plus a cooldown so a bad week sends one alert, not one per run.
- **Noise control:** ordinary theme activity goes into the review digest; only high-severity findings alert at once.
- **Delivery:** `alerts.channels` defaults to `["jsonl"]`, and only `jsonl` actually delivers today. Add one push channel (see "Decisions for review").
- **Schedule:**
  - each trading day after the IDX close: `market-sync` → `market-signals` → `crash-risk` → `alerts-run`, under the run lock;
  - several times a day: the watch sources;
  - weekly: the review digest gains market, theme and emerging-finding sections.

### 9. Research loop

- **Episodes:** a `market_episodes` table.
  - An episode opens when the state reaches `stressed` and closes after a run of calm sessions.
  - It snapshots all signals, themes and the estimate at open, then records the worst state and maximum drawdown.
  - Afterwards, the user records the cause and lessons, and links the themes involved.
  - Each closed episode becomes a new analog for component 6.
- **Backtest:** `market-backtest --from <date>` replays signals, the state and the estimate over history using point-in-time data. For each past drawdown (e.g. 2015, 2018, 2020, 2025, October 2026) it reports how early each signal fired, how many false alarms it raised, and how the estimate scored. That is the basis for tuning.
- **Small sample:** there are only a handful of real episodes, so the rules stay simple, the model stays small, and both are reviewed after each new episode.
- **Golden dates:** known dates with their expected state, run by the existing golden checks.

### 10. Dashboard

- **Home:** a Market strip showing:
  - the state, and the three signals driving it with their numbers;
  - the crash-risk estimate with its track record, or analogs only when the estimate isn't trustworthy;
  - active themes, and the count of emerging findings awaiting review;
  - the next risk dates, and the most exposed watchlist companies.
- **Company Summary:** an exposure card next to Key figures, listing the components and any active themes that name the company or its sector.

## Phases

Each phase is about one PR, tested offline with synthetic fixtures like the
existing suite.

### Phase 1 as built (2026-10-20)

- **Commands:**
  - `catalysts-sync` and `risk-calendar` (risk calendar);
  - `exposure-run` (exposure ranking and stress breadth);
  - `alerts-run` now also emits `catalyst`, `breadth` and `exposure` alerts.
- **Code and schema:** the package is `src/stock_triage/market/`. Migration `0022_market_risk_phase1.sql` adds `market_catalysts`, `exposure_scores` and `fundamental_breadth`, and rebuilds `alert_events` so `document_id` is optional (rows are kept).
- **Examples:** `config/catalysts.example.yaml` holds Fed, Bank Indonesia and MSCI dates through December 2026 with their sources. `config/index_membership.example.yaml` is an empty template, since constituents must come from the providers.
- **Dashboard:** an Exposure card in company Summary.

Departures from the design above, each for a reason found while building:

- **Filing deadlines** are read live from `filing_expectations`, not copied into `market_catalysts`, so they never go stale.
- **Breadth compares the same companies across years.** Comparing whole samples made new coverage look like spreading stress.
- **Breadth leaves out going-concern warnings for now.** That event is keyword-matched and fires on standard auditor's-report wording.
- **Breadth's default minimum is 30 matched companies.** On the current corpus only 19 can be compared, so it reports `insufficient_data` rather than a noisy level.
- **Push alerts are macOS desktop notifications,** capped at `alerts.push_max_per_run` (default 5) per run, most severe first, with the rest summarized. The first test run without a cap sent a backlog of 76 alerts at once.
- **Exposure scope:** every company with figures is scored; only watchlist companies alert. Bank leverage is reported but not scored, because deposits make banks look levered.

**Phase 1 — Exposure, calendar, delivery (existing data only)**

- `exposure_scores`, an `exposure-run` command, and the dashboard card.
- `data/catalysts.yaml` with sync, reminders, and the filing-deadline merge.
- Document-less alert kinds plus one push channel.
- Fundamental stress breadth (catalog L), computed from filings the pipeline already holds, with a market-wide and per-sector view.
- *Done when:* the watchlist is ranked with reasons, the breadth signal is computed for the existing corpus, a test proves the reminder for 2026-10-28 fires three days ahead, and a push alert arrives.

### Phase 2 as built (2026-10-20)

- **Recon:** `docs/phase2_market_recon.md`.
- **Commands:**
  - `market-sync` (IDX index and stock summaries, plus Bank Indonesia's JISDOR; `--backfill-from`, `--to`, `--refresh`);
  - `market-signals` (signals and the daily state);
  - `alerts-run` now also emits a `market_regime` alert once for each stretch of the same state, even when the first run after the change comes days later (or after a backfill).
- **Resuming:** each day is stored as it arrives, and a later run fetches only the days still missing. After three failed days in a row (the IDX page closed, or the site is down), the sync stops, exits 1, and leaves the rest for the next run.
- **Code and schema:** `market/idx_market.py`, `market/jisdor.py`, `market/sync.py` and `market/signals.py`. Migration `0023_market_series.sql` adds `market_series_daily`, a per-day `market_sync_days` ledger, `market_signal_runs` and `market_regime_days`.
- **Dashboard:** a Market State strip on Home showing the state, the three signals driving it, the next risk dates and the most exposed companies.

Departures from the design, each for a reason found while building:

- **History starts in January 2020,** because that is as far back as IDX's endpoints go, not ten years. Backtests can cover 2020, 2025 and October 2026, but not 2015 or 2018.
- **Foreign net flow is an estimate** (`flow:foreign_net_value_est`). IDX publishes foreign trades as share volumes, valued here at each stock's average traded price.
- **Sector drawdown** only has data from when the IDX-IC sector indices start. They are absent in early 2020.
- **Phase 1's stress breadth counts toward the state only from 31 March of the following year,** when that year's annual reports are due, so it never colours days before the figures were known.
- **Market calls fetch inside the IDX page without saving response bodies.** The shared helpers save each body, which would be about 0.7 GB for a backfill.
- **Golden dates wait for a backfill:** for example, 2020-03-23 (the COVID low) should read `crisis`. They belong with the phase 5 backtest.

### Phase 2b as built (2026-10-21)

- **Recon:** exchange notices in `docs/phase2_market_recon.md`. IDX serves suspensions and UMA notices at two endpoints, a rolling three years each (from 2023-10-23).
- **Code and schema:** `market/exchange_notices.py`; migration `0026_exchange_notices.sql` adds `exchange_notices`. `market-sync` fetches both lists once a day through the IDX page it already opens.
- **Signal:** `exchange_notices`, suspensions plus UMA notices over five sessions against the past year's usual (2.5x, 3.5x, 5x). It reads only after 120 sessions of history.
- **Departure:** market-wide trading halts are not in these lists; the `exchange_rules_halts` news theme covers them.

**Phase 2 — Market data and state**

- A recon spike on IDX market summaries and JISDOR, written up.
- `market-sync` with backfill, the signals, the market state, and the Market strip.
- *Done when:* at least ten years are backfilled where sources allow, the state is computed daily, and the golden dates pass.

**Phase 2b — Exchange announcements (optional)**

- A source for exchange-issued suspension, trading-halt and unusual-activity notices.
- The current disclosure source (the IDX listed-company disclosure page) doesn't include them, and the extractor has no event types for them.
- Daily counts become one more signal.

### Phase 3 as built (2026-10-21)

- **Condition kinds:** `market_regime` (`at_least`, `equals`), `market_signal` (`signal` with `operator`/`value` or `level_at_least`), `crash_risk` (`operator`/`value`) and `exposure_score` (`operator`/`value`, the thesis company's score, or with no ticker every company in scope). Code: `market/theses.py`.
- **Decision 6 taken: evaluate market theses once per run.** A thesis whose conditions are all market kinds is evaluated at the end of `market-signals` and `crash-risk`, stored in `thesis_evaluations` with no document, and alerted once per stretch of the same status. Company theses may mix in market kinds, checked when the company is derived.
- **`crash_risk` conditions count only while the estimate beats its base rate;** otherwise they are unsupported, so a thesis never breaks on a number the layer itself does not trust.
- **Alerts:** a stressed or worse state also sends one alert listing the five most fragile watchlist companies with their reasons.
- The thesis file is synced by `market-signals` too, so an edited market thesis takes effect on the next daily run.

**Phase 3 — Pre-commitment**

- The `market_regime`, `market_signal`, `crash_risk` and `exposure_score` thesis conditions.
- State-change and exposure alerts.
- *Done when:* a portfolio thesis breaks in a test when the synthetic state reaches `stressed`.

### Phase 4 as built (2026-10-21)

- **Recon:** `docs/phase4_sources_recon.md`. 17 sources in `config/watch_sources.yaml`:
  - Indonesian business news;
  - Setkab, Bank Indonesia and OJK;
  - Fitch's research sitemap;
  - BMKG, GDACS and WHO;
  - GDELT;
  - Google News, shipped disabled.
- **Commands:**
  - `watch-sync` (fetch, tag, theme activity, detectors, review items, the quarterly catalog review);
  - `watch-status`, `themes`, `findings`;
  - `finding-promote` and `finding-dismiss`;
  - `watch-replay` (what the detectors would have raised each day; stores nothing).
- **Code and schema:** `market/watch.py`, `market/themes.py`, `market/novelty.py` and `market/discovery.py`. Migration `0025_risk_discovery.sql` adds `watch_items`, `watch_source_runs`, `risk_themes`, `item_themes`, `theme_activity_daily` and `emerging_findings`.
- **Themes:** 49, seeded from every catalog "Theme" row (`config/risk_themes.yaml`), with Indonesian and English keywords. Keyword tags cite the matched words; the local LLM tags the rest and must quote the item verbatim.
- **Effects of themes:**
  - active high-severity themes join the market state as `active_themes`;
  - exposure scores gain `theme_sector` and `theme_named` components;
  - the review digest gains a market section with the state, crash risk, active themes and open findings.
- **Alerts (`emerging_theme`, `market_anomaly`):** immediate alerts are only:
  - a burst or new pairing that a high-trust source supports;
  - an unexplained IHSG move;
  - a theme promoted to high severity.

  Everything else waits in the review queue.
- **Decisions 8 and 9 taken:**
  - **News sources:** start with the feeds that allow automated reading. GDELT is a throttled wide net.
  - **LLM:** stays the configured local model.

Departures from the design, each for a reason found while building:

- **Items do not go through the document pipeline.** Feeds give headlines and short excerpts, not attachments; parse and extract are built for filings and would look for financial figures. Items live in `watch_items`, and a tag's evidence is the exact text and offset in the item (`item_themes`), not an `evidence_spans` row.
- **Done-when check, replaying archived news:**
  - Input: September–October 2026 items from the watched feeds and GDELT, loaded into a scratch copy and replayed with `watch-replay`, which only sees what was known each day.
  - Result: it raises **"standalone market" on 2026-10-16, the day of its first appearance** in a watched source, at 25 times its usual share of headlines. "downgrade" and "msci weighs" burst the same day.
  - Same replay: on 21 October it raises "karhutla" (forest fires) and the first pairings of Temasek and GIC with Indonesia, the start of the haze dispute with Singapore, which no tracked theme rated as market risk.
  - Caveat: the baseline was thin (76 headlines in the eight weeks), because GDELT refused most backfill calls.
- **Bursts compare a term's share of headlines, not its count,** against the eight weeks before. They also wait for 14 days and 60 headlines of history, and keep the 10 strongest per day. The first live run, with two days of history and raw counts, raised 300 "bursts" of everyday words. Everyday market words ("saham", "bursa", "market") never burst alone or count as names, but stay inside phrases such as "standalone market".
- **A market-wide crash is one research item.** On 16 October 2026, IHSG, foreign flows and all 11 sectors were beyond their usual range together. The item lists the others in its details, and its note named the cause from the watched items: "MSCI and FTSE classification and index reviews (19)".
- **LLM calls send `think: false`.** A reasoning model otherwise spends the time budget thinking. Calls are capped at 40 items and 180 seconds per run.
- **The model is chosen by measurement.**
  - `llm-eval` runs the labelled headlines in `config/theme_eval.yaml` through every installed model. On the M1 with 16 GB it measured, after the prompt was told that routine news gets no theme:

    | Model | Accuracy | F1 | Per headline |
    |---|---:|---:|---:|
    | `qwen3.5:9b` | 93% | 0.83 | 7.3 s |
    | `qwen2.5-coder:7b` | 62% | 0.34 | 3.1 s |
    | `rnj-1:8b` | 58% | 0.29 | 2.7 s |
    | `gemma4:e4b-it-qat`* | 76% | 0.62 | 1.6 s |
    | `qwen3.5:4b-q4_K_M`* | 78% | 0.59 | 7.4 s |

    \* Measured on Ollama 0.40.6 (these models need it), against which `qwen3.5:9b` scored 91% and F1 0.80.

  - The two small models gave no wrong themes, but they missed more than half the real risks, including every risk described without a keyword (an index downgrade with no "MSCI", a coast-guard incident at Natuna, an outbreak in China). That is the case the LLM exists for.
  - `qwen3.5:9b` uses 8.5 GB loaded on Ollama 0.20.4, which rules out 12B models on this laptop.
- **The presidential-speech theme needs an economic-policy word** next to the President's name; Setkab mentions him in nearly every item.

**Phase 4 — Risk discovery.** Can start right after phase 1, in parallel with phase 2; only 5e needs phase 2's market data.

- A spike on candidate sources (feeds, licensing, and a wide-net index such as GDELT), written up.
- The watch registry, the new source classes through the pipeline, the theme taxonomy seeded from the catalog with keyword and LLM tagging, the novelty detectors, and the review-queue flow.
- The catalog's completeness process: a quarterly review-queue item, and promoted findings added as catalog rows (catalog, "Keeping it complete").
- 5e once phase 2 exists.
- *Done when:* replaying archived news from September–October 2026 surfaces the MSCI consultation as a burst or unclassified finding within one day of its first appearance in a watched source. A test also shows a promoted finding becoming a tracked theme.
- Measure detection speed rather than "before the fall": press reports date the MSCI news to mid-October, inside the decline, so earlier warning has to come from market signals.

### Phase 5 as built (2026-10-21)

- **Commands:**
  - `crash-risk` (the estimate, its track record, episodes and analogs);
  - `market-backtest` (writes `<data_dir>/reports/market_backtest.md`, exits 1 if a golden date fails);
  - `episodes` and `episode-note` (the user's cause, lessons and themes, kept across recomputation);
  - `market-daily` (sync, signals, crash risk and alerts in one run).
- **Code and schema:** `market/forecast.py` and `market/backtest.py`. Migration `0024_market_forecast.sql` adds `crash_risk_days`, `forecast_track_record` and `market_episodes`.
- **Decision 7 taken:** a crash is a fall of 10% from a day's close within 63 sessions (configurable).
- **Golden dates** (2020-03-24, 2024-09-19, 2025-04-08, 2026-10-19) run in `market-backtest` and in `intelligence-golden` once market data exists. All four pass.
- **Calibration:** the backtest found the worst-sector signal on most days. The state now counts the share of sectors in drawdown, and a two-signal crisis needs IHSG itself 10% down. Details and the comparison are in `docs/market_calibration.md`.
- **The estimate does not beat its base rate** (walk-forward skill −6% on 1,242 days), so it is withheld everywhere and only analogs are shown, as designed. It keeps being scored daily and appears once it earns it.
- **The catalog's past episodes** are checked against IDX data, and scenario playbooks for categories A, D, E and I are in `docs/market_risk_factors.md`.
- **Dashboard:** a Market tab with:
  - IHSG with its 50- and 200-session averages over the daily state;
  - drawdown, foreign flow, the rupiah, and crash risk (only while trustworthy);
  - sectors, today's signals, the track record and analogs, themes, findings, episodes, the calendar and exchange notices.

  It is loaded from `/api/market` when the tab opens. Home's strip adds the estimate or its nearest analog, active themes and open findings.
- **Departure:** analogs compare only with episodes that had closed by each day, so the current episode is never its own analog.

**Phase 5 — History and forecasting.** Needs phase 2's backfill.

- Episodes, `market-backtest`, analogs, and the crash-risk estimate with its track record.
- The catalog's past-episodes table, checked against IDX data, plus scenario playbooks for the categories with past crashes (A, D, E, I).
- A calibration write-up in `docs/`.
- Known calibration cases from phase 2 (2025-01 to 2026-10 synced):
  - **2025-07-09 to 2025-07-16 read `crisis` while the IHSG was only about 5% below its high.** Two signals reached crisis together: foreign net selling for 15 sessions running, and IDXTECHNO 27% below its high. The sector signal takes the worst of 11 sectors, so it reaches crisis far more easily than the index does. Options to test: require the IHSG drawdown to be at least `stressed` for `crisis`, measure the share of sectors in drawdown instead of the worst one, or raise the sector thresholds.
  - **Early-2025 readings are too mild** because the window starts in January 2025 and misses the late-2024 peak. The March–April 2025 sell-off reads `stressed`; with history from 2020 it should be checked against the golden dates.
- *Done when:* the backtest report covers the past episodes; the estimate's walk-forward Brier score is reported against the base rate; and the dashboard shows analogs only if the estimate doesn't beat it.

Migrations take the next free number (currently `0027`, after
`0026_exchange_notices.sql`). Coordinate with other in-flight branches before
merging.

## Risks and open questions

1. **IDX data access** is the main risk for phase 2. The site is challenge-protected, though the repo already handles that for filings. Endpoints and history depth are unverified until the recon.
2. **News licensing and paywalls:** some outlets forbid storing full text. Keep headlines, short excerpts and links where required, and stay within personal research use (README).
3. **History for themes:** a backtest of the theme features needs news archives from past episodes, which free sources may not cover. The estimate may have to start with market features only.
4. **Look-ahead bias:** a backtest that sees revised or later data looks better than reality. All features are computed from values as known on each day.
5. **Overfitting:** few episodes. Prefer few features and simple thresholds, and publish the record honestly.
6. **LLM mistakes:** tags and notes can be wrong. Citations are required, the reviewer decides promotions, and nothing an LLM writes changes the state or the estimate directly.
7. **Alert fatigue:** news volume is high. Only high-severity findings alert; the rest go to the digest.
8. **Trading calendar and time zones:** holidays need a calendar. `source_modified_at` values are partly naive local time (WIB).
9. **False alarms:** the cooldown and step-down rule limit noise, but a stressed market will still be noisy. Alerts say so plainly.

## Decisions for review

Taken while building (2026-10-21), each reversible in config:
1 macOS notifications; 2 IHSG, the 11 IDX-IC sectors, estimated foreign flow
and JISDOR, with no commodity or global series yet; 3 every company with
figures is scored, and only the watchlist alerts; 4 the table's defaults,
then calibrated by the backtest; 5 exposure and calendar first; 6 market
theses evaluated once per run; 7 a 10% fall within 63 sessions; 8 the feeds
in `config/watch_sources.yaml`; 9 the configured local model. Still open: 10
and 11.


1. **Push channel:** Telegram bot, email, or a macOS notification?
2. **Series for phase 2:** IHSG plus which sectors, foreign flow, and USD/IDR? Commodity and global series now or later? Per-stock prices now or later?
3. **Exposure scope:** watchlist only (the default `alerts.watchlist` is empty), or all issuers with filings?
4. **Thresholds:** are the table's defaults acceptable as a starting point until the backtest exists?
5. **Phase order:** exposure and calendar first, using existing data, or market data first? Should discovery (phase 4) run in parallel with phase 2?
6. **Portfolio rules:** keep per-company evaluation with grouped alerts, or add once-per-run evaluation for `owner_scope: portfolio` theses?
7. **What counts as a crash:** a 10% or 20% IHSG drawdown, over what horizon (default: three months)?
8. **News sources:** which outlets and feeds to start with, and whether to evaluate a wide-net index such as GDELT?
9. **LLM:** keep theme tagging on the local model already configured, or allow a hosted model for better tagging? (Taken: local. `llm-eval` picked `qwen3.5:9b`, which tags 93% of the labelled headlines right on a 16 GB M1.)
10. **Catalog priorities:** each non-IDX data source is its own adapter. Which catalog categories should get market series first? A suggested order: A (global money), C (commodities), then D (domestic economy).
11. **Catalog content:** anything missing from `docs/market_risk_factors.md`, or ranked wrongly for your watchlist?

[cnbc]: https://www.cnbcindonesia.com/market/20261013093813-17-775612/ihsg-anjlok-1-ini-penyebabnya
[suara]: https://www.suara.com/bisnis/2026/10/15/165236/ihsg-makin-terpuruk-ke-level-5800-ini-penyebabnya
[bsi]: https://bsinews.id/ihsg-anjlok-15-oktober-2026
