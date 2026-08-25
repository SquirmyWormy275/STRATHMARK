from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path


def test_installed_wheel_contains_and_verifies_distinct_v3_contract(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    dist = tmp_path / "dist"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(dist),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    wheels = tuple(dist.glob("*.whl"))
    assert len(wheels) == 1
    wheel = wheels[0]
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert "strathmark/v3/contracts/v3_consumer.openapi.json" in names
    assert "strathmark/v3/contracts/v3_consumer.openapi.sha256" in names
    assert "strathmark/contracts/shadow_consumer_v1.openapi.json" in names

    installed_root = tmp_path / "installed"
    installation = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--disable-pip-version-check",
            "--target",
            str(installed_root),
            str(wheel),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert installation.returncode == 0, installation.stdout + installation.stderr

    probe = r"""
import sys
sys.path.insert(0, sys.argv[1])
from strathmark.consumer_contract import load_shadow_consumer_contract
from strathmark.v3.api.app import create_v3_app
from strathmark.v3.api.auth import InMemoryCredentialSecretStore, ServiceCredentialRegistry
from strathmark.v3.consumer_contract import (
    EXPECTED_V3_CONSUMER_PATHS,
    load_v3_consumer_contract,
    v3_consumer_contract_digest,
)
from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore
v3 = load_v3_consumer_contract()
assert set(v3["paths"]) == EXPECTED_V3_CONSUMER_PATHS
assert all(path.startswith("/v3/") for path in v3["paths"])
assert len(v3_consumer_contract_digest()) == 64
assert all(not path.startswith("/v3/") for path in load_shadow_consumer_contract()["paths"])
registry = ServiceCredentialRegistry(SQLiteEventStore(sys.argv[2]), InMemoryCredentialSecretStore())
registry.bootstrap_offline(
    principal_id="actor:installed-smoke",
    listener_stopped=True,
    credential="smv3.installed-key.installed-secret-1234567890",
)
app = create_v3_app(gateway=object(), credentials=registry)
assert app.openapi() == v3
print("installed-v3-contract-ok")
"""
    installed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            probe,
            str(installed_root),
            str(tmp_path / "installed-smoke.sqlite3"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert installed.stdout.strip() == "installed-v3-contract-ok"
