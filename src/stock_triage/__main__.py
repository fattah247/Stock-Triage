"""CLI entrypoint: `python -m stock_triage <command>`."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .config import load_config
from .db import WriterQueue, run_migrations
from .dashboard import export_dashboard, serve_dashboard
from .download import run_downloads
from .extract import load_extract_status, run_extract
from .hardening import load_backlog, run_backfill, run_sqlite_benchmark
from .parse import load_parse_status, run_parse
from .rules import load_rule_status, run_rules
from .summary import PROMPT_VERSION, build_summary_context, generate_summary, validate_summary_file
from .alerts import run_alerts
from .canary import load_canary_status, run_canary
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


def cmd_summary_context(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    context = build_summary_context(cfg, document_id=args.document_id)
    print(json.dumps(context.__dict__, indent=2, sort_keys=True))
    return 0


def cmd_summary_validate(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    result = validate_summary_file(
        cfg,
        document_id=args.document_id,
        input_path=Path(args.input_file),
        model_name=args.model_name,
        prompt_version=args.prompt_version,
    )
    print(
        "summary-validate: "
        f"analysis_run_id={result.analysis_run_id} "
        f"schema_valid={1 if result.schema_valid else 0} "
        f"evidence_link_valid={1 if result.evidence_link_valid else 0} "
        f"errors={','.join(result.errors) if result.errors else 'none'}"
    )
    return 0


def cmd_summary_generate(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    result = generate_summary(
        cfg,
        document_id=args.document_id,
        prompt_version=args.prompt_version,
    )
    print(
        "summary-generate: "
        f"analysis_run_id={result.analysis_run_id} "
        f"schema_valid={1 if result.schema_valid else 0} "
        f"evidence_link_valid={1 if result.evidence_link_valid else 0} "
        f"errors={','.join(result.errors) if result.errors else 'none'}"
    )
    return 0


def cmd_dashboard_export(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    result = export_dashboard(
        cfg,
        output_dir=Path(args.output_dir) if args.output_dir else None,
    )
    print(
        "dashboard-export: "
        f"html={result.html_path} "
        f"metrics_csv={result.metrics_csv_path} "
        f"findings_csv={result.findings_csv_path} "
        f"issuers={result.issuer_count} "
        f"documents={result.document_count} "
        f"findings={result.finding_count}"
    )
    return 0


def cmd_dashboard_serve(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    host = args.host or cfg.dashboard.host
    port = args.port or cfg.dashboard.port
    print(f"dashboard-serve: url=http://{host}:{port}")
    serve_dashboard(
        cfg,
        host=host,
        port=port,
        open_browser=args.open_browser,
    )
    return 0


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
    p_summary_context = sub.add_parser("summary-context", help="Build curated facts and evidence for local summary prompts")
    p_summary_context.add_argument("document_id", help="Document id to summarize")
    p_summary_context.set_defaults(func=cmd_summary_context)
    p_summary_validate = sub.add_parser("summary-validate", help="Validate and persist a model output against Appendix C")
    p_summary_validate.add_argument("document_id", help="Document id the summary refers to")
    p_summary_validate.add_argument("--input-file", required=True, help="Path to the model output JSON file")
    p_summary_validate.add_argument("--model-name", required=True, help="Local model name used to produce the output")
    p_summary_validate.add_argument(
        "--prompt-version",
        default=PROMPT_VERSION,
        help="Prompt version identifier for the validated output",
    )
    p_summary_validate.set_defaults(func=cmd_summary_validate)
    p_summary_generate = sub.add_parser("summary-generate", help="Generate and validate a summary through the configured local LLM")
    p_summary_generate.add_argument("document_id", help="Document id to summarize")
    p_summary_generate.add_argument(
        "--prompt-version",
        default=PROMPT_VERSION,
        help="Prompt version identifier for the generated output",
    )
    p_summary_generate.set_defaults(func=cmd_summary_generate)
    p_dashboard_export = sub.add_parser("dashboard-export", help="Render a local HTML dashboard and CSV exports")
    p_dashboard_export.add_argument("--output-dir", help="Optional output directory for dashboard artifacts")
    p_dashboard_export.set_defaults(func=cmd_dashboard_export)
    p_dashboard_serve = sub.add_parser("dashboard-serve", help="Serve the local dashboard with action buttons")
    p_dashboard_serve.add_argument("--host", help="Override dashboard bind host")
    p_dashboard_serve.add_argument("--port", type=int, help="Override dashboard bind port")
    p_dashboard_serve.add_argument(
        "--open-browser",
        action="store_true",
        help="Open the dashboard in the default browser after starting the server",
    )
    p_dashboard_serve.set_defaults(func=cmd_dashboard_serve)
    p_alerts_run = sub.add_parser("alerts-run", help="Evaluate watchlist alerts and emit local notifications")
    p_alerts_run.set_defaults(func=cmd_alerts_run)
    p_canary_run = sub.add_parser("canary-run", help="Run source healthchecks and persist canary snapshots")
    p_canary_run.add_argument(
        "--source",
        choices=["idx_company_universe", "idx_disclosures", "idx_financial_reports", "all"],
        default="all",
        help="Optional source filter for canary checks",
    )
    p_canary_run.set_defaults(func=cmd_canary_run)
    p_canary_status = sub.add_parser("canary-status", help="Show the latest persisted canary result per source")
    p_canary_status.set_defaults(func=cmd_canary_status)
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
    p_benchmark_sqlite = sub.add_parser("benchmark-sqlite", help="Measure SQLite writer throughput and queue latency")
    p_benchmark_sqlite.add_argument("--batches", type=int, default=20, help="Number of writer batches to execute")
    p_benchmark_sqlite.add_argument("--rows-per-batch", type=int, default=100, help="Rows inserted per batch")
    p_benchmark_sqlite.set_defaults(func=cmd_benchmark_sqlite)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
