from __future__ import annotations

from datetime import datetime, timezone

from stock_triage.__main__ import main
from stock_triage.db import WriterQueue, connect, configure_pragmas, run_migrations
from stock_triage.rules import load_rule_status, run_rules


def test_run_rules_persists_financial_flags(tmp_config):
    document_id = _insert_extracted_document(
        tmp_config,
        source_name="idx_financial_reports",
        source_record_id="fin-1",
        ticker="TAMU",
        metrics=[
            ("net_profit", -7_384_305.0, 2024, "2024-12-31"),
            ("operating_cash_flow", -1_120_000.0, 2024, "2024-12-31"),
            ("total_liabilities", 16_011_455.0, 2024, "2024-12-31"),
            ("equity", 18_553_984.0, 2024, "2024-12-31"),
        ],
        events=["debt_refinancing", "material_transaction", "suspension_related_notice"],
    )

    result = run_rules(tmp_config, source_name="idx_financial_reports", limit=10)

    assert result.selected == 1
    assert result.analyzed == 1
    assert result.triggered == 6

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        doc = rconn.execute(
            "SELECT process_state, ruleset_version FROM documents WHERE document_id = ?",
            (document_id,),
        ).fetchone()
        assert doc["process_state"] == "analyzed"
        assert doc["ruleset_version"] == "phase6-v1"

        rules = rconn.execute(
            """
            SELECT rule_name, triggered, severity
            FROM rule_runs
            WHERE document_id = ?
            ORDER BY rule_name
            """,
            (document_id,),
        ).fetchall()
        triggered = {row["rule_name"] for row in rules if row["triggered"] == 1}
        assert triggered == {
            "cash_flow_deterioration",
            "debt_surge",
            "funding_event",
            "material_transaction",
            "profit_drop",
            "suspension_notice",
        }
    finally:
        rconn.close()


def test_run_rules_persists_event_driven_flags(tmp_config):
    document_id = _insert_extracted_document(
        tmp_config,
        source_name="idx_disclosures",
        source_record_id="disc-1",
        ticker="CNAF",
        metrics=[],
        events=["auditor_change", "bond_issuance", "debt_facility", "governance_appointment"],
    )

    result = run_rules(tmp_config, source_name="idx_disclosures", limit=10)

    assert result.selected == 1
    assert result.analyzed == 1
    assert result.triggered == 3

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        rules = rconn.execute(
            """
            SELECT rule_name, triggered
            FROM rule_runs
            WHERE document_id = ?
            ORDER BY rule_name
            """,
            (document_id,),
        ).fetchall()
        triggered = {row["rule_name"] for row in rules if row["triggered"] == 1}
        assert triggered == {"auditor_change", "funding_event", "management_change"}
    finally:
        rconn.close()


def test_load_rule_status_reports_recent_triggers(tmp_config):
    _insert_extracted_document(
        tmp_config,
        source_name="idx_financial_reports",
        source_record_id="fin-1",
        ticker="TAMU",
        metrics=[
            ("net_profit", -10.0, 2024, "2024-12-31"),
            ("operating_cash_flow", -5.0, 2024, "2024-12-31"),
            ("total_liabilities", 100.0, 2024, "2024-12-31"),
            ("equity", 50.0, 2024, "2024-12-31"),
        ],
        events=[],
    )
    run_rules(tmp_config, source_name="idx_financial_reports", limit=10)

    report = load_rule_status(tmp_config, source_name="idx_financial_reports", trigger_limit=5)

    assert len(report.rows) == 1
    assert report.rows[0].pending == 0
    assert report.rows[0].analyzed == 1
    assert report.rows[0].triggered == 3
    assert report.recent_triggers


def test_rules_cli_uses_run_rules(tmp_config, monkeypatch, capsys):
    monkeypatch.setattr("stock_triage.commands.pipeline.load_config", lambda: tmp_config)
    monkeypatch.setattr(
        "stock_triage.commands.pipeline.run_rules",
        lambda cfg, source_name=None, document_id=None, limit=25, include_analyzed=False: type(
            "R",
            (),
            {"selected": 2, "analyzed": 2, "triggered": 4},
        )(),
    )

    rc = main(["rules", "--source", "idx_financial_reports", "--limit", "2"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "selected=2" in out
    assert "triggered=4" in out


def test_run_rules_can_target_single_document(tmp_config):
    target_id = _insert_extracted_document(
        tmp_config,
        source_name="idx_financial_reports",
        source_record_id="rules-target-1",
        ticker="TAMU",
        metrics=[("net_profit", -10.0, 2024, "2024-12-31")],
        events=[],
    )
    _insert_extracted_document(
        tmp_config,
        source_name="idx_financial_reports",
        source_record_id="rules-target-2",
        ticker="TAMU",
        metrics=[("net_profit", -10.0, 2024, "2024-12-31")],
        events=[],
    )

    result = run_rules(tmp_config, document_id=target_id)

    assert result.selected == 1
    assert result.analyzed == 1

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        target = rconn.execute("SELECT process_state FROM documents WHERE document_id = ?", (target_id,)).fetchone()
        untouched = rconn.execute(
            "SELECT process_state FROM documents WHERE source_record_id = ?",
            ("rules-target-2",),
        ).fetchone()
        assert target["process_state"] == "analyzed"
        assert untouched["process_state"] == "extracted"
    finally:
        rconn.close()


def test_rules_status_cli_uses_load_rule_status(tmp_config, monkeypatch, capsys):
    monkeypatch.setattr("stock_triage.commands.pipeline.load_config", lambda: tmp_config)
    monkeypatch.setattr(
        "stock_triage.commands.pipeline.load_rule_status",
        lambda cfg, source_name=None, trigger_limit=10: type(
            "R",
            (),
            {
                "rows": [
                    type(
                        "Row",
                        (),
                        {"source_name": "idx_financial_reports", "total": 2, "pending": 0, "analyzed": 2, "triggered": 4},
                    )()
                ],
                "recent_triggers": [
                    type(
                        "Trigger",
                        (),
                        {
                            "source_name": "idx_financial_reports",
                            "document_id": "doc-1",
                            "source_record_id": "fin-1",
                            "rule_name": "profit_drop",
                            "severity": "critical",
                            "evaluated_at": "2026-04-09T00:00:00+00:00",
                        },
                    )()
                ],
            },
        )(),
    )

    rc = main(["rules-status", "--source", "idx_financial_reports", "--trigger-limit", "3"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "analyzed=2" in out
    assert "recent_triggers:" in out
    assert "profit_drop" in out


def _insert_extracted_document(
    tmp_config,
    *,
    source_name: str,
    source_record_id: str,
    ticker: str,
    metrics: list[tuple[str, float, int, str]],
    events: list[str],
) -> str:
    now = datetime.now(timezone.utc).isoformat()
    document_id = f"doc-{source_record_id}"

    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)

        def _insert(conn):
            conn.execute(
                """
                INSERT INTO documents (
                    document_id, source_name, source_record_id, source_file_id,
                    retrieved_at, original_url, local_path, sha256, mime_type,
                    document_type, language_variant, process_state, parser_version,
                    pipeline_version, ticker, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    source_name,
                    source_record_id,
                    source_record_id,
                    now,
                    "https://example.test/file.pdf",
                    "/tmp/example.pdf",
                    f"sha-{source_record_id}",
                    "application/pdf",
                    "annual_report" if source_name == "idx_financial_reports" else "disclosure",
                    "bilingual",
                    "extracted",
                    "phase4-v1",
                    "phase5-v1",
                    ticker,
                    now,
                    now,
                ),
            )
            for idx, (metric_name, value, fiscal_year, period_end_date) in enumerate(metrics):
                conn.execute(
                    """
                    INSERT INTO metric_items (
                        metric_id, document_id, ticker, metric_name, value_normalized, raw_value_text,
                        currency, unit_scale, fiscal_year, fiscal_period, period_end_date,
                        audit_status, consolidation_scope, language_variant, evidence_span_id, extracted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"metric-{source_record_id}-{idx}",
                        document_id,
                        ticker,
                        metric_name,
                        value,
                        str(value),
                        "USD",
                        "units",
                        fiscal_year,
                        "Annual",
                        period_end_date,
                        "audited",
                        "consolidated",
                        "bilingual",
                        None,
                        now,
                    ),
                )
            for idx, event_type in enumerate(events):
                conn.execute(
                    """
                    INSERT INTO events (
                        event_id, document_id, ticker, event_type, matched_pattern,
                        severity, rationale, evidence_span_id, detected_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"event-{source_record_id}-{idx}",
                        document_id,
                        ticker,
                        event_type,
                        event_type,
                        "info",
                        event_type,
                        None,
                        now,
                    ),
                )

        wq.submit(_insert)
    return document_id
