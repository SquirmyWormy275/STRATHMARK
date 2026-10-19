"""Run one deterministic, zero-based pytest shard without extra dependencies.

Example: python scripts/run_pytest_shard.py --index 0 --count 4 -- tests/ -q
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest


def shard_for_nodeid(nodeid: str, count: int) -> int:
    if count < 1:
        raise ValueError("shard count must be positive")
    return int.from_bytes(hashlib.sha256(nodeid.encode("utf-8")).digest(), "big") % count


class ShardSelection:
    def __init__(self, index: int, count: int) -> None:
        if count < 1 or not 0 <= index < count:
            raise ValueError(
                "shard count must be positive and index must satisfy 0 <= index < count"
            )
        self.index = index
        self.count = count

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, config, items) -> None:
        selected, deselected = [], []
        for item in items:
            destination = (
                selected if shard_for_nodeid(item.nodeid, self.count) == self.index else deselected
            )
            destination.append(item)
        items[:] = selected
        if deselected:
            config.hook.pytest_deselected(items=deselected)
        message = (
            f"Shard {self.index}/{self.count} (zero-based): selected {len(selected)} "
            f"of {len(selected) + len(deselected)} eligible tests; deselected {len(deselected)}."
        )
        reporter = config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(message)
        else:
            print(message)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=int, required=True, help="zero-based shard index")
    parser.add_argument("--count", type=int, required=True, help="total number of shards")
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        plugin = ShardSelection(args.index, args.count)
    except ValueError as exc:
        parser.error(str(exc))
    if not args.pytest_args or args.pytest_args[0] != "--":
        parser.error("separate pytest arguments with --")
    # Match python -m pytest's checkout-local imports when invoked by file path.
    sys.path.insert(0, str(Path.cwd()))
    return int(pytest.main(args.pytest_args[1:], plugins=[plugin]))


if __name__ == "__main__":
    raise SystemExit(main())
