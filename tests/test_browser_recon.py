from __future__ import annotations

import json

from stock_triage.recon import (
    BrowserCapture,
    BrowserFinding,
    BrowserResponseArtifact,
    is_cloudflare_blocked,
    write_browser_findings,
)


def test_cloudflare_block_detection():
    assert is_cloudflare_blocked("Attention Required! | Cloudflare", "Please enable cookies.")
    assert is_cloudflare_blocked("Just a moment...", "Performance & security by Cloudflare")
    assert not is_cloudflare_blocked("IDX Company Profiles", "Normal page body")


def test_write_browser_findings(tmp_path):
    html_path = tmp_path / "page.html"
    state_path = tmp_path / "storage_state.json"
    html_path.write_text("<html></html>", encoding="utf-8")
    state_path.write_text("{}", encoding="utf-8")
    capture = BrowserCapture(
        source_id="idx_company_profiles",
        source_class="company_universe",
        url="https://example.invalid/source",
        final_url="https://example.invalid/final",
        title="IDX Company Profiles",
        accessible=True,
        blocked=False,
        waited_for_manual_clearance=False,
        cookies=["cf_clearance"],
        html_path=html_path,
        storage_state_path=state_path,
        responses=[
            BrowserResponseArtifact(
                url="https://example.invalid/api/list",
                status=200,
                content_type="application/json",
                resource_type="xhr",
                body_path=tmp_path / "resp.json",
            )
        ],
    )
    finding = BrowserFinding(
        source_id="idx_company_profiles",
        source_class="company_universe",
        url="https://example.invalid/source",
        accessible=True,
        blocked=False,
        capture=capture,
    )

    out_path = tmp_path / "browser_findings.json"
    write_browser_findings(out_path, [finding])

    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert len(payload["findings"]) == 1
    finding = payload["findings"][0]
    assert finding["source_id"] == "idx_company_profiles"
    assert finding["accessible"] is True
    assert finding["capture"]["responses"][0]["resource_type"] == "xhr"
