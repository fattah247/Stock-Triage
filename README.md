# Stock-Triage

A free, local-first system that discovers official IDX filings, stores them
with strong provenance, extracts normalized metrics and events, and produces
evidence-backed summaries using a local model.

This repository implements the **IDX Filing Analysis Automation System** PRD
(v2.0). It is intended for personal research use; redistribution requires
review of source-data and model licensing.

## Status

**Phase 4 — Parsing, OCR, and chunking** (in progress).

| Phase | Description | Status |
|-------|-------------|--------|
| 0 | Repo, config, SQLite schema, file storage | done |
| 1 | Source reconnaissance and proof of access | done |
| 2 | Universe and discovery layer | done |
| 3 | Download, versioning, file store | done |
| 4 | Parsing, OCR, chunking | in progress |
| 5 | Normalization, extraction, events | pending |
| 6 | Rule engine and evaluation harness | pending |
| 7 | Local LLM summary layer | pending |
| 8 | Dashboard, alerts, canary | pending |
| 9 | Exchange-scale hardening | pending |

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
- **Phase 4 bootstrap.** Parse now supports native PDF text extraction,
  threshold-based OCR fallback routing, page-aware chunk persistence into
  `document_chunks`, and document-level parse state updates via
  `python -m stock_triage parse ...`.
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
