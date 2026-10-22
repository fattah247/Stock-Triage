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
    model_name: str = "qwen3.5:9b"
    base_url: str = "http://localhost:11434/api"
    context_budget_tokens: int = 8000
    temperature: float = 0.0
    timeout_s: int = 600
    # Reasoning models (Qwen 3.5 and similar) think before answering unless told
    # not to; structured JSON output does not need it, and on a laptop it costs
    # minutes. Models without a thinking mode ignore it.
    think: bool = False


class DownloadConfig(BaseModel):
    retry_backoff_seconds: int = 300
    retry_backoff_max_seconds: int = 86400


class DashboardConfig(BaseModel):
    output_dir: Path = REPO_ROOT / "data" / "dashboard"
    title: str = "Stock Triage Dashboard"
    host: str = "127.0.0.1"
    port: int = 8765

    @field_validator("output_dir", mode="before")
    @classmethod
    def _to_path(cls, v: Any) -> Path:
        p = Path(v)
        return p if p.is_absolute() else (REPO_ROOT / p)


class AlertsConfig(BaseModel):
    watchlist: list[str] = Field(default_factory=list)
    min_severity: str = "warning"
    lookback_days: int = 7
    channels: list[str] = Field(default_factory=lambda: ["jsonl"])
    # Most desktop notifications one run may show; the rest arrive as one summary.
    push_max_per_run: int = 5
    jsonl_path: Path = REPO_ROOT / "data" / "alerts" / "alerts.jsonl"

    @field_validator("jsonl_path", mode="before")
    @classmethod
    def _to_path(cls, v: Any) -> Path:
        p = Path(v)
        return p if p.is_absolute() else (REPO_ROOT / p)


class CanaryConfig(BaseModel):
    sources: list[str] = Field(default_factory=list)
    tickers: list[str] = Field(default_factory=list)


class ThesesConfig(BaseModel):
    definition_path: Path = REPO_ROOT / "data" / "theses.yaml"

    @field_validator("definition_path", mode="before")
    @classmethod
    def _to_path(cls, v: Any) -> Path:
        p = Path(v)
        return p if p.is_absolute() else (REPO_ROOT / p)


class CompanyOverridesConfig(BaseModel):
    definition_path: Path = REPO_ROOT / "data" / "company_overrides.yaml"

    @field_validator("definition_path", mode="before")
    @classmethod
    def _to_path(cls, v: Any) -> Path:
        p = Path(v)
        return p if p.is_absolute() else (REPO_ROOT / p)


class MarketConfig(BaseModel):
    """Market risk layer (docs/market_risk_plan.md)."""

    catalysts_path: Path = REPO_ROOT / "data" / "catalysts.yaml"
    index_membership_path: Path = REPO_ROOT / "data" / "index_membership.yaml"
    # Days ahead of a risk date that its reminder fires.
    catalyst_lead_days: int = 3
    # Watchlist companies at or above this exposure score raise an alert.
    exposure_alert_score: float = 70.0
    # Fundamental stress breadth raises an alert from this level up.
    breadth_alert_level: str = "stressed"
    # Rise, in percentage points versus the prior fiscal year, for each level.
    breadth_thresholds_pts: dict[str, float] = Field(
        default_factory=lambda: {"elevated": 5.0, "stressed": 10.0, "crisis": 20.0}
    )
    # Fewer companies than this reporting a figure in both years makes the
    # comparison noise: with 20, four companies flipping moves it 20 points.
    breadth_min_filers: int = 30
    # Market data (docs/phase2_market_recon.md).
    idx_page_url: str = "https://www.idx.co.id/en/market-data/trading-summary/index-summary"
    jisdor_url: str = "https://www.bi.go.id/id/statistik/informasi-kurs/jisdor/default.aspx"
    # IDX-IC sector indices tracked for the sector-drawdown signal.
    sectors: list[str] = Field(
        default_factory=lambda: [
            "IDXENERGY",
            "IDXBASIC",
            "IDXINDUST",
            "IDXNONCYC",
            "IDXCYCLIC",
            "IDXHEALTH",
            "IDXFINANCE",
            "IDXPROPERT",
            "IDXTECHNO",
            "IDXINFRA",
            "IDXTRANS",
        ]
    )
    # Signal thresholds per level; starting points until the phase 5 backtest.
    thresholds: dict[str, dict[str, float]] = Field(
        default_factory=lambda: {
            "drawdown_pct": {"elevated": 5.0, "stressed": 10.0, "crisis": 20.0},
            "vol_ratio": {"elevated": 1.5, "stressed": 2.0, "crisis": 3.0},
            "foreign_sell_streak": {"elevated": 5.0, "stressed": 10.0, "crisis": 15.0},
            "usdidr_change_20d_pct": {"elevated": 2.0, "stressed": 3.0, "crisis": 5.0},
            "decline_share_pct": {"elevated": 60.0, "stressed": 70.0, "crisis": 80.0},
            "sector_drawdown_pct": {"elevated": 7.0, "stressed": 12.0, "crisis": 25.0},
            "sector_breadth_pct": {"elevated": 50.0, "stressed": 70.0, "crisis": 90.0},
            "active_themes": {"elevated": 1.0, "stressed": 2.0, "crisis": 3.0},
            "exchange_notices_ratio": {"elevated": 2.5, "stressed": 3.5, "crisis": 5.0},
        }
    )
    # The state is the highest level reached by at least this many signals.
    regime_min_signals: int = 2
    # The state steps down only after this many sessions at a lower level.
    regime_step_down_after_sessions: int = 5
    # A two-signal crisis also needs IHSG's own drawdown at this level (none: off).
    regime_crisis_needs_index: str | None = "stressed"
    # The sector signal: "share" counts sectors at least sector_breadth_cutoff_pct
    # below their 52-week high; "worst" takes the worst sector, which the phase 5
    # backtest found reads crisis on most days (docs/market_calibration.md).
    sector_signal: str = "share"
    sector_breadth_cutoff_pct: float = 10.0
    # Crash-risk estimate (component 6): a crash is a fall of at least this many
    # percent from a day's close at any point within the horizon.
    crash_risk_drawdown_pct: float = 10.0
    crash_risk_horizon_sessions: int = 63
    # The probability that counts as a warning, for the track record and alerts.
    crash_risk_alert_probability: float = 0.4
    # The estimate is shown as a forecast only when its walk-forward Brier score
    # beats the base rate's by at least this share; otherwise only analogs show.
    crash_risk_min_skill: float = 0.05
    # An episode closes after this many calm sessions in a row.
    episode_close_after_calm_sessions: int = 20
    # Risk discovery (component 5). Both files ship in config/; point these at
    # your own copies to change the sources or the theme taxonomy.
    watch_sources_path: Path = REPO_ROOT / "config" / "watch_sources.yaml"
    risk_themes_path: Path = REPO_ROOT / "config" / "risk_themes.yaml"
    # A theme is active with at least this many tagged items over three days.
    theme_active_min_items: int = 3

    @field_validator("catalysts_path", "index_membership_path", "watch_sources_path", "risk_themes_path", mode="before")
    @classmethod
    def _to_path(cls, v: Any) -> Path:
        p = Path(v)
        return p if p.is_absolute() else (REPO_ROOT / p)


class Config(BaseModel):
    paths: PathsConfig
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    discovery: DiscoveryConfig
    browser: BrowserConfig
    download: DownloadConfig = Field(default_factory=DownloadConfig)
    ocr: OCRConfig = Field(default_factory=OCRConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    canary: CanaryConfig = Field(default_factory=CanaryConfig)
    theses: ThesesConfig = Field(default_factory=ThesesConfig)
    company_overrides: CompanyOverridesConfig = Field(default_factory=CompanyOverridesConfig)
    market: MarketConfig = Field(default_factory=MarketConfig)

    @model_validator(mode="after")
    def _derive_data_paths(self) -> "Config":
        """Keep data-dir-relative files with ``paths.data_dir`` unless set explicitly.

        Their defaults are fixed to the repo's ``data/`` directory, so a config
        that moves ``data_dir`` would otherwise still write there.
        """
        data_dir = self.paths.data_dir
        if "output_dir" not in self.dashboard.model_fields_set:
            self.dashboard.output_dir = data_dir / "dashboard"
        if "jsonl_path" not in self.alerts.model_fields_set:
            self.alerts.jsonl_path = data_dir / "alerts" / "alerts.jsonl"
        if "definition_path" not in self.theses.model_fields_set:
            self.theses.definition_path = data_dir / "theses.yaml"
        if "definition_path" not in self.company_overrides.model_fields_set:
            self.company_overrides.definition_path = data_dir / "company_overrides.yaml"
        if "catalysts_path" not in self.market.model_fields_set:
            self.market.catalysts_path = data_dir / "catalysts.yaml"
        if "index_membership_path" not in self.market.model_fields_set:
            self.market.index_membership_path = data_dir / "index_membership.yaml"
        return self

    def ensure_dirs(self) -> None:
        for p in (
            self.paths.data_dir,
            self.paths.files_dir,
            self.paths.logs_dir,
            self.paths.quarantine_dir,
            self.paths.db_path.parent,
            self.browser.profile_dir,
            self.browser.artifacts_dir,
            self.dashboard.output_dir,
            self.alerts.jsonl_path.parent,
            self.theses.definition_path.parent,
            self.company_overrides.definition_path.parent,
            self.market.catalysts_path.parent,
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
