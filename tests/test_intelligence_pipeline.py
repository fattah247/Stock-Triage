from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

from stock_triage.__main__ import cmd_intelligence_run
from stock_triage.db import WriterQueue, connect, configure_pragmas, run_migrations
from stock_triage.intelligence import DERIVED_VERSION, run_intelligence

from .test_phase8_ops import _seed_phase8_data


def test_run_intelligence_persists_phase10_foundation_rows(tmp_config):
    _seed_phase10_inputs(tmp_config)

    result = run_intelligence(tmp_config, ticker="TAMU")

    assert result.selected_tickers == 1
    assert result.deltas == 1
    assert result.snapshots == 1
    assert result.timeseries == 2
    assert result.briefs == 1
    assert result.review_items == 2
    assert result.peer_sets == 1
    assert result.sector_rollups == 1

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        delta = rconn.execute(
            """
            SELECT metric_name, previous_value, current_value, absolute_change, percent_change
            FROM document_deltas
            WHERE ticker = ?
            """,
            ("TAMU",),
        ).fetchone()
        assert delta is not None
        assert delta["metric_name"] == "revenue"
        assert delta["previous_value"] == 4000000.0
        assert delta["current_value"] == 5082099.0
        assert delta["absolute_change"] == 1082099.0
        assert round(delta["percent_change"], 2) == 27.05

        snapshot = rconn.execute(
            """
            SELECT as_of_document_id, payload_json
            FROM company_snapshots
            WHERE ticker = ? AND snapshot_kind = 'current' AND derived_version = ?
            """,
            ("TAMU", DERIVED_VERSION),
        ).fetchone()
        assert snapshot is not None
        payload = json.loads(snapshot["payload_json"])
        assert snapshot["as_of_document_id"] == "doc-tamu-a"
        assert payload["latest_summary"] == "Debt refinancing remains a monitoring point."
        assert payload["top_deltas"][0]["metric_name"] == "revenue"

        counts = {
            table: rconn.execute(f"SELECT COUNT(*) AS cnt FROM {table}").fetchone()["cnt"]
            for table in (
                "company_timeseries",
                "decision_briefs",
                "review_queue_items",
                "peer_sets",
                "sector_rollups",
            )
        }
        assert counts == {
            "company_timeseries": 2,
            "decision_briefs": 1,
            "review_queue_items": 2,
            "peer_sets": 1,
            "sector_rollups": 1,
        }
    finally:
        rconn.close()


def test_run_intelligence_is_rerunnable_without_duplicate_rows(tmp_config):
    _seed_phase10_inputs(tmp_config)

    first = run_intelligence(tmp_config, ticker="TAMU")
    second = run_intelligence(tmp_config, ticker="TAMU")

    assert first.deltas == second.deltas == 1
    assert first.snapshots == second.snapshots == 1

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        counts = {
            table: rconn.execute(f"SELECT COUNT(*) AS cnt FROM {table}").fetchone()["cnt"]
            for table in (
                "document_deltas",
                "company_snapshots",
                "company_timeseries",
                "decision_briefs",
                "review_queue_items",
                "peer_sets",
                "sector_rollups",
            )
        }
        assert counts == {
            "document_deltas": 1,
            "company_snapshots": 1,
            "company_timeseries": 2,
            "decision_briefs": 1,
            "review_queue_items": 2,
            "peer_sets": 1,
            "sector_rollups": 1,
        }
    finally:
        rconn.close()


def test_intelligence_cli_uses_phase10_runner(tmp_config, monkeypatch, capsys):
    _seed_phase10_inputs(tmp_config)
    monkeypatch.setattr("stock_triage.__main__.load_config", lambda: tmp_config)
    monkeypatch.setattr(
        "stock_triage.__main__.run_intelligence",
        lambda cfg, ticker, limit: run_intelligence(tmp_config, ticker=ticker, limit=limit),
    )

    exit_code = cmd_intelligence_run(argparse.Namespace(ticker="TAMU", limit=5))

    captured = capsys.readouterr().out
    assert exit_code == 0
    assert "intelligence-run:" in captured
    assert "derived_version=phase10-v1" in captured
    assert "selected_tickers=1" in captured


def _seed_phase10_inputs(tmp_config) -> None:
    _seed_phase8_data(tmp_config)
    now = datetime.now(timezone.utc)
    older = (now - timedelta(days=365)).isoformat()
    newer = now.isoformat()

    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)

        def _insert(conn):
            conn.execute(
                """
                UPDATE documents
                SET source_modified_at = ?, updated_at = ?, created_at = ?
                WHERE document_id = 'doc-tamu-b'
                """,
                (older, older, older),
            )
            conn.execute(
                """
                UPDATE documents
                SET source_modified_at = ?, updated_at = ?, created_at = ?
                WHERE document_id = 'doc-tamu-a'
                """,
                (newer, newer, newer),
            )
            conn.execute(
                """
                INSERT INTO metric_items (
                    metric_id, document_id, ticker, metric_name, value_normalized, raw_value_text, currency,
                    unit_scale, fiscal_year, fiscal_period, period_end_date, audit_status, consolidation_scope,
                    language_variant, evidence_span_id, extracted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "metric-tamu-revenue-prev",
                    "doc-tamu-b",
                    "TAMU",
                    "revenue",
                    4000000.0,
                    "4,000,000",
                    "USD",
                    "units",
                    2024,
                    "Annual",
                    "2024-12-31",
                    "audited",
                    "consolidated",
                    "bilingual",
                    "span-tamu-1",
                    older,
                ),
            )
            conn.execute(
                """
                INSERT INTO companies (
                    ticker, issuer_name, listing_date, active_status, sector, subsector, industry,
                    listing_board, source_urls, raw_metadata, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "SAFE",
                    "PT Safety Shipping Tbk",
                    "2018-01-01",
                    "listed",
                    "Transportation",
                    "Marine Logistics",
                    "Shipping",
                    "main",
                    json.dumps(["https://www.idx.co.id"]),
                    json.dumps({}),
                    newer,
                    newer,
                ),
            )

        wq.submit(_insert)
