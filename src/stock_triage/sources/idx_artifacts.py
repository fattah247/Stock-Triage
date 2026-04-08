"""IDX browser-artifact parsers for Phase 2 discovery bootstrap.

These helpers parse the browser artifacts produced by `run_browser_recon.py`.
They let Phase 2 build on the already-proven browser access path without
re-solving Cloudflare for every unit test.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .types import DocumentCandidate, SourceClass

JAKARTA_TZ = timezone(timedelta(hours=7))

_COMPANY_OBJECT_RE = re.compile(
    r'\{[^{}]*?KodeEmiten:"(?P<ticker>[^"]+)"'
    r'[^{}]*?NamaEmiten:"(?P<name>[^"]+)"'
    r'(?P<body>[^{}]*?)\}',
    re.DOTALL,
)
_DATE_FIELD_RE = re.compile(r'TanggalPencatatan:"(?P<listing_date>[^"]+)"')
_WEBSITE_FIELD_RE = re.compile(r'Website:"(?P<website>[^"]*)"')
_EMAIL_FIELD_RE = re.compile(r'Email:"(?P<email>[^"]*)"')
_PHONE_FIELD_RE = re.compile(r'Telepon:"(?P<phone>[^"]*)"')
_ATTACH_CARD_SPLIT_RE = re.compile(r'<div[^>]+class="attach-card[^"]*"[^>]*>', re.IGNORECASE)
_TIME_RE = re.compile(r"<time[^>]*>(?P<ts>.*?)</time>", re.DOTALL | re.IGNORECASE)
_TITLE_RE = re.compile(
    r'<h6[^>]*class="[^"]*title[^"]*"[^>]*>\s*<a[^>]+href="(?P<url>[^"]+)"[^>]*>'
    r'\s*(?P<title>.*?)\[\s*<span[^>]*>(?P<ticker>.*?)</span>',
    re.DOTALL | re.IGNORECASE,
)
_ATTACHMENT_RE = re.compile(
    r'<a[^>]+href="(?P<url>https://www\.idx\.co\.id/StaticData/NewsAndAnnouncement[^"]+)"'
    r'[^>]+download="(?P<filename>[^"]+)"',
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")


def load_artifact_text(*, artifacts_root: Path, source_id: str, filename: str = "page.html") -> str:
    path = artifacts_root / source_id / filename
    return path.read_text(encoding="utf-8", errors="replace")


def parse_company_profiles_html(html_text: str) -> list[DocumentCandidate]:
    candidates: list[DocumentCandidate] = []
    seen: set[str] = set()

    for match in _COMPANY_OBJECT_RE.finditer(html_text):
        ticker = _decode_js_string(match.group("ticker")).strip()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)

        name = _decode_js_string(match.group("name")).strip()
        body = match.group("body")
        listing_date = _extract_optional(body, _DATE_FIELD_RE)
        website = _extract_optional(body, _WEBSITE_FIELD_RE)
        email = _extract_optional(body, _EMAIL_FIELD_RE)
        phone = _extract_optional(body, _PHONE_FIELD_RE)

        raw_metadata = {
            "issuer_name": name,
            "profile_url": f"https://www.idx.co.id/en/listed-companies/company-profiles/{ticker}",
            "listing_date": listing_date,
            "website": website,
            "email": email,
            "phone": phone,
        }
        candidates.append(
            DocumentCandidate(
                source_name="idx_company_universe",
                source_class=SourceClass.COMPANY_UNIVERSE,
                source_record_id=ticker,
                title=name,
                ticker=ticker,
                raw_metadata={k: v for k, v in raw_metadata.items() if v},
            )
        )

    return candidates


def parse_disclosure_html(html_text: str) -> list[DocumentCandidate]:
    chunks = _ATTACH_CARD_SPLIT_RE.split(html_text)[1:]
    candidates: list[DocumentCandidate] = []

    for chunk in chunks:
        title_match = _TITLE_RE.search(chunk)
        if not title_match:
            continue

        published_at = _parse_disclosure_timestamp(_clean_text(_TIME_RE.search(chunk).group("ts"))) if _TIME_RE.search(chunk) else None
        title = _clean_text(title_match.group("title"))
        ticker = _clean_text(title_match.group("ticker"))
        main_url = html.unescape(title_match.group("url"))
        attachments = [
            {
                "url": html.unescape(m.group("url")),
                "filename": html.unescape(m.group("filename")),
            }
            for m in _ATTACHMENT_RE.finditer(chunk)
        ]

        record_id = _stable_id(main_url)
        candidates.append(
            DocumentCandidate(
                source_name="idx_disclosures",
                source_class=SourceClass.DISCLOSURE,
                source_record_id=record_id,
                title=title,
                ticker=ticker or None,
                upstream_published_at=published_at,
                attachment_url=main_url,
                attachment_filename=Path(main_url).name,
                raw_metadata={"attachments": attachments},
            )
        )

    return candidates


def parse_financial_report_json(payload: dict) -> list[DocumentCandidate]:
    candidates: list[DocumentCandidate] = []
    for result in payload.get("Results", []):
        ticker = (result.get("KodeEmiten") or "").strip() or None
        issuer_name = result.get("NamaEmiten")
        report_year = result.get("Report_Year")
        report_period = result.get("Report_Period")

        for attachment in result.get("Attachments", []):
            file_id = attachment.get("File_ID")
            file_path = attachment.get("File_Path")
            file_name = attachment.get("File_Name")
            if not file_id or not file_path:
                continue
            attachment_url = _absolute_idx_url(file_path)
            modified_at = _parse_iso_datetime(attachment.get("File_Modified"))
            raw_metadata = {
                "issuer_name": issuer_name,
                "report_year": report_year,
                "report_period": report_period,
                "report_type": attachment.get("Report_Type"),
                "file_size": attachment.get("File_Size"),
                "file_type": attachment.get("File_Type"),
                "file_modified": attachment.get("File_Modified"),
                "file_path": file_path,
            }
            candidates.append(
                DocumentCandidate(
                    source_name="idx_financial_reports",
                    source_class=SourceClass.FINANCIAL_REPORT,
                    source_record_id=file_id,
                    title=file_name or f"{issuer_name or ticker} {report_year} {report_period}",
                    ticker=ticker,
                    upstream_published_at=modified_at,
                    attachment_url=attachment_url,
                    attachment_filename=file_name,
                    raw_metadata={k: v for k, v in raw_metadata.items() if v is not None},
                )
            )
    return candidates


def load_json_artifact(*, artifacts_root: Path, source_id: str, filename: str) -> dict:
    path = artifacts_root / source_id / filename
    return json.loads(path.read_text(encoding="utf-8"))


def _extract_optional(body: str, pattern: re.Pattern[str]) -> str | None:
    match = pattern.search(body)
    if not match:
        return None
    value = _decode_js_string(match.group(1)).strip()
    return value or None


def _decode_js_string(raw: str) -> str:
    return html.unescape(raw.encode("utf-8").decode("unicode_escape"))


def _clean_text(raw: str) -> str:
    text = _TAG_RE.sub(" ", raw)
    return " ".join(html.unescape(text).split())


def _parse_disclosure_timestamp(raw: str) -> datetime | None:
    try:
        return datetime.strptime(raw, "%d %B %Y %H:%M:%S").replace(tzinfo=JAKARTA_TZ)
    except ValueError:
        return None


def _stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _parse_iso_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=JAKARTA_TZ)
    except ValueError:
        return None


def _absolute_idx_url(path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if not path.startswith("/"):
        path = "/" + path
    return "https://www.idx.co.id" + path
