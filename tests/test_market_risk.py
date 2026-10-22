from __future__ import annotations

import json
import subprocess
from datetime import date, datetime, timezone

import pytest

from stock_triage.alerts import pipeline as alerts_pipeline
from stock_triage.alerts import run_alerts
from stock_triage.dashboard.snapshot import load_company_detail
from stock_triage.db import WriterQueue, connect, run_migrations
from stock_triage.market import CatalystFileError, load_latest_exposure, load_upcoming_catalysts, run_exposure, sync_catalysts

_NOW = "2026-10-20T00:00:00+00:00"


def _seed(tmp_config, *, companies=(), documents=(), metrics=(), expectations=()) -> None:
    """companies: (ticker, sector); documents: (document_id, ticker, parse_strategy);
    metrics: (document_id, ticker, metric_name, value, fiscal_year, period_end_date[, currency])."""

    def _insert(conn):
        for ticker, sector in companies:
            conn.execute(
                "INSERT INTO companies (ticker, issuer_name, sector, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?)",
                (ticker, f"Issuer {ticker}", sector, _NOW, _NOW),
            )
        for document_id, ticker, parse_strategy in documents:
            conn.execute(
                """
                INSERT INTO documents (
                    document_id, source_name, source_record_id, retrieved_at, local_path, sha256,
                    document_type, process_state, parse_strategy, ticker, created_at, updated_at
                ) VALUES (?, 'idx_financial_reports', ?, ?, '/tmp/x', ?, 'financial_statement', 'extracted', ?, ?, ?, ?)
                """,
                (document_id, document_id, _NOW, f"sha-{document_id}", parse_strategy, ticker, _NOW, _NOW),
            )
        for document_id, ticker, metric_name, value, fiscal_year, period_end, *rest in metrics:
            conn.execute(
                """
                INSERT INTO metric_items (
                    metric_id, document_id, ticker, metric_name, value_normalized, currency,
                    fiscal_year, fiscal_period, period_end_date, extracted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'Annual', ?, ?)
                """,
                (
                    f"m-{document_id}-{metric_name}",
                    document_id,
                    ticker,
                    metric_name,
                    value,
                    rest[0] if rest else "IDR",
                    fiscal_year,
                    period_end,
                    _NOW,
                ),
            )
        for ticker, period_label, period_end, deadline in expectations:
            conn.execute(
                """
                INSERT INTO filing_expectations (
                    expectation_id, ticker, obligation_type, fiscal_year, period_label, period_end_date,
                    window_start, deadline_at, state, knowledge_source, payload_json, derived_version,
                    created_at, updated_at
                ) VALUES (?, ?, 'periodic', 2026, ?, ?, ?, ?, 'due', 'inferred', '{}', 'test', ?, ?)
                """,
                (f"fe-{ticker}-{period_label}", ticker, period_label, period_end, period_end, deadline, _NOW, _NOW),
            )

    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)
        wq.submit(_insert)


def _write_catalysts(tmp_config, text: str) -> None:
    tmp_config.market.catalysts_path.write_text(text, encoding="utf-8")


_FOMC = """
catalysts:
  - date: 2026-10-28
    kind: fomc
    title: FOMC rate decision
    severity: high
    source_url: https://example.test/fomc
  - date: 2026-10-26
    kind: custom
    title: Low-key reminder
    severity: low
"""


def _jsonl_alerts(tmp_config) -> list[dict]:
    path = tmp_config.alerts.jsonl_path
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_risk_calendar_syncs_the_file_and_adds_watchlist_filing_deadlines(tmp_config):
    _seed(tmp_config, companies=[("ABCD", "Perindustrian")], expectations=[("ABCD", "Q3 filing", "2026-09-30", "2026-10-30")])
    _write_catalysts(tmp_config, _FOMC)

    result = sync_catalysts(tmp_config)

    assert (result.file_found, result.synced) == (True, 2)
    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        upcoming = load_upcoming_catalysts(conn, today=date(2026, 10, 25), days=7, watchlist={"ABCD"})
        without_watchlist = load_upcoming_catalysts(conn, today=date(2026, 10, 25), days=7, watchlist=set())
    finally:
        conn.close()
    assert [(item.catalyst_date.isoformat(), item.kind) for item in upcoming] == [
        ("2026-10-26", "custom"),
        ("2026-10-28", "fomc"),
        ("2026-10-30", "filing_deadline"),
    ]
    assert upcoming[2].ticker == "ABCD"
    assert len(without_watchlist) == 2

    # The file is the source of truth: a later sync replaces what was stored.
    _write_catalysts(tmp_config, "catalysts:\n  - {date: 2026-11-11, kind: msci_review, title: MSCI review}\n")
    assert sync_catalysts(tmp_config).synced == 1
    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        kinds = [row["kind"] for row in conn.execute("SELECT kind FROM market_catalysts")]
    finally:
        conn.close()
    assert kinds == ["msci_review"]


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ("{date: 2026-10-28, kind: rumour, title: X}", "unknown kind 'rumour'"),
        ("{date: someday, kind: fomc, title: X}", "no valid 'date'"),
        ("{date: 2026-10-28, kind: fomc, title: X, severity: extreme}", "severity 'extreme'"),
    ],
)
def test_risk_calendar_rejects_a_bad_entry_by_number(tmp_config, entry, message):
    _write_catalysts(tmp_config, f"catalysts:\n  - {{date: 2026-10-01, kind: fomc, title: Fine}}\n  - {entry}\n")

    with pytest.raises(CatalystFileError) as excinfo:
        sync_catalysts(tmp_config)

    assert "entry 2" in str(excinfo.value)
    assert message in str(excinfo.value)


def test_risk_date_reminder_fires_lead_days_ahead_without_a_watchlist(tmp_config):
    _write_catalysts(tmp_config, _FOMC)
    sync_catalysts(tmp_config)
    assert tmp_config.alerts.watchlist == []

    four_days_before = run_alerts(tmp_config, now=datetime(2026, 10, 24, 9, tzinfo=timezone.utc))
    three_days_before = run_alerts(tmp_config, now=datetime(2026, 10, 25, 9, tzinfo=timezone.utc))
    repeated = run_alerts(tmp_config, now=datetime(2026, 10, 25, 12, tzinfo=timezone.utc))

    assert four_days_before.emitted == 0
    # The low-severity entry, a day earlier, sits below the default warning floor.
    assert (three_days_before.selected, three_days_before.emitted) == (1, 1)
    assert (repeated.emitted, repeated.skipped_existing) == (0, 1)
    (alert,) = _jsonl_alerts(tmp_config)
    assert alert["signal_kind"] == "catalyst"
    assert alert["document_id"] is None
    assert alert["severity"] == "high"
    assert alert["claim"] == "FOMC rate decision on 2026-10-28 (in 3 days)"
    assert alert["original_url"] == "https://example.test/fomc"


def _fragile_corpus(tmp_config) -> None:
    _seed(
        tmp_config,
        companies=[("ABCD", "Perindustrian"), ("BANK", "Keuangan"), ("GOOD", "Energi")],
        documents=[("doc-abcd-xlsx", "ABCD", "xlsx"), ("doc-abcd-pdf", "ABCD", "native"), ("doc-bank", "BANK", "xlsx"), ("doc-good", "GOOD", "xlsx")],
        metrics=[
            ("doc-abcd-xlsx", "ABCD", "total_assets", 250e9, 2025, "2025-12-31"),
            ("doc-abcd-xlsx", "ABCD", "total_liabilities", 350e9, 2025, "2025-12-31"),
            ("doc-abcd-xlsx", "ABCD", "equity", -100e9, 2025, "2025-12-31"),
            ("doc-abcd-xlsx", "ABCD", "net_profit", -25.7e9, 2025, "2025-12-31"),
            ("doc-abcd-xlsx", "ABCD", "operating_cash_flow", 2.8e9, 2025, "2025-12-31"),
            # A text-extracted figure for the same period; the workbook's wins.
            ("doc-abcd-pdf", "ABCD", "equity", 97.0, 2025, "2025-12-31"),
            ("doc-bank", "BANK", "total_assets", 1_129.8e12, 2025, "2025-12-31"),
            ("doc-bank", "BANK", "total_liabilities", 962.6e12, 2025, "2025-12-31"),
            ("doc-bank", "BANK", "equity", 167.2e12, 2025, "2025-12-31"),
            ("doc-bank", "BANK", "net_profit", 21.7e12, 2025, "2025-12-31"),
            ("doc-good", "GOOD", "total_assets", 100e9, 2025, "2025-12-31"),
            ("doc-good", "GOOD", "total_liabilities", 30e9, 2025, "2025-12-31"),
            ("doc-good", "GOOD", "equity", 70e9, 2025, "2025-12-31"),
            ("doc-good", "GOOD", "net_profit", 9e9, 2025, "2025-12-31"),
        ],
    )


def test_exposure_scores_explain_each_point(tmp_config):
    _fragile_corpus(tmp_config)
    tmp_config.market.index_membership_path.write_text("indexes:\n  LQ45: [GOOD]\n", encoding="utf-8")

    result = run_exposure(tmp_config, as_of=date(2026, 10, 20))

    by_ticker = {score.ticker: score for score in result.scores}
    abcd = by_ticker["ABCD"]
    assert abcd.score == 70
    assert [(component.name, component.points) for component in abcd.components] == [
        ("negative_equity", 30),
        ("leverage", 25),
        ("net_loss", 15),
    ]
    assert abcd.components[0].reason == "Equity is negative (-Rp 100.0 bn)"
    assert abcd.components[1].reason == "Liabilities are 140% of assets"
    assert (abcd.period, abcd.source) == ("FY 2025", "xlsx")
    # Deposits make banks look levered; that is reported but not scored.
    bank = by_ticker["BANK"]
    assert bank.score == 0
    assert bank.components[0].reason == "Liabilities are 85% of assets; not scored for a financial company"
    assert by_ticker["GOOD"].score == 10
    assert [score.ticker for score in result.scores] == ["ABCD", "GOOD", "BANK"]

    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        stored = load_latest_exposure(conn, ticker="ABCD")
    finally:
        conn.close()
    assert stored["score"] == 70
    assert stored["as_of_date"] == "2026-10-20"
    assert stored["components"][0]["name"] == "negative_equity"


def test_breadth_measures_the_rise_in_stressed_filers(tmp_config):
    tmp_config.market.breadth_min_filers = 4
    tickers = ["AAAA", "BBBB", "CCCC", "DDDD"]
    negative_2025 = {"AAAA", "BBBB", "CCCC"}
    _seed(
        tmp_config,
        companies=[(ticker, "Energi") for ticker in tickers],
        documents=[(f"doc-{ticker}-{year}", ticker, "xlsx") for ticker in tickers for year in (2024, 2025)],
        metrics=[
            (f"doc-{ticker}-{year}", ticker, "operating_cash_flow", -1e9 if (year == 2025 and ticker in negative_2025) or (year == 2024 and ticker == "AAAA") else 1e9, year, f"{year}-12-31")
            for ticker in tickers
            for year in (2024, 2025)
        ],
    )

    result = run_exposure(tmp_config, as_of=date(2026, 10, 20))

    rows = {(row.scope, row.fiscal_year): row for row in result.breadth}
    assert rows[("market", 2024)].level == "insufficient_data"  # no prior year to compare with
    latest = rows[("market", 2025)]
    assert latest.shares["negative_ocf_share"] == 0.75
    assert (latest.change_pts, latest.level) == (50.0, "crisis")
    assert latest.details["matched_filers"]["negative_ocf_share"] == 4
    assert rows[("sector:Energi", 2025)].level == "crisis"

    alerts = run_alerts(tmp_config, now=datetime(2026, 10, 20, 9, tzinfo=timezone.utc))

    assert alerts.emitted == 1
    (alert,) = _jsonl_alerts(tmp_config)
    assert (alert["signal_kind"], alert["severity"]) == ("breadth", "critical")
    assert alert["claim"].startswith("Stress is spreading across 4 filers in FY2025: up to +50.0 pts")


def test_exposure_alerts_once_per_band_for_watchlist_names(tmp_config):
    _fragile_corpus(tmp_config)
    tmp_config.alerts.watchlist = ["ABCD", "BANK"]
    run_exposure(tmp_config, as_of=date(2026, 10, 20))

    first = run_alerts(tmp_config, now=datetime(2026, 10, 20, 9, tzinfo=timezone.utc))
    run_exposure(tmp_config, as_of=date(2026, 10, 21))
    second = run_alerts(tmp_config, now=datetime(2026, 10, 21, 9, tzinfo=timezone.utc))

    assert first.emitted == 1
    (alert,) = _jsonl_alerts(tmp_config)
    assert (alert["ticker"], alert["signal_kind"], alert["severity"]) == ("ABCD", "exposure", "warning")
    assert alert["claim"] == (
        "Exposure 70/100: Equity is negative (-Rp 100.0 bn); Liabilities are 140% of assets; Net loss of Rp 25.7 bn"
    )
    # Same band the next day: no repeat.
    assert (second.emitted, second.skipped_existing) == (0, 1)


def test_macos_channel_passes_alert_text_as_arguments(tmp_config, monkeypatch):
    _write_catalysts(
        tmp_config,
        'catalysts:\n  - {date: 2026-10-28, kind: custom, severity: high, title: \'Rate "shock" end tell; do shell script "x"\'}\n',
    )
    sync_catalysts(tmp_config)
    tmp_config.alerts.channels = ["macos"]
    calls = []
    monkeypatch.setattr(alerts_pipeline.sys, "platform", "darwin")
    monkeypatch.setattr(alerts_pipeline.shutil, "which", lambda name: "/usr/bin/osascript")
    monkeypatch.setattr(
        alerts_pipeline.subprocess,
        "run",
        lambda args, **kwargs: calls.append(args) or subprocess.CompletedProcess(args, 0),
    )

    result = run_alerts(tmp_config, now=datetime(2026, 10, 26, 9, tzinfo=timezone.utc))

    assert result.emitted == 1
    (args,) = calls
    assert args[:7] == [
        "osascript",
        "-e",
        "on run argv",
        "-e",
        "display notification (item 1 of argv) with title (item 2 of argv) subtitle (item 3 of argv)",
        "-e",
        "end run",
    ]
    # The text is data in argv, never part of the script.
    assert args[7] == 'Rate "shock" end tell; do shell script "x" on 2026-10-28 (in 2 days)'
    assert args[8:] == ["Stock-Triage · high", "catalyst"]


def test_alert_events_accepts_alerts_without_a_document(tmp_config):
    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)
    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        columns = {row["name"]: row["notnull"] for row in conn.execute("PRAGMA table_info(alert_events)")}
        indexes = {row["name"] for row in conn.execute("PRAGMA index_list(alert_events)")}
    finally:
        conn.close()
    assert columns["document_id"] == 0
    assert {"uq_alert_events_key_channel", "idx_alert_events_ticker", "idx_alert_events_created_at"} <= indexes


def test_company_detail_carries_the_exposure_score(tmp_config):
    _fragile_corpus(tmp_config)
    run_exposure(tmp_config, as_of=date(2026, 10, 20))

    detail = load_company_detail(tmp_config, "abcd")

    assert detail["exposure"]["score"] == 70
    assert detail["exposure"]["period"] == "FY 2025"
    assert load_company_detail(tmp_config, "NONE")["exposure"] is None


def test_breadth_compares_the_same_companies_across_years(tmp_config):
    tmp_config.market.breadth_min_filers = 2
    # FY2024: two healthy filers. FY2025: the same two still healthy, plus two
    # newly covered loss-makers. Their arrival is coverage, not spreading stress.
    _seed(
        tmp_config,
        companies=[(ticker, "Energi") for ticker in ("AAAA", "BBBB", "NEW1", "NEW2")],
        documents=[("d-a-24", "AAAA", "xlsx"), ("d-b-24", "BBBB", "xlsx"), ("d-a-25", "AAAA", "xlsx"),
                   ("d-b-25", "BBBB", "xlsx"), ("d-n1-25", "NEW1", "xlsx"), ("d-n2-25", "NEW2", "xlsx")],
        metrics=[
            ("d-a-24", "AAAA", "net_profit", 1e9, 2024, "2024-12-31"),
            ("d-b-24", "BBBB", "net_profit", 1e9, 2024, "2024-12-31"),
            ("d-a-25", "AAAA", "net_profit", 1e9, 2025, "2025-12-31"),
            ("d-b-25", "BBBB", "net_profit", 1e9, 2025, "2025-12-31"),
            ("d-n1-25", "NEW1", "net_profit", -1e9, 2025, "2025-12-31"),
            ("d-n2-25", "NEW2", "net_profit", -1e9, 2025, "2025-12-31"),
        ],
    )

    result = run_exposure(tmp_config, as_of=date(2026, 10, 20))

    latest = next(row for row in result.breadth if (row.scope, row.fiscal_year) == ("market", 2025))
    assert latest.shares["net_loss_share"] == 0.5  # among all four of this year's filers
    assert (latest.change_pts, latest.level) == (0.0, "none")


def test_desktop_notifications_are_capped_per_run(tmp_config, monkeypatch):
    # A backlog (here eight risk dates at once) must not arrive as eight popups.
    entries = "".join(
        f"  - {{date: 2026-10-2{day}, kind: custom, severity: {'high' if day == 7 else 'medium'}, title: Event {day}}}\n"
        for day in range(1, 9)
    )
    _write_catalysts(tmp_config, "catalysts:\n" + entries)
    sync_catalysts(tmp_config)
    tmp_config.market.catalyst_lead_days = 10
    tmp_config.alerts.channels = ["jsonl", "macos"]
    shown = []
    monkeypatch.setattr(alerts_pipeline.sys, "platform", "darwin")
    monkeypatch.setattr(alerts_pipeline.shutil, "which", lambda name: "/usr/bin/osascript")
    monkeypatch.setattr(
        alerts_pipeline.subprocess,
        "run",
        lambda args, **kwargs: shown.append(args[7]) or subprocess.CompletedProcess(args, 0),
    )

    result = run_alerts(tmp_config, now=datetime(2026, 10, 20, 9, tzinfo=timezone.utc))

    assert result.emitted == 16  # every alert is still recorded on both channels
    assert len(_jsonl_alerts(tmp_config)) == 8
    assert len(shown) == 5
    assert shown[0].startswith("Event 7")  # most severe first
    assert shown[-1] == "4 more alerts (4 catalyst); see the dashboard or alerts.jsonl"
