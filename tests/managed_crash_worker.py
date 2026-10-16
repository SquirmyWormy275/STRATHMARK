"""Private test process fault wrapper. No application endpoint selects faults."""

import sys
import time
from pathlib import Path

from strathmark.events import EventOperations, Principal

database, event_id, marker, phase = sys.argv[1:]
ops = EventOperations(database)
principal = Principal("judge", frozenset({"synthetic:missoula"}), "director")
state = ops.read(principal, event_id)
original = ops.store.append


def barrier(*args):
    original(*args)
    Path(marker).write_text("before_commit")
    while True:
        time.sleep(1)


if phase == "precommit":
    ops.store.append = barrier
ops.execute(
    principal, event_id, "crash-request", state["revision"], "cli", "handover", {"editor": "gui"}
)
Path(marker).write_text("after_commit_before_response")
while True:
    time.sleep(1)
