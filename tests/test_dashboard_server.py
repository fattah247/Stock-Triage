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
from stock_triage.dashboard.server import (
    DashboardJobState,
    _build_cli_command,
    _parse_range_header,
    create_dashboard_server,
)

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
    assert _build_cli_command("digest", {}) == ["digest-run"]
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
        assert "Run Daily Sweep" in html
        assert "Advanced actions" in html
        assert "Daily Digest" in html
        assert "Open Review Queue" in html
        assert "Decision Snapshot" in html
        assert "Claims and Thesis" in html
        assert "Sector and Peer Explorer" in html
        assert "Copy command" in html
        app_js = urlopen(f"{handle.url}/static/app.js").read().decode("utf-8")
        assert "window.__DASHBOARD_BOOTSTRAP__" in html
        assert "function render()" in app_js
        assert "daily_sweep" in app_js
        assert "updateReviewStatus" in app_js
        assert "renderTimestamp" in app_js
        assert "re-parse" in app_js
        assert "re-extract" in app_js
        assert "run rules" in app_js

        snapshot = json.loads(urlopen(f"{handle.url}/api/snapshot").read().decode("utf-8"))
        assert snapshot["issuers"][0]["ticker"] == "TAMU"
        assert snapshot["inbox_rows"][0]["ticker"] == "TAMU"
        assert snapshot["inbox_rows"][0]["kind"] in {"document", "rule", "summary", "event"}
        assert snapshot["backlog_rows"][0]["source_name"] == "idx_financial_reports"
        assert snapshot["backlog_rows"][0]["analyzed"] == 2
        assert "review_rows" in snapshot
        assert "digest_rows" in snapshot
        assert "intelligence_kpis" in snapshot
        assert "company_snapshots" in snapshot
        assert "sector_rollups" in snapshot

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
        assert snapshot_after["action_history"][0]["params"]["limit"] == "3"
        assert snapshot_after["action_history"][0]["duration_ms"] >= 0
        assert calls == [("download_financial", {"action": "download_financial", "limit": "3"})]
    finally:
        handle.server.shutdown()
        handle.server.server_close()
        thread.join(timeout=5)


def test_dashboard_server_updates_review_status(tmp_config) -> None:
    from .test_intelligence_pipeline import _seed_phase10_inputs, _write_phase12_theses
    from stock_triage.intelligence import run_intelligence

    _seed_phase10_inputs(tmp_config)
    _write_phase12_theses(tmp_config)
    run_intelligence(tmp_config, ticker="TAMU")

    handle = create_dashboard_server(tmp_config, host="127.0.0.1", port=0)
    thread = threading.Thread(target=handle.server.serve_forever, daemon=True)
    thread.start()
    try:
        snapshot = json.loads(urlopen(f"{handle.url}/api/snapshot").read().decode("utf-8"))
        review_item_id = snapshot["review_rows"][0]["review_item_id"]
        request = Request(
            f"{handle.url}/api/review",
            data=urlencode({"review_item_id": review_item_id, "status": "resolved"}).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
            method="POST",
        )
        payload = json.loads(urlopen(request).read().decode("utf-8"))
        assert payload["review_item_id"] == review_item_id
        assert payload["to_status"] == "resolved"

        snapshot_after = json.loads(urlopen(f"{handle.url}/api/snapshot").read().decode("utf-8"))
        statuses = {row["review_item_id"]: row["status"] for row in snapshot_after["review_rows"]}
        assert review_item_id not in statuses
    finally:
        handle.server.shutdown()
        handle.server.server_close()
        thread.join(timeout=5)


def test_parse_range_header_handles_common_range_forms() -> None:
    # Full file when no header is supplied.
    assert _parse_range_header(None, 100) == (0, 99)
    # Explicit start and end.
    assert _parse_range_header("bytes=0-49", 100) == (0, 49)
    # Open-ended range clamps to file size.
    assert _parse_range_header("bytes=50-", 100) == (50, 99)
    # Suffix range returns the final N bytes.
    assert _parse_range_header("bytes=-20", 100) == (80, 99)
    # Unsatisfiable ranges raise ValueError for the handler to translate into 416.
    import pytest as _pytest

    for header in ("bytes=abc", "bytes=200-300", "items=0-10", "bytes=-0"):
        with _pytest.raises(ValueError):
            _parse_range_header(header, 100)


def test_dashboard_server_streams_artifact_range(tmp_config) -> None:
    payload = (b"".join(bytes([i % 256]) for i in range(4096))) + b"TAIL"
    artifact_path = tmp_config.paths.data_dir / "range-sample.bin"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(payload)

    handle = create_dashboard_server(tmp_config, host="127.0.0.1", port=0)
    thread = threading.Thread(target=handle.server.serve_forever, daemon=True)
    thread.start()
    try:
        path_query = quote(str(artifact_path), safe="")
        base_url = f"{handle.url}/artifact?path={path_query}"

        # Full body advertises Accept-Ranges so clients know they can seek.
        full_req = Request(base_url)
        with urlopen(full_req) as full_resp:
            full_body = full_resp.read()
            assert full_body == payload
            assert full_resp.headers.get("Accept-Ranges") == "bytes"
            assert int(full_resp.headers.get("Content-Length") or "0") == len(payload)

        # Explicit byte range returns 206 with the right slice.
        ranged_req = Request(base_url, headers={"Range": "bytes=10-19"})
        with urlopen(ranged_req) as ranged_resp:
            assert ranged_resp.status == 206
            assert ranged_resp.headers.get("Content-Range") == f"bytes 10-19/{len(payload)}"
            assert ranged_resp.read() == payload[10:20]

        # Suffix range returns the tail.
        suffix_req = Request(base_url, headers={"Range": "bytes=-4"})
        with urlopen(suffix_req) as suffix_resp:
            assert suffix_resp.status == 206
            assert suffix_resp.read() == b"TAIL"

        # Unsatisfiable range returns 416.
        bad_req = Request(base_url, headers={"Range": f"bytes={len(payload) + 10}-"})
        with pytest.raises(HTTPError) as excinfo:
            urlopen(bad_req)
        assert excinfo.value.code == 416
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


def test_dashboard_server_watchlist_add_remove(tmp_config) -> None:
    _seed_phase8_data(tmp_config)
    handle = create_dashboard_server(tmp_config, host="127.0.0.1", port=0)
    thread = threading.Thread(target=handle.server.serve_forever, daemon=True)
    thread.start()
    try:
        # Initially the watchlist is empty (no sidecar file).
        wl = json.loads(urlopen(f"{handle.url}/api/watchlist").read().decode("utf-8"))
        assert wl["tickers"] == []

        # Add a ticker via POST.
        add_req = Request(
            f"{handle.url}/api/watchlist",
            data=urlencode({"ticker": "TAMU", "action": "add"}).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
            method="POST",
        )
        add_resp = json.loads(urlopen(add_req).read().decode("utf-8"))
        assert "TAMU" in add_resp["tickers"]
        assert add_resp["action"] == "add"

        # Snapshot should include the watchlist.
        snapshot = json.loads(urlopen(f"{handle.url}/api/snapshot").read().decode("utf-8"))
        assert "TAMU" in snapshot["watchlist"]

        # Remove the ticker.
        rm_req = Request(
            f"{handle.url}/api/watchlist",
            data=urlencode({"ticker": "TAMU", "action": "remove"}).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
            method="POST",
        )
        rm_resp = json.loads(urlopen(rm_req).read().decode("utf-8"))
        assert "TAMU" not in rm_resp["tickers"]

        # Sidecar file should exist on disk.
        sidecar = tmp_config.paths.data_dir / "watchlist.json"
        assert sidecar.exists()
    finally:
        handle.server.shutdown()
        handle.server.server_close()
        thread.join(timeout=5)


def test_dashboard_serve_cli_uses_server(tmp_config, monkeypatch, capsys) -> None:
    monkeypatch.setattr("stock_triage.commands.dashboard.load_config", lambda: tmp_config)
    seen = {}

    def fake_serve_dashboard(cfg, host, port, open_browser):
        seen.update({"cfg": cfg, "host": host, "port": port, "open_browser": open_browser})
        return SimpleNamespace(url=f"http://{host}:{port}")

    monkeypatch.setattr("stock_triage.commands.dashboard.serve_dashboard", fake_serve_dashboard)

    rc = main(["dashboard-serve", "--host", "127.0.0.1", "--port", "9001"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "dashboard-serve: url=http://127.0.0.1:9001" in out
    assert seen["cfg"] is tmp_config
    assert seen["host"] == "127.0.0.1"
    assert seen["port"] == 9001
    assert seen["open_browser"] is False
