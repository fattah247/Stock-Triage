"""Browser-backed Phase 1 proof-of-access helpers.

IDX is protected by Cloudflare, so a plain HTTP client is insufficient for the
required source classes. This module provides an operator-assisted Playwright
session that can:

- launch a persistent Chromium profile for manual challenge clearance
- capture page HTML plus XHR/fetch responses after access is granted
- persist storage state and response artifacts for later offline analysis
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class BrowserDependencyMissing(RuntimeError):
    """Raised when the optional browser dependency is not installed."""


class BrowserBlocked(RuntimeError):
    """Raised when the browser session is still blocked after the wait window."""


_BLOCK_TITLE_RE = re.compile(r"(attention required|just a moment)", re.IGNORECASE)
_BLOCK_BODY_RE = re.compile(
    r"(please enable cookies|sorry, you have been blocked|cf-ray|cloudflare)",
    re.IGNORECASE,
)


def is_cloudflare_blocked(title: str | None, body_text: str | None) -> bool:
    title = title or ""
    body_text = body_text or ""
    return bool(_BLOCK_TITLE_RE.search(title) or _BLOCK_BODY_RE.search(body_text))


@dataclass(frozen=True)
class BrowserResponseArtifact:
    url: str
    status: int
    content_type: str
    resource_type: str
    body_path: Path | None = None
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        if self.body_path is not None:
            out["body_path"] = str(self.body_path)
        return out


@dataclass(frozen=True)
class BrowserCapture:
    source_id: str
    source_class: str
    url: str
    final_url: str
    title: str
    accessible: bool
    blocked: bool
    waited_for_manual_clearance: bool
    cookies: list[str]
    html_path: Path
    storage_state_path: Path
    responses: list[BrowserResponseArtifact]
    captured_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_class": self.source_class,
            "url": self.url,
            "final_url": self.final_url,
            "title": self.title,
            "accessible": self.accessible,
            "blocked": self.blocked,
            "waited_for_manual_clearance": self.waited_for_manual_clearance,
            "cookies": self.cookies,
            "html_path": str(self.html_path),
            "storage_state_path": str(self.storage_state_path),
            "responses": [r.to_dict() for r in self.responses],
            "captured_at": self.captured_at,
        }


@dataclass(frozen=True)
class BrowserJSONFetchResult:
    source_id: str
    page_url: str
    api_url: str
    status: int
    content_type: str
    body_path: Path
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "page_url": self.page_url,
            "api_url": self.api_url,
            "status": self.status,
            "content_type": self.content_type,
            "body_path": str(self.body_path),
            "fetched_at": self.fetched_at,
        }


@dataclass(frozen=True)
class BrowserFinding:
    source_id: str
    source_class: str
    url: str
    accessible: bool
    blocked: bool
    error: str | None = None
    capture: BrowserCapture | None = None
    observed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        out = {
            "source_id": self.source_id,
            "source_class": self.source_class,
            "url": self.url,
            "accessible": self.accessible,
            "blocked": self.blocked,
            "error": self.error,
            "observed_at": self.observed_at,
        }
        if self.capture is not None:
            out["capture"] = self.capture.to_dict()
        return out


class PlaywrightSession:
    """Operator-assisted browser session for Phase 1 proof-of-access.

    In headless mode we only check whether access works automatically. In
    headed mode the operator can solve Cloudflare's challenge in the persistent
    browser window, after which the session captures network responses and
    storage state for later runs.
    """

    def __init__(
        self,
        *,
        profile_dir: Path,
        artifacts_dir: Path,
        headless: bool = False,
        challenge_timeout_s: int = 180,
        page_settle_ms: int = 5000,
        channel: str = "",
    ) -> None:
        self.profile_dir = profile_dir
        self.artifacts_dir = artifacts_dir
        self.headless = headless
        self.challenge_timeout_s = challenge_timeout_s
        self.page_settle_ms = page_settle_ms
        self.channel = channel or None

    def capture(
        self,
        *,
        source_id: str,
        source_class: str,
        url: str,
        wait_selector: str | None = None,
    ) -> BrowserCapture:
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ModuleNotFoundError as exc:  # pragma: no cover - exercised by environment
            raise BrowserDependencyMissing(
                "Playwright is not installed. Install with `pip install -e \".[browser]\"`."
            ) from exc

        source_root = self.artifacts_dir / source_id
        source_root.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=self.headless,
                channel=self.channel,
                locale="en-US",
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.add_init_script(
                    """
                    Object.defineProperty(navigator, 'webdriver', {
                      get: () => undefined,
                    });
                    """
                )

                responses: list[BrowserResponseArtifact] = []

                def on_response(resp: Any) -> None:
                    resource_type = resp.request.resource_type
                    content_type = resp.headers.get("content-type", "")
                    should_persist = resource_type in {"xhr", "fetch"} or "json" in content_type.lower()
                    body_path: Path | None = None
                    if should_persist:
                        try:
                            body = resp.body()
                        except Exception:  # noqa: BLE001 - browser may close mid-callback
                            body = None
                        if body is not None:
                            body_path = self._write_response_body(
                                source_root=source_root,
                                url=resp.url,
                                body=body,
                                content_type=content_type,
                            )
                    responses.append(
                        BrowserResponseArtifact(
                            url=resp.url,
                            status=resp.status,
                            content_type=content_type,
                            resource_type=resource_type,
                            body_path=body_path,
                        )
                    )

                page.on("response", on_response)
                page.goto(url, wait_until="domcontentloaded", timeout=self.challenge_timeout_s * 1000)

                waited_for_manual_clearance = False
                if self._is_blocked_page(page):
                    if self.headless:
                        raise BrowserBlocked(
                            "IDX still blocks the headless browser session. "
                            "Rerun headed and clear the challenge in the browser window."
                        )
                    waited_for_manual_clearance = True
                    self._wait_until_unblocked(page, wait_selector=wait_selector)
                elif wait_selector is not None:
                    page.wait_for_selector(wait_selector, timeout=self.challenge_timeout_s * 1000)

                page.wait_for_timeout(self.page_settle_ms)
                title = page.title()
                body_text = page.text_content("body") or ""
                blocked = is_cloudflare_blocked(title, body_text)
                html_path = self._write_text(
                    source_root / "page.html",
                    page.content(),
                )
                storage_state_path = source_root / "storage_state.json"
                context.storage_state(path=str(storage_state_path))

                return BrowserCapture(
                    source_id=source_id,
                    source_class=source_class,
                    url=url,
                    final_url=page.url,
                    title=title,
                    accessible=not blocked,
                    blocked=blocked,
                    waited_for_manual_clearance=waited_for_manual_clearance,
                    cookies=sorted(cookie["name"] for cookie in context.cookies()),
                    html_path=html_path,
                    storage_state_path=storage_state_path,
                    responses=responses,
                )
            except PlaywrightTimeoutError as exc:
                raise BrowserBlocked(
                    "Timed out waiting for IDX access to clear in the browser session."
                ) from exc
            finally:
                context.close()

    def fetch_json(
        self,
        *,
        source_id: str,
        page_url: str,
        api_url: str,
        wait_selector: str | None = None,
    ) -> BrowserJSONFetchResult:
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ModuleNotFoundError as exc:  # pragma: no cover - exercised by environment
            raise BrowserDependencyMissing(
                "Playwright is not installed. Install with `pip install -e \".[browser]\"`."
            ) from exc

        source_root = self.artifacts_dir / source_id
        source_root.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=self.headless,
                channel=self.channel,
                locale="en-US",
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(page_url, wait_until="domcontentloaded", timeout=self.challenge_timeout_s * 1000)

                if self._is_blocked_page(page):
                    if self.headless:
                        raise BrowserBlocked(
                            "IDX still blocks the headless browser session. "
                            "Rerun headed and clear the challenge in the browser window."
                        )
                    self._wait_until_unblocked(page, wait_selector=wait_selector)
                elif wait_selector is not None:
                    page.wait_for_selector(wait_selector, timeout=self.challenge_timeout_s * 1000)

                page.wait_for_timeout(self.page_settle_ms)
                payload = page.evaluate(
                    """async (targetUrl) => {
                        const res = await fetch(targetUrl, { credentials: 'include' });
                        return {
                          status: res.status,
                          contentType: res.headers.get('content-type') || '',
                          text: await res.text(),
                        };
                    }""",
                    api_url,
                )
                body_path = self._write_response_body(
                    source_root=source_root,
                    url=api_url,
                    body=payload["text"].encode("utf-8"),
                    content_type=payload["contentType"] or "application/json",
                )
                return BrowserJSONFetchResult(
                    source_id=source_id,
                    page_url=page_url,
                    api_url=api_url,
                    status=int(payload["status"]),
                    content_type=payload["contentType"],
                    body_path=body_path,
                )
            except PlaywrightTimeoutError as exc:
                raise BrowserBlocked(
                    "Timed out waiting for IDX access to clear in the browser session."
                ) from exc
            finally:
                context.close()

    def fetch_json_many(
        self,
        *,
        source_id: str,
        page_url: str,
        api_urls: list[str],
        wait_selector: str | None = None,
    ) -> list[BrowserJSONFetchResult]:
        if not api_urls:
            return []

        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ModuleNotFoundError as exc:  # pragma: no cover - exercised by environment
            raise BrowserDependencyMissing(
                "Playwright is not installed. Install with `pip install -e \".[browser]\"`."
            ) from exc

        source_root = self.artifacts_dir / source_id
        source_root.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=self.headless,
                channel=self.channel,
                locale="en-US",
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(page_url, wait_until="domcontentloaded", timeout=self.challenge_timeout_s * 1000)

                if self._is_blocked_page(page):
                    if self.headless:
                        raise BrowserBlocked(
                            "IDX still blocks the headless browser session. "
                            "Rerun headed and clear the challenge in the browser window."
                        )
                    self._wait_until_unblocked(page, wait_selector=wait_selector)
                elif wait_selector is not None:
                    page.wait_for_selector(wait_selector, timeout=self.challenge_timeout_s * 1000)

                page.wait_for_timeout(self.page_settle_ms)
                results: list[BrowserJSONFetchResult] = []
                for api_url in api_urls:
                    payload = page.evaluate(
                        """async (targetUrl) => {
                            const res = await fetch(targetUrl, { credentials: 'include' });
                            return {
                              status: res.status,
                              contentType: res.headers.get('content-type') || '',
                              text: await res.text(),
                            };
                        }""",
                        api_url,
                    )
                    body_path = self._write_response_body(
                        source_root=source_root,
                        url=api_url,
                        body=payload["text"].encode("utf-8"),
                        content_type=payload["contentType"] or "application/json",
                    )
                    results.append(
                        BrowserJSONFetchResult(
                            source_id=source_id,
                            page_url=page_url,
                            api_url=api_url,
                            status=int(payload["status"]),
                            content_type=payload["contentType"],
                            body_path=body_path,
                        )
                    )
                return results
            except PlaywrightTimeoutError as exc:
                raise BrowserBlocked(
                    "Timed out waiting for IDX access to clear in the browser session."
                ) from exc
            finally:
                context.close()

    def _wait_until_unblocked(self, page: Any, *, wait_selector: str | None) -> None:
        deadline = datetime.now(timezone.utc).timestamp() + self.challenge_timeout_s
        while datetime.now(timezone.utc).timestamp() < deadline:
            if not self._is_blocked_page(page):
                if wait_selector is not None:
                    page.wait_for_selector(wait_selector, timeout=10_000)
                return
            page.wait_for_timeout(1000)
        raise BrowserBlocked(
            "Browser session is still blocked. Solve the Cloudflare prompt in the headed browser window, "
            "or reuse a warmed persistent profile."
        )

    @staticmethod
    def _is_blocked_page(page: Any) -> bool:
        title = page.title()
        body_text = page.text_content("body") or ""
        return is_cloudflare_blocked(title, body_text)

    @staticmethod
    def _write_text(path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    @staticmethod
    def _write_response_body(*, source_root: Path, url: str, body: bytes, content_type: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        suffix = ".json" if "json" in content_type.lower() else ".bin"
        path = source_root / "responses" / f"{digest}{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        meta_path = path.with_suffix(path.suffix + ".meta.json")
        meta_path.write_text(
            json.dumps(
                {
                    "url": url,
                    "content_type": content_type,
                    "saved_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return path


def write_browser_findings(out_path: Path, findings: list[BrowserFinding]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "findings": [finding.to_dict() for finding in findings],
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def idx_source_surfaces() -> list[dict[str, str]]:
    """IDX source classes that must be proven in Phase 1."""
    return [
        {
            "id": "idx_company_profiles",
            "source_class": "company_universe",
            "url": "https://www.idx.co.id/en/listed-companies/company-profiles",
        },
        {
            "id": "idx_financial_statements_annual_report",
            "source_class": "financial_report",
            "url": "https://www.idx.co.id/en/listed-companies/financial-statements-and-annual-report",
        },
        {
            "id": "idx_disclosure",
            "source_class": "disclosure",
            "url": "https://www.idx.co.id/en/listed-companies/disclosure",
        },
    ]
