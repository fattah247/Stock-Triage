# Stock-Triage

A free, local-first system that discovers official IDX filings, stores them
with strong provenance, extracts normalized metrics and events, and produces
evidence-backed summaries using a local model.

This repository implements the **IDX Filing Analysis Automation System** PRD
(v2.0) and the v3 decision-intelligence layer on top of that baseline. It is
intended for personal research use; redistribution requires review of
source-data and model licensing.

## Status

**Phase 9 — Exchange-scale hardening** (done). On the active v3 branch,
**Phase 10 — derived intelligence foundation**, **Phase 11 — change
detection and snapshot assembly**, **Phase 12 — thesis engine and alert
upgrades**, **Phase 13 — management claim ledger**, and **Phase 14 — peer
and sector intelligence** are implemented. **Phase 15 — digests, human
review loop, and v3 KPI surfaces** is now implemented on this branch.

| Phase | Description | Status |
|-------|-------------|--------|
| 0 | Repo, config, SQLite schema, file storage | done |
| 1 | Source reconnaissance and proof of access | done |
| 2 | Universe and discovery layer | done |
| 3 | Download, versioning, file store | done |
| 4 | Parsing, OCR, chunking | done |
| 5 | Normalization, extraction, events | done |
| 6 | Rule engine and evaluation harness | done |
| 7 | Local LLM summary layer | done |
| 8 | Dashboard, alerts, canary | done |
| 9 | Exchange-scale hardening | done |

V3 preview on this branch:

| Phase | Description | Status |
|-------|-------------|--------|
| 10 | Derived intelligence foundation | done |
| 11 | Change detection and snapshot assembly | done |
| 12 | Thesis engine and alert upgrades | done |
| 13 | Management claim ledger | done |
| 14 | Peer and sector intelligence | done |
| 15 | Digests, hardening, and human review loop | done |

## Layout

```
src/stock_triage/
  config/         # YAML + env loader (pydantic-validated)
  db/             # SQLite connection, migrations, single-writer queue
  download/       # Phase 3 downloader + document/version persistence
  migrations/     # Numbered .sql migration files
  storage/        # Content-addressed file store (sha256)
  logging_setup/  # JSON logging + per-stage counters
  __main__.py     # CLI: `python -m stock_triage init`
config/default.yaml
data/             # Local-only: files/, db/, logs/ (gitignored)
tests/
```

## Quick start

Requires Python 3.11+.

```bash
# Install (uv recommended; falls back to pip)
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"

# Optional: install the browser-backed Phase 1 proof-of-access tooling
uv pip install -e ".[browser]"
python -m playwright install chromium

# Initialize directories and apply DB migrations
python -m stock_triage init

# Run the test suite
pytest -q

# Run the Phase 1 browser-backed proof-of-access flow for IDX
python scripts/run_browser_recon.py

# Sync the live company universe and issuer metadata through the warmed browser session
python -m stock_triage discover company_universe

# Discover disclosures incrementally
python -m stock_triage discover disclosure

# Discover financial-report attachment candidates incrementally. Without
# --year this scans the current and previous report year (FY Y-1 annual
# statements are filed during year Y); --year pins a single report year.
python -m stock_triage discover financial_report
python -m stock_triage discover financial_report --year 2025

# Download attachment candidates into the file store and documents table
python -m stock_triage download --source idx_financial_reports --limit 10

# Force an immediate retry of failed resources or reconsider quarantined payloads
python -m stock_triage download --source idx_disclosures --retry-failed
python -m stock_triage download --source idx_disclosures --include-quarantined

# Parse downloaded PDFs into page-aware text chunks
python -m stock_triage parse --source idx_financial_reports --limit 10

# Inspect parse-state coverage and recent failures
python -m stock_triage parse-status --source all --failure-limit 10

# Retry only parse failures
python -m stock_triage parse --source idx_financial_reports --retry-failed

# Extract normalized metrics and deterministic event signals
python -m stock_triage extract --source all --limit 10

# Inspect extraction coverage, completeness, and recent failures
python -m stock_triage extract-status --source all --failure-limit 10

# Evaluate deterministic rules over extracted facts
python -m stock_triage rules --source all --limit 10

# Inspect rule-engine coverage and recent triggered flags
python -m stock_triage rules-status --source all --trigger-limit 10

# Build curated context for a local summary prompt
python -m stock_triage summary-context <document_id>

# Validate and persist a local-model JSON output against Appendix C
python -m stock_triage summary-validate <document_id> --input-file /tmp/output.json --model-name local-model

# If a local Ollama-compatible runtime is running, generate and validate the
# summary in one step. The default local model is qwen3.5:9b, picked by
# `llm-eval` on a 16 GB M1; run `python -m stock_triage llm-eval` to compare the
# models installed on your machine.
python -m stock_triage summary-generate <document_id>

# Build the v3-derived layer: revision-aware deltas, current snapshots,
# health-state explanations, claim ledger and outcomes, thesis evaluations,
# review items, peer sets, sector rollups, peer ranks, anomaly records,
# and sector heatmap inputs.
python -m stock_triage intelligence-run --limit 25

# Build the daily digest and inspect the review queue / KPI surfaces.
python -m stock_triage digest-run --scope watchlist --limit 10
python -m stock_triage review-queue --status open --limit 20
python -m stock_triage review-update <review_item_id> --status snoozed --snooze-days 30
python -m stock_triage intelligence-kpis

# Render the local dashboard plus CSV exports
python -m stock_triage dashboard-export

# Serve the local operator dashboard so you can click actions instead of typing each command
python -m stock_triage dashboard-serve --open-browser

# Market risk, phase 1 (docs/market_risk_plan.md): risk calendar, exposure ranking,
# and stress breadth across filers. Copy config/catalysts.example.yaml to
# data/catalysts.yaml first.
python -m stock_triage catalysts-sync
python -m stock_triage risk-calendar --days 60
python -m stock_triage exposure-run

# Market risk, phase 2: daily IDX index and stock summaries plus Bank Indonesia's
# JISDOR rate (IDX history starts in 2020; a full backfill is about an hour, and
# an interrupted one resumes where it stopped), then the signals and the daily
# market state.
python -m stock_triage market-sync --backfill-from 2020-01-02
python -m stock_triage market-sync
python -m stock_triage market-signals

# Market risk, phase 5: the crash-risk estimate with its walk-forward track
# record (withheld until it beats the base rate; see docs/market_calibration.md),
# past episodes and the analogs today resembles, and a replay of the history.
python -m stock_triage crash-risk
python -m stock_triage market-backtest
python -m stock_triage episodes
python -m stock_triage episode-note episode-2026-01-29 --cause "MSCI consultation, rupiah" --themes msci_ftse_classification

# The trading-day chain in one command: sync, signals, crash risk, alerts.
python -m stock_triage market-daily

# Market risk, phase 4: watch news and official sources (config/watch_sources.yaml),
# tag risk themes (config/risk_themes.yaml), and surface what nobody tracks yet.
# Run watch-sync hourly; bursts need 14 days of headlines first. Google News is
# in the registry but disabled (its robots.txt disallows automated access); see
# docs/phase4_sources_recon.md. LLM tagging uses llm.model_name via Ollama.
python -m stock_triage watch-sync
python -m stock_triage watch-sync --source gdelt_indonesia_markets --since 2026-08-01
python -m stock_triage themes
python -m stock_triage findings
python -m stock_triage finding-promote <finding_id> --theme-id msci_standalone --title "MSCI standalone risk" --keywords "standalone market" --severity high
python -m stock_triage finding-dismiss <finding_id>
python -m stock_triage watch-replay --from 2026-09-01 --match standalone

# Evaluate watchlist alerts and market alerts (risk dates, breadth, exposure)
python -m stock_triage alerts-run

# Run and inspect source canary checks
python -m stock_triage canary-run
python -m stock_triage canary-status

# Run a bounded historical backfill and inspect backlog / SQLite throughput
python -m stock_triage backfill --source financial_report --start-year 2024 --end-year 2025 --discover-limit 50
python -m stock_triage backlog-status
python -m stock_triage benchmark-sqlite
```

For OCR fallback on scanned PDFs, install local `tesseract` plus language data.
On macOS with Homebrew:

```bash
brew install tesseract tesseract-lang
```

## Configuration

Defaults live in `config/default.yaml`. Override either by:

- Setting `STOCK_TRIAGE_CONFIG=/path/to/your.yaml`, or
- Exporting nested env vars like `STOCK_TRIAGE__LOGGING__LEVEL=DEBUG`.

The dashboard output directory, alerts JSONL, `theses.yaml`,
`company_overrides.yaml`, `catalysts.yaml`, and `index_membership.yaml` default
to locations under `paths.data_dir`, so moving `data_dir` moves them too; set
them explicitly only to split them out.

Alerts go to `alerts.channels`: `jsonl` (default) appends to
`data/alerts/alerts.jsonl`, and `macos` shows desktop notifications, at most
`alerts.push_max_per_run` per run with the rest as one summary. Market alerts
(risk dates, stress breadth, market-state changes) need no watchlist; company and exposure alerts
cover `alerts.watchlist` only.

## Running safely

- **One pipeline command at a time.** Commands that fetch from IDX or rewrite
  pipeline state (`discover`, `download`, `parse`, `extract`, `rules`,
  `intelligence-*`, `digest-run`, `alerts-run`, `canary-run`, `coverage-run`,
  `backfill`, ...) take an OS-level lock on `data/pipeline.lock`. A second one
  exits with status `75` and names the command holding the lock. Read-only
  status commands and `review-update` are never blocked. The OS releases the
  lock when a process dies, so it cannot go stale.
- **Atomic writes.** Every `WriterQueue` job runs in one SQLite transaction, so
  an interrupted run (Ctrl-C, killed process, cancelled dashboard run) leaves
  no half-rewritten chunks, coverage ledgers, or snapshots.
- **Exit codes.** `download` and `parse` exit `1` when they attempted work and
  nothing succeeded (typically IDX blocking the session); partial failures stay
  visible in the printed counts. `intelligence-run`/`intelligence-rederive`
  derive each issuer independently, print `failed_tickers=N`, list failures on
  stderr, and exit `1` only if no snapshot could be derived.
- **Dashboard runs live on the server.** `dashboard-serve` runs one action or
  sequence at a time. The Daily Sweep is executed server-side, so closing or
  reloading the tab does not stop it, and a reloaded page reattaches to the
  live output. When an IDX step fails, the remaining IDX steps are skipped but
  local steps (parse, extract, rules, intelligence, alerts, digest) still run,
  and the run finishes as `partial` with a "retry unfinished steps" option.
  Runs can be cancelled (SIGINT, then SIGTERM/SIGKILL if ignored), and finished
  runs are kept in `data/logs/dashboard_runs.jsonl`, so history survives
  restarts.
- **Local server hardening.** The dashboard only answers requests addressed to
  a loopback host name (blocking DNS rebinding), refuses cross-site
  state-changing requests, sends a strict Content-Security-Policy, and serves
  artifacts only from the file store, quarantine, recon, and dashboard
  directories. The SQLite database and the browser profile are never served.

## Filing periods and the review lifecycle

- **Interim reports are collected.** Financial-report discovery requests the
  annual (`audit`) and quarterly (`tw1`, `tw2`, `tw3`) periods for financial
  statements. Extraction labels metrics `Q1`, `H1`, `Q3`, or `Annual` and
  computes period ends from the issuer's fiscal-year-end month, including
  `data/company_overrides.yaml`, and records the reporting currency from the
  statements' "expressed in ..." headers (`EXTRACTOR_VERSION` `phase5-v3`).
  Documents extracted earlier keep their old labels until re-extracted.
- **Lateness is only claimed for periods we looked at.** A filing expectation
  is `unobserved`, not `late`, when its deadline falls before the first filing
  collected for the issuer or after the last financial-report discovery sync.
- **Decisions apply to the filing that was reviewed.** Resolving or snoozing a
  review item holds until a newer filing raises the same issue again, which
  reopens it. Snoozes expire (14 days by default; `--snooze-days` or the
  dashboard's snooze), open items whose signal disappears become `superseded`,
  and every automatic transition is written to `review_audit_log`
  (`auto_reopen`, `auto_supersede`). Grouped updates are a single transaction.
- **Comparisons are like for like.** Segment metrics are compared per segment,
  metric deltas only score against a comparable baseline and within a 10x
  plausibility bound, flow claims (revenue, profit, capex) are only judged
  against the same kind of period, peer leverage is always liabilities /
  assets, and a zero peer median never produces a critical outlier.

## Design notes

- **SQLite, single-writer.** Writes are serialized through a background
  `WriterQueue` thread; reads use independent connections. WAL mode is enabled
  by default.
- **Browser-backed IDX access.** Phase 1 now includes an operator-assisted
  Playwright session that persists browser state under `data/browser/` and
  writes proof artifacts under `data/recon/browser/`.
- **Phase 2 discovery layer.** The repository now supports live company-universe
  sync with issuer enrichment plus incremental disclosure and financial-report
  discovery backed by persisted source watermarks in SQLite.
- **Phase 3 download layer.** Download now persists concrete `documents`,
  file-level `document_versions`, and per-resource `download_resources`
  state, stores bytes in the content-addressed file store, and uses the
  warmed browser session for IDX attachment routes that block plain HTTP
  clients.
- **Retry and quarantine semantics.** Failed resources back off
  automatically, `--retry-failed` forces immediate retries, and corrupt
  payloads are quarantined under `data/quarantine/` until explicitly
  reconsidered with `--include-quarantined`.
- **Phase 4 parse layer.** Parse now supports native PDF text extraction,
  threshold-based OCR fallback routing, page-aware chunk persistence into
  `document_chunks`, section-label heuristics for heading blocks, and
  document-level parse state updates plus operator-visible status/retry
  commands via
  `python -m stock_triage parse ...`.
- **Phase 5 extraction layer.** Extraction now normalizes fiscal periods,
  currencies, units, and language variants into `metric_items`, records
  deterministic disclosure and filing-driven events into `events`, persists
  evidence spans for every extracted fact, and exposes operator-visible KPI
  reporting via `python -m stock_triage extract-status ...`.
- **Phase 6 rule layer.** Rules now run deterministically over normalized
  metrics and event detections, persist replayable rows into `rule_runs`,
  update documents into the `analyzed` state, and expose coverage plus recent
  trigger reporting via `python -m stock_triage rules-status ...`. Current
  baseline audit notes live in `docs/phase6_rule_audit.md`.
- **Phase 7 summary layer.** The repository now has an Appendix C output
  schema, curated summary-context assembly from metrics/rules/evidence, a
  validation gate that persists checked outputs into `analysis_runs`, and a
  live Ollama-backed local generation path that defaults to `gemma2:9b`. The
  generation flow uses deterministic evidence-alias expansion before final
  validation so the stored output retains valid DB-backed evidence links.
- **Phase 8 daily operations.** The repository now exports a local HTML
  dashboard and a live local dashboard server. The live view adds action
  buttons for discovery, download, parse, extract, rules, alerts, canary, and
  summary generation; local attachment viewing; issuer search; latest
  documents; metric snapshots; revision history; evidence-linked findings;
  watchlist alerts into a local JSONL channel with persisted dedupe; and
  persisted canary health snapshots for source drift and source failures
  instead of crashing on blocked checks.
- **Phase 9 hardening bootstrap.** The repository now has bounded backfill
  orchestration, per-source backlog reporting, and a SQLite writer benchmark so
  scale decisions can be made from measured queue latency and throughput instead
  of guesswork. Backfill now also bounds discovery persistence by default so a
  historical window does not automatically flood the active candidate queue.
  The operating checklist lives in
  `docs/phase9_operational_playbook.md`.
- **V3 Phase 10-11 derived intelligence.** The repository now includes a
  post-analysis derived layer that computes revision-aware and filing-to-filing
  deltas, assembles deterministic company snapshots with health-state
  explanation, persists review items for revised filings, and builds company
  timeseries, peer sets, sector rollups, and decision briefs through
  `python -m stock_triage intelligence-run ...`.
- **V3 Phase 12 thesis engine.** `python -m stock_triage intelligence-run ...`
  now also syncs local thesis definitions from `data/theses.yaml`, persists
  `thesis_definitions`, `thesis_conditions`, and `thesis_evaluations`, updates
  snapshot decision state and watchlist priority, and upgrades alerts so broken,
  weakening, or improving theses can surface as evidence-backed local signals.
- **V3 Phase 13 claim ledger.** `python -m stock_triage intelligence-run ...`
  now also extracts deterministic management claims from narrative chunks,
  persists `management_claims`, evaluates those claims into
  `claim_outcomes` when later evidence exists, and exposes credibility summary
  state such as achieved rate, contradicted rate, unresolved rate, and recycled
  claim count inside the current company snapshot.
- **V3 Phase 14 peer and sector intelligence.** `python -m stock_triage intelligence-run ...`
  now also builds comparability-aware peer sets, ranks each company against
  its sector or subsector on normalized fields such as revenue growth, margins,
  leverage, dilution frequency, governance-change frequency, and event density,
  persists richer `sector_rollups`, and exposes comparability warnings, peer
  anomalies, and sector heatmap inputs inside the current snapshot and brief.
- **V3 Phase 15 review loop and digest.** The repository now persists
  `daily_digests`, supports operator review-state transitions plus audit
  history through `review_queue_items` and `review_audit_log`, exposes
  `python -m stock_triage digest-run ...`, `review-queue`, `review-update`,
  and `intelligence-kpis`, and surfaces the digest, open review queue, and
  derived KPI snapshot in the dashboard.

Example local thesis file (`data/theses.yaml`):

```yaml
theses:
  - thesis_id: tamu-balance-sheet
    ticker: TAMU
    thesis_name: TAMU balance-sheet repair
    thesis_text: TAMU should improve revenue while avoiding debt-stress stop rules.
    owner_scope: watchlist
    status: active
    review_cadence: after_each_filing
    conditions:
      - severity: positive
        kind: metric_delta
        metric_name: revenue
        operator: gte
        value: 20
      - severity: warning
        kind: event_present
        event_type: debt_refinancing
      - severity: stop
        kind: rule_present
        rule_name: debt_surge
```

A thesis whose conditions are all market kinds is checked once per market day
(after `market-signals` and `crash-risk`) and alerts once when its status
changes. Written in calm times, it fires when the market breaks:

```yaml
theses:
  - thesis_id: watchlist-market-guard
    thesis_name: Review the watchlist when the market breaks
    thesis_text: Rules written in calm times.
    owner_scope: portfolio
    conditions:
      - severity: stop
        kind: market_regime
        at_least: stressed
      - severity: stop
        kind: crash_risk          # counts only while the estimate beats its base rate
        operator: gte
        value: 0.4
      - severity: warning
        kind: market_signal
        signal: foreign_sell_streak
        operator: gte
        value: 10
      - severity: warning
        kind: exposure_score      # any watchlist company at 70 or more
        operator: gte
        value: 70
```
- **Content-addressed storage.** Attachment bytes are stored under
  `data/files/<sha[:2]>/<sha[2:4]>/<sha>`. Identical bytes deduplicate; the DB
  tracks revision/relisting semantics.
- **Provenance first.** Every metric and finding will reference one or more
  `evidence_spans` rows (chunk, page range, char offsets). Summaries that
  cannot cite evidence will be rejected by validation.
- **Rules vs. LLM separation.** Deterministic rule outputs are versioned and
  authoritative. The LLM consumes normalized facts + curated excerpts only.

## License & scope

Personal use only at this stage. Public-document ingestion does not grant
redistribution rights for packaged datasets or model bundles.
