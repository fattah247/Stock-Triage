from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

from stock_triage.sources import DiscoveryWindow
from stock_triage.sources.adapters import IDXDisclosureAdapter
from stock_triage.sources.idx_artifacts import parse_disclosure_html


PAGE_1_HTML = """
<div class="attach-card">
  <time>09 April 2026 08:44:15</time>
  <h6 class="title"><a href="https://www.idx.co.id/StaticData/NewsAndAnnouncement/ANNOUNCEMENTSTOCK/From_EREP/202604/page1-main.pdf">
    Monthly Report of Securities Holders Registration [<span>KIAS</span>]
  </a></h6>
  <ul><li><a href="https://www.idx.co.id/StaticData/NewsAndAnnouncement/ANNOUNCEMENTSTOCK/From_EREP/202604/page1-att1.pdf" download="page1.pdf"></a></li></ul>
</div>
"""

PAGE_2_HTML = """
<div class="attach-card">
  <time>09 April 2026 05:13:30</time>
  <h6 class="title"><a href="https://www.idx.co.id/StaticData/NewsAndAnnouncement/ANNOUNCEMENTSTOCK/From_EREP/202604/page2-main.pdf">
    Treatise Summary/Treatise General Meeting of Shareholder's [<span>MSIN</span>]
  </a></h6>
  <ul><li><a href="https://www.idx.co.id/StaticData/NewsAndAnnouncement/ANNOUNCEMENTSTOCK/From_EREP/202604/page2-att1.pdf" download="page2.pdf"></a></li></ul>
</div>
"""


class _FakeNextButton:
    def __init__(self, page: "_FakeDisclosurePage") -> None:
        self.page = page

    def count(self) -> int:
        return 1

    def is_disabled(self) -> bool:
        return self.page.index >= len(self.page.html_pages) - 1

    def click(self) -> None:
        if not self.is_disabled():
            self.page.index += 1


class _FakeDisclosurePage:
    def __init__(self, html_pages: list[str]) -> None:
        self.html_pages = html_pages
        self.index = 0

    def content(self) -> str:
        return self.html_pages[self.index]

    def locator(self, selector: str):
        if selector == ".btn-arrow.--next":
            return _FakeNextButton(self)
        raise AssertionError(f"Unexpected selector: {selector}")

    def wait_for_timeout(self, _ms: int) -> None:
        return None


class _FakePlaywrightSession:
    html_pages: list[str] = []

    def __init__(self, **_kwargs) -> None:
        return None

    @contextmanager
    def open_page(self, *, page_url: str):
        del page_url
        yield _FakeDisclosurePage(self.html_pages)


def test_disclosure_bootstrap_reads_first_page_only(tmp_path, monkeypatch):
    del tmp_path
    _FakePlaywrightSession.html_pages = [PAGE_1_HTML, PAGE_2_HTML]
    fake_config = SimpleNamespace(
        browser=SimpleNamespace(
            profile_dir="unused",
            artifacts_dir="unused",
            headless=True,
            challenge_timeout_s=1,
            page_settle_ms=1,
            channel="",
        )
    )
    monkeypatch.setattr("stock_triage.sources.adapters.idx_disclosures.PlaywrightSession", _FakePlaywrightSession)
    monkeypatch.setattr("stock_triage.sources.adapters.idx_disclosures.load_config", lambda: fake_config)

    adapter = IDXDisclosureAdapter()
    candidates = list(adapter.discover(adapter.default_window()))

    assert [candidate.title for candidate in candidates] == ["Monthly Report of Securities Holders Registration"]


def test_disclosure_resume_stops_when_saved_cursor_is_seen(tmp_path, monkeypatch):
    del tmp_path
    _FakePlaywrightSession.html_pages = [PAGE_1_HTML, PAGE_2_HTML]
    fake_config = SimpleNamespace(
        browser=SimpleNamespace(
            profile_dir="unused",
            artifacts_dir="unused",
            headless=True,
            challenge_timeout_s=1,
            page_settle_ms=1,
            channel="",
        )
    )
    monkeypatch.setattr("stock_triage.sources.adapters.idx_disclosures.PlaywrightSession", _FakePlaywrightSession)
    monkeypatch.setattr("stock_triage.sources.adapters.idx_disclosures.load_config", lambda: fake_config)

    saved_cursor = parse_disclosure_html(PAGE_2_HTML)[0].source_record_id
    adapter = IDXDisclosureAdapter()
    candidates = list(adapter.discover(DiscoveryWindow(cursor=saved_cursor)))

    assert [candidate.title for candidate in candidates] == ["Monthly Report of Securities Holders Registration"]


def test_disclosure_watermark_tracks_newest_candidate():
    adapter = IDXDisclosureAdapter()
    candidates = parse_disclosure_html(PAGE_1_HTML)

    watermark = adapter.watermark_window(
        requested_window=adapter.default_window(),
        candidates=candidates,
    )

    assert watermark.end == date(2026, 4, 9)
    assert watermark.cursor == candidates[0].source_record_id


def test_disclosure_resume_window_rehydrates_empty_legacy_watermark():
    adapter = IDXDisclosureAdapter()

    resumed = adapter.resume_window(DiscoveryWindow())

    assert resumed == adapter.default_window()


DAY_2_NEW_HTML = """
<div class="attach-card">
  <time>21 April 2026 10:02:11</time>
  <h6 class="title"><a href="https://www.idx.co.id/StaticData/NewsAndAnnouncement/ANNOUNCEMENTSTOCK/From_EREP/202604/day2-main.pdf">
    Information Disclosure Regarding Rights Issue [<span>ABBA</span>]
  </a></h6>
  <ul><li><a href="https://www.idx.co.id/StaticData/NewsAndAnnouncement/ANNOUNCEMENTSTOCK/From_EREP/202604/day2-att1.pdf" download="day2.pdf"></a></li></ul>
</div>
"""


def test_disclosure_incremental_resume_finds_items_published_after_first_run(monkeypatch):
    """Day 1 bootstraps; day 2 must see what was published since.

    Regression: the persisted ``end`` date used to be reapplied as an upper
    bound, so every later disclosure was filtered out and discovery returned
    nothing forever while still exiting successfully.
    """
    fake_config = SimpleNamespace(
        browser=SimpleNamespace(
            profile_dir="unused",
            artifacts_dir="unused",
            headless=True,
            challenge_timeout_s=1,
            page_settle_ms=1,
            channel="",
        )
    )
    monkeypatch.setattr("stock_triage.sources.adapters.idx_disclosures.PlaywrightSession", _FakePlaywrightSession)
    monkeypatch.setattr("stock_triage.sources.adapters.idx_disclosures.load_config", lambda: fake_config)
    adapter = IDXDisclosureAdapter()

    _FakePlaywrightSession.html_pages = [PAGE_1_HTML, PAGE_2_HTML]
    day_1_window = adapter.default_window()
    day_1 = list(adapter.discover(day_1_window))
    watermark = adapter.watermark_window(requested_window=day_1_window, candidates=day_1)

    _FakePlaywrightSession.html_pages = [DAY_2_NEW_HTML + PAGE_1_HTML, PAGE_2_HTML]
    day_2_window = adapter.resume_window(watermark)
    day_2 = list(adapter.discover(day_2_window))

    assert [candidate.title for candidate in day_2] == ["Information Disclosure Regarding Rights Issue"]
    day_2_watermark = adapter.watermark_window(requested_window=day_2_window, candidates=day_2)
    assert day_2_watermark.end == date(2026, 4, 21)
    assert day_2_watermark.cursor == day_2[0].source_record_id
