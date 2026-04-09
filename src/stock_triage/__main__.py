"""CLI entrypoint: `python -m stock_triage <command>`."""

from __future__ import annotations

import argparse
import sys
from datetime import date

from .config import load_config
from .db import WriterQueue, run_migrations
from .download import run_downloads
from .parse import run_parse
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


def cmd_download(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    source_name = None if args.source == "all" else args.source
    result = run_downloads(
        cfg,
        source_name=source_name,
        limit=args.limit,
        include_existing=args.include_existing,
        retry_failed=args.retry_failed,
        include_quarantined=args.include_quarantined,
    )
    print(
        "download: "
        f"selected={result.selected} "
        f"fetched={result.fetched} "
        f"inserted={result.inserted} "
        f"skipped={result.skipped} "
        f"not_modified={result.not_modified} "
        f"failed={result.failed}"
    )
    return 0


def cmd_parse(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    source_name = None if args.source == "all" else args.source
    result = run_parse(
        cfg,
        source_name=source_name,
        limit=args.limit,
        include_parsed=args.include_parsed,
    )
    print(
        "parse: "
        f"selected={result.selected} "
        f"parsed={result.parsed} "
        f"failed={result.failed} "
        f"ocr_partial={result.ocr_partial} "
        f"ocr_full={result.ocr_full} "
        f"chunks={result.chunks}"
    )
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
    p_download = sub.add_parser("download", help="Download candidate attachments into the file store")
    p_download.add_argument(
        "--source",
        choices=["idx_disclosures", "idx_financial_reports", "all"],
        default="all",
        help="Optional source filter for queued document candidates",
    )
    p_download.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Maximum number of attachment candidates to process",
    )
    p_download.add_argument(
        "--include-existing",
        action="store_true",
        help="Reconsider candidates that already have at least one downloaded document",
    )
    p_download.add_argument(
        "--retry-failed",
        action="store_true",
        help="Ignore backoff windows and target failed download resources immediately",
    )
    p_download.add_argument(
        "--include-quarantined",
        action="store_true",
        help="Reconsider resources previously quarantined for corrupt payloads",
    )
    p_download.set_defaults(func=cmd_download)
    p_parse = sub.add_parser("parse", help="Parse downloaded PDFs into page-aware text chunks")
    p_parse.add_argument(
        "--source",
        choices=["idx_disclosures", "idx_financial_reports", "all"],
        default="all",
        help="Optional source filter for downloaded documents",
    )
    p_parse.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Maximum number of downloaded documents to parse",
    )
    p_parse.add_argument(
        "--include-parsed",
        action="store_true",
        help="Reconsider documents already marked parsed or failed",
    )
    p_parse.set_defaults(func=cmd_parse)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
