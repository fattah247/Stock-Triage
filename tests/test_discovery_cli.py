from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from stock_triage.__main__ import _effective_window, _window_from_args, cmd_discover
from stock_triage.db import WriterQueue, run_migrations
from stock_triage.discovery import persist_watermark
from stock_triage.sources import DiscoveryWindow, SourceClass


class _DummyLogger:
    def info(self, *_args, **_kwargs) -> None:
        return None


class _FakeAdapter:
    name = "idx_fake_disclosure"
    source_class = SourceClass.DISCLOSURE

    def __init__(self) -> None:
        self.windows: list[DiscoveryWindow] = []

    def default_window(self) -> DiscoveryWindow:
        return DiscoveryWindow(cursor="default-window")

    def resume_window(self, watermark: DiscoveryWindow) -> DiscoveryWindow:
        return DiscoveryWindow(
            start=watermark.start,
            end=watermark.end,
            cursor=f"resume:{watermark.cursor}" if watermark.cursor else "resume",
        )

    def discover(self, window: DiscoveryWindow):
        self.windows.append(window)
        return []


def test_window_from_args_returns_none_when_no_inputs():
    args = SimpleNamespace(start=None, end=None, year=None, cursor=None)
    assert _window_from_args(args) is None


def test_effective_window_prefers_requested_then_persisted_then_default():
    adapter = _FakeAdapter()
    requested = DiscoveryWindow(start=date(2025, 1, 1), end=date(2025, 12, 31), cursor="explicit")
    persisted = DiscoveryWindow(start=date(2024, 1, 1), end=date(2024, 12, 31), cursor="saved")

    assert _effective_window(
        adapter=adapter,
        requested_window=requested,
        persisted_window=persisted,
    ) == requested
    assert _effective_window(
        adapter=adapter,
        requested_window=None,
        persisted_window=persisted,
    ) == DiscoveryWindow(start=date(2024, 1, 1), end=date(2024, 12, 31), cursor="resume:saved")
    assert _effective_window(
        adapter=adapter,
        requested_window=None,
        persisted_window=None,
    ) == DiscoveryWindow(cursor="default-window")


def test_cmd_discover_uses_persisted_watermark_by_default(tmp_config, monkeypatch):
    persisted = DiscoveryWindow(start=date(2026, 1, 1), end=date(2026, 4, 9), cursor="page=4")
    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)
        wq.submit(
            lambda conn: persist_watermark(
                conn,
                source_name="idx_fake_disclosure",
                source_class=SourceClass.DISCLOSURE,
                window=persisted,
            )
        )

    adapter = _FakeAdapter()
    monkeypatch.setattr("stock_triage.__main__.load_config", lambda: tmp_config)
    monkeypatch.setattr("stock_triage.__main__.setup_logging", lambda _cfg: None)
    monkeypatch.setattr("stock_triage.__main__.get_logger", lambda _name: _DummyLogger())
    monkeypatch.setattr("stock_triage.__main__._adapters_for_source", lambda _source: [adapter])

    args = SimpleNamespace(source="disclosure", start=None, end=None, year=None, cursor=None)
    assert cmd_discover(args) == 0
    assert adapter.windows == [
        DiscoveryWindow(start=date(2026, 1, 1), end=date(2026, 4, 9), cursor="resume:page=4")
    ]


def test_cmd_discover_uses_explicit_window_over_watermark(tmp_config, monkeypatch):
    persisted = DiscoveryWindow(start=date(2026, 1, 1), end=date(2026, 4, 9), cursor="page=4")
    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)
        wq.submit(
            lambda conn: persist_watermark(
                conn,
                source_name="idx_fake_disclosure",
                source_class=SourceClass.DISCLOSURE,
                window=persisted,
            )
        )

    adapter = _FakeAdapter()
    monkeypatch.setattr("stock_triage.__main__.load_config", lambda: tmp_config)
    monkeypatch.setattr("stock_triage.__main__.setup_logging", lambda _cfg: None)
    monkeypatch.setattr("stock_triage.__main__.get_logger", lambda _name: _DummyLogger())
    monkeypatch.setattr("stock_triage.__main__._adapters_for_source", lambda _source: [adapter])

    args = SimpleNamespace(source="disclosure", start=None, end=None, year=2025, cursor="manual")
    assert cmd_discover(args) == 0
    assert adapter.windows == [
        DiscoveryWindow(start=date(2025, 1, 1), end=date(2025, 12, 31), cursor="manual")
    ]
