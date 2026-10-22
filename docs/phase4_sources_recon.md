# Phase 4 — Watched Sources Reconnaissance

**Date:** 2026-10-21
**Method:** plain HTTP with the project's user agent (`stock-triage/0.1 (+local research)`), at least two seconds between calls to one host, robots.txt read first. Nothing was fetched through a browser.
**Result:** `config/watch_sources.yaml`, the registry `watch-sync` reads.

## What works, what doesn't

| Source | How | Fields | Per fetch | Notes |
|---|---|---|---|---|
| CNBC Indonesia (`/market/rss`) | RSS | date, title, ~126-char excerpt | 100 (~32 h) | robots allows |
| Kontan investasi (`investasi.kontan.co.id/rss`) | RSS | date, title, excerpt | 25 (~4.5 h) | Poll at least hourly |
| Detik bursa dan valas | RSS | date, title, excerpt | 100 (~5 days) | One timeout seen |
| Antara ekonomi-bursa and -finansial | RSS | date, title, excerpt cut at ~120 chars | 20 each | `ekonomi.xml` is stale |
| Katadata (`/rss`) | RSS | date, title, excerpt | 25 (~3 h) | Declares iso-8859-1 but is UTF-8; parsed as bytes |
| CNN Indonesia ekonomi, Bloomberg Technoz | RSS | date, title, excerpt | 100 each | |
| Setkab (`setkab.go.id/feed/`) | RSS | date, title, ~600-char description | 10 (~1 day) | Its firewall blocks paging and robots.txt; read page 1 only |
| Bank Indonesia press releases | HTML list | title, link (`sp_<n>.aspx`) | 10 | No RSS; no challenge |
| OJK press releases | HTML list | title, link | 10 | Slow, first try timed out at 30 s |
| Fitch research sitemap | XML sitemap | slug with title and date, lastmod | 501 latest | Filter for "indonesia"; the search page is robots-disallowed |
| BMKG earthquakes | JSON | time, magnitude, area, tsunami potential | 15 | Kept from magnitude 6 |
| GDACS | RSS | alert colour, country | ~260 | Kept for Indonesia, orange and red |
| WHO Disease Outbreak News | OData JSON | time, title, link | 20 | Needs `$orderby` |
| GDELT DOC 2.0 | JSON API | time (15 min), title, domain, language | up to 250 | Titles only; heavy throttling, see below |
| Google News search | RSS | time, title, outlet | ~100 | **Disabled in the registry**, see below |
| Kemenkeu press releases | undocumented JSON | full text | 9 | Found in the site's JS bundle; too fragile to ship |
| Bisnis.com, Kompas, IDN Financials, Investor.id | — | — | — | Cloudflare/CloudFront blocks, or robots/terms forbid automated access |
| presidenri.go.id, S&P, Moody's, IDX news | — | — | — | Cloudflare or JavaScript-only |
| MSCI, FTSE Russell | — | — | — | Content pages challenge bots. MSCI's sitemap did not list the October consultation; FTSE's update PDF can be watched by its Last-Modified header (not shipped) |
| BPS | API | — | — | Needs a registered key; deferred |

Every headline from a blocked outlet still reaches the layer when GDELT indexes it.

## GDELT

- **Query syntax:** space is AND, `OR` needs parentheses, phrases go in quotes. `startdatetime`/`enddatetime` are `YYYYMMDDHHMMSS`; otherwise `timespan`. The maximum is 250 records.
- **Matching:** GDELT matches the full article text, so a broad query fills with incidental mentions. The shipped query names IHSG and Indonesian stocks explicitly.
- **History:** it searches well past the documented three months, but coverage thins out.
- **Throttling:** HTTP 429 with the text "Please limit requests to one every 5 seconds", and no `Retry-After`. In the recon only about 5 of 45 calls succeeded, even at one call a minute.
  - So `watch-sync` treats a refusal as a soft failure: a past window retries a refused day once after 30 seconds, then skips it.
  - GDELT is for cross-checks and backfills, not for timely alerts.

## How the MSCI consultation first appeared

- **MSCI's own release** ("MSCI Launches a Consultation on a Proposal to Reclassify Indonesia from Emerging Market to Standalone Market Status") is dated **2026-10-15 21:46 UTC** in Google News. Wire coverage started about an hour earlier (MarketScreener 20:55 UTC, Bloomberg 20:59 UTC).
- **Indonesian outlets:** Bisnis at 22:55 UTC and CNBC Indonesia at 23:38 UTC, both before Jakarta's open on 16 October.
- **GDELT's first on-topic article** is Antara's at **2026-10-16 01:00 UTC**, about four hours after MSCI. It indexed 65 articles that day and none the day before.
- **Earlier signs that were public:** MSCI's free-float update for Indonesian securities (15 June), its note on Indonesia and Turkey (7 July), and the August review, which kept Indonesia at Emerging.

Replaying those days with `watch-replay` (watched feeds plus GDELT's archive
for August–October) raises "standalone market" as a burst on 2026-10-16, the
day it first appears in a watched source.

So a watched Indonesian feed carried the story before Jakarta opened. The
plan's target, a burst within a day of the first appearance, is reachable,
but not before the market: IDX opened on 16 October already falling. That
supports the plan's point that earlier warning has to come from market
signals; those had read stressed since February.

## Decisions

- **Google News is in the registry, disabled.** It was the fastest route to the MSCI story and the only route to some blocked sites. But its robots.txt disallows automated access, and its feed terms allow personal, non-commercial feed readers only. Enabling it is the user's call.
- **Trust:** official bodies, the rating agency and hazard feeds are `high`; news and Setkab (official, but mostly ceremonial) are `medium`. High trust matters in two places: an unclassified high-trust item is queued for review, and a burst or new pairing alerts only when a high-trust item supports it.
- **Excerpts:** at most 280 characters are kept, even where a feed gives full text (Setkab, Kemenkeu).
- **Not verified:** GDELT's `sourcecountry:`/`sourcelang:` operators, Bank Indonesia and OJK paging, BPS with a key, FTSE's robots.txt.
