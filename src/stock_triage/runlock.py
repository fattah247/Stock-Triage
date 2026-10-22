"""Cross-process lock that serializes pipeline commands.

Pipeline commands share the browser profile, the download queue, and chunk
rewrites, so two of them must never overlap. The lock is an advisory
``flock`` on ``<data_dir>/pipeline.lock``; the OS releases it when the holder
exits, so a crashed run cannot leave it stuck.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any

try:  # POSIX only; on other platforms the lock degrades to a no-op.
    import fcntl
except ImportError:  # pragma: no cover - exercised only on Windows
    fcntl = None  # type: ignore[assignment]

# Commands that fetch from IDX or rewrite pipeline state run one at a time
# across processes. Every registered command must appear in exactly one of
# these sets (enforced by tests) so new verbs get classified deliberately.
PIPELINE_COMMANDS = frozenset(
    {
        "init",
        "discover",
        "download",
        "parse",
        "extract",
        "rules",
        "summary-generate",
        "intelligence-run",
        "intelligence-rederive",
        "intelligence-golden",
        "digest-run",
        "alerts-run",
        "canary-run",
        "coverage-run",
        "backfill",
        "backfill-sectors",
    }
)
UNLOCKED_COMMANDS = frozenset(
    {
        # Read-only reports.
        "parse-status",
        "extract-status",
        "rules-status",
        "backlog-status",
        "canary-status",
        "coverage-status",
        "coverage-preview",
        "coverage-plan",
        "nuance-audit",
        "review-queue",
        "intelligence-kpis",
        "intelligence-observability",
        "summary-context",
        "benchmark-sqlite",
        # Small single-transaction edits that must stay usable during a run.
        "review-update",
        "fiscal-year-override",
        "summary-validate",
        # Long-lived or file-only; the dashboard's own actions lock per job.
        "dashboard-serve",
        "dashboard-export",
    }
)

LOCK_FILENAME = "pipeline.lock"
# sysexits.h EX_TEMPFAIL: "try again later" - distinct from a real failure.
EXIT_PIPELINE_BUSY = 75


class PipelineBusyError(RuntimeError):
    def __init__(self, holder: dict[str, Any] | None) -> None:
        self.holder = holder or {}
        command = self.holder.get("command") or "another pipeline command"
        pid = self.holder.get("pid")
        since = self.holder.get("started_at")
        detail = f"{command}" + (f" (pid {pid})" if pid else "") + (f" since {since}" if since else "")
        super().__init__(f"pipeline busy: {detail} is still running; wait for it or stop it, then retry")


def lock_path_for(data_dir: Path) -> Path:
    return data_dir / LOCK_FILENAME


def _liveness_path(lock_path: Path) -> Path:
    return lock_path.with_name(lock_path.name + ".live")


@contextmanager
def pipeline_lock(lock_path: Path, *, command: str) -> Iterator[None]:
    if fcntl is None:
        yield
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle, _liveness_path(lock_path).open("a+") as liveness:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PipelineBusyError(_read_holder(handle)) from None
        _write_holder(
            handle,
            {
                "pid": os.getpid(),
                "command": command,
                "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
        )
        # Shared and blocking: a holder probe takes this only for an instant.
        fcntl.flock(liveness.fileno(), fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(liveness.fileno(), fcntl.LOCK_UN)
            _write_holder(handle, None)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_lock_holder(lock_path: Path) -> dict[str, Any] | None:
    """Return who holds the pipeline lock, or ``None`` when it is free.

    A holder keeps a shared lock on a sidecar file while it runs, so an
    exclusive probe there succeeds only when nobody does. Probing the sidecar
    instead of the pipeline lock means a command starting at that instant just
    waits a moment rather than being refused, and a crashed holder is noticed by
    the kernel (its locks die with it) rather than by a pid that may be reused.
    """
    if fcntl is None:
        return None
    liveness = _liveness_path(lock_path)
    if not liveness.exists():
        return None
    with liveness.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # A holder writes its record before taking the sidecar lock, so an
            # empty record here means another probe, not a running command.
            with lock_path.open("a+", encoding="utf-8") as record:
                return _read_holder(record)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return None


def _write_holder(handle: IO[str], holder: dict[str, Any] | None) -> None:
    handle.seek(0)
    handle.truncate()
    if holder is not None:
        handle.write(json.dumps(holder))
    handle.flush()


def _read_holder(handle: IO[str]) -> dict[str, Any] | None:
    handle.seek(0)
    try:
        payload = json.loads(handle.read() or "null")
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None
