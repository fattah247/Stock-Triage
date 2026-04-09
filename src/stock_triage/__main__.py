"""CLI entrypoint: `python -m stock_triage <command>`."""

from __future__ import annotations

import argparse
import sys
from datetime import date

from .config import load_config
from .db import WriterQueue, run_migrations
from .discovery import (
    load_watermark,
    persist_companies,
    persist_document_candidates,
    persist_watermark,
)
from .logging_setup import get_logger, setup_logging
from .sources import DiscoveryWindow
from .sources.adapters import (
    IDXCompanyUniverseAdapter,
    IDXDisclosureAdapter,
    IDXFinancialReportAdapter,
)


def cmd_init(_args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)
    log = get_logger("stock_triage.init")
    log.info("ensuring directories", extra={"data_dir": str(cfg.paths.data_dir)})
    with WriterQueue(cfg) as wq:
        applied = wq.submit(run_migrations)
    log.info("migrations applied", extra={"versions": applied, "db": str(cfg.paths.db_path)})
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)
    log = get_logger("stock_triage.discover")

    requested_window = _window_from_args(args)
    targets = _adapters_for_source(args.source)
    summaries: list[tuple[str, str]] = []

    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
        for adapter in targets:
            window = _effective_window(
                adapter=adapter,
                requested_window=requested_window,
                persisted_window=wq.submit(lambda conn, name=adapter.name: load_watermark(conn, name)),
            )
            candidates = list(adapter.discover(window))
            watermark_window = adapter.watermark_window(
                requested_window=window,
                candidates=candidates,
            )
            if adapter.source_class.value == "company_universe":
                result = wq.submit(lambda conn, c=candidates: persist_companies(conn, c))
            else:
                result = wq.submit(lambda conn, c=candidates: persist_document_candidates(conn, c))
            wq.submit(
                lambda conn, a=adapter, w=watermark_window, cnt=len(candidates): persist_watermark(
                    conn,
                    source_name=a.name,
                    source_class=a.source_class,
                    window=w,
                    raw_state={"candidate_count": cnt},
                )
            )
            summary = (
                f"{adapter.name}: candidates={len(candidates)} "
                f"inserted={result.inserted} updated={result.updated} skipped={result.skipped}"
            )
            summaries.append((adapter.name, summary))
            log.info(
                "discovery persisted",
                extra={
                    "source_name": adapter.name,
                    "source_class": adapter.source_class.value,
                    "candidate_count": len(candidates),
                    "inserted": result.inserted,
                    "updated": result.updated,
                    "skipped": result.skipped,
                },
            )

    for _, summary in summaries:
        print(summary)
    return 0


def _adapters_for_source(source: str):
    mapping = {
        "company_universe": IDXCompanyUniverseAdapter,
        "disclosure": IDXDisclosureAdapter,
        "financial_report": IDXFinancialReportAdapter,
    }
    if source == "all":
        return [cls() for cls in mapping.values()]
    return [mapping[source]()]


def _window_from_args(args: argparse.Namespace) -> DiscoveryWindow | None:
    if args.year is None and not args.start and not args.end and not args.cursor:
        return None
    if args.year is not None:
        return DiscoveryWindow(
            start=date(args.year, 1, 1),
            end=date(args.year, 12, 31),
            cursor=args.cursor,
        )
    return DiscoveryWindow(
        start=date.fromisoformat(args.start) if args.start else None,
        end=date.fromisoformat(args.end) if args.end else None,
        cursor=args.cursor,
    )


def _effective_window(*, adapter, requested_window: DiscoveryWindow | None, persisted_window: DiscoveryWindow | None) -> DiscoveryWindow:
    if requested_window is not None:
        return requested_window
    if persisted_window is not None:
        return adapter.resume_window(persisted_window)
    return adapter.default_window()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stock_triage")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init", help="Create directories and apply DB migrations")
    p_init.set_defaults(func=cmd_init)
    p_discover = sub.add_parser("discover", help="Run browser-backed discovery and persist results")
    p_discover.add_argument(
        "source",
        choices=["company_universe", "disclosure", "financial_report", "all"],
        help="Which source to discover and persist",
    )
    p_discover.add_argument("--start", help="Discovery window start date (YYYY-MM-DD)")
    p_discover.add_argument("--end", help="Discovery window end date (YYYY-MM-DD)")
    p_discover.add_argument("--year", type=int, help="Convenience year selector for financial reports")
    p_discover.add_argument("--cursor", help="Optional adapter cursor")
    p_discover.set_defaults(func=cmd_discover)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
