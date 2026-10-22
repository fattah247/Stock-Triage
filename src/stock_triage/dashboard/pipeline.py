"""Dashboard export glue."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from ..config import Config
from .assets import render_dashboard_html, write_dashboard_static_assets
from .snapshot import DashboardSnapshot, load_dashboard_snapshot


METRICS_CSV_FIELDS = (
    "ticker",
    "metric_name",
    "value_normalized",
    "currency",
    "fiscal_year",
    "fiscal_period",
    "period_end_date",
    "document_id",
)
FINDINGS_CSV_FIELDS = (
    "document_id",
    "ticker",
    "source_name",
    "finding_type",
    "name",
    "claim",
    "summary",
    "evidence",
    "local_path",
    "original_url",
    "finished_at",
)


@dataclass(frozen=True)
class DashboardExportResult:
    html_path: Path
    metrics_csv_path: Path
    findings_csv_path: Path
    issuer_count: int
    document_count: int
    finding_count: int


def export_dashboard(
    cfg: Config,
    *,
    output_dir: Path | None = None,
    document_limit: int = 50,
    finding_limit: int = 50,
) -> DashboardExportResult:
    cfg.ensure_dirs()
    target_dir = output_dir or cfg.dashboard.output_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    snapshot = load_dashboard_snapshot(
        cfg,
        document_limit=document_limit,
        finding_limit=finding_limit,
    )

    metrics_csv_path = target_dir / "metrics.csv"
    findings_csv_path = target_dir / "findings.csv"
    html_path = target_dir / "index.html"

    _write_metrics_csv(metrics_csv_path, snapshot.metrics)
    _write_findings_csv(findings_csv_path, snapshot.findings)
    write_dashboard_static_assets(target_dir)
    html_path.write_text(
        render_dashboard_html(
            title=cfg.dashboard.title,
            issuers=snapshot.issuers,
            market_monitor=snapshot.market_monitor,
            documents=snapshot.documents,
            metrics=snapshot.metrics,
            findings=snapshot.findings,
            revisions=snapshot.revisions,
            company_snapshots=snapshot.company_snapshots,
            sector_rollups=snapshot.sector_rollups,
            inbox_rows=snapshot.inbox_rows,
            digest_rows=snapshot.digest_rows,
            review_rows=snapshot.review_rows,
            intelligence_kpis=snapshot.intelligence_kpis,
            backlog_rows=snapshot.backlog_rows,
            canary_rows=snapshot.canary_rows,
            metrics_href=metrics_csv_path.name,
            findings_href=findings_csv_path.name,
            static_root="static",
        ),
        encoding="utf-8",
    )

    return DashboardExportResult(
        html_path=html_path,
        metrics_csv_path=metrics_csv_path,
        findings_csv_path=findings_csv_path,
        issuer_count=len(snapshot.issuers),
        document_count=len(snapshot.documents),
        finding_count=len(snapshot.findings),
    )


def _write_metrics_csv(path: Path, metrics: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=METRICS_CSV_FIELDS,
        )
        writer.writeheader()
        writer.writerows(metrics)


def _write_findings_csv(path: Path, findings: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=FINDINGS_CSV_FIELDS,
        )
        writer.writeheader()
        writer.writerows(findings)


__all__ = [
    "DashboardExportResult",
    "DashboardSnapshot",
    "export_dashboard",
    "load_dashboard_snapshot",
    "render_dashboard_html",
]
