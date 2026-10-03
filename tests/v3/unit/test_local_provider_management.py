import hashlib
import json
import sys

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
