# Stock-Triage

A free, local-first system that discovers official IDX filings, stores them
with strong provenance, extracts normalized metrics and events, and produces
evidence-backed summaries using a local model.

This repository implements the **IDX Filing Analysis Automation System** PRD
(v2.0). It is intended for personal research use; redistribution requires
review of source-data and model licensing.

## Status

**Phase 0 — Foundations and repo setup** (complete).

| Phase | Description | Status |
|-------|-------------|--------|
| 0 | Repo, config, SQLite schema, file storage | done |
| 1 | Source reconnaissance and proof of access | pending |
| 2 | Universe and discovery layer | pending |
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

# Initialize directories and apply DB migrations
python -m stock_triage init

# Run the Phase 0 smoke test
pytest -q
```

## Configuration

Defaults live in `config/default.yaml`. Override either by:

- Setting `STOCK_TRIAGE_CONFIG=/path/to/your.yaml`, or
- Exporting nested env vars like `STOCK_TRIAGE__LOGGING__LEVEL=DEBUG`.

## Design notes

- **SQLite, single-writer.** Writes are serialized through a background
  `WriterQueue` thread; reads use independent connections. WAL mode is enabled
  by default.
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
