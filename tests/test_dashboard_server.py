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
from stock_triage.db import WriterQueue, run_migrations
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
    assert _build_cli_command("golden", {"limit": "9"}) == ["intelligence-golden", "--limit", "9"]
    assert _build_cli_command("golden", {"ticker": "TAMU"}) == ["intelligence-golden", "--ticker", "TAMU", "--limit", "1"]
    assert _build_cli_command("rederive_all", {"limit": "8"}) == ["intelligence-rederive", "--limit", "8"]
    assert _build_cli_command("rederive_company", {"ticker": "TAMU"}) == ["intelligence-rederive", "--ticker", "TAMU", "--limit", "1"]
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
        assert "Decision board" in html
        assert "Summary" in html
        assert "Claims and Thesis" in html
        assert "Context and Comparison" in html
        assert "Observability" in html
        assert "Golden Checks" in html
        assert "Rerun Preview" in html
        assert "Home Modes" in html
        assert "Company Modes" in html
        assert "Ops Modes" in html
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
        assert "Stock-pick candidates" in app_js
        assert "Avoid candidates" in app_js
        assert "rebuild company intelligence" in app_js
        assert "fetchRerunPreview" in app_js

        snapshot = json.loads(urlopen(f"{handle.url}/api/snapshot").read().decode("utf-8"))
        assert snapshot["issuers"][0]["ticker"] == "TAMU"
        assert "market_monitor" in snapshot
        assert "counts" in snapshot["market_monitor"]
        assert "stock_pick_candidates" in snapshot["market_monitor"]
        assert "avoid_candidates" in snapshot["market_monitor"]
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


def test_dashboard_server_updates_grouped_review_status(tmp_config) -> None:
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
        grouped = next((row for row in snapshot["review_rows"] if len(row.get("review_item_ids") or []) > 1), None)
        assert grouped is not None
        request = Request(
            f"{handle.url}/api/review",
            data=urlencode({"review_item_ids": ",".join(grouped["review_item_ids"]), "status": "resolved"}).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
            method="POST",
        )
        payload = json.loads(urlopen(request).read().decode("utf-8"))
        assert payload["updated_count"] == len(grouped["review_item_ids"])

        snapshot_after = json.loads(urlopen(f"{handle.url}/api/snapshot").read().decode("utf-8"))
        remaining_ids = {
            item_id
            for row in snapshot_after["review_rows"]
            for item_id in (row.get("review_item_ids") or [row["review_item_id"]])
        }
        for item_id in grouped["review_item_ids"]:
            assert item_id not in remaining_ids
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


def test_dashboard_server_lazy_load_endpoints(tmp_config) -> None:
    from .test_intelligence_pipeline import _seed_phase10_inputs, _write_phase12_theses
    from stock_triage.intelligence import run_intelligence
    from stock_triage.intelligence.review import run_golden_set

    _seed_phase10_inputs(tmp_config)
    _write_phase12_theses(tmp_config)
    run_intelligence(tmp_config, ticker="TAMU")
    run_golden_set(tmp_config, ticker="TAMU")
    handle = create_dashboard_server(tmp_config, host="127.0.0.1", port=0)
    thread = threading.Thread(target=handle.server.serve_forever, daemon=True)
    thread.start()
    try:
        # Lite endpoint should return issuers and KPIs but not heavy sections.
        lite = json.loads(urlopen(f"{handle.url}/api/snapshot/lite").read().decode("utf-8"))
        assert "issuers" in lite
        assert "intelligence_kpis" in lite
        assert "documents" not in lite

        # Section detail endpoints.
        for section in ("inbox", "digest", "review", "ops"):
            detail = json.loads(
                urlopen(f"{handle.url}/api/detail?section={section}").read().decode("utf-8")
            )
            assert isinstance(detail, dict)
        ops = json.loads(urlopen(f"{handle.url}/api/detail?section=ops").read().decode("utf-8"))
        assert "observability" in ops
        assert "golden_rows" in ops
        assert ops["golden_rows"]

        # Invalid section returns 400.
        with pytest.raises(HTTPError) as excinfo:
            urlopen(f"{handle.url}/api/detail?section=nope")
        assert excinfo.value.code == 400

        # Company detail endpoint.
        company = json.loads(
            urlopen(f"{handle.url}/api/company?ticker=TAMU").read().decode("utf-8")
        )
        assert company["ticker"] == "TAMU"
        assert "documents" in company
        assert "metrics" in company

        preview = json.loads(
            urlopen(f"{handle.url}/api/rerun-preview?ticker=TAMU").read().decode("utf-8")
        )
        assert preview["scope"] == "ticker"
        assert preview["ticker"] == "TAMU"
        assert preview["document_count"] >= 1
        assert "rederive_company" in preview["actions"]
    finally:
        handle.server.shutdown()
        handle.server.server_close()
        thread.join(timeout=5)


def test_company_detail_uses_ticker_scoped_queries_not_global_caps(tmp_config) -> None:
    _seed_phase8_data(tmp_config)
    old_ts = "2024-01-01T00:00:00+00:00"
    new_ts = "2026-01-01T00:00:00+00:00"

    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)

        def _seed_many_other_documents(conn):
            conn.execute(
                """
                INSERT INTO companies (
                    ticker, issuer_name, listing_date, active_status, sector, subsector, industry,
                    listing_board, source_urls, raw_metadata, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "ZZZZ",
                    "PT Overflow Testing Tbk",
                    "2020-01-01",
                    "listed",
                    "Testing",
                    "Overflow",
                    "Synthetic",
                    "main",
                    json.dumps(["https://www.idx.co.id"]),
                    json.dumps({}),
                    new_ts,
                    new_ts,
                ),
            )
            conn.execute(
                """
                UPDATE documents
                SET updated_at = ?, retrieved_at = ?, created_at = ?
                WHERE ticker = 'TAMU'
                """,
                (old_ts, old_ts, old_ts),
            )
            for idx in range(220):
                conn.execute(
                    """
                    INSERT INTO documents (
                        document_id, source_name, source_record_id, source_file_id, retrieved_at, original_url,
                        local_path, sha256, mime_type, document_type, language_variant, process_state,
                        created_at, updated_at, ticker, version_group_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            f"doc-zzzz-{idx}",
                            "idx_disclosures",
                            f"zzzz-{idx}",
                            f"zzzz-file-{idx}",
                            new_ts,
                            f"https://example.com/zzzz/{idx}",
                            str(tmp_config.paths.data_dir / f"zzzz-{idx}.pdf"),
                            f"sha-zzzz-{idx}",
                            "application/pdf",
                        "material_information",
                        "id",
                        "analyzed",
                        new_ts,
                        new_ts,
                        "ZZZZ",
                        None,
                    ),
                )

        wq.submit(_seed_many_other_documents)

    handle = create_dashboard_server(tmp_config, host="127.0.0.1", port=0)
    thread = threading.Thread(target=handle.server.serve_forever, daemon=True)
    thread.start()
    try:
        company = json.loads(urlopen(f"{handle.url}/api/company?ticker=TAMU").read().decode("utf-8"))
        assert company["ticker"] == "TAMU"
        assert company["documents"]
        assert all(item["ticker"] == "TAMU" for item in company["documents"])
    finally:
        handle.server.shutdown()
        handle.server.server_close()
        thread.join(timeout=5)


def test_dashboard_server_updates_company_fiscal_year_override(tmp_config) -> None:
    from .test_intelligence_pipeline import _seed_phase10_inputs, _write_phase12_theses
    from stock_triage.intelligence import run_intelligence

    _seed_phase10_inputs(tmp_config)
    _write_phase12_theses(tmp_config)
    run_intelligence(tmp_config, ticker="TAMU")

    handle = create_dashboard_server(tmp_config, host="127.0.0.1", port=0)
    thread = threading.Thread(target=handle.server.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(
            f"{handle.url}/api/company-override",
            data=urlencode({"ticker": "TAMU", "month": "6"}).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
            method="POST",
        )
        payload = json.loads(urlopen(request).read().decode("utf-8"))
        assert payload == {"ticker": "TAMU", "month": 6, "source": "override"}

        company = json.loads(urlopen(f"{handle.url}/api/company?ticker=TAMU").read().decode("utf-8"))
        snapshot_payload = company["company_snapshot"]["payload"]
        assert snapshot_payload["fiscal_year_end_month"] == 6
        assert snapshot_payload["fiscal_year_end_source"] == "override"

        override_get = json.loads(urlopen(f"{handle.url}/api/company-override?ticker=TAMU").read().decode("utf-8"))
        assert override_get["ticker"] == "TAMU"
        assert override_get["month"] == 6
        assert override_get["overrides"]["TAMU"] == 6

        clear_request = Request(
            f"{handle.url}/api/company-override",
            data=urlencode({"ticker": "TAMU", "clear": "1"}).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
            method="POST",
        )
        clear_payload = json.loads(urlopen(clear_request).read().decode("utf-8"))
        assert clear_payload == {"ticker": "TAMU", "month": None, "source": "default"}
    finally:
        handle.server.shutdown()
        handle.server.server_close()
        thread.join(timeout=5)


def test_market_monitor_falls_back_to_closest_recommendation_setups(tmp_config) -> None:
    from stock_triage.intelligence.pipeline import DERIVED_VERSION

    _seed_phase8_data(tmp_config)
    current_ts = "2026-04-17T08:00:00+00:00"

    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)

        def _seed_snapshots(conn):
            companies = [
                ("PICK1", "PT Pick One Tbk"),
                ("PICK2", "PT Pick Two Tbk"),
                ("PICK3", "PT Pick Three Tbk"),
                ("PICK4", "PT Pick Four Tbk"),
            ]
            for ticker, issuer_name in companies:
                conn.execute(
                    """
                    INSERT INTO companies (
                        ticker, issuer_name, listing_date, active_status, sector, subsector, industry,
                        listing_board, source_urls, raw_metadata, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(ticker) DO UPDATE SET issuer_name = excluded.issuer_name
                    """,
                    (
                        ticker,
                        issuer_name,
                        "2020-01-01",
                        "listed",
                        "Banking",
                        "Banks",
                        "Financials",
                        "main",
                        json.dumps(["https://example.com"]),
                        json.dumps({}),
                        current_ts,
                        current_ts,
                    ),
                )
            rows = [
                (
                    "PICK1",
                    {
                        "issuer_name": "PT Pick One Tbk",
                        "top_card": {
                            "decision_state": "Watch",
                            "overall_state": "stable",
                            "thesis_state": "holding",
                            "positive_factors": ["Revenue improved"],
                            "negative_factors": [],
                        },
                        "decision_panel": {
                            "confidence": "medium",
                            "recommendation": {
                                "bucket": "neither",
                                "reasons": ["Revenue improved.", "The baseline is comparable.", "Review work is clear."],
                                "decision_constraints": ["Current stance is Watch."],
                                "evidence_ref_count": 3,
                                "ranking_inputs": {
                                    "stance": "Watch",
                                    "confidence": "medium",
                                    "overall_state": "stable",
                                    "thesis_state": "holding",
                                    "credibility_band": "acceptable",
                                    "data_completeness_pct": 80.0,
                                    "open_review_count": 0,
                                    "critical_review_count": 0,
                                    "critical_signal_count": 0,
                                    "warning_signal_count": 0,
                                    "positive_factor_count": 2,
                                    "negative_factor_count": 0,
                                    "quality_flag_count": 0,
                                    "peer_anomaly_count": 0,
                                    "pick_score": 88,
                                    "avoid_score": 12,
                                },
                            },
                        },
                        "peer_comparison": {"anomalies": []},
                    },
                ),
                (
                    "PICK2",
                    {
                        "issuer_name": "PT Pick Two Tbk",
                        "top_card": {
                            "decision_state": "Watch",
                            "overall_state": "review_required",
                            "thesis_state": "none",
                            "positive_factors": ["Margins held"],
                            "negative_factors": ["Debt signal active"],
                        },
                        "decision_panel": {
                            "confidence": "low",
                            "recommendation": {
                                "bucket": "neither",
                                "reasons": ["Debt signals remain active.", "One review item remains open.", "The stance is still Watch."],
                                "decision_constraints": ["Resolve the open review item."],
                                "evidence_ref_count": 3,
                                "ranking_inputs": {
                                    "stance": "Watch",
                                    "confidence": "low",
                                    "overall_state": "review_required",
                                    "thesis_state": "none",
                                    "credibility_band": "acceptable",
                                    "data_completeness_pct": 74.0,
                                    "open_review_count": 1,
                                    "critical_review_count": 0,
                                    "critical_signal_count": 0,
                                    "warning_signal_count": 1,
                                    "positive_factor_count": 1,
                                    "negative_factor_count": 1,
                                    "quality_flag_count": 0,
                                    "peer_anomaly_count": 0,
                                    "pick_score": 64,
                                    "avoid_score": 31,
                                },
                            },
                        },
                        "peer_comparison": {"anomalies": []},
                    },
                ),
                (
                    "PICK3",
                    {
                        "issuer_name": "PT Pick Three Tbk",
                        "top_card": {
                            "decision_state": "Review manually",
                            "overall_state": "critical",
                            "thesis_state": "none",
                            "positive_factors": [],
                            "negative_factors": ["Legal risk active"],
                        },
                        "decision_panel": {
                            "confidence": "high",
                            "recommendation": {
                                "bucket": "neither",
                                "reasons": ["Legal risk is active.", "The thesis is not broken yet.", "The stance is still Review manually."],
                                "decision_constraints": ["Wait for the legal issue to resolve."],
                                "evidence_ref_count": 3,
                                "ranking_inputs": {
                                    "stance": "Review manually",
                                    "confidence": "high",
                                    "overall_state": "critical",
                                    "thesis_state": "none",
                                    "credibility_band": "acceptable",
                                    "data_completeness_pct": 83.0,
                                    "open_review_count": 1,
                                    "critical_review_count": 0,
                                    "critical_signal_count": 1,
                                    "warning_signal_count": 2,
                                    "positive_factor_count": 0,
                                    "negative_factor_count": 1,
                                    "quality_flag_count": 1,
                                    "peer_anomaly_count": 0,
                                    "pick_score": 20,
                                    "avoid_score": 95,
                                },
                            },
                        },
                        "peer_comparison": {"anomalies": []},
                    },
                ),
                (
                    "PICK4",
                    {
                        "issuer_name": "PT Pick Four Tbk",
                        "top_card": {
                            "decision_state": "Watch",
                            "overall_state": "watch",
                            "thesis_state": "none",
                            "positive_factors": ["Cash remained positive"],
                            "negative_factors": [],
                        },
                        "decision_panel": {
                            "confidence": "low",
                            "recommendation": {
                                "bucket": "neither",
                                "reasons": ["Cash remained positive.", "The stance is still Watch.", "Coverage is thin."],
                                "decision_constraints": ["Wait for one more comparable filing."],
                                "evidence_ref_count": 3,
                                "ranking_inputs": {
                                    "stance": "Watch",
                                    "confidence": "low",
                                    "overall_state": "watch",
                                    "thesis_state": "none",
                                    "credibility_band": "acceptable",
                                    "data_completeness_pct": 68.0,
                                    "open_review_count": 0,
                                    "critical_review_count": 0,
                                    "critical_signal_count": 0,
                                    "warning_signal_count": 0,
                                    "positive_factor_count": 1,
                                    "negative_factor_count": 0,
                                    "quality_flag_count": 0,
                                    "peer_anomaly_count": 0,
                                    "pick_score": 55,
                                    "avoid_score": 8,
                                },
                            },
                        },
                        "peer_comparison": {"anomalies": []},
                    },
                ),
            ]
            for ticker, payload in rows:
                conn.execute(
                    """
                    INSERT INTO documents (
                        document_id, source_name, source_record_id, source_file_id, retrieved_at, original_url,
                        local_path, sha256, mime_type, document_type, language_variant, process_state,
                        created_at, updated_at, ticker, version_group_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"doc-{ticker.lower()}",
                        "idx_financial_reports",
                        f"{ticker.lower()}-record",
                        f"{ticker.lower()}-file",
                        current_ts,
                        f"https://example.com/{ticker.lower()}",
                        str(tmp_config.paths.data_dir / f"{ticker.lower()}.pdf"),
                        f"sha-{ticker.lower()}",
                        "application/pdf",
                        "financial_statement",
                        "id",
                        "analyzed",
                        current_ts,
                        current_ts,
                        ticker,
                        None,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO company_snapshots (
                        snapshot_id, ticker, snapshot_kind, as_of_document_id, as_of_date,
                        latest_analysis_run_id, payload_json, derived_version, created_at, updated_at
                    ) VALUES (?, ?, 'current', ?, ?, NULL, ?, ?, ?, ?)
                    ON CONFLICT(ticker, snapshot_kind, derived_version)
                    DO UPDATE SET
                        payload_json = excluded.payload_json,
                        as_of_document_id = excluded.as_of_document_id,
                        as_of_date = excluded.as_of_date,
                        updated_at = excluded.updated_at
                    """,
                    (
                        f"snapshot-{ticker}",
                        ticker,
                        f"doc-{ticker.lower()}",
                        current_ts,
                        json.dumps(payload),
                        DERIVED_VERSION,
                        current_ts,
                        current_ts,
                    ),
                )

        wq.submit(_seed_snapshots)

    handle = create_dashboard_server(tmp_config, host="127.0.0.1", port=0)
    thread = threading.Thread(target=handle.server.serve_forever, daemon=True)
    thread.start()
    try:
        snapshot = json.loads(urlopen(f"{handle.url}/api/snapshot").read().decode("utf-8"))
        market = snapshot["market_monitor"]
        assert market["counts"]["stock_pick_candidates"] == 0
        assert market["counts"]["avoid_candidates"] == 0
        assert len(market["stock_pick_candidates"]) >= 3
        assert len(market["avoid_candidates"]) >= 3
        assert all(row["qualification"] == "closest" for row in market["stock_pick_candidates"][:3])
        assert all(row["qualification"] == "closest" for row in market["avoid_candidates"][:3])
        assert market["stock_pick_candidates"][0]["blockers"]
        assert market["avoid_candidates"][0]["blockers"]
    finally:
        handle.server.shutdown()
        handle.server.server_close()
        thread.join(timeout=5)


def test_ops_detail_exposes_zero_case_golden_reason(tmp_config) -> None:
    from stock_triage.intelligence.review import run_golden_set

    _seed_phase8_data(tmp_config)
    run_golden_set(tmp_config, ticker="TAMU", limit=1)
    handle = create_dashboard_server(tmp_config, host="127.0.0.1", port=0)
    thread = threading.Thread(target=handle.server.serve_forever, daemon=True)
    thread.start()
    try:
        ops = json.loads(urlopen(f"{handle.url}/api/detail?section=ops").read().decode("utf-8"))
        zero_case = next((row for row in ops["golden_rows"] if row["target_ticker"] == "TAMU"), None)
        assert zero_case is not None
        assert zero_case["case_count"] == 0
        assert "no analyzed" in (zero_case["no_case_reason"] or "").lower() or "no current" in (zero_case["no_case_reason"] or "").lower()
        assert zero_case["summary"]
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
