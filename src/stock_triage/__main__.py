"""CLI entrypoint: `python -m stock_triage <command>`."""

from __future__ import annotations

import argparse
import sys
from datetime import date

from .config import load_config
from .db import WriterQueue, run_migrations
from .download import run_downloads
from .extract import load_extract_status, run_extract
from .parse import load_parse_status, run_parse
from .rules import load_rule_status, run_rules
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
        retry_failed_only=args.retry_failed,
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


def cmd_parse_status(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    source_name = None if args.source == "all" else args.source
    report = load_parse_status(
        cfg,
        source_name=source_name,
        failure_limit=args.failure_limit,
    )
    for row in report.rows:
        print(
            f"{row.source_name}: total={row.total} downloaded={row.downloaded} "
            f"parsed={row.parsed} failed={row.failed} "
            f"ocr_partial={row.ocr_partial} ocr_full={row.ocr_full}"
        )
    if report.recent_failures:
        print("recent_failures:")
        for failure in report.recent_failures:
            print(
                f"{failure.source_name} {failure.document_id} "
                f"record={failure.source_record_id} "
                f"error={failure.process_error_code or 'unknown'} "
                f"strategy={failure.parse_strategy or 'unknown'} "
                f"updated_at={failure.updated_at}"
            )
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    source_name = None if args.source == "all" else args.source
    result = run_extract(
        cfg,
        source_name=source_name,
        limit=args.limit,
        include_extracted=args.include_extracted,
        retry_failed_only=args.retry_failed,
    )
    print(
        "extract: "
        f"selected={result.selected} "
        f"extracted={result.extracted} "
        f"partial={result.partial} "
        f"failed={result.failed} "
        f"metrics={result.metrics} "
        f"events={result.events}"
    )
    return 0


def cmd_extract_status(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    source_name = None if args.source == "all" else args.source
    report = load_extract_status(
        cfg,
        source_name=source_name,
        failure_limit=args.failure_limit,
    )
    for row in report.rows:
        completeness = f"{row.completeness_pct:.2f}%" if row.completeness_pct is not None else "n/a"
        print(
            f"{row.source_name}: total={row.total} pending={row.pending} "
            f"extracted={row.extracted} partial={row.partial} failed={row.failed} "
            f"metrics={row.metrics} events={row.events} completeness={completeness}"
        )
    if report.recent_failures:
        print("recent_failures:")
        for failure in report.recent_failures:
            print(
                f"{failure.source_name} {failure.document_id} "
                f"record={failure.source_record_id} status={failure.status} "
                f"error={failure.error_code or 'unknown'} "
                f"updated_at={failure.updated_at}"
            )
    return 0


def cmd_rules(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    source_name = None if args.source == "all" else args.source
    result = run_rules(
        cfg,
        source_name=source_name,
        limit=args.limit,
        include_analyzed=args.include_analyzed,
    )
    print(
        "rules: "
        f"selected={result.selected} "
        f"analyzed={result.analyzed} "
        f"triggered={result.triggered}"
    )
    return 0


def cmd_rules_status(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    source_name = None if args.source == "all" else args.source
    report = load_rule_status(
        cfg,
        source_name=source_name,
        trigger_limit=args.trigger_limit,
    )
    for row in report.rows:
        print(
            f"{row.source_name}: total={row.total} pending={row.pending} "
            f"analyzed={row.analyzed} triggered={row.triggered}"
        )
    if report.recent_triggers:
        print("recent_triggers:")
        for trigger in report.recent_triggers:
            print(
                f"{trigger.source_name} {trigger.document_id} "
                f"record={trigger.source_record_id} rule={trigger.rule_name} "
                f"severity={trigger.severity} evaluated_at={trigger.evaluated_at}"
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
    p_parse.add_argument(
        "--retry-failed",
        action="store_true",
        help="Retry only documents currently marked failed",
    )
    p_parse.set_defaults(func=cmd_parse)
    p_parse_status = sub.add_parser("parse-status", help="Show parse-state counts and recent failures")
    p_parse_status.add_argument(
        "--source",
        choices=["idx_disclosures", "idx_financial_reports", "all"],
        default="all",
        help="Optional source filter for parsed documents",
    )
    p_parse_status.add_argument(
        "--failure-limit",
        type=int,
        default=10,
        help="Maximum number of recent failed documents to display",
    )
    p_parse_status.set_defaults(func=cmd_parse_status)
    p_extract = sub.add_parser("extract", help="Normalize facts and detect events from parsed documents")
    p_extract.add_argument(
        "--source",
        choices=["idx_disclosures", "idx_financial_reports", "all"],
        default="all",
        help="Optional source filter for parsed documents",
    )
    p_extract.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Maximum number of parsed documents to extract",
    )
    p_extract.add_argument(
        "--include-extracted",
        action="store_true",
        help="Reconsider documents already marked extracted or analyzed",
    )
    p_extract.add_argument(
        "--retry-failed",
        action="store_true",
        help="Retry only documents currently marked failed",
    )
    p_extract.set_defaults(func=cmd_extract)
    p_extract_status = sub.add_parser("extract-status", help="Show extraction-state counts and recent failures")
    p_extract_status.add_argument(
        "--source",
        choices=["idx_disclosures", "idx_financial_reports", "all"],
        default="all",
        help="Optional source filter for extracted documents",
    )
    p_extract_status.add_argument(
        "--failure-limit",
        type=int,
        default=10,
        help="Maximum number of recent extraction failures to display",
    )
    p_extract_status.set_defaults(func=cmd_extract_status)
    p_rules = sub.add_parser("rules", help="Evaluate deterministic rule flags over extracted facts")
    p_rules.add_argument(
        "--source",
        choices=["idx_disclosures", "idx_financial_reports", "all"],
        default="all",
        help="Optional source filter for extracted documents",
    )
    p_rules.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Maximum number of extracted documents to analyze",
    )
    p_rules.add_argument(
        "--include-analyzed",
        action="store_true",
        help="Re-evaluate documents already marked analyzed",
    )
    p_rules.set_defaults(func=cmd_rules)
    p_rules_status = sub.add_parser("rules-status", help="Show rule-engine coverage and recent triggers")
    p_rules_status.add_argument(
        "--source",
        choices=["idx_disclosures", "idx_financial_reports", "all"],
        default="all",
        help="Optional source filter for analyzed documents",
    )
    p_rules_status.add_argument(
        "--trigger-limit",
        type=int,
        default=10,
        help="Maximum number of recent triggered rules to display",
    )
    p_rules_status.set_defaults(func=cmd_rules_status)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
