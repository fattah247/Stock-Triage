from __future__ import annotations

import json
from datetime import datetime, timezone

from stock_triage.__main__ import main
from stock_triage.db import WriterQueue, connect, configure_pragmas, run_migrations
from stock_triage.summary import build_summary_context, generate_summary, validate_summary_file


def test_build_summary_context_collects_curated_facts(tmp_config):
    document_id, span_id = _insert_analyzed_document(tmp_config)

    context = build_summary_context(tmp_config, document_id=document_id)

    assert context.document_id == document_id
    assert context.metrics
    assert context.events
    assert context.rule_flags
    assert context.evidence_links[0]["id"] == span_id


def test_validate_summary_file_persists_analysis_run(tmp_config, tmp_path):
    document_id, span_id = _insert_analyzed_document(tmp_config)
    payload = {
        "document_id": document_id,
        "model_name": "local-test-model",
        "summary": "Revenue fell and financing-related risks remain elevated.",
        "key_changes": [{"claim": "Revenue weakened materially.", "severity": "high", "evidence_links": [span_id]}],
        "risks": [{"claim": "Liquidity pressure persists.", "evidence_links": [span_id]}],
        "events": [{"event_type": "debt_refinancing", "evidence_links": [span_id]}],
        "what_to_verify": ["Confirm whether refinancing improved near-term covenants."],
        "evidence_links": [
            {"id": span_id, "document_id": document_id, "chunk_id": "chunk-1", "page_start": 18, "page_end": 18}
        ],
    }
    input_file = tmp_path / "summary.json"
    input_file.write_text(json.dumps(payload), encoding="utf-8")

    result = validate_summary_file(
        tmp_config,
        document_id=document_id,
        input_path=input_file,
        model_name="local-test-model",
    )

    assert result.schema_valid is True
    assert result.evidence_link_valid is True
    assert result.errors == []

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        run = rconn.execute(
            """
            SELECT model_name, prompt_version, output_schema_version, schema_valid, evidence_link_valid
            FROM analysis_runs
            WHERE document_id = ?
            """,
            (document_id,),
        ).fetchone()
        assert run["model_name"] == "local-test-model"
        assert run["prompt_version"] == "phase7-v2"
        assert run["output_schema_version"] == "appendix-c-v1"
        assert run["schema_valid"] == 1
        assert run["evidence_link_valid"] == 1
    finally:
        rconn.close()


def test_validate_summary_file_rejects_unknown_evidence(tmp_config, tmp_path):
    document_id, _span_id = _insert_analyzed_document(tmp_config)
    payload = {
        "document_id": document_id,
        "model_name": "local-test-model",
        "summary": "Bad evidence reference.",
        "key_changes": [{"claim": "Revenue weakened materially.", "severity": "high", "evidence_links": ["span-missing"]}],
        "risks": [],
        "events": [],
        "what_to_verify": [],
        "evidence_links": [
            {"id": "span-missing", "document_id": document_id, "chunk_id": "chunk-1", "page_start": 18, "page_end": 18}
        ],
    }
    input_file = tmp_path / "summary-invalid.json"
    input_file.write_text(json.dumps(payload), encoding="utf-8")

    result = validate_summary_file(
        tmp_config,
        document_id=document_id,
        input_path=input_file,
        model_name="local-test-model",
    )

    assert result.schema_valid is True
    assert result.evidence_link_valid is False
    assert "unknown_evidence_links" in result.errors


def test_summary_context_cli_and_validate_cli(tmp_config, tmp_path, monkeypatch, capsys):
    document_id, span_id = _insert_analyzed_document(tmp_config)
    monkeypatch.setattr("stock_triage.commands.summary.load_config", lambda: tmp_config)

    rc = main(["summary-context", document_id])
    out = capsys.readouterr().out
    assert rc == 0
    assert document_id in out

    payload = {
        "document_id": document_id,
        "model_name": "local-test-model",
        "summary": "Revenue fell.",
        "key_changes": [{"claim": "Revenue weakened materially.", "severity": "high", "evidence_links": [span_id]}],
        "risks": [],
        "events": [],
        "what_to_verify": [],
        "evidence_links": [
            {"id": span_id, "document_id": document_id, "chunk_id": "chunk-1", "page_start": 18, "page_end": 18}
        ],
    }
    input_file = tmp_path / "summary.json"
    input_file.write_text(json.dumps(payload), encoding="utf-8")

    rc = main(["summary-validate", document_id, "--input-file", str(input_file), "--model-name", "local-test-model"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "schema_valid=1" in out
    assert "evidence_link_valid=1" in out


def test_generate_summary_calls_local_model_and_persists_result(tmp_config, monkeypatch):
    document_id, span_id = _insert_analyzed_document(tmp_config)
    tmp_config.llm.model_name = "ollama-test"

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "message": {
                    "content": json.dumps(
                        {
                            "document_id": document_id,
                            "summary": "Revenue fell.",
                            "key_changes": [
                                {"claim": "Revenue weakened materially.", "severity": "high", "evidence_links": ["E1"]}
                            ],
                            "risks": [],
                            "events": [],
                            "what_to_verify": [],
                        }
                    )
                }
            }

    monkeypatch.setattr("stock_triage.summary.pipeline.httpx.post", lambda *args, **kwargs: FakeResponse())

    result = generate_summary(tmp_config, document_id=document_id)

    assert result.schema_valid is True
    assert result.evidence_link_valid is True
    assert result.errors == []

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        run = rconn.execute(
            """
            SELECT output_json
            FROM analysis_runs
            WHERE document_id = ?
            """,
            (document_id,),
        ).fetchone()
        payload = json.loads(run["output_json"])
        assert payload["model_name"] == "ollama-test"
        assert payload["key_changes"][0]["evidence_links"] == [span_id]
        assert payload["evidence_links"][0]["id"] == span_id
    finally:
        rconn.close()


def test_summary_generate_cli_uses_generate_summary(tmp_config, monkeypatch, capsys):
    monkeypatch.setattr("stock_triage.commands.summary.load_config", lambda: tmp_config)
    monkeypatch.setattr(
        "stock_triage.commands.summary.generate_summary",
        lambda cfg, document_id, prompt_version="phase7-v2": type(
            "R",
            (),
            {
                "analysis_run_id": "run-1",
                "schema_valid": True,
                "evidence_link_valid": True,
                "errors": [],
            },
        )(),
    )

    rc = main(["summary-generate", "doc-1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "analysis_run_id=run-1" in out
    assert "schema_valid=1" in out


def _insert_analyzed_document(tmp_config) -> tuple[str, str]:
    now = datetime.now(timezone.utc).isoformat()
    document_id = "doc-summary-1"
    span_id = "span-summary-1"

    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)

        def _insert(conn):
            conn.execute(
                """
                INSERT INTO documents (
                    document_id, source_name, source_record_id, source_file_id,
                    retrieved_at, original_url, local_path, sha256, mime_type,
                    document_type, language_variant, process_state, parser_version,
                    pipeline_version, ruleset_version, ticker, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    "idx_financial_reports",
                    "fin-summary-1",
                    "fin-summary-1",
                    now,
                    "https://example.test/file.pdf",
                    "/tmp/example.pdf",
                    "sha-summary-1",
                    "application/pdf",
                    "annual_report",
                    "bilingual",
                    "analyzed",
                    "phase4-v1",
                    "phase5-v2",
                    "phase6-v1",
                    "TAMU",
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
                ("chunk-1", document_id, "Corporate Action", 18, 18, 0, 80, "Debt restructuring improved liquidity.", 0),
            )
            conn.execute(
                """
                INSERT INTO evidence_spans (
                    span_id, document_id, chunk_id, page_start, page_end, char_start, char_end, excerpt_text, display_anchor
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (span_id, document_id, "chunk-1", 18, 18, 0, 35, "Debt restructuring improved liquidity.", "p18"),
            )
            conn.execute(
                """
                INSERT INTO metric_items (
                    metric_id, document_id, ticker, metric_name, value_normalized, raw_value_text,
                    currency, unit_scale, fiscal_year, fiscal_period, period_end_date,
                    audit_status, consolidation_scope, language_variant, evidence_span_id, extracted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "metric-summary-1",
                    document_id,
                    "TAMU",
                    "net_profit",
                    -7.0,
                    "(7.0)",
                    "USD",
                    "million",
                    2024,
                    "Annual",
                    "2024-12-31",
                    "audited",
                    "consolidated",
                    "bilingual",
                    span_id,
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
                    "event-summary-1",
                    document_id,
                    "TAMU",
                    "debt_refinancing",
                    "restrukturisasi kredit",
                    "warning",
                    "Debt restructuring detected.",
                    span_id,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO rule_runs (
                    rule_run_id, document_id, ticker, ruleset_version, rule_name,
                    severity, threshold, input_values, rationale, triggered, evaluated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "rule-summary-1",
                    document_id,
                    "TAMU",
                    "phase6-v1",
                    "profit_drop",
                    "critical",
                    "net_profit < 0",
                    json.dumps({"value_normalized": -7.0}),
                    "Net profit is negative.",
                    1,
                    now,
                ),
            )

        wq.submit(_insert)
    return document_id, span_id
