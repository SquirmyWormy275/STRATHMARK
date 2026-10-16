import subprocess
import sys
import time
from pathlib import Path

import pytest

from strathmark.events import EventOperations, Principal
from strathmark.events.examples import synthetic_event


@pytest.mark.parametrize("phase", ["precommit", "postcommit"])
def test_real_process_kill_retains_atomic_state_and_exact_receipt(tmp_path, phase):
    ops = EventOperations(tmp_path / "authority.sqlite3")
    principal = Principal("judge", frozenset({"synthetic:missoula"}), "director")
    state = ops.create(principal, "create", synthetic_event(), "cli")
    marker = tmp_path / "barrier"
    child = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("managed_crash_worker.py")),
            str(ops.store.path),
            state["event_id"],
            str(marker),
            phase,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 15
        while not marker.exists() and time.monotonic() < deadline and child.poll() is None:
            time.sleep(0.02)
        assert marker.exists(), child.communicate(timeout=1)
        child.kill()
        child.communicate(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)
    reopened = EventOperations(ops.store.path)
    before_retry = reopened.read(principal, state["event_id"])
    assert before_retry["revision"] == (1 if phase == "precommit" else 2)
    recovered = reopened.execute(
        principal, state["event_id"], "crash-request", 1, "cli", "handover", {"editor": "gui"}
    )
    assert recovered["revision"] == 2
    with reopened.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM commands").fetchone()[0] == 2
