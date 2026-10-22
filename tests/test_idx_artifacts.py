from __future__ import annotations

import json
from contextlib import contextmanager
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
    parse_company_profile_rows,
    parse_company_profiles_html,
    parse_disclosure_html,
    parse_financial_report_json,
)

_EMPTY_PAGE = {"ResultCount": 0, "Results": []}


def test_parse_company_profiles_html_extracts_candidates():
    html = """
    <script>
    window.__NUXT__={fetch:{"data-v-ac7150ac:0":{tableData:{data:[
      {KodeEmiten:"AADI",NamaEmiten:"PT Adaro Andalan Indonesia Tbk",TanggalPencatatan:"2024-12-05T00:00:00",Website:"www.adaroindonesia.com",Email:"corsec@adaroindonesia.com",Telepon:"(021) 2553 3065",Fax:"(021) 2553 3066",Alamat:"Cyber 2 Tower Lantai 26\\nJakarta",KegiatanUsahaUtama:"Coal mining and holding",NPWP:"02.433.115.9-091.000",Logo:"\\u002FPortals\\u002F0\\u002FStaticData\\u002FListedCompanies\\u002FLogoEmiten\\u002FAADI.jpg"},
      {KodeEmiten:"BBCA",NamaEmiten:"PT Bank Central Asia Tbk.",TanggalPencatatan:"2000-05-31T00:00:00",Website:"www.bca.co.id",Email:"investor_relations@bca.co.id",Telepon:"021-23588000"}
    ]}}}
    </script>
    """
    candidates = parse_company_profiles_html(html)
    assert [c.ticker for c in candidates] == ["AADI", "BBCA"]
    assert candidates[0].source_class is SourceClass.COMPANY_UNIVERSE
    assert candidates[1].raw_metadata["website"] == "www.bca.co.id"
    assert candidates[0].raw_metadata["address"] == "Cyber 2 Tower Lantai 26\nJakarta"
    assert candidates[0].raw_metadata["fax"] == "(021) 2553 3066"
    assert candidates[0].raw_metadata["npwp"] == "02.433.115.9-091.000"
    assert candidates[0].raw_metadata["business_activity"] == "Coal mining and holding"
    assert candidates[0].raw_metadata["logo_url"] == "https://www.idx.co.id/Portals/0/StaticData/ListedCompanies/LogoEmiten/AADI.jpg"


def test_parse_company_profile_rows_extracts_enriched_candidates():
    candidates = parse_company_profile_rows(
        [
            {
                "KodeEmiten": "AADI",
                "NamaEmiten": "PT Adaro Andalan Indonesia Tbk",
                "TanggalPencatatan": "2024-12-05T00:00:00",
                "Website": "www.adaroindonesia.com",
                "Email": "corsec@adaroindonesia.com",
                "Telepon": "(021) 2553 3065",
                "Fax": "(021) 2553 3066",
                "Alamat": "Cyber 2 Tower Lantai 26\nJakarta",
                "BAE": "PT. Datindo Entrycom",
                "Industri": "Batu Bara",
                "SubIndustri": "Produksi Batu Bara",
                "Sektor": "Energi",
                "SubSektor": "Minyak, Gas & Batu Bara",
                "KegiatanUsahaUtama": "Coal mining and holding",
                "NPWP": "02.433.115.9-091.000",
                "NPKP": "01.111.222.3-444.000",
                "PapanPencatatan": "Utama",
                "Logo": "/Portals/0/StaticData/ListedCompanies/LogoEmiten/AADI.jpg",
                "Status": 0,
                "EfekEmiten_Saham": True,
                "EfekEmiten_Obligasi": False,
                "EfekEmiten_ETF": False,
                "EfekEmiten_EBA": False,
                "EfekEmiten_SPEI": False,
            }
        ]
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.ticker == "AADI"
    assert candidate.raw_metadata["share_registrar"] == "PT. Datindo Entrycom"
    assert candidate.raw_metadata["industry"] == "Batu Bara"
    assert candidate.raw_metadata["listing_board"] == "Utama"
    assert candidate.raw_metadata["listed_instruments"] == ["stock"]
    assert candidate.raw_metadata["status_code"] == 0


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


def test_idx_company_universe_adapter_reads_live_rows(tmp_path, monkeypatch):
    class FakePage:
        def evaluate(self, script):
            del script
            return [
                {
                    "KodeEmiten": "AADI",
                    "NamaEmiten": "PT Adaro Andalan Indonesia Tbk",
                    "TanggalPencatatan": "2024-12-05T00:00:00",
                    "Website": "www.adaroindonesia.com",
                    "Email": "corsec@adaroindonesia.com",
                    "Telepon": "(021) 2553 3065",
                    "Alamat": "Cyber 2 Tower Lantai 26\nJakarta",
                    "Sektor": "Energi",
                    "PapanPencatatan": "Utama",
                }
            ]

        def content(self):
            raise AssertionError("content() should not be used when live rows are available")

    class FakeSession:
        def __init__(self, **_kwargs):
            self.page = FakePage()

        @contextmanager
        def open_page(self, *, page_url):
            assert page_url == IDXCompanyUniverseAdapter.public_url
            yield self.page

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
    monkeypatch.setattr("stock_triage.sources.adapters.idx_company_universe.PlaywrightSession", FakeSession)
    monkeypatch.setattr("stock_triage.sources.adapters.idx_company_universe.load_config", lambda: fake_config)

    adapter = IDXCompanyUniverseAdapter()
    candidates = list(adapter.discover(DiscoveryWindow()))

    assert len(candidates) == 1
    assert candidates[0].ticker == "AADI"
    assert candidates[0].raw_metadata["sector"] == "Energi"


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
            self.page = SimpleNamespace(url=IDXFinancialReportAdapter.public_url)

        @contextmanager
        def open_page(self, *, page_url):
            del page_url
            yield self.page

        def fetch_json_from_page(self, *, source_id, page, api_url):
            del source_id, page
            FakeSession.fetched_urls.append(api_url)
            return self._result_for(api_url)

        def _result_for(self, api_url):
            self.counter += 1
            qs = parse_qs(urlparse(api_url).query)
            key = (qs["reportType"][0], int(qs["indexFrom"][0]))
            if qs["periode"][0] != "audit":
                key = (qs["reportType"][0], qs["periode"][0], int(qs["indexFrom"][0]))
            body_path = tmp_path / f"payload-{self.counter}.json"
            body_path.write_text(json.dumps(payloads.get(key, _EMPTY_PAGE)), encoding="utf-8")
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


def test_financial_report_adapter_stops_at_saved_cursors(tmp_path, monkeypatch):
    payloads = {
        ("rdf", 1): {
            "ResultCount": 200,
            "Results": [
                {
                    "KodeEmiten": "AAA",
                    "NamaEmiten": "Issuer AAA",
                    "Report_Period": "Audit",
                    "Report_Year": "2025",
                    "Attachments": [
                        {
                            "File_ID": "rdf-cursor",
                            "File_Modified": "2026-04-08T23:45:55.517",
                            "File_Name": "rdf-cursor.pdf",
                            "File_Path": "/files/rdf-cursor.pdf",
                            "Report_Type": "rdf",
                        }
                    ],
                }
            ],
        },
        ("rda", 1): {
            "ResultCount": 120,
            "Results": [
                {
                    "KodeEmiten": "BBB",
                    "NamaEmiten": "Issuer BBB",
                    "Report_Period": "Audit",
                    "Report_Year": "2025",
                    "Attachments": [
                        {
                            "File_ID": "rda-new",
                            "File_Modified": "2026-04-08T21:00:00.000",
                            "File_Name": "rda-new.pdf",
                            "File_Path": "/files/rda-new.pdf",
                            "Report_Type": "rda",
                        }
                    ],
                },
                {
                    "KodeEmiten": "CCC",
                    "NamaEmiten": "Issuer CCC",
                    "Report_Period": "Audit",
                    "Report_Year": "2025",
                    "Attachments": [
                        {
                            "File_ID": "rda-cursor",
                            "File_Modified": "2026-04-07T21:00:00.000",
                            "File_Name": "rda-cursor.pdf",
                            "File_Path": "/files/rda-cursor.pdf",
                            "Report_Type": "rda",
                        }
                    ],
                },
            ],
        },
    }

    class FakeSession:
        fetched_urls: list[str] = []

        def __init__(self, **_kwargs):
            self.counter = 0
            self.page = SimpleNamespace(url=IDXFinancialReportAdapter.public_url)

        @contextmanager
        def open_page(self, *, page_url):
            del page_url
            yield self.page

        def fetch_json_from_page(self, *, source_id, page, api_url):
            del source_id, page
            FakeSession.fetched_urls.append(api_url)
            return self._result_for(api_url)

        def _result_for(self, api_url):
            self.counter += 1
            qs = parse_qs(urlparse(api_url).query)
            key = (qs["reportType"][0], int(qs["indexFrom"][0]))
            if qs["periode"][0] != "audit":
                key = (qs["reportType"][0], qs["periode"][0], int(qs["indexFrom"][0]))
            body_path = tmp_path / f"payload-stop-{self.counter}.json"
            body_path.write_text(json.dumps(payloads.get(key, _EMPTY_PAGE)), encoding="utf-8")
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
                cursor=json.dumps({"rdf": "rdf-cursor", "rda": "rda-cursor"}),
            )
        )
    )

    assert [candidate.source_record_id for candidate in candidates] == ["rda-new"]
    assert not any("reportType=rdf" in url and "indexFrom=2" in url for url in FakeSession.fetched_urls)


def test_financial_report_watermark_merges_new_and_existing_type_cursors():
    adapter = IDXFinancialReportAdapter()
    candidates = parse_financial_report_json(
        {
            "Results": [
                {
                    "KodeEmiten": "BBB",
                    "NamaEmiten": "Issuer BBB",
                    "Report_Period": "Audit",
                    "Report_Year": "2025",
                    "Attachments": [
                        {
                            "File_ID": "rda-new",
                            "File_Modified": "2026-04-08T21:00:00.000",
                            "File_Name": "rda-new.pdf",
                            "File_Path": "/files/rda-new.pdf",
                            "Report_Type": "rda",
                        }
                    ],
                }
            ]
        }
    )

    watermark = adapter.watermark_window(
        requested_window=DiscoveryWindow(
            start=date(2025, 1, 1),
            end=date(2025, 12, 31),
            cursor=json.dumps({"rdf": "rdf-old", "rda": "rda-old"}),
        ),
        candidates=candidates,
    )

    # New cursors are keyed by report year so a rolling multi-year scan can stop
    # per year; legacy type-only keys are kept for single-year windows.
    assert json.loads(watermark.cursor) == {"rdf": "rdf-old", "rda": "rda-old", "2025:rda": "rda-new"}


def test_financial_report_years_cover_prior_year_and_explicit_spans():
    resolve = IDXFinancialReportAdapter._resolve_years

    assert resolve(DiscoveryWindow(), today=date(2026, 10, 13)) == [2026, 2025]
    assert resolve(DiscoveryWindow(start=date(2024, 1, 1), end=date(2026, 12, 31))) == [2026, 2025, 2024]
    assert resolve(DiscoveryWindow(start=date(2025, 1, 1), end=date(2025, 12, 31))) == [2025]


def test_financial_report_resume_ignores_upload_dates_in_watermark():
    adapter = IDXFinancialReportAdapter()
    # A FY2025 scan persists the newest *upload* date (2026) as ``end``.
    persisted = DiscoveryWindow(start=date(2025, 1, 1), end=date(2026, 4, 8), cursor='{"2025:rdf": "x"}')

    resumed = adapter.resume_window(persisted)

    assert resumed.start is None and resumed.end is None
    assert resumed.cursor == persisted.cursor


def test_financial_report_adapter_scans_interim_periods_with_their_own_cursors(tmp_path, monkeypatch):
    interim = {
        "ResultCount": 1,
        "Results": [
            {
                "KodeEmiten": "ABCD",
                "NamaEmiten": "Issuer ABCD",
                "Report_Period": "TW1",
                "Report_Year": "2026",
                "Attachments": [
                    {
                        "File_ID": "rdf-q1",
                        "File_Modified": "2026-04-28T10:00:00.000",
                        "File_Name": "FinancialStatement-2026-I-ABCD.pdf",
                        "File_Path": "/files/FinancialStatement-2026-I-ABCD.pdf",
                        "Report_Type": "rdf",
                    }
                ],
            }
        ],
    }
    requested: list[tuple[str, str]] = []

    class FakeSession:
        def __init__(self, **_kwargs):
            self.page = SimpleNamespace(url=IDXFinancialReportAdapter.public_url)

        @contextmanager
        def open_page(self, *, page_url):
            del page_url
            yield self.page

        def fetch_json_from_page(self, *, source_id, page, api_url):
            del source_id, page
            qs = parse_qs(urlparse(api_url).query)
            requested.append((qs["reportType"][0], qs["periode"][0]))
            payload = interim if (qs["reportType"][0], qs["periode"][0]) == ("rdf", "tw1") else _EMPTY_PAGE
            body_path = tmp_path / f"payload-{len(requested)}.json"
            body_path.write_text(json.dumps(payload), encoding="utf-8")
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
    window = DiscoveryWindow(start=date(2026, 1, 1), end=date(2026, 12, 31))
    candidates = list(adapter.discover(window))

    assert sorted(set(requested)) == [("rda", "audit"), ("rdf", "audit"), ("rdf", "tw1"), ("rdf", "tw2"), ("rdf", "tw3")]
    assert [candidate.raw_metadata["requested_period"] for candidate in candidates] == ["tw1"]
    watermark = adapter.watermark_window(requested_window=window, candidates=candidates)
    assert json.loads(watermark.cursor) == {"2026:rdf:tw1": "rdf-q1"}
