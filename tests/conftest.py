from __future__ import annotations

import builtins
import io
import os
import sqlite3
from pathlib import Path

import pytest
import yaml

from stock_triage.config import load_config
from stock_triage.config.loader import REPO_ROOT

_REPO_DATA = (REPO_ROOT / "data").resolve()


def _is_repo_data(path: object) -> bool:
    try:
        resolved = Path(os.fsdecode(path)).resolve()
    except (TypeError, ValueError, OSError):
        return False
    return resolved == _REPO_DATA or _REPO_DATA in resolved.parents


@pytest.fixture(autouse=True, scope="session")
def _forbid_writes_to_operator_data():
    """Fail the offending test the moment it would write the operator's ``data/``.

    Every database open goes through ``sqlite3.connect`` and every file write
    through ``open``; guarding those attributes the write to the test that made
    it, and the operator's own pipeline writing ``data/`` meanwhile is ignored.
    """
    real_open, real_connect = builtins.open, sqlite3.connect

    def guarded_open(file, mode="r", *args, **kwargs):
        if isinstance(file, (str, bytes, os.PathLike)) and set(mode) & set("wax+") and _is_repo_data(file):
            raise AssertionError(f"test tried to write operator data: {os.fsdecode(file)}")
        return real_open(file, mode, *args, **kwargs)

    def guarded_connect(database, *args, **kwargs):
        target = os.fsdecode(database)
        read_only = kwargs.get("uri") and "mode=ro" in target
        path = target.removeprefix("file:").split("?", 1)[0] if kwargs.get("uri") else target
        if not read_only and _is_repo_data(path):
            raise AssertionError(f"test tried to open operator database for writing: {path}")
        return real_connect(database, *args, **kwargs)

    builtins.open = io.open = guarded_open
    sqlite3.connect = guarded_connect
    try:
        yield
    finally:
        builtins.open = io.open = real_open
        sqlite3.connect = real_connect


def _isolated_raw_config(tmp_path: Path) -> dict:
    data_dir = tmp_path / "data"
    return {
        "paths": {
            "data_dir": str(data_dir),
            "files_dir": str(data_dir / "files"),
            "db_path": str(data_dir / "db" / "stock_triage.sqlite"),
            "logs_dir": str(data_dir / "logs"),
            "quarantine_dir": str(data_dir / "quarantine"),
        },
        "discovery": {
            "user_agent": "stock-triage-test/0.0",
            "request_timeout_s": 10,
            "rate_limit_per_second": 1.0,
        },
        "browser": {
            "headless": True,
            "profile_dir": str(data_dir / "browser" / "profile"),
            "artifacts_dir": str(data_dir / "recon" / "browser"),
            "challenge_timeout_s": 5,
            "page_settle_ms": 10,
            "channel": "",
        },
        "logging": {"level": "WARNING", "json": False},
        "download": {
            "retry_backoff_seconds": 1,
            "retry_backoff_max_seconds": 30,
        },
        "ocr": {
            "min_chars_per_page": 50,
            "languages": "eng",
        },
    }


@pytest.fixture(autouse=True)
def _isolated_config_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every implicit ``load_config()`` (and spawned CLI) at tmp_path."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(_isolated_raw_config(tmp_path)))
    monkeypatch.setenv("STOCK_TRIAGE_CONFIG", str(cfg_path))
    for key in list(os.environ):
        if key.startswith("STOCK_TRIAGE__"):
            monkeypatch.delenv(key, raising=False)
    return cfg_path


@pytest.fixture()
def tmp_config(_isolated_config_env: Path):
    cfg = load_config(_isolated_config_env)
    cfg.ensure_dirs()
    return cfg
