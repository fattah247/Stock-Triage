"""Hardening commands: backfill, backlog-status, benchmark-sqlite."""

from __future__ import annotations

import argparse

from ..config import load_config
from ..hardening import load_backlog, run_backfill, run_sqlite_benchmark
from ..logging_setup import setup_logging


def cmd_backfill(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    result = run_backfill(
        cfg,
        source=args.source,
        start_year=args.start_year,
        end_year=args.end_year,
        discover_limit=args.discover_limit,
        download_limit=args.download_limit,
        parse_limit=args.parse_limit,
        extract_limit=args.extract_limit,
        rules_limit=args.rules_limit,
    )
    for row in result.rows:
        print(
            f"year={row.year} discovered={row.discovered} inserted={row.inserted} "
            f"updated={row.updated} downloaded={row.downloaded} parsed={row.parsed} "
            f"extracted={row.extracted} analyzed={row.analyzed}"
        )
    return 0


def cmd_backlog_status(_args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    report = load_backlog(cfg)
    for row in report.rows:
        print(
            f"{row.source_name}: discovered={row.discovered} downloaded={row.downloaded} "
            f"parsed={row.parsed} extracted={row.extracted} analyzed={row.analyzed} failed={row.failed}"
        )
    return 0


def cmd_benchmark_sqlite(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    result = run_sqlite_benchmark(
        cfg,
        batches=args.batches,
        rows_per_batch=args.rows_per_batch,
    )
    print(
        "benchmark-sqlite: "
        f"batches={result.batches} rows_per_batch={result.rows_per_batch} total_rows={result.total_rows} "
        f"total_seconds={result.total_seconds:.4f} rows_per_second={result.rows_per_second:.2f} "
        f"avg_submit_ms={result.avg_submit_ms:.2f} p95_submit_ms={result.p95_submit_ms:.2f}"
    )
    return 0


def register(sub) -> None:
    p_backfill = sub.add_parser("backfill", help="Run a bounded historical backfill through the pipeline")
    p_backfill.add_argument(
        "--source",
        choices=["company_universe", "disclosure", "financial_report", "all"],
        default="all",
        help="Source slice to backfill",
    )
    p_backfill.add_argument("--start-year", type=int, required=True, help="First year to backfill")
    p_backfill.add_argument("--end-year", type=int, required=True, help="Last year to backfill")
    p_backfill.add_argument(
        "--discover-limit",
        type=int,
        help="Maximum discovery candidates to persist per year; defaults to the largest downstream stage budget",
    )
    p_backfill.add_argument("--download-limit", type=int, default=50, help="Download budget per year")
    p_backfill.add_argument("--parse-limit", type=int, default=50, help="Parse budget per year")
    p_backfill.add_argument("--extract-limit", type=int, default=50, help="Extract budget per year")
    p_backfill.add_argument("--rules-limit", type=int, default=50, help="Rule-evaluation budget per year")
    p_backfill.set_defaults(func=cmd_backfill)

    p_backlog_status = sub.add_parser("backlog-status", help="Show current per-source processing backlog")
    p_backlog_status.set_defaults(func=cmd_backlog_status)

    p_benchmark_sqlite = sub.add_parser(
        "benchmark-sqlite",
        help="Measure SQLite writer throughput and queue latency",
    )
    p_benchmark_sqlite.add_argument("--batches", type=int, default=20, help="Number of writer batches to execute")
    p_benchmark_sqlite.add_argument("--rows-per-batch", type=int, default=100, help="Rows inserted per batch")
    p_benchmark_sqlite.set_defaults(func=cmd_benchmark_sqlite)
