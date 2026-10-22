"""Phase 9 hardening bootstrap: bounded backfills, backlog reporting, and SQLite benchmarks."""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass
from datetime import date

from ..config import Config
from ..db import WriterQueue, connect, configure_pragmas, run_migrations
from ..discovery import persist_companies, persist_document_candidates, record_historical_discovery
from ..download import run_downloads
from ..extract import run_extract
from ..parse import run_parse
from ..rules import run_rules
from ..sources import DiscoveryWindow
from ..sources.adapters import IDXCompanyUniverseAdapter, IDXDisclosureAdapter, IDXFinancialReportAdapter


@dataclass(frozen=True)
class BackfillYearResult:
    year: int
    discovered: int
    inserted: int
    updated: int
    downloaded: int
    parsed: int
    extracted: int
    analyzed: int


@dataclass(frozen=True)
class BackfillRunResult:
    rows: list[BackfillYearResult]


@dataclass(frozen=True)
class BacklogRow:
    source_name: str
    discovered: int
    downloaded: int
    parsed: int
    extracted: int
    analyzed: int
    failed: int


@dataclass(frozen=True)
class BacklogReport:
    rows: list[BacklogRow]


@dataclass(frozen=True)
class SQLiteBenchmarkResult:
    batches: int
    rows_per_batch: int
    total_rows: int
    total_seconds: float
    rows_per_second: float
    avg_submit_ms: float
    p95_submit_ms: float


def run_backfill(
    cfg: Config,
    *,
    source: str,
    start_year: int,
    end_year: int,
    discover_limit: int | None = None,
    download_limit: int = 50,
    parse_limit: int = 50,
    extract_limit: int = 50,
    rules_limit: int = 50,
) -> BackfillRunResult:
    cfg.ensure_dirs()
    rows: list[BackfillYearResult] = []
    for year in range(start_year, end_year + 1):
        year_discover_limit = discover_limit
        if year_discover_limit is None and source != "company_universe":
            year_discover_limit = max(download_limit, parse_limit, extract_limit, rules_limit)
        discover_inserted = 0
        discover_updated = 0
        discovered = 0
        with WriterQueue(cfg) as wq:
            wq.submit(run_migrations)
            for adapter in _adapters_for_source(source):
                if adapter.name == "idx_company_universe" and year != end_year:
                    continue
                window = DiscoveryWindow(start=date(year, 1, 1), end=date(year, 12, 31))
                candidates = list(adapter.discover(window))
                discovered += len(candidates)
                persisted_candidates = candidates[:year_discover_limit] if year_discover_limit is not None else candidates
                if adapter.source_class.value == "company_universe":
                    persist = wq.submit(lambda conn, c=persisted_candidates: persist_companies(conn, c))
                else:
                    persist = wq.submit(lambda conn, c=persisted_candidates: persist_document_candidates(conn, c))
                discover_inserted += persist.inserted
                discover_updated += persist.updated
                wq.submit(
                    lambda conn, a=adapter, w=window, cnt=len(persisted_candidates): record_historical_discovery(
                        conn,
                        source_name=a.name,
                        source_class=a.source_class,
                        window=w,
                        raw_state={
                            "candidate_count": cnt,
                            "discovered_total": len(candidates),
                            "mode": "phase9_backfill",
                        },
                    )
                )

        download_result = run_downloads(
            cfg,
            source_name=_stage_source_name(source),
            limit=download_limit,
        )
        parse_result = run_parse(
            cfg,
            source_name=_stage_source_name(source),
            limit=parse_limit,
        )
        extract_result = run_extract(
            cfg,
            source_name=_stage_source_name(source),
            limit=extract_limit,
        )
        rules_result = run_rules(
            cfg,
            source_name=_stage_source_name(source),
            limit=rules_limit,
        )
        rows.append(
            BackfillYearResult(
                year=year,
                discovered=discovered,
                inserted=discover_inserted,
                updated=discover_updated,
                downloaded=download_result.inserted,
                parsed=parse_result.parsed,
                extracted=extract_result.extracted + extract_result.partial,
                analyzed=rules_result.analyzed,
            )
        )
    return BackfillRunResult(rows=rows)


def load_backlog(cfg: Config) -> BacklogReport:
    cfg.ensure_dirs()
    conn = connect(cfg.paths.db_path, read_only=True)
    try:
        configure_pragmas(conn, cfg)
        discovered_rows = conn.execute(
            """
            SELECT
                dc.source_name,
                COUNT(*) AS discovered
            FROM document_candidates dc
            LEFT JOIN documents d
              ON d.source_name = dc.source_name
             AND d.source_record_id = dc.source_record_id
            WHERE d.document_id IS NULL
            GROUP BY dc.source_name
            """
        ).fetchall()
        discovered_by_source = {
            row["source_name"]: row["discovered"] or 0
            for row in discovered_rows
        }
        rows = conn.execute(
            """
            SELECT
                source_name,
                SUM(CASE WHEN process_state = 'downloaded' THEN 1 ELSE 0 END) AS downloaded,
                SUM(CASE WHEN process_state = 'parsed' THEN 1 ELSE 0 END) AS parsed,
                SUM(CASE WHEN process_state = 'extracted' THEN 1 ELSE 0 END) AS extracted,
                SUM(CASE WHEN process_state = 'analyzed' THEN 1 ELSE 0 END) AS analyzed,
                SUM(CASE WHEN process_state = 'failed' THEN 1 ELSE 0 END) AS failed
            FROM documents
            GROUP BY source_name
            ORDER BY source_name
            """
        ).fetchall()
        row_by_source = {
            row["source_name"]: BacklogRow(
                source_name=row["source_name"],
                discovered=discovered_by_source.get(row["source_name"], 0),
                downloaded=row["downloaded"] or 0,
                parsed=row["parsed"] or 0,
                extracted=row["extracted"] or 0,
                analyzed=row["analyzed"] or 0,
                failed=row["failed"] or 0,
            )
            for row in rows
        }
        for source_name, discovered in discovered_by_source.items():
            if source_name in row_by_source:
                continue
            row_by_source[source_name] = BacklogRow(
                source_name=source_name,
                discovered=discovered,
                downloaded=0,
                parsed=0,
                extracted=0,
                analyzed=0,
                failed=0,
            )
        return BacklogReport(rows=sorted(row_by_source.values(), key=lambda row: row.source_name))
    finally:
        conn.close()


def run_sqlite_benchmark(
    cfg: Config,
    *,
    batches: int = 20,
    rows_per_batch: int = 100,
) -> SQLiteBenchmarkResult:
    cfg.ensure_dirs()
    latencies: list[float] = []
    total_rows = batches * rows_per_batch
    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)
        wq.submit(lambda conn: conn.execute("CREATE TEMP TABLE IF NOT EXISTS benchmark_writes (id TEXT PRIMARY KEY, payload TEXT NOT NULL)"))
        start = time.perf_counter()
        for batch_idx in range(batches):
            rows = [
                (f"{batch_idx}:{row_idx}", f"payload-{batch_idx}-{row_idx}")
                for row_idx in range(rows_per_batch)
            ]
            batch_start = time.perf_counter()
            wq.submit(lambda conn, r=rows: conn.executemany("INSERT INTO benchmark_writes (id, payload) VALUES (?, ?)", r))
            latencies.append(time.perf_counter() - batch_start)
        total_seconds = time.perf_counter() - start
        wq.submit(lambda conn: conn.execute("DROP TABLE benchmark_writes"))

    p95_index = max(0, min(len(latencies) - 1, round(0.95 * (len(latencies) - 1))))
    return SQLiteBenchmarkResult(
        batches=batches,
        rows_per_batch=rows_per_batch,
        total_rows=total_rows,
        total_seconds=total_seconds,
        rows_per_second=(total_rows / total_seconds) if total_seconds else float("inf"),
        avg_submit_ms=statistics.mean(latencies) * 1000 if latencies else 0.0,
        p95_submit_ms=sorted(latencies)[p95_index] * 1000 if latencies else 0.0,
    )


def _adapters_for_source(source: str):
    mapping = {
        "company_universe": IDXCompanyUniverseAdapter,
        "disclosure": IDXDisclosureAdapter,
        "financial_report": IDXFinancialReportAdapter,
    }
    if source == "all":
        return [cls() for cls in mapping.values()]
    return [mapping[source]()]


def _stage_source_name(source: str) -> str | None:
    return {
        "all": None,
        "company_universe": None,
        "disclosure": "idx_disclosures",
        "financial_report": "idx_financial_reports",
    }[source]
