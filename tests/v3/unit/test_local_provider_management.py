import hashlib
import json
import sys
from unittest.mock import Mock

import pytest

from strathmark.v3.factory import local_provider_cli as provider

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="the app-owned provider uses Linux process identities"
)


def test_provider_stop_refuses_a_reused_process_identifier(tmp_path, monkeypatch):
    binary = tmp_path / "ollama"
    binary.write_bytes(b"synthetic provider")
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "runtime_binary": str(binary),
                "runtime_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            }
        )
    )
    (tmp_path / "provider-process.json").write_text(
        json.dumps({"pid": 999, "start_ticks": "1", "binary": str(binary)})
    )
    monkeypatch.setattr(
        provider,
        "_process_identity",
        lambda _pid: {"pid": 999, "start_ticks": "2", "binary": str(binary)},
    )
    monkeypatch.setattr(
        provider.os,
        "kill",
        lambda *_args: pytest.fail("an unrelated process must not be signalled"),
    )
    with pytest.raises(ValueError, match="identity changed"):
        provider.manage(profile, "stop")


def test_provider_start_rejects_nonloopback_before_spawning(tmp_path, monkeypatch):
    binary = tmp_path / "ollama"
    binary.write_bytes(b"synthetic provider")
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "runtime_binary": str(binary),
                "runtime_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                "origin": "http://192.0.2.1:11435",
            }
        )
    )
    monkeypatch.setattr(
        provider.subprocess,
        "Popen",
        lambda *_a, **_kw: pytest.fail("nonlocal provider must not start"),
    )
    with pytest.raises(ValueError, match="loopback"):
        provider.manage(profile, "start")


@pytest.mark.parametrize("unsafe", ["directory", "binary", "profile", "log-link"])
def test_provider_rejects_peer_replaceable_inputs_and_log_aliases(tmp_path, monkeypatch, unsafe):
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    binary = root / "ollama"
    binary.write_bytes(b"synthetic provider")
    profile = root / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "runtime_binary": str(binary),
                "runtime_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                "origin": "http://127.0.0.1:11435",
            }
        )
    )
    victim = tmp_path / "unrelated"
    victim.write_bytes(b"unchanged")
    before = victim.stat().st_mode
    if unsafe == "directory":
        root.chmod(0o777)
    elif unsafe == "binary":
        binary.chmod(0o666)
    elif unsafe == "profile":
        profile.chmod(0o666)
    else:
        (root / "provider.log").symlink_to(victim)
    monkeypatch.setattr(provider.socket, "socket", lambda *_args: Mock())
    monkeypatch.setattr(
        provider.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("unsafe provider started")
    )
    with pytest.raises((ValueError, PermissionError)):
        provider.manage(profile, "start")
    assert victim.read_bytes() == b"unchanged"
    assert victim.stat().st_mode == before
    assert not (root / "provider-process.json").exists()
