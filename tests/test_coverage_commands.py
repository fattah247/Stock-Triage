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
from stock_triage.coverage import load_coverage_status
from stock_triage.dashboard.server import create_dashboard_server
from stock_triage.db import connect, configure_pragmas

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
    finally:
        handle.server.shutdown()
        handle.server.server_close()
        thread.join(timeout=5)
