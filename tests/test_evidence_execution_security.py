"""Release proofs reject ambient execution controls and substituted native tools."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.evidence_execution import (
    evidence_environment,
    isolated_python_argv,
    pinned_executable,
    require_clean_release_inputs,
)
from tests.test_package_release import _git, _release_repository


@pytest.mark.parametrize(
    "name", ["conftest.py", "sitecustomize.py", "pytest.py", "scripts/hook.py"]
)
def test_untracked_execution_inputs_are_rejected(tmp_path, name):
    repository = _release_repository(tmp_path)
    path = repository / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("raise RuntimeError('untrusted')\n", encoding="utf-8")
    with pytest.raises(ValueError, match="committed source inputs"):
        require_clean_release_inputs(repository)


@pytest.mark.parametrize("name", ["conftest.py", "__pycache__/hook.cpython-313.pyc"])
def test_ignored_root_hook_is_rejected(tmp_path, name):
    repository = _release_repository(tmp_path)
    (repository / ".gitignore").write_text(f"{name}\n", encoding="utf-8")
    _git(repository, "add", ".gitignore")
    _git(repository, "commit", "-m", "ignore fixture")
    path = repository / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("raise RuntimeError('untrusted')\n", encoding="utf-8")
    with pytest.raises(ValueError, match="ignored executable inputs"):
        require_clean_release_inputs(repository)


def test_git_identity_ignores_path_repository_and_process_overrides(tmp_path, monkeypatch):
    from scripts.release_evidence import git_head, source_tree_digest

    repository = _release_repository(tmp_path)
    expected = git_head(repository)
    digest = source_tree_digest(repository)
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    marker = attacker / "executed"
    fake_git = attacker / "git"
    fake_git.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n", encoding="utf-8")
    fake_git.chmod(0o700)
    _git(repository, "config", "core.fsmonitor", str(fake_git))
    monkeypatch.setenv("PATH", str(attacker))
    monkeypatch.setenv("GIT_DIR", str(attacker))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(fake_git))
    require_clean_release_inputs(repository)
    assert git_head(repository) == expected
    assert source_tree_digest(repository) == digest
    assert not marker.exists()


def test_child_does_not_inherit_python_pytest_or_secret_settings(tmp_path, monkeypatch):
    for key in ("PYTHONPATH", "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "LD_PRELOAD", "GH_TOKEN"):
        monkeypatch.setenv(key, "untrusted")
    environment = evidence_environment(
        {
            "STRATHMARK_TEST_DB": "1",
            "STRATHMARK_DB_PATH": str(tmp_path / "v2.sqlite"),
            "STRATHMARK_V3_DB_PATH": str(tmp_path / "v3.sqlite"),
        }
    )
    assert (
        not {"PYTHONPATH", "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "LD_PRELOAD", "GH_TOKEN"}
        & environment.keys()
    )
    assert environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    marker = tmp_path / "executed"
    (tmp_path / "sitecustomize.py").write_text(
        f"from pathlib import Path; Path({str(marker)!r}).touch()", encoding="utf-8"
    )
    subprocess.run(
        isolated_python_argv(
            [
                sys.executable,
                "-c",
                "import sys; assert sys.flags.isolated and sys.dont_write_bytecode",
            ]
        ),
        cwd=tmp_path,
        env=environment,
        check=True,
    )
    assert not marker.exists()


def test_telemetry_uses_pinned_absolute_tool_and_rejects_substitution(tmp_path, monkeypatch):
    tool = tmp_path / "approved-nvidia-smi"
    tool.write_bytes(b"approved fixture executable")
    digest = hashlib.sha256(tool.read_bytes()).hexdigest()
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    (attacker / "nvidia-smi").write_bytes(b"untrusted executable")
    monkeypatch.setenv("PATH", str(attacker))
    assert pinned_executable(tool, digest) == {"path": str(tool), "sha256": digest}
    with pytest.raises(ValueError, match="absolute path"):
        pinned_executable(Path("nvidia-smi"), digest)
    tool.write_bytes(b"substituted executable")
    with pytest.raises(ValueError, match="approved identity"):
        pinned_executable(tool, digest)
