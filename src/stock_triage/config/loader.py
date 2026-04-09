"""Config loader: YAML file + STOCK_TRIAGE__SECTION__KEY env overrides."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "default.yaml"
ENV_PREFIX = "STOCK_TRIAGE__"


class PathsConfig(BaseModel):
    data_dir: Path
    files_dir: Path
    db_path: Path
    logs_dir: Path
    quarantine_dir: Path | None = None

    @field_validator("data_dir", "files_dir", "db_path", "logs_dir", "quarantine_dir", mode="before")
    @classmethod
    def _to_path(cls, v: Any) -> Path | None:
        if v is None:
            return None
        p = Path(v)
        return p if p.is_absolute() else (REPO_ROOT / p)

    @model_validator(mode="after")
    def _default_quarantine_dir(self) -> "PathsConfig":
        if self.quarantine_dir is None:
            self.quarantine_dir = self.data_dir / "quarantine"
        return self


class DatabaseConfig(BaseModel):
    journal_mode: str = "WAL"
    synchronous: str = "NORMAL"
    busy_timeout_ms: int = 5000


class LoggingConfig(BaseModel):
    model_config = {"populate_by_name": True}

    level: str = "INFO"
    # `json` shadows BaseModel.json in pydantic v2; alias the attribute.
    json_format: bool = Field(default=True, alias="json")


class DiscoveryConfig(BaseModel):
    user_agent: str
    request_timeout_s: int = 30
    rate_limit_per_second: float = 1.0


class BrowserConfig(BaseModel):
    headless: bool = False
    profile_dir: Path
    artifacts_dir: Path
    challenge_timeout_s: int = 180
    page_settle_ms: int = 5000
    channel: str = ""

    @field_validator("profile_dir", "artifacts_dir", mode="before")
    @classmethod
    def _to_path(cls, v: Any) -> Path:
        p = Path(v)
        return p if p.is_absolute() else (REPO_ROOT / p)


class OCRConfig(BaseModel):
    min_chars_per_page: int = 50
    languages: str = "eng+ind"


class LLMConfig(BaseModel):
    model_name: str = ""
    context_budget_tokens: int = 8000


class DownloadConfig(BaseModel):
    retry_backoff_seconds: int = 300
    retry_backoff_max_seconds: int = 86400


class Config(BaseModel):
    paths: PathsConfig
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    discovery: DiscoveryConfig
    browser: BrowserConfig
    download: DownloadConfig = Field(default_factory=DownloadConfig)
    ocr: OCRConfig = Field(default_factory=OCRConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)

    def ensure_dirs(self) -> None:
        for p in (
            self.paths.data_dir,
            self.paths.files_dir,
            self.paths.logs_dir,
            self.paths.quarantine_dir,
            self.paths.db_path.parent,
            self.browser.profile_dir,
            self.browser.artifacts_dir,
        ):
            p.mkdir(parents=True, exist_ok=True)


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _env_overrides() -> dict:
    """Parse env vars like STOCK_TRIAGE__LOGGING__LEVEL=DEBUG into nested dict."""
    out: dict = {}
    for key, val in os.environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        parts = key[len(ENV_PREFIX):].lower().split("__")
        if not parts:
            continue
        cursor = out
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = _coerce_scalar(val)
    return out


def _coerce_scalar(v: str) -> Any:
    if v.lower() in {"true", "false"}:
        return v.lower() == "true"
    try:
        if "." in v:
            return float(v)
        return int(v)
    except ValueError:
        return v


def load_config(path: str | Path | None = None) -> Config:
    cfg_path = Path(path or os.environ.get("STOCK_TRIAGE_CONFIG") or DEFAULT_CONFIG_PATH)
    with cfg_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    merged = _deep_merge(raw, _env_overrides())
    return Config.model_validate(merged)
