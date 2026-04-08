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
        },
        "discovery": {
            "user_agent": "stock-triage-test/0.0",
            "request_timeout_s": 10,
            "rate_limit_per_second": 1.0,
        },
        "logging": {"level": "WARNING", "json": False},
    }
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(raw))
    cfg = load_config(cfg_path)
    cfg.ensure_dirs()
    return cfg
