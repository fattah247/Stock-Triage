from __future__ import annotations

import subprocess
import sys
import time

from stock_triage.dashboard.runs import (
    RUN_BLOCKED,
    RUN_CANCELLED,
    RUN_FAILED,
    RUN_OK,
    RUN_PARTIAL,
    RunLedger,
    sequence_steps,
    stop_process,
    summarize_run,
)


def test_stop_process_interrupts_then_escalates_for_stubborn_children() -> None:
    polite = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    started = time.monotonic()
    stop_process(polite, grace_s=5)
    assert polite.poll() is not None
    assert time.monotonic() - started < 5

    stubborn = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, sys, time; signal.signal(signal.SIGINT, signal.SIG_IGN); "
            "print('ready', flush=True); time.sleep(30)",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert stubborn.stdout is not None and stubborn.stdout.readline().strip() == "ready"
    stop_process(stubborn, grace_s=0.2)
    assert stubborn.poll() is not None


def test_run_status_summary() -> None:
    assert summarize_run(["ok", "ok"]) == RUN_OK
    assert summarize_run(["failed", "skipped", "ok"]) == RUN_PARTIAL
    assert summarize_run(["failed", "skipped"]) == RUN_FAILED
    assert summarize_run(["ok", "cancelled"]) == RUN_CANCELLED
    assert summarize_run(["busy", "skipped"]) == RUN_BLOCKED


def test_sequence_steps_can_retry_a_subset() -> None:
    steps = sequence_steps("daily_sweep", {"steps": "parse,digest"})
    assert [step.action for step in steps] == ["parse", "digest"]
    assert [step.action for step in sequence_steps("parse", {})] == ["parse"]


def test_run_ledger_tails_output_and_compacts(tmp_path) -> None:
    ledger = RunLedger(tmp_path / "runs.jsonl")
    ledger.MAX_LINES, ledger.KEEP_LINES = 6, 3
    for index in range(8):
        ledger.append({"action": f"run-{index}", "stdout": "x" * 10_000})
    (tmp_path / "runs.jsonl").open("a").write("not json\n")

    recent = ledger.recent(10)
    assert [entry["action"] for entry in recent][:2] == ["run-7", "run-6"]
    assert len(recent) <= 6
    assert len(recent[0]["stdout"]) == RunLedger.OUTPUT_TAIL_CHARS
