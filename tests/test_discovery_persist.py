from __future__ import annotations

from datetime import date

from stock_triage.db import WriterQueue, connect, run_migrations
from stock_triage.discovery import load_watermark, persist_companies, persist_document_candidates, persist_watermark
from stock_triage.sources import DiscoveryWindow, DocumentCandidate, SourceClass


def test_persist_companies_and_watermark(tmp_config):
    candidates = [
        DocumentCandidate(
            source_name="idx_company_universe",
            source_class=SourceClass.COMPANY_UNIVERSE,
            source_record_id="BBCA",
            title="PT Bank Central Asia Tbk.",
            ticker="BBCA",
            raw_metadata={
                "issuer_name": "PT Bank Central Asia Tbk.",
                "listing_date": "2000-05-31T00:00:00",
                "profile_url": "https://www.idx.co.id/en/listed-companies/company-profiles/BBCA",
            },
        )
    ]
    window = DiscoveryWindow(start=date(2026, 1, 1), end=date(2026, 4, 9))

    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)
        result = wq.submit(lambda conn: persist_companies(conn, candidates))
        assert result.inserted == 1
        wq.submit(
            lambda conn: persist_watermark(
                conn,
                source_name="idx_company_universe",
                source_class=SourceClass.COMPANY_UNIVERSE,
                window=window,
                raw_state={"candidate_count": 1},
            )
        )

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        row = rconn.execute(
            "SELECT issuer_name, listing_date FROM companies WHERE ticker = ?",
            ("BBCA",),
        ).fetchone()
        assert row["issuer_name"] == "PT Bank Central Asia Tbk."
        assert row["listing_date"] == "2000-05-31T00:00:00"

        loaded = load_watermark(rconn, "idx_company_universe")
        assert loaded == window
    finally:
        rconn.close()


def test_persist_document_candidates_upserts(tmp_config):
    first = DocumentCandidate(
        source_name="idx_disclosures",
        source_class=SourceClass.DISCLOSURE,
        source_record_id="disc-1",
        title="Advertisement Submission",
        ticker="BRMS",
        attachment_url="https://example.invalid/1.pdf",
        raw_metadata={"attachments": [{"filename": "1.pdf"}]},
    )
    updated = DocumentCandidate(
        source_name="idx_disclosures",
        source_class=SourceClass.DISCLOSURE,
        source_record_id="disc-1",
        title="Advertisement Submission Updated",
        ticker="BRMS",
        attachment_url="https://example.invalid/2.pdf",
        raw_metadata={"attachments": [{"filename": "2.pdf"}]},
    )

    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)
        first_result = wq.submit(lambda conn: persist_document_candidates(conn, [first]))
        second_result = wq.submit(lambda conn: persist_document_candidates(conn, [updated]))
        assert first_result.inserted == 1
        assert second_result.updated == 1

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        row = rconn.execute(
            "SELECT title, attachment_url, raw_metadata FROM document_candidates WHERE candidate_id = ?",
            ("idx_disclosures:disc-1",),
        ).fetchone()
        assert row["title"] == "Advertisement Submission Updated"
        assert row["attachment_url"] == "https://example.invalid/2.pdf"
        assert "2.pdf" in row["raw_metadata"]
    finally:
        rconn.close()
