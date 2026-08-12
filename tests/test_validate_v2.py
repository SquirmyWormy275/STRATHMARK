"""Offline tests for the reproducible Prediction V2 release gate."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.validate_v2 import load_benchmark_manifest, verify_source_checksum


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
