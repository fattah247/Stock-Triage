"""Local dashboard server with pipeline action controls."""

from __future__ import annotations

import csv
import io
import json
import mimetypes
import os
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

from ..config import Config
from ..fiscal_year import load_fiscal_year_overrides, save_fiscal_year_override
from ..intelligence import rederive_intelligence
from ..intelligence.review import load_review_queue, update_review_status
from ..logging_setup import get_logger
from .assets import read_static_asset
from .pipeline import load_dashboard_snapshot, render_dashboard_html
from .snapshot import load_company_detail, load_section_detail, load_snapshot_lite


def _watchlist_path(cfg: Config) -> Path:
    """Sidecar JSON file that overrides ``cfg.alerts.watchlist``."""
    return cfg.paths.data_dir / "watchlist.json"


def _load_watchlist(cfg: Config) -> list[str]:
    """Return the effective watchlist — sidecar file wins if it exists."""
    path = _watchlist_path(cfg)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("tickers"), list):
                return [str(t).upper() for t in data["tickers"]]
        except (json.JSONDecodeError, OSError):
            pass
    return [t.upper() for t in cfg.alerts.watchlist]


def _save_watchlist(cfg: Config, tickers: list[str]) -> list[str]:
    """Persist a watchlist sidecar and return the normalised list."""
    normalised = sorted({t.upper().strip() for t in tickers if t.strip()})
    path = _watchlist_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"tickers": normalised}, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )
    # Also update the live config so subsequent snapshot loads pick it up.
    cfg.alerts.watchlist = list(normalised)
    return normalised

REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class DashboardActionResult:
    action: str
    params: dict[str, str]
    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    timestamp: str
    ok: bool
    duration_ms: int


@dataclass(frozen=True)
class DashboardServerHandle:
    server: ThreadingHTTPServer
    url: str


@dataclass
class DashboardJobState:
    job_id: str
    action: str
    timestamp: str
    command: list[str] = field(default_factory=list)
    params: dict[str, str] = field(default_factory=dict)
    stdout_lines: list[str] = field(default_factory=list)
    stderr_lines: list[str] = field(default_factory=list)
    events: list[dict[str, object]] = field(default_factory=list)
    returncode: int | None = None
    ok: bool | None = None
    duration_ms: int | None = None
    finished: bool = False
    _started_at: float = field(default_factory=time.monotonic, repr=False)
    _event_seq: int = field(default=0, repr=False)
    _condition: threading.Condition = field(default_factory=threading.Condition, repr=False)

    def set_command(self, command: list[str]) -> None:
        with self._condition:
            self.command = list(command)
            self._push_event("started", {"command": list(command), "timestamp": self.timestamp})
            self._condition.notify_all()

    def append_output(self, stream: str, text: str) -> None:
        if stream == "stdout":
            target = self.stdout_lines
        else:
            target = self.stderr_lines
        with self._condition:
            target.append(text)
            self._push_event(stream, {"text": text})
            self._condition.notify_all()

    def finish(self, *, returncode: int, ok: bool) -> None:
        with self._condition:
            self.returncode = returncode
            self.ok = ok
            self.finished = True
            self.duration_ms = int((time.monotonic() - self._started_at) * 1000)
            self._push_event(
                "complete",
                {
                    "returncode": returncode,
                    "ok": ok,
                    "duration_ms": self.duration_ms,
                },
            )
            self._condition.notify_all()

    def wait_for_events(self, offset: int, *, timeout_s: float = 1.0) -> tuple[list[dict[str, object]], bool]:
        with self._condition:
            if offset >= len(self.events) and not self.finished:
                self._condition.wait(timeout=timeout_s)
            return self.events[offset:], self.finished

    def to_history_entry(self) -> dict[str, object]:
        return DashboardActionResult(
            action=self.action,
            params=dict(self.params),
            command=list(self.command),
            returncode=self.returncode or 0,
            stdout="".join(self.stdout_lines),
            stderr="".join(self.stderr_lines),
            timestamp=self.timestamp,
            ok=bool(self.ok),
            duration_ms=self.duration_ms or 0,
        ).__dict__

    def _push_event(self, event_type: str, payload: dict[str, object]) -> None:
        self._event_seq += 1
        self.events.append(
            {
                "id": self._event_seq,
                "type": event_type,
                **payload,
            }
        )


ActionRunner = Callable[[Config, str, dict[str, str], DashboardJobState], None]


class DashboardHTTPServer(ThreadingHTTPServer):
    cfg: Config
    title: str
    action_history: list[dict[str, object]]
    jobs: dict[str, DashboardJobState]
    action_runner: ActionRunner
    history_lock: threading.Lock


def create_dashboard_server(
    cfg: Config,
    *,
    host: str | None = None,
    port: int | None = None,
    action_runner: ActionRunner | None = None,
) -> DashboardServerHandle:
    bind_host = host if host is not None else cfg.dashboard.host
    bind_port = port if port is not None else cfg.dashboard.port
    server = DashboardHTTPServer((bind_host, bind_port), _handler_factory())
    server.cfg = cfg
    server.title = cfg.dashboard.title
    server.action_history = []
    server.jobs = {}
    server.action_runner = action_runner or _run_cli_action
    server.history_lock = threading.Lock()
    bound_host, bound_port = server.server_address[:2]
    return DashboardServerHandle(server=server, url=f"http://{bound_host}:{bound_port}")


def serve_dashboard(
    cfg: Config,
    *,
    host: str | None = None,
    port: int | None = None,
    open_browser: bool = False,
    action_runner: ActionRunner | None = None,
) -> DashboardServerHandle:
    handle = create_dashboard_server(
        cfg,
        host=host,
        port=port,
        action_runner=action_runner,
    )
    if open_browser:
        webbrowser.open(handle.url)
    try:
        handle.server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        handle.server.server_close()
    return handle


def _handler_factory():
    class DashboardHandler(BaseHTTPRequestHandler):
        server: DashboardHTTPServer

        def do_GET(self) -> None:  # noqa: N802 - stdlib interface
            self._handle_read(emit_body=True)

        def do_HEAD(self) -> None:  # noqa: N802 - stdlib interface
            self._handle_read(emit_body=False)

        def _handle_read(self, *, emit_body: bool) -> None:
            parsed = urlparse(self.path)
            if parsed.path in {"/", "/index.html"}:
                snapshot = _snapshot_payload(self.server.cfg, self.server.action_history)
                html = render_dashboard_html(
                    title=self.server.title,
                    issuers=snapshot["issuers"],
                    market_monitor=snapshot["market_monitor"],
                    documents=snapshot["documents"],
                    metrics=snapshot["metrics"],
                    findings=snapshot["findings"],
                    revisions=snapshot["revisions"],
                    company_snapshots=snapshot["company_snapshots"],
                    sector_rollups=snapshot["sector_rollups"],
                    inbox_rows=snapshot["inbox_rows"],
                    digest_rows=snapshot["digest_rows"],
                    review_rows=snapshot["review_rows"],
                    intelligence_kpis=snapshot["intelligence_kpis"],
                    backlog_rows=snapshot["backlog_rows"],
                    canary_rows=snapshot["canary_rows"],
                    metrics_href="/metrics.csv",
                    findings_href="/findings.csv",
                    action_endpoint="/api/action",
                    snapshot_endpoint="/api/snapshot",
                    artifact_endpoint="/artifact",
                    action_history=snapshot["action_history"],
                    static_root="/static",
                )
                self._write_bytes(HTTPStatus.OK, "text/html; charset=utf-8", html.encode("utf-8"), emit_body=emit_body)
                return
            if parsed.path.startswith("/static/"):
                asset_name = parsed.path.removeprefix("/static/")
                self._serve_static(asset_name, emit_body=emit_body)
                return
            if parsed.path == "/api/snapshot":
                self._write_json(HTTPStatus.OK, _snapshot_payload(self.server.cfg, self.server.action_history), emit_body=emit_body)
                return
            if parsed.path == "/api/snapshot/lite":
                lite = load_snapshot_lite(self.server.cfg)
                lite["watchlist"] = _load_watchlist(self.server.cfg)
                lite["action_history"] = list(self.server.action_history)
                self._write_json(HTTPStatus.OK, lite, emit_body=emit_body)
                return
            if parsed.path == "/api/company":
                params = parse_qs(parsed.query)
                ticker = params.get("ticker", [""])[0].strip()
                if not ticker:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"error": "missing_ticker"}, emit_body=emit_body)
                    return
                detail = load_company_detail(self.server.cfg, ticker)
                self._write_json(HTTPStatus.OK, detail, emit_body=emit_body)
                return
            if parsed.path == "/api/rerun-preview":
                params = parse_qs(parsed.query)
                ticker = params.get("ticker", [""])[0].strip()
                preview = _load_rerun_preview(self.server.cfg, ticker=ticker or None)
                self._write_json(HTTPStatus.OK, preview, emit_body=emit_body)
                return
            if parsed.path == "/api/detail":
                params = parse_qs(parsed.query)
                section = params.get("section", [""])[0].strip()
                if section not in {"inbox", "digest", "review", "ops"}:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_section"}, emit_body=emit_body)
                    return
                detail = load_section_detail(self.server.cfg, section)
                self._write_json(HTTPStatus.OK, detail, emit_body=emit_body)
                return
            if parsed.path == "/api/action/stream":
                params = parse_qs(parsed.query)
                job_id = params.get("job_id", [""])[0]
                self._serve_action_stream(job_id)
                return
            if parsed.path == "/metrics.csv":
                snapshot = load_dashboard_snapshot(self.server.cfg)
                payload = _csv_bytes(
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
                    rows=snapshot.metrics,
                )
                self._write_bytes(HTTPStatus.OK, "text/csv; charset=utf-8", payload, emit_body=emit_body)
                return
            if parsed.path == "/findings.csv":
                snapshot = load_dashboard_snapshot(self.server.cfg)
                payload = _csv_bytes(
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
                    rows=snapshot.findings,
                )
                self._write_bytes(HTTPStatus.OK, "text/csv; charset=utf-8", payload, emit_body=emit_body)
                return
            if parsed.path == "/artifact":
                params = parse_qs(parsed.query)
                requested = params.get("path", [""])[0]
                self._serve_artifact(requested, emit_body=emit_body)
                return
            if parsed.path == "/api/watchlist":
                tickers = _load_watchlist(self.server.cfg)
                self._write_json(HTTPStatus.OK, {"tickers": tickers}, emit_body=emit_body)
                return
            if parsed.path == "/api/company-override":
                params = parse_qs(parsed.query)
                ticker = params.get("ticker", [""])[0].strip().upper()
                overrides = load_fiscal_year_overrides(self.server.cfg.company_overrides.definition_path)
                self._write_json(
                    HTTPStatus.OK,
                    {"ticker": ticker, "month": overrides.get(ticker), "overrides": overrides},
                    emit_body=emit_body,
                )
                return
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "not_found"}, emit_body=emit_body)

        def do_POST(self) -> None:  # noqa: N802 - stdlib interface
            parsed = urlparse(self.path)
            if parsed.path == "/api/review":
                self._handle_review_update()
                return
            if parsed.path == "/api/watchlist":
                self._handle_watchlist_update()
                return
            if parsed.path == "/api/company-override":
                self._handle_company_override_update()
                return
            if parsed.path != "/api/action":
                self._write_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            params = {
                key: values[-1]
                for key, values in parse_qs(body.decode("utf-8")).items()
            }
            action = params.get("action", "").strip()
            if not action:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": "missing_action"})
                return
            try:
                job = self._start_action_job(action, params)
            except ValueError as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            response = {
                "job_id": job.job_id,
                "stream_url": f"/api/action/stream?job_id={job.job_id}",
                "action": job.action,
                "timestamp": job.timestamp,
            }
            self._write_json(HTTPStatus.ACCEPTED, response)

        def _handle_review_update(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            params = {
                key: values[-1]
                for key, values in parse_qs(body.decode("utf-8")).items()
            }
            review_item_id = params.get("review_item_id", "").strip()
            review_item_ids = [item.strip() for item in params.get("review_item_ids", "").split(",") if item.strip()]
            status = params.get("status", "").strip()
            note = params.get("note", "").strip() or None
            if (not review_item_id and not review_item_ids) or not status:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": "missing_review_item_id_or_status"})
                return
            try:
                targets = review_item_ids or [review_item_id]
                results = [
                    update_review_status(
                        self.server.cfg,
                        review_item_id=target,
                        status=status,
                        note=note,
                    )
                    for target in targets
                ]
            except ValueError as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            result = results[0]
            self._write_json(
                HTTPStatus.OK,
                {
                    "review_item_id": result.review_item_id,
                    "review_item_ids": targets,
                    "updated_count": len(targets),
                    "from_status": result.previous_status,
                    "to_status": result.current_status,
                    "note": result.note,
                },
            )

        def _handle_watchlist_update(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            params = {
                key: values[-1]
                for key, values in parse_qs(body.decode("utf-8")).items()
            }
            action = params.get("action", "").strip()
            ticker = params.get("ticker", "").strip().upper()
            if not ticker:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": "missing_ticker"})
                return
            current = _load_watchlist(self.server.cfg)
            current_set = set(current)
            if action == "remove":
                current_set.discard(ticker)
            else:
                current_set.add(ticker)
            updated = _save_watchlist(self.server.cfg, list(current_set))
            self._write_json(HTTPStatus.OK, {"tickers": updated, "ticker": ticker, "action": action or "add"})

        def _handle_company_override_update(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            params = {key: values[-1] for key, values in parse_qs(body.decode("utf-8")).items()}
            ticker = params.get("ticker", "").strip().upper()
            if not ticker:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": "missing_ticker"})
                return
            clear = params.get("clear", "").strip().lower() in {"1", "true", "yes"}
            month_text = params.get("month", "").strip()
            try:
                month = None if clear else int(month_text)
            except ValueError:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_month"})
                return
            try:
                overrides = save_fiscal_year_override(
                    self.server.cfg.company_overrides.definition_path,
                    ticker=ticker,
                    month=month,
                )
            except ValueError as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            rederive_intelligence(self.server.cfg, ticker=ticker, limit=1)
            self._write_json(
                HTTPStatus.OK,
                {
                    "ticker": ticker,
                    "month": overrides.get(ticker),
                    "source": "override" if ticker in overrides else "default",
                },
            )

        def log_message(self, format: str, *args) -> None:  # noqa: A003 - stdlib name
            get_logger("stock_triage.dashboard").info(
                "dashboard request",
                extra={"request": format % args},
            )

        def _serve_artifact(self, raw_path: str, *, emit_body: bool) -> None:
            if not raw_path:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": "missing_path"}, emit_body=emit_body)
                return
            path = Path(raw_path).expanduser().resolve()
            if not _is_allowed_artifact_path(path, self.server.cfg):
                self._write_json(HTTPStatus.FORBIDDEN, {"error": "path_not_allowed"}, emit_body=emit_body)
                return
            if not path.exists() or not path.is_file():
                self._write_json(HTTPStatus.NOT_FOUND, {"error": "file_not_found"}, emit_body=emit_body)
                return
            mime, _ = mimetypes.guess_type(str(path))
            content_type = mime or "application/octet-stream"
            total_size = path.stat().st_size
            range_header = self.headers.get("Range")
            try:
                start, end = _parse_range_header(range_header, total_size)
            except ValueError:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE.value)
                self.send_header("Content-Range", f"bytes */{total_size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            length = end - start + 1
            status = HTTPStatus.PARTIAL_CONTENT if range_header else HTTPStatus.OK
            self.send_response(status.value)
            self.send_header("Content-Type", content_type)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if range_header:
                self.send_header("Content-Range", f"bytes {start}-{end}/{total_size}")
            self.end_headers()
            if not emit_body:
                return
            _stream_file_range(self.wfile, path, start=start, length=length)

        def _serve_action_stream(self, job_id: str) -> None:
            if not job_id:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": "missing_job_id"})
                return
            with self.server.history_lock:
                job = self.server.jobs.get(job_id)
            if job is None:
                self._write_json(HTTPStatus.NOT_FOUND, {"error": "job_not_found"})
                return

            self.send_response(HTTPStatus.OK.value)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            offset = 0
            while True:
                events, finished = job.wait_for_events(offset)
                for event in events:
                    payload = json.dumps(event, ensure_ascii=True)
                    self.wfile.write(f"event: {event['type']}\n".encode("utf-8"))
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    offset += 1
                if finished and offset >= len(job.events):
                    self.close_connection = True
                    break

        def _start_action_job(self, action: str, params: dict[str, str]) -> DashboardJobState:
            job = DashboardJobState(
                job_id=uuid.uuid4().hex[:12],
                action=action,
                timestamp=datetime.now(timezone.utc).isoformat(),
                params=dict(params),
            )
            with self.server.history_lock:
                self.server.jobs[job.job_id] = job
                self._prune_jobs_locked()

            thread = threading.Thread(
                target=self._run_action_job,
                args=(job, action, params),
                daemon=True,
            )
            thread.start()
            return job

        def _run_action_job(self, job: DashboardJobState, action: str, params: dict[str, str]) -> None:
            try:
                self.server.action_runner(self.server.cfg, action, params, job)
                if not job.finished:
                    job.finish(returncode=0, ok=True)
            except ValueError as exc:
                job.append_output("stderr", f"{exc}\n")
                job.finish(returncode=2, ok=False)
            except Exception as exc:  # noqa: BLE001 - keep UI responsive on failures
                job.append_output("stderr", f"{type(exc).__name__}: {exc}\n")
                job.finish(returncode=1, ok=False)

            with self.server.history_lock:
                self.server.action_history.insert(0, job.to_history_entry())
                del self.server.action_history[10:]
                self._prune_jobs_locked()

        def _prune_jobs_locked(self) -> None:
            stale = [
                job_id
                for job_id, job in self.server.jobs.items()
                if job.finished
            ]
            if len(stale) <= 25:
                return
            stale.sort(key=lambda job_id: self.server.jobs[job_id].timestamp, reverse=True)
            for job_id in stale[25:]:
                self.server.jobs.pop(job_id, None)

        def _serve_static(self, asset_name: str, *, emit_body: bool) -> None:
            if "/" in asset_name or not asset_name:
                self._write_json(HTTPStatus.NOT_FOUND, {"error": "asset_not_found"}, emit_body=emit_body)
                return
            try:
                payload = read_static_asset(asset_name)
            except FileNotFoundError:
                self._write_json(HTTPStatus.NOT_FOUND, {"error": "asset_not_found"}, emit_body=emit_body)
                return
            mime, _ = mimetypes.guess_type(asset_name)
            self._write_bytes(HTTPStatus.OK, mime or "application/octet-stream", payload, emit_body=emit_body)

        def _write_json(self, status: HTTPStatus, payload: dict[str, object], *, emit_body: bool = True) -> None:
            self._write_bytes(
                status,
                "application/json; charset=utf-8",
                json.dumps(payload, ensure_ascii=True).encode("utf-8"),
                emit_body=emit_body,
            )

        def _write_bytes(self, status: HTTPStatus, content_type: str, payload: bytes, *, emit_body: bool = True) -> None:
            self.send_response(status.value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if emit_body:
                self.wfile.write(payload)

    return DashboardHandler


def _snapshot_payload(cfg: Config, action_history: list[dict[str, object]]) -> dict[str, object]:
    snapshot = load_dashboard_snapshot(cfg)
    return {
        "issuers": snapshot.issuers,
        "market_monitor": snapshot.market_monitor,
        "source_sync_rows": snapshot.source_sync_rows,
        "documents": snapshot.documents,
        "metrics": snapshot.metrics,
        "findings": snapshot.findings,
        "revisions": snapshot.revisions,
        "company_snapshots": snapshot.company_snapshots,
        "sector_rollups": snapshot.sector_rollups,
        "inbox_rows": snapshot.inbox_rows,
        "digest_rows": snapshot.digest_rows,
        "review_rows": snapshot.review_rows,
        "intelligence_kpis": snapshot.intelligence_kpis,
        "backlog_rows": snapshot.backlog_rows,
        "canary_rows": snapshot.canary_rows,
        "action_history": list(action_history),
        "watchlist": _load_watchlist(cfg),
    }


def _csv_bytes(*, fieldnames: list[str], rows: list[dict[str, object]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _run_cli_action(cfg: Config, action: str, params: dict[str, str], job: DashboardJobState) -> None:
    command = _build_cli_command(action, params)
    env = os.environ.copy()
    full_command = [sys.executable, "-m", "stock_triage", *command]
    job.set_command(full_command)

    process = subprocess.Popen(
        full_command,
        cwd=REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
    )
    readers = [
        threading.Thread(target=_pipe_stream, args=(process.stdout, job, "stdout"), daemon=True),
        threading.Thread(target=_pipe_stream, args=(process.stderr, job, "stderr"), daemon=True),
    ]
    for reader in readers:
        reader.start()
    returncode = process.wait()
    for reader in readers:
        reader.join(timeout=1)
    job.finish(returncode=returncode, ok=returncode == 0)


def _build_cli_command(action: str, params: dict[str, str]) -> list[str]:
    limit = _int_arg(params.get("limit", ""), default=5, minimum=1)
    year = _int_arg(params.get("year", ""), default=datetime.now().year, minimum=2000)
    document_id = params.get("document_id", "").strip()
    ticker = params.get("ticker", "").strip().upper()
    mapping = {
        "discover_company": ["discover", "company_universe"],
        "discover_disclosure": ["discover", "disclosure"],
        "discover_financial": ["discover", "financial_report", "--year", str(year)],
        "download_financial": ["download", "--source", "idx_financial_reports", "--limit", str(limit)],
        "download_disclosure": ["download", "--source", "idx_disclosures", "--limit", str(limit)],
        "parse": ["parse", "--source", "all", "--limit", str(limit)],
        "extract": ["extract", "--source", "all", "--limit", str(limit)],
        "rules": ["rules", "--source", "all", "--limit", str(limit)],
        "alerts": ["alerts-run"],
        "canary": ["canary-run", "--source", "all"],
        "export": ["dashboard-export"],
        "digest": ["digest-run"],
        "rederive_all": ["intelligence-rederive", "--limit", str(limit)],
    }
    if action == "golden":
        if ticker:
            return ["intelligence-golden", "--ticker", ticker, "--limit", "1"]
        return ["intelligence-golden", "--limit", str(limit)]
    if action == "rederive_company":
        if not ticker:
            raise ValueError("ticker is required for company rerun")
        return ["intelligence-rederive", "--ticker", ticker, "--limit", "1"]
    if action == "summary":
        if not document_id:
            raise ValueError("document_id is required for summary generation")
        return ["summary-generate", document_id]
    if action == "parse_one":
        if not document_id:
            raise ValueError("document_id is required for single-document parse")
        return ["parse", "--document-id", document_id]
    if action == "extract_one":
        if not document_id:
            raise ValueError("document_id is required for single-document extract")
        return ["extract", "--document-id", document_id]
    if action == "rules_one":
        if not document_id:
            raise ValueError("document_id is required for single-document rules")
        return ["rules", "--document-id", document_id]
    if action not in mapping:
        raise ValueError(f"unknown action: {action}")
    return mapping[action]


def _load_rerun_preview(cfg: Config, *, ticker: str | None) -> dict[str, object]:
    if ticker:
        ticker_upper = ticker.upper()
        company_detail = load_company_detail(cfg, ticker_upper)
        review_rows = load_review_queue(cfg, status="open", limit=100, ticker=ticker_upper).rows
        company_snapshot = company_detail.get("company_snapshot")
        return {
            "scope": "ticker",
            "ticker": ticker_upper,
            "document_count": len(company_detail.get("documents") or []),
            "open_review_count": len(review_rows),
            "snapshot_updated_at": company_snapshot.get("updated_at") if company_snapshot else None,
            "actions": ["rederive_company", "golden"],
        }
    snapshot = load_dashboard_snapshot(cfg)
    return {
        "scope": "global",
        "snapshot_count": len(snapshot.company_snapshots),
        "open_review_count": len(snapshot.review_rows),
        "late_filing_companies": int(snapshot.intelligence_kpis.get("late_filing_companies") or 0),
        "actions": ["rederive_all", "golden", "digest"],
    }


def _int_arg(raw: str, *, default: int, minimum: int) -> int:
    if not raw:
        return default
    value = int(raw)
    if value < minimum:
        raise ValueError(f"value must be >= {minimum}")
    return value


def _pipe_stream(stream, job: DashboardJobState, stream_name: str) -> None:
    if stream is None:
        return
    try:
        for line in iter(stream.readline, ""):
            if not line:
                break
            job.append_output(stream_name, line)
    finally:
        stream.close()


_ARTIFACT_CHUNK_SIZE = 64 * 1024


def _parse_range_header(header: str | None, total_size: int) -> tuple[int, int]:
    """Return an inclusive ``(start, end)`` tuple for a ``Range: bytes=`` header.

    Raises ``ValueError`` for malformed or unsatisfiable ranges so the caller
    can return a ``416`` response. When ``header`` is ``None`` the caller
    receives the full file range.
    """

    if header is None:
        if total_size == 0:
            return 0, 0
        return 0, total_size - 1
    if not header.startswith("bytes="):
        raise ValueError("invalid_range_unit")
    spec = header[len("bytes="):].strip()
    if "," in spec:
        spec = spec.split(",", 1)[0].strip()
    if "-" not in spec:
        raise ValueError("invalid_range")
    start_raw, end_raw = spec.split("-", 1)
    if start_raw == "":
        if end_raw == "":
            raise ValueError("invalid_range")
        suffix = int(end_raw)
        if suffix <= 0 or total_size == 0:
            raise ValueError("invalid_range")
        start = max(0, total_size - suffix)
        end = total_size - 1
    else:
        start = int(start_raw)
        end = int(end_raw) if end_raw != "" else total_size - 1
    if start < 0 or end < start or start >= total_size:
        raise ValueError("invalid_range")
    end = min(end, total_size - 1)
    return start, end


def _stream_file_range(wfile, path: Path, *, start: int, length: int) -> None:
    with path.open("rb") as handle:
        handle.seek(start)
        remaining = length
        while remaining > 0:
            chunk = handle.read(min(_ARTIFACT_CHUNK_SIZE, remaining))
            if not chunk:
                break
            try:
                wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                return
            remaining -= len(chunk)


def _is_allowed_artifact_path(path: Path, cfg: Config) -> bool:
    allowed_roots = [
        cfg.paths.data_dir.resolve(),
        cfg.dashboard.output_dir.resolve(),
    ]
    if cfg.paths.quarantine_dir is not None:
        allowed_roots.append(cfg.paths.quarantine_dir.resolve())
    return any(_is_relative_to(path, root) for root in allowed_roots)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
