"""Byte authentication cannot be replaced by matching package version labels."""

from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import verify_toolchain as tooling


@pytest.fixture
def approved_wheel(tmp_path, monkeypatch):
    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir()
    installed = tmp_path / "site-packages"
    installed.mkdir()
    wheel = wheelhouse / "proof_tool-1.0-py3-none-any.whl"
    members = {
        "proof_tool/__init__.py": b"VALUE = 'original'\n",
        "proof_tool/check.py": b"def verify(): return True\n",
        "proof_tool-1.0.dist-info/METADATA": b"Metadata-Version: 2.1\nName: proof-tool\nVersion: 1.0\n",
    }
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
            target = installed / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    lock = tmp_path / "approved.lock"
    lock.write_text(
        "proof-tool==1.0 --hash=sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest() + "\n"
    )
    required = tmp_path / "required.in"
    required.write_text("proof-tool==1.0\n")
    monkeypatch.setattr(
        tooling.importlib.metadata,
        "distribution",
        lambda name: SimpleNamespace(
            version="1.0",
            locate_file=lambda relative: installed / relative,
        ),
    )
    return lock, wheelhouse, installed, required


def test_verified_wheel_and_exact_installed_bytes_produce_bound_identity(approved_wheel):
    lock, wheelhouse, _installed, required = approved_wheel
    result = tooling.verify_toolchain(lock, wheelhouse, installed=True, required=(required,))
    assert result["lock_sha256"] == hashlib.sha256(lock.read_bytes()).hexdigest()
    assert result["installed_files_sha256"]
    assert len(result["wheel_sha256"]) == 1


@pytest.mark.parametrize("target", ["wheel", "installed", "extra", "bytecode"])
def test_same_version_tamper_is_rejected(approved_wheel, target):
    lock, wheelhouse, installed, required = approved_wheel
    if target == "wheel":
        with zipfile.ZipFile(next(wheelhouse.glob("*.whl")), "a") as archive:
            archive.writestr("forged.py", "print('forged verification')")
    elif target == "installed":
        (installed / "proof_tool/check.py").write_text("def verify(): return 'forged'\n")
    elif target == "extra":
        (installed / "proof_tool/forged.py").write_text("print('forged verification')")
    else:
        cache = installed / "proof_tool/__pycache__"
        cache.mkdir()
        (cache / "check.cpython-313.pyc").write_bytes(b"forged cached code")
    with pytest.raises(ValueError, match="differs|differ|unexpected"):
        tooling.verify_toolchain(lock, wheelhouse, installed=True, required=(required,))


def test_required_version_must_be_the_actual_wheel_version(approved_wheel):
    lock, wheelhouse, _installed, required = approved_wheel
    lock.write_text(lock.read_text() + "proof-tool==2.0 --hash=sha256:" + "a" * 64 + "\n")
    required.write_text("proof-tool==2.0\n")
    with pytest.raises(ValueError, match="missing a required exact"):
        tooling.verify_toolchain(lock, wheelhouse, required=(required,))


def test_unhashed_or_direct_url_requirements_are_not_trusted(tmp_path):
    lock = tmp_path / "untrusted.lock"
    for value in [
        "proof-tool==1.0",
        "proof-tool==1.0 --hash=sha256:" + "a" * 64 + " --extra-index-url https://example.invalid",
    ]:
        lock.write_text(value)
        with pytest.raises(ValueError, match="exact versions and SHA-256"):
            tooling.locked_hashes(lock)


def test_python_identity_requires_separate_approval_before_wheel_checks(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATHMARK_VERIFIED_WHEELHOUSE", str(tmp_path))
    monkeypatch.setenv("STRATHMARK_EVIDENCE_PYTHON_SHA256", "f" * 64)
    with pytest.raises(ValueError, match="Python differs from its approved digest"):
        tooling.qualification_toolchain(tmp_path)


def test_qualification_rechecks_approved_wheel_bytes_after_use(
    approved_wheel, monkeypatch, tmp_path
):
    lock, wheelhouse, installed, required = approved_wheel
    requirements = tmp_path / "source/requirements"
    requirements.mkdir(parents=True)
    (requirements / "v3-verified.lock").write_bytes(lock.read_bytes())
    for name in ("v3-release.lock", "v3-evidence-tools.lock"):
        (requirements / name).write_bytes(required.read_bytes())
    monkeypatch.setenv("STRATHMARK_VERIFIED_WHEELHOUSE", str(wheelhouse))
    monkeypatch.setenv(
        "STRATHMARK_EVIDENCE_PYTHON_SHA256",
        hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest(),
    )
    before = tooling.qualification_toolchain(requirements.parent)
    assert json.dumps(before)
    (installed / "proof_tool/check.py").write_text("def verify(): return 'changed during proof'\n")
    with pytest.raises(ValueError, match="installed toolchain bytes differ"):
        tooling.qualification_toolchain(requirements.parent)


def test_platform_marker_requires_only_matching_locked_artifacts(approved_wheel):
    lock, wheelhouse, _installed, required = approved_wheel
    lock.write_text(
        lock.read_text()
        + "absent==1.0 ; sys_platform == 'unsupported-fixture' --hash=sha256:"
        + "a" * 64
        + "\n"
    )
    tooling.verify_toolchain(lock, wheelhouse, required=(required,))
    lock.write_text(lock.read_text() + "required-dependency==1.0 --hash=sha256:" + "b" * 64 + "\n")
    with pytest.raises(ValueError, match="missing a required exact"):
        tooling.verify_toolchain(lock, wheelhouse, required=(required,))


def test_lock_marker_cannot_execute_python(tmp_path):
    lock = tmp_path / "bad.lock"
    lock.write_text("proof-tool==1.0 ; __import__('os').system('false') --hash=sha256:" + "a" * 64)
    with pytest.raises(ValueError, match="unsupported toolchain lock marker"):
        tooling.locked_hashes(lock)
