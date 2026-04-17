from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

from stock_triage.commands.intelligence import cmd_intelligence_run
from stock_triage.db import WriterQueue, connect, configure_pragmas, run_migrations
from stock_triage.fiscal_year import save_fiscal_year_override
from stock_triage.intelligence import DERIVED_VERSION, rederive_intelligence, run_intelligence
from stock_triage.intelligence.claims import ClaimCredibilitySummary, ClaimRecord
from stock_triage.intelligence.pipeline import (
    AnalysisSummary,
    BaselineChoice,
    CompanyDocument,
    EvidenceContext,
    EventSignal,
    FilingExpectationRecord,
    MetricPoint,
    NextReadRecord,
    RECOMMENDATION_BUCKET_AVOID,
    RECOMMENDATION_BUCKET_STOCK_PICK,
    _build_narrative_signals,
    _build_next_read_guidance,
    _build_uncertainty_rows,
    _rank_next_read_rows,
    _build_recommendation_summary,
    _build_segment_summary,
    _derive_stance,
    _next_expected_filing_window,
)
from stock_triage.intelligence.review import load_intelligence_observability, run_golden_set

from .test_phase8_ops import _seed_phase8_data


def test_run_intelligence_persists_phase10_foundation_rows(tmp_config):
    _seed_phase10_inputs(tmp_config)
    _write_phase12_theses(tmp_config)

    result = run_intelligence(tmp_config, ticker="TAMU")

    assert result.selected_tickers == 1
    assert result.deltas == 8
    assert result.snapshots == 1
    assert result.timeseries == 7
    assert result.briefs == 1
    assert result.review_items == 5
    assert result.peer_sets == 1
    assert result.sector_rollups == 1
    assert result.thesis_evaluations == 1
    assert result.claims == 4
    assert result.claim_outcomes == 4

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
        assert len(deltas) == 8
        metric_delta = next(
            row
            for row in deltas
            if row["delta_kind"] == "metric_change" and row["metric_name"] == "revenue"
        )
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
        assert payload["top_card"]["management_credibility"] == "deteriorating"
        assert payload["top_card"]["credibility_band"] == "poor"
        assert payload["top_card"]["thesis_state"] == "broken"
        assert payload["top_card"]["decision_state"] == "Avoid"
        assert payload["top_card"]["watchlist_priority"] == "high"
        assert payload["decision_panel"]["stance"] == "Avoid"
        assert payload["decision_panel"]["confidence"] == "high"
        assert payload["decision_panel"]["what_changed"]
        assert payload["decision_panel"]["what_to_watch"]
        assert payload["decision_panel"]["must_improve"]
        assert payload["decision_panel"]["thesis_breakers"]
        assert payload["decision_panel"]["decision_constraints"]
        assert payload["decision_panel"]["next_catalyst"]["label"]
        assert payload["decision_panel"]["next_catalyst"]["report_type"] in {"quarterly_fs", "interim_fs", "annual_fs", "annual_report"}
        assert payload["decision_panel"]["recommendation"]["bucket"] == RECOMMENDATION_BUCKET_AVOID
        assert payload["decision_panel"]["recommendation"]["reason_1"]
        assert payload["decision_panel"]["recommendation"]["reason_2"]
        assert payload["decision_panel"]["recommendation"]["reason_3"]
        assert payload["filing_expectations"]
        assert payload["nuance_findings"]
        assert payload["uncertainty_rows"]
        assert payload["next_read_guidance"]
        assert payload["contradiction_rows"]
        assert any(item["nuance_class"] == "claim_contradiction" for item in payload["nuance_findings"])
        assert any(item["uncertainty_class"] == "event_confidence_missing" for item in payload["uncertainty_rows"])
        assert payload["next_read_guidance"][0]["reason_class"] in {"missing_filing", "nuance_review", "uncertainty_followup"}
        assert payload["contradiction_rows"][0]["contradiction_class"] == "claim_vs_outcome"
        assert any(item["obligation_type"] == "annual_report" and item["state"] == "satisfied" for item in payload["filing_expectations"])
        assert payload["segment_summary"] == []
        assert "revenue increased" in payload["top_card"]["positive_factors"]
        assert "debt_surge appeared" in payload["top_card"]["negative_factors"]
        assert payload["delta_count_by_kind"]["metric_change"] == 3
        assert payload["review_required"][0]["item_type"] == "revision_change"
        assert payload["management_credibility"]["achieved"] == 2
        assert payload["management_credibility"]["contradicted"] == 1
        assert payload["management_credibility"]["credibility_state"] == "deteriorating"
        assert {claim["claim_type"] for claim in payload["claims"]} == {
            "debt_reduction",
            "margin_improvement",
            "outlook_guidance",
            "revenue_growth",
        }
        assert {outcome["outcome_status"] for outcome in payload["claim_outcomes"]} == {
            "Achieved",
            "Contradicted",
            "Qualitative",
        }
        assert payload["thesis_evaluations"][0]["status"] == "broken"
        assert payload["thesis_evaluations"][0]["details"]["matched_stop"][0]["kind"] == "rule_present"
        assert payload["peer_comparison"]["peer_key"] == "subsector:Marine Logistics"
        assert payload["peer_comparison"]["comparable_peer_count"] == 3
        assert any("SAFE: currency mismatch" in warning for warning in payload["peer_comparison"]["comparability_warnings"])
        assert payload["peer_comparison"]["metric_ranks"]["revenue_growth_pct"]["rank"] == 1
        assert payload["peer_comparison"]["metric_ranks"]["event_density"]["anomaly"] == "negative_outlier"
        assert payload["sector_context"]["heatmap"]["red_flag_density"] == 0.6
        assert any(anomaly["ticker"] == "TAMU" for anomaly in payload["sector_context"]["top_anomalies"])

        counts = {
            table: rconn.execute(f"SELECT COUNT(*) AS cnt FROM {table}").fetchone()["cnt"]
            for table in (
                "filing_expectations",
                "nuance_findings",
                "uncertainty_rows",
                "next_read_guidance",
                "contradiction_rows",
                "company_timeseries",
                "decision_briefs",
                "review_queue_items",
                "peer_sets",
                "sector_rollups",
                "management_claims",
                "claim_outcomes",
                "thesis_definitions",
                "thesis_conditions",
                "thesis_evaluations",
            )
        }
        assert counts == {
            "filing_expectations": 20,
            "nuance_findings": len(payload["nuance_findings"]),
            "uncertainty_rows": len(payload["uncertainty_rows"]),
            "next_read_guidance": len(payload["next_read_guidance"]),
            "contradiction_rows": len(payload["contradiction_rows"]),
            "company_timeseries": 7,
            "decision_briefs": 1,
            "review_queue_items": 5,
            "peer_sets": 1,
            "sector_rollups": 1,
            "management_claims": 4,
            "claim_outcomes": 4,
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

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        first_counts = {
            table: rconn.execute(f"SELECT COUNT(*) AS cnt FROM {table}").fetchone()["cnt"]
            for table in (
                "document_deltas",
                "company_snapshots",
                "filing_expectations",
                "nuance_findings",
                "uncertainty_rows",
                "next_read_guidance",
                "contradiction_rows",
                "company_timeseries",
                "decision_briefs",
                "review_queue_items",
                "peer_sets",
                "sector_rollups",
                "management_claims",
                "claim_outcomes",
                "thesis_evaluations",
            )
        }
    finally:
        rconn.close()

    second = run_intelligence(tmp_config, ticker="TAMU")

    assert first.deltas == second.deltas == 8
    assert first.snapshots == second.snapshots == 1

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        counts = {
            table: rconn.execute(f"SELECT COUNT(*) AS cnt FROM {table}").fetchone()["cnt"]
            for table in (
                "document_deltas",
                "company_snapshots",
                "filing_expectations",
                "nuance_findings",
                "uncertainty_rows",
                "next_read_guidance",
                "contradiction_rows",
                "company_timeseries",
                "decision_briefs",
                "review_queue_items",
                "peer_sets",
                "sector_rollups",
                "management_claims",
                "claim_outcomes",
                "thesis_evaluations",
            )
        }
        assert counts == first_counts
        assert counts["nuance_findings"] > 0
        assert counts["uncertainty_rows"] > 0
        assert counts["next_read_guidance"] > 0
        assert counts["contradiction_rows"] > 0
    finally:
        rconn.close()


def test_intelligence_cli_uses_phase10_runner(tmp_config, monkeypatch, capsys):
    _seed_phase10_inputs(tmp_config)
    _write_phase12_theses(tmp_config)
    monkeypatch.setattr("stock_triage.commands.intelligence.load_config", lambda: tmp_config)
    monkeypatch.setattr(
        "stock_triage.commands.intelligence.run_intelligence",
        lambda cfg, ticker, limit: run_intelligence(tmp_config, ticker=ticker, limit=limit),
    )

    exit_code = cmd_intelligence_run(argparse.Namespace(ticker="TAMU", limit=5))

    captured = capsys.readouterr().out
    assert exit_code == 0
    assert "intelligence-run:" in captured
    assert f"derived_version={DERIVED_VERSION}" in captured
    assert "selected_tickers=1" in captured
    assert "thesis_evaluations=1" in captured
    assert "claims=4" in captured
    assert "claim_outcomes=4" in captured


def test_rederive_intelligence_rebuilds_existing_snapshot_tickers(tmp_config):
    _seed_phase10_inputs(tmp_config)
    _write_phase12_theses(tmp_config)

    run_intelligence(tmp_config, ticker="TAMU")
    result = rederive_intelligence(tmp_config, limit=10)

    assert result.selected_tickers == 1
    assert result.snapshots == 1
    assert result.briefs == 1


def test_run_golden_set_persists_audit_run_and_updates_observability(tmp_config):
    _seed_phase10_inputs(tmp_config)
    _write_phase12_theses(tmp_config)
    run_intelligence(tmp_config, ticker="TAMU")

    result = run_golden_set(tmp_config, ticker="TAMU", limit=5)

    assert result.scope == "ticker"
    assert result.target_ticker == "TAMU"
    assert result.case_count >= 5
    assert result.passed_count >= 1

    observability = load_intelligence_observability(tmp_config)
    latest = observability["latest_golden_run"]
    assert latest is not None
    assert latest["golden_run_id"] == result.golden_run_id
    assert latest["target_ticker"] == "TAMU"


def test_run_intelligence_prefers_local_fiscal_year_override(tmp_config):
    _seed_phase10_inputs(tmp_config)
    _write_phase12_theses(tmp_config)
    save_fiscal_year_override(tmp_config.company_overrides.definition_path, ticker="TAMU", month=6)

    run_intelligence(tmp_config, ticker="TAMU")

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        snapshot = rconn.execute(
            """
            SELECT payload_json
            FROM company_snapshots
            WHERE ticker = ? AND snapshot_kind = 'current' AND derived_version = ?
            """,
            ("TAMU", DERIVED_VERSION),
        ).fetchone()
        payload = json.loads(snapshot["payload_json"])
        assert payload["fiscal_year_end_month"] == 6
        assert payload["fiscal_year_end_source"] == "override"
    finally:
        rconn.close()


def test_run_intelligence_infers_non_december_fiscal_year_end(tmp_config):
    _seed_phase10_inputs(tmp_config)
    _write_phase12_theses(tmp_config)

    with WriterQueue(tmp_config) as wq:
        def _update(conn):
            conn.execute(
                """
                UPDATE companies
                SET fiscal_year_end_month = 12
                WHERE ticker = 'TAMU'
                """
            )
            conn.execute(
                """
                UPDATE metric_items
                SET period_end_date = '2024-06-30'
                WHERE ticker = 'TAMU' AND fiscal_period = 'Annual'
                """
            )

        wq.submit(_update)

    run_intelligence(tmp_config, ticker="TAMU")

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        snapshot = rconn.execute(
            """
            SELECT payload_json
            FROM company_snapshots
            WHERE ticker = ? AND snapshot_kind = 'current' AND derived_version = ?
            """,
            ("TAMU", DERIVED_VERSION),
        ).fetchone()
        payload = json.loads(snapshot["payload_json"])
        assert payload["fiscal_year_end_month"] == 6
        assert payload["fiscal_year_end_source"] == "inferred"
    finally:
        rconn.close()


def test_stance_mapping_table_rows():
    cases = [
        (dict(overall_state="stable", thesis_state="broken", credibility_state="high", has_critical_rules=False, has_open_reviews=False, data_completeness=True), ("Avoid", "high")),
        (dict(overall_state="critical", thesis_state="holding", credibility_state="acceptable", has_critical_rules=True, has_open_reviews=False, data_completeness=True), ("Review manually", "high")),
        (dict(overall_state="critical", thesis_state="holding", credibility_state="acceptable", has_critical_rules=False, has_open_reviews=False, data_completeness=True), ("Review manually", "medium")),
        (dict(overall_state="watch", thesis_state="holding", credibility_state="poor", has_critical_rules=False, has_open_reviews=True, data_completeness=True), ("Avoid", "medium")),
        (dict(overall_state="watch", thesis_state="weakening", credibility_state="acceptable", has_critical_rules=True, has_open_reviews=False, data_completeness=True), ("Review manually", "medium")),
        (dict(overall_state="watch", thesis_state="weakening", credibility_state="acceptable", has_critical_rules=False, has_open_reviews=False, data_completeness=True), ("Watch", "medium")),
        (dict(overall_state="stable", thesis_state="improving", credibility_state="high", has_critical_rules=False, has_open_reviews=False, data_completeness=True), ("Buy", "high")),
        (dict(overall_state="stable", thesis_state="holding", credibility_state="acceptable", has_critical_rules=False, has_open_reviews=False, data_completeness=True), ("Watch", "high")),
        (dict(overall_state="watch", thesis_state="holding", credibility_state="acceptable", has_critical_rules=False, has_open_reviews=False, data_completeness=True), ("Watch", "medium")),
        (dict(overall_state="stable", thesis_state="none", credibility_state="unknown", has_critical_rules=False, has_open_reviews=False, data_completeness=True), ("Watch", "low")),
        (dict(overall_state="stable", thesis_state="unsupported", credibility_state="unknown", has_critical_rules=False, has_open_reviews=False, data_completeness=False), ("Watch", "low")),
        (dict(overall_state="stable", thesis_state="holding", credibility_state="acceptable", has_critical_rules=True, has_open_reviews=False, data_completeness=False), ("Review manually", "low")),
    ]
    for payload, expected in cases:
        assert _derive_stance(**payload) == expected


def test_recommendation_summary_classifies_pick_and_avoid_candidates():
    pick = _build_recommendation_summary(
        ticker="GOOD",
        issuer_name="PT Good Shipping Tbk",
        as_of_document_id="doc-good-a",
        stance="Buy",
        confidence="high",
        overall_state="stable",
        thesis_state="improving",
        credibility_band="high",
        data_completeness_pct=88.0,
        why=["Comparable baseline is clean", "Improving thesis conditions are matched", "No review queue remains open"],
        what_changed=["Revenue changed +24.0% versus baseline.", "Operating profit changed +18.0% versus baseline."],
        what_to_watch=["Watch the next H1 filing window."],
        must_improve=["Keep leverage stable."],
        thesis_breakers=["Debt surge appears."],
        quality_flags=["Auditor changed."],
        positive_factors=["revenue increased", "operating_profit increased"],
        negative_factors=[],
        review_items=[],
        events=[],
        triggered_rules=[],
        peer_anomaly_count=0,
        decision_constraints=["Keep leverage stable.", "Debt surge appears.", "Watch the next H1 filing window."],
    )
    assert pick["bucket"] == RECOMMENDATION_BUCKET_STOCK_PICK
    assert pick["score"] > 0
    assert pick["reason_1"]
    assert pick["reason_2"]
    assert pick["reason_3"]

    avoid = _build_recommendation_summary(
        ticker="RISK",
        issuer_name="PT Risk Shipping Tbk",
        as_of_document_id="doc-risk-a",
        stance="Avoid",
        confidence="high",
        overall_state="critical",
        thesis_state="broken",
        credibility_band="poor",
        data_completeness_pct=82.0,
        why=["At least one configured thesis is broken", "Critical rules or events remain active", "Management credibility is poor"],
        what_changed=["New rule trigger: debt surge.", "New event detected: lawsuit."],
        what_to_watch=["Resolve the active debt stress before revisiting the name."],
        must_improve=["Clear critical rules."],
        thesis_breakers=["Debt surge appears."],
        quality_flags=["Debt grew faster than revenue.", "Legal or regulatory risk is active."],
        positive_factors=[],
        negative_factors=["debt_surge appeared", "lawsuit appeared"],
        review_items=[],
        events=[],
        triggered_rules=[],
        peer_anomaly_count=1,
        decision_constraints=["Clear critical rules.", "Debt surge appears.", "Resolve the active debt stress before revisiting the name."],
    )
    assert avoid["bucket"] == RECOMMENDATION_BUCKET_AVOID
    assert avoid["score"] > 0
    assert avoid["reason_1"]
    assert avoid["reason_2"]
    assert avoid["reason_3"]


def test_next_expected_filing_window_matches_v3_2_deadline_table():
    h1 = _next_expected_filing_window(
        fiscal_year_end_month=12,
        now=datetime(2026, 7, 10, tzinfo=timezone.utc).date(),
        latest_period_end=datetime(2026, 3, 31, tzinfo=timezone.utc).date(),
        latest_document_type="financial_statement",
    )
    assert h1 is not None
    assert h1["label"] == "H1 filing due"
    assert h1["window_start"] == "2026-08-08"

    annual_report = _next_expected_filing_window(
        fiscal_year_end_month=12,
        now=datetime(2026, 3, 1, tzinfo=timezone.utc).date(),
        latest_period_end=datetime(2025, 12, 31, tzinfo=timezone.utc).date(),
        latest_document_type="financial_statement",
    )
    assert annual_report is not None
    assert annual_report["report_type"] == "annual_report"
    assert annual_report["window_start"] == "2026-03-31"


def test_build_segment_summary_tracks_trend_and_concentration():
    latest_metrics = [
        MetricPoint(
            metric_id="m1",
            document_id="doc-current",
            metric_name="revenue",
            value_normalized=1000.0,
            raw_value_text="1000",
            currency="USD",
            unit_scale="units",
            fiscal_year=2025,
            fiscal_period="Annual",
            period_end_date="2025-12-31",
            audit_status="audited",
            consolidation_scope="consolidated",
            evidence_span_id=None,
        ),
        MetricPoint(
            metric_id="m2",
            document_id="doc-current",
            metric_name="segment_revenue",
            value_normalized=700.0,
            raw_value_text="700",
            currency="USD",
            unit_scale="units",
            fiscal_year=2025,
            fiscal_period="Annual",
            period_end_date="2025-12-31",
            audit_status="audited",
            consolidation_scope="segment:Retail",
            evidence_span_id=None,
        ),
        MetricPoint(
            metric_id="m3",
            document_id="doc-current",
            metric_name="segment_profit",
            value_normalized=105.0,
            raw_value_text="105",
            currency="USD",
            unit_scale="units",
            fiscal_year=2025,
            fiscal_period="Annual",
            period_end_date="2025-12-31",
            audit_status="audited",
            consolidation_scope="segment:Retail",
            evidence_span_id=None,
        ),
        MetricPoint(
            metric_id="m4",
            document_id="doc-current",
            metric_name="segment_revenue",
            value_normalized=300.0,
            raw_value_text="300",
            currency="USD",
            unit_scale="units",
            fiscal_year=2025,
            fiscal_period="Annual",
            period_end_date="2025-12-31",
            audit_status="audited",
            consolidation_scope="segment:Wholesale",
            evidence_span_id=None,
        ),
        MetricPoint(
            metric_id="m5",
            document_id="doc-current",
            metric_name="segment_profit",
            value_normalized=30.0,
            raw_value_text="30",
            currency="USD",
            unit_scale="units",
            fiscal_year=2025,
            fiscal_period="Annual",
            period_end_date="2025-12-31",
            audit_status="audited",
            consolidation_scope="segment:Wholesale",
            evidence_span_id=None,
        ),
    ]
    all_metrics = latest_metrics + [
        MetricPoint(
            metric_id="m6",
            document_id="doc-previous",
            metric_name="segment_revenue",
            value_normalized=500.0,
            raw_value_text="500",
            currency="USD",
            unit_scale="units",
            fiscal_year=2024,
            fiscal_period="Annual",
            period_end_date="2024-12-31",
            audit_status="audited",
            consolidation_scope="segment:Retail",
            evidence_span_id=None,
        ),
        MetricPoint(
            metric_id="m7",
            document_id="doc-previous",
            metric_name="segment_profit",
            value_normalized=80.0,
            raw_value_text="80",
            currency="USD",
            unit_scale="units",
            fiscal_year=2024,
            fiscal_period="Annual",
            period_end_date="2024-12-31",
            audit_status="audited",
            consolidation_scope="segment:Retail",
            evidence_span_id=None,
        ),
        MetricPoint(
            metric_id="m8",
            document_id="doc-previous",
            metric_name="segment_revenue",
            value_normalized=450.0,
            raw_value_text="450",
            currency="USD",
            unit_scale="units",
            fiscal_year=2024,
            fiscal_period="Annual",
            period_end_date="2024-12-31",
            audit_status="audited",
            consolidation_scope="segment:Wholesale",
            evidence_span_id=None,
        ),
        MetricPoint(
            metric_id="m9",
            document_id="doc-previous",
            metric_name="segment_profit",
            value_normalized=60.0,
            raw_value_text="60",
            currency="USD",
            unit_scale="units",
            fiscal_year=2024,
            fiscal_period="Annual",
            period_end_date="2024-12-31",
            audit_status="audited",
            consolidation_scope="segment:Wholesale",
            evidence_span_id=None,
        ),
    ]

    rows = _build_segment_summary(
        latest_document_id="doc-current",
        latest_metrics=latest_metrics,
        all_metrics=all_metrics,
    )

    assert rows[0]["segment_name"] == "Retail"
    assert rows[0]["revenue_share_pct"] == 70.0
    assert rows[0]["trend"] == "improving"
    assert rows[0]["dominant"] is True
    assert rows[0]["concentration_risk"] == "high"
    assert rows[1]["segment_name"] == "Wholesale"
    assert rows[1]["trend"] == "deteriorating"


def test_build_narrative_signals_is_deterministic_and_deduped():
    latest_analysis = AnalysisSummary(
        analysis_run_id="analysis-1",
        summary="Management expects performance to remain stable and still emphasizes the same turnaround plan.",
        what_to_verify=["Watch covenant headroom."],
        finished_at="2026-04-16T00:00:00+00:00",
    )
    events = [
        EventSignal(
            event_id="event-1",
            event_type="guidance_change",
            severity="warning",
            impact_direction="negative",
            confidence=0.8,
            rationale="Guidance was revised downward.",
            evidence_span_id=None,
            payload_json=json.dumps({"matched_regex": "guidance revision"}),
            detected_at="2026-04-16T00:00:00+00:00",
        ),
        EventSignal(
            event_id="event-2",
            event_type="use_of_proceeds",
            severity="info",
            impact_direction="mixed",
            confidence=0.75,
            rationale="Issuer updated proceeds deployment.",
            evidence_span_id=None,
            payload_json=json.dumps({"matched_regex": "use of proceeds"}),
            detected_at="2026-04-16T00:00:00+00:00",
        ),
    ]
    claims = [
        ClaimRecord(
            claim_id="claim-1",
            ticker="TAMU",
            source_document_id="doc-1",
            claim_text="Management expects guidance to improve.",
            claim_type="outlook_guidance",
            claim_topic="guidance",
            claim_subject="guidance",
            claim_date=None,
            claim_time_horizon="12m",
            claim_confidence=0.8,
            evidence_span_id=None,
            status="open",
            payload_json="{}",
        ),
        ClaimRecord(
            claim_id="claim-2",
            ticker="TAMU",
            source_document_id="doc-1",
            claim_text="Use of proceeds will fund fleet upgrades.",
            claim_type="funding_use",
            claim_topic="capital_allocation",
            claim_subject="use_of_proceeds",
            claim_date=None,
            claim_time_horizon="12m",
            claim_confidence=0.7,
            evidence_span_id=None,
            status="open",
            payload_json="{}",
        ),
        ClaimRecord(
            claim_id="claim-3",
            ticker="TAMU",
            source_document_id="doc-1",
            claim_text="Management explained downside risks.",
            claim_type="risk_explanation",
            claim_topic="risk",
            claim_subject="risk",
            claim_date=None,
            claim_time_horizon=None,
            claim_confidence=0.7,
            evidence_span_id=None,
            status="open",
            payload_json="{}",
        ),
    ]
    credibility_summary = ClaimCredibilitySummary(
        total_claims=3,
        achieved=1,
        partially_supported=0,
        missed=0,
        contradicted=1,
        unresolved=1,
        achieved_rate=0.33,
        missed_rate=0.0,
        unresolved_rate=0.33,
        recycled_claim_count=2,
        credibility_state="mixed",
    )

    signals = _build_narrative_signals(
        latest_analysis=latest_analysis,
        events=events,
        claims=claims,
        credibility_summary=credibility_summary,
    )

    assert signals == [
        "Management guidance was later revised or missed.",
        "Funding-use claims now have matching proceeds disclosures.",
        "Management explicitly framed downside risks in the latest narrative.",
        "Management repeated similar claims across filings, which may indicate boilerplate narrative reuse.",
    ]


def test_rank_next_read_rows_prioritizes_reason_and_section():
    rows = [
        NextReadRecord(
            guidance_id="g-1",
            ticker="TAMU",
            document_id="doc-1",
            priority_rank=3,
            target_document_type="annual_report",
            target_period_label=None,
            target_section_label="MD&A",
            reason_class="uncertainty_followup",
            why_this_matters="Read MD&A for narrative context.",
            evidence_reference_count=0,
            blocked_by=None,
            payload_json="{}",
        ),
        NextReadRecord(
            guidance_id="g-2",
            ticker="TAMU",
            document_id="doc-1",
            priority_rank=2,
            target_document_type="annual_report",
            target_period_label=None,
            target_section_label="Auditor Report",
            reason_class="contradiction_review",
            why_this_matters="Read auditor report for contradiction.",
            evidence_reference_count=1,
            blocked_by=None,
            payload_json="{}",
        ),
        NextReadRecord(
            guidance_id="g-3",
            ticker="TAMU",
            document_id="doc-1",
            priority_rank=1,
            target_document_type="financial_statement",
            target_period_label="Annual 2024",
            target_section_label=None,
            reason_class="missing_filing",
            why_this_matters="Fetch missing annual filing.",
            evidence_reference_count=0,
            blocked_by=None,
            payload_json="{}",
        ),
    ]

    ranked = _rank_next_read_rows(rows)

    assert [row.guidance_id for row in ranked] == ["g-3", "g-2", "g-1"]
    assert [row.priority_rank for row in ranked] == [1, 2, 3]


def test_build_uncertainty_rows_adds_note_level_scope_rows():
    latest_document = CompanyDocument(
        document_id="doc-1",
        source_name="idx_financial_reports",
        source_record_id="fin-1",
        ticker="TAMU",
        title="Annual Report 2024",
        document_type="annual_report",
        version_group_id=None,
        source_modified_at="2026-04-17T00:00:00+00:00",
        updated_at="2026-04-17T00:00:00+00:00",
        created_at="2026-04-17T00:00:00+00:00",
    )
    baseline = BaselineChoice(
        kind="prior_period",
        document=latest_document,
        comparable=True,
        reason="Comparable prior filing exists.",
    )
    events = [
        EventSignal(
            event_id="event-audit",
            event_type="qualified_audit_opinion",
            severity="critical",
            impact_direction="negative",
            confidence=0.98,
            rationale="Qualified opinion detected.",
            evidence_span_id="span-audit",
            payload_json="{}",
            detected_at="2026-04-17T00:00:00+00:00",
        ),
        EventSignal(
            event_id="event-rpt",
            event_type="related_party_transaction",
            severity="warning",
            impact_direction="mixed",
            confidence=0.88,
            rationale="Related-party transaction detected.",
            evidence_span_id="span-rpt",
            payload_json="{}",
            detected_at="2026-04-17T00:00:00+00:00",
        ),
    ]
    evidence_context = {
        "span-audit": EvidenceContext(
            span_id="span-audit",
            document_id="doc-1",
            section_label="Auditor Report",
            excerpt_text="Qualified opinion...",
        ),
        "span-rpt": EvidenceContext(
            span_id="span-rpt",
            document_id="doc-1",
            section_label="Notes to Financial Statements",
            excerpt_text="Related party transactions...",
        ),
    }

    rows = _build_uncertainty_rows(
        ticker="TAMU",
        latest_document=latest_document,
        baseline=baseline,
        events=events,
        review_items=[],
        peer_view={},
        filing_expectations=[],
        evidence_context=evidence_context,
        section_cues=[],
    )

    row_map = {row.uncertainty_class: row for row in rows}
    assert row_map["audit_qualification_scope"].section_label == "Auditor Report"
    assert row_map["audit_qualification_scope"].requires_human_review is True
    assert "Focus on Auditor Report" in row_map["audit_qualification_scope"].summary
    assert row_map["related_party_scope"].section_label == "Notes to Financial Statements"
    assert row_map["related_party_scope"].severity == "warning"


def test_build_uncertainty_rows_uses_section_cues_without_events():
    latest_document = CompanyDocument(
        document_id="doc-1",
        source_name="idx_financial_reports",
        source_record_id="fin-1",
        ticker="TAMU",
        title="Annual Report 2024",
        document_type="annual_report",
        version_group_id=None,
        source_modified_at="2026-04-17T00:00:00+00:00",
        updated_at="2026-04-17T00:00:00+00:00",
        created_at="2026-04-17T00:00:00+00:00",
    )
    baseline = BaselineChoice(
        kind="prior_period",
        document=latest_document,
        comparable=True,
        reason="Comparable prior filing exists.",
    )

    rows = _build_uncertainty_rows(
        ticker="TAMU",
        latest_document=latest_document,
        baseline=baseline,
        events=[],
        review_items=[],
        peer_view={},
        filing_expectations=[],
        evidence_context={},
        section_cues=[
            type("Cue", (), {
                "chunk_id": "chunk-1",
                "document_id": "doc-1",
                "section_label": "Notes to Financial Statements",
                "text": "The notes discuss related party transactions and contingent liabilities in detail.",
                "excerpt_text": "The notes discuss related party transactions and contingent liabilities in detail.",
            })()
        ],
    )

    row_map = {row.uncertainty_class: row for row in rows}
    assert row_map["related_party_scope"].section_label == "Notes to Financial Statements"
    assert row_map["contingent_liability_scope"].section_label == "Notes to Financial Statements"


def test_build_next_read_guidance_falls_back_to_section_focus_with_excerpt():
    latest_document = CompanyDocument(
        document_id="doc-1",
        source_name="idx_financial_reports",
        source_record_id="fin-1",
        ticker="TAMU",
        title="Annual Report 2024",
        document_type="annual_report",
        version_group_id=None,
        source_modified_at="2026-04-17T00:00:00+00:00",
        updated_at="2026-04-17T00:00:00+00:00",
        created_at="2026-04-17T00:00:00+00:00",
    )

    rows = _build_next_read_guidance(
        ticker="TAMU",
        latest_document=latest_document,
        filing_expectations=[],
        nuance_findings=[],
        uncertainty_rows=[],
        contradiction_rows=[],
        latest_analysis=None,
        section_cues=[
            type("Cue", (), {
                "chunk_id": "chunk-a",
                "document_id": "doc-1",
                "section_label": "Auditor Report",
                "text": "Independent auditor report text.",
                "excerpt_text": "Independent auditor report text.",
            })(),
            type("Cue", (), {
                "chunk_id": "chunk-b",
                "document_id": "doc-1",
                "section_label": "MD&A",
                "text": "Management discussion text.",
                "excerpt_text": "Management discussion text.",
            })(),
        ],
    )

    assert rows[0].reason_class == "section_focus"
    assert rows[0].target_section_label == "Auditor Report"
    assert json.loads(rows[0].payload_json)["excerpt_text"] == "Independent auditor report text."


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
                    "phase5-v2",
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
                    "metric-tamu-liabilities-prev",
                    "doc-tamu-b",
                    "TAMU",
                    "total_liabilities",
                    800000.0,
                    "800,000",
                    "USD",
                    "units",
                    2024,
                    "Annual",
                    "2024-12-31",
                    "audited",
                    "consolidated",
                    "bilingual",
                    None,
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
                    "metric-tamu-op-profit-prev",
                    "doc-tamu-b",
                    "TAMU",
                    "operating_profit",
                    200000.0,
                    "200,000",
                    "USD",
                    "units",
                    2024,
                    "Annual",
                    "2024-12-31",
                    "audited",
                    "consolidated",
                    "bilingual",
                    None,
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
                INSERT INTO metric_items (
                    metric_id, document_id, ticker, metric_name, value_normalized, raw_value_text, currency,
                    unit_scale, fiscal_year, fiscal_period, period_end_date, audit_status, consolidation_scope,
                    language_variant, evidence_span_id, extracted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "metric-tamu-liabilities-current",
                    "doc-tamu-a",
                    "TAMU",
                    "total_liabilities",
                    950000.0,
                    "950,000",
                    "USD",
                    "units",
                    2024,
                    "Annual",
                    "2024-12-31",
                    "audited",
                    "consolidated",
                    "bilingual",
                    "span-tamu-1",
                    newer,
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
                    "metric-tamu-op-profit-current",
                    "doc-tamu-a",
                    "TAMU",
                    "operating_profit",
                    350000.0,
                    "350,000",
                    "USD",
                    "units",
                    2024,
                    "Annual",
                    "2024-12-31",
                    "audited",
                    "consolidated",
                    "bilingual",
                    "span-tamu-1",
                    newer,
                ),
            )
            conn.execute(
                """
                INSERT INTO document_chunks (
                    chunk_id, document_id, section_label, page_start, page_end, char_start, char_end, text, ord
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "chunk-claim-prev",
                    "doc-tamu-b",
                    "Outlook",
                    12,
                    12,
                    0,
                    180,
                    "Management expects revenue growth in 2025. The company plans to reduce debt over the next 12 months. Management expects margins to improve after restructuring.",
                    1,
                ),
            )
            conn.execute(
                """
                INSERT INTO evidence_spans (
                    span_id, document_id, chunk_id, page_start, page_end, char_start, char_end, excerpt_text, display_anchor
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "span-tamu-prev-claim",
                    "doc-tamu-b",
                    "chunk-claim-prev",
                    12,
                    12,
                    0,
                    150,
                    "Management expects revenue growth in 2025. The company plans to reduce debt over the next 12 months. Management expects margins to improve after restructuring.",
                    "p12",
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
            peer_companies = [
                ("SHIP", "PT Shipping Alpha Tbk", "USD", 3900000.0, 4200000.0, 150000.0, 700000.0, []),
                ("DEEP", "PT Deep Harbor Tbk", "USD", 3950000.0, 4300000.0, 175000.0, 680000.0, ["governance_issue"]),
                ("WAVE", "PT Wave Marine Tbk", "USD", 4500000.0, 4700000.0, 210000.0, 640000.0, []),
                ("SAFE", "PT Safety Shipping Tbk", "IDR", 3000000.0, 3200000.0, 120000.0, 500000.0, ["capital_raise"]),
            ]
            for ticker, issuer_name, currency, previous_revenue, current_revenue, operating_profit, total_liabilities, event_types in peer_companies:
                if ticker != "SAFE":
                    conn.execute(
                        """
                        INSERT INTO companies (
                            ticker, issuer_name, listing_date, active_status, sector, subsector, industry,
                            listing_board, source_urls, raw_metadata, first_seen_at, last_seen_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            ticker,
                            issuer_name,
                            "2019-01-01",
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
                conn.execute(
                    """
                    INSERT INTO document_versions (
                        version_group_id, source_name, source_record_id, ticker, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (f"vg-{ticker.lower()}-1", "idx_financial_reports", f"fin-2024-{ticker.lower()}", ticker, newer),
                )
                for suffix, stamp in (("b", older), ("a", newer)):
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
                            f"doc-{ticker.lower()}-{suffix}",
                            "idx_financial_reports",
                            f"fin-2024-{ticker.lower()}",
                            f"fin-2024-{ticker.lower()}-{suffix}",
                            stamp,
                            stamp,
                            f"https://example.test/{ticker.lower()}.pdf",
                            str(tmp_config.paths.data_dir / f"{ticker.lower()}-annual-report.pdf"),
                            f"sha-{ticker.lower()}-{suffix}",
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
                            ticker,
                            f"vg-{ticker.lower()}-1",
                            stamp,
                            stamp,
                        ),
                    )
                metric_rows = [
                    (f"metric-{ticker.lower()}-revenue-prev", f"doc-{ticker.lower()}-b", "revenue", previous_revenue, older),
                    (f"metric-{ticker.lower()}-revenue-current", f"doc-{ticker.lower()}-a", "revenue", current_revenue, newer),
                    (f"metric-{ticker.lower()}-op-profit-current", f"doc-{ticker.lower()}-a", "operating_profit", operating_profit, newer),
                    (f"metric-{ticker.lower()}-liabilities-current", f"doc-{ticker.lower()}-a", "total_liabilities", total_liabilities, newer),
                ]
                for metric_id, document_id, metric_name, value, stamp in metric_rows:
                    conn.execute(
                        """
                        INSERT INTO metric_items (
                            metric_id, document_id, ticker, metric_name, value_normalized, raw_value_text, currency,
                            unit_scale, fiscal_year, fiscal_period, period_end_date, audit_status, consolidation_scope,
                            language_variant, evidence_span_id, extracted_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            metric_id,
                            document_id,
                            ticker,
                            metric_name,
                            value,
                            str(value),
                            currency,
                            "units",
                            2024,
                            "Annual",
                            "2024-12-31",
                            "audited",
                            "consolidated",
                            "bilingual",
                            None,
                            stamp,
                        ),
                    )
                for idx, event_type in enumerate(event_types, start=1):
                    conn.execute(
                        """
                        INSERT INTO events (
                            event_id, document_id, ticker, event_type, matched_pattern, severity, rationale, evidence_span_id, detected_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"event-{ticker.lower()}-{idx}",
                            f"doc-{ticker.lower()}-a",
                            ticker,
                            event_type,
                            event_type,
                            "warning",
                            f"{ticker} emitted {event_type}.",
                            None,
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
