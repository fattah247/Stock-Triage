from __future__ import annotations

import httpx

from stock_triage.__main__ import main
from stock_triage.db import WriterQueue, connect, configure_pragmas, run_migrations
from stock_triage.discovery import persist_document_candidates
from stock_triage.download import run_downloads
from stock_triage.download.pipeline import AttachmentDownloader, DownloadCandidate
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
            "SELECT source_name, source_record_id FROM document_versions WHERE version_group_id = ?",
            (row["version_group_id"],),
        ).fetchone()
        assert version is not None
        assert version["source_name"] == "idx_disclosures"
        assert version["source_record_id"] == "disc-1"
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
            existing=type("Existing", (), {"etag": '"v1"', "last_modified": None, "document_id": "doc-1"})(),
        )
    finally:
        client.close()

    assert fetched is not None
    assert fetched.mime_type == "application/pdf"
    assert fetched.etag == '"v2"'
    assert fetched.stored_file.local_path.exists()
