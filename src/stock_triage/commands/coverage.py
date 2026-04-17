"""v3.4 coverage and nuance operator commands."""

from __future__ import annotations

import argparse
import json
from datetime import date

from ..config import load_config
from ..coverage import (
    default_coverage_range,
    load_coverage_status,
    preview_coverage_action,
    run_coverage_action,
)
from ..db import WriterQueue, run_migrations
from ..intelligence.review import load_intelligence_observability, load_nuance_audit
from ..logging_setup import setup_logging


def cmd_coverage_status(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)
    report = load_coverage_status(
        cfg,
        date_from=_date_or_none(args.date_from),
        date_to=_date_or_none(args.date_to),
        scope=args.scope,
        sector=args.sector,
        ticker=args.ticker,
        tickers=_split_tickers(args.tickers),
        source_name=args.source,
        document_type=args.document_type,
        limit=args.limit,
        materialize=True,
    )
    print(
        "coverage-status: "
        f"scope={report['scope']} "
        f"date_from={report['date_from']} "
        f"date_to={report['date_to']} "
        f"ticker_count={report['ticker_count']} "
        f"expected={report['summary']['expected_count']} "
        f"analyzed={report['summary']['analyzed_count']} "
        f"blocked={report['summary']['blocked_count']}"
    )
    print(json.dumps(report, sort_keys=True))
    return 0


def cmd_coverage_preview(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)
    preview = preview_coverage_action(
        cfg,
        action=args.action,
        date_from=_date_or_none(args.date_from),
        date_to=_date_or_none(args.date_to),
        scope=args.scope,
        sector=args.sector,
        ticker=args.ticker,
        tickers=_split_tickers(args.tickers),
        source_name=args.source,
        document_type=args.document_type,
        limit=args.limit,
    )
    print(
        "coverage-preview: "
        f"action={preview['action']} "
        f"scope={preview['scope_label']} "
        f"date_from={preview['date_from']} "
        f"date_to={preview['date_to']} "
        f"ticker_count={preview['ticker_count']} "
        f"targets={preview['action_target_count']}"
    )
    print(json.dumps(preview, sort_keys=True))
    return 0


def cmd_coverage_run(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)
    result = run_coverage_action(
        cfg,
        action=args.action,
        date_from=_date_or_none(args.date_from),
        date_to=_date_or_none(args.date_to),
        scope=args.scope,
        sector=args.sector,
        ticker=args.ticker,
        tickers=_split_tickers(args.tickers),
        source_name=args.source,
        document_type=args.document_type,
        limit=args.limit,
    )
    status = result["status"]
    print(
        "coverage-run: "
        f"action={result['action']} "
        f"executed={result['executed']} "
        f"scope={result['preview']['scope_label']} "
        f"expected={status['summary']['expected_count']} "
        f"analyzed={status['summary']['analyzed_count']} "
        f"blocked={status['summary']['blocked_count']}"
    )
    print(json.dumps(result, sort_keys=True))
    return 0


def cmd_nuance_audit(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)
    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
    observability = load_intelligence_observability(cfg)
    audit = load_nuance_audit(cfg, ticker=args.ticker, limit=args.limit)
    output = {
        "scope": audit["scope"],
        "target_ticker": audit["target_ticker"],
        "limit": args.limit,
        "observability": observability,
        "audit": audit,
    }
    print(
        "nuance-audit: "
        f"scope={output['scope']} "
        f"target_ticker={output['target_ticker'] or '-'} "
        f"leaders={audit['summary'].get('leader_count', 0)} "
        f"coverage_bridge={audit['summary'].get('coverage_bridge_count', 0)} "
        f"low_confidence_event_companies={observability.get('summary', {}).get('low_confidence_event_companies', 0)}"
    )
    print(json.dumps(output, sort_keys=True))
    return 0


def _date_or_none(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _split_tickers(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def _add_scope_args(parser: argparse.ArgumentParser) -> None:
    default_from, default_to = default_coverage_range()
    parser.add_argument("--date-from", default=default_from.isoformat(), help="Coverage range start (YYYY-MM-DD)")
    parser.add_argument("--date-to", default=default_to.isoformat(), help="Coverage range end (YYYY-MM-DD)")
    parser.add_argument("--scope", choices=["all", "watchlist", "sector", "ticker-set"], default="watchlist")
    parser.add_argument("--sector", help="Sector filter when --scope=sector")
    parser.add_argument("--ticker", help="Single ticker for company-scoped coverage")
    parser.add_argument("--tickers", help="Comma-separated ticker set when --scope=ticker-set")
    parser.add_argument("--source", help="Optional source filter, for example idx_financial_reports or idx_disclosures")
    parser.add_argument("--document-type", help="Optional document type filter, for example annual_report or financial_statement")
    parser.add_argument("--limit", type=int, default=20, help="Preview or action target limit")


def register(sub) -> None:
    p_status = sub.add_parser("coverage-status", help="Materialize and show v3.4 coverage accounting for a scope and range")
    _add_scope_args(p_status)
    p_status.set_defaults(func=cmd_coverage_status)

    p_preview = sub.add_parser("coverage-preview", help="Show a read-only preview for a scoped v3.4 coverage action")
    p_preview.add_argument("action", choices=["discover-missing", "download-missing", "process-missing", "rerun-blocked"])
    _add_scope_args(p_preview)
    p_preview.set_defaults(func=cmd_coverage_preview)

    p_run = sub.add_parser("coverage-run", help="Execute a scoped v3.4 coverage catch-up action")
    p_run.add_argument("action", choices=["discover-missing", "download-missing", "process-missing", "rerun-blocked"])
    _add_scope_args(p_run)
    p_run.set_defaults(func=cmd_coverage_run)

    p_nuance = sub.add_parser("nuance-audit", help="Show v3.4 nuance and uncertainty audit summaries")
    p_nuance.add_argument("--ticker", help="Optional ticker focus for interpreting the audit")
    p_nuance.add_argument("--limit", type=int, default=20)
    p_nuance.set_defaults(func=cmd_nuance_audit)
