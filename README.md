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
**Phase 10 — derived intelligence foundation** and **Phase 11 — change
detection and snapshot assembly** are implemented.

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

# Discover financial-report attachment candidates incrementally
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
# summary in one step. The default local model is gemma2:9b.
python -m stock_triage summary-generate <document_id>

# Build the v3-derived layer: revision-aware deltas, current snapshots,
# health-state explanations, review items, peer sets, and sector rollups.
python -m stock_triage intelligence-run --limit 25

# Render the local dashboard plus CSV exports
python -m stock_triage dashboard-export

# Serve the local operator dashboard so you can click actions instead of typing each command
python -m stock_triage dashboard-serve --open-browser

# Evaluate watchlist alerts from local config
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
