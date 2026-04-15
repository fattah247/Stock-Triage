from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from stock_triage.config import load_config


@pytest.fixture()
def tmp_config(tmp_path: Path):
    raw = {
        "paths": {
            "data_dir": str(tmp_path / "data"),
            "files_dir": str(tmp_path / "data" / "files"),
            "db_path": str(tmp_path / "data" / "db" / "stock_triage.sqlite"),
            "logs_dir": str(tmp_path / "data" / "logs"),
            "quarantine_dir": str(tmp_path / "data" / "quarantine"),
        },
        "discovery": {
            "user_agent": "stock-triage-test/0.0",
            "request_timeout_s": 10,
            "rate_limit_per_second": 1.0,
        },
        "browser": {
            "headless": True,
            "profile_dir": str(tmp_path / "data" / "browser" / "profile"),
            "artifacts_dir": str(tmp_path / "data" / "recon" / "browser"),
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
        "theses": {
            "definition_path": str(tmp_path / "data" / "theses.yaml"),
        },
        "company_overrides": {
            "definition_path": str(tmp_path / "data" / "company_overrides.yaml"),
        },
    }
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(raw))
    cfg = load_config(cfg_path)
    cfg.ensure_dirs()
    return cfg
