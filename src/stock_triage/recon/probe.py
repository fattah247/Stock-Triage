"""Phase 1 probe runner.

For each public source surface listed in PRD Appendix D, fetches the page
through the polite client, inspects the response shape, and writes a
structured per-source finding. Network calls are skipped if the cache is
populated, so re-runs do not re-hit upstream.

The findings are deliberately conservative: they record whether the page is
reachable, what content-type came back, whether it looks like a JS-rendered
shell vs. server-rendered HTML, and whether common JSON fingerprints
(__NEXT_DATA__, embedded API URLs) are present.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .client import PoliteClient, RobotsBlocked

# PRD Appendix D — reference URLs.
SOURCES: list[dict[str, str]] = [
    {
        "id": "idx_company_profiles",
        "source_class": "company_universe",
        "url": "https://www.idx.co.id/en/listed-companies/company-profiles",
        "purpose": "Universe of IDX-listed issuers",
    },
    {
        "id": "idx_financial_statements_annual_report",
        "source_class": "financial_report",
        "url": "https://www.idx.co.id/en/listed-companies/financial-statements-and-annual-report",
        "purpose": "Periodic financial filings + annual reports",
    },
    {
        "id": "idx_disclosure",
        "source_class": "disclosure",
        "url": "https://www.idx.co.id/en/listed-companies/disclosure",
        "purpose": "Issuer disclosures and announcements",
    },
    {
        "id": "idx_xbrl",
        "source_class": "financial_report",
        "url": "https://www.idx.co.id/en/listed-companies/xbrl",
        "purpose": "XBRL information page (future structured ingestion)",
    },
    {
        "id": "ojk_pojk_29_2016",
        "source_class": "regulation",
        "url": "https://ojk.go.id/en/regulasi/Pages/Annual-Report-of-Issuers-or-Public-Companies-.aspx",
        "purpose": "OJK POJK 29/2016 — annual report contents",
    },
    {
        "id": "ojk_seojk_16_2021",
        "source_class": "regulation",
        "url": "https://ojk.go.id/id/regulasi/Pages/Bentuk-dan-Isi-Laporan-Tahunan--Emiten-atau-Perusahaan-Publik.aspx",
        "purpose": "SEOJK 16/2021 — form and content of annual reports",
    },
]

# Patterns we look for to learn how the page is built.
NEXT_DATA_RE = re.compile(rb'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)
NUXT_DATA_RE = re.compile(rb"window\.__NUXT__\s*=", re.DOTALL)
APP_DATA_RE = re.compile(rb'<script[^>]+id="(?:app|root|app-data)"[^>]*>', re.DOTALL)
GENERIC_API_RE = re.compile(rb"https?://[^\s\"'<>]*?/api/[^\s\"'<>]+")
SCRIPT_SRC_RE = re.compile(rb'<script[^>]+src="([^"]+)"', re.IGNORECASE)
TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)


@dataclass
class SourceFinding:
    id: str
    source_class: str
    url: str
    purpose: str
    reachable: bool
    status_code: int | None = None
    content_type: str | None = None
    elapsed_s: float | None = None
    body_size: int | None = None
    title: str | None = None
    likely_render: str = "unknown"  # server | client | static | challenge | unknown
    has_next_data: bool = False
    has_nuxt_data: bool = False
    cloudflare_challenge: bool = False
    cf_mitigated: str | None = None
    api_endpoints_seen: list[str] = field(default_factory=list)
    script_srcs: list[str] = field(default_factory=list)
    sets_cookies: list[str] = field(default_factory=list)
    robots_blocked: bool = False
    error: str | None = None
    cache_hit: bool = False
    fetched_at: str | None = None


def _classify(body: bytes, headers: dict[str, str]) -> tuple[str, dict[str, Any]]:
    """Crude render classifier. Returns (label, signals)."""
    signals: dict[str, Any] = {}
    has_next = bool(NEXT_DATA_RE.search(body))
    has_nuxt = bool(NUXT_DATA_RE.search(body))
    signals["has_next_data"] = has_next
    signals["has_nuxt_data"] = has_nuxt

    # API endpoints embedded in the HTML / JS bundle URLs.
    apis = sorted({m.decode("utf-8", "replace") for m in GENERIC_API_RE.findall(body)})
    signals["api_endpoints_seen"] = apis[:25]

    scripts = sorted({m.decode("utf-8", "replace") for m in SCRIPT_SRC_RE.findall(body)})
    signals["script_srcs"] = scripts[:25]

    title_match = TITLE_RE.search(body)
    signals["title"] = (
        title_match.group(1).decode("utf-8", "replace").strip() if title_match else None
    )

    # Heuristic: lots of script srcs + tiny body text = client-rendered shell.
    text_only_chars = len(re.sub(rb"<[^>]+>", b" ", body).split())
    signals["text_token_count"] = text_only_chars

    if has_next or has_nuxt:
        return "client_with_embedded_state", signals
    if len(scripts) > 5 and text_only_chars < 200:
        return "client_shell", signals
    if text_only_chars > 500 and len(scripts) <= 5:
        return "server_or_static", signals
    return "unknown", signals


def probe_one(client: PoliteClient, source: dict[str, str]) -> SourceFinding:
    finding = SourceFinding(
        id=source["id"],
        source_class=source["source_class"],
        url=source["url"],
        purpose=source["purpose"],
        reachable=False,
    )
    try:
        resp = client.get(source["url"])
    except RobotsBlocked as e:
        finding.robots_blocked = True
        finding.error = str(e)
        return finding
    except httpx.HTTPError as e:
        finding.error = f"{type(e).__name__}: {e}"
        return finding
    except Exception as e:  # noqa: BLE001
        finding.error = f"{type(e).__name__}: {e}"
        return finding

    finding.reachable = True
    finding.status_code = resp.status_code
    finding.content_type = resp.headers.get("content-type")
    finding.elapsed_s = round(resp.elapsed_s, 3)
    finding.cache_hit = resp.cache_hit
    finding.fetched_at = resp.fetched_at.isoformat()
    body = resp.bytes()
    finding.body_size = len(body)

    set_cookie = resp.headers.get("set-cookie")
    if set_cookie:
        # We do not store values; just names so analysts can see what the host
        # would set if a real session were established.
        finding.sets_cookies = sorted({c.split("=", 1)[0].strip() for c in set_cookie.split(",")})

    label, signals = _classify(body, resp.headers)
    finding.likely_render = label
    finding.has_next_data = signals["has_next_data"]
    finding.has_nuxt_data = signals["has_nuxt_data"]
    finding.api_endpoints_seen = signals["api_endpoints_seen"]
    finding.script_srcs = signals["script_srcs"]
    finding.title = signals["title"]

    # Cloudflare challenge detection: cf-mitigated header is conclusive,
    # body title "Just a moment..." is the visible interstitial.
    finding.cf_mitigated = resp.headers.get("cf-mitigated")
    if finding.cf_mitigated == "challenge" or (
        finding.title and "just a moment" in finding.title.lower()
    ):
        finding.cloudflare_challenge = True
        finding.likely_render = "cloudflare_challenge"

    return finding


def run_probes(
    *,
    cache_root: Path,
    user_agent: str,
    rate_limit_per_second: float = 1.0,
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    findings: list[SourceFinding] = []
    with PoliteClient(
        cache_root=cache_root,
        user_agent=user_agent,
        rate_limit_per_second=rate_limit_per_second,
        timeout_s=timeout_s,
    ) as client:
        for source in SOURCES:
            findings.append(probe_one(client, source))
    return {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "user_agent": user_agent,
        "cache_root": str(cache_root),
        "findings": [asdict(f) for f in findings],
    }


def write_findings(out_path: Path, findings: dict[str, Any]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(findings, indent=2, default=str))
