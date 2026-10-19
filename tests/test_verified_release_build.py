"""Publication must stop when the source or verified build environment changes."""

from __future__ import annotations

import json
import subprocess

import pytest

from scripts import build_release as module
from tests.test_package_release import _git, _release_repository


@pytest.mark.parametrize("mutation", ["source", "toolchain"])
def test_build_rechecks_identity_before_recording_success(tmp_path, monkeypatch, mutation):
    source = _release_repository(tmp_path)
    (source / ".gitignore").write_text("dist/\n")
    _git(source, "add", ".gitignore")
    _git(source, "commit", "-m", "ignore generated release artifacts")
    run = subprocess.run
    build_seen = False

    def fake_run(command, **kwargs):
        nonlocal build_seen
        if command[0] == "git":
            return run(command, **kwargs)
        if "build" in command:
            assert "--no-isolation" in command
            assert kwargs["env"]["PIP_NO_INDEX"] == "1"
            build_seen = True
            dist = source / "dist"
            dist.mkdir()
            (dist / "strathmark.whl").write_bytes(b"synthetic wheel")
            (dist / "strathmark.tar.gz").write_bytes(b"synthetic source archive")
            if mutation == "source":
                (source / "pyproject.toml").write_text("changed after authorized source check")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        module,
        "verify_toolchain",
        lambda *args, **kwargs: {"verified": not (build_seen and mutation == "toolchain")},
    )
    provenance = tmp_path / "provenance.json"
    with pytest.raises(ValueError, match="clean|changed"):
        module.build_release(source, tmp_path / "wheels", provenance)
    assert build_seen
    assert not provenance.exists()


def test_build_refuses_stale_or_ambient_wheelhouse(tmp_path):
    source = _release_repository(tmp_path)
    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir()
    (wheelhouse / "ambient.whl").write_bytes(b"unreviewed")
    with pytest.raises(ValueError, match="fresh wheelhouse"):
        module.build_release(source, wheelhouse, tmp_path / "provenance.json")


def test_installed_rehearsal_cannot_replace_ignored_artifact_bytes(tmp_path, monkeypatch):
    source = _release_repository(tmp_path)
    (source / ".gitignore").write_text("dist/\n")
    _git(source, "add", ".gitignore")
    _git(source, "commit", "-m", "ignore generated artifacts")
    dist = source / "dist"
    dist.mkdir()
    wheel = dist / "strathmark.whl"
    wheel.write_bytes(b"reviewed wheel")
    (dist / "strathmark.tar.gz").write_bytes(b"reviewed sdist")
    monkeypatch.setattr(module, "verify_toolchain", lambda *args, **kwargs: {"verified": True})
    provenance = tmp_path / "provenance.json"
    provenance.write_text(
        json.dumps(
            {
                "source_commit": module.clean_source(source),
                "toolchain": {"verified": True},
                "artifacts": module.artifact_identity(source),
            }
        )
    )
    module.verify_built_release(source, tmp_path / "wheels", provenance)
    wheel.write_bytes(b"substituted after installed rehearsal")
    assert not _git(source, "status", "--porcelain", "--untracked-files=all")
    with pytest.raises(ValueError, match="artifacts.*changed"):
        module.verify_built_release(source, tmp_path / "wheels", provenance)
