"""Run plans, step outcomes, and the run ledger for dashboard actions.

The server owns every run so it outlives the browser tab that started it. A
sequence keeps going after a failed step: once a step that talks to IDX fails,
the remaining IDX steps are skipped, but local processing of what is already
on disk still runs.
"""

from __future__ import annotations

import json
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..runlock import EXIT_PIPELINE_BUSY

# Step and run states. A run is "partial" when some steps succeeded and others
# failed or were skipped, "blocked" when another process held the pipeline lock.
STEP_QUEUED = "queued"
STEP_RUNNING = "running"
STEP_OK = "ok"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"
STEP_CANCELLED = "cancelled"
STEP_BUSY = "busy"

RUN_RUNNING = "running"
RUN_OK = "ok"
RUN_PARTIAL = "partial"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"
RUN_BLOCKED = "blocked"

CANCEL_GRACE_SECONDS = 15.0
TERMINATE_GRACE_SECONDS = 5.0


@dataclass(frozen=True)
class SequenceStep:
    action: str
    label: str
    network: bool = False
    params: tuple[tuple[str, str], ...] = ()


# discover_financial clears --year so the adapter scans its rolling report-year
# window; "intelligence" derives snapshots for the issuers just analyzed.
SEQUENCES: dict[str, tuple[SequenceStep, ...]] = {
    "daily_sweep": (
        SequenceStep("discover_disclosure", "Discover disclosures", network=True),
        SequenceStep("discover_financial", "Discover financial reports", network=True, params=(("year", ""),)),
        SequenceStep("download_financial", "Download financial reports", network=True),
        SequenceStep("download_disclosure", "Download disclosures", network=True),
        SequenceStep("parse", "Parse documents"),
        SequenceStep("extract", "Extract metrics and events"),
        SequenceStep("rules", "Evaluate rules"),
        SequenceStep("intelligence", "Derive company intelligence"),
        SequenceStep("alerts", "Evaluate alerts"),
        SequenceStep("digest", "Build digest"),
    ),
}

SEQUENCE_LABELS = {"daily_sweep": "Daily sweep"}


def sequence_steps(action: str, params: dict[str, str]) -> list[SequenceStep]:
    """Steps for ``action``; a plain action is a one-step run.

    ``params["steps"]`` (comma-separated actions) narrows a sequence, which is
    how "retry failed steps" reruns only what did not succeed.
    """
    steps = SEQUENCES.get(action)
    if steps is None:
        return [SequenceStep(action, "")]
    wanted = {item.strip() for item in params.get("steps", "").split(",") if item.strip()}
    if not wanted:
        return list(steps)
    selected = [step for step in steps if step.action in wanted]
    if not selected:
        raise ValueError(f"no steps of {action} match: {', '.join(sorted(wanted))}")
    return selected


def describe_step(step: SequenceStep) -> dict[str, object]:
    return {"action": step.action, "label": step.label, "network": step.network}


def describe_sequences() -> dict[str, list[dict[str, object]]]:
    """Sequence routes for the dashboard to preview before a run starts."""
    return {name: [describe_step(step) for step in steps] for name, steps in SEQUENCES.items()}


def classify_step(*, returncode: int, cancelled: bool) -> str:
    if cancelled:
        return STEP_CANCELLED
    if returncode == 0:
        return STEP_OK
    if returncode == EXIT_PIPELINE_BUSY:
        return STEP_BUSY
    return STEP_FAILED


def summarize_run(step_states: list[str]) -> str:
    if STEP_CANCELLED in step_states:
        return RUN_CANCELLED
    if STEP_BUSY in step_states:
        return RUN_BLOCKED
    if step_states and all(state == STEP_OK for state in step_states):
        return RUN_OK
    if STEP_OK in step_states:
        return RUN_PARTIAL
    return RUN_FAILED


def stop_process(process: subprocess.Popen, *, grace_s: float = CANCEL_GRACE_SECONDS) -> None:
    """Ask a pipeline subprocess to stop, escalating if it ignores SIGINT.

    SIGINT lets the command unwind (browser closed, current writer transaction
    finished, pipeline lock released); writer jobs are transactional, so even
    SIGKILL cannot leave half-written rows.
    """
    if process.poll() is not None:
        return
    for sig, wait_s in ((signal.SIGINT, grace_s), (signal.SIGTERM, TERMINATE_GRACE_SECONDS)):
        try:
            process.send_signal(sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=wait_s)
            return
        except subprocess.TimeoutExpired:
            continue
    process.kill()


class RunLedger:
    """Append-only JSONL history of finished dashboard runs."""

    MAX_LINES = 500
    KEEP_LINES = 200
    OUTPUT_TAIL_CHARS = 6000

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    @classmethod
    def trimmed(cls, entry: dict[str, Any]) -> dict[str, Any]:
        record = dict(entry)
        for key in ("stdout", "stderr"):
            record[key] = str(record.get(key) or "")[-cls.OUTPUT_TAIL_CHARS :]
        return record

    def append(self, entry: dict[str, Any]) -> None:
        line = json.dumps(self.trimmed(entry), ensure_ascii=True)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            self._compact_locked()

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        """Most recent first; unreadable lines are skipped, never fatal."""
        with self._lock:
            lines = self._read_lines_locked()
        entries: list[dict[str, Any]] = []
        for line in reversed(lines):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                entries.append(payload)
            if len(entries) >= limit:
                break
        return entries

    def _read_lines_locked(self) -> list[str]:
        try:
            return [line for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except FileNotFoundError:
            return []

    def _compact_locked(self) -> None:
        lines = self._read_lines_locked()
        if len(lines) <= self.MAX_LINES:
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("\n".join(lines[-self.KEEP_LINES :]) + "\n", encoding="utf-8")
        tmp.replace(self.path)

