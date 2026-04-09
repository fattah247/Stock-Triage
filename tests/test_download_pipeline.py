from __future__ import annotations

import httpx

from stock_triage.__main__ import main
from stock_triage.db import WriterQueue, connect, configure_pragmas, run_migrations
from stock_triage.discovery import persist_document_candidates
from stock_triage.download import run_downloads
from stock_triage.download.pipeline import AttachmentDownloader, DownloadCandidate, DownloadResource
from stock_triage.sources import DocumentCandidate, SourceClass


def test_run_downloads_persists_documents_and_version_groups(tmp_config):
    candidate = DocumentCandidate(
        source_name="idx_disclosures",
        source_class=SourceClass.DISCLOSURE,
        source_record_id="disc-1",
        ticker="BRMS",
        title="Advertisement Submission",
        attachment_url="https://example.test/disclosures/disc-1.pdf",
        attachment_filename="disc-1.pdf",
        raw_metadata={"filename": "disc-1.pdf"},
    )

    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)
        wq.submit(lambda conn: persist_document_candidates(conn, [candidate]))

    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"Content-Type": "application/pdf", "ETag": '"disc-1-v1"'},
                content=b"%PDF-1.4 disclosure",
                request=request,
            )
        )
    )
    try:
        result = run_downloads(tmp_config, source_name="idx_disclosures", limit=10, client=client)
    finally:
        client.close()

    assert result.selected == 1
    assert result.fetched == 1
    assert result.inserted == 1
    assert result.failed == 0

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        row = rconn.execute(
            """
            SELECT document_type, process_state, ticker, mime_type, local_path, sha256, version_group_id
            FROM documents
            WHERE source_name = 'idx_disclosures' AND source_record_id = 'disc-1'
            """
        ).fetchone()
        assert row is not None
        assert row["document_type"] == "disclosure"
        assert row["process_state"] == "downloaded"
        assert row["ticker"] == "BRMS"
        assert row["mime_type"] == "application/pdf"
        assert row["sha256"]

        version = rconn.execute(
            "SELECT source_name, source_record_id, source_file_id FROM document_versions WHERE version_group_id = ?",
            (row["version_group_id"],),
        ).fetchone()
        assert version is not None
        assert version["source_name"] == "idx_disclosures"
        assert version["source_record_id"] == "disc-1"
        assert version["source_file_id"] == "disc-1"
        attempt = rconn.execute(
            """
            SELECT status, document_id
            FROM download_attempts
            WHERE candidate_id = 'idx_disclosures:disc-1'
            ORDER BY finished_at DESC
            LIMIT 1
            """
        ).fetchone()
        assert attempt is not None
        assert attempt["status"] == "downloaded"
        assert attempt["document_id"] is not None
    finally:
        rconn.close()


def test_run_downloads_uses_conditional_headers_and_handles_not_modified(tmp_config):
    candidate = DocumentCandidate(
        source_name="idx_financial_reports",
        source_class=SourceClass.FINANCIAL_REPORT,
        source_record_id="file-1",
        ticker="BNBR",
        title="FinancialStatement-2025-Tahunan-BNBR.pdf",
        attachment_url="https://example.test/reports/file-1.pdf",
        attachment_filename="file-1.pdf",
        raw_metadata={"file_name": "file-1.pdf", "report_type": "rda", "file_modified": "2026-04-09T00:00:00"},
    )

    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)
        wq.submit(lambda conn: persist_document_candidates(conn, [candidate]))

    first_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={
                    "Content-Type": "application/pdf",
                    "ETag": '"file-1-v1"',
                    "Last-Modified": "Wed, 09 Apr 2026 00:00:00 GMT",
                },
                content=b"%PDF-1.4 annual report",
                request=request,
            )
        )
    )
    try:
        first = run_downloads(tmp_config, source_name="idx_financial_reports", limit=10, client=first_client)
    finally:
        first_client.close()

    assert first.inserted == 1

    def _not_modified(request: httpx.Request) -> httpx.Response:
        assert request.headers["If-None-Match"] == '"file-1-v1"'
        assert request.headers["If-Modified-Since"] == "Wed, 09 Apr 2026 00:00:00 GMT"
        return httpx.Response(304, request=request)

    second_client = httpx.Client(transport=httpx.MockTransport(_not_modified))
    try:
        second = run_downloads(
            tmp_config,
            source_name="idx_financial_reports",
            limit=10,
            include_existing=True,
            client=second_client,
        )
    finally:
        second_client.close()

    assert second.selected == 1
    assert second.fetched == 0
    assert second.inserted == 0
    assert second.not_modified == 1

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        row = rconn.execute(
            """
            SELECT COUNT(*) AS n, MAX(document_type) AS document_type
            FROM documents
            WHERE source_name = 'idx_financial_reports' AND source_record_id = 'file-1'
            """
        ).fetchone()
        assert row["n"] == 1
        assert row["document_type"] == "annual_report"
    finally:
        rconn.close()


def test_run_downloads_skips_existing_documents_by_default(tmp_config):
    candidate = DocumentCandidate(
        source_name="idx_disclosures",
        source_class=SourceClass.DISCLOSURE,
        source_record_id="disc-1",
        ticker="BRMS",
        title="Advertisement Submission",
        attachment_url="https://example.test/disclosures/disc-1.pdf",
        attachment_filename="disc-1.pdf",
        raw_metadata={"filename": "disc-1.pdf"},
    )

    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)
        wq.submit(lambda conn: persist_document_candidates(conn, [candidate]))

    first_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"Content-Type": "application/pdf"},
                content=b"%PDF-1.4 disclosure",
                request=request,
            )
        )
    )
    try:
        first = run_downloads(tmp_config, source_name="idx_disclosures", limit=10, client=first_client)
    finally:
        first_client.close()

    assert first.inserted == 1

    second_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(AssertionError("existing candidates should not be fetched"))
        )
    )
    try:
        second = run_downloads(tmp_config, source_name="idx_disclosures", limit=10, client=second_client)
    finally:
        second_client.close()

    assert second.selected == 0
    assert second.fetched == 0
    assert second.inserted == 0
    assert second.skipped == 0


def test_run_downloads_include_existing_reconsiders_prior_documents(tmp_config):
    candidate = DocumentCandidate(
        source_name="idx_disclosures",
        source_class=SourceClass.DISCLOSURE,
        source_record_id="disc-1",
        ticker="BRMS",
        title="Advertisement Submission",
        attachment_url="https://example.test/disclosures/disc-1.pdf",
        attachment_filename="disc-1.pdf",
        raw_metadata={"filename": "disc-1.pdf"},
    )

    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)
        wq.submit(lambda conn: persist_document_candidates(conn, [candidate]))

    first_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"Content-Type": "application/pdf"},
                content=b"%PDF-1.4 disclosure v1",
                request=request,
            )
        )
    )
    try:
        first = run_downloads(tmp_config, source_name="idx_disclosures", limit=10, client=first_client)
    finally:
        first_client.close()

    assert first.inserted == 1

    second_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"Content-Type": "application/pdf"},
                content=b"%PDF-1.4 disclosure v2",
                request=request,
            )
        )
    )
    try:
        second = run_downloads(
            tmp_config,
            source_name="idx_disclosures",
            limit=10,
            include_existing=True,
            client=second_client,
        )
    finally:
        second_client.close()

    assert second.selected == 1
    assert second.fetched == 1
    assert second.inserted == 1

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        row = rconn.execute(
            """
            SELECT COUNT(*) AS n
            FROM documents
            WHERE source_name = 'idx_disclosures' AND source_record_id = 'disc-1'
            """
        ).fetchone()
        assert row["n"] == 2
    finally:
        rconn.close()


def test_disclosure_candidate_remains_pending_until_attachments_downloaded(tmp_config):
    candidate = DocumentCandidate(
        source_name="idx_disclosures",
        source_class=SourceClass.DISCLOSURE,
        source_record_id="disc-attach",
        ticker="BGTG",
        title="Disclosure With Attachments",
        attachment_url="https://example.test/disclosures/main.pdf",
        attachment_filename="main.pdf",
        raw_metadata={
            "attachments": [
                {"filename": "att-1.pdf", "url": "https://example.test/disclosures/att-1.pdf"},
                {"filename": "att-2.pdf", "url": "https://example.test/disclosures/att-2.pdf"},
            ]
        },
    )

    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)
        wq.submit(lambda conn: persist_document_candidates(conn, [candidate]))

    calls: list[str] = []

    def _transport(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url).endswith("main.pdf"):
            return httpx.Response(200, headers={"Content-Type": "application/pdf"}, content=b"%PDF-1.4 main", request=request)
        if str(request.url).endswith("att-1.pdf"):
            return httpx.Response(200, headers={"Content-Type": "application/pdf"}, content=b"%PDF-1.4 att1", request=request)
        return httpx.Response(200, headers={"Content-Type": "application/pdf"}, content=b"%PDF-1.4 att2", request=request)

    first_client = httpx.Client(transport=httpx.MockTransport(_transport))
    try:
        first = run_downloads(tmp_config, source_name="idx_disclosures", limit=10, client=first_client)
    finally:
        first_client.close()

    assert first.selected == 1
    assert first.fetched == 3
    assert first.inserted == 3
    assert len(calls) == 3

    second_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(AssertionError("fully downloaded disclosure should not be selected"))
        )
    )
    try:
        second = run_downloads(tmp_config, source_name="idx_disclosures", limit=10, client=second_client)
    finally:
        second_client.close()

    assert second.selected == 0

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        row = rconn.execute(
            """
            SELECT COUNT(DISTINCT version_group_id) AS groups, COUNT(*) AS docs
            FROM documents
            WHERE source_name = 'idx_disclosures' AND source_record_id = 'disc-attach'
            """
        ).fetchone()
        assert row["docs"] == 3
        assert row["groups"] == 3
    finally:
        rconn.close()


def test_disclosure_allows_same_bytes_for_multiple_attachment_resources(tmp_config):
    candidate = DocumentCandidate(
        source_name="idx_disclosures",
        source_class=SourceClass.DISCLOSURE,
        source_record_id="disc-dup",
        ticker="BGTG",
        title="Disclosure With Duplicate Attachment Bytes",
        attachment_url="https://example.test/disclosures/main.pdf",
        attachment_filename="main.pdf",
        raw_metadata={
            "attachments": [
                {"filename": "att-1.pdf", "url": "https://example.test/disclosures/att-1.pdf"},
            ]
        },
    )

    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)
        wq.submit(lambda conn: persist_document_candidates(conn, [candidate]))

    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"Content-Type": "application/pdf"},
                content=b"%PDF-1.4 same bytes",
                request=request,
            )
        )
    )
    try:
        result = run_downloads(tmp_config, source_name="idx_disclosures", limit=10, client=client)
    finally:
        client.close()

    assert result.inserted == 2

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        row = rconn.execute(
            """
            SELECT COUNT(*) AS docs, COUNT(DISTINCT source_file_id) AS resources, COUNT(DISTINCT version_group_id) AS groups
            FROM documents
            WHERE source_name = 'idx_disclosures' AND source_record_id = 'disc-dup'
            """
        ).fetchone()
        assert row["docs"] == 2
        assert row["resources"] == 2
        assert row["groups"] == 2
    finally:
        rconn.close()


def test_run_downloads_persists_failed_attempts(tmp_config):
    candidate = DocumentCandidate(
        source_name="idx_disclosures",
        source_class=SourceClass.DISCLOSURE,
        source_record_id="disc-fail",
        ticker="BRMS",
        title="Advertisement Submission",
        attachment_url="https://example.test/disclosures/disc-fail.pdf",
        attachment_filename="disc-fail.pdf",
        raw_metadata={"filename": "disc-fail.pdf"},
    )

    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)
        wq.submit(lambda conn: persist_document_candidates(conn, [candidate]))

    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(404, request=request)
        )
    )
    try:
        result = run_downloads(tmp_config, source_name="idx_disclosures", limit=10, client=client)
    finally:
        client.close()

    assert result.failed == 1

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        attempt = rconn.execute(
            """
            SELECT status, http_status, error_code, document_id
            FROM download_attempts
            WHERE candidate_id = 'idx_disclosures:disc-fail'
            ORDER BY finished_at DESC
            LIMIT 1
            """
        ).fetchone()
        assert attempt is not None
        assert attempt["status"] == "failed"
        assert attempt["http_status"] == 404
        assert attempt["error_code"] == "http_404"
        assert attempt["document_id"] is None
    finally:
        rconn.close()


def test_download_cli_uses_run_downloads(tmp_config, monkeypatch, capsys):
    monkeypatch.setattr("stock_triage.__main__.load_config", lambda: tmp_config)
    monkeypatch.setattr(
        "stock_triage.__main__.run_downloads",
        lambda cfg, source_name=None, limit=25, include_existing=False: type(
            "R",
            (),
            {
                "selected": 2,
                "fetched": 1,
                "inserted": 1,
                "skipped": 0,
                "not_modified": 1,
                "failed": 0,
            },
        )(),
    )

    rc = main(["download", "--source", "idx_disclosures", "--limit", "2"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "selected=2" in out
    assert "not_modified=1" in out


def test_attachment_downloader_uses_browser_session_for_idx_urls(tmp_config, monkeypatch):
    class FakeSession:
        def __init__(self, **_kwargs):
            pass

        def fetch_resource(self, *, source_id, page_url, resource_url, headers=None, wait_selector=None):
            assert source_id == "idx_financial_reports"
            assert "financial-statements-and-annual-report" in page_url
            assert resource_url == "https://www.idx.co.id/Portals/0/StaticData/example.pdf"
            assert headers is None
            assert wait_selector is None
            return type(
                "BrowserResult",
                (),
                {
                    "status": 200,
                    "final_url": resource_url,
                    "content_type": "application/pdf",
                    "headers": {"etag": '"v2"'},
                    "body": b"%PDF-1.4 browser fetch",
                },
            )()

    monkeypatch.setattr("stock_triage.download.pipeline.PlaywrightSession", FakeSession)

    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(AssertionError("httpx client should not be used"))
        )
    )
    try:
        downloader = AttachmentDownloader(tmp_config, client=client)
        fetched = downloader.fetch(
            DownloadCandidate(
                candidate_id="idx_financial_reports:file-1",
                source_name="idx_financial_reports",
                source_record_id="file-1",
                ticker="BNBR",
                title="Report",
                attachment_url="https://www.idx.co.id/Portals/0/StaticData/example.pdf",
                attachment_filename="example.pdf",
                upstream_published_at=None,
                raw_metadata={"report_type": "rdf"},
            ),
            DownloadResource(
                source_file_id="file-1",
                attachment_url="https://www.idx.co.id/Portals/0/StaticData/example.pdf",
                attachment_filename="example.pdf",
            ),
            existing=type("Existing", (), {"etag": '"v1"', "last_modified": None, "document_id": "doc-1"})(),
        )
    finally:
        client.close()

    assert fetched is not None
    assert fetched.mime_type == "application/pdf"
    assert fetched.etag == '"v2"'
    assert fetched.stored_file.local_path.exists()
