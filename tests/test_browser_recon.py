from __future__ import annotations

import json
from pathlib import Path

from stock_triage.recon import (
    BrowserCapture,
    BrowserFinding,
    BrowserResponseArtifact,
    PlaywrightSession,
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


def test_playwright_session_falls_back_to_cloned_profile_when_locked(tmp_path):
    profile_dir = tmp_path / "profile"
    artifacts_dir = tmp_path / "artifacts"
    profile_dir.mkdir()
    (profile_dir / "Cookies").write_text("cookie-data", encoding="utf-8")
    (profile_dir / "SingletonLock").write_text("locked", encoding="utf-8")

    session = PlaywrightSession(profile_dir=profile_dir, artifacts_dir=artifacts_dir, headless=True)
    launched_paths: list[Path] = []

    class FakeContext:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    fake_context = FakeContext()

    class FakeChromium:
        def __init__(self) -> None:
            self.calls = 0

        def launch_persistent_context(self, path: str, **_kwargs):
            self.calls += 1
            launched_paths.append(Path(path))
            if self.calls == 1:
                raise RuntimeError("Failed to create a ProcessSingleton for your profile directory. SingletonLock exists.")
            assert Path(path) != profile_dir
            assert (Path(path) / "Cookies").exists()
            assert not (Path(path) / "SingletonLock").exists()
            return fake_context

    class FakePlaywright:
        chromium = FakeChromium()

    with session._open_context(FakePlaywright()) as context:
        assert context is fake_context

    assert fake_context.closed is True
    assert launched_paths[0] == profile_dir
    assert launched_paths[1] != profile_dir
    assert not launched_paths[1].parent.exists()
