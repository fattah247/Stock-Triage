"""Intelligence / derived-layer commands."""

from __future__ import annotations

import argparse
import json
from datetime import date

from ..config import load_config
from ..fiscal_year import save_fiscal_year_override
from ..intelligence import DERIVED_VERSION, rederive_intelligence, run_intelligence
from ..intelligence.review import (
    load_intelligence_observability,
    load_intelligence_kpis,
    load_latest_golden_runs,
    load_review_queue,
    run_golden_set,
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


def cmd_intelligence_observability(_args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    report = load_intelligence_observability(cfg)
    print(json.dumps(report, sort_keys=True))
    return 0


def cmd_intelligence_golden(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    result = run_golden_set(
        cfg,
        ticker=args.ticker,
        limit=args.limit,
    )
    payload = load_latest_golden_runs(cfg, limit=1)[0]
    print(
        "intelligence-golden: "
        f"derived_version={DERIVED_VERSION} "
        f"golden_run_id={result.golden_run_id} "
        f"scope={result.scope} "
        f"target_ticker={result.target_ticker or '-'} "
        f"case_count={result.case_count} "
        f"passed_count={result.passed_count} "
        f"failed_count={result.failed_count} "
        f"warning_count={result.warning_count}"
    )
    if payload.get("summary"):
        print(f"summary={payload['summary']}")
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


def cmd_fiscal_year_override(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    overrides = save_fiscal_year_override(
        cfg.company_overrides.definition_path,
        ticker=args.ticker,
        month=None if args.clear else args.month,
    )
    ticker = args.ticker.upper()
    month = overrides.get(ticker)
    print(
        "fiscal-year-override: "
        f"ticker={ticker} "
        f"month={month if month is not None else '-'} "
        f"path={cfg.company_overrides.definition_path}"
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

    p_intelligence_observability = sub.add_parser(
        "intelligence-observability",
        help="Show v3 intelligence QA and observability summary",
    )
    p_intelligence_observability.set_defaults(func=cmd_intelligence_observability)

    p_intelligence_golden = sub.add_parser(
        "intelligence-golden",
        help="Run deterministic golden-set checks against current company snapshots",
    )
    p_intelligence_golden.add_argument("--ticker", help="Optional ticker filter for a single issuer")
    p_intelligence_golden.add_argument("--limit", type=int, default=25, help="Maximum number of snapshot rows to audit")
    p_intelligence_golden.set_defaults(func=cmd_intelligence_golden)

    p_fye = sub.add_parser(
        "fiscal-year-override",
        help="Set or clear a local fiscal year end month override for one ticker",
    )
    p_fye.add_argument("ticker", help="Ticker to override")
    p_fye_group = p_fye.add_mutually_exclusive_group(required=True)
    p_fye_group.add_argument("--month", type=int, help="Fiscal year end month (1-12)")
    p_fye_group.add_argument("--clear", action="store_true", help="Remove the local override")
    p_fye.set_defaults(func=cmd_fiscal_year_override)
