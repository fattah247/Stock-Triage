from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta, timezone

from stock_triage.commands.intelligence import cmd_intelligence_run
from stock_triage.db import WriterQueue, connect, configure_pragmas, run_migrations
from stock_triage.fiscal_year import save_fiscal_year_override
from stock_triage.intelligence import DERIVED_VERSION, rederive_intelligence, run_intelligence
from stock_triage.intelligence.claims import ClaimCredibilitySummary, ClaimRecord
from stock_triage.intelligence.pipeline import (
    AnalysisSummary,
    BaselineChoice,
    CompanyDocument,
    CompanyProfile,
    ContradictionHistoryRow,
    ContradictionRecord,
    EvidenceContext,
    EventSignal,
    FilingDocument,
    FilingExpectationRecord,
    FocusHistoryRow,
    MetricPoint,
    NextReadRecord,
    NuanceFindingRecord,
    RECOMMENDATION_BUCKET_AVOID,
    RECOMMENDATION_BUCKET_STOCK_PICK,
    _build_filing_expectations,
    _build_nuance_findings,
    _build_narrative_signals,
    _build_next_read_guidance,
    _build_persistence_review_items,
    _build_persistent_contradiction_signals,
    _build_persistent_risk_signals,
    _build_unresolved_nuance_summary,
    _build_uncertainty_rows,
    _rank_next_read_rows,
    _build_recommendation_summary,
    _build_segment_summary,
    _derive_stance,
    _resolved_nuance_severity_tier,
    UncertaintyRecord,
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
        assert any(item["nuance_class"] == "management_contradiction" for item in payload["nuance_findings"])
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
        # Peers have no events (median 0): worst in group, but not a measurable gap.
        assert payload["peer_comparison"]["metric_ranks"]["event_density"]["relative_gap_pct"] is None
        assert payload["peer_comparison"]["metric_ranks"]["event_density"]["severity"] == "warning"
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


def test_run_intelligence_persists_v35_issuer_condition(tmp_config):
    _seed_phase10_inputs(tmp_config)
    _write_phase12_theses(tmp_config)

    run_intelligence(tmp_config, ticker="TAMU")

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        snapshot = rconn.execute(
            """
            SELECT issuer_condition, payload_json
            FROM company_snapshots
            WHERE ticker = ? AND snapshot_kind = 'current' AND derived_version = ?
            """,
            ("TAMU", DERIVED_VERSION),
        ).fetchone()
        assert snapshot is not None
        payload = json.loads(snapshot["payload_json"])
        assert snapshot["issuer_condition"] in {"clean", "watch", "stressed"}
        assert payload["issuer_condition"] == snapshot["issuer_condition"]
        assert payload["top_card"]["issuer_condition"] == snapshot["issuer_condition"]
        assert payload["decision_panel"]["issuer_condition"] == snapshot["issuer_condition"]

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


def test_run_golden_set_adds_sparse_issuer_case(tmp_config):
    _seed_phase8_data(tmp_config)
    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        as_of_document_id = rconn.execute(
            "SELECT document_id FROM documents WHERE ticker = ? ORDER BY updated_at DESC, document_id DESC LIMIT 1",
            ("TAMU",),
        ).fetchone()["document_id"]
    finally:
        rconn.close()
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "top_card": {"overall_state": "stable"},
        "decision_panel": {"stance": "Watch", "confidence": "medium"},
        "filing_expectations": [{"obligation_type": "annual_report", "state": "satisfied"}],
        "recommendation": {"bucket": "neither"},
        "events": [],
        "segment_summary": [],
        "narrative_signals": ["watch liquidity"],
        "review_required": [],
    }

    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)

        def _seed_sparse_snapshot(conn) -> None:
            conn.execute(
                """
                INSERT INTO company_snapshots (
                    ticker, as_of_document_id, as_of_date, snapshot_kind, payload_json,
                    issuer_condition, derived_version, created_at, updated_at
                ) VALUES (?, ?, ?, 'current', ?, ?, ?, ?, ?)
                    """,
                    (
                        "TAMU",
                        as_of_document_id,
                        "2026-04-18",
                        json.dumps(payload, sort_keys=True),
                        "watch",
                    DERIVED_VERSION,
                    now,
                    now,
                ),
            )

        wq.submit(_seed_sparse_snapshot)

    result = run_golden_set(tmp_config, ticker="TAMU", limit=5)

    assert result.case_count >= 1
    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        row = rconn.execute(
            """
            SELECT payload_json
            FROM golden_set_runs
            WHERE golden_run_id = ?
            """,
            (result.golden_run_id,),
        ).fetchone()
        payload = json.loads(row["payload_json"])
        sparse_case = next(case for case in payload["cases"] if case["case_name"] == "sparse_issuer_handled")
        assert sparse_case["status"] == "pass"
    finally:
        rconn.close()


def test_run_golden_set_adds_no_snapshot_case(tmp_config):
    _seed_phase8_data(tmp_config)

    result = run_golden_set(tmp_config, ticker="TAMU", limit=1)

    assert result.case_count == 1
    assert result.passed_count == 1
    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        row = rconn.execute(
            """
            SELECT payload_json
            FROM golden_set_runs
            WHERE golden_run_id = ?
            """,
            (result.golden_run_id,),
        ).fetchone()
        payload = json.loads(row["payload_json"])
        assert payload["cases"][0]["case_name"] == "no_snapshot_issuer_handled"
        assert payload["cases"][0]["status"] == "pass"
        assert payload["no_case_reason"]
    finally:
        rconn.close()


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
        peer_weakness_count=0,
        decision_constraints=["Keep leverage stable.", "Debt surge appears.", "Watch the next H1 filing window."],
        persistent_risk_signals=[],
        persistent_contradiction_signals=[],
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
        peer_weakness_count=1,
        decision_constraints=["Clear critical rules.", "Debt surge appears.", "Resolve the active debt stress before revisiting the name."],
        persistent_risk_signals=[],
        persistent_contradiction_signals=[],
    )
    assert avoid["bucket"] == RECOMMENDATION_BUCKET_AVOID
    assert avoid["score"] > 0
    assert avoid["reason_1"]
    assert avoid["reason_2"]
    assert avoid["reason_3"]


def test_recommendation_summary_demotes_pick_when_persistent_critical_risk_is_active():
    recommendation = _build_recommendation_summary(
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
        quality_flags=[],
        positive_factors=["revenue increased", "operating_profit increased"],
        negative_factors=[],
        review_items=[],
        events=[],
        triggered_rules=[],
        peer_weakness_count=0,
        decision_constraints=["Keep leverage stable.", "Debt surge appears.", "Watch the next H1 filing window."],
        persistent_risk_signals=[
            {
                "label": "Legal / Regulatory",
                "severity": "critical",
                "persistence_state": "active",
                "repeat_count": 2,
            }
        ],
        persistent_contradiction_signals=[],
    )

    assert recommendation["bucket"] == "neither"
    assert recommendation["ranking_inputs"]["persistent_active_critical_count"] == 1
    assert recommendation["eligible"] is False


def test_filing_expectations_follow_the_deadline_table_and_skip_unobserved_periods():
    profile = CompanyProfile(
        ticker="ABCD",
        issuer_name="Issuer ABCD",
        sector=None,
        subsector=None,
        industry=None,
        listing_board=None,
        active_status="listed",
        fiscal_year_end_month=12,
        fiscal_year_end_source="default",
    )
    observed = FilingDocument(
        document_id="doc-fy25",
        document_type="financial_statement",
        as_of="2026-03-28T10:00:00+07:00",
        period_end_dates=("2025-12-31",),
        fiscal_periods=("Annual",),
    )
    rows = {
        (row.period_label, row.period_end_date): row
        for row in _build_filing_expectations(
            profile=profile,
            filing_documents=[observed],
            now=date(2026, 7, 10),
        )
    }

    h1 = rows[("H1 filing", "2026-06-30")]
    assert (h1.window_start, h1.deadline_at, h1.state) == ("2026-08-08", "2026-08-29", "upcoming")
    assert rows[("Annual report", "2026-12-31")].window_start == "2027-03-31"
    assert rows[("Annual FS", "2025-12-31")].state == "satisfied"
    # Due before anything was collected for the issuer: not evidence of a late filing.
    assert rows[("Q3 filing", "2025-09-30")].state == "unobserved"
    assert rows[("Q1 filing", "2026-03-31")].state == "late"


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
                "page_start": 12,
                "page_end": 13,
                "text": "The notes discuss related party transactions and contingent liabilities in detail.",
                "excerpt_text": "The notes discuss related party transactions and contingent liabilities in detail.",
            })()
        ],
    )

    row_map = {row.uncertainty_class: row for row in rows}
    assert row_map["related_party_scope"].section_label == "Notes to Financial Statements"
    assert row_map["contingent_liability_scope"].section_label == "Notes to Financial Statements"


def test_build_nuance_findings_uses_section_cues_without_events():
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

    rows = _build_nuance_findings(
        ticker="TAMU",
        latest_document=latest_document,
        latest_analysis=None,
        latest_metrics=[],
        events=[],
        triggered_rules=[],
        claims=[],
        claim_outcomes=[],
        segment_summary=[],
        evidence_context={},
        section_cues=[
            type("Cue", (), {
                "chunk_id": "chunk-1",
                "document_id": "doc-1",
                "section_label": "Legal and Regulatory",
                "page_start": 44,
                "page_end": 45,
                "text": "The company discusses a lawsuit and regulatory sanction in this section.",
                "excerpt_text": "The company discusses a lawsuit and regulatory sanction in this section.",
            })()
        ],
    )

    row_map = {row.nuance_class: row for row in rows}
    legal_row = row_map["legal_or_regulatory_severity"]
    assert legal_row.section_label == "Legal and Regulatory"
    assert legal_row.severity_tier == "active_litigation"
    assert json.loads(legal_row.payload_json)["excerpt_text"] == "The company discusses a lawsuit and regulatory sanction in this section."
    assert json.loads(legal_row.payload_json)["page_start"] == 44
    assert json.loads(legal_row.payload_json)["stage"] == "active_process"
    assert json.loads(legal_row.payload_json)["severity_context"] == "developing"
    assert json.loads(legal_row.payload_json)["severity_tier"] == "active_litigation"
    assert json.loads(legal_row.payload_json)["source_sections"] == ["Legal and Regulatory"]
    assert _resolved_nuance_severity_tier(legal_row) == "active_litigation"


def test_build_nuance_findings_adds_capital_allocation_signals():
    latest_document = CompanyDocument(
        document_id="doc-1",
        source_name="idx_disclosures",
        source_record_id="disc-1",
        ticker="TAMU",
        title="Keterbukaan Informasi",
        document_type="disclosure",
        version_group_id=None,
        source_modified_at="2026-04-17T00:00:00+00:00",
        updated_at="2026-04-17T00:00:00+00:00",
        created_at="2026-04-17T00:00:00+00:00",
    )
    rows = _build_nuance_findings(
        ticker="TAMU",
        latest_document=latest_document,
        latest_analysis=None,
        latest_metrics=[
            MetricPoint(
                metric_id="m1",
                document_id="doc-1",
                metric_name="operating_cash_flow",
                value_normalized=-1000000.0,
                raw_value_text="-1000000",
                currency="IDR",
                unit_scale="units",
                fiscal_year=2025,
                fiscal_period="Annual",
                period_end_date="2025-12-31",
                audit_status=None,
                consolidation_scope=None,
                evidence_span_id=None,
            ),
            MetricPoint(
                metric_id="m2",
                document_id="doc-1",
                metric_name="free_cash_flow",
                value_normalized=-500000.0,
                raw_value_text="-500000",
                currency="IDR",
                unit_scale="units",
                fiscal_year=2025,
                fiscal_period="Annual",
                period_end_date="2025-12-31",
                audit_status=None,
                consolidation_scope=None,
                evidence_span_id=None,
            ),
        ],
        events=[
            EventSignal(
                event_id="ev-raise",
                event_type="rights_issue",
                severity="warning",
                impact_direction="negative",
                confidence=0.9,
                rationale="Issuer launched a rights issue.",
                evidence_span_id="span-raise",
                payload_json=None,
                detected_at="2026-04-17T00:00:00+00:00",
            ),
            EventSignal(
                event_id="ev-proceeds",
                event_type="use_of_proceeds",
                severity="info",
                impact_direction="mixed",
                confidence=0.8,
                rationale="Issuer updated proceeds deployment.",
                evidence_span_id="span-proceeds",
                payload_json=None,
                detected_at="2026-04-17T00:00:00+00:00",
            ),
        ],
        triggered_rules=[],
        claims=[
            ClaimRecord(
                claim_id="claim-1",
                ticker="TAMU",
                source_document_id="doc-1",
                claim_text="Use of proceeds will fund working capital needs.",
                claim_type="funding_use",
                claim_topic="funding",
                claim_subject="use_of_proceeds",
                claim_date=None,
                claim_time_horizon=None,
                claim_confidence=0.8,
                evidence_span_id=None,
                status="open",
                payload_json=json.dumps({"qualitative_only": True}, sort_keys=True),
            )
        ],
        claim_outcomes=[],
        segment_summary=[],
            evidence_context={
                "span-proceeds": EvidenceContext(
                    span_id="span-proceeds",
                    document_id="doc-1",
                    section_label="Use of Proceeds",
                    excerpt_text="The proceeds will be used for working capital and debt repayment and may be reallocated later.",
                )
            },
        section_cues=[],
    )

    row_map = {row.nuance_class: row for row in rows}
    assert "capital_allocation_shift" in row_map
    assert "use_of_proceeds_follow_through" in row_map
    capital_payload = json.loads(row_map["capital_allocation_shift"].payload_json)
    assert "dilution_dependence" in capital_payload["subsignals"]
    proceeds_payload = json.loads(row_map["use_of_proceeds_follow_through"].payload_json)
    assert proceeds_payload["working_capital_use"] is True
    assert proceeds_payload["revision_signal"] is True
    assert "proceeds_redeployment" in proceeds_payload["subsignals"]


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
                "page_start": 3,
                "page_end": 4,
                "text": "Independent auditor report text.",
                "excerpt_text": "Independent auditor report text.",
            })(),
            type("Cue", (), {
                "chunk_id": "chunk-b",
                "document_id": "doc-1",
                "section_label": "MD&A",
                "page_start": 8,
                "page_end": 8,
                "text": "Management discussion text.",
                "excerpt_text": "Management discussion text.",
            })(),
        ],
    )

    assert rows[0].reason_class == "section_focus"
    assert rows[0].target_section_label == "Auditor Report"
    assert json.loads(rows[0].payload_json)["excerpt_text"] == "Independent auditor report text."
    assert json.loads(rows[0].payload_json)["page_start"] == 3


def test_build_next_read_guidance_carries_nuance_excerpt():
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
        nuance_findings=[
            type("Finding", (), {
                "nuance_id": "nuance-1",
                "section_label": "Auditor Report",
                "summary": "Auditor qualification language appears in the current filing.",
                "requires_human_review": True,
                "evidence_span_id": None,
                "nuance_class": "auditor_language",
                "severity": "critical",
                "payload_json": json.dumps({"excerpt_text": "Basis for qualified opinion paragraph.", "page_start": 7, "page_end": 8}, sort_keys=True),
            })()
        ],
        uncertainty_rows=[],
        contradiction_rows=[],
        latest_analysis=None,
        section_cues=[],
    )

    assert rows[0].reason_class == "nuance_review"
    assert json.loads(rows[0].payload_json)["excerpt_text"] == "Basis for qualified opinion paragraph."
    assert json.loads(rows[0].payload_json)["page_start"] == 7


def test_build_unresolved_nuance_summary_groups_focuses():
    summary = _build_unresolved_nuance_summary(
        filing_expectations=[
            FilingExpectationRecord(
                expectation_id="exp-1",
                ticker="TAMU",
                obligation_type="annual_report",
                fiscal_year=2025,
                period_label="FY2025",
                period_end_date="2025-12-31",
                window_start="2026-01-01",
                deadline_at="2026-04-30",
                satisfied_at=None,
                state="late",
                knowledge_source="stored",
                satisfied_document_id=None,
                payload_json="{}",
            )
        ],
        nuance_findings=[
            NuanceFindingRecord(
                nuance_id="n1",
                ticker="TAMU",
                document_id="doc-1",
                nuance_class="legal_or_regulatory_severity",
                severity="critical",
                confidence=None,
                section_label="Legal and Regulatory",
                summary="Legal dispute language appears.",
                evidence_span_id=None,
                requires_human_review=True,
                payload_json="{}",
            ),
            NuanceFindingRecord(
                nuance_id="n2",
                ticker="TAMU",
                document_id="doc-1",
                nuance_class="use_of_proceeds_follow_through",
                severity="warning",
                confidence=None,
                section_label="Use of Proceeds",
                summary="Use of proceeds was revised.",
                evidence_span_id=None,
                requires_human_review=True,
                payload_json="{}",
            ),
        ],
        uncertainty_rows=[
            UncertaintyRecord(
                uncertainty_id="u1",
                ticker="TAMU",
                document_id="doc-1",
                uncertainty_class="regulatory_severity_scope",
                severity="critical",
                summary="Regulatory restriction needs direct reading.",
                confidence=None,
                section_label="Legal and Regulatory",
                evidence_span_id=None,
                requires_human_review=True,
                payload_json="{}",
            )
        ],
        contradiction_rows=[
            ContradictionRecord(
                contradiction_id="c1",
                ticker="TAMU",
                document_id="doc-1",
                contradiction_class="claim_vs_outcome",
                severity="critical",
                summary="Current claim conflicts with later evidence.",
                source_claim_id="claim-1",
                section_label="MD&A",
                evidence_span_id=None,
                payload_json="{}",
            )
        ],
        next_read_guidance=[
            NextReadRecord(
                guidance_id="g1",
                ticker="TAMU",
                document_id="doc-1",
                priority_rank=1,
                target_document_type="annual_report",
                target_period_label=None,
                target_section_label="Legal and Regulatory",
                reason_class="nuance_review",
                why_this_matters="Read the legal section first.",
                evidence_reference_count=0,
                blocked_by=None,
                payload_json="{}",
            )
        ],
        persistent_risk_signals=[],
        persistent_contradiction_signals=[
            {
                "contradiction_class": "claim_vs_outcome",
                "label": "Claim vs Outcome",
                "repeat_count": 2,
                "summary": "Claim credibility contradiction persists across 2 recent analyzed filings.",
                "severity": "critical",
            }
        ],
    )

    assert summary["critical_count"] == 5
    assert summary["focus_rows"][0]["label"] == "Claim / Credibility"
    assert {item["label"] for item in summary["focus_rows"]} >= {"Claim / Credibility", "Legal / Regulatory"}
    assert summary["persistent_contradictions"][0]["label"] == "Claim vs Outcome"
    assert summary["next_action"]["why_this_matters"] == "Read the legal section first."


def test_build_persistent_risk_signals_repeats_focus_across_filings():
    rows = _build_persistent_risk_signals(
        [
            FocusHistoryRow(
                document_id="doc-1",
                document_type="annual_report",
                as_of="2026-04-01T00:00:00+00:00",
                focus_keys=("capital_allocation", "legal_regulatory"),
            ),
            FocusHistoryRow(
                document_id="doc-2",
                document_type="financial_statement",
                as_of="2025-12-31T00:00:00+00:00",
                focus_keys=("capital_allocation",),
            ),
            FocusHistoryRow(
                document_id="doc-3",
                document_type="annual_report",
                as_of="2025-06-30T00:00:00+00:00",
                focus_keys=("accounting_audit",),
            ),
            FocusHistoryRow(
                document_id="doc-4",
                document_type="annual_report",
                as_of="2024-12-31T00:00:00+00:00",
                focus_keys=("accounting_audit",),
            ),
        ]
    )

    assert rows[0]["focus_key"] == "accounting_audit"
    assert rows[0]["repeat_count"] == 2
    assert rows[0]["persistence_state"] == "reappeared"
    assert rows[1]["focus_key"] == "capital_allocation"
    assert rows[1]["repeat_count"] == 2
    assert rows[1]["persistence_state"] == "active"


def test_build_persistent_contradiction_signals_repeats_across_filings():
    rows = _build_persistent_contradiction_signals(
        [
            ContradictionHistoryRow(
                document_id="doc-1",
                document_type="annual_report",
                as_of="2026-04-01T00:00:00+00:00",
                contradiction_class="claim_vs_outcome",
                severity="critical",
                source_claim_id="claim-1",
            ),
            ContradictionHistoryRow(
                document_id="doc-2",
                document_type="financial_statement",
                as_of="2025-12-31T00:00:00+00:00",
                contradiction_class="claim_vs_outcome",
                severity="critical",
                source_claim_id="claim-2",
            ),
            ContradictionHistoryRow(
                document_id="doc-3",
                document_type="annual_report",
                as_of="2025-06-30T00:00:00+00:00",
                contradiction_class="mixed_contract_signals",
                severity="warning",
                source_claim_id=None,
            ),
            ContradictionHistoryRow(
                document_id="doc-4",
                document_type="annual_report",
                as_of="2024-12-31T00:00:00+00:00",
                contradiction_class="mixed_contract_signals",
                severity="warning",
                source_claim_id=None,
            ),
        ]
    )

    assert rows[0]["contradiction_class"] == "claim_vs_outcome"
    assert rows[0]["repeat_count"] == 2
    assert rows[0]["severity"] == "critical"
    assert "persists across 2 recent analyzed filings" in rows[0]["summary"]
    assert rows[0]["persistence_state"] == "active"
    assert rows[1]["contradiction_class"] == "mixed_contract_signals"
    assert rows[1]["repeat_count"] == 2
    assert rows[1]["persistence_state"] == "reappeared"


def test_build_persistent_risk_signals_marks_reappeared_after_gap():
    rows = _build_persistent_risk_signals(
        [
            FocusHistoryRow(
                document_id="doc-1",
                document_type="annual_report",
                as_of="2026-04-15T00:00:00+00:00",
                focus_keys=("legal_regulatory",),
            ),
            FocusHistoryRow(
                document_id="doc-2",
                document_type="annual_report",
                as_of="2025-08-01T00:00:00+00:00",
                focus_keys=("legal_regulatory",),
            ),
        ]
    )

    assert rows[0]["persistence_state"] == "reappeared"
    assert rows[0]["gap_days"] >= 180
    assert "Reappeared after" in rows[0]["summary"]


def test_build_persistent_contradiction_signals_marks_active_when_recent():
    rows = _build_persistent_contradiction_signals(
        [
            ContradictionHistoryRow(
                document_id="doc-1",
                document_type="annual_report",
                as_of="2026-04-15T00:00:00+00:00",
                contradiction_class="claim_vs_outcome",
                severity="critical",
                source_claim_id="claim-1",
            ),
            ContradictionHistoryRow(
                document_id="doc-2",
                document_type="financial_statement",
                as_of="2026-01-15T00:00:00+00:00",
                contradiction_class="claim_vs_outcome",
                severity="critical",
                source_claim_id="claim-2",
            ),
        ]
    )

    assert rows[0]["persistence_state"] == "active"
    assert rows[0]["latest_age_days"] <= 120


def test_build_persistence_review_items_emits_active_and_reappeared_only():
    rows = _build_persistence_review_items(
        ticker="TAMU",
        document_id="doc-1",
        persistent_risk_signals=[
            {
                "focus_key": "legal_regulatory",
                "label": "Legal / Regulatory",
                "repeat_count": 2,
                "severity": "critical",
                "summary": "Legal / Regulatory issues repeated across recent analyzed filings.",
                "persistence_state": "active",
                "latest_age_days": 12,
                "gap_days": 30,
            },
            {
                "focus_key": "capital_allocation",
                "label": "Capital Allocation",
                "repeat_count": 2,
                "severity": "warning",
                "summary": "Capital allocation issues repeated across recent analyzed filings.",
                "persistence_state": "stale",
                "latest_age_days": 480,
                "gap_days": 240,
            },
        ],
        persistent_contradiction_signals=[
            {
                "contradiction_class": "claim_vs_outcome",
                "label": "Claim vs Outcome",
                "repeat_count": 2,
                "severity": "critical",
                "summary": "Claim credibility contradiction persists across recent analyzed filings.",
                "persistence_state": "reappeared",
                "latest_age_days": 25,
                "gap_days": 190,
            }
        ],
    )

    assert [row.item_type for row in rows] == ["persistence_focus", "persistence_contradiction"]
    assert rows[0].priority == "high"
    assert rows[1].priority == "high"
    assert "legal_regulatory" in json.loads(rows[0].payload_json)["focus_key"]
    assert json.loads(rows[1].payload_json)["persistence_state"] == "reappeared"


def test_build_uncertainty_rows_marks_regulatory_restriction_context():
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
                "section_label": "Legal and Regulatory",
                "page_start": 21,
                "page_end": 22,
                "text": "The company is under special supervision and faces pembatasan kegiatan usaha from the regulator.",
                "excerpt_text": "The company is under special supervision and faces pembatasan kegiatan usaha from the regulator.",
            })()
        ],
    )

    row_map = {row.uncertainty_class: row for row in rows}
    payload = json.loads(row_map["regulatory_severity_scope"].payload_json)
    assert payload["operational_restriction"] is True
    assert payload["severity_context"] == "material"


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
                    str(tmp_config.paths.files_dir / "tamu-annual-report.pdf"),
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


def test_persistence_state_is_anchored_to_filings_not_the_wall_clock(monkeypatch):
    """An idle operator must not see recurring risks expire on their own."""
    import datetime as real_datetime

    import stock_triage.intelligence.pipeline as pipeline_module

    class FarFuture(real_datetime.date):
        @classmethod
        def today(cls):
            return cls(2031, 1, 1)

    history = [
        FocusHistoryRow(
            document_id="doc-1",
            document_type="annual_report",
            as_of="2026-04-01T00:00:00+00:00",
            focus_keys=("accounting_audit",),
        ),
        FocusHistoryRow(
            document_id="doc-2",
            document_type="financial_statement",
            as_of="2025-12-31T00:00:00+00:00",
            focus_keys=("accounting_audit",),
        ),
    ]
    before = _build_persistent_risk_signals(history)
    monkeypatch.setattr(pipeline_module, "date", FarFuture)
    after = _build_persistent_risk_signals(history)

    assert before == after
    assert after[0]["persistence_state"] == "active"
    assert after[0]["latest_age_days"] == 0


def test_persistence_state_turns_stale_only_when_later_filings_omit_it():
    rows = _build_persistent_risk_signals(
        [
            FocusHistoryRow(
                document_id="doc-old-1",
                document_type="annual_report",
                as_of="2024-06-30T00:00:00+00:00",
                focus_keys=("legal_regulatory",),
            ),
            FocusHistoryRow(
                document_id="doc-old-2",
                document_type="annual_report",
                as_of="2024-03-31T00:00:00+00:00",
                focus_keys=("legal_regulatory",),
            ),
        ],
        reference_date=datetime(2026, 3, 31, tzinfo=timezone.utc).date(),
    )

    assert rows[0]["persistence_state"] == "stale"
    assert "older than the latest filing" in rows[0]["summary"]


def test_rederive_keeps_review_item_ids_that_have_audit_history(tmp_config):
    """A new filing changes the computed id; the audited row must keep its own."""
    from stock_triage.intelligence.review import update_review_status

    _seed_phase10_inputs(tmp_config)
    _write_phase12_theses(tmp_config)
    run_intelligence(tmp_config, ticker="TAMU")
    conn = connect(tmp_config.paths.db_path)
    try:
        configure_pragmas(conn, tmp_config)
        row = conn.execute(
            "SELECT review_item_id, source_key FROM review_queue_items WHERE ticker = 'TAMU' AND status = 'open' LIMIT 1"
        ).fetchone()
        assert row is not None
        conn.execute(
            "UPDATE review_queue_items SET review_item_id = 'review-from-older-filing' WHERE review_item_id = ?",
            (row["review_item_id"],),
        )
    finally:
        conn.close()
    # Resolve then reopen: the item is open again and now has audit history.
    update_review_status(tmp_config, review_item_id="review-from-older-filing", status="resolved")
    update_review_status(tmp_config, review_item_id="review-from-older-filing", status="open")

    result = run_intelligence(tmp_config, ticker="TAMU")

    assert result.failed_tickers == ()
    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        ids = {
            r["review_item_id"]
            for r in conn.execute(
                "SELECT review_item_id FROM review_queue_items WHERE source_key = ?", (row["source_key"],)
            )
        }
    finally:
        conn.close()
    assert "review-from-older-filing" in ids


def test_one_failing_issuer_does_not_abort_the_run(tmp_config, monkeypatch):
    import stock_triage.intelligence.pipeline as pipeline_module

    _seed_phase10_inputs(tmp_config)
    _write_phase12_theses(tmp_config)
    real_build = pipeline_module._build_bundle

    def flaky_build(reader, cfg, ticker, *, as_of):
        if ticker == "BROKEN":
            raise ValueError("malformed metric payload")
        return real_build(reader, cfg, ticker, as_of=as_of)

    monkeypatch.setattr(pipeline_module, "_select_tickers", lambda conn, ticker, limit: ["BROKEN", "TAMU"])
    monkeypatch.setattr(pipeline_module, "_build_bundle", flaky_build)

    result = run_intelligence(tmp_config)

    assert result.snapshots >= 1
    assert len(result.failed_tickers) == 1
    assert result.failed_tickers[0].startswith("BROKEN: ValueError")


def test_snapshot_anchors_on_newest_filing_with_metrics_and_keeps_newer_events(tmp_config):
    """A newer attachment without metrics must not hide the issuer or its events."""
    _seed_phase10_inputs(tmp_config)
    newest = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()

    def _add_metricless_notice(conn):
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(documents)")]
        overrides = {
            "document_id": "'doc-tamu-notice'",
            "source_record_id": "'notice-1'",
            "source_file_id": "'notice-1-file'",
            "sha256": "'sha-tamu-notice'",
            "document_type": "'financial_statement'",
            "source_modified_at": "?",
            "updated_at": "?",
            "created_at": "?",
        }
        select = ", ".join(overrides.get(column, column) for column in columns)
        conn.execute(
            f"INSERT INTO documents ({', '.join(columns)}) SELECT {select} FROM documents WHERE document_id = 'doc-tamu-a'",
            (newest, newest, newest),
        )
        conn.execute(
            """
            INSERT INTO events (
                event_id, document_id, ticker, event_type, matched_pattern, severity, rationale, evidence_span_id, detected_at
            ) VALUES ('event-tamu-going-concern', 'doc-tamu-notice', 'TAMU', 'going_concern_warning',
                      'going concern', 'critical', 'Auditor flags material uncertainty.', NULL, ?)
            """,
            (newest,),
        )

    with WriterQueue(tmp_config) as wq:
        wq.submit(_add_metricless_notice)

    result = run_intelligence(tmp_config, ticker="TAMU")

    assert result.snapshots == 1
    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        row = conn.execute(
            "SELECT as_of_document_id, payload_json FROM company_snapshots WHERE ticker = 'TAMU' AND snapshot_kind = 'current'"
        ).fetchone()
    finally:
        conn.close()
    assert row["as_of_document_id"] == "doc-tamu-a"
    top_card = json.loads(row["payload_json"])["top_card"]
    assert top_card["overall_state"] == "critical"


def test_filing_expectations_due_after_the_last_discovery_sync_are_unobserved():
    profile = CompanyProfile(
        ticker="ABCD",
        issuer_name="Issuer ABCD",
        sector=None,
        subsector=None,
        industry=None,
        listing_board=None,
        active_status="listed",
        fiscal_year_end_month=12,
        fiscal_year_end_source="default",
    )
    observed = FilingDocument(
        document_id="doc-fy25",
        document_type="financial_statement",
        as_of="2026-03-28T10:00:00+07:00",
        period_end_dates=("2025-12-31",),
        fiscal_periods=("Annual",),
    )
    rows = {
        row.period_label + ":" + row.period_end_date: row.state
        for row in _build_filing_expectations(
            profile=profile,
            filing_documents=[observed],
            now=date(2026, 10, 17),
            last_checked=date(2026, 4, 18),
        )
    }

    # Nobody looked for these after 18 April, so their absence is not lateness.
    assert rows["Q1 filing:2026-03-31"] == "unobserved"
    assert rows["H1 filing:2026-06-30"] == "unobserved"
    # Not due yet: still tracked normally.
    assert rows["Q3 filing:2026-09-30"] == "due"


def _document(document_id: str, as_of: str) -> CompanyDocument:
    return CompanyDocument(
        document_id=document_id,
        source_name="idx_financial_reports",
        source_record_id=document_id,
        ticker="ABCD",
        title=None,
        document_type="financial_statement",
        version_group_id=None,
        source_modified_at=as_of,
        updated_at=as_of,
        created_at=as_of,
    )


def _metric(document_id: str, name: str, value: float, *, scope: str = "consolidated") -> MetricPoint:
    return MetricPoint(
        metric_id=f"{document_id}-{name}-{scope}",
        document_id=document_id,
        metric_name=name,
        value_normalized=value,
        raw_value_text=str(value),
        currency="IDR",
        unit_scale="units",
        fiscal_year=2025,
        fiscal_period="Annual",
        period_end_date="2025-12-31",
        audit_status=None,
        consolidation_scope=scope,
        evidence_span_id=None,
    )


def test_metric_deltas_keep_segments_apart_and_only_score_comparable_plausible_changes():
    from stock_triage.intelligence.pipeline import _build_deltas, _match_condition
    from stock_triage.intelligence.thesis import LocalThesisCondition

    previous, current = _document("doc-a", "2025-03-01T00:00:00"), _document("doc-b", "2026-03-01T00:00:00")
    comparable = BaselineChoice(kind="revision", document=previous, comparable=True, reason="same period")

    def deltas(baseline, previous_metrics, current_metrics):
        return _build_deltas(
            ticker="ABCD",
            baseline=baseline,
            to_document=current,
            previous_metrics=previous_metrics,
            current_metrics=current_metrics,
            previous_events=[],
            current_events=[],
            previous_rules=[],
            current_rules=[],
            previous_analysis=None,
            current_analysis=None,
        )

    segments = [
        _metric("doc-a", "segment_revenue", 10.0, scope="segment:Shipping"),
        _metric("doc-a", "segment_revenue", 90.0, scope="segment:Mining"),
    ]
    # Same segment values, listed in a different order by the later filing.
    unchanged = [_metric("doc-b", m.metric_name, m.value_normalized, scope=m.consolidation_scope) for m in reversed(segments)]
    assert [d for d in deltas(comparable, segments, unchanged) if d.delta_kind == "metric_change"] == []

    growth = LocalThesisCondition(severity="positive", kind="metric_delta", payload={"metric_name": "revenue", "value": 20})

    def matches(baseline, current_revenue):
        records = deltas(baseline, [_metric("doc-a", "revenue", 100.0)], [_metric("doc-b", "revenue", current_revenue)])
        return _match_condition(growth, baseline=baseline, deltas=records, events=[], triggered_rules=[], review_items=[])

    assert matches(comparable, 130.0) is True
    not_comparable = BaselineChoice(kind="source_prior", document=previous, comparable=False, reason="different period")
    assert matches(not_comparable, 130.0) is None
    # A 200x jump is almost certainly a unit or parsing error, not growth.
    assert matches(comparable, 20_000.0) is None


def test_review_status_follows_the_reviewed_filing():
    from stock_triage.intelligence.pipeline import StoredReviewState, _effective_review_status

    def state(status, document_id="doc-a", snoozed_until=None):
        return StoredReviewState("r1", status, document_id, snoozed_until)

    today = date(2026, 10, 18)
    effective = lambda stored, document_id="doc-a": _effective_review_status(stored, document_id=document_id, as_of=today)

    assert effective(None) == ("open", None)
    assert effective(state("resolved")) == ("resolved", None)
    assert effective(state("resolved"), "doc-b") == ("open", "raised again by a newer filing")
    assert effective(state("snoozed", snoozed_until="2026-10-30")) == ("snoozed", None)
    assert effective(state("snoozed", snoozed_until="2026-10-18")) == ("open", "snooze expired")
    assert effective(state("superseded")) == ("open", "signal raised again")


def test_rederive_reopens_supersedes_and_audits_review_items(tmp_config):
    from stock_triage.intelligence.review import update_review_status

    _seed_phase10_inputs(tmp_config)
    _write_phase12_theses(tmp_config)
    run_intelligence(tmp_config, ticker="TAMU")

    def rows():
        conn = connect(tmp_config.paths.db_path, read_only=True)
        try:
            return {
                row["review_item_id"]: dict(row)
                for row in conn.execute(
                    "SELECT review_item_id, item_type, source_key, status, document_id, snoozed_until "
                    "FROM review_queue_items WHERE ticker = 'TAMU' AND derived_version = ?",
                    (DERIVED_VERSION,),
                )
            }
        finally:
            conn.close()

    by_type = {row["item_type"]: row for row in rows().values()}
    resolved, reviewed_earlier, snoozed = by_type["rule_trigger"], by_type["event_signal"], by_type["thesis_broken"]
    update_review_status(tmp_config, review_item_id=resolved["review_item_id"], status="resolved")
    update_review_status(tmp_config, review_item_id=reviewed_earlier["review_item_id"], status="resolved")
    update_review_status(tmp_config, review_item_id=snoozed["review_item_id"], status="snoozed", snooze_days=3)
    assert rows()[snoozed["review_item_id"]]["snoozed_until"] is not None

    with WriterQueue(tmp_config) as wq:
        wq.submit(
            lambda conn: conn.execute(
                "UPDATE review_queue_items SET document_id = 'doc-tamu-b' WHERE review_item_id = ?",
                (reviewed_earlier["review_item_id"],),
            )
        )
        wq.submit(
            lambda conn: conn.execute(
                """
                INSERT INTO review_queue_items (
                    review_item_id, item_type, ticker, document_id, source_key, priority, status,
                    rationale, payload_json, derived_version, created_at, updated_at
                ) VALUES ('stale-item', 'rule_trigger', 'TAMU', 'doc-tamu-a', 'no-longer-raised', 'medium', 'open',
                          'An old rule outcome.', '{}', ?, '2026-01-01', '2026-01-01')
                """,
                (DERIVED_VERSION,),
            )
        )

    run_intelligence(tmp_config, ticker="TAMU", as_of=date.today() + timedelta(days=5))
    after = rows()

    assert after[resolved["review_item_id"]]["status"] == "resolved"
    assert after[reviewed_earlier["review_item_id"]]["status"] == "open"
    assert after[snoozed["review_item_id"]]["status"] == "open"
    assert after["stale-item"]["status"] == "superseded"

    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        audit = {
            (row["review_item_id"], row["action"], row["note"])
            for row in conn.execute("SELECT review_item_id, action, note FROM review_audit_log WHERE action LIKE 'auto_%'")
        }
    finally:
        conn.close()
    assert (reviewed_earlier["review_item_id"], "auto_reopen", "raised again by a newer filing") in audit
    assert (snoozed["review_item_id"], "auto_reopen", "snooze expired") in audit
    assert ("stale-item", "auto_supersede", "no longer raised by the latest filings") in audit


def test_group_review_updates_are_all_or_nothing(tmp_config):
    import pytest

    from stock_triage.intelligence.review import update_review_statuses

    _seed_phase10_inputs(tmp_config)
    _write_phase12_theses(tmp_config)
    run_intelligence(tmp_config, ticker="TAMU")
    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        first_id = conn.execute("SELECT review_item_id FROM review_queue_items WHERE status = 'open' LIMIT 1").fetchone()[0]
    finally:
        conn.close()

    with pytest.raises(ValueError):
        update_review_statuses(tmp_config, review_item_ids=[first_id, "missing-item"], status="resolved")

    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        status = conn.execute("SELECT status FROM review_queue_items WHERE review_item_id = ?", (first_id,)).fetchone()[0]
    finally:
        conn.close()
    assert status == "open"


def test_thesis_ids_survive_reordering_and_removed_theses_retire(tmp_path):
    import sqlite3

    from stock_triage.intelligence.thesis import load_local_theses, sync_local_theses

    def write(names):
        path = tmp_path / "theses.yaml"
        path.write_text(
            "theses:\n"
            + "".join(f"  - ticker: TAMU\n    thesis_name: {name}\n    thesis_text: {name}.\n" for name in names),
            encoding="utf-8",
        )
        return load_local_theses(path)

    first = {thesis.thesis_name: thesis.thesis_id for thesis in write(["Debt cleanup", "Margin repair"])}
    reordered = {thesis.thesis_name: thesis.thesis_id for thesis in write(["Margin repair", "Debt cleanup"])}
    assert first == reordered

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    run_migrations(conn)
    sync_local_theses(conn, theses=write(["Debt cleanup", "Margin repair"]))
    sync_local_theses(conn, theses=write(["Margin repair"]))
    statuses = {row["thesis_name"]: row["status"] for row in conn.execute("SELECT thesis_name, status FROM thesis_definitions")}
    assert statuses == {"Debt cleanup": "retired", "Margin repair": "active"}


def test_rederive_keeps_contradiction_history_from_earlier_filings(tmp_config):
    _seed_phase10_inputs(tmp_config)
    _write_phase12_theses(tmp_config)
    run_intelligence(tmp_config, ticker="TAMU")
    with WriterQueue(tmp_config) as wq:
        wq.submit(
            lambda conn: conn.execute(
                """
                INSERT INTO contradiction_rows (
                    contradiction_id, ticker, document_id, contradiction_class, severity, summary,
                    source_claim_id, evidence_span_id, payload_json, derived_version, created_at, updated_at
                ) VALUES ('older-contradiction', 'TAMU', 'doc-tamu-b', 'claim_vs_outcome', 'critical',
                          'Earlier filing contradiction.', NULL, NULL, '{}', ?, '2025-01-01', '2025-01-01')
                """,
                (DERIVED_VERSION,),
            )
        )

    rederive_intelligence(tmp_config, ticker="TAMU")

    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        kept = conn.execute("SELECT 1 FROM contradiction_rows WHERE contradiction_id = 'older-contradiction'").fetchone()
    finally:
        conn.close()
    assert kept is not None


def test_persistence_ageing_compares_naive_and_offset_idx_timestamps():
    """Financial reports are stored naive (WIB), disclosures with +07:00."""
    rows = _build_persistent_risk_signals(
        [
            FocusHistoryRow(
                document_id="doc-notice",
                document_type="disclosure",
                as_of="2026-04-08T22:34:05+07:00",
                focus_keys=("legal_regulatory",),
            ),
            FocusHistoryRow(
                document_id="doc-fs",
                document_type="financial_statement",
                as_of="2025-04-30T14:45:15.073",
                focus_keys=("legal_regulatory",),
            ),
        ]
    )

    assert rows[0]["gap_days"] == 343
    assert rows[0]["persistence_state"] == "reappeared"


def test_a_newer_disclosure_reopens_a_resolved_event_and_points_at_it(tmp_config):
    from stock_triage.intelligence.review import update_review_status

    _seed_phase10_inputs(tmp_config)
    _write_phase12_theses(tmp_config)
    run_intelligence(tmp_config, ticker="TAMU")

    def event_item():
        conn = connect(tmp_config.paths.db_path, read_only=True)
        try:
            return dict(
                conn.execute(
                    "SELECT review_item_id, status, document_id FROM review_queue_items "
                    "WHERE ticker = 'TAMU' AND item_type = 'event_signal' AND derived_version = ?",
                    (DERIVED_VERSION,),
                ).fetchone()
            )
        finally:
            conn.close()

    item = event_item()
    update_review_status(tmp_config, review_item_id=item["review_item_id"], status="resolved")
    newest = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()

    def add_notice_raising_the_same_event(conn):
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(documents)")]
        overrides = {
            "document_id": "'doc-tamu-notice'",
            "source_record_id": "'notice-2'",
            "source_file_id": "'notice-2-file'",
            "sha256": "'sha-tamu-notice-2'",
            "document_type": "'disclosure'",
            "source_modified_at": "?",
            "updated_at": "?",
            "created_at": "?",
        }
        select = ", ".join(overrides.get(column, column) for column in columns)
        conn.execute(
            f"INSERT INTO documents ({', '.join(columns)}) SELECT {select} FROM documents WHERE document_id = 'doc-tamu-a'",
            (newest, newest, newest),
        )
        event_type = conn.execute(
            "SELECT event_type FROM events WHERE document_id = 'doc-tamu-a' AND severity IN ('warning', 'critical') LIMIT 1"
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO events (event_id, document_id, ticker, event_type, matched_pattern, severity, rationale,
                                evidence_span_id, detected_at)
            VALUES ('event-tamu-notice', 'doc-tamu-notice', 'TAMU', ?, ?, 'warning', 'Raised again.', NULL, ?)
            """,
            (event_type, event_type, newest),
        )

    with WriterQueue(tmp_config) as wq:
        wq.submit(add_notice_raising_the_same_event)
    run_intelligence(tmp_config, ticker="TAMU")

    reopened = event_item()
    assert reopened["review_item_id"] == item["review_item_id"]
    assert (reopened["status"], reopened["document_id"]) == ("open", "doc-tamu-notice")


def test_claims_made_after_the_anchor_filing_are_not_yet_testable():
    from stock_triage.intelligence.claims import ClaimRecord, _evaluate_claim

    claim = ClaimRecord(
        claim_id="claim-new",
        ticker="TAMU",
        source_document_id="doc-notice",
        claim_text="Proceeds will be allocated to fleet renewal.",
        claim_type="funding_use",
        claim_topic="capital_allocation",
        claim_subject=None,
        claim_date=None,
        claim_time_horizon=None,
        claim_confidence=0.8,
        evidence_span_id=None,
        status="open",
        payload_json="{}",
    )

    status, _, _, _ = _evaluate_claim(
        None, claim=claim, latest_document_id="doc-anchor", newer_document_ids=frozenset({"doc-notice"})
    )

    assert status == "Not Yet Testable"


def test_undoing_a_snooze_reopens_without_a_leftover_snooze_date(tmp_config):
    from stock_triage.intelligence.review import update_review_status

    _seed_phase10_inputs(tmp_config)
    _write_phase12_theses(tmp_config)
    run_intelligence(tmp_config, ticker="TAMU")
    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        item_id = conn.execute("SELECT review_item_id FROM review_queue_items WHERE status = 'open' LIMIT 1").fetchone()[0]
    finally:
        conn.close()

    update_review_status(tmp_config, review_item_id=item_id, status="snoozed")
    # The dashboard only lists open items, so Undo always restores "open".
    update_review_status(tmp_config, review_item_id=item_id, status="open")

    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        row = conn.execute("SELECT status, snoozed_until FROM review_queue_items WHERE review_item_id = ?", (item_id,)).fetchone()
    finally:
        conn.close()
    assert (row["status"], row["snoozed_until"]) == ("open", None)
