"""Offline tests for the reproducible Prediction V2 release gate."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.validate_v2 import load_benchmark_manifest, verify_release, verify_source_checksum
from scripts.verify_v2_golden import build_golden, verify_golden

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "benchmarks" / "prediction_v2_report.json"
PRELOCK = ROOT / "benchmarks" / "prediction_v2_prelock.json"
ARTIFACT = ROOT / "strathmark" / "models" / "prediction_v2_core.json"
MANIFEST = ROOT / "benchmarks" / "prediction_v2_manifest.json"
ATTESTATION = ROOT / "benchmarks" / "prediction_v2_release_attestation.json"
GOLDEN = ROOT / "benchmarks" / "prediction_v2_golden.json"
SOURCE = ROOT / "woodchopping_clean.xlsx"


def test_manifest_loader_rejects_missing_locked_contract(tmp_path: Path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"schema_version": "prediction-v2-benchmark/v1"}))

    with pytest.raises(ValueError, match="manifest fields"):
        load_benchmark_manifest(path)


def test_source_checksum_must_match_before_evaluation(tmp_path: Path):
    source = tmp_path / "source.xlsx"
    source.write_bytes(b"not the pinned workbook")
    actual = hashlib.sha256(source.read_bytes()).hexdigest()

    assert verify_source_checksum(source, actual) == actual
    with pytest.raises(ValueError, match="checksum mismatch"):
        verify_source_checksum(source, "0" * 64)


def test_published_release_verifies_without_reopening_locked_rows(monkeypatch):
    from scripts import validate_v2

    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (MANIFEST, PRELOCK, REPORT, ARTIFACT, ATTESTATION)
    }
    monkeypatch.setattr(
        validate_v2,
        "load_woodchopping_xlsx",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("verify-only must not parse or score workbook rows")
        ),
    )
    report = verify_release(
        REPORT,
        ARTIFACT,
        MANIFEST,
        SOURCE,
        prelock_path=PRELOCK,
        attestation_path=ATTESTATION,
    )
    assert report["promotion"]["core_promoted"] is True
    assert {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (MANIFEST, PRELOCK, REPORT, ARTIFACT, ATTESTATION)
    } == before


def test_release_verifier_requires_independent_attestation(tmp_path: Path):
    with pytest.raises(ValueError, match="attestation"):
        verify_release(
            REPORT,
            ARTIFACT,
            MANIFEST,
            SOURCE,
            prelock_path=PRELOCK,
            attestation_path=tmp_path / "missing-attestation.json",
        )


def test_release_verifier_rejects_coordinated_report_and_artifact_tampering(
    tmp_path: Path,
):
    artifact = tmp_path / "artifact.json"
    artifact.write_bytes(ARTIFACT.read_bytes() + b"\n")
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    report["artifact"]["sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    report["artifact"]["bytes"] = len(artifact.read_bytes())
    tampered_report = tmp_path / "report.json"
    tampered_report.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="attestation digest mismatch"):
        verify_release(
            tampered_report,
            artifact,
            MANIFEST,
            SOURCE,
            prelock_path=PRELOCK,
            attestation_path=ATTESTATION,
        )


def test_prediction_v2_golden_matches_public_audit_output(tmp_path: Path):
    actual = build_golden(artifact_path=ARTIFACT, db_path=tmp_path / "golden.db")

    verify_golden(GOLDEN, actual)


def test_prediction_v2_golden_rejects_normalized_field_change(tmp_path: Path):
    actual = build_golden(artifact_path=ARTIFACT, db_path=tmp_path / "golden.db")
    changed = json.loads(json.dumps(actual))
    changed["marks"][0] += 1
    expected = tmp_path / "changed-golden.json"
    expected.write_text(json.dumps(changed), encoding="utf-8")

    with pytest.raises(ValueError, match="golden output mismatch"):
        verify_golden(expected, actual)


def test_release_verifier_rejects_manifest_contract_tampering(tmp_path: Path):
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest["core_gate"]["minimum_mae_relative_improvement"] = 0.5
    tampered = tmp_path / "manifest.json"
    tampered.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="manifest checksum"):
        verify_release(REPORT, ARTIFACT, tampered, SOURCE)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda report: report.update(algorithm_contract="changed"), "algorithm contract"),
        (
            lambda report: report["promotion"].update(core_promoted=False),
            "failed core gate",
        ),
        (
            lambda report: report["locked_test"]["core_gate"].update(promoted=False),
            "passing locked core gate",
        ),
    ],
)
def test_release_verifier_rejects_report_tampering(tmp_path: Path, mutate, message):
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    mutate(report)
    tampered = tmp_path / "report.json"
    tampered.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        verify_release(tampered, ARTIFACT, MANIFEST, SOURCE)


def test_release_verifier_rejects_artifact_tampering(tmp_path: Path):
    tampered = tmp_path / "artifact.json"
    tampered.write_bytes(ARTIFACT.read_bytes() + b" ")

    with pytest.raises(ValueError, match="artifact checksum"):
        verify_release(REPORT, tampered, MANIFEST, SOURCE)
