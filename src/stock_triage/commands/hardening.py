"""Hardening commands: backfill, backlog-status, benchmark-sqlite, backfill-sectors."""

from __future__ import annotations

import argparse
import json

from ..config import load_config
from ..db import WriterQueue, connect, configure_pragmas, run_migrations
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


def cmd_backfill_sectors(_args: argparse.Namespace) -> int:
    """Read raw_metadata from companies table and fill missing sector/subsector."""
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)

    conn = connect(cfg.paths.db_path, read_only=True)
    try:
        configure_pragmas(conn, cfg)
        rows = conn.execute(
            """
            SELECT ticker, raw_metadata, sector, subsector, industry, listing_board
            FROM companies
            """
        ).fetchall()
    finally:
        conn.close()

    updates: list[tuple[str, str, str, str, str, str]] = []
    for row in rows:
        needs_update = False
        try:
            meta = json.loads(row["raw_metadata"]) if row["raw_metadata"] else {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        sector = row["sector"] or "unknown"
        subsector = row["subsector"] or "unknown"
        industry = row["industry"] or "unknown"
        listing_board = row["listing_board"] or ""
        if sector in ("unknown", "") and (meta.get("sector") or meta.get("sub_sector")):
            sector = meta.get("sector") or meta.get("sub_sector")
            needs_update = True
        if subsector in ("unknown", "") and meta.get("sub_sector"):
            subsector = meta["sub_sector"]
            needs_update = True
        if industry in ("unknown", "") and meta.get("industry"):
            industry = meta["industry"]
            needs_update = True
        if not listing_board and meta.get("listing_board"):
            listing_board = meta["listing_board"]
            needs_update = True
        if needs_update:
            updates.append((sector, subsector, industry, listing_board, row["ticker"], row["ticker"]))

    if not updates:
        print("backfill-sectors: all companies already have sector data")
        return 0

    def _apply(conn):
        for sector, subsector, industry, listing_board, _ticker, ticker in updates:
            conn.execute(
                """
                UPDATE companies
                SET sector = ?, subsector = ?, industry = ?, listing_board = ?
                WHERE ticker = ?
                """,
                (sector, subsector, industry, listing_board, ticker),
            )
        return len(updates)

    with WriterQueue(cfg) as wq:
        count = wq.submit(_apply)
    print(f"backfill-sectors: updated {count} companies with sector data from raw_metadata")
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

    p_backfill_sectors = sub.add_parser(
        "backfill-sectors",
        help="Populate sector/subsector from existing raw_metadata in companies table",
    )
    p_backfill_sectors.set_defaults(func=cmd_backfill_sectors)
