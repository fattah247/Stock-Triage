"""Read-side dashboard snapshot queries."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from ..canary import load_canary_status
from ..config import Config
from ..coverage import default_coverage_range, load_coverage_plan, load_coverage_plan_audit, load_coverage_status_from_conn
from ..db import WriterQueue, connect, configure_pragmas, preferred_metric_sql, run_migrations
from ..fiscal_year import resolve_fiscal_year_end_month
from ..hardening import load_backlog
from ..intelligence.pipeline import DERIVED_VERSION
from ..market.catalysts import load_upcoming_catalysts
from ..market.exposure import load_latest_exposure
from ..market.signals import load_market_state
from ..intelligence.review import (
    load_intelligence_kpis,
    load_nuance_audit,
    load_intelligence_observability,
    load_latest_digests,
    load_latest_golden_runs,
    load_review_queue,
)


@dataclass(frozen=True)
class DashboardSnapshot:
    issuers: list[dict[str, object]]
    market_monitor: dict[str, object]
    source_sync_rows: list[dict[str, object]]
    documents: list[dict[str, object]]
    metrics: list[dict[str, object]]
    findings: list[dict[str, object]]
    revisions: list[dict[str, object]]
    company_snapshots: list[dict[str, object]]
    sector_rollups: list[dict[str, object]]
    inbox_rows: list[dict[str, object]]
    digest_rows: list[dict[str, object]]
    review_rows: list[dict[str, object]]
    intelligence_kpis: dict[str, object]
    backlog_rows: list[dict[str, object]]
    canary_rows: list[dict[str, object]]


def load_dashboard_snapshot(
    cfg: Config,
    *,
    document_limit: int = 50,
    finding_limit: int = 50,
) -> DashboardSnapshot:
    cfg.ensure_dirs()

    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)

    conn = connect(cfg.paths.db_path, read_only=True)
    try:
        configure_pragmas(conn, cfg)
        snapshot = DashboardSnapshot(
            issuers=_load_issuers(conn),
            market_monitor=_load_market_monitor(conn, cfg),
            source_sync_rows=_load_source_sync_rows(conn),
            documents=_load_latest_documents(conn, limit=document_limit),
            metrics=_load_metric_snapshot(conn),
            findings=_load_findings(conn, limit=finding_limit),
            revisions=_load_revision_history(conn, limit=50),
            company_snapshots=_load_company_snapshots(conn),
            sector_rollups=_load_sector_rollups(conn),
            inbox_rows=_load_inbox_rows(conn, limit=60),
            digest_rows=[],
            review_rows=[],
            intelligence_kpis={},
            backlog_rows=[],
            canary_rows=[],
        )
    finally:
        conn.close()

    backlog = load_backlog(cfg)
    canary = load_canary_status(cfg)
    review = load_review_queue(cfg, status="open", limit=50)
    digests = load_latest_digests(cfg, limit=5)
    kpis = load_intelligence_kpis(cfg)
    return DashboardSnapshot(
        issuers=snapshot.issuers,
        market_monitor=snapshot.market_monitor,
        source_sync_rows=snapshot.source_sync_rows,
        documents=snapshot.documents,
        metrics=snapshot.metrics,
        findings=snapshot.findings,
        revisions=snapshot.revisions,
        company_snapshots=snapshot.company_snapshots,
        sector_rollups=snapshot.sector_rollups,
        inbox_rows=snapshot.inbox_rows,
        digest_rows=digests,
        review_rows=review.rows,
        intelligence_kpis=kpis,
        backlog_rows=[row.__dict__ for row in backlog.rows],
        canary_rows=[row.__dict__ for row in canary.rows],
    )


def load_snapshot_lite(cfg: Config) -> dict[str, object]:
    """Lightweight snapshot: issuers + KPIs only, suitable for frequent polling."""
    cfg.ensure_dirs()

    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)

    conn = connect(cfg.paths.db_path, read_only=True)
    try:
        configure_pragmas(conn, cfg)
        issuers = _load_issuers(conn)
        market_monitor = _load_market_monitor(conn, cfg)
        source_sync_rows = _load_source_sync_rows(conn)
    finally:
        conn.close()

    kpis = load_intelligence_kpis(cfg)
    return {
        "issuers": issuers,
        "market_monitor": market_monitor,
        "source_sync_rows": source_sync_rows,
        "intelligence_kpis": kpis,
    }


def load_company_detail(cfg: Config, ticker: str) -> dict[str, object]:
    """Load documents, metrics, findings, revisions, and snapshot for one ticker."""
    cfg.ensure_dirs()

    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)

    conn = connect(cfg.paths.db_path, read_only=True)
    try:
        configure_pragmas(conn, cfg)
        ticker_upper = ticker.upper()
        documents = _load_documents_for_ticker(conn, ticker=ticker_upper, limit=200)
        metrics = _load_metrics_for_ticker(conn, ticker=ticker_upper)
        findings = _load_findings_for_ticker(conn, ticker=ticker_upper, limit=100)
        revisions = _load_revision_history_for_ticker(conn, ticker=ticker_upper, limit=50)
        company_snapshot = _load_company_snapshot_for_ticker(conn, ticker=ticker_upper)
        sector_rollup = _load_sector_rollup_for_ticker(conn, ticker=ticker_upper, company_snapshot=company_snapshot)
        exposure = load_latest_exposure(conn, ticker=ticker_upper)
    finally:
        conn.close()

    return {
        "ticker": ticker_upper,
        "documents": documents,
        "metrics": metrics,
        "findings": findings,
        "revisions": revisions,
        "company_snapshot": company_snapshot,
        "sector_rollup": sector_rollup,
        "exposure": exposure,
    }


def load_section_detail(cfg: Config, section: str) -> dict[str, object]:
    """Load a single detail section: inbox, digest, review, or ops."""
    cfg.ensure_dirs()

    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)

    if section == "inbox":
        conn = connect(cfg.paths.db_path, read_only=True)
        try:
            configure_pragmas(conn, cfg)
            return {"inbox_rows": _load_inbox_rows(conn, limit=60)}
        finally:
            conn.close()

    if section == "digest":
        digests = load_latest_digests(cfg, limit=5)
        return {"digest_rows": digests}

    if section == "review":
        review = load_review_queue(cfg, status="open", limit=50)
        return {"review_rows": review.rows}

    if section == "ops":
        backlog = load_backlog(cfg)
        canary = load_canary_status(cfg)
        conn = connect(cfg.paths.db_path, read_only=True)
        try:
            configure_pragmas(conn, cfg)
            coverage = load_coverage_status_from_conn(conn, cfg, scope="all", limit=10)
        finally:
            conn.close()
        return {
            "backlog_rows": [row.__dict__ for row in backlog.rows],
            "canary_rows": [row.__dict__ for row in canary.rows],
            "source_sync_rows": _load_source_sync_rows_from_cfg(cfg),
            "observability": load_intelligence_observability(cfg),
            "nuance_audit": load_nuance_audit(cfg, limit=8),
            "coverage_plan": load_coverage_plan(cfg, scope="watchlist", limit=5, materialize=False),
            "coverage_plan_audit": load_coverage_plan_audit(cfg, limit=8),
            "golden_rows": load_latest_golden_runs(cfg, limit=5),
            "coverage_status": coverage,
        }

    return {}


def _load_issuers(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT
            c.ticker,
            c.issuer_name,
            c.sector,
            c.subsector,
            c.active_status,
            c.last_seen_at,
            COUNT(d.document_id) AS document_count,
            -- When the filing was published, not when a pipeline stage last touched it.
            MAX(COALESCE(d.source_modified_at, d.retrieved_at)) AS latest_filing_at
        FROM companies c
        LEFT JOIN documents d ON d.ticker = c.ticker
        GROUP BY c.ticker, c.issuer_name, c.sector, c.subsector, c.active_status, c.last_seen_at
        -- Issuers with filings first, newest filing first; discovery recency only orders the rest.
        ORDER BY latest_filing_at IS NULL, latest_filing_at DESC, c.last_seen_at DESC, c.ticker ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _load_latest_documents(conn: sqlite3.Connection, *, limit: int) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT
            d.document_id,
            d.source_name,
            d.source_record_id,
            d.ticker,
            d.document_type,
            d.process_state,
            d.retrieved_at,
            d.updated_at,
            COALESCE(d.source_modified_at, d.retrieved_at) AS filed_at,
            dc.title,
            d.local_path,
            d.original_url,
            d.version_group_id,
            COALESCE((
                SELECT COUNT(*)
                FROM rule_runs rr
                WHERE rr.document_id = d.document_id AND rr.triggered = 1
            ), 0) AS triggered_rules
        FROM documents d
        LEFT JOIN document_candidates dc
          ON dc.source_name = d.source_name
         AND dc.source_record_id = d.source_record_id
        ORDER BY d.updated_at DESC, d.document_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def _load_documents_for_ticker(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    limit: int,
) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT
            d.document_id,
            d.source_name,
            d.source_record_id,
            d.ticker,
            d.document_type,
            d.process_state,
            d.retrieved_at,
            d.updated_at,
            COALESCE(d.source_modified_at, d.retrieved_at) AS filed_at,
            dc.title,
            d.local_path,
            d.original_url,
            d.version_group_id,
            COALESCE((
                SELECT COUNT(*)
                FROM rule_runs rr
                WHERE rr.document_id = d.document_id AND rr.triggered = 1
            ), 0) AS triggered_rules
        FROM documents d
        LEFT JOIN document_candidates dc
          ON dc.source_name = d.source_name
         AND dc.source_record_id = d.source_record_id
        WHERE d.ticker = ?
        ORDER BY filed_at DESC, d.updated_at DESC, d.document_id DESC
        LIMIT ?
        """,
        (ticker, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def _load_market_risk(conn: sqlite3.Connection, cfg: Config, *, today: date) -> dict[str, object]:
    """Home's Market State strip: the state, the next risk dates, and the most exposed names."""
    watchlist = {ticker.upper() for ticker in cfg.alerts.watchlist}
    upcoming = load_upcoming_catalysts(conn, today=today, days=30, watchlist=watchlist)[:4]
    where = f"AND es.ticker IN ({','.join('?' for _ in watchlist)})" if watchlist else ""
    exposed = conn.execute(
        f"""
        SELECT es.ticker, es.score, es.components_json
        FROM exposure_scores es
        WHERE es.as_of_date = (SELECT MAX(as_of_date) FROM exposure_scores)
          {where}
        ORDER BY es.score DESC, es.ticker ASC
        LIMIT 3
        """,
        tuple(sorted(watchlist)),
    ).fetchall()
    from ..market.discovery import active_themes
    from ..market.forecast import load_crash_risk
    from ..market.themes import ThemeFileError, load_themes

    risk = load_crash_risk(conn, history=1)
    try:
        themes = active_themes(
            conn, themes=load_themes(conn, cfg.market.risk_themes_path), as_of=today, min_items=cfg.market.theme_active_min_items
        )
    except ThemeFileError:
        themes = []
    findings_open = conn.execute("SELECT COUNT(*) FROM emerging_findings WHERE status = 'open'").fetchone()[0]
    return {
        "state": load_market_state(conn),
        "crash_risk": None
        if risk is None
        else {
            "trade_date": risk["trade_date"],
            "trustworthy": risk["trustworthy"],
            # The number is withheld until the estimate beats its base rate.
            "probability": risk["probability"] if risk["trustworthy"] else None,
            "base_rate": risk["base_rate"],
            "analog": (risk["analogs"] or [None])[0],
        },
        "active_themes": [
            {"title": item["title"], "severity": item["severity"], "items": item["items"]} for item in themes[:3]
        ],
        "findings_open": findings_open,
        "upcoming": [
            {"date": item.catalyst_date.isoformat(), "kind": item.kind, "title": item.title, "severity": item.severity}
            for item in upcoming
        ],
        "exposure_scope": "watchlist" if watchlist else "all",
        "most_exposed": [
            {
                "ticker": row["ticker"],
                "score": row["score"],
                "top_reason": next(
                    (
                        item["reason"]
                        for item in sorted(json.loads(row["components_json"]).get("components", []), key=lambda item: -item["points"])
                        if item["points"]
                    ),
                    None,
                ),
            }
            for row in exposed
        ],
    }


def _load_market_monitor(conn: sqlite3.Connection, cfg: Config) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    week_start = now - timedelta(days=7)
    coverage_from, coverage_to = default_coverage_range(now.date())
    coverage_status = load_coverage_status_from_conn(
        conn,
        cfg,
        date_from=coverage_from,
        date_to=coverage_to,
        scope="all",
        limit=10,
    )
    source_syncs = {row["source_name"]: row for row in _load_source_sync_rows(conn)}
    disclosure_sync = _parse_iso_datetime(source_syncs.get("idx_disclosures", {}).get("updated_at"))
    disclosure_window_start = disclosure_sync or week_start

    new_disclosures_since_sync = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM documents
        WHERE source_name = 'idx_disclosures'
          AND COALESCE(retrieved_at, updated_at, created_at) >= ?
        """,
        (disclosure_window_start.isoformat(),),
    ).fetchone()["count"]

    low_signal_event_types = {"shareholder_meeting_summary", "dividend_announcement"}
    material_event_filings_today = conn.execute(
        f"""
        SELECT COUNT(DISTINCT e.document_id) AS count
        FROM events e
        WHERE e.detected_at >= ?
          AND e.event_type NOT IN ({",".join("?" for _ in low_signal_event_types)})
        """,
        (disclosure_window_start.isoformat(), *sorted(low_signal_event_types)),
    ).fetchone()["count"]

    recent_event_rows = conn.execute(
        """
        SELECT event_type, COUNT(*) AS count
        FROM events
        WHERE detected_at >= ?
        GROUP BY event_type
        ORDER BY count DESC, event_type ASC
        """,
        (week_start.isoformat(),),
    ).fetchall()
    event_counts = {row["event_type"]: row["count"] for row in recent_event_rows}

    late_periodic_rows = _load_late_periodic_rows(conn, cfg, now)

    sector_rows = conn.execute(
        """
        SELECT
            COALESCE(c.sector, 'unknown') AS sector,
            d.document_id,
            d.updated_at,
            e.event_type,
            e.severity,
            rr.rule_name,
            rr.severity AS rule_severity
        FROM companies c
        LEFT JOIN documents d
          ON d.ticker = c.ticker
         AND d.updated_at >= ?
        LEFT JOIN events e
          ON e.document_id = d.document_id
         AND e.detected_at >= ?
        LEFT JOIN rule_runs rr
          ON rr.document_id = d.document_id
         AND rr.triggered = 1
         AND rr.evaluated_at >= ?
        ORDER BY sector ASC
        """,
        (week_start.isoformat(), week_start.isoformat(), week_start.isoformat()),
    ).fetchall()
    sector_map: dict[str, dict[str, object]] = {}
    for row in sector_rows:
        sector = str(row["sector"] or "unknown")
        entry = sector_map.setdefault(
            sector,
            {
                "sector": sector,
                "disclosures_7d": set(),
                "material_events_7d": set(),
                "warning_signals_7d": 0,
                "critical_signals_7d": 0,
            },
        )
        if row["document_id"]:
            entry["disclosures_7d"].add(row["document_id"])
        if row["event_type"] and row["event_type"] not in low_signal_event_types:
            entry["material_events_7d"].add(row["document_id"])
            if row["severity"] == "critical":
                entry["critical_signals_7d"] += 1
            elif row["severity"] == "warning":
                entry["warning_signals_7d"] += 1
        if row["rule_name"]:
            if row["rule_severity"] == "critical":
                entry["critical_signals_7d"] += 1
            elif row["rule_severity"] == "warning":
                entry["warning_signals_7d"] += 1
    sector_heatmap = sorted(
        (
            {
                "sector": sector,
                "disclosures_7d": len(payload["disclosures_7d"]),
                "material_events_7d": len(payload["material_events_7d"]),
                "warning_signals_7d": payload["warning_signals_7d"],
                "critical_signals_7d": payload["critical_signals_7d"],
                "signal_count_7d": payload["warning_signals_7d"] + payload["critical_signals_7d"],
            }
            for sector, payload in sector_map.items()
        ),
        key=lambda item: (
            -int(item["signal_count_7d"]),
            -int(item["critical_signals_7d"]),
            -int(item["material_events_7d"]),
            -int(item["disclosures_7d"]),
            str(item["sector"]),
        ),
    )[:10]

    review_counts = {
        row["ticker"]: row["count"]
        for row in conn.execute(
            """
            SELECT ticker, COUNT(*) AS count
            FROM review_queue_items
            WHERE status = 'open'
              AND derived_version = ?
            GROUP BY ticker
            """,
            (DERIVED_VERSION,),
        ).fetchall()
    }
    snapshot_rows = conn.execute(
        """
        SELECT ticker, as_of_date, payload_json, updated_at
        FROM company_snapshots
        WHERE snapshot_kind = 'current'
          AND derived_version = ?
        ORDER BY updated_at DESC, ticker ASC
        """
        ,
        (DERIVED_VERSION,),
    ).fetchall()
    highest_risk_changes = [
        {
            "ticker": row["ticker"],
            "issuer_name": row["issuer_name"],
            "decision_state": row["decision_state"],
            "overall_state": row["overall_state"],
            "risk_score": 100 if row["priority"] == "high" else 60 if row["priority"] == "medium" else 20,
            "reasons": [row["rationale"]] if row["rationale"] else [],
            "as_of_date": row["updated_at"],
            "item_type": row["item_type"],
            "priority": row["priority"],
        }
        for row in conn.execute(
            """
            SELECT
                rqi.ticker,
                c.issuer_name,
                rqi.item_type,
                rqi.priority,
                rqi.rationale,
                rqi.updated_at,
                json_extract(cs.payload_json, '$.top_card.decision_state') AS decision_state,
                json_extract(cs.payload_json, '$.top_card.overall_state') AS overall_state
            FROM review_queue_items rqi
            LEFT JOIN companies c ON c.ticker = rqi.ticker
            LEFT JOIN company_snapshots cs
              ON cs.ticker = rqi.ticker
             AND cs.snapshot_kind = 'current'
             AND cs.derived_version = ?
            WHERE rqi.status = 'open'
              AND rqi.derived_version = ?
              AND rqi.updated_at >= ?
            ORDER BY
              CASE rqi.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
              rqi.updated_at DESC,
              rqi.review_item_id ASC
            LIMIT 10
            """,
            (DERIVED_VERSION, DERIVED_VERSION, week_start.isoformat()),
        ).fetchall()
    ]
    stock_pick_candidates: list[dict[str, object]] = []
    avoid_candidates: list[dict[str, object]] = []
    stock_pick_fallbacks: list[dict[str, object]] = []
    avoid_fallbacks: list[dict[str, object]] = []
    most_improved: list[dict[str, object]] = []
    for row in snapshot_rows:
        payload = json.loads(row["payload_json"] or "{}")
        top_card = payload.get("top_card", {})
        decision_panel = payload.get("decision_panel", {})
        recommendation = decision_panel.get("recommendation") or payload.get("recommendation") or {}
        ranking_inputs = recommendation.get("ranking_inputs") or {}
        ticker = str(row["ticker"])
        peer_anomalies = len(payload.get("peer_comparison", {}).get("anomalies") or [])
        open_reviews = review_counts.get(ticker, 0)
        negative_factors = payload.get("top_card", {}).get("negative_factors") or []
        positive_factors = payload.get("top_card", {}).get("positive_factors") or []
        risk_score = (
            (120 if top_card.get("decision_state") == "Avoid" else 0)
            + (95 if top_card.get("decision_state") == "Review manually" else 0)
            + (40 if top_card.get("overall_state") == "critical" else 0)
            + (25 if top_card.get("thesis_state") == "broken" else 0)
            + (open_reviews * 8)
            + (peer_anomalies * 5)
            + (len(negative_factors) * 4)
        )
        improvement_score = (
            (60 if top_card.get("decision_state") == "Buy" else 0)
            + (20 if top_card.get("thesis_state") == "improving" else 0)
            + (8 * len(positive_factors))
            - (10 if top_card.get("overall_state") in {"critical", "review_required"} else 0)
        )
        candidate_row = {
            "ticker": ticker,
            "issuer_name": payload.get("issuer_name"),
            "decision_state": top_card.get("decision_state"),
            "confidence": decision_panel.get("confidence") or top_card.get("stance_confidence") or top_card.get("confidence"),
            "score": recommendation.get("score", 0),
            "reasons": recommendation.get("reasons") or [],
            "decision_constraints": recommendation.get("decision_constraints") or [],
            "evidence_ref_count": recommendation.get("evidence_ref_count", 0),
            "as_of_document_id": recommendation.get("as_of_document_id") or row["ticker"],
            "as_of_date": row["as_of_date"] or row["updated_at"],
            "ranking_inputs": ranking_inputs,
            "qualification": "strict",
            "qualification_label": "Qualified",
            "blockers": [],
        }
        if recommendation.get("bucket") == "stock_pick_candidate":
            stock_pick_candidates.append(candidate_row)
        elif recommendation.get("bucket") == "avoid_candidate":
            avoid_candidates.append(candidate_row)
        else:
            pick_fallback = _build_recommendation_fallback_row(
                candidate_row,
                top_card=top_card,
                recommendation=recommendation,
                target="pick",
            )
            if pick_fallback is not None:
                stock_pick_fallbacks.append(pick_fallback)
            avoid_fallback = _build_recommendation_fallback_row(
                candidate_row,
                top_card=top_card,
                recommendation=recommendation,
                target="avoid",
            )
            if avoid_fallback is not None:
                avoid_fallbacks.append(avoid_fallback)
        if improvement_score > 0 and top_card.get("overall_state") != "critical":
            most_improved.append(
                {
                    "ticker": ticker,
                    "issuer_name": payload.get("issuer_name"),
                    "decision_state": top_card.get("decision_state"),
                    "improvement_score": improvement_score,
                    "positive_factors": positive_factors[:3],
                    "as_of_date": row["as_of_date"] or row["updated_at"],
                }
            )

    major_event_counts = {
        "auditor_changes_7d": _sum_event_counts(event_counts, {"auditor_change"}),
        "management_changes_7d": _sum_event_counts(
            event_counts,
            {"governance_appointment", "director_change", "commissioner_change", "board_change", "management_change"},
        ),
        "debt_restructurings_7d": _sum_event_counts(event_counts, {"debt_refinancing", "debt_restructuring"}),
        "rights_issues_7d": _sum_event_counts(
            event_counts,
            {"rights_issue", "private_placement", "capital_raise", "share_issue", "warrant_issue"},
        ),
        "buybacks_7d": _sum_event_counts(event_counts, {"buyback", "share_buyback", "treasury_share_change"}),
        "lawsuits_regulatory_7d": _sum_event_counts(
            event_counts,
            {"lawsuit", "regulatory_action", "regulator_supervision", "special_supervision", "business_restriction"},
        ),
        "contract_changes_7d": _sum_event_counts(
            event_counts,
            {"major_contract_win", "major_contract_loss", "contract_change"},
        ),
    }
    stock_pick_display_rows = _combine_recommendation_rows(
        strict_rows=stock_pick_candidates,
        fallback_rows=stock_pick_fallbacks,
    )
    avoid_display_rows = _combine_recommendation_rows(
        strict_rows=avoid_candidates,
        fallback_rows=avoid_fallbacks,
    )

    return {
        "as_of": now.isoformat(),
        "market_risk": _load_market_risk(conn, cfg, today=now.date()),
        "counts": {
            "new_disclosures_since_sync": new_disclosures_since_sync,
            "material_event_filings_since_sync": material_event_filings_today,
            "late_periodic_filings": len(late_periodic_rows),
            "stock_pick_candidates": len(stock_pick_candidates),
            "avoid_candidates": len(avoid_candidates),
            "stock_pick_displayed": len(stock_pick_display_rows),
            "avoid_displayed": len(avoid_display_rows),
            "coverage_expected": coverage_status["summary"]["expected_count"],
            "coverage_analyzed": coverage_status["summary"]["analyzed_count"],
            "coverage_blocked": coverage_status["summary"]["blocked_count"],
            "coverage_blocked_fixable": coverage_status["summary"].get("blocked_fixable_count", 0),
            "coverage_blocked_retryable": coverage_status["summary"].get("blocked_retryable_count", 0),
            "coverage_blocked_access_constrained": coverage_status["summary"].get("blocked_access_constrained_count", 0),
            **major_event_counts,
        },
        "source_syncs": list(source_syncs.values()),
        "staleness_warning": any(bool(row.get("stale")) for row in source_syncs.values()),
        "coverage_status": coverage_status,
        "sector_heatmap": sector_heatmap,
        "stock_pick_candidates": stock_pick_display_rows,
        "avoid_candidates": avoid_display_rows,
        "highest_risk_changes": sorted(
            highest_risk_changes,
            key=lambda item: (-int(item["risk_score"]), str(item["ticker"])),
        )[:10],
        "most_improved": sorted(
            most_improved,
            key=lambda item: (-int(item["improvement_score"]), str(item["ticker"])),
        )[:10],
        "late_periodic_rows": sorted(
            late_periodic_rows,
            key=lambda item: (
                item["days_since_periodic"] is None,
                -(item["days_since_periodic"] or 0),
                str(item["ticker"]),
            ),
        )[:10],
        "coverage_gap_leaders": coverage_status["gap_leaders"][:10],
        "undercovered_watchlist": coverage_status["undercovered_watchlist"][:10],
    }


def _combine_recommendation_rows(
    *,
    strict_rows: list[dict[str, object]],
    fallback_rows: list[dict[str, object]],
    limit: int = 10,
    minimum_display: int = 3,
) -> list[dict[str, object]]:
    strict_sorted = sorted(
        strict_rows,
        key=lambda item: (-int(item.get("score") or 0), str(item.get("ticker") or "")),
    )
    rows = strict_sorted[:limit]
    if len(rows) >= minimum_display:
        return rows
    seen = {str(item.get("ticker") or "") for item in rows}
    for item in sorted(
        fallback_rows,
        key=lambda row: (-int(row.get("score") or 0), str(row.get("ticker") or "")),
    ):
        ticker = str(item.get("ticker") or "")
        if ticker in seen:
            continue
        rows.append(item)
        seen.add(ticker)
        if len(rows) >= max(minimum_display, min(limit, len(strict_rows) + len(fallback_rows))):
            break
    return rows[:limit]


def _build_recommendation_fallback_row(
    candidate_row: dict[str, object],
    *,
    top_card: dict[str, object],
    recommendation: dict[str, object],
    target: str,
) -> dict[str, object] | None:
    ranking_inputs = recommendation.get("ranking_inputs") or {}
    if target == "pick":
        if str(top_card.get("overall_state") or "") == "critical":
            return None
        score = int(ranking_inputs.get("pick_score") or 0)
        if score <= 0:
            return None
        blockers = _build_pick_blockers(ranking_inputs)
        if not blockers:
            return None
        row = dict(candidate_row)
        row.update(
            {
                "score": score,
                "qualification": "closest",
                "qualification_label": "Closest current setup",
                "blockers": blockers[:3],
            }
        )
        return row
    if target == "avoid":
        score = int(ranking_inputs.get("avoid_score") or 0)
        if score <= 0:
            return None
        blockers = _build_avoid_blockers(ranking_inputs)
        if not blockers:
            return None
        row = dict(candidate_row)
        row.update(
            {
                "score": score,
                "qualification": "closest",
                "qualification_label": "Closest current setup",
                "blockers": blockers[:3],
            }
        )
        return row
    return None


def _build_pick_blockers(ranking_inputs: dict[str, object]) -> list[str]:
    blockers: list[str] = []
    stance = str(ranking_inputs.get("stance") or "Watch")
    confidence = str(ranking_inputs.get("confidence") or "low")
    overall_state = str(ranking_inputs.get("overall_state") or "stable")
    thesis_state = str(ranking_inputs.get("thesis_state") or "none")
    credibility_band = str(ranking_inputs.get("credibility_band") or "unknown")
    data_completeness_pct = float(ranking_inputs.get("data_completeness_pct") or 0.0)
    open_review_count = int(ranking_inputs.get("open_review_count") or 0)
    critical_signal_count = int(ranking_inputs.get("critical_signal_count") or 0)
    quality_flag_count = int(ranking_inputs.get("quality_flag_count") or 0)
    if stance != "Buy":
        blockers.append(f"Current stance is {stance}, not Buy.")
    if confidence != "high":
        blockers.append(f"Confidence is {confidence}, not high.")
    if overall_state != "stable":
        blockers.append(f"Overall state is {overall_state}, not stable.")
    if thesis_state != "improving":
        blockers.append(f"Thesis state is {thesis_state}, not improving.")
    if credibility_band not in {"high", "acceptable"}:
        blockers.append(f"Credibility band is {credibility_band}.")
    if open_review_count > 0:
        blockers.append(f"{open_review_count} open review item(s) still need judgment.")
    if critical_signal_count > 0:
        blockers.append(f"{critical_signal_count} critical signal(s) remain active.")
    if quality_flag_count > 1:
        blockers.append(f"{quality_flag_count} quality flags exceed the pick threshold.")
    if data_completeness_pct < 75.0:
        blockers.append(f"Data completeness is only {data_completeness_pct:.1f}%.")
    return blockers


def _build_avoid_blockers(ranking_inputs: dict[str, object]) -> list[str]:
    blockers: list[str] = []
    stance = str(ranking_inputs.get("stance") or "Watch")
    confidence = str(ranking_inputs.get("confidence") or "low")
    overall_state = str(ranking_inputs.get("overall_state") or "stable")
    thesis_state = str(ranking_inputs.get("thesis_state") or "none")
    credibility_band = str(ranking_inputs.get("credibility_band") or "unknown")
    critical_signal_count = int(ranking_inputs.get("critical_signal_count") or 0)
    critical_review_count = int(ranking_inputs.get("critical_review_count") or 0)
    open_review_count = int(ranking_inputs.get("open_review_count") or 0)
    quality_flag_count = int(ranking_inputs.get("quality_flag_count") or 0)
    if stance != "Avoid":
        blockers.append(f"Current stance is {stance}, not Avoid.")
    if confidence not in {"high", "medium"}:
        blockers.append(f"Confidence is {confidence}, below the avoid threshold.")
    if overall_state not in {"critical", "review_required"} and thesis_state != "broken" and credibility_band != "poor":
        blockers.append("Stress is visible, but it has not crossed the strict avoid gate yet.")
    if critical_signal_count == 0 and critical_review_count == 0 and thesis_state != "broken" and credibility_band != "poor" and quality_flag_count < 2:
        blockers.append("Signals are negative, but not strong enough for a strict avoid call yet.")
    if open_review_count > 0 and critical_review_count == 0:
        blockers.append(f"{open_review_count} open review item(s) are still unresolved.")
    return blockers


def _load_source_sync_rows_from_cfg(cfg: Config) -> list[dict[str, object]]:
    conn = connect(cfg.paths.db_path, read_only=True)
    try:
        configure_pragmas(conn, cfg)
        return _load_source_sync_rows(conn)
    finally:
        conn.close()


def _load_source_sync_rows(conn: sqlite3.Connection) -> list[dict[str, object]]:
    now = datetime.now(timezone.utc)
    rows = conn.execute(
        """
        SELECT source_name, source_class, updated_at
        FROM source_watermarks
        ORDER BY source_name ASC
        """
    ).fetchall()
    return [
        {
            "source_name": row["source_name"],
            "source_class": row["source_class"],
            "updated_at": row["updated_at"],
            "stale": (
                (now - parsed).total_seconds() > 86400
                if (parsed := _parse_iso_datetime(row["updated_at"])) is not None
                else True
            ),
        }
        for row in rows
    ]


def _most_recent_periodic_deadline(
    *,
    fiscal_year_end_month: int,
    now: date,
    latest_financial_period_end: date | None,
    latest_annual_report_period_end: date | None,
) -> dict[str, object] | None:
    candidates: list[dict[str, object]] = []
    for year in range(now.year - 1, now.year + 1):
        for due_date, label, period_end_iso, _order, report_type in _filing_deadlines_for_dashboard(fiscal_year_end_month, year):
            period_end = _parse_iso_date(period_end_iso)
            if due_date > now or period_end is None:
                continue
            if report_type == "annual_report":
                if latest_annual_report_period_end is not None and period_end <= latest_annual_report_period_end:
                    continue
            else:
                if latest_financial_period_end is not None and period_end <= latest_financial_period_end:
                    continue
            candidates.append(
                {
                    "due_date": due_date,
                    "label": label,
                    "period_end": period_end,
                    "report_type": report_type,
                    "order": _order,
                }
            )
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (item["due_date"], item["order"]), reverse=True)[0]


def _filing_deadlines_for_dashboard(fiscal_year_end_month: int, fiscal_year: int) -> list[tuple[date, str, str, int, str]]:
    fiscal_year_end = _month_end(fiscal_year, fiscal_year_end_month)
    q1_end = _shift_months(fiscal_year_end, -9)
    h1_end = _shift_months(fiscal_year_end, -6)
    q3_end = _shift_months(fiscal_year_end, -3)
    return [
        (q1_end + timedelta(days=30), "Q1 filing due", q1_end.isoformat(), 1, "quarterly_fs"),
        (h1_end + timedelta(days=60), "H1 filing due", h1_end.isoformat(), 2, "interim_fs"),
        (q3_end + timedelta(days=30), "Q3 filing due", q3_end.isoformat(), 3, "quarterly_fs"),
        (fiscal_year_end + timedelta(days=120), "Annual FS due", fiscal_year_end.isoformat(), 4, "annual_fs"),
        (fiscal_year_end + timedelta(days=120), "Annual report due", fiscal_year_end.isoformat(), 5, "annual_report"),
    ]


def _month_end(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def _shift_months(anchor: date, months: int) -> date:
    month_index = (anchor.year * 12 + (anchor.month - 1)) + months
    year = month_index // 12
    month = (month_index % 12) + 1
    return _month_end(year, month)


def _load_late_periodic_rows(
    conn: sqlite3.Connection,
    cfg: Config,
    now: datetime,
) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT
            fe.ticker,
            c.issuer_name,
            fe.period_label,
            fe.obligation_type,
            fe.period_end_date,
            fe.deadline_at,
            fe.satisfied_at,
            fe.state,
            fe.knowledge_source
        FROM filing_expectations fe
        LEFT JOIN companies c ON c.ticker = fe.ticker
        WHERE fe.derived_version = ?
          AND fe.state = 'late'
          AND COALESCE(c.active_status, 'listed') != 'delisted'
        ORDER BY fe.deadline_at DESC, fe.ticker ASC
        LIMIT 25
        """,
        (DERIVED_VERSION,),
    ).fetchall()
    if rows:
        return [
            {
                "ticker": row["ticker"],
                "issuer_name": row["issuer_name"],
                "latest_periodic_at": row["period_end_date"],
                "days_since_periodic": (
                    now.date() - _parse_iso_date(row["period_end_date"])
                ).days if _parse_iso_date(row["period_end_date"]) is not None else None,
                "expected_label": row["period_label"],
                "expected_due_date": row["deadline_at"],
                "knowledge_source": row["knowledge_source"],
            }
            for row in rows
        ]

    recent_financial_rows = conn.execute(
        """
        SELECT
            c.ticker,
            c.issuer_name,
            c.fiscal_year_end_month,
            MAX(
                CASE
                    WHEN d.document_type != 'annual_report'
                    THEN COALESCE(mi.period_end_date, d.source_modified_at, d.updated_at, d.created_at)
                END
            ) AS latest_financial_periodic_at,
            MAX(
                CASE
                    WHEN d.document_type = 'annual_report'
                    THEN COALESCE(mi.period_end_date, d.source_modified_at, d.updated_at, d.created_at)
                END
            ) AS latest_annual_report_at
        FROM companies c
        LEFT JOIN documents d
          ON d.ticker = c.ticker
         AND d.document_type IN ('annual_report', 'financial_statement')
        LEFT JOIN metric_items mi
          ON mi.document_id = d.document_id
        WHERE COALESCE(c.active_status, 'listed') != 'delisted'
        GROUP BY c.ticker, c.issuer_name, c.fiscal_year_end_month
        ORDER BY c.ticker ASC
        """
    ).fetchall()
    legacy_rows: list[dict[str, object]] = []
    for row in recent_financial_rows:
        latest_financial_period_end = _parse_iso_date(row["latest_financial_periodic_at"]) or (
            _parse_iso_datetime(row["latest_financial_periodic_at"]).date()
            if _parse_iso_datetime(row["latest_financial_periodic_at"]) is not None
            else None
        )
        latest_annual_report_period_end = _parse_iso_date(row["latest_annual_report_at"]) or (
            _parse_iso_datetime(row["latest_annual_report_at"]).date()
            if _parse_iso_datetime(row["latest_annual_report_at"]) is not None
            else None
        )
        deadline = _most_recent_periodic_deadline(
            fiscal_year_end_month=resolve_fiscal_year_end_month(
                conn,
                cfg,
                ticker=row["ticker"],
                stored_month=int(row["fiscal_year_end_month"] or 12),
            )[0],
            now=now.date(),
            latest_financial_period_end=latest_financial_period_end,
            latest_annual_report_period_end=latest_annual_report_period_end,
        )
        if deadline is None:
            continue
        legacy_rows.append(
            {
                "ticker": row["ticker"],
                "issuer_name": row["issuer_name"],
                "latest_periodic_at": row["latest_financial_periodic_at"] or row["latest_annual_report_at"],
                "days_since_periodic": (
                    now.date() - (latest_annual_report_period_end or latest_financial_period_end)
                ).days if (latest_annual_report_period_end or latest_financial_period_end) is not None else None,
                "expected_label": deadline["label"],
                "expected_due_date": deadline["due_date"].isoformat(),
                "knowledge_source": "legacy",
            }
        )
    return legacy_rows


def _load_metric_snapshot(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        f"""
        SELECT
            mi.ticker,
            mi.metric_name,
            mi.value_normalized,
            mi.currency,
            mi.fiscal_year,
            mi.fiscal_period,
            mi.period_end_date,
            mi.document_id
        FROM metric_items mi
        JOIN documents d ON d.document_id = mi.document_id
        WHERE mi.ticker IS NOT NULL
          AND {preferred_metric_sql("mi")}
        ORDER BY COALESCE(mi.period_end_date, '') DESC, mi.fiscal_year DESC, d.updated_at DESC
        """
    ).fetchall()
    latest: dict[tuple[str, str], dict[str, object]] = {}
    for row in rows:
        key = (row["ticker"], row["metric_name"])
        latest.setdefault(key, dict(row))
    return sorted(latest.values(), key=lambda item: (str(item["ticker"]), str(item["metric_name"])))


def _load_metrics_for_ticker(conn: sqlite3.Connection, *, ticker: str) -> list[dict[str, object]]:
    rows = conn.execute(
        f"""
        SELECT
            mi.ticker,
            mi.metric_name,
            mi.value_normalized,
            mi.currency,
            mi.fiscal_year,
            mi.fiscal_period,
            mi.period_end_date,
            mi.document_id
        FROM metric_items mi
        JOIN documents d ON d.document_id = mi.document_id
        WHERE mi.ticker = ?
          AND {preferred_metric_sql("mi")}
        ORDER BY COALESCE(mi.period_end_date, '') DESC, mi.fiscal_year DESC, d.updated_at DESC
        """,
        (ticker,),
    ).fetchall()
    latest: dict[str, dict[str, object]] = {}
    for row in rows:
        latest.setdefault(str(row["metric_name"]), dict(row))
    return sorted(latest.values(), key=lambda item: str(item["metric_name"]))


def _load_findings(conn: sqlite3.Connection, *, limit: int) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT
            ar.document_id,
            d.ticker,
            d.source_name,
            d.local_path,
            d.original_url,
            ar.output_json,
            ar.finished_at
        FROM analysis_runs ar
        JOIN documents d ON d.document_id = ar.document_id
        ORDER BY ar.finished_at DESC, ar.analysis_run_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return _findings_from_analysis_rows(rows, limit=limit)


def _load_findings_for_ticker(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    limit: int,
) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT
            ar.document_id,
            d.ticker,
            d.source_name,
            d.local_path,
            d.original_url,
            ar.output_json,
            ar.finished_at
        FROM analysis_runs ar
        JOIN documents d ON d.document_id = ar.document_id
        WHERE d.ticker = ?
        ORDER BY ar.finished_at DESC, ar.analysis_run_id DESC
        LIMIT ?
        """,
        (ticker, limit),
    ).fetchall()
    return _findings_from_analysis_rows(rows, limit=limit)


def _findings_from_analysis_rows(
    rows: list[sqlite3.Row],
    *,
    limit: int,
) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for row in rows:
        payload = json.loads(row["output_json"])
        evidence_map = {
            item["id"]: item
            for item in payload.get("evidence_links", [])
        }
        summary = payload.get("summary", "")
        for item in payload.get("key_changes", []):
            findings.append(
                {
                    "document_id": row["document_id"],
                    "ticker": row["ticker"],
                    "source_name": row["source_name"],
                    "local_path": row["local_path"],
                    "original_url": row["original_url"],
                    "finding_type": "key_change",
                    "name": item.get("severity", "unknown"),
                    "claim": item.get("claim", ""),
                    "summary": summary,
                    "evidence": _evidence_preview(item.get("evidence_links", []), evidence_map),
                    "finished_at": row["finished_at"],
                }
            )
        for item in payload.get("risks", []):
            findings.append(
                {
                    "document_id": row["document_id"],
                    "ticker": row["ticker"],
                    "source_name": row["source_name"],
                    "local_path": row["local_path"],
                    "original_url": row["original_url"],
                    "finding_type": "risk",
                    "name": "risk",
                    "claim": item.get("claim", ""),
                    "summary": summary,
                    "evidence": _evidence_preview(item.get("evidence_links", []), evidence_map),
                    "finished_at": row["finished_at"],
                }
            )
        for item in payload.get("events", []):
            findings.append(
                {
                    "document_id": row["document_id"],
                    "ticker": row["ticker"],
                    "source_name": row["source_name"],
                    "local_path": row["local_path"],
                    "original_url": row["original_url"],
                    "finding_type": "event",
                    "name": item.get("event_type", ""),
                    "claim": item.get("event_type", ""),
                    "summary": summary,
                    "evidence": _evidence_preview(item.get("evidence_links", []), evidence_map),
                    "finished_at": row["finished_at"],
                }
            )
    return findings[:limit]


def _load_revision_history(conn: sqlite3.Connection, *, limit: int) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT
            version_group_id,
            source_name,
            source_record_id,
            ticker,
            COUNT(*) AS revision_count,
            MAX(COALESCE(source_modified_at, retrieved_at)) AS latest_revision_at
        FROM documents
        WHERE version_group_id IS NOT NULL
        GROUP BY version_group_id, source_name, source_record_id, ticker
        ORDER BY latest_revision_at DESC, version_group_id ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def _load_revision_history_for_ticker(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    limit: int,
) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT
            version_group_id,
            source_name,
            source_record_id,
            ticker,
            COUNT(*) AS revision_count,
            MAX(COALESCE(source_modified_at, retrieved_at)) AS latest_revision_at
        FROM documents
        WHERE version_group_id IS NOT NULL
          AND ticker = ?
        GROUP BY version_group_id, source_name, source_record_id, ticker
        ORDER BY latest_revision_at DESC, version_group_id ASC
        LIMIT ?
        """,
        (ticker, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def _load_company_snapshots(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT ticker, as_of_document_id, as_of_date, issuer_condition, payload_json, updated_at
        FROM company_snapshots
        WHERE snapshot_kind = 'current'
          AND derived_version = ?
        ORDER BY updated_at DESC, ticker ASC
        """
        ,
        (DERIVED_VERSION,),
    ).fetchall()
    snapshots: list[dict[str, object]] = []
    for row in rows:
        payload = json.loads(row["payload_json"] or "{}")
        snapshots.append(
            {
                "ticker": row["ticker"],
                "as_of_document_id": row["as_of_document_id"],
                "as_of_date": row["as_of_date"],
                "issuer_condition": row["issuer_condition"],
                "updated_at": row["updated_at"],
                "payload": payload,
            }
        )
    return snapshots


def _load_company_snapshot_for_ticker(
    conn: sqlite3.Connection,
    *,
    ticker: str,
) -> dict[str, object] | None:
    row = conn.execute(
        """
        SELECT ticker, as_of_document_id, as_of_date, issuer_condition, payload_json, updated_at
        FROM company_snapshots
        WHERE snapshot_kind = 'current'
          AND derived_version = ?
          AND ticker = ?
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (DERIVED_VERSION, ticker),
    ).fetchone()
    if row is None:
        return None
    return {
        "ticker": row["ticker"],
        "as_of_document_id": row["as_of_document_id"],
        "as_of_date": row["as_of_date"],
        "issuer_condition": row["issuer_condition"],
        "updated_at": row["updated_at"],
        "payload": json.loads(row["payload_json"] or "{}"),
    }


def _load_sector_rollups(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT sector, subsector, rollup_key, payload_json, updated_at
        FROM sector_rollups
        WHERE derived_version = ?
        ORDER BY updated_at DESC, sector ASC, COALESCE(subsector, '') ASC
        """
        ,
        (DERIVED_VERSION,),
    ).fetchall()
    return [
        {
            "sector": row["sector"],
            "subsector": row["subsector"],
            "rollup_key": row["rollup_key"],
            "payload": json.loads(row["payload_json"] or "{}"),
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def _load_sector_rollup_for_ticker(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    company_snapshot: dict[str, object] | None,
) -> dict[str, object] | None:
    sector = None
    if company_snapshot is not None:
        sector = (company_snapshot.get("payload") or {}).get("sector")
    if not sector:
        row = conn.execute(
            """
            SELECT sector
            FROM companies
            WHERE ticker = ?
            LIMIT 1
            """,
            (ticker,),
        ).fetchone()
        sector = row["sector"] if row is not None else None
    if not sector:
        return None
    row = conn.execute(
        """
        SELECT sector, subsector, rollup_key, payload_json, updated_at
        FROM sector_rollups
        WHERE rollup_key = 'peer_intelligence'
          AND sector = ?
          AND derived_version = ?
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (sector, DERIVED_VERSION),
    ).fetchone()
    if row is None:
        return None
    return {
        "sector": row["sector"],
        "subsector": row["subsector"],
        "rollup_key": row["rollup_key"],
        "payload": json.loads(row["payload_json"] or "{}"),
        "updated_at": row["updated_at"],
    }


def _load_inbox_rows(conn: sqlite3.Connection, *, limit: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []

    document_rows = conn.execute(
        """
        SELECT
            d.updated_at AS happened_at,
            d.ticker,
            d.document_id,
            d.source_name,
            d.document_type,
            d.process_state,
            d.local_path,
            d.original_url
        FROM documents d
        ORDER BY d.updated_at DESC, d.document_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    for row in document_rows:
        rows.append(
            {
                "kind": "document",
                "happened_at": row["happened_at"],
                "ticker": row["ticker"],
                "document_id": row["document_id"],
                "source_name": row["source_name"],
                "severity": "ok",
                "title": f"{row['ticker'] or 'Unknown'} {row['document_type'] or 'document'}",
                "summary": f"{row['source_name']} is now {row['process_state']}.",
                "local_path": row["local_path"],
                "original_url": row["original_url"],
            }
        )

    rule_rows = conn.execute(
        """
        SELECT
            rr.evaluated_at AS happened_at,
            rr.ticker,
            rr.document_id,
            rr.rule_name,
            rr.severity,
            rr.rationale,
            d.local_path,
            d.original_url
        FROM rule_runs rr
        JOIN documents d ON d.document_id = rr.document_id
        WHERE rr.triggered = 1
        ORDER BY rr.evaluated_at DESC, rr.rule_run_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    for row in rule_rows:
        rows.append(
            {
                "kind": "rule",
                "happened_at": row["happened_at"],
                "ticker": row["ticker"],
                "document_id": row["document_id"],
                "source_name": "rule_runs",
                "severity": row["severity"] or "warning",
                "title": row["rule_name"],
                "summary": row["rationale"] or "Rule triggered.",
                "local_path": row["local_path"],
                "original_url": row["original_url"],
            }
        )

    analysis_rows = conn.execute(
        """
        SELECT
            ar.finished_at AS happened_at,
            d.ticker,
            ar.document_id,
            d.local_path,
            d.original_url,
            ar.output_json
        FROM analysis_runs ar
        JOIN documents d ON d.document_id = ar.document_id
        ORDER BY ar.finished_at DESC, ar.analysis_run_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    for row in analysis_rows:
        payload = json.loads(row["output_json"])
        rows.append(
            {
                "kind": "summary",
                "happened_at": row["happened_at"],
                "ticker": row["ticker"],
                "document_id": row["document_id"],
                "source_name": "analysis_runs",
                "severity": "info",
                "title": "Summary generated",
                "summary": payload.get("summary", ""),
                "local_path": row["local_path"],
                "original_url": row["original_url"],
            }
        )

    event_rows = conn.execute(
        """
        SELECT
            e.detected_at AS happened_at,
            e.ticker,
            e.document_id,
            e.event_type,
            e.severity,
            e.rationale,
            d.local_path,
            d.original_url
        FROM events e
        JOIN documents d ON d.document_id = e.document_id
        ORDER BY e.detected_at DESC, e.event_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    for row in event_rows:
        rows.append(
            {
                "kind": "event",
                "happened_at": row["happened_at"],
                "ticker": row["ticker"],
                "document_id": row["document_id"],
                "source_name": "events",
                "severity": row["severity"] or "info",
                "title": row["event_type"],
                "summary": row["rationale"] or row["event_type"],
                "local_path": row["local_path"],
                "original_url": row["original_url"],
            }
        )

    alert_rows = conn.execute(
        """
        SELECT
            ae.created_at AS happened_at,
            ae.ticker,
            ae.document_id,
            ae.signal_kind,
            ae.signal_name,
            ae.severity,
            d.local_path,
            d.original_url
        FROM alert_events ae
        -- Market alerts have no document.
        LEFT JOIN documents d ON d.document_id = ae.document_id
        ORDER BY ae.created_at DESC, ae.alert_event_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    for row in alert_rows:
        rows.append(
            {
                "kind": "alert",
                "happened_at": row["happened_at"],
                "ticker": row["ticker"],
                "document_id": row["document_id"],
                "source_name": "alert_events",
                "severity": row["severity"] or "warning",
                "title": row["signal_name"],
                "summary": "Market alert emitted." if row["document_id"] is None else "Watchlist alert emitted.",
                "local_path": row["local_path"],
                "original_url": row["original_url"],
            }
        )

    canary_rows = conn.execute(
        """
        SELECT
            finished_at AS happened_at,
            source_name,
            status,
            accessible,
            drifted
        FROM canary_runs
        ORDER BY finished_at DESC, canary_run_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    for row in canary_rows:
        severity = "critical" if row["status"] == "failed" else "warning" if row["drifted"] else "ok"
        rows.append(
            {
                "kind": "canary",
                "happened_at": row["happened_at"],
                "ticker": None,
                "document_id": None,
                "source_name": row["source_name"],
                "severity": severity,
                "title": f"{row['source_name']} canary",
                "summary": f"status={row['status']} accessible={row['accessible']} drifted={row['drifted']}",
                "local_path": None,
                "original_url": None,
            }
        )

    def _sort_key(item: dict[str, object]) -> tuple[str, str]:
        return (str(item.get("happened_at") or ""), str(item.get("title") or ""))

    rows.sort(key=_sort_key, reverse=True)
    return rows[:limit]


def _evidence_preview(span_ids: list[str], evidence_map: dict[str, dict[str, object]]) -> str:
    previews: list[str] = []
    for span_id in span_ids:
        entry = evidence_map.get(span_id)
        if entry is None:
            previews.append(span_id)
            continue
        previews.append(f"{entry['id']} p{entry['page_start']}: {entry.get('excerpt_text', '')[:120]}")
    return " | ".join(previews)


def _parse_iso_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _parse_iso_date(value: object) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _sum_event_counts(event_counts: dict[str, int], watched: set[str]) -> int:
    return sum(event_counts.get(name, 0) for name in watched)
