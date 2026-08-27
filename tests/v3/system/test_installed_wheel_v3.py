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
    assert wheel.name.startswith("strathmark-3.0.0rc1-")
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        entry_points_name = next(
            name for name in names if name.endswith(".dist-info/entry_points.txt")
        )
        entry_points = archive.read(entry_points_name).decode("utf-8")
    assert "strathmark/v3/contracts/v3_consumer.openapi.json" in names
    assert "strathmark/v3/contracts/v3_consumer.openapi.sha256" in names
    assert "strathmark/v3/contracts/windows_capacity_manifest.json" in names
    assert "strathmark/v3/contracts/v3_release_attestation.json" not in names
    assert "strathmark/v3/contracts/v3-release.lock" in names
    assert "strathmark/contracts/shadow_consumer_v1.openapi.json" in names
    assert "strathmark/v3/factory/evaluator_cli.py" in names
    assert (
        "strathmark-v3-factory-evaluator = strathmark.v3.factory.evaluator_cli:main" in entry_points
    )

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
from strathmark.v3.application.cutover import verify_windows_capacity_manifest
from strathmark.v3.application.operations import (
    FieldDisposition,
    RaceDayField,
    RoundStage,
    verify_race_day_replay,
)
from importlib.resources import files
import json
import re
from importlib.metadata import distribution, version
import strathmark
v3 = load_v3_consumer_contract()
assert strathmark.__version__ == "3.0.0rc1"
assert version("strathmark") == "3.0.0rc1"
console_scripts = {
    item.name: item.value
    for item in distribution("strathmark").entry_points
    if item.group == "console_scripts"
}
assert console_scripts["strathmark-v3-factory-evaluator"] == (
    "strathmark.v3.factory.evaluator_cli:main"
)
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
capacity = json.loads(
    files("strathmark.v3.contracts").joinpath("windows_capacity_manifest.json").read_text("utf-8")
)
assert verify_windows_capacity_manifest(capacity)["candidate_tier"] == "rehearsal"
lock_lines = files("strathmark.v3.contracts").joinpath("v3-release.lock").read_text("utf-8").splitlines()
assert "cryptography==46.0.5" in lock_lines
assert "fastapi==0.135.1" in lock_lines
for locked in lock_lines:
    locked = locked.strip()
    if not locked or locked.startswith("#"):
        continue
    name, expected = locked.split("==")
    assert re.sub(r"[-_.]+", "-", name).lower()
    assert version(name) == expected
field = lambda name, stage, epoch, entrants, winner, offset, call_delay=0: RaceDayField(
    name,
    stage,
    epoch,
    entrants,
    tuple((entrant, index + 3) for index, entrant in enumerate(entrants)),
    (winner, *(entrant for entrant in entrants if entrant != winner)),
    winner,
    1000,
    100,
    FieldDisposition.PREDICTIVE,
    "a" * 64,
    offset,
    call_delay,
)
report = verify_race_day_replay((
    field("field:h1", RoundStage.HEAT, 1, ("c:a", "c:b"), "c:a", 0),
    field("field:h2", RoundStage.HEAT, 1, ("c:c", "c:d"), "c:c", 600000),
    field("field:q", RoundStage.QUARTER_FINAL, 2, ("c:a", "c:c"), "c:c", 900000),
    field("field:s", RoundStage.SEMI_FINAL, 3, ("c:c", "c:e"), "c:e", 1200000),
    field("field:d", RoundStage.DIVISIONAL_FINAL, 4, ("c:e", "c:f"), "c:e", 1500000),
    field("field:g", RoundStage.GRAND_FINAL, 5, ("c:e", "c:g"), "c:g", 1800000, 300000),
))
assert report.field_count == 6
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
