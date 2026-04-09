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
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

from ..config import Config
from ..logging_setup import get_logger
from .pipeline import load_dashboard_snapshot, render_dashboard_html

REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class DashboardActionResult:
    action: str
    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    timestamp: str
    ok: bool


@dataclass(frozen=True)
class DashboardServerHandle:
    server: ThreadingHTTPServer
    url: str


ActionRunner = Callable[[Config, str, dict[str, str]], DashboardActionResult]


class DashboardHTTPServer(ThreadingHTTPServer):
    cfg: Config
    title: str
    action_history: list[dict[str, object]]
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
                    documents=snapshot["documents"],
                    metrics=snapshot["metrics"],
                    findings=snapshot["findings"],
                    revisions=snapshot["revisions"],
                    backlog_rows=snapshot["backlog_rows"],
                    canary_rows=snapshot["canary_rows"],
                    metrics_href="/metrics.csv",
                    findings_href="/findings.csv",
                    action_endpoint="/api/action",
                    snapshot_endpoint="/api/snapshot",
                    artifact_endpoint="/artifact",
                    action_history=snapshot["action_history"],
                )
                self._write_bytes(HTTPStatus.OK, "text/html; charset=utf-8", html.encode("utf-8"), emit_body=emit_body)
                return
            if parsed.path == "/api/snapshot":
                self._write_json(HTTPStatus.OK, _snapshot_payload(self.server.cfg, self.server.action_history), emit_body=emit_body)
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
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "not_found"}, emit_body=emit_body)

        def do_POST(self) -> None:  # noqa: N802 - stdlib interface
            parsed = urlparse(self.path)
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
                result = self.server.action_runner(self.server.cfg, action, params)
            except ValueError as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            with self.server.history_lock:
                self.server.action_history.insert(0, result.__dict__)
                del self.server.action_history[10:]
                history = list(self.server.action_history)
            response = {
                **result.__dict__,
                "snapshot": _snapshot_payload(self.server.cfg, history),
            }
            self._write_json(HTTPStatus.OK if result.ok else HTTPStatus.INTERNAL_SERVER_ERROR, response)

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
            self._write_bytes(HTTPStatus.OK, mime or "application/octet-stream", path.read_bytes(), emit_body=emit_body)

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
        "documents": snapshot.documents,
        "metrics": snapshot.metrics,
        "findings": snapshot.findings,
        "revisions": snapshot.revisions,
        "backlog_rows": snapshot.backlog_rows,
        "canary_rows": snapshot.canary_rows,
        "action_history": list(action_history),
    }


def _csv_bytes(*, fieldnames: list[str], rows: list[dict[str, object]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _run_cli_action(cfg: Config, action: str, params: dict[str, str]) -> DashboardActionResult:
    command = _build_cli_command(action, params)
    env = os.environ.copy()
    completed = subprocess.run(
        [sys.executable, "-m", "stock_triage", *command],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return DashboardActionResult(
        action=action,
        command=[sys.executable, "-m", "stock_triage", *command],
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        timestamp=datetime.now(timezone.utc).isoformat(),
        ok=completed.returncode == 0,
    )


def _build_cli_command(action: str, params: dict[str, str]) -> list[str]:
    limit = _int_arg(params.get("limit", ""), default=5, minimum=1)
    year = _int_arg(params.get("year", ""), default=datetime.now().year, minimum=2000)
    document_id = params.get("document_id", "").strip()
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
    }
    if action == "summary":
        if not document_id:
            raise ValueError("document_id is required for summary generation")
        return ["summary-generate", document_id]
    if action not in mapping:
        raise ValueError(f"unknown action: {action}")
    return mapping[action]


def _int_arg(raw: str, *, default: int, minimum: int) -> int:
    if not raw:
        return default
    value = int(raw)
    if value < minimum:
        raise ValueError(f"value must be >= {minimum}")
    return value


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
