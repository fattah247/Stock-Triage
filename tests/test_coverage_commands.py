from __future__ import annotations

import argparse
import json
import threading
from urllib.request import urlopen

from stock_triage.commands.coverage import (
    cmd_coverage_plan_audit,
    cmd_coverage_plan_issuers_next,
    cmd_coverage_preview,
    cmd_coverage_status,
    cmd_nuance_audit,
)
from stock_triage.coverage.pipeline import (
    _build_action_plan,
    _blocked_reason_for,
    _enrich_coverage_rows_with_access_state,
    _persist_coverage_plan_action_event,
    _persist_coverage_rows,
    _prioritize_candidate_ids,
    _raw_blocked_reason_for,
    preview_coverage_action,
    run_coverage_action,
    BLOCK_CLASS_ACCESS_CONSTRAINT,
    BLOCK_CLASS_ORCHESTRATION_ERROR,
    BLOCK_CLASS_UPSTREAM_403_PERSISTENT,
    BLOCK_CLASS_UPSTREAM_403_TRANSIENT,
)
from stock_triage.coverage import load_coverage_plan, load_coverage_status
from stock_triage.dashboard.server import create_dashboard_server
from stock_triage.db import WriterQueue, connect, configure_pragmas, run_migrations
from stock_triage.discovery import persist_document_candidates
from stock_triage.sources import DocumentCandidate, SourceClass

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


def test_coverage_plan_commands_surface_bootstrap_and_audit(tmp_config, monkeypatch, capsys):
    _seed_phase10_inputs(tmp_config)
    _write_phase12_theses(tmp_config)
    load_coverage_status(
        tmp_config,
        scope="ticker-set",
        ticker="TAMU",
        limit=10,
        materialize=True,
    )
    monkeypatch.setattr("stock_triage.commands.coverage.load_config", lambda: tmp_config)

    plan_exit = cmd_coverage_plan_issuers_next(
        argparse.Namespace(
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
    plan_out = capsys.readouterr().out
    assert plan_exit == 0
    assert "coverage-plan:" in plan_out
    plan_payload = json.loads(plan_out.strip().splitlines()[-1])
    assert plan_payload["plan_mode"] in {"bootstrap", "adaptive"}
    assert "controller" in plan_payload
    assert plan_payload["controller"]["bootstrap_confidence"] in {"low", "learned"}
    assert plan_payload["rows"]
    assert plan_payload["recommended_tickers"] == ["TAMU"]

    audit_exit = cmd_coverage_plan_audit(argparse.Namespace(limit=10))
    audit_out = capsys.readouterr().out
    assert audit_exit == 0
    assert "coverage-plan-audit:" in audit_out
    audit_payload = json.loads(audit_out.strip().splitlines()[-1])
    assert audit_payload["summary"]["plan_generated_count"] >= 1
    assert any(row["event_type"] == "plan_generated" for row in audit_payload["rows"])


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
        assert "coverage_plan" in ops
        assert "coverage_plan_audit" in ops
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
    raw = _raw_blocked_reason_for(
        matched_documents=[],
        matched_candidates=[
            {"status": "failed", "last_error_code": "Error"},
            {"status": "failed", "last_error_code": "http_403"},
        ],
    )
    reason = _blocked_reason_for(raw)

    assert reason == "upstream_403_transient"


def test_persist_coverage_rows_tracks_artifact_access_state(tmp_config):
    now = "2026-04-19T00:00:00+00:00"
    conn = connect(tmp_config.paths.db_path, read_only=False)
    try:
        configure_pragmas(conn, tmp_config)
        run_migrations(conn)
        conn.execute(
            """
            INSERT INTO document_candidates (
                candidate_id, source_name, source_record_id, ticker, title, upstream_published_at,
                attachment_url, raw_metadata, discovered_at, queued
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "cand-1",
                "idx_financial_reports",
                "rec-1",
                "TAMU",
                "Financial Statement 2025",
                None,
                "https://example.test/fs.pdf",
                "{}",
                now,
                0,
            ),
        )
        conn.execute(
            """
            INSERT INTO download_resources (
                resource_id, candidate_id, source_name, source_record_id, source_file_id,
                attachment_url, attachment_filename, status, document_id, attempt_count, failure_count,
                last_attempted_at, next_attempt_at, last_http_status, last_error_code,
                last_error_detail, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "res-1",
                "cand-1",
                "idx_financial_reports",
                "rec-1",
                "file-1",
                "https://example.test/fs.pdf",
                "fs.pdf",
                "failed",
                None,
                1,
                1,
                now,
                None,
                403,
                "http_403",
                "forbidden",
                now,
                now,
            ),
        )
        _persist_coverage_rows(
            conn,
            rows=[
                {
                    "coverage_id": "cov-1",
                    "ticker": "TAMU",
                    "source_name": "idx_financial_reports",
                    "obligation_type": "annual_fs",
                    "document_type": "financial_statement",
                    "fiscal_year": 2025,
                    "period_label": "Annual FS",
                    "period_end_date": "2025-12-31",
                    "window_start": "2026-03-30",
                    "deadline_at": "2026-04-29",
                    "expected_state": "late",
                    "actual_state": "blocked",
                    "candidate_count": 1,
                    "downloaded_count": 0,
                    "analyzed_count": 0,
                    "blocked_reason": "upstream_403_transient",
                    "knowledge_source": "default",
                    "payload_json": json.dumps({"matched_candidate_ids": ["cand-1"]}),
                    "coverage_version": "v3_4-v1",
                    "updated_at": now,
                }
            ],
            tickers=["TAMU"],
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT candidate_id, block_class, endpoint_family, retry_count, retry_eligible_until, last_http_status, last_error_code
            FROM artifact_access_state
            WHERE artifact_id = ?
            """,
            ("cand-1",),
        ).fetchone()
        assert row is not None
        assert row["candidate_id"] == "cand-1"
        assert row["block_class"] == "upstream_403_transient"
        assert row["endpoint_family"] == "idx_financial_reports_artifact"
        assert row["retry_count"] == 1
        assert row["retry_eligible_until"] is not None
        assert row["last_http_status"] == 403
        assert row["last_error_code"] == "http_403"
    finally:
        conn.close()


def test_persist_coverage_rows_promotes_403_to_persistent_then_access_constraint(tmp_config):
    now = "2026-04-19T00:00:00+00:00"
    conn = connect(tmp_config.paths.db_path, read_only=False)
    try:
        configure_pragmas(conn, tmp_config)
        run_migrations(conn)
        conn.execute(
            """
            INSERT INTO document_candidates (
                candidate_id, source_name, source_record_id, ticker, title, upstream_published_at,
                attachment_url, raw_metadata, discovered_at, queued
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "cand-403",
                "idx_financial_reports",
                "rec-403",
                "TAMU",
                "Financial Statement 2025",
                None,
                "https://example.test/fs.pdf",
                "{}",
                now,
                0,
            ),
        )
        conn.execute(
            """
            INSERT INTO download_resources (
                resource_id, candidate_id, source_name, source_record_id, source_file_id,
                attachment_url, attachment_filename, status, document_id, attempt_count, failure_count,
                last_attempted_at, next_attempt_at, last_http_status, last_error_code,
                last_error_detail, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "res-403",
                "cand-403",
                "idx_financial_reports",
                "rec-403",
                "file-403",
                "https://example.test/fs.pdf",
                "fs.pdf",
                "failed",
                None,
                1,
                1,
                now,
                None,
                403,
                "http_403",
                "forbidden",
                now,
                now,
            ),
        )
        row = {
            "coverage_id": "cov-403",
            "ticker": "TAMU",
            "source_name": "idx_financial_reports",
            "obligation_type": "annual_fs",
            "document_type": "financial_statement",
            "fiscal_year": 2025,
            "period_label": "Annual FS",
            "period_end_date": "2025-12-31",
            "window_start": "2026-03-30",
            "deadline_at": "2026-04-01",
            "expected_state": "late",
            "actual_state": "blocked",
            "candidate_count": 1,
            "downloaded_count": 0,
            "analyzed_count": 0,
            "blocked_reason": BLOCK_CLASS_UPSTREAM_403_TRANSIENT,
            "knowledge_source": "default",
            "payload_json": json.dumps({"matched_candidate_ids": ["cand-403"]}),
            "coverage_version": "v3_4-v1",
            "updated_at": now,
        }
        _persist_coverage_rows(conn, rows=[row], tickers=["TAMU"])
        first = conn.execute(
            "SELECT block_class, retry_count, needs_alternative_source_check, retry_eligible_until FROM artifact_access_state WHERE artifact_id = ?",
            ("cand-403",),
        ).fetchone()
        _persist_coverage_rows(conn, rows=[row], tickers=["TAMU"])
        second = conn.execute(
            "SELECT block_class, retry_count, needs_alternative_source_check, retry_eligible_until FROM artifact_access_state WHERE artifact_id = ?",
            ("cand-403",),
        ).fetchone()
        _persist_coverage_rows(conn, rows=[row], tickers=["TAMU"])
        third = conn.execute(
            "SELECT block_class, retry_count, needs_alternative_source_check, retry_eligible_until FROM artifact_access_state WHERE artifact_id = ?",
            ("cand-403",),
        ).fetchone()
        assert first["block_class"] == BLOCK_CLASS_UPSTREAM_403_TRANSIENT
        assert first["retry_count"] == 1
        assert first["needs_alternative_source_check"] == 0
        assert first["retry_eligible_until"] is not None
        assert second["block_class"] == BLOCK_CLASS_UPSTREAM_403_PERSISTENT
        assert second["retry_count"] == 2
        assert second["needs_alternative_source_check"] == 1
        assert second["retry_eligible_until"] is not None
        assert third["block_class"] == BLOCK_CLASS_ACCESS_CONSTRAINT
        assert third["retry_count"] == 3
        assert third["needs_alternative_source_check"] == 1
        assert third["retry_eligible_until"] is None
    finally:
        conn.close()


def test_coverage_status_splits_blocked_rows_by_operator_bucket(tmp_config):
    conn = connect(tmp_config.paths.db_path, read_only=False)
    try:
        configure_pragmas(conn, tmp_config)
        run_migrations(conn)
        conn.execute(
            """
            INSERT INTO filing_coverage (
                coverage_id, ticker, source_name, obligation_type, document_type, fiscal_year,
                period_label, period_end_date, window_start, deadline_at, expected_state, actual_state,
                candidate_count, downloaded_count, analyzed_count, blocked_reason, knowledge_source,
                payload_json, coverage_version, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "cov-fixable",
                "TAMU",
                "idx_financial_reports",
                "annual_fs",
                "financial_statement",
                2025,
                "Annual FS",
                "2025-12-31",
                "2026-03-30",
                "2026-04-29",
                "late",
                "blocked",
                0,
                0,
                0,
                BLOCK_CLASS_ORCHESTRATION_ERROR,
                "default",
                json.dumps({"matched_document_ids": ["doc-fixable"]}),
                "v3_4-v1",
                "2026-04-19T00:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO filing_coverage (
                coverage_id, ticker, source_name, obligation_type, document_type, fiscal_year,
                period_label, period_end_date, window_start, deadline_at, expected_state, actual_state,
                candidate_count, downloaded_count, analyzed_count, blocked_reason, knowledge_source,
                payload_json, coverage_version, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "cov-constrained",
                "TAMU",
                "idx_financial_reports",
                "interim_fs",
                "financial_statement",
                2026,
                "Interim FS",
                "2026-06-30",
                "2026-07-30",
                "2026-08-31",
                "late",
                "blocked",
                1,
                0,
                0,
                BLOCK_CLASS_UPSTREAM_403_TRANSIENT,
                "default",
                json.dumps({"matched_candidate_ids": ["cand-constraint"]}),
                "v3_4-v1",
                "2026-04-19T00:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO artifact_access_state (
                artifact_id, candidate_id, source_name, ticker, endpoint_family, document_type,
                first_blocked_at, last_blocked_at, last_success_at, retry_count, block_class,
                retry_eligible_until, needs_alternative_source_check, last_http_status, last_error_code,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "cand-constraint",
                "cand-constraint",
                "idx_financial_reports",
                "TAMU",
                "idx_financial_reports_artifact",
                "financial_statement",
                "2026-04-17T00:00:00+00:00",
                "2026-04-19T00:00:00+00:00",
                None,
                3,
                BLOCK_CLASS_ACCESS_CONSTRAINT,
                None,
                1,
                403,
                "http_403",
                "2026-04-17T00:00:00+00:00",
                "2026-04-19T00:00:00+00:00",
            ),
        )
        conn.commit()

        report = load_coverage_status(
            tmp_config,
            scope="ticker-set",
            ticker="TAMU",
            limit=10,
            materialize=False,
        )

        assert report["summary"]["blocked_count"] == 2
        assert report["summary"]["blocked_fixable_count"] == 1
        assert report["summary"]["blocked_retryable_count"] == 0
        assert report["summary"]["blocked_access_constrained_count"] == 1
        constrained = next(row for row in report["gap_leaders"] if row["coverage_id"] == "cov-constrained")
        assert constrained["effective_block_class"] == BLOCK_CLASS_ACCESS_CONSTRAINT
        assert constrained["blocked_bucket"] == "access_constrained"
        assert constrained["needs_alternative_source_check"] is True
    finally:
        conn.close()


def test_rerun_blocked_plan_excludes_access_constraints(tmp_config):
    conn = connect(tmp_config.paths.db_path, read_only=False)
    try:
        configure_pragmas(conn, tmp_config)
        run_migrations(conn)
        rows = _enrich_coverage_rows_with_access_state(
            conn,
            [
                {
                    "ticker": "TAMU",
                    "payload_json": json.dumps({"matched_document_ids": ["doc-fixable"]}),
                    "actual_state": "blocked",
                    "blocked_reason": BLOCK_CLASS_ORCHESTRATION_ERROR,
                },
                {
                    "ticker": "TAMU",
                    "payload_json": json.dumps({"matched_document_ids": ["doc-constrained"], "matched_candidate_ids": ["cand-constraint"]}),
                    "actual_state": "blocked",
                    "blocked_reason": BLOCK_CLASS_UPSTREAM_403_TRANSIENT,
                },
            ],
        )
        conn.execute(
            """
            INSERT INTO artifact_access_state (
                artifact_id, candidate_id, source_name, ticker, endpoint_family, document_type,
                first_blocked_at, last_blocked_at, last_success_at, retry_count, block_class,
                retry_eligible_until, needs_alternative_source_check, last_http_status, last_error_code,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "cand-constraint",
                "cand-constraint",
                "idx_financial_reports",
                "TAMU",
                "idx_financial_reports_artifact",
                "financial_statement",
                "2026-04-17T00:00:00+00:00",
                "2026-04-19T00:00:00+00:00",
                None,
                3,
                BLOCK_CLASS_ACCESS_CONSTRAINT,
                None,
                1,
                403,
                "http_403",
                "2026-04-17T00:00:00+00:00",
                "2026-04-19T00:00:00+00:00",
            ),
        )
        conn.commit()
        rows = _enrich_coverage_rows_with_access_state(conn, rows)
        plan = _build_action_plan(conn, ["TAMU"], rows, action="rerun-blocked", limit=10)
        assert plan["targets"] == ["doc-fixable"]
    finally:
        conn.close()


def test_action_event_logs_divergence_to_plan_audit(tmp_config):
    _seed_phase10_inputs(tmp_config)
    _write_phase12_theses(tmp_config)
    load_coverage_status(
        tmp_config,
        scope="ticker-set",
        ticker="TAMU",
        limit=10,
        materialize=True,
    )
    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)

        def _write_audit(conn):
            _persist_coverage_plan_action_event(
                conn,
                tmp_config,
                action="discover-missing",
                preview={
                    "date_from": "2024-01-01",
                    "date_to": "2026-12-31",
                    "scope_label": "all",
                    "target_tickers": ["TAMU", "ZZZZ"],
                },
                scope="watchlist",
                sector=None,
                ticker="TAMU",
                tickers=None,
                source_name=None,
                document_type=None,
                limit=5,
            )

        wq.submit(_write_audit)

    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(conn, tmp_config)
        row = conn.execute(
            """
            SELECT event_type, payload_json
            FROM coverage_plan_audit_log
            ORDER BY created_at DESC, audit_id DESC
            LIMIT 1
            """
        ).fetchone()
        assert row["event_type"] == "action_diverged"
        payload = json.loads(row["payload_json"])
        assert "ZZZZ" in payload["extra_targeted_tickers"]
    finally:
        conn.close()


def test_coverage_controller_adapts_with_telemetry_and_caps_preview_limit(tmp_config):
    _seed_phase10_inputs(tmp_config)
    _write_phase12_theses(tmp_config)
    load_coverage_status(
        tmp_config,
        scope="ticker-set",
        ticker="TAMU",
        source_name="idx_financial_reports",
        document_type="financial_statement",
        limit=20,
        materialize=True,
    )

    conn = connect(tmp_config.paths.db_path, read_only=False)
    try:
        configure_pragmas(conn, tmp_config)
        run_migrations(conn)
        for idx in range(20):
            conn.execute(
                """
                INSERT INTO coverage_plan_audit_log (
                    audit_id, event_type, plan_mode, scope_label, action, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"audit-{idx}",
                    "action_followed",
                    "adaptive",
                    "tickers:TAMU",
                    "download-missing",
                    "{}",
                    f"2026-04-2{idx % 9}T00:00:0{idx % 10}+00:00",
                ),
            )
        for idx in range(40):
            created_at = f"2026-04-2{idx % 9}T00:00:0{idx % 10}+00:00"
            conn.execute(
                """
                INSERT INTO coverage_request_telemetry (
                    telemetry_id, action, source_name, endpoint_family, transport_mode, ticker,
                    document_type, candidate_id, document_id, result_class, http_status, latency_ms,
                    delay_applied_ms, controller_mode, source_health, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"telemetry-{idx}",
                    "download-missing",
                    "idx_financial_reports",
                    "idx_financial_reports_artifact",
                    "http",
                    "TAMU",
                    "financial_statement",
                    None,
                    None,
                    "http_403",
                    403,
                    120,
                    1500,
                    "adaptive",
                    "degraded",
                    "{}",
                    created_at,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    preview = preview_coverage_action(
        tmp_config,
        action="download-missing",
        scope="ticker-set",
        ticker="TAMU",
        source_name="idx_financial_reports",
        document_type="financial_statement",
        limit=20,
    )
    plan = load_coverage_plan(
        tmp_config,
        scope="ticker-set",
        ticker="TAMU",
        source_name="idx_financial_reports",
        document_type="financial_statement",
        limit=5,
        materialize=False,
    )

    assert preview["controller"]["plan_mode"] == "adaptive"
    assert preview["controller"]["source_health"] == "constrained"
    assert preview["controller"]["bootstrap_confidence"] == "learned"
    assert preview["effective_limit"] == 3
    assert preview["controller"]["recommended_delay_ms"] == 3000
    assert plan["plan_mode"] == "adaptive"
    assert plan["controller"]["source_health"] == "constrained"
    assert plan["rows"][0]["selection_reasons"]
    assert plan["rows"][0]["proposed_filing_targets"]


def test_run_coverage_action_passes_controller_pressure_into_download(tmp_config, monkeypatch):
    _seed_phase10_inputs(tmp_config)
    _write_phase12_theses(tmp_config)
    load_coverage_status(
        tmp_config,
        scope="ticker-set",
        ticker="TAMU",
        source_name="idx_financial_reports",
        document_type="financial_statement",
        limit=20,
        materialize=True,
    )

    conn = connect(tmp_config.paths.db_path, read_only=False)
    try:
        configure_pragmas(conn, tmp_config)
        run_migrations(conn)
        for idx in range(20):
            conn.execute(
                """
                INSERT INTO coverage_plan_audit_log (
                    audit_id, event_type, plan_mode, scope_label, action, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"action-audit-{idx}",
                    "action_followed",
                    "adaptive",
                    "tickers:TAMU",
                    "download-missing",
                    "{}",
                    f"2026-04-21T00:10:{idx:02d}+00:00",
                ),
            )
        for idx in range(40):
            conn.execute(
                """
                INSERT INTO coverage_request_telemetry (
                    telemetry_id, action, source_name, endpoint_family, transport_mode, ticker,
                    document_type, candidate_id, document_id, result_class, http_status, latency_ms,
                    delay_applied_ms, controller_mode, source_health, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"pressure-{idx}",
                    "download-missing",
                    "idx_financial_reports",
                    "idx_financial_reports_artifact",
                    "http",
                    "TAMU",
                    "financial_statement",
                    None,
                    None,
                    "http_403",
                    403,
                    100,
                    1500,
                    "adaptive",
                    "degraded",
                    "{}",
                    f"2026-04-21T00:00:{idx:02d}+00:00",
                ),
            )
        conn.commit()
    finally:
        conn.close()

    observed: dict[str, object] = {}

    def _fake_download(cfg, *, candidate_ids, controller_mode, source_health, delay_ms):
        observed["candidate_ids"] = list(candidate_ids)
        observed["controller_mode"] = controller_mode
        observed["source_health"] = source_health
        observed["delay_ms"] = delay_ms
        return len(candidate_ids)

    monkeypatch.setattr("stock_triage.coverage.pipeline._run_download_for_scope", _fake_download)

    result = run_coverage_action(
        tmp_config,
        action="download-missing",
        scope="ticker-set",
        ticker="TAMU",
        source_name="idx_financial_reports",
        document_type="financial_statement",
        limit=20,
    )

    assert result["controller"]["source_health"] == "constrained"
    assert result["controller"]["bootstrap_confidence"] == "learned"
    assert observed["controller_mode"] == "adaptive"
    assert observed["source_health"] == "constrained"
    assert observed["delay_ms"] == 3000


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


def test_prioritize_candidate_ids_collapses_financial_report_siblings(tmp_config):
    candidates = [
        DocumentCandidate(
            source_name="idx_financial_reports",
            source_class=SourceClass.FINANCIAL_REPORT,
            source_record_id="pdf-1",
            ticker="ABDA",
            title="FinancialStatement-2025-Tahunan-ABDA.pdf",
            attachment_url="https://example.test/abda/FinancialStatement-2025-Tahunan-ABDA.pdf",
            attachment_filename="FinancialStatement-2025-Tahunan-ABDA.pdf",
            raw_metadata={"report_year": "2025", "report_period": "Audit", "report_type": "rdf", "file_type": ".pdf"},
        ),
        DocumentCandidate(
            source_name="idx_financial_reports",
            source_class=SourceClass.FINANCIAL_REPORT,
            source_record_id="zip-1",
            ticker="ABDA",
            title="instance.zip",
            attachment_url="https://example.test/abda/instance.zip",
            attachment_filename="instance.zip",
            raw_metadata={"report_year": "2025", "report_period": "Audit", "report_type": "rdf", "file_type": ".zip"},
        ),
        DocumentCandidate(
            source_name="idx_financial_reports",
            source_class=SourceClass.FINANCIAL_REPORT,
            source_record_id="xlsx-1",
            ticker="ABDA",
            title="FinancialStatement-2025-Tahunan-ABDA.xlsx",
            attachment_url="https://example.test/abda/FinancialStatement-2025-Tahunan-ABDA.xlsx",
            attachment_filename="FinancialStatement-2025-Tahunan-ABDA.xlsx",
            raw_metadata={"report_year": "2025", "report_period": "Audit", "report_type": "rdf", "file_type": ".xlsx"},
        ),
    ]
    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)
        wq.submit(lambda conn: persist_document_candidates(conn, candidates))

    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(conn, tmp_config)
        ranked = _prioritize_candidate_ids(
            conn,
            [f"idx_financial_reports:{candidate.source_record_id}" for candidate in candidates],
        )
    finally:
        conn.close()

    assert ranked == ["idx_financial_reports:pdf-1"]
