from __future__ import annotations

import json
from datetime import datetime, timezone

from stock_triage.__main__ import main
from stock_triage.alerts import run_alerts
from stock_triage.canary import load_canary_status, run_canary
from stock_triage.dashboard import export_dashboard
from stock_triage.db import WriterQueue, connect, configure_pragmas, run_migrations
from stock_triage.intelligence import run_intelligence


def test_export_dashboard_writes_html_and_csv(tmp_config):
    _seed_phase8_data(tmp_config)

    result = export_dashboard(tmp_config)

    assert result.html_path.exists()
    assert result.metrics_csv_path.exists()
    assert result.findings_csv_path.exists()
    assert (result.html_path.parent / "static" / "app.css").exists()
    assert (result.html_path.parent / "static" / "app.js").exists()
    html = result.html_path.read_text(encoding="utf-8")
    assert "Stock Triage Dashboard" in html
    assert "TAMU" in html
    assert "Debt refinancing remains a monitoring point." in html
    assert "What changed recently" in html


def test_run_alerts_persists_and_dedupes_watchlist_signals(tmp_config):
    _seed_phase8_data(tmp_config)
    tmp_config.alerts.watchlist = ["TAMU"]
    tmp_config.alerts.jsonl_path = tmp_config.paths.data_dir / "alerts" / "alerts.jsonl"

    first = run_alerts(tmp_config)
    second = run_alerts(tmp_config)

    assert first.selected == 2
    assert first.emitted == 2
    assert second.emitted == 0
    assert second.skipped_existing == 2

    lines = tmp_config.alerts.jsonl_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        row = rconn.execute("SELECT COUNT(*) AS cnt FROM alert_events").fetchone()
        assert row["cnt"] == 2
    finally:
        rconn.close()


def test_run_alerts_includes_broken_thesis_signals(tmp_config):
    _seed_phase8_data(tmp_config)
    tmp_config.alerts.watchlist = ["TAMU"]
    tmp_config.alerts.jsonl_path = tmp_config.paths.data_dir / "alerts" / "alerts.jsonl"
    tmp_config.theses.definition_path.write_text(
        json.dumps(
            {
                "theses": [
                    {
                        "thesis_id": "tamu-alert-thesis",
                        "ticker": "TAMU",
                        "thesis_name": "TAMU debt cleanup",
                        "thesis_text": "No debt stress should remain active.",
                        "conditions": [
                            {
                                "severity": "stop",
                                "kind": "rule_present",
                                "rule_name": "debt_surge",
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)

        def _bump_latest(conn):
            later = datetime.now(timezone.utc).isoformat()
            earlier = "2025-01-01T00:00:00+00:00"
            conn.execute(
                "UPDATE documents SET source_modified_at = ?, updated_at = ?, created_at = ? WHERE document_id = 'doc-tamu-a'",
                (later, later, later),
            )
            conn.execute(
                "UPDATE documents SET source_modified_at = ?, updated_at = ?, created_at = ? WHERE document_id = 'doc-tamu-b'",
                (earlier, earlier, earlier),
            )

        wq.submit(_bump_latest)

    run_intelligence(tmp_config, ticker="TAMU")
    result = run_alerts(tmp_config)

    assert result.selected == 3
    assert result.emitted == 3

    lines = [json.loads(line) for line in tmp_config.alerts.jsonl_path.read_text(encoding="utf-8").strip().splitlines()]
    thesis_payload = next(line for line in lines if line["signal_kind"] == "thesis")
    assert thesis_payload["signal_name"] == "TAMU debt cleanup (broken)"
    assert thesis_payload["severity"] == "critical"
    assert thesis_payload["suggested_next_action"] == "review now"
    assert thesis_payload["details"]["matched_stop"][0]["kind"] == "rule_present"


def test_run_canary_persists_and_detects_drift(tmp_config, monkeypatch):
    class FakeAdapter:
        def __init__(self, name: str, response: dict[str, object]):
            self.name = name
            self._response = response

        def healthcheck(self) -> dict[str, object]:
            return dict(self._response)

    monkeypatch.setattr(
        "stock_triage.canary.pipeline._adapters",
        lambda cfg, source_name=None: [FakeAdapter("idx_disclosures", {"accessible": True, "count": 10, "sample_ids": ["a"]})],
    )

    first = run_canary(tmp_config)
    assert first.rows[0].status == "ok"
    assert first.rows[0].drifted is False

    monkeypatch.setattr(
        "stock_triage.canary.pipeline._adapters",
        lambda cfg, source_name=None: [FakeAdapter("idx_disclosures", {"accessible": True, "count": 2, "sample_ids": ["b"]})],
    )
    second = run_canary(tmp_config)
    assert second.rows[0].status == "warning"
    assert second.rows[0].drifted is True

    report = load_canary_status(tmp_config)
    assert report.rows[0].source_name == "idx_disclosures"
    assert report.rows[0].drifted is True


def test_run_canary_persists_failures_instead_of_raising(tmp_config, monkeypatch):
    class FailingAdapter:
        name = "idx_financial_reports"

        def healthcheck(self) -> dict[str, object]:
            raise RuntimeError("blocked")

    monkeypatch.setattr(
        "stock_triage.canary.pipeline._adapters",
        lambda cfg, source_name=None: [FailingAdapter()],
    )

    result = run_canary(tmp_config)

    assert result.rows[0].source_name == "idx_financial_reports"
    assert result.rows[0].status == "failed"
    assert result.rows[0].accessible is False


def test_dashboard_and_canary_cli_use_phase8_commands(tmp_config, monkeypatch, capsys):
    monkeypatch.setattr("stock_triage.commands.dashboard.load_config", lambda: tmp_config)
    monkeypatch.setattr("stock_triage.commands.alerts_canary.load_config", lambda: tmp_config)
    monkeypatch.setattr(
        "stock_triage.commands.dashboard.export_dashboard",
        lambda cfg, output_dir=None: type(
            "R",
            (),
            {
                "html_path": tmp_config.dashboard.output_dir / "index.html",
                "metrics_csv_path": tmp_config.dashboard.output_dir / "metrics.csv",
                "findings_csv_path": tmp_config.dashboard.output_dir / "findings.csv",
                "issuer_count": 1,
                "document_count": 1,
                "finding_count": 1,
            },
        )(),
    )
    monkeypatch.setattr(
        "stock_triage.commands.alerts_canary.load_canary_status",
        lambda cfg: type(
            "R",
            (),
            {
                "rows": [
                    type(
                        "Row",
                        (),
                        {
                            "source_name": "idx_disclosures",
                            "status": "ok",
                            "accessible": True,
                            "drifted": False,
                            "finished_at": "2026-04-09T00:00:00+00:00",
                        },
                    )()
                ]
            },
        )(),
    )

    rc = main(["dashboard-export"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "dashboard-export:" in out

    rc = main(["canary-status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "idx_disclosures: status=ok" in out


def _seed_phase8_data(tmp_config) -> None:
    now = datetime.now(timezone.utc).isoformat()
    local_path = tmp_config.paths.files_dir / "tamu-annual-report.pdf"
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(b"%PDF-1.4\n%phase8\n")

    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)

        def _insert(conn):
            conn.execute(
                """
                INSERT INTO companies (
                    ticker, issuer_name, listing_date, active_status, sector, subsector, industry,
                    listing_board, source_urls, raw_metadata, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "TAMU",
                    "PT Pelayaran Tamarin Samudra Tbk",
                    "2017-05-10",
                    "listed",
                    "Transportation",
                    "Marine Logistics",
                    "Shipping",
                    "main",
                    json.dumps(["https://www.idx.co.id"]),
                    json.dumps({"country": "ID"}),
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO document_versions (
                    version_group_id, source_name, source_record_id, ticker, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                ("vg-tamu-1", "idx_financial_reports", "fin-2024-tamu", "TAMU", now),
            )
            for suffix in ("a", "b"):
                conn.execute(
                    """
                    INSERT INTO documents (
                        document_id, source_name, source_record_id, source_file_id, retrieved_at, original_url,
                        local_path, sha256, mime_type, document_type, language_variant, process_state,
                        parser_version, pipeline_version, ruleset_version, prompt_version, model_name,
                        output_schema_version, ticker, version_group_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"doc-tamu-{suffix}",
                        "idx_financial_reports",
                        "fin-2024-tamu",
                        f"fin-2024-tamu-{suffix}",
                        now,
                        "https://example.test/tamu.pdf",
                        str(local_path),
                        f"sha-{suffix}",
                        "application/pdf",
                        "annual_report",
                        "bilingual",
                        "analyzed",
                        "phase4-v1",
                        "phase5-v2",
                        "phase6-v1",
                        "phase7-v2",
                        "gemma2:9b",
                        "appendix-c-v1",
                        "TAMU",
                        "vg-tamu-1",
                        now,
                        now,
                    ),
                )
            conn.execute(
                """
                INSERT INTO document_chunks (
                    chunk_id, document_id, section_label, page_start, page_end, char_start, char_end, text, ord
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "chunk-1",
                    "doc-tamu-a",
                    "Corporate Action",
                    18,
                    18,
                    0,
                    60,
                    "Debt restructuring improved liquidity.",
                    0,
                ),
            )
            conn.execute(
                """
                INSERT INTO evidence_spans (
                    span_id, document_id, chunk_id, page_start, page_end, char_start, char_end, excerpt_text, display_anchor
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("span-tamu-1", "doc-tamu-a", "chunk-1", 18, 18, 0, 40, "Debt restructuring improved liquidity.", "p18"),
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
                    "metric-tamu-revenue",
                    "doc-tamu-a",
                    "TAMU",
                    "revenue",
                    5082099.0,
                    "5,082,099",
                    "USD",
                    "units",
                    2024,
                    "Annual",
                    "2024-12-31",
                    "audited",
                    "consolidated",
                    "bilingual",
                    "span-tamu-1",
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO events (
                    event_id, document_id, ticker, event_type, matched_pattern, severity, rationale, evidence_span_id, detected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "event-tamu-1",
                    "doc-tamu-a",
                    "TAMU",
                    "debt_refinancing",
                    "restructuring",
                    "warning",
                    "Debt refinancing remains a monitoring point.",
                    "span-tamu-1",
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO rule_runs (
                    rule_run_id, document_id, ticker, ruleset_version, rule_name, severity, threshold, input_values, rationale, triggered, evaluated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "rule-tamu-1",
                    "doc-tamu-a",
                    "TAMU",
                    "phase6-v1",
                    "debt_surge",
                    "warning",
                    "total_liabilities / equity > 0.75",
                    json.dumps({"ratio": 0.86}),
                    "Leverage remains elevated.",
                    1,
                    now,
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
                    "analysis-tamu-1",
                    "doc-tamu-a",
                    "gemma2:9b",
                    "phase7-v2",
                    "appendix-c-v1",
                    1,
                    1,
                    json.dumps(
                        {
                            "document_id": "doc-tamu-a",
                            "model_name": "gemma2:9b",
                            "summary": "Debt refinancing remains a monitoring point.",
                            "key_changes": [
                                {
                                    "claim": "Debt refinancing remains a monitoring point.",
                                    "severity": "warning",
                                    "evidence_links": ["span-tamu-1"],
                                }
                            ],
                            "risks": [],
                            "events": [
                                {
                                    "event_type": "debt_refinancing",
                                    "evidence_links": ["span-tamu-1"],
                                }
                            ],
                            "what_to_verify": ["Review covenant headroom after restructuring."],
                            "evidence_links": [
                                {
                                    "id": "span-tamu-1",
                                    "document_id": "doc-tamu-a",
                                    "chunk_id": "chunk-1",
                                    "page_start": 18,
                                    "page_end": 18,
                                    "excerpt_text": "Debt restructuring improved liquidity.",
                                }
                            ],
                        }
                    ),
                    now,
                    now,
                ),
            )

        wq.submit(_insert)
