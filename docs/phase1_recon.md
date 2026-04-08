# Phase 1 — Source Reconnaissance Findings

**Date:** 2026-04-09
**Branch:** `phase-1-discovery`
**Probe runner:** `scripts/run_recon.py`
**Browser probe runner:** `scripts/run_browser_recon.py`
**Raw data:** `data/recon/` (gitignored)
**Structured findings:** `data/recon/findings.json`
**Browser artifacts:** `data/recon/browser/` + `data/recon/browser_findings.json`

This document satisfies PRD §11 Phase 1 exit criteria:

> - At least one reliable discovery path exists per source class
> - Discovery metadata can be serialized as `DocumentCandidate`
> - Collector failure modes are documented

## Method

A polite reconnaissance HTTP client (`src/stock_triage/recon/client.py`) was
used to probe each public surface listed in PRD Appendix D. The client:

- Sends a single identifying `User-Agent` (`stock-triage/0.1 (+local research)`).
- Rate-limits to 1 req/s.
- Honors `robots.txt` per host.
- Persists every raw response to `data/recon/<host>/<sha>.{bin,meta.json}`
  so re-analysis is possible without re-hitting upstream.
- Performs `GET` only. No cookies are sent unless explicitly supplied.

The probe runner classifies each response by content type, render strategy,
embedded API hints, and Cloudflare mitigation headers.

A confirmation re-fetch was also performed with full browser-style headers
(realistic Chrome `User-Agent`, `Sec-CH-UA-*`, `Sec-Fetch-*`, etc.) to rule
out simple UA filtering.

## Per-source findings

### IDX surfaces — all blocked by Cloudflare managed challenge

| Source | URL | Status | Outcome |
|---|---|---|---|
| `idx_company_profiles` | `…/listed-companies/company-profiles` | 403 | CF challenge |
| `idx_financial_statements_annual_report` | `…/listed-companies/financial-statements-and-annual-report` | 403 | CF challenge |
| `idx_disclosure` | `…/listed-companies/disclosure` | 403 | CF challenge |
| `idx_xbrl` | `…/listed-companies/xbrl` | 403 | CF challenge |

**Symptom.** Every request to `www.idx.co.id` returns HTTP 403 with
`server: cloudflare`, `cf-mitigated: challenge`, and a body whose `<title>`
is `Just a moment...` — the standard Cloudflare interstitial that requires
JavaScript execution and a `cf_clearance` cookie before the real content is
served.

**UA confirmation.** Re-running with a realistic Chrome 124 macOS profile
(matching `Sec-CH-UA`, `Sec-Fetch-*`, `Accept-Language`, `Upgrade-Insecure-
Requests`) produced the same `cf-mitigated: challenge` response. The wall is
*not* a simple bot-UA filter; it is a managed challenge that requires actual
browser JS execution.

**Implication for v1.** Endpoint-backed discovery from a plain Python HTTP
client is **not viable** for IDX surfaces. The PRD anticipated this in §3.3
("the system should prefer endpoint-backed discovery if stable, but it must
retain a fallback path using browser automation or HTML scraping for
resilience"). For v1, IDX discovery must go through one of these paths:

1. **Browser automation (recommended).** Use Playwright (Chromium) to drive
   the real page, let Cloudflare's challenge resolve, then either:
   - Extract `cf_clearance` + `__cf_bm` cookies and pass them to the polite
     HTTP client for subsequent requests until they expire (~30 min for
     `cf_clearance`, longer for `__cf_bm`), or
   - Stay inside Playwright for the entire IDX session and intercept the
     XHR responses the SPA makes — this is the most stable approach because
     it sidesteps the challenge entirely after the initial page load.
2. **User-supplied session cookies.** Have the operator open the page in a
   real browser once per session and export `cf_clearance` + `__cf_bm` for
   the polite client. Lower dependency cost, higher operator burden.
3. **Manual HTML capture.** As a last resort, the operator saves HTML/JSON
   from a real browser and feeds it to the parser. Not viable for daily
   operation but useful for the v1 golden set.

Browser automation is the only path that satisfies "discovery runs are
incremental" (PRD §11 Phase 2 exit criterion) without operator-in-the-loop.

**Implementation landed.** The repository now includes an optional browser
extra, `scripts/run_browser_recon.py`, and
`src/stock_triage/recon/browser.py`, which provide the Playwright-backed
session helper originally proposed here.

**Browser-session path.** Install the optional browser extra
`pip install -e ".[browser]"`, install Chromium via Playwright, and run the
browser probe. It uses a persistent browser profile under `data/browser/`
and writes HTML, storage state, and captured XHR/fetch responses under
`data/recon/browser/`.

The helper:

- Boots Chromium with reasonable stealth settings.
- Navigates to a warm-up IDX page.
- Waits for the CF challenge to resolve (`page.wait_for_url` until the real
  content selector appears).
- Exposes a `request_context` that can be used either to capture XHRs or to
  hand cookies back to the polite HTTP client.

The dependency remains opt-in because Playwright is heavy and expands the
project's operational footprint.

### OJK surfaces — fully reachable

| Source | URL | Status | Render | Notes |
|---|---|---|---|---|
| `ojk_pojk_29_2016` | `…/regulasi/Pages/Annual-Report-of-Issuers-or-Public-Companies-.aspx` | 200 | server-rendered HTML | Direct PDF link to POJK 29/2016 (English) |
| `ojk_seojk_16_2021` | `…/regulasi/Pages/Bentuk-dan-Isi-Laporan-Tahunan…aspx` | 200 | server-rendered HTML | Direct PDF links to SEOJK 16/2021 + summary |

OJK serves classic SharePoint-style server-rendered pages with stable PDF
links. The polite client retrieves both pages cleanly and the regulation
PDFs are extractable with a single follow-up request each. This satisfies
Phase 1 for the **regulation** source class — at least one reliable path
exists, and a `DocumentCandidate` can be constructed today.

PDF links observed (relative to `https://ojk.go.id`):

- `/en/regulasi/Documents/.../4. POJK 29 year 2016 - English.pdf`
- `/id/regulasi/Documents/.../SEOJK - 16 - 2021.pdf`
- `/id/regulasi/Documents/.../Ringkasan SEOJK - 16 - 2021.pdf`

These are **not** issuer filings; they are the regulatory reference texts
the rule engine and parser will be measured against. They belong to the
project as ground-truth references, not as primary v1 data.

## Source-class coverage summary

| Source class | Reliable path? | How |
|---|---|---|
| `company_universe` | **Yes** | Browser-backed probe via Playwright on `/listed-companies/company-profiles` |
| `financial_report` | **Yes** | Browser-backed probe via Playwright on `/listed-companies/financial-statements-and-annual-report` |
| `disclosure` | **Yes** | Browser-backed probe via Playwright on `/listed-companies/disclosure` |
| `regulation` (reference, not in PRD §6.2 contract) | **Yes** | Polite HTTP client against OJK pages |

Three of four PRD-required source classes require browser automation. That is
still the key Phase 1 finding, but the repository now includes the browser
bootstrap path needed to carry this into Phase 2.

## Collector failure modes

| Failure | How it manifests | Mitigation |
|---|---|---|
| Cloudflare challenge | HTTP 403, `cf-mitigated: challenge`, title "Just a moment..." | Detected by `probe.py`; collectors must route through Playwright |
| Cookie expiry (`cf_clearance`) | Sudden 403s mid-session after working requests | Refresh via Playwright on 403; backoff |
| `__cf_bm` rotation | Same as above, more frequent | Same |
| OJK SharePoint occasional 5xx | HTTP 500/503 with HTML error page | Standard exponential backoff |
| Page-structure drift | PDF link selector returns 0 results | Canary monitor (PRD §6.11) compares against last-known link count |
| Robots.txt change | New disallow rule for our UA | Probe re-checks robots per session; fails closed |
| TLS / DNS / network | `httpx.HTTPError` | Recorded in `findings.json` `error` field; retried by upstream caller |

## What the next phase needs from this one

Phase 2 can now proceed on top of the browser-session artifacts and persistent
profile introduced in Phase 1. The next step is to extract stable
`DocumentCandidate` records from the IDX browser session responses instead of
stopping at proof-of-access.

## Artifacts produced in Phase 1

- `src/stock_triage/sources/types.py` — `DocumentCandidate`, `DiscoveryWindow`, `SourceAdapter` Protocol, `SourceClass` enum.
- `src/stock_triage/sources/adapters/` — stub adapters for the three v1 source classes; each raises `NotImplementedError` with a pointer to this document.
- `src/stock_triage/recon/client.py` — `PoliteClient` (rate limit, robots, cache).
- `src/stock_triage/recon/probe.py` — probe runner with CF detection.
- `src/stock_triage/recon/browser.py` — `PlaywrightSession`, Cloudflare block detection, and browser artifact persistence.
- `scripts/run_recon.py` — operator entrypoint.
- `scripts/run_browser_recon.py` — browser-backed operator entrypoint for IDX surfaces.
- `data/recon/findings.json` — structured findings (re-runnable).
- `data/recon/browser_findings.json` — browser-backed proof-of-access findings.
- `tests/test_document_candidate.py` — round-trip test for the candidate type.
