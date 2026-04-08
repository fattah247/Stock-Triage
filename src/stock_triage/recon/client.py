"""Polite reconnaissance HTTP client (PRD §9, §6.11).

Used by Phase 1 probes to discover the shape of IDX/OJK public surfaces.
Behaviors:

- Rate limits to a configured RPS (default 1 req/s).
- Identifies via the configured User-Agent.
- Honors robots.txt with per-host caching.
- Persists every response to disk under <root>/<host>/<sha>.{bin,meta.json}
  so the analyst can re-inspect responses without re-hitting the server.
- GET only. No cookies are sent unless the caller passes them explicitly.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.robotparser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx


class RobotsBlocked(Exception):
    """Raised when robots.txt disallows the requested URL for our UA."""


@dataclass
class ReconResponse:
    url: str
    status_code: int
    headers: dict[str, str]
    body_path: Path
    meta_path: Path
    elapsed_s: float
    fetched_at: datetime
    cache_hit: bool

    def text(self) -> str:
        return self.body_path.read_text(encoding="utf-8", errors="replace")

    def bytes(self) -> bytes:
        return self.body_path.read_bytes()


class PoliteClient:
    def __init__(
        self,
        *,
        cache_root: Path,
        user_agent: str,
        rate_limit_per_second: float = 1.0,
        timeout_s: float = 30.0,
        respect_cache: bool = True,
    ) -> None:
        self.cache_root = cache_root
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.user_agent = user_agent
        self.min_interval = 1.0 / max(rate_limit_per_second, 0.01)
        self.timeout_s = timeout_s
        self.respect_cache = respect_cache

        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept": "*/*"},
            timeout=timeout_s,
            follow_redirects=True,
        )
        self._lock = threading.Lock()
        self._last_request_at: float = 0.0
        self._robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PoliteClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ---- robots ------------------------------------------------------------

    def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        parsed = urlparse(url)
        host_key = f"{parsed.scheme}://{parsed.netloc}"
        if host_key in self._robots_cache:
            return self._robots_cache[host_key]
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(f"{host_key}/robots.txt")
        try:
            self._throttle()
            r = self._client.get(f"{host_key}/robots.txt")
            if r.status_code == 200:
                rp.parse(r.text.splitlines())
            else:
                rp = None  # treat absence as allow-all (per RFC 9309)
        except Exception:
            rp = None
        self._robots_cache[host_key] = rp
        return rp

    def _allowed(self, url: str) -> bool:
        rp = self._robots_for(url)
        if rp is None:
            return True
        return rp.can_fetch(self.user_agent, url)

    # ---- rate limiting -----------------------------------------------------

    def _throttle(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = (self._last_request_at + self.min_interval) - now
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()

    # ---- cache layout ------------------------------------------------------

    def _cache_paths(self, url: str) -> tuple[Path, Path]:
        host = urlparse(url).netloc.replace(":", "_")
        h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        host_dir = self.cache_root / host
        host_dir.mkdir(parents=True, exist_ok=True)
        return host_dir / f"{h}.bin", host_dir / f"{h}.meta.json"

    # ---- fetch -------------------------------------------------------------

    def get(self, url: str, *, headers: dict[str, str] | None = None) -> ReconResponse:
        body_path, meta_path = self._cache_paths(url)
        if self.respect_cache and body_path.exists() and meta_path.exists():
            meta = json.loads(meta_path.read_text())
            return ReconResponse(
                url=url,
                status_code=meta["status_code"],
                headers=meta["headers"],
                body_path=body_path,
                meta_path=meta_path,
                elapsed_s=meta.get("elapsed_s", 0.0),
                fetched_at=datetime.fromisoformat(meta["fetched_at"]),
                cache_hit=True,
            )
        if not self._allowed(url):
            raise RobotsBlocked(f"robots.txt disallows {url} for UA {self.user_agent!r}")
        self._throttle()
        t0 = time.monotonic()
        r = self._client.get(url, headers=headers or {})
        elapsed = time.monotonic() - t0
        body_path.write_bytes(r.content)
        meta: dict[str, Any] = {
            "url": url,
            "final_url": str(r.url),
            "status_code": r.status_code,
            "headers": dict(r.headers),
            "elapsed_s": elapsed,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "request_headers": {"User-Agent": self.user_agent, **(headers or {})},
        }
        meta_path.write_text(json.dumps(meta, indent=2))
        return ReconResponse(
            url=url,
            status_code=r.status_code,
            headers=dict(r.headers),
            body_path=body_path,
            meta_path=meta_path,
            elapsed_s=elapsed,
            fetched_at=datetime.now(timezone.utc),
            cache_hit=False,
        )
