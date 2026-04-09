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
    _write_phase12_theses(tmp_config)

    result = run_intelligence(tmp_config, ticker="TAMU")

    assert result.selected_tickers == 1
    assert result.deltas == 6
    assert result.snapshots == 1
    assert result.timeseries == 3
    assert result.briefs == 1
    assert result.review_items == 4
    assert result.peer_sets == 1
    assert result.sector_rollups == 1
    assert result.thesis_evaluations == 1

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        deltas = rconn.execute(
            """
            SELECT delta_kind, metric_name, previous_value, current_value, absolute_change, percent_change, payload_json
            FROM document_deltas
            WHERE ticker = ?
            ORDER BY delta_kind, metric_name
            """,
            ("TAMU",),
        ).fetchall()
        assert len(deltas) == 6
        metric_delta = next(row for row in deltas if row["delta_kind"] == "metric_change")
        assert metric_delta["metric_name"] == "revenue"
        assert metric_delta["previous_value"] == 4000000.0
        assert metric_delta["current_value"] == 5082099.0
        assert metric_delta["absolute_change"] == 1082099.0
        assert round(metric_delta["percent_change"], 2) == 27.05
        metric_payload = json.loads(metric_delta["payload_json"])
        assert metric_payload["baseline_kind"] == "revision"
        assert metric_payload["current_evidence_span_id"] == "span-tamu-1"

        assert {row["delta_kind"] for row in deltas} == {
            "event_addition",
            "event_resolution",
            "metric_change",
            "narrative_change",
            "rule_addition",
            "rule_resolution",
        }

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
        assert payload["baseline"]["kind"] == "revision"
        assert payload["baseline"]["document_id"] == "doc-tamu-b"
        assert payload["top_card"]["overall_state"] == "critical"
        assert payload["top_card"]["leverage_state"] == "warning"
        assert payload["top_card"]["thesis_state"] == "broken"
        assert payload["top_card"]["decision_state"] == "Avoid Candidate"
        assert payload["top_card"]["watchlist_priority"] == "high"
        assert "revenue increased" in payload["top_card"]["positive_factors"]
        assert "debt_surge appeared" in payload["top_card"]["negative_factors"]
        assert payload["delta_count_by_kind"]["metric_change"] == 1
        assert payload["review_required"][0]["item_type"] == "revision_change"
        assert payload["thesis_evaluations"][0]["status"] == "broken"
        assert payload["thesis_evaluations"][0]["details"]["matched_stop"][0]["kind"] == "rule_present"

        counts = {
            table: rconn.execute(f"SELECT COUNT(*) AS cnt FROM {table}").fetchone()["cnt"]
            for table in (
                "company_timeseries",
                "decision_briefs",
                "review_queue_items",
                "peer_sets",
                "sector_rollups",
                "thesis_definitions",
                "thesis_conditions",
                "thesis_evaluations",
            )
        }
        assert counts == {
            "company_timeseries": 3,
            "decision_briefs": 1,
            "review_queue_items": 4,
            "peer_sets": 1,
            "sector_rollups": 1,
            "thesis_definitions": 1,
            "thesis_conditions": 3,
            "thesis_evaluations": 1,
        }
    finally:
        rconn.close()


def test_run_intelligence_is_rerunnable_without_duplicate_rows(tmp_config):
    _seed_phase10_inputs(tmp_config)
    _write_phase12_theses(tmp_config)

    first = run_intelligence(tmp_config, ticker="TAMU")
    second = run_intelligence(tmp_config, ticker="TAMU")

    assert first.deltas == second.deltas == 6
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
                "thesis_evaluations",
            )
        }
        assert counts == {
            "document_deltas": 6,
            "company_snapshots": 1,
            "company_timeseries": 3,
            "decision_briefs": 1,
            "review_queue_items": 4,
            "peer_sets": 1,
            "sector_rollups": 1,
            "thesis_evaluations": 1,
        }
    finally:
        rconn.close()


def test_intelligence_cli_uses_phase10_runner(tmp_config, monkeypatch, capsys):
    _seed_phase10_inputs(tmp_config)
    _write_phase12_theses(tmp_config)
    monkeypatch.setattr("stock_triage.__main__.load_config", lambda: tmp_config)
    monkeypatch.setattr(
        "stock_triage.__main__.run_intelligence",
        lambda cfg, ticker, limit: run_intelligence(tmp_config, ticker=ticker, limit=limit),
    )

    exit_code = cmd_intelligence_run(argparse.Namespace(ticker="TAMU", limit=5))

    captured = capsys.readouterr().out
    assert exit_code == 0
    assert "intelligence-run:" in captured
    assert "derived_version=phase12-v1" in captured
    assert "selected_tickers=1" in captured
    assert "thesis_evaluations=1" in captured


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
                INSERT INTO document_versions (
                    version_group_id, source_name, source_record_id, ticker, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                ("vg-tamu-0", "idx_financial_reports", "fin-2023-tamu", "TAMU", older),
            )
            conn.execute(
                """
                INSERT INTO documents (
                    document_id, source_name, source_record_id, source_file_id, source_modified_at, retrieved_at, original_url,
                    local_path, sha256, mime_type, document_type, language_variant, process_state,
                    parser_version, pipeline_version, ruleset_version, prompt_version, model_name,
                    output_schema_version, ticker, version_group_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "doc-tamu-c",
                    "idx_financial_reports",
                    "fin-2023-tamu",
                    "fin-2023-tamu-c",
                    (now - timedelta(days=730)).isoformat(),
                    (now - timedelta(days=730)).isoformat(),
                    "https://example.test/tamu-2023.pdf",
                    str(tmp_config.paths.data_dir / "tamu-annual-report.pdf"),
                    "sha-c",
                    "application/pdf",
                    "annual_report",
                    "bilingual",
                    "analyzed",
                    "phase4-v1",
                    "phase5-v1",
                    "phase6-v1",
                    "phase7-v2",
                    "gemma2:9b",
                    "appendix-c-v1",
                    "TAMU",
                    "vg-tamu-0",
                    (now - timedelta(days=730)).isoformat(),
                    (now - timedelta(days=730)).isoformat(),
                ),
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
                INSERT INTO metric_items (
                    metric_id, document_id, ticker, metric_name, value_normalized, raw_value_text, currency,
                    unit_scale, fiscal_year, fiscal_period, period_end_date, audit_status, consolidation_scope,
                    language_variant, evidence_span_id, extracted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "metric-tamu-revenue-oldest",
                    "doc-tamu-c",
                    "TAMU",
                    "revenue",
                    3000000.0,
                    "3,000,000",
                    "USD",
                    "units",
                    2023,
                    "Annual",
                    "2023-12-31",
                    "audited",
                    "consolidated",
                    "bilingual",
                    "span-tamu-1",
                    (now - timedelta(days=730)).isoformat(),
                ),
            )
            conn.execute(
                """
                INSERT INTO events (
                    event_id, document_id, ticker, event_type, matched_pattern, severity, rationale, evidence_span_id, detected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "event-tamu-prev",
                    "doc-tamu-b",
                    "TAMU",
                    "capital_raise",
                    "rights issue",
                    "warning",
                    "Prior capital raise was the earlier focus.",
                    "span-tamu-1",
                    older,
                ),
            )
            conn.execute(
                """
                INSERT INTO rule_runs (
                    rule_run_id, document_id, ticker, ruleset_version, rule_name, severity, threshold, input_values, rationale, triggered, evaluated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "rule-tamu-prev",
                    "doc-tamu-b",
                    "TAMU",
                    "phase6-v1",
                    "cash_burn",
                    "warning",
                    "operating_cash_flow < 0",
                    json.dumps({"operating_cash_flow": -1}),
                    "Prior year cash burn required monitoring.",
                    1,
                    older,
                ),
            )
            conn.execute(
                """
                INSERT INTO analysis_runs (
                    analysis_run_id, document_id, model_name, prompt_version, output_schema_version,
                    schema_valid, evidence_link_valid, output_json, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "analysis-tamu-prev",
                    "doc-tamu-b",
                    "gemma2:9b",
                    "phase7-v2",
                    "appendix-c-v1",
                    1,
                    1,
                    json.dumps(
                        {
                            "document_id": "doc-tamu-b",
                            "model_name": "gemma2:9b",
                            "summary": "Capital raise remained the main monitoring point.",
                            "what_to_verify": ["Check post-raise share dilution."],
                            "evidence_links": [],
                        }
                    ),
                    older,
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


def _write_phase12_theses(tmp_config) -> None:
    tmp_config.theses.definition_path.write_text(
        json.dumps(
            {
                "theses": [
                    {
                        "thesis_id": "tamu-balance-sheet",
                        "ticker": "TAMU",
                        "thesis_name": "TAMU balance-sheet repair",
                        "thesis_text": "TAMU should improve revenue while avoiding debt-stress stop rules.",
                        "owner_scope": "watchlist",
                        "status": "active",
                        "review_cadence": "after_each_filing",
                        "conditions": [
                            {
                                "severity": "positive",
                                "kind": "metric_delta",
                                "metric_name": "revenue",
                                "operator": "gte",
                                "value": 20,
                            },
                            {
                                "severity": "warning",
                                "kind": "event_present",
                                "event_type": "debt_refinancing",
                            },
                            {
                                "severity": "stop",
                                "kind": "rule_present",
                                "rule_name": "debt_surge",
                            },
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
