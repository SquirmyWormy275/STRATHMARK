from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _run(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/verify_v3_release.py", *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_checked_in_rehearsal_is_exact_and_never_claims_authority_switch() -> None:
    root = Path(__file__).resolve().parents[3]
    completed = _run(root)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(completed.stdout)
    assert report["result"] == "passed"
    assert report["tier"] == "rehearsal"
    assert report["evidence_count"] == 11
    assert report["authority_changed"] is False

    production = _run(root, "--require-production")
    assert production.returncode == 2
    failure = json.loads(production.stderr)
    assert failure["reason"] == "production_attestation_required"
    assert failure["authority_changed"] is False


def test_verifier_fails_closed_on_capacity_tamper(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    capacity = json.loads(
        (root / "benchmarks/v3/windows_capacity_manifest.json").read_text("utf-8")
    )
    capacity["measured"]["field_assembly_p99_ms"] = 2_000
    tampered = tmp_path / "tampered-capacity.json"
    tampered.write_text(json.dumps(capacity), encoding="utf-8")

    completed = _run(root, "--capacity", str(tampered))
    assert completed.returncode == 2
    failure = json.loads(completed.stderr)
    assert "hard budget" in failure["reason"]
    assert failure["authority_changed"] is False


def test_replay_script_is_deterministic_and_reports_complete_progression() -> None:
    root = Path(__file__).resolve().parents[3]
    first = subprocess.run(
        [sys.executable, "scripts/replay_v3.py"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    second = subprocess.run(
        [sys.executable, "scripts/replay_v3.py"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout
    report = json.loads(first.stdout)
    assert report["result"] == "passed"
    assert report["race_day"]["stage_count"] == 5
    assert len(report["recovery"]["failures"]) == 10
