# Stock-Triage

A free, local-first system that discovers official IDX filings, stores them
with strong provenance, extracts normalized metrics and events, and produces
evidence-backed summaries using a local model.

This repository implements the **IDX Filing Analysis Automation System** PRD
(v2.0). It is intended for personal research use; redistribution requires
review of source-data and model licensing.

## Status

**Phase 2 — Universe and discovery bootstrap** (in progress).

| Phase | Description | Status |
|-------|-------------|--------|
| 0 | Repo, config, SQLite schema, file storage | done |
| 1 | Source reconnaissance and proof of access | done |
| 2 | Universe and discovery layer | in progress |
| 3 | Download, versioning, file store | pending |
| 4 | Parsing, OCR, chunking | pending |
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

# Persist the company universe from browser artifacts
python -m stock_triage discover company_universe

# Persist disclosures from browser artifacts
python -m stock_triage discover disclosure

# Persist financial-report attachment candidates through the warmed browser session
python -m stock_triage discover financial_report --year 2025
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
- **Phase 2 bootstrap.** The initial discovery layer can already persist the
  company universe, disclosures, financial-report attachment candidates, and
  source watermarks into SQLite.
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
