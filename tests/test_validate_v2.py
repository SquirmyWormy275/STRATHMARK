"""Offline tests for the reproducible Prediction V2 release gate."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.validate_v2 import load_benchmark_manifest, verify_release, verify_source_checksum

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "benchmarks" / "prediction_v2_report.json"
ARTIFACT = ROOT / "strathmark" / "models" / "prediction_v2_core.json"
MANIFEST = ROOT / "benchmarks" / "prediction_v2_manifest.json"
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


def test_published_release_verifies_without_reopening_locked_rows():
    report = verify_release(REPORT, ARTIFACT, MANIFEST, SOURCE)
    assert report["promotion"]["core_promoted"] is True


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
