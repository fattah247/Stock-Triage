from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape

import httpx
import pytest
import yaml

from stock_triage.alerts import run_alerts
from stock_triage.db import WriterQueue, connect, run_migrations
from stock_triage.market import run_exposure, run_market_signals
from stock_triage.market.discovery import (
    dismiss_finding,
    load_open_findings,
    promote_finding,
    replay_findings,
    run_watch_sync,
)
from stock_triage.market.novelty import Headline, detect_bursts, detect_new_pairings, terms
from stock_triage.market.signals import compute_market_days
from stock_triage.market.themes import LLMTagger, Theme, keyword_tags, load_theme_file, validate_llm_tag
from stock_triage.market.watch import WatchSource, parse_feed, parse_gdelt

from .test_market_data import _THRESHOLDS, _seed_series, _weekdays

_THEMES = """
themes:
  - theme_id: msci_ftse_classification
    category: E
    title: MSCI and FTSE classification
    severity: high
    keywords: [msci, ftse russell]
    sectors: [IDXFINANCE]
  - theme_id: rupiah_weakness
    category: D
    title: Rupiah weakness
    severity: high
    keywords: [rupiah melemah]
    sectors: [IDXFINANCE]
  - theme_id: middle_east_conflict
    category: H
    title: Middle East conflict
    severity: high
    keywords: [perang, rudal]
    require_any: [iran, israel]
    sectors: [IDXENERGY]
"""
_D_DAY = date(2026, 10, 13)


def _rss(items: list[tuple[str, datetime, str | None]]) -> bytes:
    entries = "".join(
        f"<item><title>{escape(title)}</title><link>https://news.example/{i}</link>"
        f"<guid>g-{i}-{abs(hash(title)) % 10**8}</guid><pubDate>{format_datetime(stamp)}</pubDate>"
        + (f"<description>{escape(text)}</description>" if text else "")
        + "</item>"
        for i, (title, stamp, text) in enumerate(items)
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{entries}</channel></rss>'.encode()


def _ordinary_week(start: date, days: int) -> list[tuple[str, datetime, str | None]]:
    words = ["menguat", "melemah tipis", "bergerak datar", "ditutup hijau", "ditutup merah"]
    return [
        (f"IHSG {words[i % 5]} di sesi {k + 1} hari ke-{i}", datetime.combine(start + timedelta(days=i), datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=3 + k), None)
        for i in range(days)
        for k in range(2)
    ]


def _msci_burst() -> list[tuple[str, datetime, str | None]]:
    stamp = datetime.combine(_D_DAY, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=2)
    return [
        ("MSCI kaji status pasar Indonesia jadi standalone market", stamp, "Konsultasi enam bulan."),
        ("Investor cemas, MSCI buka konsultasi standalone market untuk Indonesia", stamp + timedelta(hours=1), None),
        ("Apa itu standalone market MSCI dan dampaknya ke IHSG", stamp + timedelta(hours=2), None),
        ("Asing jual saham bank setelah kabar standalone market MSCI", stamp + timedelta(hours=3), None),
    ]


def _setup(tmp_config, *, sources: list[dict]) -> None:
    themes_path = tmp_config.paths.data_dir / "risk_themes.yaml"
    sources_path = tmp_config.paths.data_dir / "watch_sources.yaml"
    themes_path.parent.mkdir(parents=True, exist_ok=True)
    themes_path.write_text(_THEMES)
    sources_path.write_text(yaml.safe_dump({"sources": sources}))
    tmp_config.market.risk_themes_path = themes_path
    tmp_config.market.watch_sources_path = sources_path


def _client(feeds: dict[str, bytes]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        body = feeds.get(str(request.url))
        return httpx.Response(200, content=body) if body is not None else httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


_SOURCES = [
    {"source_id": "bi_press", "name": "Bank Indonesia", "kind": "rss", "trust": "high", "url": "https://official.example/feed"},
    {"source_id": "news", "name": "Business news", "kind": "rss", "trust": "medium", "url": "https://news.example/feed"},
]


def test_feeds_parse_rss_atom_google_news_and_gdelt():
    source = WatchSource("s", "s", "rss", "medium")
    rss = parse_feed(_rss([("Rupiah <b>melemah</b> &amp; asing jual", datetime(2026, 10, 13, 2, tzinfo=timezone.utc), "<p>Ringkas.</p>")]), source=source)
    assert [(item.title, item.excerpt, item.published_at.isoformat()) for item in rss] == [
        ("Rupiah melemah & asing jual", "Ringkas.", "2026-10-13T02:00:00+00:00")
    ]
    atom = parse_feed(
        b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>a1</id><title>OJK rules</title>'
        b'<link rel="alternate" href="https://ojk.example/a1"/><updated>2026-10-12T08:00:00Z</updated>'
        b"<summary>New rule.</summary></entry></feed>",
        source=source,
    )
    assert (atom[0].title, atom[0].url, atom[0].excerpt) == ("OJK rules", "https://ojk.example/a1", "New rule.")
    google = parse_feed(
        b'<rss><channel><item><title>IHSG anjlok - CNBC Indonesia</title><link>https://news.google.com/x</link>'
        b"<source url='https://cnbc.example'>CNBC Indonesia</source></item></channel></rss>",
        source=WatchSource("g", "g", "google_news", "medium", query="IHSG"),
    )
    assert google[0].title == "IHSG anjlok" and google[0].raw["outlet"] == "CNBC Indonesia"
    gdelt = parse_gdelt(
        {"articles": [{"url": "https://x.example/a", "title": "MSCI weighs Indonesia", "seendate": "20261012T010000Z", "domain": "x.example"}]},
        source=WatchSource("w", "w", "gdelt", "medium", query="Indonesia"),
    )
    assert gdelt[0].published_at == datetime(2026, 10, 12, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="entities"):
        parse_feed(b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "x">]><rss></rss>', source=source)


def test_keyword_tags_cite_their_words_and_respect_require_any(tmp_path):
    path = tmp_path / "themes.yaml"
    path.write_text(_THEMES)
    themes = load_theme_file(path)

    title = "Rupiah Melemah, Iran Luncurkan Rudal"
    tags = keyword_tags(title, None, themes)
    assert [(tag.theme_id, tag.field, tag.start, tag.text) for tag in tags] == [
        ("rupiah_weakness", "title", 0, "Rupiah Melemah"),
        ("middle_east_conflict", "title", title.index("Rudal"), "Rudal"),
    ]
    # "perang" alone, without Iran or Israel, is not the Middle East theme.
    assert keyword_tags("Perang harga di pasar ritel", None, themes) == []
    # Whole words only: "msci" does not match inside another word.
    assert keyword_tags("Emsciology", None, themes) == []


def test_an_llm_tag_survives_only_with_verbatim_evidence(tmp_path):
    themes = [Theme("msci_ftse_classification", "MSCI", "high", ("msci",))]
    title = "Indeks global pertimbangkan turunkan status bursa RI"

    assert validate_llm_tag({"theme_id": "msci_ftse_classification", "evidence": "turunkan status bursa"}, title=title, excerpt=None, themes=themes).text == "turunkan status bursa"
    assert validate_llm_tag({"theme_id": "msci_ftse_classification", "evidence": "downgrade of the market"}, title=title, excerpt=None, themes=themes) is None
    assert validate_llm_tag({"theme_id": "invented", "evidence": "status bursa"}, title=title, excerpt=None, themes=themes) is None

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "local-model" and body["format"]["required"] == ["theme_id", "evidence"]
        assert body["think"] is False  # reasoning models would spend the budget thinking
        return httpx.Response(200, json={"message": {"content": json.dumps({"theme_id": "msci_ftse_classification", "evidence": "status bursa RI"})}})

    tagger = LLMTagger(base_url="http://llm.local/api", model_name="local-model")
    tag = tagger.tag(title=title, excerpt=None, themes=themes, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert (tag.method, tag.field, tag.start) == ("llm", "title", title.index("status bursa RI"))


def test_the_msci_story_bursts_on_the_day_it_first_appears(tmp_config):
    _setup(tmp_config, sources=_SOURCES)
    history = _ordinary_week(_D_DAY - timedelta(days=63), 63)
    feeds = {
        "https://news.example/feed": _rss(history + _msci_burst()),
        "https://official.example/feed": _rss([("Bank Indonesia umumkan jadwal rapat dewan gubernur", datetime(2026, 10, 13, 1, tzinfo=timezone.utc), None)]),
    }

    result = run_watch_sync(tmp_config, use_llm=False, client=_client(feeds), today=_D_DAY)

    assert result.sources_fetched == 2 and result.items_new == len(history) + 5
    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        findings = load_open_findings(conn)
        tagged = conn.execute("SELECT COUNT(*) FROM item_themes WHERE theme_id = 'msci_ftse_classification'").fetchone()[0]
        activity = conn.execute("SELECT item_count FROM theme_activity_daily WHERE theme_id = 'msci_ftse_classification' AND day = ?", (_D_DAY.isoformat(),)).fetchone()
        review = conn.execute("SELECT item_type, priority, rationale FROM review_queue_items WHERE item_type = 'emerging_finding'").fetchall()
    finally:
        conn.close()
    bursts = {item["term"]: item for item in findings if item["kind"] == "burst"}
    assert "standalone market" in bursts
    assert bursts["standalone market"]["first_seen"] == _D_DAY.isoformat()
    # The single word only bursts inside the phrase, so it is not raised twice.
    assert "standalone" not in bursts
    assert tagged == 4 and activity["item_count"] == 4
    # The high-trust item no theme matches is queued too.
    assert any(item["kind"] == "unclassified" for item in findings)
    assert any("standalone market" in row["rationale"] for row in review)
    # No ordinary daily wording bursts.
    assert not any(term.startswith("ihsg") or "ditutup" in term for term in bursts)

    again = run_watch_sync(tmp_config, use_llm=False, client=_client(feeds), today=_D_DAY)
    assert again.items_new == 0 and again.findings_new == 0


def test_a_promoted_finding_becomes_a_tracked_theme(tmp_config):
    _setup(tmp_config, sources=_SOURCES[1:])
    feeds = {"https://news.example/feed": _rss(_ordinary_week(_D_DAY - timedelta(days=63), 63) + _msci_burst())}
    run_watch_sync(tmp_config, use_llm=False, client=_client(feeds), today=_D_DAY)
    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        finding = next(item for item in load_open_findings(conn) if item["term"] == "standalone market")
    finally:
        conn.close()

    with WriterQueue(tmp_config) as wq:
        wq.submit(
            lambda conn: promote_finding(
                conn, finding_id=finding["finding_id"], theme_id="msci_standalone", title="MSCI standalone-market risk",
                keywords=["standalone market", "pasar standalone"], severity="high", sectors=["idxfinance"], category="E",
            )
        )
    run_watch_sync(tmp_config, use_llm=False, client=_client(feeds), today=_D_DAY)

    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        tagged = conn.execute("SELECT COUNT(*) FROM item_themes WHERE theme_id = 'msci_standalone'").fetchone()[0]
        status = conn.execute("SELECT status, theme_id FROM emerging_findings WHERE finding_id = ?", (finding["finding_id"],)).fetchone()
        review = conn.execute("SELECT status FROM review_queue_items WHERE source_key = ?", (finding["finding_id"],)).fetchone()
        theme = conn.execute("SELECT sectors_json, origin FROM risk_themes WHERE theme_id = 'msci_standalone'").fetchone()
        reraised = [item for item in load_open_findings(conn) if item["term"] == "standalone market"]
    finally:
        conn.close()
    assert tagged == 4
    assert (status["status"], status["theme_id"]) == ("promoted", "msci_standalone")
    assert review["status"] == "resolved"
    assert (json.loads(theme["sectors_json"]), theme["origin"]) == (["IDXFINANCE"], "promoted")
    assert reraised == []  # a promoted finding is not raised again


def test_a_dismissed_finding_stays_dismissed(tmp_config):
    _setup(tmp_config, sources=_SOURCES[1:])
    feeds = {"https://news.example/feed": _rss(_ordinary_week(_D_DAY - timedelta(days=63), 63) + _msci_burst())}
    run_watch_sync(tmp_config, use_llm=False, client=_client(feeds), today=_D_DAY)
    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        finding_id = next(item["finding_id"] for item in load_open_findings(conn) if item["term"] == "standalone market")
    finally:
        conn.close()
    with WriterQueue(tmp_config) as wq:
        wq.submit(lambda conn: dismiss_finding(conn, finding_id=finding_id, note="known"))

    run_watch_sync(tmp_config, use_llm=False, client=_client(feeds), today=_D_DAY + timedelta(days=1))

    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        assert all(item["term"] != "standalone market" for item in load_open_findings(conn))
    finally:
        conn.close()


def test_replay_reports_the_first_day_and_stores_nothing(tmp_config):
    _setup(tmp_config, sources=_SOURCES[1:])
    feeds = {"https://news.example/feed": _rss(_ordinary_week(_D_DAY - timedelta(days=63), 63) + _msci_burst())}
    run_watch_sync(tmp_config, use_llm=False, client=_client(feeds), today=_D_DAY - timedelta(days=20))
    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        before = conn.execute("SELECT COUNT(*) FROM emerging_findings").fetchone()[0]
        hits = replay_findings(conn, start=_D_DAY - timedelta(days=5), end=_D_DAY + timedelta(days=3), match="standalone")
        after = conn.execute("SELECT COUNT(*) FROM emerging_findings").fetchone()[0]
    finally:
        conn.close()
    hit = next(hit for hit in hits if hit.term == "standalone market")
    assert hit.detected_on == hit.first_seen == _D_DAY
    assert before == after


def test_new_pairings_need_a_name_next_to_indonesia_for_the_first_time():
    day = date(2026, 10, 10)
    # Twenty days and 120 headlines of history, so "first time" means something.
    old = [Headline(f"o{i}", day - timedelta(days=10 + i % 20), "OJK and Indonesia regulators meet", "medium", True) for i in range(120)]
    new = [
        Headline("n1", day, "Danantara takes stakes as Indonesia restructures state firms", "medium", False),
        Headline("n2", day, "Investors ask what Danantara means for Indonesia banks", "medium", False),
        Headline("n3", day, "OJK and Indonesia regulators meet again", "medium", True),
    ]

    found = {finding.term for finding in detect_new_pairings(old + new, as_of=day)}

    assert "danantara" in found and "ojk" not in found
    assert "standalone market" in terms("MSCI weighs standalone market status")
    assert detect_bursts(old, as_of=day) == []
    # Without that history, neither detector speaks.
    assert detect_new_pairings(new, as_of=day) == [] and detect_bursts(new * 3, as_of=day) == []


def test_active_high_themes_join_the_market_state():
    days = _weekdays(date(2026, 9, 1), 30)
    series = {"index:COMPOSITE": dict(zip(days, [6000.0] * 24 + [5400.0] * 6))}
    themes = {day: [] for day in days}
    themes[days[-1]] = ["MSCI classification", "Rupiah weakness"]

    result = compute_market_days(
        series, thresholds={**_THRESHOLDS, "active_themes": {"elevated": 1, "stressed": 2, "crisis": 3}},
        sectors=[], min_signals=2, step_down_after=5, active_themes=themes,
    )

    latest = result[-1]
    reading = next(r for r in latest.readings if r.signal_name == "active_themes")
    assert (reading.value, reading.level) == (2.0, "stressed")
    assert reading.text == "2 high-severity risk themes active: MSCI classification, Rupiah weakness"
    # 10% below the high plus two high-severity themes: two signals at stressed.
    assert latest.regime == "stressed"
    # Before any watched item exists, the signal has no reading at all.
    early = compute_market_days(series, thresholds=_THRESHOLDS, sectors=[], min_signals=2, step_down_after=5, active_themes={days[-1]: []})
    assert all(r.signal_name != "active_themes" for r in early[0].readings)


def test_an_unexplained_crash_opens_a_research_item_with_its_headlines(tmp_config):
    _setup(tmp_config, sources=_SOURCES[1:])
    days = _weekdays(date(2026, 1, 5), 200)
    closes = [7000.0 + (i % 5) * 10 for i in range(199)] + [6500.0]
    _seed_series(tmp_config, {"index:COMPOSITE": dict(zip(days, closes))})
    crash_day = days[-1]
    stamp = datetime.combine(crash_day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=4)
    feeds = {
        "https://news.example/feed": _rss(
            [
                ("Rupiah melemah tajam, asing kabur", stamp, None),
                ("Rupiah melemah lagi sore ini", stamp + timedelta(hours=1), None),
                ("Kabar akuisisi mendadak bank BUMN mengguncang pasar", stamp + timedelta(hours=2), None),
                ("Akuisisi mendadak bank BUMN bikin investor panik", stamp + timedelta(hours=3), None),
            ]
        )
    }

    result = run_watch_sync(tmp_config, use_llm=False, client=_client(feeds), today=crash_day)

    assert result.anomalies == 1
    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        [anomaly] = [item for item in load_open_findings(conn) if item["kind"] == "anomaly"]
    finally:
        conn.close()
    note = anomaly["details"]["note"]
    assert note.startswith(f"IHSG daily change (%) fell to -7.539 on {crash_day}")
    assert "Rupiah weakness (2)" in note
    assert "akuisisi mendadak" in anomaly["details"]["candidate_themes"]
    assert len(anomaly["details"]["headlines"]) == 4

    alerts = run_alerts(tmp_config, now=datetime.combine(crash_day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=12))
    lines = [json.loads(line) for line in tmp_config.alerts.jsonl_path.read_text().splitlines()]
    assert [line["signal_kind"] for line in lines if line["signal_kind"] == "market_anomaly"] == ["market_anomaly"]
    assert alerts.emitted >= 1


def test_a_known_risk_date_explains_the_move(tmp_config):
    _setup(tmp_config, sources=_SOURCES[1:])
    days = _weekdays(date(2026, 1, 5), 200)
    closes = [7000.0 + (i % 5) * 10 for i in range(199)] + [6500.0]
    _seed_series(tmp_config, {"index:COMPOSITE": dict(zip(days, closes))})

    def seed(conn):
        conn.execute(
            "INSERT INTO market_catalysts (catalyst_id, catalyst_date, kind, title, severity, region, source_url, notes, synced_at) "
            "VALUES ('c1', ?, 'bi_rate', 'BI decision', 'high', 'ID', NULL, NULL, 'now')",
            (days[-1].isoformat(),),
        )

    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)
        wq.submit(seed)
    result = run_watch_sync(tmp_config, use_llm=False, client=_client({"https://news.example/feed": _rss([])}), today=days[-1])

    assert result.anomalies == 0


def test_a_burst_a_high_trust_source_supports_alerts_once(tmp_config):
    _setup(tmp_config, sources=_SOURCES)
    history = _ordinary_week(_D_DAY - timedelta(days=63), 63)
    burst = _msci_burst()
    feeds = {
        # The official feed carries one of the burst's headlines and an unrelated release.
        "https://news.example/feed": _rss(history + burst[1:]),
        "https://official.example/feed": _rss(burst[:1] + [("Siaran pers: kebijakan baru lindung nilai", datetime(2026, 10, 13, 1, tzinfo=timezone.utc), None)]),
    }
    run_watch_sync(tmp_config, use_llm=False, client=_client(feeds), today=_D_DAY)

    now = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
    run_alerts(tmp_config, now=now)
    again = run_alerts(tmp_config, now=now + timedelta(hours=1))

    lines = [json.loads(line) for line in tmp_config.alerts.jsonl_path.read_text().splitlines()]
    emerging = [line for line in lines if line["signal_kind"] == "emerging_theme"]
    assert emerging and all(line["claim"].startswith(("Emerging burst", "Emerging new pairing")) for line in emerging)
    assert any("'standalone market'" in line["claim"] for line in emerging)
    # The unclassified official release waits in the review queue instead.
    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        kinds = {item["kind"] for item in load_open_findings(conn)}
    finally:
        conn.close()
    assert "unclassified" in kinds
    assert again.emitted == 0


def test_active_themes_add_to_exposure_and_a_quarterly_review_is_queued(tmp_config):
    _setup(tmp_config, sources=_SOURCES[1:])
    stamp = datetime.now(timezone.utc) - timedelta(hours=2)
    feeds = {"https://news.example/feed": _rss([(f"MSCI review: BBCA and others {i}", stamp - timedelta(minutes=i), None) for i in range(4)])}
    today = (datetime.now(timezone.utc) + timedelta(hours=7)).date()

    def seed(conn):
        for ticker, name, sector in (("BBCA", "Bank Central Asia", "Keuangan"), ("ADRO", "Adaro", "Energi")):
            conn.execute(
                "INSERT INTO companies (ticker, issuer_name, sector, first_seen_at, last_seen_at) VALUES (?, ?, ?, 'now', 'now')",
                (ticker, name, sector),
            )

    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)
        wq.submit(seed)
    run_watch_sync(tmp_config, use_llm=False, client=_client(feeds), today=today)

    from stock_triage.market.exposure import score_exposure

    active = [{"theme_id": "msci_ftse_classification", "title": "MSCI and FTSE classification", "severity": "high", "sectors": ["IDXFINANCE"], "items": 4}]
    bank = score_exposure(ticker="BBCA", sector="Keuangan", metrics={}, index_memberships=[], watchlist_sector_share=None,
                          active_themes=active, named_in={"MSCI and FTSE classification": 4})
    miner = score_exposure(ticker="ADRO", sector="Energi", metrics={}, index_memberships=[], watchlist_sector_share=None, active_themes=active)
    assert [(c.name, c.points) for c in bank.components] == [("theme_sector", 10), ("theme_named", 10)]
    assert bank.components[1].reason == "Named in 4 recent headlines on MSCI and FTSE classification"
    assert miner.components == []
    assert run_exposure(tmp_config) is not None

    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        reviews = conn.execute("SELECT source_key, rationale FROM review_queue_items WHERE item_type = 'catalog_review'").fetchall()
    finally:
        conn.close()
    quarter = f"{today.year}Q{(today.month - 1) // 3 + 1}"
    assert [row["source_key"] for row in reviews] == [f"catalog:{quarter}"]
    assert "docs/market_risk_factors.md" in reviews[0]["rationale"]


def test_the_shipped_themes_and_sources_load():
    from stock_triage.config.loader import REPO_ROOT
    from stock_triage.market.watch import load_watch_sources

    themes = load_theme_file(REPO_ROOT / "config" / "risk_themes.yaml")
    assert len(themes) >= 40
    assert {theme.category for theme in themes} >= set("ABCDEFGHIJKM")
    assert all(code.startswith("IDX") for theme in themes for code in theme.sectors)
    sources = load_watch_sources(REPO_ROOT / "config" / "watch_sources.yaml")
    assert sources and all(source.trust in {"high", "medium", "low"} for source in sources)


def test_market_signals_reads_theme_activity(tmp_config):
    _setup(tmp_config, sources=_SOURCES[1:])
    days = _weekdays(date(2026, 9, 1), 30)
    _seed_series(tmp_config, {"index:COMPOSITE": dict(zip(days, [6000.0] * 30))})

    def seed(conn):
        conn.execute(
            "INSERT INTO watch_items VALUES ('i1', 'news', 'medium', ?, ?, 'MSCI', 'https://x', NULL, 'id', '{}')",
            (f"{days[0]}T01:00:00+00:00", f"{days[0]}T01:00:00+00:00"),
        )
        # Two items a day over the last three calendar days: active with six.
        for back in range(3):
            day = days[-1] - timedelta(days=back)
            conn.execute("INSERT INTO theme_activity_daily VALUES ('msci_ftse_classification', ?, 2, 6)", (day.isoformat(),))

    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)
        wq.submit(seed)

    latest = run_market_signals(tmp_config).latest

    reading = next(r for r in latest.readings if r.signal_name == "active_themes")
    assert (reading.value, reading.level) == (1.0, "elevated")


def test_html_links_sitemaps_and_json_lists_become_items():
    from stock_triage.market.watch import fetch_source

    pages = {
        "https://bi.example/news/default.aspx": (
            '<a href="/news/Pages/sp_2823326.aspx"><img src="x.jpg"></a>'
            '<a href="/news/Pages/sp_2823326.aspx">BI-Rate Tetap 5,75%: Memperkuat Stabilitas</a>'
            '<a href="/about.aspx">Tentang kami dan sejarah bank</a>'
        ).encode(),
        "https://fitch.example/sitemap.xml": (
            b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            b"<url><loc>https://fitch.example/research/sovereigns/indonesias-policy-uncertainty-clouds-outlook-21-10-2026</loc><lastmod>2026-10-21</lastmod></url>"
            b"<url><loc>https://fitch.example/research/banks/thai-banks-steady-21-10-2026</loc><lastmod>2026-10-21</lastmod></url></urlset>"
        ),
        "https://bmkg.example/quakes.json": json.dumps(
            {"Infogempa": {"gempa": [
                {"Magnitude": "6.4", "Wilayah": "45 km BaratDaya BANTEN", "Potensi": "Tidak berpotensi tsunami", "DateTime": "2026-10-21T07:44:15+00:00"},
                {"Magnitude": "5.1", "Wilayah": "62 km TimurLaut MALUKU", "Potensi": "Tidak berpotensi tsunami", "DateTime": "2026-10-20T16:29:35+00:00"},
            ]}}
        ).encode(),
        "https://gdacs.example/rss.xml": _rss(
            [
                ("Orange earthquake alert (Magnitude 6.8M) in Indonesia", datetime(2026, 10, 21, 3, tzinfo=timezone.utc), None),
                ("Green flood alert in Indonesia", datetime(2026, 10, 21, 2, tzinfo=timezone.utc), None),
                ("Red cyclone alert in Philippines", datetime(2026, 10, 21, 1, tzinfo=timezone.utc), None),
            ]
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = pages.get(str(request.url))
        return httpx.Response(200, content=body) if body is not None else httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    bi = WatchSource("bi", "BI", "html_links", "high", url="https://bi.example/news/default.aspx", options={"link_pattern": r"Pages/sp_\d+\.aspx"})
    fitch = WatchSource("fitch", "Fitch", "sitemap", "high", url="https://fitch.example/sitemap.xml", match=("indonesia",))
    bmkg = WatchSource(
        "bmkg", "BMKG", "json_list", "high", url="https://bmkg.example/quakes.json",
        options={"items_path": "Infogempa.gempa", "title_template": "Gempa M{Magnitude} {Wilayah} (BMKG)", "date_field": "DateTime",
                 "url_template": "https://bmkg.example/#{DateTime}", "min_values": {"Magnitude": 6.0}},
    )
    gdacs = WatchSource("gdacs", "GDACS", "rss", "high", url="https://gdacs.example/rss.xml", match=("indonesia",), exclude=("green",))

    [release] = fetch_source(client, bi)
    assert (release.title, release.url) == ("BI-Rate Tetap 5,75%: Memperkuat Stabilitas", "https://bi.example/news/Pages/sp_2823326.aspx")
    [research] = fetch_source(client, fitch)
    assert research.title == "Indonesias policy uncertainty clouds outlook"
    assert research.published_at.date() == date(2026, 10, 21)
    [quake] = fetch_source(client, bmkg)
    assert quake.title == "Gempa M6.4 45 km BaratDaya BANTEN (BMKG)"
    assert [item.title for item in fetch_source(client, gdacs)] == ["Orange earthquake alert (Magnitude 6.8M) in Indonesia"]


def test_gdelt_titles_are_tidied_and_a_throttled_window_is_skipped(monkeypatch):
    from stock_triage.market import watch

    monkeypatch.setattr(watch, "GDELT_PAUSE_S", 0)
    monkeypatch.setattr(watch, "GDELT_RETRY_PAUSE_S", 0)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "20261015" in str(request.url):
            return httpx.Response(429, text="Please limit requests to one every 5 seconds")
        return httpx.Response(
            200,
            json={"articles": [{"url": f"https://x.example/{len(calls)}", "title": "IHSG naik 10 , 61 % hari ini", "seendate": "20261016T010000Z"}]},
        )

    source = WatchSource("g", "GDELT", "gdelt", "medium", query="IHSG")
    items = watch.fetch_source(
        httpx.Client(transport=httpx.MockTransport(handler)),
        source,
        start=datetime(2026, 10, 15, tzinfo=timezone.utc),
        end=datetime(2026, 10, 17, tzinfo=timezone.utc),
    )

    assert [item.title for item in items] == ["IHSG naik 10, 61% hari ini"]
    assert len(calls) == 3  # the refused day was retried once, then skipped


def test_a_market_wide_crash_is_one_research_item(tmp_config):
    _setup(tmp_config, sources=_SOURCES[1:])
    days = _weekdays(date(2026, 1, 5), 200)
    closes = [7000.0 + (i % 5) * 10 for i in range(199)] + [6500.0]
    sector = [1000.0 + (i % 3) * 2 for i in range(199)] + [900.0]
    _seed_series(
        tmp_config,
        {"index:COMPOSITE": dict(zip(days, closes)), "index:IDXFINANCE": dict(zip(days, sector)), "index:IDXENERGY": dict(zip(days, sector))},
    )

    result = run_watch_sync(tmp_config, use_llm=False, client=_client({"https://news.example/feed": _rss([])}), today=days[-1])

    assert result.anomalies == 1
    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        [anomaly] = [item for item in load_open_findings(conn) if item["kind"] == "anomaly"]
    finally:
        conn.close()
    assert {item["series_id"] for item in anomaly["details"]["also_unusual"]} >= {"index:IDXFINANCE", "index:IDXENERGY"}
    assert "Also beyond its usual range that day: IDXFINANCE" in anomaly["details"]["note"] or "IDXENERGY" in anomaly["details"]["note"]


def test_a_busier_week_alone_raises_no_bursts():
    day = date(2026, 10, 20)
    words = ["menguat", "melemah", "datar", "hijau", "merah"]
    baseline = [Headline(f"b{i}", day - timedelta(days=10 + i // 2), f"IHSG {words[i % 5]} sesi siang", "medium", True) for i in range(120)]
    # Five times as many sources this week, saying the same ordinary things.
    busy = [Headline(f"w{i}", day - timedelta(days=i % 7), f"IHSG {words[i % 5]} sesi siang ke-{i}", "medium", True) for i in range(150)]

    assert detect_bursts(baseline + busy, as_of=day) == []


def test_llm_eval_scores_models_and_recommends_the_best():
    from stock_triage.market.llm_eval import EvalCase, evaluate_model, load_eval_cases, recommend
    from stock_triage.config.loader import REPO_ROOT

    themes = [Theme("rupiah_weakness", "Rupiah weakness", "high", ("rupiah melemah",)), Theme("oil_price_shock", "Oil", "high", ("minyak",))]
    cases = [
        EvalCase("Rupiah tembus 18.000 per dolar", "rupiah_weakness"),
        EvalCase("Harga minyak dunia melonjak", "oil_price_shock"),
        EvalCase("Peluncuran kopi baru", None),
    ]

    def server(answers):
        def handler(request: httpx.Request) -> httpx.Response:
            title = json.loads(request.content)["messages"][1]["content"].split("Headline: ")[1].split("\n")[0]
            return httpx.Response(200, json={"message": {"content": json.dumps(answers.get(title, {"theme_id": None, "evidence": None}))}})

        return httpx.Client(transport=httpx.MockTransport(handler))

    good = server(
        {
            "Rupiah tembus 18.000 per dolar": {"theme_id": "rupiah_weakness", "evidence": "Rupiah tembus 18.000"},
            "Harga minyak dunia melonjak": {"theme_id": "oil_price_shock", "evidence": "minyak dunia melonjak"},
        }
    )
    sloppy = server(
        {
            "Rupiah tembus 18.000 per dolar": {"theme_id": "rupiah_weakness", "evidence": "the rupiah fell"},  # not verbatim
            "Peluncuran kopi baru": {"theme_id": "oil_price_shock", "evidence": "kopi baru"},
        }
    )

    best = evaluate_model(LLMTagger(base_url="http://llm", model_name="good"), cases, themes, client=good)
    worse = evaluate_model(LLMTagger(base_url="http://llm", model_name="sloppy"), cases, themes, client=sloppy)

    assert (best.accuracy, best.recall, best.precision) == (1.0, 1.0, 1.0)
    assert (worse.correct, worse.evidence_failures, worse.tagged, worse.tagged_correct) == (0, 1, 1, 0)
    assert recommend([worse, best]).model == "good"
    shipped = load_eval_cases(REPO_ROOT / "config" / "theme_eval.yaml")
    known = {theme.theme_id for theme in load_theme_file(REPO_ROOT / "config" / "risk_themes.yaml")}
    assert len(shipped) >= 40
    assert all(answer is None or answer in known for case in shipped for answer in case.accepted)
