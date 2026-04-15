"""Intelligence / derived-layer commands."""

from __future__ import annotations

import argparse
import json
from datetime import date

from ..config import load_config
from ..intelligence import DERIVED_VERSION, rederive_intelligence, run_intelligence
from ..intelligence.review import (
    load_intelligence_kpis,
    load_review_queue,
    run_daily_digest,
    update_review_status,
)
from ..logging_setup import setup_logging


def cmd_intelligence_run(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    result = run_intelligence(
        cfg,
        ticker=args.ticker,
        limit=args.limit,
    )
    print(
        "intelligence-run: "
        f"derived_version={DERIVED_VERSION} "
        f"selected_tickers={result.selected_tickers} "
        f"deltas={result.deltas} "
        f"snapshots={result.snapshots} "
        f"timeseries={result.timeseries} "
        f"briefs={result.briefs} "
        f"review_items={result.review_items} "
        f"peer_sets={result.peer_sets} "
        f"sector_rollups={result.sector_rollups} "
        f"thesis_evaluations={result.thesis_evaluations} "
        f"claims={result.claims} "
        f"claim_outcomes={result.claim_outcomes}"
    )
    return 0


def cmd_digest_run(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    target_date = date.fromisoformat(args.date) if args.date else None
    result = run_daily_digest(
        cfg,
        digest_date=target_date,
        scope=args.scope,
        limit=args.limit,
    )
    print(
        "digest-run: "
        f"digest_id={result.digest_id} "
        f"digest_date={result.digest_date} "
        f"scope={result.scope} "
        f"companies={result.companies} "
        f"review_items={result.review_items} "
        f"alerts={result.alerts} "
        f"anomalies={result.anomalies}"
    )
    return 0


def cmd_review_queue(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    report = load_review_queue(
        cfg,
        status=args.status,
        limit=args.limit,
        ticker=args.ticker,
    )
    for row in report.rows:
        print(
            f"{row['review_item_id']}: ticker={row['ticker'] or '-'} "
            f"type={row['item_type']} priority={row['priority']} status={row['status']} "
            f"decision={row['decision_state'] or '-'} updated_at={row['updated_at']}"
        )
    if report.status_counts:
        print(
            "status_counts: "
            + " ".join(f"{status}={count}" for status, count in sorted(report.status_counts.items()))
        )
    return 0


def cmd_review_update(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    result = update_review_status(
        cfg,
        review_item_id=args.review_item_id,
        status=args.status,
        note=args.note,
    )
    print(
        "review-update: "
        f"review_item_id={result.review_item_id} "
        f"from_status={result.previous_status} "
        f"to_status={result.current_status} "
        f"note={result.note or '-'}"
    )
    return 0


def cmd_intelligence_kpis(_args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    report = load_intelligence_kpis(cfg)
    print(json.dumps(report, sort_keys=True))
    return 0


def cmd_intelligence_rederive(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    result = rederive_intelligence(
        cfg,
        ticker=args.ticker,
        limit=args.limit,
    )
    print(
        "intelligence-rederive: "
        f"derived_version={DERIVED_VERSION} "
        f"selected_tickers={result.selected_tickers} "
        f"deltas={result.deltas} "
        f"snapshots={result.snapshots} "
        f"timeseries={result.timeseries} "
        f"briefs={result.briefs} "
        f"review_items={result.review_items} "
        f"peer_sets={result.peer_sets} "
        f"sector_rollups={result.sector_rollups} "
        f"thesis_evaluations={result.thesis_evaluations} "
        f"claims={result.claims} "
        f"claim_outcomes={result.claim_outcomes}"
    )
    return 0


def register(sub) -> None:
    p_intelligence = sub.add_parser(
        "intelligence-run",
        help="Derive Phase 10 company deltas, snapshots, and review items",
    )
    p_intelligence.add_argument("--ticker", help="Optional ticker filter for derived intelligence")
    p_intelligence.add_argument("--limit", type=int, default=25, help="Maximum number of tickers to derive per run")
    p_intelligence.set_defaults(func=cmd_intelligence_run)

    p_rederive = sub.add_parser(
        "intelligence-rederive",
        help="Rebuild current derived snapshots for existing tickers",
    )
    p_rederive.add_argument("--ticker", help="Optional ticker filter for a single issuer")
    p_rederive.add_argument("--limit", type=int, default=1000, help="Maximum number of existing snapshot tickers to rebuild")
    p_rederive.set_defaults(func=cmd_intelligence_rederive)

    p_digest = sub.add_parser("digest-run", help="Build and persist a Phase 15 daily digest")
    p_digest.add_argument("--scope", choices=["watchlist", "all"], default="watchlist", help="Digest scope")
    p_digest.add_argument("--date", help="Digest date override (YYYY-MM-DD)")
    p_digest.add_argument("--limit", type=int, default=25, help="Maximum number of companies to include")
    p_digest.set_defaults(func=cmd_digest_run)

    p_review_queue = sub.add_parser("review-queue", help="Show Phase 15 review queue items")
    p_review_queue.add_argument("--status", choices=["open", "resolved", "snoozed", "all"], default="open")
    p_review_queue.add_argument("--limit", type=int, default=25)
    p_review_queue.add_argument("--ticker", help="Optional ticker filter")
    p_review_queue.set_defaults(func=cmd_review_queue)

    p_review_update = sub.add_parser("review-update", help="Update the status of a review queue item")
    p_review_update.add_argument("review_item_id", help="Review item id to update")
    p_review_update.add_argument("--status", required=True, choices=["open", "resolved", "snoozed"])
    p_review_update.add_argument("--note", help="Optional operator note")
    p_review_update.set_defaults(func=cmd_review_update)

    p_intelligence_kpis = sub.add_parser("intelligence-kpis", help="Show v3 derived-layer KPI counts")
    p_intelligence_kpis.set_defaults(func=cmd_intelligence_kpis)
