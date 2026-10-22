"""Template and static-asset helpers for the local dashboard."""

from __future__ import annotations

import html
import json
import shutil
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"
STATIC_ASSET_NAMES = ("app.css", "app.js")


def render_dashboard_html(
    *,
    title: str,
    issuers: list[dict[str, object]],
    market_monitor: dict[str, object],
    documents: list[dict[str, object]],
    metrics: list[dict[str, object]],
    findings: list[dict[str, object]],
    revisions: list[dict[str, object]],
    company_snapshots: list[dict[str, object]],
    sector_rollups: list[dict[str, object]],
    inbox_rows: list[dict[str, object]],
    digest_rows: list[dict[str, object]],
    review_rows: list[dict[str, object]],
    intelligence_kpis: dict[str, object],
    backlog_rows: list[dict[str, object]],
    canary_rows: list[dict[str, object]],
    metrics_href: str,
    findings_href: str,
    action_endpoint: str | None = None,
    snapshot_endpoint: str | None = None,
    artifact_endpoint: str | None = None,
    action_history: list[dict[str, object]] | None = None,
    run_sequences: dict[str, list[dict[str, object]]] | None = None,
    static_root: str = "static",
) -> str:
    template = (TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")
    bootstrap = json.dumps(
        {
            "payload": {
                "issuers": issuers,
                "market_monitor": market_monitor,
                "documents": documents,
                "metrics": metrics,
                "findings": findings,
                "revisions": revisions,
                "company_snapshots": company_snapshots,
                "sector_rollups": sector_rollups,
                "inbox_rows": inbox_rows,
                "digest_rows": digest_rows,
                "review_rows": review_rows,
                "intelligence_kpis": intelligence_kpis,
                "backlog_rows": backlog_rows,
                "canary_rows": canary_rows,
                "action_history": action_history or [],
            },
            "title": title,
            "metricsHref": metrics_href,
            "findingsHref": findings_href,
            "actionEndpoint": action_endpoint,
            "snapshotEndpoint": snapshot_endpoint,
            "artifactEndpoint": artifact_endpoint,
            "runSequences": run_sequences or {},
        },
        ensure_ascii=True,
    )
    # The payload carries filing-derived text; escape markup-significant
    # characters so a "</script>" inside a claim cannot end the JSON block.
    bootstrap = bootstrap.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return (
        template
        .replace("__TITLE__", html.escape(title))
        .replace("__METRICS_HREF__", metrics_href)
        .replace("__FINDINGS_HREF__", findings_href)
        .replace("__ISSUER_COUNT__", str(len(issuers)))
        .replace("__DOCUMENT_COUNT__", str(len(documents)))
        .replace("__FINDING_COUNT__", str(len(findings)))
        .replace("__BOOTSTRAP_JSON__", bootstrap)
        .replace("__STATIC_ROOT__", static_root.rstrip("/"))
    )


def write_dashboard_static_assets(output_dir: Path) -> None:
    static_target = output_dir / "static"
    static_target.mkdir(parents=True, exist_ok=True)
    for name in STATIC_ASSET_NAMES:
        shutil.copyfile(STATIC_DIR / name, static_target / name)


def read_static_asset(asset_name: str) -> bytes:
    return (STATIC_DIR / asset_name).read_bytes()
