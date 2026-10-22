"""IDX FinancialStatement-*.xlsx workbooks, read with the standard library.

IDX publishes each filing's statements as an XBRL-derived workbook: one sheet
per taxonomy role ('1000000' general information, '4220000' financial position,
...), titled "[<code>] <role name>" in A1, with a row naming the XBRL context of
each value column ("CurrentYearInstant", "PriorYearDuration", ...) above line
items laid out as [Indonesian label, values..., English label].

The parse stage renders every visible sheet as one line per row, cells joined by
``CELL_SEPARATOR``, so the statements are stored as chunks like any other text;
the extract stage reads the figures back with ``split_cells`` and
``value_contexts``.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import BinaryIO

WORKBOOK_MIME_TYPES = frozenset({"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"})
CELL_SEPARATOR = "\t"

# IDX's workbooks are well under 1 MB; anything this large is not one of them.
_MAX_PART_BYTES = 64 * 1024 * 1024
_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_RELATIONSHIP_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_PACKAGE_RELATIONSHIP = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
_CONTEXT = re.compile(r"^(?:Current|Prior)\w*(?:Instant|Duration)$")
_CELL_COLUMN = re.compile(r"^([A-Z]+)\d+$")


class WorkbookError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class WorkbookSheet:
    name: str
    title: str
    lines: list[str]


def read_workbook(path: Path | BinaryIO) -> list[WorkbookSheet]:
    """The visible sheets of the workbook at ``path`` (or an open binary file), rendered as lines of cells.

    Below a sheet's context row, rows with no figure in any value column are
    captions of empty line items and are left out.
    """
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise WorkbookError("xlsx_open_failed", f"Not a readable xlsx workbook: {path}: {exc}") from exc
    with archive:
        try:
            workbook = _read_xml(archive, "xl/workbook.xml")
            targets: dict[str, str] = {}
            shared_strings_part = None
            for relationship in _read_xml(archive, "xl/_rels/workbook.xml.rels").iter(_PACKAGE_RELATIONSHIP):
                target = _part_name(relationship.get("Target") or "")
                targets[relationship.get("Id") or ""] = target
                if (relationship.get("Type") or "").endswith("/sharedStrings"):
                    shared_strings_part = target
            shared_strings = (
                [_normalize_text("".join(node.text or "" for node in item.iter(f"{_MAIN}t"))) for item in _read_xml(archive, shared_strings_part).iter(f"{_MAIN}si")]
                if shared_strings_part
                else []
            )
            sheets: list[WorkbookSheet] = []
            for sheet in workbook.iter(f"{_MAIN}sheet"):
                if sheet.get("state") in {"hidden", "veryHidden"}:
                    continue
                name = sheet.get("name") or ""
                rows = _read_rows(_read_xml(archive, targets[sheet.get(_RELATIONSHIP_ID) or ""]), shared_strings)
                rendered = _render_sheet(name, rows)
                if rendered is not None:
                    sheets.append(rendered)
        except (KeyError, ET.ParseError, zipfile.BadZipFile) as exc:
            raise WorkbookError("xlsx_malformed", f"Malformed xlsx workbook: {path}: {exc}") from exc
    return sheets


def split_cells(line: str) -> list[str]:
    return line.split(CELL_SEPARATOR)


def value_contexts(cells: list[str]) -> dict[int, str]:
    """XBRL context by column when ``cells`` is a sheet's context row, else empty."""
    return {index: cell for index, cell in enumerate(cells) if _CONTEXT.match(cell)}


def _read_xml(archive: zipfile.ZipFile, name: str) -> ET.Element:
    if archive.getinfo(name).file_size > _MAX_PART_BYTES:
        raise WorkbookError("xlsx_too_large", f"Workbook part {name} exceeds {_MAX_PART_BYTES} bytes")
    data = archive.read(name)
    # Spreadsheet parts never declare a DTD; refusing one rules out entity expansion.
    if b"<!DOCTYPE" in data:
        raise WorkbookError("xlsx_malformed", f"Workbook part {name} declares a DTD")
    return ET.fromstring(data)


def _part_name(target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return str(PurePosixPath("xl") / target)


def _read_rows(worksheet: ET.Element, shared_strings: list[str]) -> list[dict[int, str]]:
    rows: list[dict[int, str]] = []
    for row in worksheet.iter(f"{_MAIN}row"):
        cells: dict[int, str] = {}
        for position, cell in enumerate(row.iter(f"{_MAIN}c")):
            text = _cell_text(cell, shared_strings)
            if text:
                cells[_column_index(cell.get("r"), position)] = text
        rows.append(cells)
    return rows


def _cell_text(cell: ET.Element, shared_strings: list[str]) -> str:
    kind = cell.get("t")
    if kind == "inlineStr":
        return _normalize_text("".join(node.text or "" for node in cell.iter(f"{_MAIN}t")))
    value = cell.find(f"{_MAIN}v")
    if value is None or not value.text:
        return ""
    if kind == "s":
        try:
            return shared_strings[int(value.text)]
        except (IndexError, ValueError):
            return ""
    if kind == "e":
        return ""
    if kind in {"str", "b"}:
        return _normalize_text(value.text)
    return _canonical_number(value.text)


def _column_index(reference: str | None, position: int) -> int:
    match = _CELL_COLUMN.match(reference or "")
    if match is None:
        return position
    index = 0
    for letter in match.group(1):
        index = index * 26 + ord(letter) - ord("A") + 1
    return index - 1


def _canonical_number(text: str) -> str:
    """Plain decimal text for a numeric cell: '2.5018245654E11' -> '250182456540'."""
    try:
        number = Decimal(text.strip())
    except InvalidOperation:
        return _normalize_text(text)
    if not number.is_finite():
        return ""
    if number == number.to_integral_value():
        return str(int(number))
    return format(number.normalize(), "f")


def _normalize_text(text: str) -> str:
    # Tabs and newlines delimit cells and rows once rendered.
    return " ".join(text.split())


def _render_sheet(name: str, rows: list[dict[int, str]]) -> WorkbookSheet | None:
    lines: list[str] = []
    title = ""
    value_columns: list[int] | None = None
    for cells in rows:
        if not cells:
            continue
        if not title:
            title = cells[min(cells)]
        line = [cells.get(column, "") for column in range(max(cells) + 1)]
        if value_columns is None:
            contexts = value_contexts(line)
            if contexts:
                value_columns = list(contexts)
        elif not any(cells.get(column) for column in value_columns):
            continue
        lines.append(CELL_SEPARATOR.join(line))
    if not lines:
        return None
    return WorkbookSheet(name=name, title=title or name, lines=lines)
