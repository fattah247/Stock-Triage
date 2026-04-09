"""Phase 3 download + versioning bootstrap.

Downloads candidate attachments into the content-addressed file store and
persists concrete document rows plus stable version groups.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from ..config import Config
from ..db import WriterQueue, connect, configure_pragmas, run_migrations
from ..logging_setup import get_logger
from ..recon import PlaywrightSession
from ..storage import FileStore, StoredFile


@dataclass(frozen=True)
class DownloadCandidate:
    candidate_id: str
    source_name: str
    source_record_id: str
    ticker: str | None
    title: str | None
    attachment_url: str
    attachment_filename: str | None
    upstream_published_at: str | None
    raw_metadata: dict[str, Any]


@dataclass(frozen=True)
class ExistingDownload:
    document_id: str
    etag: str | None
    last_modified: str | None


@dataclass(frozen=True)
class DownloadedAttachment:
    original_url: str
    retrieved_at: datetime
    mime_type: str | None
    etag: str | None
    last_modified: str | None
    stored_file: StoredFile


@dataclass(frozen=True)
class DownloadPersistResult:
    inserted: bool
    document_id: str | None = None


@dataclass(frozen=True)
class DownloadRunResult:
    selected: int = 0
    fetched: int = 0
    inserted: int = 0
    skipped: int = 0
    not_modified: int = 0
    failed: int = 0


class AttachmentDownloader:
    def __init__(self, cfg: Config, *, client: httpx.Client | None = None) -> None:
        self.cfg = cfg
        self.store = FileStore(cfg.paths.files_dir)
        self._owns_client = client is None
        self.client = client or httpx.Client(
            headers={"User-Agent": cfg.discovery.user_agent, "Accept": "*/*"},
            timeout=cfg.discovery.request_timeout_s,
            follow_redirects=True,
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "AttachmentDownloader":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def fetch(
        self,
        candidate: DownloadCandidate,
        *,
        existing: ExistingDownload | None = None,
    ) -> DownloadedAttachment | None:
        headers: dict[str, str] = {}
        if existing is not None:
            if existing.etag:
                headers["If-None-Match"] = existing.etag
            if existing.last_modified:
                headers["If-Modified-Since"] = existing.last_modified

        retrieved_at = datetime.now(timezone.utc)
        if _requires_browser_session(candidate.attachment_url):
            session = PlaywrightSession(
                profile_dir=self.cfg.browser.profile_dir,
                artifacts_dir=self.cfg.browser.artifacts_dir,
                headless=self.cfg.browser.headless,
                challenge_timeout_s=self.cfg.browser.challenge_timeout_s,
                page_settle_ms=self.cfg.browser.page_settle_ms,
                channel=self.cfg.browser.channel,
            )
            fetched = session.fetch_resource(
                source_id=candidate.source_name,
                page_url=_source_page_url(candidate.source_name),
                resource_url=candidate.attachment_url,
                # IDX binary routes succeed via browser navigation, but
                # conditional headers cause ERR_ABORTED on repeat navigations.
                headers=None,
            )
            if fetched.status == 304:
                return None
            if fetched.status >= 400:
                raise httpx.HTTPStatusError(
                    f"Client error '{fetched.status}' for url '{candidate.attachment_url}'",
                    request=httpx.Request("GET", candidate.attachment_url, headers=headers or None),
                    response=httpx.Response(fetched.status, headers=fetched.headers, request=httpx.Request("GET", candidate.attachment_url)),
                )
            _validate_download_bytes(
                body=fetched.body,
                mime_type=fetched.content_type,
                filename=candidate.attachment_filename,
                url=candidate.attachment_url,
            )
            stored = self.store.put_bytes(fetched.body)
            return DownloadedAttachment(
                original_url=fetched.final_url,
                retrieved_at=retrieved_at,
                mime_type=_normalize_mime_type(fetched.content_type, candidate.attachment_filename),
                etag=fetched.headers.get("etag"),
                last_modified=fetched.headers.get("last-modified"),
                stored_file=stored,
            )

        response = self.client.get(candidate.attachment_url, headers=headers or None)
        if response.status_code == 304:
            return None
        response.raise_for_status()
        _validate_download_bytes(
            body=response.content,
            mime_type=response.headers.get("content-type"),
            filename=candidate.attachment_filename,
            url=candidate.attachment_url,
        )

        stored = self.store.put_bytes(response.content)
        return DownloadedAttachment(
            original_url=str(response.url),
            retrieved_at=retrieved_at,
            mime_type=_normalize_mime_type(response.headers.get("content-type"), candidate.attachment_filename),
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
            stored_file=stored,
        )


def run_downloads(
    cfg: Config,
    *,
    source_name: str | None = None,
    limit: int = 25,
    include_existing: bool = False,
    client: httpx.Client | None = None,
) -> DownloadRunResult:
    cfg.ensure_dirs()
    log = get_logger("stock_triage.download")

    result = DownloadRunResult()
    with WriterQueue(cfg) as wq, AttachmentDownloader(cfg, client=client) as downloader:
        wq.submit(run_migrations)
        reader = connect(cfg.paths.db_path, read_only=True)
        try:
            configure_pragmas(reader, cfg)
            candidates = _select_candidates(
                reader,
                source_name=source_name,
                limit=limit,
                include_existing=include_existing,
            )
            result = DownloadRunResult(selected=len(candidates))

            for candidate in candidates:
                existing = _latest_download(reader, candidate)
                try:
                    fetched = downloader.fetch(candidate, existing=existing)
                except Exception as exc:  # noqa: BLE001 - surfaced in summary/logs
                    result = _replace_result(result, failed=result.failed + 1)
                    log.warning(
                        "download failed",
                        extra={
                            "candidate_id": candidate.candidate_id,
                            "source_name": candidate.source_name,
                            "source_record_id": candidate.source_record_id,
                            "attachment_url": candidate.attachment_url,
                            "error": str(exc),
                        },
                    )
                    continue

                if fetched is None:
                    result = _replace_result(result, not_modified=result.not_modified + 1)
                    continue

                persist = wq.submit(
                    lambda conn, c=candidate, f=fetched: _persist_download(conn, candidate=c, fetched=f)
                )
                result = _replace_result(result, fetched=result.fetched + 1)
                if persist.inserted:
                    result = _replace_result(result, inserted=result.inserted + 1)
                else:
                    result = _replace_result(result, skipped=result.skipped + 1)

                log.info(
                    "attachment downloaded",
                    extra={
                        "candidate_id": candidate.candidate_id,
                        "document_id": persist.document_id,
                        "source_name": candidate.source_name,
                        "source_record_id": candidate.source_record_id,
                        "sha256": fetched.stored_file.sha256,
                        "deduplicated": fetched.stored_file.deduplicated,
                    },
                )
        finally:
            reader.close()

    return result


def _select_candidates(
    conn: sqlite3.Connection,
    *,
    source_name: str | None,
    limit: int,
    include_existing: bool,
) -> list[DownloadCandidate]:
    sql = """
        SELECT
            candidate_id,
            source_name,
            source_record_id,
            ticker,
            title,
            upstream_published_at,
            attachment_url,
            raw_metadata
        FROM document_candidates
        WHERE attachment_url IS NOT NULL
    """
    params: list[Any] = []
    if source_name:
        sql += " AND source_name = ?"
        params.append(source_name)
    if not include_existing:
        sql += """
            AND NOT EXISTS (
                SELECT 1
                FROM documents d
                WHERE d.source_name = document_candidates.source_name
                  AND d.source_record_id = document_candidates.source_record_id
            )
        """
    sql += """
        ORDER BY COALESCE(upstream_published_at, discovered_at) ASC, candidate_id ASC
        LIMIT ?
    """
    params.append(limit)

    rows = conn.execute(sql, tuple(params)).fetchall()
    out: list[DownloadCandidate] = []
    for row in rows:
        raw_metadata = json.loads(row["raw_metadata"])
        out.append(
            DownloadCandidate(
                candidate_id=row["candidate_id"],
                source_name=row["source_name"],
                source_record_id=row["source_record_id"],
                ticker=row["ticker"],
                title=row["title"],
                attachment_url=row["attachment_url"],
                attachment_filename=raw_metadata.get("filename") or raw_metadata.get("file_name"),
                upstream_published_at=row["upstream_published_at"],
                raw_metadata=raw_metadata,
            )
        )
    return out


def _latest_download(conn: sqlite3.Connection, candidate: DownloadCandidate) -> ExistingDownload | None:
    row = conn.execute(
        """
        SELECT document_id, etag, last_modified
        FROM documents
        WHERE source_name = ? AND source_record_id = ?
        ORDER BY retrieved_at DESC
        LIMIT 1
        """,
        (candidate.source_name, candidate.source_record_id),
    ).fetchone()
    if row is None:
        return None
    return ExistingDownload(
        document_id=row["document_id"],
        etag=row["etag"],
        last_modified=row["last_modified"],
    )


def _persist_download(
    conn: sqlite3.Connection,
    *,
    candidate: DownloadCandidate,
    fetched: DownloadedAttachment,
) -> DownloadPersistResult:
    existing = conn.execute(
        """
        SELECT document_id
        FROM documents
        WHERE source_name = ? AND source_record_id = ? AND sha256 = ?
        """,
        (candidate.source_name, candidate.source_record_id, fetched.stored_file.sha256),
    ).fetchone()
    if existing is not None:
        return DownloadPersistResult(inserted=False, document_id=existing["document_id"])

    now = fetched.retrieved_at.isoformat()
    version_group_id = _stable_id(f"version:{candidate.source_name}:{candidate.source_record_id}")
    document_id = _stable_id(
        f"document:{candidate.source_name}:{candidate.source_record_id}:{fetched.stored_file.sha256}"
    )

    conn.execute(
        """
        INSERT INTO document_versions (
            version_group_id, source_name, source_record_id, ticker, created_at
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(version_group_id) DO NOTHING
        """,
        (
            version_group_id,
            candidate.source_name,
            candidate.source_record_id,
            candidate.ticker,
            now,
        ),
    )
    conn.execute(
        """
        INSERT INTO documents (
            document_id, source_name, source_record_id, source_file_id,
            source_modified_at, retrieved_at, original_url, local_path, sha256,
            mime_type, document_type, language_variant, process_state,
            version_group_id, etag, last_modified, ticker, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            document_id,
            candidate.source_name,
            candidate.source_record_id,
            candidate.source_record_id,
            candidate.raw_metadata.get("file_modified") or candidate.upstream_published_at,
            now,
            fetched.original_url,
            str(fetched.stored_file.local_path),
            fetched.stored_file.sha256,
            fetched.mime_type,
            _document_type(candidate),
            "unknown",
            "downloaded",
            version_group_id,
            fetched.etag,
            fetched.last_modified,
            candidate.ticker,
            now,
            now,
        ),
    )
    return DownloadPersistResult(inserted=True, document_id=document_id)


def _document_type(candidate: DownloadCandidate) -> str:
    if candidate.source_name == "idx_financial_reports":
        if candidate.raw_metadata.get("report_type") == "rda":
            return "annual_report"
        return "financial_statement"
    if candidate.source_name == "idx_disclosures":
        return "disclosure"
    return "other"


def _normalize_mime_type(content_type: str | None, filename: str | None) -> str | None:
    if content_type:
        mime_type = content_type.split(";", 1)[0].strip()
        if mime_type:
            return mime_type
    if filename:
        guessed, _ = mimetypes.guess_type(filename)
        return guessed
    return None


def _validate_download_bytes(*, body: bytes, mime_type: str | None, filename: str | None, url: str) -> None:
    normalized_mime = _normalize_mime_type(mime_type, filename)
    expected_pdf = normalized_mime == "application/pdf" or (filename or url).lower().endswith(".pdf")
    if expected_pdf and not body.startswith(b"%PDF-"):
        raise ValueError(f"Expected PDF bytes from {url}, but received non-PDF content")


def _stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _replace_result(result: DownloadRunResult, **updates: int) -> DownloadRunResult:
    payload = {
        "selected": result.selected,
        "fetched": result.fetched,
        "inserted": result.inserted,
        "skipped": result.skipped,
        "not_modified": result.not_modified,
        "failed": result.failed,
    }
    payload.update(updates)
    return DownloadRunResult(**payload)


def _requires_browser_session(url: str) -> bool:
    return "idx.co.id" in url


def _source_page_url(source_name: str) -> str:
    mapping = {
        "idx_financial_reports": "https://www.idx.co.id/en/listed-companies/financial-statements-and-annual-report",
        "idx_disclosures": "https://www.idx.co.id/en/listed-companies/disclosure",
    }
    return mapping.get(source_name, "https://www.idx.co.id/en/")
