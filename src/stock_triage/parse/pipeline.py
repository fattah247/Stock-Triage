"""Phase 4 parse pipeline: native extraction, OCR fallback, and chunk persistence."""

from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import fitz

from ..config import Config
from ..db import WriterQueue, connect, configure_pragmas, run_migrations

PARSER_VERSION = "phase4-v1"
CHUNK_TARGET_CHARS = 1500


@dataclass(frozen=True)
class ParseDocument:
    document_id: str
    source_name: str
    source_record_id: str
    local_path: Path
    mime_type: str | None
    process_state: str


@dataclass(frozen=True)
class ParsedPage:
    page_number: int
    text: str
    ocr_used: bool


@dataclass(frozen=True)
class ChunkRecord:
    chunk_id: str
    document_id: str
    section_label: str | None
    page_start: int
    page_end: int
    char_start: int
    char_end: int
    text: str
    ord: int


@dataclass(frozen=True)
class ParsedDocumentResult:
    page_count: int
    parse_strategy: str
    parse_confidence: float
    ocr_used: bool
    chunks: list[ChunkRecord]


@dataclass(frozen=True)
class ParseRunResult:
    selected: int = 0
    parsed: int = 0
    failed: int = 0
    skipped: int = 0
    ocr_partial: int = 0
    ocr_full: int = 0
    chunks: int = 0


@dataclass(frozen=True)
class ParseStatusRow:
    source_name: str
    total: int
    downloaded: int
    parsed: int
    failed: int
    ocr_partial: int
    ocr_full: int


@dataclass(frozen=True)
class ParseFailureRow:
    document_id: str
    source_name: str
    source_record_id: str
    process_error_code: str | None
    parse_strategy: str | None
    updated_at: str


@dataclass(frozen=True)
class ParseStatusReport:
    rows: list[ParseStatusRow]
    recent_failures: list[ParseFailureRow]


class ParseError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def run_parse(
    cfg: Config,
    *,
    source_name: str | None = None,
    limit: int = 25,
    include_parsed: bool = False,
    retry_failed_only: bool = False,
) -> ParseRunResult:
    cfg.ensure_dirs()

    result = ParseRunResult()
    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
        reader = connect(cfg.paths.db_path, read_only=True)
        try:
            configure_pragmas(reader, cfg)
            documents = _select_documents(
                reader,
                source_name=source_name,
                limit=limit,
                include_parsed=include_parsed,
                retry_failed_only=retry_failed_only,
            )
            result = _replace_result(result, selected=len(documents))
            for document in documents:
                try:
                    parsed = _parse_document(cfg, document)
                except ParseError as exc:
                    wq.submit(
                        lambda conn, d=document, e=exc: _persist_parse_failure(conn, document=d, error=e)
                    )
                    result = _replace_result(result, failed=result.failed + 1)
                    continue

                wq.submit(
                    lambda conn, d=document, p=parsed: _persist_parse_success(conn, document=d, parsed=p)
                )
                result = _replace_result(
                    result,
                    parsed=result.parsed + 1,
                    ocr_partial=result.ocr_partial + (1 if parsed.parse_strategy == "ocr_partial" else 0),
                    ocr_full=result.ocr_full + (1 if parsed.parse_strategy == "ocr_full" else 0),
                    chunks=result.chunks + len(parsed.chunks),
                )
        finally:
            reader.close()
    return result


def load_parse_status(
    cfg: Config,
    *,
    source_name: str | None = None,
    failure_limit: int = 10,
) -> ParseStatusReport:
    cfg.ensure_dirs()
    conn = connect(cfg.paths.db_path, read_only=True)
    try:
        configure_pragmas(conn, cfg)
        params: list[object] = []
        where = ""
        if source_name:
            where = "WHERE source_name = ?"
            params.append(source_name)
        rows = conn.execute(
            f"""
            SELECT
                source_name,
                COUNT(*) AS total,
                SUM(CASE WHEN process_state = 'downloaded' THEN 1 ELSE 0 END) AS downloaded,
                SUM(CASE WHEN process_state = 'parsed' THEN 1 ELSE 0 END) AS parsed,
                SUM(CASE WHEN process_state = 'failed' THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN parse_strategy = 'ocr_partial' THEN 1 ELSE 0 END) AS ocr_partial,
                SUM(CASE WHEN parse_strategy = 'ocr_full' THEN 1 ELSE 0 END) AS ocr_full
            FROM documents
            {where}
            GROUP BY source_name
            ORDER BY source_name
            """,
            tuple(params),
        ).fetchall()
        failures = conn.execute(
            f"""
            SELECT document_id, source_name, source_record_id, process_error_code, parse_strategy, updated_at
            FROM documents
            {where} {"AND" if where else "WHERE"} process_state = 'failed'
            ORDER BY updated_at DESC, document_id ASC
            LIMIT ?
            """,
            tuple(params + [failure_limit]),
        ).fetchall()
        return ParseStatusReport(
            rows=[
                ParseStatusRow(
                    source_name=row["source_name"],
                    total=row["total"],
                    downloaded=row["downloaded"] or 0,
                    parsed=row["parsed"] or 0,
                    failed=row["failed"] or 0,
                    ocr_partial=row["ocr_partial"] or 0,
                    ocr_full=row["ocr_full"] or 0,
                )
                for row in rows
            ],
            recent_failures=[
                ParseFailureRow(
                    document_id=row["document_id"],
                    source_name=row["source_name"],
                    source_record_id=row["source_record_id"],
                    process_error_code=row["process_error_code"],
                    parse_strategy=row["parse_strategy"],
                    updated_at=row["updated_at"],
                )
                for row in failures
            ],
        )
    finally:
        conn.close()


def _select_documents(
    conn: sqlite3.Connection,
    *,
    source_name: str | None,
    limit: int,
    include_parsed: bool,
    retry_failed_only: bool,
) -> list[ParseDocument]:
    if retry_failed_only:
        states = ["failed"]
    else:
        states = ["downloaded"]
    if include_parsed and not retry_failed_only:
        states.extend(["parsed", "failed"])
    placeholders = ",".join("?" for _ in states)
    sql = f"""
        SELECT document_id, source_name, source_record_id, local_path, mime_type, process_state
        FROM documents
        WHERE process_state IN ({placeholders})
          AND local_path IS NOT NULL
    """
    params: list[object] = list(states)
    if source_name:
        sql += " AND source_name = ?"
        params.append(source_name)
    sql += " ORDER BY retrieved_at ASC, document_id ASC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, tuple(params)).fetchall()
    return [
        ParseDocument(
            document_id=row["document_id"],
            source_name=row["source_name"],
            source_record_id=row["source_record_id"],
            local_path=Path(row["local_path"]),
            mime_type=row["mime_type"],
            process_state=row["process_state"],
        )
        for row in rows
    ]


def _parse_document(cfg: Config, document: ParseDocument) -> ParsedDocumentResult:
    if document.mime_type and document.mime_type != "application/pdf":
        raise ParseError("unsupported_mime", f"Unsupported MIME type: {document.mime_type}")
    if not document.local_path.exists():
        raise ParseError("missing_local_file", f"Local file is missing: {document.local_path}")

    try:
        pdf = fitz.open(document.local_path)
    except Exception as exc:  # noqa: BLE001
        raise ParseError("pdf_open_failed", str(exc)) from exc

    try:
        if pdf.page_count == 0:
            raise ParseError("empty_pdf", f"PDF has no pages: {document.local_path}")

        pages: list[ParsedPage] = []
        ocr_required_pages: list[int] = []

        for idx, page in enumerate(pdf):
            text = _normalize_text(page.get_text("text", sort=True))
            char_count = len(_visible_text(text))
            pages.append(
                ParsedPage(
                    page_number=idx + 1,
                    text=text,
                    ocr_used=False,
                )
            )
            if char_count < cfg.ocr.min_chars_per_page:
                ocr_required_pages.append(idx)

        if ocr_required_pages:
            if not _ocr_available():
                raise ParseError(
                    "ocr_unavailable",
                    "OCR fallback required but the local Tesseract binary is not available",
                )
            full_ocr = len(ocr_required_pages) == pdf.page_count
            for idx in ocr_required_pages:
                page = pdf[idx]
                ocr_text = _normalize_text(_ocr_page_text(cfg, page))
                pages[idx] = ParsedPage(page_number=idx + 1, text=ocr_text, ocr_used=True)
            strategy = "ocr_full" if full_ocr else "ocr_partial"
        else:
            strategy = "native"

        chunks = _build_chunks(document.document_id, pages)
        if not chunks:
            raise ParseError("parse_empty_text", f"Parsing produced no usable text for {document.document_id}")
        confidence = _parse_confidence(strategy=strategy, pages=pages)
        return ParsedDocumentResult(
            page_count=pdf.page_count,
            parse_strategy=strategy,
            parse_confidence=confidence,
            ocr_used=any(page.ocr_used for page in pages),
            chunks=chunks,
        )
    finally:
        pdf.close()


def _ocr_available() -> bool:
    return shutil.which("tesseract") is not None


def _ocr_page_text(cfg: Config, page: fitz.Page) -> str:
    try:
        textpage = page.get_textpage_ocr(
            language=cfg.ocr.languages,
            dpi=300,
            full=False,
        )
    except Exception as exc:  # noqa: BLE001
        raise ParseError("ocr_failed", str(exc)) from exc
    return page.get_text("text", textpage=textpage, sort=True)


def _build_chunks(document_id: str, pages: list[ParsedPage]) -> list[ChunkRecord]:
    chunks: list[ChunkRecord] = []
    ord_value = 0
    current_section: str | None = None
    for page in pages:
        if not _visible_text(page.text):
            continue
        for start, end, block_text in _page_blocks(page.text):
            heading = _section_heading(block_text)
            if heading is not None:
                current_section = heading
                continue

            relative_start = 0
            while relative_start < len(block_text):
                relative_end = min(relative_start + CHUNK_TARGET_CHARS, len(block_text))
                if relative_end < len(block_text):
                    split = max(
                        block_text.rfind("\n\n", relative_start, relative_end),
                        block_text.rfind("\n", relative_start, relative_end),
                        block_text.rfind(" ", relative_start, relative_end),
                    )
                    if split > relative_start:
                        relative_end = split
                chunk_text = block_text[relative_start:relative_end].strip()
                if not chunk_text:
                    relative_start = relative_end + 1
                    continue
                char_start = start + block_text.find(chunk_text, relative_start, relative_end + len(chunk_text))
                char_end = char_start + len(chunk_text)
                chunks.append(
                    ChunkRecord(
                        chunk_id=_stable_id(
                            f"chunk:{document_id}:{page.page_number}:{ord_value}:{char_start}:{char_end}"
                        ),
                        document_id=document_id,
                        section_label=current_section,
                        page_start=page.page_number,
                        page_end=page.page_number,
                        char_start=char_start,
                        char_end=char_end,
                        text=chunk_text,
                        ord=ord_value,
                    )
                )
                ord_value += 1
                relative_start = block_text.find(chunk_text, relative_start, relative_end + len(chunk_text)) + len(chunk_text)
                while relative_start < len(block_text) and block_text[relative_start].isspace():
                    relative_start += 1
    return chunks


def _page_blocks(text: str) -> list[tuple[int, int, str]]:
    blocks: list[tuple[int, int, str]] = []
    cursor = 0
    for raw_block in re.split(r"\n{2,}", text):
        block = raw_block.strip()
        if not block:
            cursor += len(raw_block) + 2
            continue
        start = text.find(block, cursor)
        end = start + len(block)
        blocks.append((start, end, block))
        cursor = end
    return blocks


def _section_heading(block_text: str) -> str | None:
    lines = [line.strip() for line in block_text.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    line = lines[0]
    if len(line) > 120 or line.endswith("."):
        return None
    words = re.findall(r"[A-Za-z][A-Za-z/&-]*", line)
    if len(words) < 2 or len(words) > 12:
        return None
    letters = [ch for ch in line if ch.isalpha()]
    if not letters:
        return None
    uppercase_ratio = sum(ch.isupper() for ch in letters) / len(letters)
    titlecase_ratio = sum(word[:1].isupper() for word in words) / len(words)
    if uppercase_ratio >= 0.75 or titlecase_ratio >= 0.75:
        return line
    return None


def _persist_parse_success(
    conn: sqlite3.Connection,
    *,
    document: ParseDocument,
    parsed: ParsedDocumentResult,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("DELETE FROM evidence_spans WHERE document_id = ?", (document.document_id,))
    conn.execute("DELETE FROM document_chunks WHERE document_id = ?", (document.document_id,))
    for chunk in parsed.chunks:
        conn.execute(
            """
            INSERT INTO document_chunks (
                chunk_id, document_id, section_label, page_start, page_end,
                char_start, char_end, text, ord
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chunk.chunk_id,
                chunk.document_id,
                chunk.section_label,
                chunk.page_start,
                chunk.page_end,
                chunk.char_start,
                chunk.char_end,
                chunk.text,
                chunk.ord,
            ),
        )
    conn.execute(
        """
        UPDATE documents
        SET process_state = ?, process_error_code = NULL,
            page_count = ?, parse_strategy = ?, parser_version = ?,
            parse_confidence = ?, ocr_used = ?, chunk_count = ?,
            updated_at = ?
        WHERE document_id = ?
        """,
        (
            "parsed",
            parsed.page_count,
            parsed.parse_strategy,
            PARSER_VERSION,
            parsed.parse_confidence,
            1 if parsed.ocr_used else 0,
            len(parsed.chunks),
            now,
            document.document_id,
        ),
    )


def _persist_parse_failure(
    conn: sqlite3.Connection,
    *,
    document: ParseDocument,
    error: ParseError,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("DELETE FROM evidence_spans WHERE document_id = ?", (document.document_id,))
    conn.execute("DELETE FROM document_chunks WHERE document_id = ?", (document.document_id,))
    conn.execute(
        """
        UPDATE documents
        SET process_state = ?, process_error_code = ?,
            parse_strategy = ?, parser_version = ?,
            parse_confidence = ?, ocr_used = ?, chunk_count = ?,
            updated_at = ?
        WHERE document_id = ?
        """,
        (
            "failed",
            error.code,
            "parse_failed",
            PARSER_VERSION,
            0.0,
            0,
            0,
            now,
            document.document_id,
        ),
    )


def _parse_confidence(*, strategy: str, pages: list[ParsedPage]) -> float:
    nonempty = sum(1 for page in pages if _visible_text(page.text))
    coverage = nonempty / max(len(pages), 1)
    base = {
        "native": 0.95,
        "ocr_partial": 0.8,
        "ocr_full": 0.7,
    }[strategy]
    return round(max(0.0, min(1.0, base * coverage)), 3)


def _normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _visible_text(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _replace_result(result: ParseRunResult, **updates: int) -> ParseRunResult:
    payload = {
        "selected": result.selected,
        "parsed": result.parsed,
        "failed": result.failed,
        "skipped": result.skipped,
        "ocr_partial": result.ocr_partial,
        "ocr_full": result.ocr_full,
        "chunks": result.chunks,
    }
    payload.update(updates)
    return ParseRunResult(**payload)
