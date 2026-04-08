from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from stock_triage.sources import DiscoveryWindow, SourceClass
from stock_triage.sources.adapters import (
    IDXCompanyUniverseAdapter,
    IDXDisclosureAdapter,
    IDXFinancialReportAdapter,
)
from stock_triage.sources.idx_artifacts import (
    parse_company_profiles_html,
    parse_disclosure_html,
    parse_financial_report_json,
)


def test_parse_company_profiles_html_extracts_candidates():
    html = """
    <script>
    window.__NUXT__={fetch:{"data-v-ac7150ac:0":{tableData:{data:[
      {KodeEmiten:"AADI",NamaEmiten:"PT Adaro Andalan Indonesia Tbk",TanggalPencatatan:"2024-12-05T00:00:00",Website:"www.adaroindonesia.com",Email:"corsec@adaroindonesia.com",Telepon:"(021) 2553 3065"},
      {KodeEmiten:"BBCA",NamaEmiten:"PT Bank Central Asia Tbk.",TanggalPencatatan:"2000-05-31T00:00:00",Website:"www.bca.co.id",Email:"investor_relations@bca.co.id",Telepon:"021-23588000"}
    ]}}}
    </script>
    """
    candidates = parse_company_profiles_html(html)
    assert [c.ticker for c in candidates] == ["AADI", "BBCA"]
    assert candidates[0].source_class is SourceClass.COMPANY_UNIVERSE
    assert candidates[1].raw_metadata["website"] == "www.bca.co.id"


def test_parse_disclosure_html_extracts_candidates():
    html = """
    <div class="attach-card pb-16 mb-24">
      <time class="text-small">08 April 2026 22:53:46</time>
      <h6 class="f-m-20 title">
        <a href="https://www.idx.co.id/StaticData/NewsAndAnnouncement/ANNOUNCEMENTSTOCK/From_EREP/202604/main.pdf">
          Invitation of Annual General Meeting of Shareholders [<span>BRMS</span>]
        </a>
      </h6>
      <ul class="list-nostyle">
        <li><a href="https://www.idx.co.id/StaticData/NewsAndAnnouncement/ANNOUNCEMENTSTOCK/From_EREP/202604/att1.pdf" download="20260408_BRMS_att1.pdf"></a></li>
        <li><a href="https://www.idx.co.id/StaticData/NewsAndAnnouncement/ANNOUNCEMENTSTOCK/From_EREP/202604/att2.pdf" download="20260408_BRMS_att2.pdf"></a></li>
      </ul>
    </div>
    """
    candidates = parse_disclosure_html(html)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.source_class is SourceClass.DISCLOSURE
    assert candidate.ticker == "BRMS"
    assert candidate.title == "Invitation of Annual General Meeting of Shareholders"
    assert len(candidate.raw_metadata["attachments"]) == 2


def test_idx_company_universe_adapter_reads_artifact(tmp_path):
    source_dir = tmp_path / "idx_company_profiles"
    source_dir.mkdir(parents=True)
    (source_dir / "page.html").write_text(
        '<script>{KodeEmiten:"AADI",NamaEmiten:"PT Adaro",TanggalPencatatan:"2024-12-05T00:00:00"}</script>',
        encoding="utf-8",
    )
    adapter = IDXCompanyUniverseAdapter(artifacts_root=tmp_path)
    candidates = list(adapter.discover(DiscoveryWindow()))
    assert len(candidates) == 1
    assert candidates[0].ticker == "AADI"


def test_idx_disclosure_adapter_reads_artifact(tmp_path):
    source_dir = tmp_path / "idx_disclosure"
    source_dir.mkdir(parents=True)
    (source_dir / "page.html").write_text(
        """
        <div class="attach-card">
          <time>08 April 2026 22:53:46</time>
          <h6 class="title"><a href="https://www.idx.co.id/StaticData/NewsAndAnnouncement/ANNOUNCEMENTSTOCK/From_EREP/202604/main.pdf">
            Advertisement Submission [<span>BRMS</span>]
          </a></h6>
          <ul><li><a href="https://www.idx.co.id/StaticData/NewsAndAnnouncement/ANNOUNCEMENTSTOCK/From_EREP/202604/att1.pdf" download="file1.pdf"></a></li></ul>
        </div>
        """,
        encoding="utf-8",
    )
    adapter = IDXDisclosureAdapter(artifacts_root=tmp_path)
    candidates = list(adapter.discover(DiscoveryWindow()))
    assert len(candidates) == 1
    assert candidates[0].title == "Advertisement Submission"


def test_parse_financial_report_json_extracts_candidates():
    payload = {
        "Search": {"year": "2025"},
        "ResultCount": 1,
        "Results": [
            {
                "KodeEmiten": "BNBR",
                "NamaEmiten": "Bakrie & Brothers Tbk",
                "Report_Period": "Audit",
                "Report_Year": "2025",
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
    candidates = parse_financial_report_json(payload)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.source_class is SourceClass.FINANCIAL_REPORT
    assert candidate.source_record_id == "file-1"
    assert candidate.attachment_url == "https://www.idx.co.id/Portals/0/StaticData/file.pdf"
    assert candidate.ticker == "BNBR"


def test_financial_report_api_url_builder():
    url = IDXFinancialReportAdapter._build_api_url(
        year=2025,
        page_size=50,
        index_from=2,
        report_type="rda",
    )
    assert "year=2025" in url
    assert "pageSize=50" in url
    assert "indexFrom=2" in url
    assert "reportType=rda" in url


def test_financial_report_total_pages():
    assert IDXFinancialReportAdapter._total_pages(result_count=802, page_size=100) == 9
    assert IDXFinancialReportAdapter._total_pages(result_count=120, page_size=100) == 2
    assert IDXFinancialReportAdapter._total_pages(result_count=0, page_size=100) == 1


def test_financial_report_adapter_paginates_and_combines_report_types(tmp_path, monkeypatch):
    payloads = {
        ("rdf", 1): {
            "ResultCount": 150,
            "Results": [
                {
                    "KodeEmiten": "AAA",
                    "NamaEmiten": "Issuer AAA",
                    "Report_Period": "Audit",
                    "Report_Year": "2025",
                    "Attachments": [
                        {
                            "File_ID": "rdf-1",
                            "File_Modified": "2026-04-08T23:45:55.517",
                            "File_Name": "rdf-1.pdf",
                            "File_Path": "/files/rdf-1.pdf",
                            "Report_Type": "rdf",
                        }
                    ],
                }
            ],
        },
        ("rdf", 2): {
            "ResultCount": 150,
            "Results": [
                {
                    "KodeEmiten": "BBB",
                    "NamaEmiten": "Issuer BBB",
                    "Report_Period": "Audit",
                    "Report_Year": "2025",
                    "Attachments": [
                        {
                            "File_ID": "rdf-2",
                            "File_Modified": "2026-04-07T23:45:55.517",
                            "File_Name": "rdf-2.pdf",
                            "File_Path": "/files/rdf-2.pdf",
                            "Report_Type": "rdf",
                        }
                    ],
                }
            ],
        },
        ("rda", 1): {
            "ResultCount": 20,
            "Results": [
                {
                    "KodeEmiten": "CCC",
                    "NamaEmiten": "Issuer CCC",
                    "Report_Period": "Audit",
                    "Report_Year": "2025",
                    "Attachments": [
                        {
                            "File_ID": "rda-1",
                            "File_Modified": "2026-04-06T23:45:55.517",
                            "File_Name": "rda-1.pdf",
                            "File_Path": "/files/rda-1.pdf",
                            "Report_Type": "rda",
                        }
                    ],
                }
            ],
        },
    }

    class FakeSession:
        fetched_urls: list[str] = []

        def __init__(self, **_kwargs):
            self.counter = 0

        def fetch_json(self, *, source_id, page_url, api_url):
            del source_id, page_url
            FakeSession.fetched_urls.append(api_url)
            return self._result_for(api_url)

        def fetch_json_many(self, *, source_id, page_url, api_urls):
            del source_id, page_url
            out = []
            for api_url in api_urls:
                FakeSession.fetched_urls.append(api_url)
                out.append(self._result_for(api_url))
            return out

        def _result_for(self, api_url):
            self.counter += 1
            qs = parse_qs(urlparse(api_url).query)
            key = (qs["reportType"][0], int(qs["indexFrom"][0]))
            body_path = tmp_path / f"payload-{self.counter}.json"
            body_path.write_text(json.dumps(payloads[key]), encoding="utf-8")
            return SimpleNamespace(status=200, api_url=api_url, body_path=body_path)

    fake_config = SimpleNamespace(
        browser=SimpleNamespace(
            profile_dir=tmp_path / "profile",
            artifacts_dir=tmp_path / "artifacts",
            headless=True,
            challenge_timeout_s=1,
            page_settle_ms=1,
            channel="",
        )
    )
    monkeypatch.setattr("stock_triage.sources.adapters.idx_financial_reports.PlaywrightSession", FakeSession)
    monkeypatch.setattr("stock_triage.sources.adapters.idx_financial_reports.load_config", lambda: fake_config)

    adapter = IDXFinancialReportAdapter()
    candidates = list(
        adapter.discover(
            DiscoveryWindow(
                start=date(2025, 1, 1),
                end=date(2025, 12, 31),
            )
        )
    )

    assert [candidate.source_record_id for candidate in candidates] == ["rdf-1", "rdf-2", "rda-1"]
    assert any("reportType=rda" in url for url in FakeSession.fetched_urls)
    assert any("indexFrom=2" in url for url in FakeSession.fetched_urls)
