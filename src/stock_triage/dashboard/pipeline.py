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
    action_panel = ""
    if action_endpoint and snapshot_endpoint:
        action_panel = """
    <section class="grid">
      <article class="panel">
        <h2>Control Panel</h2>
        <div class="meta">Run the common pipeline stages here instead of typing commands.</div>
        <div class="control-grid">
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
        <div class="toolbar controls">
          <label>Year <input id="financialYear" type="number" min="2000" step="1"></label>
          <label>Limit <input id="stageLimit" type="number" min="1" step="1" value="5"></label>
          <label>Document ID <input id="summaryDocumentId" placeholder="Paste a document id or click summarize on a row"></label>
          <button id="refreshButton" class="ghostButton" type="button">Refresh Data</button>
        </div>
      </article>
      <article class="panel">
        <h2>Action Output</h2>
        <div id="actionStatus" class="meta">Ready.</div>
        <pre id="actionOutput" class="terminal">Select an action to run it from the dashboard.</pre>
        <div id="actionHistory"></div>
      </article>
    </section>"""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{
      --bg: #f3efe4;
      --panel: #fffaf2;
      --ink: #1c1b19;
      --muted: #6f665a;
      --accent: #0b6e4f;
      --warn: #b85c38;
      --line: #d9cdbb;
    }}
    body {{ margin: 0; font-family: Georgia, 'Iowan Old Style', serif; background: radial-gradient(circle at top, #fffaf2, var(--bg)); color: var(--ink); }}
    header {{ padding: 32px 24px 16px; }}
    h1, h2 {{ margin: 0 0 12px; }}
    .shell {{ display: grid; gap: 16px; padding: 0 24px 32px; }}
    .panel {{ background: var(--panel); border: 1px solid var(--line); border-radius: 18px; padding: 18px; box-shadow: 0 10px 30px rgba(28,27,25,.05); }}
    .toolbar {{ display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }}
    input {{ padding: 10px 12px; border-radius: 999px; border: 1px solid var(--line); min-width: 240px; font: inherit; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
    th, td {{ text-align: left; padding: 8px 6px; border-bottom: 1px solid var(--line); vertical-align: top; }}
    .chips {{ display: flex; gap: 8px; flex-wrap: wrap; color: var(--muted); }}
    .kpi {{ font-size: 28px; color: var(--accent); }}
    .grid {{ display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }}
    .finding {{ border-top: 1px solid var(--line); padding: 10px 0; }}
    .meta {{ color: var(--muted); font-size: 13px; }}
    a {{ color: var(--accent); }}
    .severity {{ color: var(--warn); }}
    .control-grid {{ display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); margin-top: 14px; }}
    .controls label {{ display: flex; flex-direction: column; gap: 6px; color: var(--muted); }}
    button {{ border: 1px solid var(--line); border-radius: 999px; background: var(--ink); color: #fffaf2; padding: 10px 14px; font: inherit; cursor: pointer; }}
    button:hover {{ opacity: .92; }}
    .ghostButton {{ background: transparent; color: var(--accent); }}
    .miniButton {{ padding: 6px 10px; font-size: 12px; background: var(--accent); color: white; }}
    .terminal {{ background: #201d19; color: #f7efd8; border-radius: 14px; padding: 14px; min-height: 180px; overflow: auto; white-space: pre-wrap; }}
    .historyItem {{ border-top: 1px solid var(--line); padding: 10px 0; }}
    .historyItem:first-child {{ border-top: 0; }}
    @media (max-width: 720px) {{ body {{ font-size: 15px; }} table {{ display:block; overflow:auto; }} }}
  </style>
</head>
<body>
  <header>
    <h1>{title}</h1>
    <div class="chips">
      <span>Issuers: {len(issuers)}</span>
      <span>Documents: {len(documents)}</span>
      <span>Findings: {len(findings)}</span>
      <a href="{metrics_href}">metrics.csv</a>
      <a href="{findings_href}">findings.csv</a>
    </div>
  </header>
  <main class="shell">
    <section class="panel">
      <div class="toolbar">
        <input id="issuerSearch" placeholder="Search ticker, issuer, source record">
      </div>
    </section>
{action_panel}
    <section class="grid">
      <article class="panel"><h2>Issuer Search</h2><div id="issuerList"></div></article>
      <article class="panel"><h2>Latest Documents</h2><div id="documentList"></div></article>
    </section>
    <section class="grid">
      <article class="panel"><h2>Metric Snapshot</h2><div id="metricList"></div></article>
      <article class="panel"><h2>Revision History</h2><div id="revisionList"></div></article>
    </section>
    <section class="grid">
      <article class="panel"><h2>Backlog</h2><div id="backlogList"></div></article>
      <article class="panel"><h2>Canary</h2><div id="canaryList"></div></article>
    </section>
    <section class="panel"><h2>Evidence-Linked Findings</h2><div id="findingList"></div></section>
  </main>
  <script>
    let data = {payload};
    const actionEndpoint = {json.dumps(action_endpoint)};
    const snapshotEndpoint = {json.dumps(snapshot_endpoint)};
    const artifactEndpoint = {json.dumps(artifact_endpoint)};
    const search = document.getElementById('issuerSearch');
    const issuerList = document.getElementById('issuerList');
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
      const docs = data.documents.filter(item => !q || [item.ticker, item.source_record_id, item.source_name].join(' ').toLowerCase().includes(q));
      const metrics = data.metrics.filter(item => !q || [item.ticker, item.metric_name].join(' ').toLowerCase().includes(q));
      const findings = data.findings.filter(item => !q || [item.ticker, item.claim, item.name, item.finding_type].join(' ').toLowerCase().includes(q));
      const revisions = data.revisions.filter(item => !q || [item.ticker, item.source_record_id, item.source_name].join(' ').toLowerCase().includes(q));
      const backlogRows = data.backlog_rows.filter(item => !q || [item.source_name].join(' ').toLowerCase().includes(q));
      const canaryRows = data.canary_rows.filter(item => !q || [item.source_name, item.status].join(' ').toLowerCase().includes(q));

      issuerList.innerHTML = `<table><thead><tr><th>Ticker</th><th>Issuer</th><th>Sector</th><th>Docs</th><th>Latest</th></tr></thead><tbody>${{issuers.slice(0, 50).map(item => `<tr><td>${{esc(item.ticker)}}</td><td>${{esc(item.issuer_name)}}</td><td>${{esc(item.sector)}} / ${{esc(item.subsector)}}</td><td>${{esc(item.document_count)}}</td><td>${{esc(item.latest_document_at || '')}}</td></tr>`).join('')}}</tbody></table>`;
      documentList.innerHTML = `<table><thead><tr><th>Ticker</th><th>Source</th><th>State</th><th>Rules</th><th>Attachment</th></tr></thead><tbody>${{docs.slice(0, 50).map(item => `<tr><td>${{esc(item.ticker || '')}}</td><td>${{esc(item.source_name)}}<div class="meta">${{esc(item.source_record_id)}}</div><div class="meta">${{esc(item.document_id || '')}}</div></td><td>${{esc(item.process_state)}}</td><td>${{esc(item.triggered_rules)}}</td><td>${{item.local_path ? `<a href="${{localHref(item.local_path)}}">local</a>` : ''}}${{item.original_url ? ` / <a href="${{esc(item.original_url)}}">origin</a>` : ''}}${{actionEndpoint && item.document_id ? ` <button class="miniButton" type="button" data-document-id="${{esc(item.document_id)}}" data-inline-action="summary">summarize</button>` : ''}}</td></tr>`).join('')}}</tbody></table>`;
      metricList.innerHTML = `<table><thead><tr><th>Ticker</th><th>Metric</th><th>Value</th><th>Period</th></tr></thead><tbody>${{metrics.slice(0, 80).map(item => `<tr><td>${{esc(item.ticker)}}</td><td>${{esc(item.metric_name)}}</td><td>${{esc(item.value_normalized)}} ${{esc(item.currency || '')}}</td><td>${{esc(item.fiscal_year || '')}} ${{esc(item.fiscal_period || '')}}</td></tr>`).join('')}}</tbody></table>`;
      revisionList.innerHTML = `<table><thead><tr><th>Ticker</th><th>Source Record</th><th>Revisions</th><th>Latest</th></tr></thead><tbody>${{revisions.slice(0, 50).map(item => `<tr><td>${{esc(item.ticker || '')}}</td><td>${{esc(item.source_record_id)}}</td><td>${{esc(item.revision_count)}}</td><td>${{esc(item.latest_revision_at)}}</td></tr>`).join('')}}</tbody></table>`;
      backlogList.innerHTML = `<table><thead><tr><th>Source</th><th>Discovered</th><th>Downloaded</th><th>Parsed</th><th>Extracted</th><th>Analyzed</th><th>Failed</th></tr></thead><tbody>${{backlogRows.map(item => `<tr><td>${{esc(item.source_name)}}</td><td>${{esc(item.discovered)}}</td><td>${{esc(item.downloaded)}}</td><td>${{esc(item.parsed)}}</td><td>${{esc(item.extracted)}}</td><td>${{esc(item.analyzed)}}</td><td>${{esc(item.failed)}}</td></tr>`).join('')}}</tbody></table>`;
      canaryList.innerHTML = `<table><thead><tr><th>Source</th><th>Status</th><th>Accessible</th><th>Drifted</th><th>Finished</th></tr></thead><tbody>${{canaryRows.map(item => `<tr><td>${{esc(item.source_name)}}</td><td>${{esc(item.status)}}</td><td>${{esc(item.accessible)}}</td><td>${{esc(item.drifted)}}</td><td>${{esc(item.finished_at)}}</td></tr>`).join('')}}</tbody></table>`;
      findingList.innerHTML = findings.slice(0, 80).map(item => `<div class="finding"><div><strong>${{esc(item.ticker || '')}}</strong> <span class="severity">${{esc(item.finding_type)}}:${{esc(item.name)}}</span></div><div>${{esc(item.claim)}}</div><div class="meta">${{esc(item.summary)}}</div><div class="meta">${{esc(item.evidence)}}</div><div class="meta">${{item.local_path ? `<a href="${{localHref(item.local_path)}}">attachment</a>` : ''}}${{item.original_url ? ` / <a href="${{esc(item.original_url)}}">origin</a>` : ''}}</div></div>`).join('');
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
