from __future__ import annotations

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
    url = IDXFinancialReportAdapter._build_api_url(year=2025, page_size=50)
    assert "year=2025" in url
    assert "pageSize=50" in url
    assert "reportType=rdf" in url
