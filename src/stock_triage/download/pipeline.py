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
class DownloadResource:
    source_file_id: str
    attachment_url: str
    attachment_filename: str | None


@dataclass(frozen=True)
class ExistingDownload:
    source_file_id: str
    document_id: str
    etag: str | None
    last_modified: str | None


@dataclass(frozen=True)
class ResourceState:
    source_file_id: str
    status: str
    document_id: str | None
    attempt_count: int
    failure_count: int
    next_attempt_at: str | None


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
class DownloadAttemptPersist:
    status: str
    started_at: datetime
    finished_at: datetime
    http_status: int | None = None
    error_code: str | None = None
    error_detail: str | None = None
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
        resource: DownloadResource,
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
        if _requires_browser_session(resource.attachment_url):
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
                resource_url=resource.attachment_url,
                # IDX binary routes succeed via browser navigation, but
                # conditional headers cause ERR_ABORTED on repeat navigations.
                headers=None,
            )
            if fetched.status == 304:
                return None
            if fetched.status >= 400:
                raise httpx.HTTPStatusError(
                    f"Client error '{fetched.status}' for url '{resource.attachment_url}'",
                    request=httpx.Request("GET", resource.attachment_url, headers=headers or None),
                    response=httpx.Response(fetched.status, headers=fetched.headers, request=httpx.Request("GET", resource.attachment_url)),
                )
            _validate_download_bytes(
                body=fetched.body,
                mime_type=fetched.content_type,
                filename=resource.attachment_filename,
                url=resource.attachment_url,
            )
            stored = self.store.put_bytes(fetched.body)
            return DownloadedAttachment(
                original_url=fetched.final_url,
                retrieved_at=retrieved_at,
                mime_type=_normalize_mime_type(fetched.content_type, resource.attachment_filename),
                etag=fetched.headers.get("etag"),
                last_modified=fetched.headers.get("last-modified"),
                stored_file=stored,
            )

        response = self.client.get(resource.attachment_url, headers=headers or None)
        if response.status_code == 304:
            return None
        response.raise_for_status()
        _validate_download_bytes(
            body=response.content,
            mime_type=response.headers.get("content-type"),
            filename=resource.attachment_filename,
            url=resource.attachment_url,
        )

        stored = self.store.put_bytes(response.content)
        return DownloadedAttachment(
            original_url=str(response.url),
            retrieved_at=retrieved_at,
            mime_type=_normalize_mime_type(response.headers.get("content-type"), resource.attachment_filename),
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
                resources = _resources_for_candidate(candidate)
                existing_downloads = _existing_downloads_for_candidate(reader, candidate)
                for resource in resources:
                    state = _resource_state_for_candidate(reader, candidate, resource)
                    if state is None:
                        initial_status = "downloaded" if resource.source_file_id in existing_downloads else "pending"
                        wq.submit(
                            lambda conn, c=candidate, r=resource, s=initial_status, doc=existing_downloads.get(resource.source_file_id): _upsert_download_resource(
                                conn,
                                candidate=c,
                                resource=r,
                                status=s,
                                document_id=None if doc is None else doc.document_id,
                            )
                        )
                    existing = existing_downloads.get(resource.source_file_id)
                    if not include_existing and existing is not None:
                        continue

                    started_at = datetime.now(timezone.utc)
                    try:
                        fetched = downloader.fetch(candidate, resource, existing=existing)
                    except Exception as exc:  # noqa: BLE001 - surfaced in summary/logs
                        finished_at = datetime.now(timezone.utc)
                        failure = _attempt_from_exception(exc, started_at=started_at, finished_at=finished_at)
                        wq.submit(
                            lambda conn, c=candidate, r=resource, a=failure: _persist_download_attempt(
                                conn,
                                candidate=c,
                                resource=r,
                                attempt=a,
                            )
                        )
                        wq.submit(
                            lambda conn, c=candidate, r=resource, a=failure: _upsert_download_resource(
                                conn,
                                candidate=c,
                                resource=r,
                                status=a.status,
                                document_id=None,
                                attempt=a,
                            )
                        )
                        result = _replace_result(result, failed=result.failed + 1)
                        log.warning(
                            "download failed",
                            extra={
                                "candidate_id": candidate.candidate_id,
                                "source_name": candidate.source_name,
                                "source_record_id": candidate.source_record_id,
                                "source_file_id": resource.source_file_id,
                                "attachment_url": resource.attachment_url,
                                "error": str(exc),
                            },
                        )
                        continue

                    if fetched is None:
                        finished_at = datetime.now(timezone.utc)
                        wq.submit(
                            lambda conn, c=candidate, r=resource, a=DownloadAttemptPersist(
                                status="not_modified",
                                started_at=started_at,
                                finished_at=finished_at,
                                document_id=existing.document_id if existing is not None else None,
                            ): _persist_download_attempt(conn, candidate=c, resource=r, attempt=a)
                        )
                        wq.submit(
                            lambda conn, c=candidate, r=resource, existing_doc=existing: _upsert_download_resource(
                                conn,
                                candidate=c,
                                resource=r,
                                status="downloaded",
                                document_id=None if existing_doc is None else existing_doc.document_id,
                            )
                        )
                        result = _replace_result(result, not_modified=result.not_modified + 1)
                        continue

                    persist = wq.submit(
                        lambda conn, c=candidate, r=resource, f=fetched: _persist_download(
                            conn,
                            candidate=c,
                            resource=r,
                            fetched=f,
                        )
                    )
                    finished_at = datetime.now(timezone.utc)
                    attempt_status = "downloaded" if persist.inserted else "duplicate"
                    http_status = 200 if fetched.original_url.startswith("http") else None
                    wq.submit(
                        lambda conn, c=candidate, r=resource, a=DownloadAttemptPersist(
                            status=attempt_status,
                            started_at=started_at,
                            finished_at=finished_at,
                            http_status=http_status,
                            document_id=persist.document_id,
                        ): _persist_download_attempt(conn, candidate=c, resource=r, attempt=a)
                    )
                    wq.submit(
                        lambda conn, c=candidate, r=resource, doc_id=persist.document_id: _upsert_download_resource(
                            conn,
                            candidate=c,
                            resource=r,
                            status="downloaded",
                            document_id=doc_id,
                        )
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
                            "source_file_id": resource.source_file_id,
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
    sql += """
        ORDER BY COALESCE(upstream_published_at, discovered_at) ASC, candidate_id ASC
    """

    rows = conn.execute(sql, tuple(params)).fetchall()
    selected: list[DownloadCandidate] = []
    for row in rows:
        raw_metadata = json.loads(row["raw_metadata"])
        candidate = DownloadCandidate(
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
        if include_existing or _candidate_has_pending_work(conn, candidate):
            selected.append(candidate)
            if len(selected) >= limit:
                break
    return selected


def _existing_downloads_for_candidate(conn: sqlite3.Connection, candidate: DownloadCandidate) -> dict[str, ExistingDownload]:
    rows = conn.execute(
        """
        SELECT source_file_id, document_id, etag, last_modified
        FROM documents
        WHERE source_name = ? AND source_record_id = ?
        ORDER BY retrieved_at DESC
        """,
        (candidate.source_name, candidate.source_record_id),
    ).fetchall()
    out: dict[str, ExistingDownload] = {}
    for row in rows:
        source_file_id = row["source_file_id"] or candidate.source_record_id
        if source_file_id in out:
            continue
        out[source_file_id] = ExistingDownload(
            source_file_id=source_file_id,
            document_id=row["document_id"],
            etag=row["etag"],
            last_modified=row["last_modified"],
        )
    return out


def _resource_state_for_candidate(
    conn: sqlite3.Connection,
    candidate: DownloadCandidate,
    resource: DownloadResource,
) -> ResourceState | None:
    row = conn.execute(
        """
        SELECT source_file_id, status, document_id, attempt_count, failure_count, next_attempt_at
        FROM download_resources
        WHERE source_name = ? AND source_record_id = ? AND source_file_id = ?
        """,
        (candidate.source_name, candidate.source_record_id, resource.source_file_id),
    ).fetchone()
    if row is None:
        return None
    return ResourceState(
        source_file_id=row["source_file_id"],
        status=row["status"],
        document_id=row["document_id"],
        attempt_count=row["attempt_count"],
        failure_count=row["failure_count"],
        next_attempt_at=row["next_attempt_at"],
    )


def _persist_download(
    conn: sqlite3.Connection,
    *,
    candidate: DownloadCandidate,
    resource: DownloadResource,
    fetched: DownloadedAttachment,
) -> DownloadPersistResult:
    existing = conn.execute(
        """
        SELECT document_id
        FROM documents
        WHERE source_name = ? AND source_record_id = ? AND source_file_id = ? AND sha256 = ?
        """,
        (candidate.source_name, candidate.source_record_id, resource.source_file_id, fetched.stored_file.sha256),
    ).fetchone()
    if existing is not None:
        return DownloadPersistResult(inserted=False, document_id=existing["document_id"])

    now = fetched.retrieved_at.isoformat()
    version_group_id = _version_group_id(candidate, resource)
    document_id = _stable_id(
        f"document:{candidate.source_name}:{candidate.source_record_id}:{resource.source_file_id}:{fetched.stored_file.sha256}"
    )

    conn.execute(
        """
        INSERT INTO document_versions (
            version_group_id, source_name, source_record_id, ticker, created_at, source_file_id
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(version_group_id) DO NOTHING
        """,
        (
            version_group_id,
            candidate.source_name,
            candidate.source_record_id,
            candidate.ticker,
            now,
            resource.source_file_id,
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
            resource.source_file_id,
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


def _upsert_download_resource(
    conn: sqlite3.Connection,
    *,
    candidate: DownloadCandidate,
    resource: DownloadResource,
    status: str,
    document_id: str | None,
    attempt: DownloadAttemptPersist | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    existing = conn.execute(
        """
        SELECT attempt_count, failure_count, status, document_id, first_seen_at
        FROM download_resources
        WHERE source_name = ? AND source_record_id = ? AND source_file_id = ?
        """,
        (candidate.source_name, candidate.source_record_id, resource.source_file_id),
    ).fetchone()
    attempt_count = 0 if existing is None else existing["attempt_count"]
    failure_count = 0 if existing is None else existing["failure_count"]
    if attempt is not None:
        attempt_count += 1
        if attempt.status == "failed":
            failure_count += 1
        elif status == "downloaded":
            failure_count = 0
    first_seen_at = now if existing is None else existing["first_seen_at"]
    resolved_document_id = document_id or (None if existing is None else existing["document_id"])
    conn.execute(
        """
        INSERT INTO download_resources (
            resource_id, candidate_id, source_name, source_record_id, source_file_id,
            attachment_url, attachment_filename, status, document_id, attempt_count,
            failure_count, last_attempted_at, next_attempt_at, last_http_status,
            last_error_code, last_error_detail, first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_name, source_record_id, source_file_id) DO UPDATE SET
            candidate_id = excluded.candidate_id,
            attachment_url = excluded.attachment_url,
            attachment_filename = excluded.attachment_filename,
            status = excluded.status,
            document_id = excluded.document_id,
            attempt_count = excluded.attempt_count,
            failure_count = excluded.failure_count,
            last_attempted_at = excluded.last_attempted_at,
            next_attempt_at = excluded.next_attempt_at,
            last_http_status = excluded.last_http_status,
            last_error_code = excluded.last_error_code,
            last_error_detail = excluded.last_error_detail,
            first_seen_at = excluded.first_seen_at,
            last_seen_at = excluded.last_seen_at
        """,
        (
            _resource_id(candidate, resource),
            candidate.candidate_id,
            candidate.source_name,
            candidate.source_record_id,
            resource.source_file_id,
            resource.attachment_url,
            resource.attachment_filename,
            status,
            resolved_document_id,
            attempt_count,
            failure_count,
            None if attempt is None else attempt.finished_at.isoformat(),
            None,
            None if attempt is None else attempt.http_status,
            None if attempt is None else attempt.error_code,
            None if attempt is None else attempt.error_detail,
            first_seen_at,
            now,
        ),
    )


def _persist_download_attempt(
    conn: sqlite3.Connection,
    *,
    candidate: DownloadCandidate,
    resource: DownloadResource,
    attempt: DownloadAttemptPersist,
) -> None:
    attempt_id = _stable_id(
        f"download-attempt:{candidate.candidate_id}:{resource.source_file_id}:{attempt.started_at.isoformat()}:{attempt.status}"
    )
    conn.execute(
        """
        INSERT INTO download_attempts (
            attempt_id, candidate_id, source_name, source_record_id, attachment_url,
            status, http_status, error_code, error_detail, document_id, started_at, finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            attempt_id,
            candidate.candidate_id,
            candidate.source_name,
            candidate.source_record_id,
            resource.attachment_url,
            attempt.status,
            attempt.http_status,
            attempt.error_code,
            attempt.error_detail,
            attempt.document_id,
            attempt.started_at.isoformat(),
            attempt.finished_at.isoformat(),
        ),
    )


def _document_type(candidate: DownloadCandidate) -> str:
    if candidate.source_name == "idx_financial_reports":
        if candidate.raw_metadata.get("report_type") == "rda":
            return "annual_report"
        return "financial_statement"
    if candidate.source_name == "idx_disclosures":
        return "disclosure"
    return "other"


def _resources_for_candidate(candidate: DownloadCandidate) -> list[DownloadResource]:
    resources: list[DownloadResource] = [
        DownloadResource(
            source_file_id=candidate.source_record_id,
            attachment_url=candidate.attachment_url,
            attachment_filename=candidate.attachment_filename,
        )
    ]
    if candidate.source_name != "idx_disclosures":
        return resources

    seen_urls = {candidate.attachment_url}
    for attachment in candidate.raw_metadata.get("attachments", []):
        url = attachment.get("url")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        resources.append(
            DownloadResource(
                source_file_id=_stable_id(f"attachment:{url}"),
                attachment_url=url,
                attachment_filename=attachment.get("filename"),
            )
        )
    return resources


def _candidate_has_pending_work(conn: sqlite3.Connection, candidate: DownloadCandidate) -> bool:
    existing_downloads = _existing_downloads_for_candidate(conn, candidate)
    for resource in _resources_for_candidate(candidate):
        state = _resource_state_for_candidate(conn, candidate, resource)
        if state is not None and state.status == "downloaded" and state.document_id is not None:
            continue
        if resource.source_file_id not in existing_downloads:
            return True
    return False


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


def _attempt_from_exception(exc: Exception, *, started_at: datetime, finished_at: datetime) -> DownloadAttemptPersist:
    if isinstance(exc, httpx.HTTPStatusError):
        return DownloadAttemptPersist(
            status="failed",
            started_at=started_at,
            finished_at=finished_at,
            http_status=exc.response.status_code,
            error_code=f"http_{exc.response.status_code}",
            error_detail=str(exc),
        )
    return DownloadAttemptPersist(
        status="failed",
        started_at=started_at,
        finished_at=finished_at,
        error_code=type(exc).__name__,
        error_detail=str(exc),
    )


def _stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _resource_id(candidate: DownloadCandidate, resource: DownloadResource) -> str:
    return _stable_id(f"download-resource:{candidate.source_name}:{candidate.source_record_id}:{resource.source_file_id}")


def _version_group_id(candidate: DownloadCandidate, resource: DownloadResource) -> str:
    return f"version:{candidate.source_name}:{candidate.source_record_id}:{resource.source_file_id}"


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
