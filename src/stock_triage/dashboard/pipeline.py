"""Phase 8 dashboard export: local HTML plus CSV snapshots."""

from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..config import Config
from ..db import WriterQueue, connect, configure_pragmas, run_migrations


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

    with WriterQueue(cfg) as wq:
        wq.submit(run_migrations)

    conn = connect(cfg.paths.db_path, read_only=True)
    try:
        configure_pragmas(conn, cfg)
        issuers = _load_issuers(conn)
        documents = _load_latest_documents(conn, limit=document_limit)
        metrics = _load_metric_snapshot(conn)
        findings = _load_findings(conn, limit=finding_limit)
        revisions = _load_revision_history(conn, limit=50)
    finally:
        conn.close()

    metrics_csv_path = target_dir / "metrics.csv"
    findings_csv_path = target_dir / "findings.csv"
    html_path = target_dir / "index.html"

    _write_metrics_csv(metrics_csv_path, metrics)
    _write_findings_csv(findings_csv_path, findings)
    html_path.write_text(
        _render_html(
            title=cfg.dashboard.title,
            issuers=issuers,
            documents=documents,
            metrics=metrics,
            findings=findings,
            revisions=revisions,
            metrics_csv_path=metrics_csv_path.name,
            findings_csv_path=findings_csv_path.name,
        ),
        encoding="utf-8",
    )

    return DashboardExportResult(
        html_path=html_path,
        metrics_csv_path=metrics_csv_path,
        findings_csv_path=findings_csv_path,
        issuer_count=len(issuers),
        document_count=len(documents),
        finding_count=len(findings),
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


def _render_html(
    *,
    title: str,
    issuers: list[dict[str, object]],
    documents: list[dict[str, object]],
    metrics: list[dict[str, object]],
    findings: list[dict[str, object]],
    revisions: list[dict[str, object]],
    metrics_csv_path: str,
    findings_csv_path: str,
) -> str:
    payload = json.dumps(
        {
            "issuers": issuers,
            "documents": documents,
            "metrics": metrics,
            "findings": findings,
            "revisions": revisions,
        },
        ensure_ascii=True,
    )
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
      <a href="{metrics_csv_path}">metrics.csv</a>
      <a href="{findings_csv_path}">findings.csv</a>
    </div>
  </header>
  <main class="shell">
    <section class="panel">
      <div class="toolbar">
        <input id="issuerSearch" placeholder="Search ticker, issuer, source record">
      </div>
    </section>
    <section class="grid">
      <article class="panel"><h2>Issuer Search</h2><div id="issuerList"></div></article>
      <article class="panel"><h2>Latest Documents</h2><div id="documentList"></div></article>
    </section>
    <section class="grid">
      <article class="panel"><h2>Metric Snapshot</h2><div id="metricList"></div></article>
      <article class="panel"><h2>Revision History</h2><div id="revisionList"></div></article>
    </section>
    <section class="panel"><h2>Evidence-Linked Findings</h2><div id="findingList"></div></section>
  </main>
  <script>
    const data = {payload};
    const search = document.getElementById('issuerSearch');
    const issuerList = document.getElementById('issuerList');
    const documentList = document.getElementById('documentList');
    const metricList = document.getElementById('metricList');
    const findingList = document.getElementById('findingList');
    const revisionList = document.getElementById('revisionList');

    function esc(value) {{
      return String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
    }}

    function render() {{
      const q = search.value.trim().toLowerCase();
      const issuers = data.issuers.filter(item => !q || [item.ticker, item.issuer_name, item.sector, item.subsector].join(' ').toLowerCase().includes(q));
      const docs = data.documents.filter(item => !q || [item.ticker, item.source_record_id, item.source_name].join(' ').toLowerCase().includes(q));
      const metrics = data.metrics.filter(item => !q || [item.ticker, item.metric_name].join(' ').toLowerCase().includes(q));
      const findings = data.findings.filter(item => !q || [item.ticker, item.claim, item.name, item.finding_type].join(' ').toLowerCase().includes(q));
      const revisions = data.revisions.filter(item => !q || [item.ticker, item.source_record_id, item.source_name].join(' ').toLowerCase().includes(q));

      issuerList.innerHTML = `<table><thead><tr><th>Ticker</th><th>Issuer</th><th>Sector</th><th>Docs</th><th>Latest</th></tr></thead><tbody>${{issuers.slice(0, 50).map(item => `<tr><td>${{esc(item.ticker)}}</td><td>${{esc(item.issuer_name)}}</td><td>${{esc(item.sector)}} / ${{esc(item.subsector)}}</td><td>${{esc(item.document_count)}}</td><td>${{esc(item.latest_document_at || '')}}</td></tr>`).join('')}}</tbody></table>`;
      documentList.innerHTML = `<table><thead><tr><th>Ticker</th><th>Source</th><th>State</th><th>Rules</th><th>Attachment</th></tr></thead><tbody>${{docs.slice(0, 50).map(item => `<tr><td>${{esc(item.ticker || '')}}</td><td>${{esc(item.source_name)}}<div class="meta">${{esc(item.source_record_id)}}</div></td><td>${{esc(item.process_state)}}</td><td>${{esc(item.triggered_rules)}}</td><td><a href="${{esc(item.local_path)}}">local</a>${{item.original_url ? ` / <a href="${{esc(item.original_url)}}">origin</a>` : ''}}</td></tr>`).join('')}}</tbody></table>`;
      metricList.innerHTML = `<table><thead><tr><th>Ticker</th><th>Metric</th><th>Value</th><th>Period</th></tr></thead><tbody>${{metrics.slice(0, 80).map(item => `<tr><td>${{esc(item.ticker)}}</td><td>${{esc(item.metric_name)}}</td><td>${{esc(item.value_normalized)}} ${{esc(item.currency || '')}}</td><td>${{esc(item.fiscal_year || '')}} ${{esc(item.fiscal_period || '')}}</td></tr>`).join('')}}</tbody></table>`;
      revisionList.innerHTML = `<table><thead><tr><th>Ticker</th><th>Source Record</th><th>Revisions</th><th>Latest</th></tr></thead><tbody>${{revisions.slice(0, 50).map(item => `<tr><td>${{esc(item.ticker || '')}}</td><td>${{esc(item.source_record_id)}}</td><td>${{esc(item.revision_count)}}</td><td>${{esc(item.latest_revision_at)}}</td></tr>`).join('')}}</tbody></table>`;
      findingList.innerHTML = findings.slice(0, 80).map(item => `<div class="finding"><div><strong>${{esc(item.ticker || '')}}</strong> <span class="severity">${{esc(item.finding_type)}}:${{esc(item.name)}}</span></div><div>${{esc(item.claim)}}</div><div class="meta">${{esc(item.summary)}}</div><div class="meta">${{esc(item.evidence)}}</div><div class="meta"><a href="${{esc(item.local_path)}}">attachment</a>${{item.original_url ? ` / <a href="${{esc(item.original_url)}}">origin</a>` : ''}}</div></div>`).join('');
    }}

    search.addEventListener('input', render);
    render();
  </script>
</body>
</html>"""
