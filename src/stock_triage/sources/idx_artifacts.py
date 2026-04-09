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
from typing import Any, Iterable

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
_FAX_FIELD_RE = re.compile(r'Fax:"(?P<fax>[^"]*)"')
_ADDRESS_FIELD_RE = re.compile(r'Alamat:"(?P<address>[^"]*)"')
_BUSINESS_ACTIVITY_FIELD_RE = re.compile(r'KegiatanUsahaUtama:"(?P<business_activity>[^"]*)"')
_NPWP_FIELD_RE = re.compile(r'NPWP:"(?P<npwp>[^"]*)"')
_NPKP_FIELD_RE = re.compile(r'NPKP:"(?P<npkp>[^"]*)"')
_LOGO_FIELD_RE = re.compile(r'Logo:"(?P<logo>[^"]*)"')
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
    rows: list[dict[str, Any]] = []
    for match in _COMPANY_OBJECT_RE.finditer(html_text):
        rows.append(
            {
                "KodeEmiten": _decode_js_string(match.group("ticker")).strip(),
                "NamaEmiten": _decode_js_string(match.group("name")).strip(),
                "TanggalPencatatan": _extract_optional(match.group("body"), _DATE_FIELD_RE),
                "Website": _extract_optional(match.group("body"), _WEBSITE_FIELD_RE),
                "Email": _extract_optional(match.group("body"), _EMAIL_FIELD_RE),
                "Telepon": _extract_optional(match.group("body"), _PHONE_FIELD_RE),
                "Fax": _extract_optional(match.group("body"), _FAX_FIELD_RE),
                "Alamat": _extract_optional(match.group("body"), _ADDRESS_FIELD_RE),
                "KegiatanUsahaUtama": _extract_optional(match.group("body"), _BUSINESS_ACTIVITY_FIELD_RE),
                "NPWP": _extract_optional(match.group("body"), _NPWP_FIELD_RE),
                "NPKP": _extract_optional(match.group("body"), _NPKP_FIELD_RE),
                "Logo": _extract_optional(match.group("body"), _LOGO_FIELD_RE),
            }
        )
    return parse_company_profile_rows(rows)


def parse_company_profile_rows(rows: Iterable[dict[str, Any]]) -> list[DocumentCandidate]:
    candidates: list[DocumentCandidate] = []
    seen: set[str] = set()

    for row in rows:
        ticker = _string_or_none(row.get("KodeEmiten"))
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)

        name = _string_or_none(row.get("NamaEmiten")) or ticker
        raw_metadata = {
            "issuer_name": name,
            "profile_url": f"https://www.idx.co.id/en/listed-companies/company-profiles/{ticker}",
            "listing_date": _string_or_none(row.get("TanggalPencatatan")),
            "website": _string_or_none(row.get("Website")),
            "email": _string_or_none(row.get("Email")),
            "phone": _string_or_none(row.get("Telepon")),
            "fax": _string_or_none(row.get("Fax")),
            "address": _string_or_none(row.get("Alamat")),
            "share_registrar": _string_or_none(row.get("BAE")),
            "industry": _string_or_none(row.get("Industri")),
            "sub_industry": _string_or_none(row.get("SubIndustri")),
            "sector": _string_or_none(row.get("Sektor")),
            "sub_sector": _string_or_none(row.get("SubSektor")),
            "business_activity": _string_or_none(row.get("KegiatanUsahaUtama")),
            "npwp": _string_or_none(row.get("NPWP")),
            "npkp": _string_or_none(row.get("NPKP")),
            "listing_board": _string_or_none(row.get("PapanPencatatan")),
            "logo_url": _absolute_idx_url(_string_or_none(row.get("Logo"))),
            "listed_instruments": _listed_instruments(row),
            "status_code": row.get("Status"),
        }

        candidates.append(
            DocumentCandidate(
                source_name="idx_company_universe",
                source_class=SourceClass.COMPANY_UNIVERSE,
                source_record_id=ticker,
                title=name,
                ticker=ticker,
                raw_metadata={k: v for k, v in raw_metadata.items() if v not in (None, "", [])},
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


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = _decode_js_string(value).strip()
        return text or None
    return str(value)


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
    if not path:
        return path
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if not path.startswith("/"):
        path = "/" + path
    return "https://www.idx.co.id" + path


def _listed_instruments(row: dict[str, Any]) -> list[str]:
    field_map = {
        "EfekEmiten_Saham": "stock",
        "EfekEmiten_Obligasi": "bond",
        "EfekEmiten_ETF": "etf",
        "EfekEmiten_EBA": "asset_backed_security",
        "EfekEmiten_SPEI": "special_purpose_entity",
    }
    return [label for key, label in field_map.items() if row.get(key)]
