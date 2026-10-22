"""Bank Indonesia's JISDOR USD/IDR reference rate (docs/phase2_market_recon.md).

The page is an ASP.NET form. A date-range search followed by the export button
returns the whole range as an xlsx file, read with the stdlib workbook reader.
Field names carry a SharePoint web-part prefix that can change on redeploy, so
they are found by suffix.
"""

from __future__ import annotations

import html
import io
import re
import time
from datetime import date, datetime

import httpx

from ..parse.workbook import WorkbookError, read_workbook, split_cells

SOURCE_NAME = "bi_jisdor"
SERIES_ID = "fx:USDIDR_JISDOR"
_HIDDEN_INPUT = re.compile(r'<input[^>]+type="hidden"[^>]+name="([^"]+)"[^>]+value="([^"]*)"')
_PAUSE_S = 1.5


class JisdorError(RuntimeError):
    """The JISDOR page did not behave as recorded in the recon."""


def fetch_jisdor_export(client: httpx.Client, *, url: str, start: date, end: date) -> bytes:
    """The xlsx export for ``start``..``end``: load, search, then export."""
    page = client.get(url)
    page.raise_for_status()
    date_from, date_to, search_button = (
        _field_name(page.text, suffix) for suffix in ("TextBoxFrom", "TextBoxDateTo", "ButtonCari")
    )
    dates = {date_from: start.strftime("%d/%m/%Y"), date_to: end.strftime("%d/%m/%Y")}
    time.sleep(_PAUSE_S)
    search = client.post(url, data={**_hidden_fields(page.text), **dates, search_button: "Cari"})
    search.raise_for_status()
    time.sleep(_PAUSE_S)
    export = client.post(
        url,
        data={
            **_hidden_fields(search.text),
            **dates,
            "__EVENTTARGET": _field_name(search.text, "ButtonExport"),
            "__EVENTARGUMENT": "",
        },
    )
    export.raise_for_status()
    if "spreadsheetml" not in export.headers.get("content-type", ""):
        raise JisdorError(f"JISDOR export returned {export.headers.get('content-type')!r}, not an xlsx file")
    return export.content


def parse_jisdor_export(content: bytes) -> dict[date, float]:
    """``{date: rupiah per US dollar}`` from the export's ``NO, Tanggal, Kurs`` rows."""
    try:
        sheets = read_workbook(io.BytesIO(content))
    except WorkbookError as exc:
        raise JisdorError(f"JISDOR export is not a readable workbook: {exc}") from exc
    rates: dict[date, float] = {}
    for sheet in sheets:
        for line in sheet.lines:
            cells = split_cells(line)
            if len(cells) < 3:
                continue
            day, rate = _parse_date(cells[1]), _parse_rate(cells[2])
            if day is not None and rate is not None:
                rates[day] = rate
    return rates


def _field_name(page: str, suffix: str) -> str:
    # Inputs carry the name; the export link only names itself in an escaped
    # ``href="javascript:__doPostBack(&#39;…$ButtonExport&#39;,&#39;&#39;)"``.
    text = html.unescape(page)
    match = re.search(rf'name="([^"]*\${suffix})"', text) or re.search(rf"__doPostBack\('([^']*\${suffix})'", text)
    if match is None:
        raise JisdorError(f"JISDOR page has no field ending in {suffix}; the form changed")
    return match.group(1)


def _hidden_fields(page: str) -> dict[str, str]:
    return {name: html.unescape(value) for name, value in _HIDDEN_INPUT.findall(page)}


def _parse_date(text: str) -> date | None:
    # The export writes US-style timestamps: "1/2/2020 12:00:00 AM".
    try:
        return datetime.strptime(text.strip().split(" ")[0], "%m/%d/%Y").date()
    except ValueError:
        return None


def _parse_rate(text: str) -> float | None:
    # Indonesian notation: "13.895,00".
    cleaned = text.strip().replace("Rp", "").strip().replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None
