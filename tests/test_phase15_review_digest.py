from __future__ import annotations

import json

from stock_triage.alerts import run_alerts
from stock_triage.db import WriterQueue, connect, configure_pragmas, run_migrations
from stock_triage.intelligence import run_intelligence
from stock_triage.intelligence.review import (
    _group_review_rows,
    load_intelligence_kpis,
    load_review_queue,
    run_daily_digest,
    update_review_status,
)

from .test_intelligence_pipeline import _seed_phase10_inputs, _write_phase12_theses


def test_phase15_digest_review_and_kpis(tmp_config):
    _seed_phase10_inputs(tmp_config)
    _write_phase12_theses(tmp_config)
    tmp_config.alerts.watchlist = ["TAMU"]

    run_intelligence(tmp_config, ticker="TAMU")
    alert_result = run_alerts(tmp_config)
    digest_result = run_daily_digest(tmp_config, scope="watchlist", limit=10)

    assert alert_result.emitted >= 1
    assert digest_result.scope == "watchlist"
    assert digest_result.companies == 1
    assert digest_result.review_items >= 1

    report = load_review_queue(tmp_config, status="open", limit=20)
    assert report.rows
    assert any((row.get("group_count") or 0) > 1 for row in report.rows)
    first = report.rows[0]
    update = update_review_status(tmp_config, review_item_id=first["review_item_id"], status="resolved", note="verified")
    assert update.previous_status == "open"
    assert update.current_status == "resolved"

    kpis = load_intelligence_kpis(tmp_config)
    assert kpis["snapshot_count"] == 1
    assert kpis["open_review_items"] >= 0
    assert "critical" in kpis["overall_state_counts"]
    # PRD v3 §18 derived KPI fields
    assert kpis["snapshot_freshness_pct"] is not None
    assert 0 <= kpis["snapshot_freshness_pct"] <= 100
    assert kpis["snapshot_completeness_pct"] is not None
    assert kpis["digest_validity_pct"] is not None
    assert kpis["kpi_targets"]["snapshot_freshness_pct"] == 95.0
    assert kpis["kpi_targets"]["snapshot_completeness_pct"] == 90.0
    assert kpis["kpi_targets"]["digest_validity_pct"] == 100.0
    assert kpis["kpi_targets"]["delta_precision_pct"] == 95.0
    assert kpis["kpi_targets"]["thesis_correctness_pct"] == 98.0

    rconn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        configure_pragmas(rconn, tmp_config)
        digest_row = rconn.execute(
            """
            SELECT payload_json
            FROM daily_digests
            WHERE digest_id = ?
            """,
            (digest_result.digest_id,),
        ).fetchone()
        assert digest_row is not None
        digest_payload = json.loads(digest_row["payload_json"])
        assert digest_payload["company_cards"][0]["ticker"] == "TAMU"
        assert digest_payload["review_focus"][0]["ticker"] == "TAMU"

        updated_row = rconn.execute(
            """
            SELECT status
            FROM review_queue_items
            WHERE review_item_id = ?
            """,
            (first["review_item_id"],),
        ).fetchone()
        assert updated_row["status"] == "resolved"

        audit_row = rconn.execute(
            """
            SELECT action, from_status, to_status, note
            FROM review_audit_log
            WHERE review_item_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (first["review_item_id"],),
        ).fetchone()
        assert audit_row["action"] == "status_update"
        assert audit_row["from_status"] == "open"
        assert audit_row["to_status"] == "resolved"
        assert audit_row["note"] == "verified"
    finally:
        rconn.close()


def test_phase15_migration_applies(tmp_config):
    with WriterQueue(tmp_config) as wq:
        applied = wq.submit(run_migrations)
    assert applied == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]


def test_group_review_rows_collapse_persistence_into_issue_family():
    rows = _group_review_rows(
        [
            {
                "review_item_id": "r1",
                "item_type": "event_signal",
                "ticker": "TAMU",
                "document_id": "doc-1",
                "source_key": "event:lawsuit",
                "priority": "high",
                "status": "open",
                "rationale": "Active lawsuit needs review.",
                "payload": {"event_type": "lawsuit"},
                "created_at": "2026-04-18T00:00:00+00:00",
                "updated_at": "2026-04-18T00:00:00+00:00",
                "source_name": "idx_disclosures",
                "local_path": None,
                "as_of_date": "2026-04-18T00:00:00+00:00",
                "decision_state": "Review manually",
                "overall_state": "critical",
            },
            {
                "review_item_id": "r2",
                "item_type": "persistence_focus",
                "ticker": "TAMU",
                "document_id": "doc-1",
                "source_key": "persist:legal",
                "priority": "high",
                "status": "open",
                "rationale": "Legal / Regulatory issues repeated across recent analyzed filings.",
                "payload": {"focus_key": "legal_regulatory", "persistence_state": "active"},
                "created_at": "2026-04-18T00:00:00+00:00",
                "updated_at": "2026-04-18T00:00:00+00:00",
                "source_name": "derived",
                "local_path": None,
                "as_of_date": "2026-04-18T00:00:00+00:00",
                "decision_state": "Review manually",
                "overall_state": "critical",
            },
        ],
        limit=10,
    )

    assert len(rows) == 1
    assert rows[0]["issue_family"] == "legal_regulatory"
    assert rows[0]["group_count"] == 2
    assert rows[0]["ranking_inputs"]["persistence_count"] == 1
