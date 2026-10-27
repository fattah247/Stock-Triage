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
from urllib.parse import parse_qs, urlparse, urlsplit

from ..config import Config
from ..config.loader import REPO_ROOT
from ..fiscal_year import load_fiscal_year_overrides, save_fiscal_year_override
from ..intelligence import rederive_intelligence
from ..intelligence.review import DEFAULT_SNOOZE_DAYS, load_review_queue, update_review_statuses
from ..logging_setup import get_logger
from .assets import read_static_asset
from .pipeline import FINDINGS_CSV_FIELDS, METRICS_CSV_FIELDS, load_dashboard_snapshot, render_dashboard_html
from ..runlock import PIPELINE_COMMANDS, lock_path_for, read_lock_holder
from .runs import (
    RUN_CANCELLED,
    RUN_FAILED,
    RUN_OK,
    RUN_RUNNING,
    SEQUENCE_LABELS,
    STEP_BUSY,
    STEP_CANCELLED,
    STEP_FAILED,
    STEP_QUEUED,
    STEP_RUNNING,
    STEP_SKIPPED,
    RunLedger,
    SequenceStep,
    classify_step,
    describe_sequences,
    describe_step,
    sequence_steps,
    stop_process,
    summarize_run,
)
from .market import load_market_view
from .stocks import load_overview, load_portfolio_view, load_stock_view, load_stocks_view
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
    label: str = ""
    status: str = RUN_RUNNING
    steps: list[dict[str, object]] = field(default_factory=list)
    finished_at: str | None = None
    plan: list[SequenceStep] = field(default_factory=list, repr=False)
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _process: subprocess.Popen | None = field(default=None, repr=False)
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

    def update_step(self, index: int, **fields: object) -> None:
        with self._condition:
            self.steps[index].update(fields)
            self._push_event("step", {"index": index, **self.steps[index]})
            self._condition.notify_all()

    def finish(self, *, returncode: int, ok: bool, status: str | None = None) -> None:
        with self._condition:
            self.returncode = returncode
            self.ok = ok
            self.status = status or (RUN_OK if ok else RUN_FAILED)
            self.finished = True
            self.finished_at = datetime.now(timezone.utc).isoformat()
            self.duration_ms = int((time.monotonic() - self._started_at) * 1000)
            self._push_event(
                "complete",
                {
                    "returncode": returncode,
                    "ok": ok,
                    "status": self.status,
                    "duration_ms": self.duration_ms,
                    "steps": [dict(step) for step in self.steps],
                },
            )
            self._condition.notify_all()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel.is_set()

    def attach_process(self, process: subprocess.Popen | None) -> None:
        with self._condition:
            self._process = process
        if process is not None and self.cancel_requested:
            threading.Thread(target=stop_process, args=(process,), daemon=True).start()

    def request_cancel(self) -> bool:
        # A second SIGINT would interrupt the step's own cleanup.
        if self.finished or self._cancel.is_set():
            return False
        self._cancel.set()
        with self._condition:
            process = self._process
        if process is not None:
            threading.Thread(target=stop_process, args=(process,), daemon=True).start()
        self.append_output("stderr", "Cancellation requested; stopping the current step.\n")
        return True

    def wait_for_events(self, offset: int, *, timeout_s: float = 1.0) -> tuple[list[dict[str, object]], bool]:
        with self._condition:
            if offset >= len(self.events) and not self.finished:
                self._condition.wait(timeout=timeout_s)
            return self.events[offset:], self.finished

    def summary(self) -> dict[str, object]:
        with self._condition:
            return {
                "job_id": self.job_id,
                "action": self.action,
                "params": dict(self.params),
                "label": self.label,
                "status": self.status,
                "timestamp": self.timestamp,
                "finished": self.finished,
                "cancel_requested": self.cancel_requested,
                "steps": [dict(step) for step in self.steps],
                "event_count": len(self.events),
                "stream_url": f"/api/action/stream?job_id={self.job_id}",
            }

    def to_history_entry(self) -> dict[str, object]:
        entry = DashboardActionResult(
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
        entry.update(
            run_id=self.job_id,
            label=self.label,
            status=self.status,
            finished_at=self.finished_at,
            steps=[dict(step) for step in self.steps],
        )
        return entry

    def _push_event(self, event_type: str, payload: dict[str, object]) -> None:
        self._event_seq += 1
        self.events.append(
            {
                "id": self._event_seq,
                "type": event_type,
                **payload,
            }
        )


class _StepContext:
    """What an action runner sees while executing one step of a run.

    Output streams into the parent run; ``finish`` records the step's exit
    code instead of finishing the whole run.
    """

    def __init__(self, job: DashboardJobState) -> None:
        self._job = job
        self.returncode: int | None = None

    def set_command(self, command: list[str]) -> None:
        self._job.set_command(command)

    def append_output(self, stream: str, text: str) -> None:
        self._job.append_output(stream, text)

    def attach_process(self, process: subprocess.Popen | None) -> None:
        self._job.attach_process(process)

    @property
    def cancel_requested(self) -> bool:
        return self._job.cancel_requested

    def finish(self, *, returncode: int, ok: bool) -> None:
        del ok
        self.returncode = returncode


class JobConflictError(RuntimeError):
    def __init__(
        self,
        *,
        active: DashboardJobState | None = None,
        external_lock: dict[str, object] | None = None,
    ) -> None:
        super().__init__("a dashboard run is active" if active is not None else "a pipeline command holds the lock")
        self.active = active
        self.external_lock = external_lock

    def payload(self) -> dict[str, object]:
        if self.active is not None:
            return {"error": "job_already_running", "active_job": self.active.summary(), "external_lock": None}
        return {"error": "pipeline_busy", "active_job": None, "external_lock": self.external_lock}


ActionRunner = Callable[[Config, str, dict[str, str], _StepContext], None]


class DashboardHTTPServer(ThreadingHTTPServer):
    cfg: Config
    title: str
    action_history: list[dict[str, object]]
    jobs: dict[str, DashboardJobState]
    active_job_id: str | None
    action_runner: ActionRunner
    history_lock: threading.Lock
    ledger: RunLedger


def _run_ledger_path(cfg: Config) -> Path:
    return cfg.paths.logs_dir / "dashboard_runs.jsonl"


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
    server.ledger = RunLedger(_run_ledger_path(cfg))
    server.action_history = server.ledger.recent(10)
    server.jobs = {}
    server.active_job_id = None
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
            if not self._request_is_trusted(state_changing=False, emit_body=emit_body):
                return
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
                    run_sequences=describe_sequences(),
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
            if parsed.path == "/api/market":
                self._write_json(HTTPStatus.OK, load_market_view(self.server.cfg), emit_body=emit_body)
                return
            if parsed.path == "/api/overview":
                self._write_json(HTTPStatus.OK, load_overview(self.server.cfg, _load_watchlist(self.server.cfg)), emit_body=emit_body)
                return
            if parsed.path == "/api/stocks":
                self._write_json(HTTPStatus.OK, load_stocks_view(self.server.cfg, _load_watchlist(self.server.cfg)), emit_body=emit_body)
                return
            if parsed.path == "/api/stock":
                ticker = parse_qs(parsed.query).get("ticker", [""])[0].strip().upper()
                detail = load_stock_view(self.server.cfg, ticker, _load_watchlist(self.server.cfg)) if ticker else None
                if detail is None:
                    self._write_json(HTTPStatus.NOT_FOUND, {"error": "unknown_ticker", "ticker": ticker}, emit_body=emit_body)
                    return
                self._write_json(HTTPStatus.OK, detail, emit_body=emit_body)
                return
            if parsed.path == "/api/portfolio":
                self._write_json(HTTPStatus.OK, load_portfolio_view(self.server.cfg, _load_watchlist(self.server.cfg)), emit_body=emit_body)
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
                try:
                    offset = max(0, int(params.get("offset", ["0"])[0] or 0))
                except ValueError:
                    offset = 0
                self._serve_action_stream(job_id, offset=offset)
                return
            if parsed.path == "/api/jobs/active":
                self._write_json(HTTPStatus.OK, self._active_job_payload(), emit_body=emit_body)
                return
            if parsed.path == "/metrics.csv":
                snapshot = load_dashboard_snapshot(self.server.cfg)
                payload = _csv_bytes(fieldnames=METRICS_CSV_FIELDS, rows=snapshot.metrics)
                self._write_bytes(HTTPStatus.OK, "text/csv; charset=utf-8", payload, emit_body=emit_body)
                return
            if parsed.path == "/findings.csv":
                snapshot = load_dashboard_snapshot(self.server.cfg)
                payload = _csv_bytes(fieldnames=FINDINGS_CSV_FIELDS, rows=snapshot.findings)
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
            if not self._request_is_trusted(state_changing=True):
                return
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
            if parsed.path == "/api/jobs/cancel":
                self._handle_job_cancel()
                return
            if parsed.path != "/api/action":
                self._write_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            params = self._read_form()
            action = params.get("action", "").strip()
            if not action:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": "missing_action"})
                return
            try:
                plan = sequence_steps(action, params)
                commands = [_build_cli_command(step.action, {**params, **dict(step.params)}) for step in plan]
            except ValueError as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            try:
                job = self._start_action_job(
                    action,
                    params,
                    plan,
                    needs_pipeline_lock=any(command[0] in PIPELINE_COMMANDS for command in commands),
                )
            except JobConflictError as exc:
                self._write_json(HTTPStatus.CONFLICT, exc.payload())
                return
            response = {
                **job.summary(),
                "stream_url": f"/api/action/stream?job_id={job.job_id}",
            }
            self._write_json(HTTPStatus.ACCEPTED, response)

        def _handle_job_cancel(self) -> None:
            params = self._read_form()
            requested = params.get("job_id", "").strip()
            with self.server.history_lock:
                job = self.server.jobs.get(self.server.active_job_id or "")
            if job is None or job.finished or (requested and requested != job.job_id):
                self._write_json(HTTPStatus.CONFLICT, {"error": "no_active_job"})
                return
            job.request_cancel()
            self._write_json(HTTPStatus.ACCEPTED, {**job.summary(), "status": "cancelling"})

        def _active_job_payload(self) -> dict[str, object]:
            # A finished run stays "active" until it is in the history, so a page
            # reconnecting in between can still read its outcome from the stream.
            with self.server.history_lock:
                job = self.server.jobs.get(self.server.active_job_id or "")
            active = job.summary() if job is not None else None
            return {
                "active_job": active,
                "external_lock": None
                if active is not None
                else read_lock_holder(lock_path_for(self.server.cfg.paths.data_dir)),
            }

        def _read_form(self) -> dict[str, str]:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            return {key: values[-1] for key, values in parse_qs(body.decode("utf-8")).items()}

        def _handle_review_update(self) -> None:
            params = self._read_form()
            review_item_id = params.get("review_item_id", "").strip()
            review_item_ids = [item.strip() for item in params.get("review_item_ids", "").split(",") if item.strip()]
            status = params.get("status", "").strip()
            note = params.get("note", "").strip() or None
            if (not review_item_id and not review_item_ids) or not status:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": "missing_review_item_id_or_status"})
                return
            try:
                targets = review_item_ids or [review_item_id]
                snooze_days = _int_arg(params.get("snooze_days", ""), default=DEFAULT_SNOOZE_DAYS, minimum=1)
                results = update_review_statuses(
                    self.server.cfg,
                    review_item_ids=targets,
                    status=status,
                    note=note,
                    snooze_days=snooze_days,
                )
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
            params = self._read_form()
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
            params = self._read_form()
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

        def _request_is_trusted(self, *, state_changing: bool, emit_body: bool = True) -> bool:
            """Refuse DNS-rebinding and cross-site requests.

            Any page open in the operator's browser can reach this server, which
            launches pipeline runs and streams local files. A foreign ``Host`` is
            refused, and state-changing requests must come from this origin;
            non-browser clients such as curl send neither header and pass.
            """
            host_header = self.headers.get("Host", "")
            if not _host_header_allowed(host_header, self.server.server_address[0]):
                self._write_json(HTTPStatus.FORBIDDEN, {"error": "host_not_allowed"}, emit_body=emit_body)
                return False
            if not state_changing:
                return True
            origin = self.headers.get("Origin")
            if origin is not None and urlsplit(origin).netloc.lower() != host_header.lower():
                self._write_json(HTTPStatus.FORBIDDEN, {"error": "cross_origin_request_blocked"})
                return False
            fetch_site = self.headers.get("Sec-Fetch-Site")
            if fetch_site is not None and fetch_site not in {"same-origin", "none"}:
                self._write_json(HTTPStatus.FORBIDDEN, {"error": "cross_origin_request_blocked"})
                return False
            return True

        def end_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            super().end_headers()

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
            content_type, extension = _sniff_artifact_type(path)
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
            # Content-addressed files have no extension; name them so PDFs open inline.
            disposition = "inline" if content_type == "application/pdf" else "attachment"
            filename = path.name if path.suffix else f"{path.name[:16]}{extension}"
            self.send_header("Content-Disposition", f'{disposition}; filename="{filename}"')
            if range_header:
                self.send_header("Content-Range", f"bytes {start}-{end}/{total_size}")
            self.end_headers()
            if not emit_body:
                return
            _stream_file_range(self.wfile, path, start=start, length=length)

        def _serve_action_stream(self, job_id: str, *, offset: int = 0) -> None:
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

            while True:
                events, finished = job.wait_for_events(offset)
                try:
                    for event in events:
                        payload = json.dumps(event, ensure_ascii=True)
                        self.wfile.write(f"id: {event['id']}\nevent: {event['type']}\n".encode("utf-8"))
                        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                        self.wfile.flush()
                        offset += 1
                except (BrokenPipeError, ConnectionResetError):
                    # The viewer went away; the run itself keeps going.
                    self.close_connection = True
                    return
                if finished and offset >= len(job.events):
                    self.close_connection = True
                    break

        def _start_action_job(
            self,
            action: str,
            params: dict[str, str],
            plan: list[SequenceStep],
            *,
            needs_pipeline_lock: bool,
        ) -> DashboardJobState:
            job = DashboardJobState(
                job_id=uuid.uuid4().hex[:12],
                action=action,
                timestamp=datetime.now(timezone.utc).isoformat(),
                params=dict(params),
                label=SEQUENCE_LABELS.get(action, ""),
                steps=[{**describe_step(step), "status": STEP_QUEUED} for step in plan],
                plan=list(plan),
            )
            with self.server.history_lock:
                # The dashboard's own run holds the pipeline lock through its
                # child process, so check for it before the external lock.
                active = self.server.jobs.get(self.server.active_job_id or "")
                if active is not None and not active.finished:
                    raise JobConflictError(active=active)
                if needs_pipeline_lock:
                    holder = read_lock_holder(lock_path_for(self.server.cfg.paths.data_dir))
                    if holder is not None:
                        raise JobConflictError(external_lock=holder)
                self.server.jobs[job.job_id] = job
                self.server.active_job_id = job.job_id
                self._prune_jobs_locked()

            thread = threading.Thread(
                target=self._run_action_job,
                args=(job,),
                daemon=True,
            )
            thread.start()
            return job

        def _run_action_job(self, job: DashboardJobState) -> None:
            try:
                _execute_plan(self.server.cfg, job, self.server.action_runner)
            finally:
                if not job.finished:
                    job.finish(returncode=1, ok=False)
                entry = RunLedger.trimmed(job.to_history_entry())
                try:
                    self.server.ledger.append(entry)
                except OSError as exc:
                    get_logger("stock_triage.dashboard").warning(
                        "could not persist dashboard run", extra={"error": str(exc)}
                    )
                with self.server.history_lock:
                    if self.server.active_job_id == job.job_id:
                        self.server.active_job_id = None
                    self.server.action_history.insert(0, entry)
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
            if content_type.startswith("text/html"):
                # HTML only: a CSP on PDF responses can disable the browser's viewer.
                self.send_header("Content-Security-Policy", _CONTENT_SECURITY_POLICY)
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


def _csv_bytes(*, fieldnames: tuple[str, ...], rows: list[dict[str, object]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _execute_plan(cfg: Config, job: DashboardJobState, runner: ActionRunner) -> None:
    """Run every step of ``job``'s plan and finish it with an overall status."""
    step_states: list[str] = []
    returncodes: list[int] = []
    network_failure: str | None = None
    blocked = False
    for index, step in enumerate(job.plan):
        if job.cancel_requested:
            job.update_step(index, status=STEP_CANCELLED, note="did not start")
            step_states.append(STEP_CANCELLED)
            continue
        if blocked:
            job.update_step(index, status=STEP_SKIPPED, note="another pipeline command held the lock")
            step_states.append(STEP_SKIPPED)
            continue
        if step.network and network_failure is not None:
            job.update_step(index, status=STEP_SKIPPED, note=f"{network_failure} could not reach IDX")
            step_states.append(STEP_SKIPPED)
            continue

        started = time.monotonic()
        job.update_step(index, status=STEP_RUNNING, started_at=datetime.now(timezone.utc).isoformat())
        context = _StepContext(job)
        try:
            runner(cfg, step.action, {**job.params, **dict(step.params)}, context)
        except ValueError as exc:
            context.append_output("stderr", f"{exc}\n")
            context.returncode = 2
        except Exception as exc:  # noqa: BLE001 - keep the run and UI alive
            context.append_output("stderr", f"{type(exc).__name__}: {exc}\n")
            context.returncode = 1
        returncode = 0 if context.returncode is None else context.returncode
        state = classify_step(returncode=returncode, cancelled=job.cancel_requested and returncode != 0)
        job.update_step(
            index,
            status=state,
            returncode=returncode,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        step_states.append(state)
        returncodes.append(returncode)
        if state == STEP_FAILED and step.network:
            network_failure = step.label or step.action
        if state == STEP_BUSY:
            blocked = True

    status = summarize_run(step_states)
    if status == RUN_CANCELLED:
        returncode = 130
    else:
        returncode = next((code for code in returncodes if code != 0), 0)
    job.finish(returncode=returncode, ok=status == RUN_OK, status=status)


def _run_cli_action(cfg: Config, action: str, params: dict[str, str], job: _StepContext) -> None:
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
    job.attach_process(process)
    readers = [
        threading.Thread(target=_pipe_stream, args=(process.stdout, job, "stdout"), daemon=True),
        threading.Thread(target=_pipe_stream, args=(process.stderr, job, "stderr"), daemon=True),
    ]
    for reader in readers:
        reader.start()
    returncode = process.wait()
    for reader in readers:
        reader.join(timeout=1)
    job.attach_process(None)
    job.finish(returncode=returncode, ok=returncode == 0)


def _build_cli_command(action: str, params: dict[str, str]) -> list[str]:
    limit = _int_arg(params.get("limit", ""), default=5, minimum=1)
    year = params.get("year", "").strip()
    # No --year lets the adapter scan its rolling current + previous report year.
    financial_year = ["--year", str(_int_arg(year, default=0, minimum=2000))] if year else []
    document_id = params.get("document_id", "").strip()
    ticker = params.get("ticker", "").strip().upper()
    mapping = {
        "discover_company": ["discover", "company_universe"],
        "discover_disclosure": ["discover", "disclosure"],
        "discover_financial": ["discover", "financial_report", *financial_year],
        "download_financial": ["download", "--source", "idx_financial_reports", "--limit", str(limit)],
        "download_disclosure": ["download", "--source", "idx_disclosures", "--limit", str(limit)],
        "parse": ["parse", "--source", "all", "--limit", str(limit)],
        "extract": ["extract", "--source", "all", "--limit", str(limit)],
        "rules": ["rules", "--source", "all", "--limit", str(limit)],
        "intelligence": ["intelligence-run", "--limit", str(max(limit, 25))],
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
        documents = company_detail.get("documents") or []
        review_rows = load_review_queue(cfg, status="open", limit=100, ticker=ticker_upper).rows
        company_snapshot = company_detail.get("company_snapshot")
        return {
            "scope": "ticker",
            "ticker": ticker_upper,
            "document_count": len(documents),
            "analyzed_count": sum(1 for item in documents if item.get("process_state") == "analyzed"),
            "failed_count": sum(1 for item in documents if item.get("process_state") == "failed"),
            "latest_document_type": documents[0].get("document_type") if documents else None,
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


def _pipe_stream(stream, job: _StepContext, stream_name: str) -> None:
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
    # Only the stores the UI links to; never the database or the browser
    # profile (cookies, saved logins), even when they sit under an allowed root.
    allowed_roots = [
        cfg.paths.files_dir.resolve(),
        cfg.dashboard.output_dir.resolve(),
        cfg.browser.artifacts_dir.resolve(),
    ]
    if cfg.paths.quarantine_dir is not None:
        allowed_roots.append(cfg.paths.quarantine_dir.resolve())
    denied_roots = [
        cfg.browser.profile_dir.resolve(),
        cfg.paths.db_path.parent.resolve(),
    ]
    if any(_is_relative_to(path, root) for root in denied_roots):
        return False
    return any(_is_relative_to(path, root) for root in allowed_roots)


_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)

_LOOPBACK_HOSTNAMES = {"127.0.0.1", "localhost", "::1"}
_WILDCARD_BIND_HOSTS = {"", "0.0.0.0", "::"}


def _host_header_allowed(host_header: str, bind_host: str) -> bool:
    if not host_header:
        return False
    try:
        hostname = (urlsplit(f"//{host_header}").hostname or "").lower()
    except ValueError:
        return False
    if bind_host in _WILDCARD_BIND_HOSTS:
        # Binding every interface is an explicit opt-out: any Host is accepted,
        # and DNS rebinding can then also satisfy the Origin check.
        return True
    return hostname in _LOOPBACK_HOSTNAMES or hostname == bind_host.lower()


_ARTIFACT_SIGNATURES = (
    (b"%PDF-", "application/pdf", ".pdf"),
    (b"PK\x03\x04", "application/zip", ".zip"),
)


def _sniff_artifact_type(path: Path) -> tuple[str, str]:
    mime, _ = mimetypes.guess_type(str(path))
    if mime:
        return mime, path.suffix
    try:
        with path.open("rb") as handle:
            head = handle.read(8)
    except OSError:
        head = b""
    for signature, content_type, extension in _ARTIFACT_SIGNATURES:
        if head.startswith(signature):
            return content_type, extension
    return "application/octet-stream", ""


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
