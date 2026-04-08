"""Polite client tests using an in-process HTTP server (no real network)."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from stock_triage.recon import PoliteClient, RobotsBlocked


class _Handler(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, bytes, dict[str, str]]] = {}

    def log_message(self, *args, **kwargs):  # silence
        pass

    def do_GET(self):  # noqa: N802
        path = self.path
        if path not in self.routes:
            self.send_response(404)
            self.end_headers()
            return
        status, body, headers = self.routes[path]
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def server():
    _Handler.routes = {
        "/robots.txt": (200, b"User-agent: *\nDisallow: /forbidden\n", {"Content-Type": "text/plain"}),
        "/ok": (200, b"<html><title>hello</title></html>", {"Content-Type": "text/html"}),
        "/forbidden": (200, b"should not be served", {"Content-Type": "text/plain"}),
    }
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()


def test_polite_client_fetches_and_caches(tmp_path: Path, server: str):
    client = PoliteClient(
        cache_root=tmp_path / "cache",
        user_agent="test/1.0",
        rate_limit_per_second=100.0,  # avoid slowing the test
    )
    try:
        r1 = client.get(f"{server}/ok")
        assert r1.status_code == 200
        assert r1.cache_hit is False
        # Second fetch should hit cache
        r2 = client.get(f"{server}/ok")
        assert r2.cache_hit is True
        assert r2.body_path == r1.body_path
    finally:
        client.close()


def test_polite_client_honors_robots(tmp_path: Path, server: str):
    client = PoliteClient(
        cache_root=tmp_path / "cache",
        user_agent="test/1.0",
        rate_limit_per_second=100.0,
    )
    try:
        with pytest.raises(RobotsBlocked):
            client.get(f"{server}/forbidden")
    finally:
        client.close()
