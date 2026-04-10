"""Alerts and canary health check commands."""

from __future__ import annotations

import argparse

from ..alerts import run_alerts
from ..canary import load_canary_status, run_canary
from ..config import load_config
from ..logging_setup import setup_logging


def cmd_alerts_run(_args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    result = run_alerts(cfg)
    print(
        "alerts-run: "
        f"selected={result.selected} "
        f"emitted={result.emitted} "
        f"skipped_existing={result.skipped_existing}"
    )
    return 0


def cmd_canary_run(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    result = run_canary(cfg, source_name=None if args.source == "all" else args.source)
    for row in result.rows:
        print(
            f"{row.source_name}: status={row.status} "
            f"accessible={1 if row.accessible else 0} "
            f"drifted={1 if row.drifted else 0}"
        )
    return 0


def cmd_canary_status(_args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    report = load_canary_status(cfg)
    for row in report.rows:
        print(
            f"{row.source_name}: status={row.status} "
            f"accessible={1 if row.accessible else 0} "
            f"drifted={1 if row.drifted else 0} "
            f"finished_at={row.finished_at}"
        )
    return 0


def register(sub) -> None:
    p_alerts_run = sub.add_parser(
        "alerts-run",
        help="Evaluate watchlist alerts and emit local notifications",
    )
    p_alerts_run.set_defaults(func=cmd_alerts_run)

    p_canary_run = sub.add_parser(
        "canary-run",
        help="Run source healthchecks and persist canary snapshots",
    )
    p_canary_run.add_argument(
        "--source",
        choices=["idx_company_universe", "idx_disclosures", "idx_financial_reports", "all"],
        default="all",
        help="Optional source filter for canary checks",
    )
    p_canary_run.set_defaults(func=cmd_canary_run)

    p_canary_status = sub.add_parser(
        "canary-status",
        help="Show the latest persisted canary result per source",
    )
    p_canary_status.set_defaults(func=cmd_canary_status)
