from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import pytest

from stock_triage.__main__ import main
from stock_triage.dashboard.server import DashboardJobState, _build_cli_command, create_dashboard_server

from .test_phase8_ops import _seed_phase8_data


def test_build_cli_command_maps_dashboard_actions() -> None:
    assert _build_cli_command("discover_company", {}) == ["discover", "company_universe"]
    assert _build_cli_command("discover_financial", {"year": "2025"}) == [
        "discover",
        "financial_report",
        "--year",
        "2025",
    ]
    assert _build_cli_command("download_disclosure", {"limit": "7"}) == [
        "download",
        "--source",
        "idx_disclosures",
        "--limit",
        "7",
    ]
    assert _build_cli_command("summary", {"document_id": "doc-123"}) == ["summary-generate", "doc-123"]
    assert _build_cli_command("parse_one", {"document_id": "doc-123"}) == ["parse", "--document-id", "doc-123"]
    assert _build_cli_command("extract_one", {"document_id": "doc-123"}) == ["extract", "--document-id", "doc-123"]
    assert _build_cli_command("rules_one", {"document_id": "doc-123"}) == ["rules", "--document-id", "doc-123"]
    with pytest.raises(ValueError):
        _build_cli_command("summary", {})


def test_dashboard_server_serves_snapshot_actions_and_artifacts(tmp_config) -> None:
    _seed_phase8_data(tmp_config)
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_runner(cfg, action: str, params: dict[str, str], job: DashboardJobState) -> None:
        calls.append((action, dict(params)))
        job.set_command(["python", "-m", "stock_triage", "download", "--limit", params.get("limit", "5")])
        job.append_output("stdout", "ok\n")
        job.finish(returncode=0, ok=True)

    handle = create_dashboard_server(tmp_config, host="127.0.0.1", port=0, action_runner=fake_runner)
    thread = threading.Thread(target=handle.server.serve_forever, daemon=True)
    thread.start()
    try:
        html = urlopen(handle.url).read().decode("utf-8")
        assert "Control Panel" in html
        app_js = urlopen(f"{handle.url}/static/app.js").read().decode("utf-8")
        assert "window.__DASHBOARD_BOOTSTRAP__" in html
        assert "function render()" in app_js
        assert "re-parse" in app_js
        assert "re-extract" in app_js
        assert "run rules" in app_js

        snapshot = json.loads(urlopen(f"{handle.url}/api/snapshot").read().decode("utf-8"))
        assert snapshot["issuers"][0]["ticker"] == "TAMU"
        assert snapshot["inbox_rows"][0]["ticker"] == "TAMU"
        assert snapshot["inbox_rows"][0]["kind"] in {"document", "rule", "summary", "event"}
        assert snapshot["backlog_rows"][0]["source_name"] == "idx_financial_reports"
        assert snapshot["backlog_rows"][0]["analyzed"] == 2

        local_path = quote(str(tmp_config.paths.data_dir / "tamu-annual-report.pdf"), safe="")
        artifact = urlopen(f"{handle.url}/artifact?path={local_path}").read()
        assert artifact.startswith(b"%PDF-1.4")

        request = Request(
            f"{handle.url}/api/action",
            data=urlencode({"action": "download_financial", "limit": "3"}).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
            method="POST",
        )
        action_payload = json.loads(urlopen(request).read().decode("utf-8"))
        assert action_payload["job_id"]
        stream_payload = urlopen(f"{handle.url}{action_payload['stream_url']}").read().decode("utf-8")
        assert "event: started" in stream_payload
        assert "event: stdout" in stream_payload
        assert "event: complete" in stream_payload

        snapshot_after = json.loads(urlopen(f"{handle.url}/api/snapshot").read().decode("utf-8"))
        assert snapshot_after["action_history"][0]["action"] == "download_financial"
        assert snapshot_after["action_history"][0]["duration_ms"] >= 0
        assert calls == [("download_financial", {"action": "download_financial", "limit": "3"})]
    finally:
        handle.server.shutdown()
        handle.server.server_close()
        thread.join(timeout=5)


def test_dashboard_server_rejects_paths_outside_data_dir(tmp_config) -> None:
    handle = create_dashboard_server(tmp_config, host="127.0.0.1", port=0)
    thread = threading.Thread(target=handle.server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(HTTPError) as excinfo:
            urlopen(f"{handle.url}/artifact?path={quote('/etc/hosts', safe='')}")
        assert excinfo.value.code == 403
    finally:
        handle.server.shutdown()
        handle.server.server_close()
        thread.join(timeout=5)


def test_dashboard_serve_cli_uses_server(tmp_config, monkeypatch, capsys) -> None:
    monkeypatch.setattr("stock_triage.__main__.load_config", lambda: tmp_config)
    seen = {}

    def fake_serve_dashboard(cfg, host, port, open_browser):
        seen.update({"cfg": cfg, "host": host, "port": port, "open_browser": open_browser})
        return SimpleNamespace(url=f"http://{host}:{port}")

    monkeypatch.setattr("stock_triage.__main__.serve_dashboard", fake_serve_dashboard)

    rc = main(["dashboard-serve", "--host", "127.0.0.1", "--port", "9001"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "dashboard-serve: url=http://127.0.0.1:9001" in out
    assert seen["cfg"] is tmp_config
    assert seen["host"] == "127.0.0.1"
    assert seen["port"] == 9001
    assert seen["open_browser"] is False
