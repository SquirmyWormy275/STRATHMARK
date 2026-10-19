"""The union of CI shards must run the full selected suite exactly once."""

from __future__ import annotations

import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.run_pytest_shard import ShardSelection, main, shard_for_nodeid

WRAPPER = Path(__file__).resolve().parents[1] / "scripts" / "run_pytest_shard.py"


def selected_nodeids(nodeids, index, count):
    notices, lines = [], []
    config = SimpleNamespace(
        hook=SimpleNamespace(
            pytest_deselected=lambda items: notices.extend(item.nodeid for item in items)
        ),
        pluginmanager=SimpleNamespace(
            get_plugin=lambda name: SimpleNamespace(write_line=lines.append)
        ),
    )
    items = [SimpleNamespace(nodeid=nodeid) for nodeid in nodeids]
    ShardSelection(index, count).pytest_collection_modifyitems(config, items)
    selected = [item.nodeid for item in items]
    assert Counter(selected + notices) == Counter(nodeids)
    assert lines == [
        f"Shard {index}/{count} (zero-based): selected {len(selected)} "
        f"of {len(nodeids)} eligible tests; deselected {len(notices)}."
    ]
    return selected


def test_every_node_is_selected_once_and_assignment_is_independent_of_order():
    nodeids = [
        f"tests/test_example.py::TestCases::test_round[athlete-é-{index}]" for index in range(200)
    ]
    shards = [selected_nodeids(nodeids, index, 7) for index in range(7)]
    assert Counter(nodeid for shard in shards for nodeid in shard) == Counter(nodeids)
    for index, shard in enumerate(shards):
        assert selected_nodeids(list(reversed(nodeids)), index, 7) == list(reversed(shard))


def test_one_shard_preserves_all_tests_and_order():
    nodeids = ["test_z.py::test_second", "test_a.py::test_first[param]"]
    assert selected_nodeids(nodeids, 0, 1) == nodeids


@pytest.mark.parametrize("index,count", [(-1, 3), (3, 3), (0, 0), (0, -1)])
def test_invalid_bounds_are_rejected_before_pytest(index, count, capsys):
    with pytest.raises(SystemExit) as error:
        main(["--index", str(index), "--count", str(count), "--", "tests/"])
    assert error.value.code == 2
    assert "0 <= index < count" in capsys.readouterr().err


def test_invalid_hash_partition_count_is_rejected():
    with pytest.raises(ValueError, match="count must be positive"):
        shard_for_nodeid("test_file.py::test_one", 0)


def run_wrapper(directory, index, count, *pytest_args):
    environment = dict(
        os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONHASHSEED=str(index + 10)
    )
    environment.pop("PYTHONPATH", None)
    return subprocess.run(
        [
            sys.executable,
            str(WRAPPER),
            "--index",
            str(index),
            "--count",
            str(count),
            "--",
            *pytest_args,
        ],
        cwd=directory,
        env=environment,
        text=True,
        capture_output=True,
        timeout=60,
    )


@pytest.mark.parametrize("count", [1, 3])
def test_real_pytest_shards_execute_every_selected_case_once(tmp_path, count):
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "root_support.py").write_text("CASE_COUNT = 24\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_cases.py").write_text(
        "from pathlib import Path\n"
        "import pytest\n"
        "from root_support import CASE_COUNT\n"
        "@pytest.mark.parametrize('number', range(CASE_COUNT))\n"
        "def test_selected(number):\n"
        "    with Path('executed.txt').open('a') as output:\n"
        "        output.write(str(number) + '\\n')\n"
        "def test_excluded():\n"
        "    raise AssertionError('the original -k selection must be preserved')\n",
        encoding="utf-8",
    )
    for index in range(count):
        result = run_wrapper(
            tmp_path, index, count, "-q", "-k", "selected", "-p", "no:cacheprovider"
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert f"Shard {index}/{count} (zero-based): selected" in result.stdout
        assert "of 24 eligible tests" in result.stdout
        assert "deselected" in result.stdout
    assert Counter((tmp_path / "executed.txt").read_text().splitlines()) == Counter(
        map(str, range(24))
    )


def test_real_pytest_failure_exit_status_is_preserved(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "test_failure.py").write_text(
        "def test_failure():\n    assert False\n", encoding="utf-8"
    )
    result = run_wrapper(tmp_path, 0, 1, "-q", "-p", "no:cacheprovider")
    assert result.returncode == 1
    assert "1 failed" in result.stdout


def test_empty_selection_does_not_turn_into_a_successful_gate(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "test_example.py").write_text("def test_example():\n    pass\n", encoding="utf-8")
    result = run_wrapper(tmp_path, 0, 1, "-q", "-k", "missing", "-p", "no:cacheprovider")
    assert result.returncode == 5
    assert "selected 0 of 0 eligible tests" in result.stdout
