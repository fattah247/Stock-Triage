from __future__ import annotations

import argparse
import json
import threading
from urllib.request import urlopen

from stock_triage.commands.coverage import (
    cmd_coverage_preview,
    cmd_coverage_status,
    cmd_nuance_audit,
)
from stock_triage.coverage.pipeline import _blocked_reason_for, _persist_coverage_rows
from stock_triage.coverage import load_coverage_status
from stock_triage.dashboard.server import create_dashboard_server
from stock_triage.db import connect, configure_pragmas, run_migrations

from .test_intelligence_pipeline import _seed_phase10_inputs, _write_phase12_theses


def test_coverage_status_materializes_rows_and_prints_summary(tmp_config, monkeypatch, capsys):
    _seed_phase10_inputs(tmp_config)
    _write_phase12_theses(tmp_config)
    monkeypatch.setattr("stock_triage.commands.coverage.load_config", lambda: tmp_config)

    exit_code = cmd_coverage_status(
        argparse.Namespace(
            date_from="2024-01-01",
            date_to="2026-12-31",
            scope="ticker-set",
            sector=None,
            ticker="TAMU",
            tickers=None,
            source=None,
            document_type=None,
            limit=10,
        )
    )

    captured = capsys.readouterr().out
    assert exit_code == 0
    assert "coverage-status:" in captured
    assert "scope=ticker-set" in captured

    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(conn, tmp_config)
        row = conn.execute(
            """
            SELECT ticker, obligation_type, expected_state, actual_state, candidate_count, analyzed_count
            FROM filing_coverage
            WHERE ticker = ?
            ORDER BY deadline_at ASC
            LIMIT 1
            """,
            ("TAMU",),
        ).fetchone()
        assert row is not None
        assert row["ticker"] == "TAMU"
        assert row["obligation_type"] in {"quarterly_fs", "interim_fs", "annual_fs", "annual_report"}
        assert row["expected_state"] in {"upcoming", "due", "late"}
        assert row["actual_state"] in {"analyzed", "downloaded", "discovered", "blocked", "unknown"}
    finally:
        conn.close()


def test_coverage_preview_and_nuance_audit_commands_are_scoped(tmp_config, monkeypatch, capsys):
    _seed_phase10_inputs(tmp_config)
    _write_phase12_theses(tmp_config)
    load_coverage_status(
        tmp_config,
        date_from=None,
        date_to=None,
        scope="ticker-set",
        ticker="TAMU",
        tickers=None,
        limit=10,
        materialize=True,
    )
    monkeypatch.setattr("stock_triage.commands.coverage.load_config", lambda: tmp_config)

    preview_exit = cmd_coverage_preview(
        argparse.Namespace(
            action="process-missing",
            date_from="2024-01-01",
            date_to="2026-12-31",
            scope="ticker-set",
            sector=None,
            ticker="TAMU",
            tickers=None,
            source=None,
            document_type=None,
            limit=5,
        )
    )
    preview_out = capsys.readouterr().out
    assert preview_exit == 0
    assert "coverage-preview:" in preview_out
    assert "scope=tickers:TAMU" in preview_out

    audit_exit = cmd_nuance_audit(argparse.Namespace(ticker="TAMU", limit=5))
    audit_out = capsys.readouterr().out
    assert audit_exit == 0
    assert "nuance-audit:" in audit_out
    assert "target_ticker=TAMU" in audit_out
    audit_payload = json.loads(audit_out.strip().splitlines()[-1])
    assert audit_payload["audit"]["scope"] == "ticker"
    assert audit_payload["audit"]["target_ticker"] == "TAMU"
    assert "summary" in audit_payload["audit"]
    target = audit_payload["audit"]["target"]
    assert target["ticker"] == "TAMU"
    if target.get("bridge"):
        assert "recommended_command" in target["bridge"]


def test_dashboard_snapshot_exposes_coverage_panels(tmp_config):
    _seed_phase10_inputs(tmp_config)
    _write_phase12_theses(tmp_config)
    load_coverage_status(
        tmp_config,
        date_from=None,
        date_to=None,
        scope="ticker-set",
        ticker="TAMU",
        tickers=None,
        limit=10,
        materialize=True,
    )

    handle = create_dashboard_server(tmp_config, host="127.0.0.1", port=0)
    thread = threading.Thread(target=handle.server.serve_forever, daemon=True)
    thread.start()
    try:
        app_js = urlopen(f"{handle.url}/static/app.js").read().decode("utf-8")
        assert "Coverage gap leaders" in app_js

        snapshot = json.loads(urlopen(f"{handle.url}/api/snapshot").read().decode("utf-8"))
        assert "coverage_status" in snapshot["market_monitor"]
        assert "coverage_gap_leaders" in snapshot["market_monitor"]

        ops = json.loads(urlopen(f"{handle.url}/api/detail?section=ops").read().decode("utf-8"))
        assert "coverage_status" in ops
        assert "summary" in ops["coverage_status"]
        assert "nuance_audit" in ops
        assert "summary" in ops["nuance_audit"]
    finally:
        handle.server.shutdown()
        handle.server.server_close()
        thread.join(timeout=5)


def test_persist_coverage_rows_upserts_duplicate_keys(tmp_config):
    row_a = {
        "coverage_id": "cov-a",
        "ticker": "TAMU",
        "source_name": "idx_financial_reports",
        "obligation_type": "annual_fs",
        "document_type": "financial_statement",
        "fiscal_year": 2024,
        "period_label": "Annual FS",
        "period_end_date": "2024-12-31",
        "window_start": "2025-03-30",
        "deadline_at": "2025-04-29",
        "expected_state": "late",
        "actual_state": "unknown",
        "candidate_count": 0,
        "downloaded_count": 0,
        "analyzed_count": 0,
        "blocked_reason": None,
        "knowledge_source": "default",
        "payload_json": "{}",
        "coverage_version": "v3_4-v1",
        "updated_at": "2026-04-18T00:00:00+00:00",
    }
    row_b = dict(row_a)
    row_b["coverage_id"] = "cov-b"
    row_b["candidate_count"] = 2
    row_b["actual_state"] = "discovered"

    conn = connect(tmp_config.paths.db_path, read_only=False)
    try:
        configure_pragmas(conn, tmp_config)
        run_migrations(conn)
        _persist_coverage_rows(conn, rows=[row_a, row_b], tickers=["TAMU"])
        conn.commit()
        row = conn.execute(
            """
            SELECT coverage_id, actual_state, candidate_count
            FROM filing_coverage
            WHERE ticker = ? AND obligation_type = ? AND period_end_date = ? AND coverage_version = ?
            """,
            ("TAMU", "annual_fs", "2024-12-31", "v3_4-v1"),
        ).fetchone()
        assert row is not None
        assert row["coverage_id"] == "cov-b"
        assert row["actual_state"] == "discovered"
        assert row["candidate_count"] == 2
    finally:
        conn.close()


def test_blocked_reason_prefers_specific_candidate_error_code():
    reason = _blocked_reason_for(
        matched_documents=[],
        matched_candidates=[
            {"status": "failed", "last_error_code": "Error"},
            {"status": "failed", "last_error_code": "http_403"},
        ],
    )

    assert reason == "http_403"


def test_coverage_status_filters_by_source_and_document_type(tmp_config):
    _seed_phase10_inputs(tmp_config)
    _write_phase12_theses(tmp_config)

    report = load_coverage_status(
        tmp_config,
        scope="ticker-set",
        ticker="TAMU",
        source_name="idx_financial_reports",
        document_type="financial_statement",
        limit=20,
        materialize=True,
    )

    assert report["summary"]["expected_count"] > 0
    assert all(row["source_name"] == "idx_financial_reports" for row in report["gap_leaders"])
    assert all(row["document_type"] == "financial_statement" for row in report["gap_leaders"])
