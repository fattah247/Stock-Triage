"""Phase 8 dashboard export: local HTML plus CSV snapshots."""

from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..canary import load_canary_status
from ..config import Config
from ..db import WriterQueue, connect, configure_pragmas, run_migrations
from ..hardening import load_backlog


@dataclass(frozen=True)
class DashboardExportResult:
    html_path: Path
    metrics_csv_path: Path
    findings_csv_path: Path
    issuer_count: int
    document_count: int
    finding_count: int


@dataclass(frozen=True)
class DashboardSnapshot:
    issuers: list[dict[str, object]]
    documents: list[dict[str, object]]
    metrics: list[dict[str, object]]
    findings: list[dict[str, object]]
    revisions: list[dict[str, object]]
    backlog_rows: list[dict[str, object]]
    canary_rows: list[dict[str, object]]


def load_dashboard_snapshot(
    cfg: Config,
    *,
    document_limit: int = 50,
    finding_limit: int = 50,
) -> DashboardSnapshot:
    cfg.ensure_dirs()

    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)

    conn = connect(cfg.paths.db_path, read_only=True)
    try:
        configure_pragmas(conn, cfg)
        snapshot = DashboardSnapshot(
            issuers=_load_issuers(conn),
            documents=_load_latest_documents(conn, limit=document_limit),
            metrics=_load_metric_snapshot(conn),
            findings=_load_findings(conn, limit=finding_limit),
            revisions=_load_revision_history(conn, limit=50),
            backlog_rows=[],
            canary_rows=[],
        )
    finally:
        conn.close()

    backlog = load_backlog(cfg)
    canary = load_canary_status(cfg)
    return DashboardSnapshot(
        issuers=snapshot.issuers,
        documents=snapshot.documents,
        metrics=snapshot.metrics,
        findings=snapshot.findings,
        revisions=snapshot.revisions,
        backlog_rows=[row.__dict__ for row in backlog.rows],
        canary_rows=[row.__dict__ for row in canary.rows],
    )


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
    html_path.write_text(
        render_dashboard_html(
            title=cfg.dashboard.title,
            issuers=snapshot.issuers,
            documents=snapshot.documents,
            metrics=snapshot.metrics,
            findings=snapshot.findings,
            revisions=snapshot.revisions,
            backlog_rows=snapshot.backlog_rows,
            canary_rows=snapshot.canary_rows,
            metrics_href=metrics_csv_path.name,
            findings_href=findings_csv_path.name,
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


def _load_issuers(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT
            c.ticker,
            c.issuer_name,
            c.sector,
            c.subsector,
            c.active_status,
            c.last_seen_at,
            COUNT(d.document_id) AS document_count,
            MAX(d.updated_at) AS latest_document_at
        FROM companies c
        LEFT JOIN documents d ON d.ticker = c.ticker
        GROUP BY c.ticker, c.issuer_name, c.sector, c.subsector, c.active_status, c.last_seen_at
        ORDER BY COALESCE(MAX(d.updated_at), c.last_seen_at) DESC, c.ticker ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _load_latest_documents(conn: sqlite3.Connection, *, limit: int) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT
            d.document_id,
            d.source_name,
            d.source_record_id,
            d.ticker,
            d.document_type,
            d.process_state,
            d.retrieved_at,
            d.updated_at,
            d.local_path,
            d.original_url,
            d.version_group_id,
            COALESCE((
                SELECT COUNT(*)
                FROM rule_runs rr
                WHERE rr.document_id = d.document_id AND rr.triggered = 1
            ), 0) AS triggered_rules
        FROM documents d
        ORDER BY d.updated_at DESC, d.document_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def _load_metric_snapshot(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT
            mi.ticker,
            mi.metric_name,
            mi.value_normalized,
            mi.currency,
            mi.fiscal_year,
            mi.fiscal_period,
            mi.period_end_date,
            mi.document_id
        FROM metric_items mi
        JOIN documents d ON d.document_id = mi.document_id
        WHERE mi.ticker IS NOT NULL
        ORDER BY COALESCE(mi.period_end_date, '') DESC, mi.fiscal_year DESC, d.updated_at DESC
        """
    ).fetchall()
    latest: dict[tuple[str, str], dict[str, object]] = {}
    for row in rows:
        key = (row["ticker"], row["metric_name"])
        latest.setdefault(key, dict(row))
    return sorted(latest.values(), key=lambda item: (str(item["ticker"]), str(item["metric_name"])))


def _load_findings(conn: sqlite3.Connection, *, limit: int) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT
            ar.document_id,
            d.ticker,
            d.source_name,
            d.local_path,
            d.original_url,
            ar.output_json,
            ar.finished_at
        FROM analysis_runs ar
        JOIN documents d ON d.document_id = ar.document_id
        ORDER BY ar.finished_at DESC, ar.analysis_run_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    findings: list[dict[str, object]] = []
    for row in rows:
        payload = json.loads(row["output_json"])
        evidence_map = {
            item["id"]: item
            for item in payload.get("evidence_links", [])
        }
        summary = payload.get("summary", "")
        for item in payload.get("key_changes", []):
            findings.append(
                {
                    "document_id": row["document_id"],
                    "ticker": row["ticker"],
                    "source_name": row["source_name"],
                    "local_path": row["local_path"],
                    "original_url": row["original_url"],
                    "finding_type": "key_change",
                    "name": item.get("severity", "unknown"),
                    "claim": item.get("claim", ""),
                    "summary": summary,
                    "evidence": _evidence_preview(item.get("evidence_links", []), evidence_map),
                    "finished_at": row["finished_at"],
                }
            )
        for item in payload.get("risks", []):
            findings.append(
                {
                    "document_id": row["document_id"],
                    "ticker": row["ticker"],
                    "source_name": row["source_name"],
                    "local_path": row["local_path"],
                    "original_url": row["original_url"],
                    "finding_type": "risk",
                    "name": "risk",
                    "claim": item.get("claim", ""),
                    "summary": summary,
                    "evidence": _evidence_preview(item.get("evidence_links", []), evidence_map),
                    "finished_at": row["finished_at"],
                }
            )
        for item in payload.get("events", []):
            findings.append(
                {
                    "document_id": row["document_id"],
                    "ticker": row["ticker"],
                    "source_name": row["source_name"],
                    "local_path": row["local_path"],
                    "original_url": row["original_url"],
                    "finding_type": "event",
                    "name": item.get("event_type", ""),
                    "claim": item.get("event_type", ""),
                    "summary": summary,
                    "evidence": _evidence_preview(item.get("evidence_links", []), evidence_map),
                    "finished_at": row["finished_at"],
                }
            )
    return findings[:limit]


def _load_revision_history(conn: sqlite3.Connection, *, limit: int) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT
            version_group_id,
            source_name,
            source_record_id,
            ticker,
            COUNT(*) AS revision_count,
            MAX(updated_at) AS latest_revision_at
        FROM documents
        WHERE version_group_id IS NOT NULL
        GROUP BY version_group_id, source_name, source_record_id, ticker
        ORDER BY latest_revision_at DESC, version_group_id ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def _evidence_preview(span_ids: list[str], evidence_map: dict[str, dict[str, object]]) -> str:
    previews: list[str] = []
    for span_id in span_ids:
        entry = evidence_map.get(span_id)
        if entry is None:
            previews.append(span_id)
            continue
        previews.append(f"{entry['id']} p{entry['page_start']}: {entry.get('excerpt_text', '')[:120]}")
    return " | ".join(previews)


def _write_metrics_csv(path: Path, metrics: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "ticker",
                "metric_name",
                "value_normalized",
                "currency",
                "fiscal_year",
                "fiscal_period",
                "period_end_date",
                "document_id",
            ],
        )
        writer.writeheader()
        writer.writerows(metrics)


def _write_findings_csv(path: Path, findings: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
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
            ],
        )
        writer.writeheader()
        writer.writerows(findings)


def render_dashboard_html(
    *,
    title: str,
    issuers: list[dict[str, object]],
    documents: list[dict[str, object]],
    metrics: list[dict[str, object]],
    findings: list[dict[str, object]],
    revisions: list[dict[str, object]],
    backlog_rows: list[dict[str, object]],
    canary_rows: list[dict[str, object]],
    metrics_href: str,
    findings_href: str,
    action_endpoint: str | None = None,
    snapshot_endpoint: str | None = None,
    artifact_endpoint: str | None = None,
    action_history: list[dict[str, object]] | None = None,
) -> str:
    history = action_history or []
    payload = json.dumps(
        {
            "issuers": issuers,
            "documents": documents,
            "metrics": metrics,
            "findings": findings,
            "revisions": revisions,
            "backlog_rows": backlog_rows,
            "canary_rows": canary_rows,
            "action_history": history,
        },
        ensure_ascii=True,
    )
    action_endpoint_json = json.dumps(action_endpoint)
    snapshot_endpoint_json = json.dumps(snapshot_endpoint)
    artifact_endpoint_json = json.dumps(artifact_endpoint)
    action_panel = ""
    if action_endpoint and snapshot_endpoint:
        action_panel = """
    <section class="opsShell">
      <article class="panel panelDark">
        <div class="sectionEyebrow">Workflow</div>
        <h2>Control Panel</h2>
        <p class="lead">Run the common pipeline stages here instead of typing commands.</p>
        <div class="controlGrid">
          <button class="actionButton" data-action="discover_company">Discover Companies</button>
          <button class="actionButton" data-action="discover_disclosure">Discover Disclosures</button>
          <button class="actionButton" data-action="discover_financial">Discover Financials</button>
          <button class="actionButton" data-action="download_financial">Download Financials</button>
          <button class="actionButton" data-action="download_disclosure">Download Disclosures</button>
          <button class="actionButton" data-action="parse">Parse</button>
          <button class="actionButton" data-action="extract">Extract</button>
          <button class="actionButton" data-action="rules">Run Rules</button>
          <button class="actionButton" data-action="alerts">Alerts</button>
          <button class="actionButton" data-action="canary">Canary</button>
          <button class="actionButton" data-action="export">Export Snapshot</button>
          <button class="actionButton" data-action="summary">Summary (Doc ID)</button>
        </div>
        <div class="controlToolbar">
          <label>Year <input id="financialYear" type="number" min="2000" step="1"></label>
          <label>Limit <input id="stageLimit" type="number" min="1" step="1" value="5"></label>
          <label>Document ID <input id="summaryDocumentId" placeholder="Paste a document id or click summarize on a filing"></label>
          <button id="refreshButton" class="ghostButton" type="button">Refresh Data</button>
        </div>
      </article>
      <article class="panel panelDark">
        <div class="sectionEyebrow">Recent Run</div>
        <h2>Action Output</h2>
        <div id="actionStatus" class="meta metaLight">Ready.</div>
        <pre id="actionOutput" class="terminal">Select an action to run it from the dashboard.</pre>
        <div id="actionHistory"></div>
      </article>
    </section>"""
    template = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__TITLE__</title>
  <style>
    :root {{
      --bg: #efe9dc;
      --paper: #fffdf8;
      --panel: rgba(255, 252, 245, 0.92);
      --panel-strong: #f7f1e4;
      --ink: #17211d;
      --muted: #6c7067;
      --accent: #0f7660;
      --accent-strong: #0b5d4b;
      --warning: #ba5d2a;
      --line: #d8cfbf;
      --shadow: 0 20px 45px rgba(23, 33, 29, 0.08);
      --panel-dark: linear-gradient(145deg, #1f2a25, #293a33);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top left, rgba(15, 118, 96, 0.12), transparent 32%),
        radial-gradient(circle at top right, rgba(186, 93, 42, 0.08), transparent 28%),
        linear-gradient(180deg, #fbf8f1, var(--bg));
      color: var(--ink);
    }}
    h1, h2, h3 {{ margin: 0; font-family: "Iowan Old Style", Georgia, serif; font-weight: 600; }}
    a {{ color: var(--accent-strong); }}
    button {{
      border: 1px solid transparent;
      border-radius: 999px;
      background: linear-gradient(135deg, var(--accent), var(--accent-strong));
      color: #f8fff9;
      padding: 11px 14px;
      font: inherit;
      cursor: pointer;
      transition: transform .16s ease, box-shadow .16s ease, opacity .16s ease;
      box-shadow: 0 10px 20px rgba(15, 118, 96, 0.18);
    }}
    button:hover {{ transform: translateY(-1px); }}
    input {{
      padding: 11px 14px;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.8);
      font: inherit;
      width: 100%;
    }}
    .page {{
      max-width: 1480px;
      margin: 0 auto;
      padding: 28px 22px 40px;
      display: grid;
      gap: 18px;
    }}
    .hero {{
      display: grid;
      grid-template-columns: minmax(0, 1.4fr) minmax(320px, .9fr);
      gap: 18px;
      align-items: stretch;
    }}
    .panel {{
      background: var(--panel);
      backdrop-filter: blur(12px);
      border: 1px solid rgba(216, 207, 191, 0.8);
      border-radius: 24px;
      padding: 20px;
      box-shadow: var(--shadow);
    }}
    .panelDark {{
      background: var(--panel-dark);
      color: #f8f3e8;
      border-color: rgba(255,255,255,.08);
    }}
    .panelDark a {{ color: #d9fff0; }}
    .meta {{ color: var(--muted); font-size: 13px; }}
    .metaLight {{ color: rgba(248, 243, 232, 0.72); }}
    .lead {{ margin: 10px 0 0; color: var(--muted); line-height: 1.55; }}
    .heroCard {{
      display: grid;
      gap: 16px;
      align-content: start;
      min-height: 220px;
      background:
        linear-gradient(130deg, rgba(255,255,255,0.76), rgba(255,255,255,0.3)),
        linear-gradient(120deg, rgba(15,118,96,.08), rgba(186,93,42,.06));
    }}
    .heroTitle {{
      font-size: clamp(32px, 4vw, 52px);
      line-height: 1;
      max-width: 12ch;
    }}
    .heroBar {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      align-items: center;
    }}
    .chip {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.75);
      border: 1px solid rgba(216, 207, 191, 0.95);
      color: var(--muted);
      font-size: 13px;
    }}
    .summaryStrip {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 12px;
    }}
    .summaryCard {{
      padding: 14px;
      border-radius: 18px;
      background: var(--panel-strong);
      border: 1px solid var(--line);
    }}
    .summaryLabel {{ font-size: 12px; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); }}
    .summaryValue {{ margin-top: 8px; font-size: 28px; color: var(--accent-strong); font-weight: 700; }}
    .sectionEyebrow {{ font-size: 11px; letter-spacing: .16em; text-transform: uppercase; color: var(--muted); margin-bottom: 10px; }}
    .workspace {{
      display: grid;
      grid-template-columns: 320px minmax(0, 1fr);
      gap: 18px;
      align-items: start;
    }}
    .sidebar {{
      position: sticky;
      top: 18px;
      display: grid;
      gap: 14px;
    }}
    .searchBox {{ display: grid; gap: 10px; }}
    .issuerStack {{
      display: grid;
      gap: 10px;
      max-height: 74vh;
      overflow: auto;
      padding-right: 4px;
    }}
    .issuerCard {{
      border-radius: 18px;
      border: 1px solid rgba(216, 207, 191, 0.95);
      background: rgba(255, 255, 255, 0.78);
      padding: 14px;
      display: grid;
      gap: 8px;
      text-align: left;
      box-shadow: none;
      color: var(--ink);
    }}
    .issuerCard.active {{
      border-color: rgba(15, 118, 96, 0.55);
      background: linear-gradient(135deg, rgba(15,118,96,.14), rgba(255,255,255,.94));
      box-shadow: 0 14px 30px rgba(15, 118, 96, 0.12);
    }}
    .issuerTicker {{ font-size: 20px; font-weight: 700; }}
    .issuerName {{ color: var(--ink); line-height: 1.35; }}
    .issuerMeta {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      color: var(--muted);
      font-size: 12px;
    }}
    .mainColumn {{
      display: grid;
      gap: 18px;
    }}
    .focusHeader {{
      display: grid;
      grid-template-columns: minmax(0, 1.2fr) minmax(260px, .8fr);
      gap: 16px;
      align-items: start;
    }}
    .focusCard {{
      display: grid;
      gap: 14px;
    }}
    .focusTitle {{ font-size: clamp(28px, 3vw, 42px); line-height: 1.05; }}
    .focusSubtitle {{ font-size: 16px; color: var(--muted); max-width: 68ch; line-height: 1.6; }}
    .statsGrid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 12px;
    }}
    .stat {{
      border-radius: 18px;
      padding: 14px;
      background: linear-gradient(180deg, rgba(255,255,255,.9), rgba(247,241,228,.92));
      border: 1px solid var(--line);
    }}
    .stat strong {{ display: block; font-size: 26px; color: var(--accent-strong); margin-top: 6px; }}
    .contentGrid {{
      display: grid;
      gap: 18px;
      grid-template-columns: repeat(12, minmax(0, 1fr));
    }}
    .span7 {{ grid-column: span 7; }}
    .span5 {{ grid-column: span 5; }}
    .span6 {{ grid-column: span 6; }}
    .span12 {{ grid-column: span 12; }}
    .docGroups {{ display: grid; gap: 14px; }}
    .docGroup {{
      border: 1px solid var(--line);
      border-radius: 18px;
      background: rgba(255,255,255,.6);
      overflow: hidden;
    }}
    .docGroupHeader {{
      padding: 14px 16px;
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: baseline;
      background: rgba(247,241,228,.85);
      border-bottom: 1px solid var(--line);
    }}
    .docList {{ display: grid; gap: 0; }}
    .docItem {{
      display: grid;
      grid-template-columns: minmax(0, 1.2fr) auto;
      gap: 14px;
      padding: 14px 16px;
      border-top: 1px solid rgba(216, 207, 191, 0.75);
    }}
    .docItem:first-child {{ border-top: 0; }}
    .docTitle {{ font-weight: 700; }}
    .tagRow {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }}
    .tag {{
      padding: 4px 8px;
      border-radius: 999px;
      background: rgba(15,118,96,.08);
      border: 1px solid rgba(15,118,96,.12);
      color: var(--accent-strong);
      font-size: 12px;
    }}
    .tagWarn {{
      background: rgba(186,93,42,.08);
      border-color: rgba(186,93,42,.12);
      color: var(--warning);
    }}
    .docActions {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      justify-content: flex-end;
    }}
    .miniButton {{
      padding: 7px 11px;
      font-size: 12px;
      background: linear-gradient(135deg, #1a8f72, #0b5d4b);
    }}
    .ghostButton {{
      background: transparent;
      color: var(--accent-strong);
      border-color: rgba(15,118,96,.18);
      box-shadow: none;
    }}
    .metricTable, .systemTable {{
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }}
    .metricTable th, .metricTable td, .systemTable th, .systemTable td {{
      text-align: left;
      padding: 10px 8px;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
    }}
    .metricTable th, .systemTable th {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .08em; }}
    .findingStack {{ display: grid; gap: 12px; }}
    .findingCard {{
      border-radius: 18px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,.7);
      padding: 14px 16px;
      display: grid;
      gap: 8px;
    }}
    .findingClaim {{ font-size: 16px; line-height: 1.5; }}
    .severity {{ color: var(--warning); font-weight: 700; }}
    .opsShell {{
      display: grid;
      grid-template-columns: 1.15fr .85fr;
      gap: 18px;
    }}
    .controlGrid {{
      display: grid;
      gap: 10px;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      margin-top: 16px;
    }}
    .controlToolbar {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 10px;
      margin-top: 16px;
      align-items: end;
    }}
    .controlToolbar label {{ display: grid; gap: 6px; color: rgba(248,243,232,.72); font-size: 13px; }}
    .terminal {{
      background: rgba(10, 14, 12, 0.84);
      color: #eef7ee;
      border-radius: 18px;
      padding: 14px;
      min-height: 220px;
      overflow: auto;
      white-space: pre-wrap;
      border: 1px solid rgba(255,255,255,.08);
    }}
    .historyItem {{ border-top: 1px solid rgba(255,255,255,.08); padding: 10px 0; }}
    .historyItem:first-child {{ border-top: 0; }}
    .emptyState {{
      border: 1px dashed var(--line);
      border-radius: 18px;
      padding: 18px;
      color: var(--muted);
      background: rgba(255,255,255,.5);
    }}
    @media (max-width: 1160px) {{
      .hero,
      .workspace,
      .focusHeader,
      .opsShell {{ grid-template-columns: 1fr; }}
      .sidebar {{ position: static; }}
      .contentGrid > * {{ grid-column: span 12; }}
    }}
    @media (max-width: 720px) {{
      body {{ font-size: 15px; }}
      .page {{ padding: 18px 14px 28px; }}
      .heroTitle, .focusTitle {{ max-width: none; }}
      .docItem {{ grid-template-columns: 1fr; }}
      .docActions {{ justify-content: flex-start; }}
      .issuerStack {{ max-height: none; }}
    }}
  </style>
</head>
<body>
  <main class="page">
    <section class="hero">
      <article class="panel heroCard">
        <div class="sectionEyebrow">Local IDX Filing Workspace</div>
        <h1 class="heroTitle">__TITLE__</h1>
        <p class="lead">Operate the pipeline from one place, then review each issuer in a focused company workspace instead of scanning one long flat report.</p>
        <div class="heroBar">
          <span class="chip">Company-first navigation</span>
          <span class="chip">Documents grouped per issuer</span>
          <span class="chip">Evidence-linked findings</span>
          <a class="chip" href="__METRICS_HREF__">metrics.csv</a>
          <a class="chip" href="__FINDINGS_HREF__">findings.csv</a>
        </div>
      </article>
      <article class="panel">
        <div class="sectionEyebrow">System Snapshot</div>
        <h2>What is in scope right now</h2>
        <div class="summaryStrip">
          <div class="summaryCard">
            <div class="summaryLabel">Issuers</div>
            <div class="summaryValue">__ISSUER_COUNT__</div>
          </div>
          <div class="summaryCard">
            <div class="summaryLabel">Documents</div>
            <div class="summaryValue">__DOCUMENT_COUNT__</div>
          </div>
          <div class="summaryCard">
            <div class="summaryLabel">Findings</div>
            <div class="summaryValue">__FINDING_COUNT__</div>
          </div>
        </div>
      </article>
    </section>
__ACTION_PANEL__
    <section class="workspace">
      <aside class="sidebar">
        <article class="panel searchBox">
          <div class="sectionEyebrow">Navigate</div>
          <h2>Companies</h2>
          <input id="issuerSearch" placeholder="Search ticker, issuer, sector">
          <div class="meta">Pick a company to focus the entire workspace.</div>
        </article>
        <article class="panel">
          <div id="issuerList" class="issuerStack"></div>
        </article>
      </aside>
      <section class="mainColumn">
        <article class="panel focusHeader">
          <div id="companyOverview" class="focusCard"></div>
          <div id="companyStats" class="statsGrid"></div>
        </article>
        <section class="contentGrid">
          <article class="panel span7">
            <div class="sectionEyebrow">Company Filings</div>
            <h2>Documents</h2>
            <div id="documentList"></div>
          </article>
          <article class="panel span5">
            <div class="sectionEyebrow">Signals</div>
            <h2>Findings</h2>
            <div id="findingList"></div>
          </article>
          <article class="panel span6">
            <div class="sectionEyebrow">Normalized Facts</div>
            <h2>Metrics</h2>
            <div id="metricList"></div>
          </article>
          <article class="panel span6">
            <div class="sectionEyebrow">Versioning</div>
            <h2>Revision History</h2>
            <div id="revisionList"></div>
          </article>
          <article class="panel span6">
            <div class="sectionEyebrow">Pipeline Health</div>
            <h2>Backlog</h2>
            <div id="backlogList"></div>
          </article>
          <article class="panel span6">
            <div class="sectionEyebrow">Source Health</div>
            <h2>Canary</h2>
            <div id="canaryList"></div>
          </article>
        </section>
      </section>
    </section>
  </main>
  <script>
    let data = __PAYLOAD__;
    const actionEndpoint = __ACTION_ENDPOINT__;
    const snapshotEndpoint = __SNAPSHOT_ENDPOINT__;
    const artifactEndpoint = __ARTIFACT_ENDPOINT__;
    const state = {{
      selectedTicker: null,
    }};
    const search = document.getElementById('issuerSearch');
    const issuerList = document.getElementById('issuerList');
    const companyOverview = document.getElementById('companyOverview');
    const companyStats = document.getElementById('companyStats');
    const documentList = document.getElementById('documentList');
    const metricList = document.getElementById('metricList');
    const findingList = document.getElementById('findingList');
    const revisionList = document.getElementById('revisionList');
    const backlogList = document.getElementById('backlogList');
    const canaryList = document.getElementById('canaryList');
    const actionStatus = document.getElementById('actionStatus');
    const actionOutput = document.getElementById('actionOutput');
    const actionHistory = document.getElementById('actionHistory');
    const yearInput = document.getElementById('financialYear');
    const limitInput = document.getElementById('stageLimit');
    const summaryDocumentIdInput = document.getElementById('summaryDocumentId');
    const refreshButton = document.getElementById('refreshButton');

    function esc(value) {{
      return String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
    }}

    function localHref(path) {{
      if (!path) return '';
      if (!artifactEndpoint) return esc(path);
      return `${{artifactEndpoint}}?path=${{encodeURIComponent(path)}}`;
    }}

    function issuerName(ticker) {{
      return (data.issuers.find(item => item.ticker === ticker) || {{}}).issuer_name || ticker || 'Unknown issuer';
    }}

    function ensureSelection(visibleIssuers) {{
      if (visibleIssuers.length === 0) {{
        state.selectedTicker = null;
        return;
      }}
      if (!state.selectedTicker || !visibleIssuers.some(item => item.ticker === state.selectedTicker)) {{
        state.selectedTicker = visibleIssuers[0].ticker;
      }}
    }}

    function groupBy(items, keyFn) {{
      const buckets = new Map();
      items.forEach(item => {{
        const key = keyFn(item);
        if (!buckets.has(key)) buckets.set(key, []);
        buckets.get(key).push(item);
      }});
      return buckets;
    }}

    function renderHistory() {{
      if (!actionHistory) return;
      const rows = (data.action_history || []).slice(0, 10);
      actionHistory.innerHTML = rows.length
        ? rows.map(item => `<div class="historyItem"><div><strong>${{esc(item.action)}}</strong> <span class="meta">${{esc(item.timestamp)}}</span></div><div class="meta">${{esc((item.command || []).join(' '))}}</div><div class="meta">exit=${{esc(item.returncode)}}</div></div>`).join('')
        : `<div class="meta">No action has been run from this dashboard yet.</div>`;
    }}

    function render() {{
      if (yearInput && !yearInput.value) {{
        yearInput.value = String(new Date().getFullYear());
      }}
      const q = search.value.trim().toLowerCase();
      const issuers = data.issuers.filter(item => !q || [item.ticker, item.issuer_name, item.sector, item.subsector].join(' ').toLowerCase().includes(q));
      ensureSelection(issuers);
      const selectedIssuer = issuers.find(item => item.ticker === state.selectedTicker) || null;
      const selectedTicker = selectedIssuer?.ticker || null;
      const docs = data.documents
        .filter(item => selectedTicker ? item.ticker === selectedTicker : false)
        .sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')));
      const metrics = data.metrics
        .filter(item => selectedTicker ? item.ticker === selectedTicker : false)
        .sort((a, b) => String(a.metric_name || '').localeCompare(String(b.metric_name || '')));
      const findings = data.findings
        .filter(item => selectedTicker ? item.ticker === selectedTicker : false)
        .sort((a, b) => String(b.finished_at || '').localeCompare(String(a.finished_at || '')));
      const revisions = data.revisions
        .filter(item => selectedTicker ? item.ticker === selectedTicker : false)
        .sort((a, b) => String(b.latest_revision_at || '').localeCompare(String(a.latest_revision_at || '')));
      const backlogRows = data.backlog_rows;
      const canaryRows = data.canary_rows;

      issuerList.innerHTML = issuers.length
        ? issuers.slice(0, 120).map(item => `
          <button class="issuerCard ${{item.ticker === state.selectedTicker ? 'active' : ''}}" type="button" data-ticker="${{esc(item.ticker)}}">
            <div class="issuerTicker">${{esc(item.ticker)}}</div>
            <div class="issuerName">${{esc(item.issuer_name)}}</div>
            <div class="issuerMeta">
              <span>${{esc(item.sector || 'unknown')}}</span>
              <span>${{esc(item.document_count || 0)}} docs</span>
              <span>${{esc(item.latest_document_at || item.last_seen_at || '')}}</span>
            </div>
          </button>
        `).join('')
        : `<div class="emptyState">No issuer matches this search.</div>`;

      if (!selectedIssuer) {{
        companyOverview.innerHTML = `<div class="emptyState">Select a company from the left rail to view grouped documents, metrics, and findings.</div>`;
        companyStats.innerHTML = '';
        documentList.innerHTML = `<div class="emptyState">No documents to show.</div>`;
        metricList.innerHTML = `<div class="emptyState">No metrics to show.</div>`;
        revisionList.innerHTML = `<div class="emptyState">No revisions to show.</div>`;
        findingList.innerHTML = `<div class="emptyState">No findings to show.</div>`;
      }} else {{
        const analyzedCount = docs.filter(item => item.process_state === 'analyzed').length;
        const failedCount = docs.filter(item => item.process_state === 'failed').length;
        const groupedDocs = groupBy(docs, item => item.source_name || 'unknown');
        companyOverview.innerHTML = `
          <div class="sectionEyebrow">Selected Company</div>
          <h2 class="focusTitle">${{esc(selectedIssuer.issuer_name)}}</h2>
          <div class="focusSubtitle">${{esc(selectedIssuer.ticker)}} is the active focus. Everything below is scoped to this issuer so you do not need to mentally regroup flat rows.</div>
          <div class="heroBar">
            <span class="chip">${{esc(selectedIssuer.sector || 'unknown')}} / ${{esc(selectedIssuer.subsector || 'unknown')}}</span>
            <span class="chip">Last seen ${{esc(selectedIssuer.last_seen_at || '')}}</span>
            <span class="chip">Latest filing ${{esc(selectedIssuer.latest_document_at || '')}}</span>
          </div>
        `;
        companyStats.innerHTML = `
          <div class="stat"><div class="summaryLabel">Documents</div><strong>${{docs.length}}</strong></div>
          <div class="stat"><div class="summaryLabel">Findings</div><strong>${{findings.length}}</strong></div>
          <div class="stat"><div class="summaryLabel">Analyzed</div><strong>${{analyzedCount}}</strong></div>
          <div class="stat"><div class="summaryLabel">Failed</div><strong>${{failedCount}}</strong></div>
        `;
        documentList.innerHTML = docs.length
          ? `<div class="docGroups">${{Array.from(groupedDocs.entries()).map(([groupName, items]) => `
              <section class="docGroup">
                <div class="docGroupHeader">
                  <div>
                    <h3>${{esc(groupName)}}</h3>
                    <div class="meta">${{items.length}} filing${{items.length === 1 ? '' : 's'}} for ${{esc(selectedIssuer.ticker)}}</div>
                  </div>
                </div>
                <div class="docList">${{items.map(item => `
                  <article class="docItem">
                    <div>
                      <div class="docTitle">${{esc(item.document_type || 'document')}}<span class="meta"> · ${{esc(item.source_record_id || '')}}</span></div>
                      <div class="meta">${{esc(item.updated_at || item.retrieved_at || '')}}</div>
                      <div class="tagRow">
                        <span class="tag">${{esc(item.process_state || 'unknown')}}</span>
                        <span class="tag">${{esc(item.triggered_rules || 0)}} rules</span>
                        ${{item.version_group_id ? `<span class="tag">rev ${{esc(item.version_group_id)}}</span>` : ''}}
                        ${{item.process_state === 'failed' ? `<span class="tag tagWarn">needs retry</span>` : ''}}
                      </div>
                    </div>
                    <div class="docActions">
                      ${{item.local_path ? `<a class="chip" href="${{localHref(item.local_path)}}">local</a>` : ''}}
                      ${{item.original_url ? `<a class="chip" href="${{esc(item.original_url)}}">origin</a>` : ''}}
                      ${{actionEndpoint && item.document_id ? `<button class="miniButton" type="button" data-document-id="${{esc(item.document_id)}}" data-inline-action="summary">summarize</button>` : ''}}
                    </div>
                  </article>
                `).join('')}}</div>
              </section>
            `).join('')}</div>`
          : `<div class="emptyState">No persisted documents yet for this company.</div>`;
        metricList.innerHTML = metrics.length
          ? `<table class="metricTable"><thead><tr><th>Metric</th><th>Value</th><th>Period</th></tr></thead><tbody>${{metrics.slice(0, 80).map(item => `<tr><td>${{esc(item.metric_name)}}</td><td>${{esc(item.value_normalized)}} ${{esc(item.currency || '')}}</td><td>${{esc(item.fiscal_year || '')}} ${{esc(item.fiscal_period || '')}}</td></tr>`).join('')}}</tbody></table>`
          : `<div class="emptyState">No normalized metrics yet for this company.</div>`;
        revisionList.innerHTML = revisions.length
          ? `<table class="metricTable"><thead><tr><th>Source Record</th><th>Revisions</th><th>Latest</th></tr></thead><tbody>${{revisions.map(item => `<tr><td>${{esc(item.source_record_id)}}</td><td>${{esc(item.revision_count)}}</td><td>${{esc(item.latest_revision_at)}}</td></tr>`).join('')}}</tbody></table>`
          : `<div class="emptyState">No revision groups yet for this company.</div>`;
        findingList.innerHTML = findings.length
          ? `<div class="findingStack">${{findings.slice(0, 80).map(item => `<article class="findingCard"><div><span class="severity">${{esc(item.finding_type)}}:${{esc(item.name)}}</span></div><div class="findingClaim">${{esc(item.claim)}}</div><div class="meta">${{esc(item.summary)}}</div><div class="meta">${{esc(item.evidence)}}</div><div class="heroBar">${{item.local_path ? `<a class="chip" href="${{localHref(item.local_path)}}">attachment</a>` : ''}}${{item.original_url ? `<a class="chip" href="${{esc(item.original_url)}}">origin</a>` : ''}}</div></article>`).join('')}</div>`
          : `<div class="emptyState">No findings have been persisted yet for this company.</div>`;
      }}

      backlogList.innerHTML = `<table class="systemTable"><thead><tr><th>Source</th><th>Discovered</th><th>Downloaded</th><th>Parsed</th><th>Extracted</th><th>Analyzed</th><th>Failed</th></tr></thead><tbody>${{backlogRows.map(item => `<tr><td>${{esc(item.source_name)}}</td><td>${{esc(item.discovered)}}</td><td>${{esc(item.downloaded)}}</td><td>${{esc(item.parsed)}}</td><td>${{esc(item.extracted)}}</td><td>${{esc(item.analyzed)}}</td><td>${{esc(item.failed)}}</td></tr>`).join('')}}</tbody></table>`;
      canaryList.innerHTML = `<table class="systemTable"><thead><tr><th>Source</th><th>Status</th><th>Accessible</th><th>Drifted</th><th>Finished</th></tr></thead><tbody>${{canaryRows.map(item => `<tr><td>${{esc(item.source_name)}}</td><td>${{esc(item.status)}}</td><td>${{esc(item.accessible)}}</td><td>${{esc(item.drifted)}}</td><td>${{esc(item.finished_at)}}</td></tr>`).join('')}}</tbody></table>`;
      renderHistory();
    }}

    async function refreshSnapshot() {{
      if (!snapshotEndpoint) return;
      const response = await fetch(snapshotEndpoint);
      if (!response.ok) {{
        throw new Error(`Snapshot refresh failed: ${{response.status}}`);
      }}
      data = await response.json();
      render();
    }}

    async function runAction(action, overrides = {{}}) {{
      if (!actionEndpoint) return;
      const params = new URLSearchParams();
      params.set('action', action);
      params.set('year', yearInput?.value || '');
      params.set('limit', limitInput?.value || '');
      params.set('document_id', summaryDocumentIdInput?.value || '');
      Object.entries(overrides).forEach(([key, value]) => params.set(key, String(value ?? '')));

      actionStatus.textContent = `Running ${{action}}...`;
      actionOutput.textContent = '';
      const response = await fetch(actionEndpoint, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' }},
        body: params.toString(),
      }});
      const result = await response.json();
      actionOutput.textContent = `${{(result.command || []).join(' ')}}\n\nstdout:\n${{result.stdout || ''}}\n\nstderr:\n${{result.stderr || ''}}`;
      actionStatus.textContent = result.ok ? `${{action}} finished.` : `${{action}} failed (exit ${{result.returncode}}).`;
      if (response.ok && result.snapshot) {{
        data = result.snapshot;
        render();
      }} else if (response.ok) {{
        await refreshSnapshot();
      }}
    }}

    search.addEventListener('input', render);
    if (refreshButton) {{
      refreshButton.addEventListener('click', () => refreshSnapshot().catch(error => {{
        actionStatus.textContent = error.message;
      }}));
    }}
    document.querySelectorAll('.actionButton').forEach(button => {{
      button.addEventListener('click', () => {{
        runAction(button.dataset.action).catch(error => {{
          actionStatus.textContent = error.message;
        }});
      }});
    }});
    document.addEventListener('click', event => {{
      const issuerTarget = event.target.closest('[data-ticker]');
      if (issuerTarget) {{
        state.selectedTicker = issuerTarget.dataset.ticker || null;
        render();
        return;
      }}
      const target = event.target.closest('[data-inline-action]');
      if (!target) return;
      const documentId = target.dataset.documentId || '';
      if (summaryDocumentIdInput) {{
        summaryDocumentIdInput.value = documentId;
      }}
      runAction(target.dataset.inlineAction, {{ document_id: documentId }}).catch(error => {{
        actionStatus.textContent = error.message;
      }});
    }});
    render();
  </script>
</body>
</html>"""
    return (
        template
        .replace("__TITLE__", title)
        .replace("__METRICS_HREF__", metrics_href)
        .replace("__FINDINGS_HREF__", findings_href)
        .replace("__ISSUER_COUNT__", str(len(issuers)))
        .replace("__DOCUMENT_COUNT__", str(len(documents)))
        .replace("__FINDING_COUNT__", str(len(findings)))
        .replace("__ACTION_PANEL__", action_panel)
        .replace("__PAYLOAD__", payload)
        .replace("__ACTION_ENDPOINT__", action_endpoint_json)
        .replace("__SNAPSHOT_ENDPOINT__", snapshot_endpoint_json)
        .replace("__ARTIFACT_ENDPOINT__", artifact_endpoint_json)
        .replace("{{", "{")
        .replace("}}", "}")
    )
