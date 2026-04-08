"""Structured JSON logging + per-stage counters (PRD §6.11)."""

from __future__ import annotations

import json
import logging
import sys
import threading
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from ..config import Config

_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime",
}


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k in _RESERVED or k.startswith("_"):
                continue
            payload[k] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(cfg: Config) -> None:
    cfg.paths.logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(cfg.logging.level.upper())

    stdout_handler = logging.StreamHandler(sys.stdout)
    file_handler = logging.FileHandler(cfg.paths.logs_dir / "stock_triage.log", encoding="utf-8")

    if cfg.logging.json_format:
        fmt: logging.Formatter = JSONFormatter()
    else:
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    stdout_handler.setFormatter(fmt)
    file_handler.setFormatter(fmt)
    root.addHandler(stdout_handler)
    root.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


class StageCounters:
    """Thread-safe stage counters for the processing state machine (PRD §5.2).

    Stages: discovered, downloaded, parsed, extracted, analyzed, failed.
    """

    STAGES = ("discovered", "downloaded", "parsed", "extracted", "analyzed", "failed")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = defaultdict(int)

    def incr(self, stage: str, n: int = 1) -> None:
        with self._lock:
            self._counts[stage] += n

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {s: self._counts.get(s, 0) for s in self.STAGES}
