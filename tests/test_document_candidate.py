"""Phase 1 deliverables: DocumentCandidate round-trip + adapter contract."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from stock_triage.db import WriterQueue, connect, run_migrations
from stock_triage.sources import DiscoveryWindow, DocumentCandidate, SourceClass
from stock_triage.sources.adapters import (
    IDXCompanyUniverseAdapter,
    IDXDisclosureAdapter,
    IDXFinancialReportAdapter,
)


def _sample_candidate() -> DocumentCandidate:
    return DocumentCandidate(
        source_name="idx_disclosures",
        source_class=SourceClass.DISCLOSURE,
        source_record_id="DISC-2026-0001",
        title="Sample disclosure title",
        ticker="BBCA",
        upstream_published_at=datetime(2026, 4, 1, 9, 30, tzinfo=timezone.utc),
        attachment_url="https://example.invalid/attach.pdf",
        attachment_filename="attach.pdf",
        raw_metadata={"category": "material_information", "lang": "id"},
    )


def test_document_candidate_json_round_trip():
    cand = _sample_candidate()
    payload = cand.to_json()
    parsed = json.loads(payload)
    revived = DocumentCandidate.from_dict(parsed)
    assert revived.source_name == cand.source_name
    assert revived.source_class is SourceClass.DISCLOSURE
    assert revived.source_record_id == cand.source_record_id
    assert revived.upstream_published_at == cand.upstream_published_at
    assert revived.raw_metadata == cand.raw_metadata
    assert revived.discovered_at == cand.discovered_at


def test_document_candidate_requires_identity():
    with pytest.raises(ValueError):
        DocumentCandidate(
            source_name="",
            source_class=SourceClass.DISCLOSURE,
            source_record_id="x",
        )
    with pytest.raises(ValueError):
        DocumentCandidate(
            source_name="idx_disclosures",
            source_class=SourceClass.DISCLOSURE,
            source_record_id="",
        )


def test_document_candidate_persists_to_document_candidates_table(tmp_config):
    cand = _sample_candidate()
    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)

        def _insert(conn):
            conn.execute(
                """
                INSERT INTO document_candidates (
                    candidate_id, source_name, source_record_id, ticker,
                    title, upstream_published_at, attachment_url,
                    raw_metadata, discovered_at, queued
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    f"{cand.source_name}:{cand.source_record_id}",
                    cand.source_name,
                    cand.source_record_id,
                    cand.ticker,
                    cand.title,
                    cand.upstream_published_at.isoformat() if cand.upstream_published_at else None,
                    cand.attachment_url,
                    json.dumps(cand.raw_metadata, sort_keys=True),
                    cand.discovered_at.isoformat(),
                ),
            )

        wq.submit(_insert)

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        row = rconn.execute(
            "SELECT source_name, ticker, raw_metadata FROM document_candidates "
            "WHERE source_record_id = ?",
            (cand.source_record_id,),
        ).fetchone()
        assert row is not None
        assert row["source_name"] == cand.source_name
        assert row["ticker"] == "BBCA"
        assert json.loads(row["raw_metadata"]) == cand.raw_metadata
    finally:
        rconn.close()


def test_discovery_window_serialization():
    from datetime import date

    w = DiscoveryWindow(start=date(2026, 1, 1), end=date(2026, 4, 1), cursor="page=3")
    d = w.to_dict()
    assert d == {"start": "2026-01-01", "end": "2026-04-01", "cursor": "page=3"}

    empty = DiscoveryWindow().to_dict()
    assert empty == {"start": None, "end": None, "cursor": None}


@pytest.mark.parametrize(
    "adapter_cls,expected_class",
    [
        (IDXCompanyUniverseAdapter, SourceClass.COMPANY_UNIVERSE),
        (IDXFinancialReportAdapter, SourceClass.FINANCIAL_REPORT),
        (IDXDisclosureAdapter, SourceClass.DISCLOSURE),
    ],
)
def test_idx_stub_adapter_healthcheck_reports_blocked(adapter_cls, expected_class, tmp_path):
    if adapter_cls is IDXCompanyUniverseAdapter:
        source_dir = tmp_path / "idx_company_profiles"
        source_dir.mkdir(parents=True)
        (source_dir / "page.html").write_text(
            """
            <script>
            window.__NUXT__={fetch:{"data-v-ac7150ac:0":{tableData:{data:[
              {KodeEmiten:"AADI",NamaEmiten:"PT Adaro",TanggalPencatatan:"2024-12-05T00:00:00"}
            ]}}}}
            </script>
            """,
            encoding="utf-8",
        )
        a = adapter_cls(artifacts_root=tmp_path)
    elif adapter_cls is IDXDisclosureAdapter:
        source_dir = tmp_path / "idx_disclosure"
        source_dir.mkdir(parents=True)
        (source_dir / "page.html").write_text(
            """
            <div class="attach-card pb-16 mb-24">
              <time class="text-small">08 April 2026 22:53:46</time>
              <h6 class="f-m-20 title">
                <a href="https://www.idx.co.id/StaticData/NewsAndAnnouncement/ANNOUNCEMENTSTOCK/From_EREP/202604/main.pdf">
                  Invitation of Annual General Meeting of Shareholders [<span>BRMS</span>]
                </a>
              </h6>
            </div>
            """,
            encoding="utf-8",
        )
        a = adapter_cls(artifacts_root=tmp_path)
    else:
        source_dir = tmp_path / "idx_financial_statements_annual_report" / "responses"
        source_dir.mkdir(parents=True)
        (source_dir / "sample.json").write_text(
            json.dumps(
                {
                    "Search": {"year": "2025"},
                    "ResultCount": 1,
                    "Results": [
                        {
                            "KodeEmiten": "BNBR",
                            "NamaEmiten": "Bakrie & Brothers Tbk",
                            "Attachments": [
                                {
                                    "File_ID": "file-1",
                                    "File_Modified": "2026-04-08T23:45:55.517",
                                    "File_Name": "FinancialStatement-2025-Tahunan-BNBR.pdf",
                                    "File_Path": "/Portals/0/StaticData/file.pdf",
                                    "File_Size": 902729,
                                    "File_Type": ".pdf",
                                    "Report_Period": "Audit",
                                    "Report_Type": "rdf",
                                }
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        a = adapter_cls(artifacts_root=tmp_path)
    h = a.healthcheck()
    assert h["source_class"] == expected_class.value
    assert h["status"] == "ok"
    assert h["accessible"] is True
    assert h["public_url"].startswith("https://www.idx.co.id/")
