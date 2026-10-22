from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time

from stock_triage.runlock import PipelineBusyError, lock_path_for, pipeline_lock, read_lock_holder

_HOLD = """
import sys, time
from pathlib import Path
from stock_triage.runlock import pipeline_lock
with pipeline_lock(Path(sys.argv[1]), command="download --source all"):
    print("locked", flush=True)
    time.sleep(60)
"""


def _spawn_holder(lock_path):
    child = subprocess.Popen([sys.executable, "-c", _HOLD, str(lock_path)], stdout=subprocess.PIPE, text=True)
    assert child.stdout is not None and child.stdout.readline().strip() == "locked"
    return child


def test_a_crashed_holder_reads_as_free_even_if_its_pid_is_reused(tmp_config) -> None:
    lock_path = lock_path_for(tmp_config.paths.data_dir)
    child = _spawn_holder(lock_path)
    assert read_lock_holder(lock_path)["command"] == "download --source all"

    child.send_signal(signal.SIGKILL)
    child.wait(timeout=5)
    # SIGKILL skips cleanup: the record still names the dead holder.
    assert json.loads(lock_path.read_text())["pid"] == child.pid
    assert read_lock_holder(lock_path) is None

    # Even a record pointing at a live process (a reused pid) is not a holder.
    lock_path.write_text(json.dumps({"pid": os.getpid(), "command": "stale"}))
    assert read_lock_holder(lock_path) is None
    with pipeline_lock(lock_path, command="parse"):
        assert read_lock_holder(lock_path)["command"] == "parse"


def test_probing_never_refuses_a_command_that_is_starting(tmp_config) -> None:
    lock_path = lock_path_for(tmp_config.paths.data_dir)
    with pipeline_lock(lock_path, command="warm-up"):
        pass
    stop = threading.Event()
    seen: list[object] = []

    def probe() -> None:
        while not stop.is_set():
            seen.append(read_lock_holder(lock_path))

    probers = [threading.Thread(target=probe) for _ in range(2)]
    for thread in probers:
        thread.start()
    refused = 0
    try:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                with pipeline_lock(lock_path, command="parse"):
                    pass
            except PipelineBusyError:
                refused += 1
    finally:
        stop.set()
        for thread in probers:
            thread.join()

    assert refused == 0
    assert seen and all(holder is None or holder.get("command") == "parse" for holder in seen)
