"""Frozen component calibration over unchanged native, mark-free forecasts."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import stat
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

SCHEMA = "strath-accuracy-preview-v1"
PURPOSE = "accuracy_preview_only"
MANIFEST_FIELDS = {
    "schema_version",
    "purpose",
    "eligible_for_official_use",
    "baseline_bundle_digest",
    "formula_digest",
    "implementation_digest",
    "files",
    "provenance",
    "artifact_digest",
}
CALIBRATION_FIELDS = {
    "supported_scale",
    "supported_rows",
    "supported_groups",
    "cold_rows",
    "cold_groups",
    "cold_formula",
    "cold_ml",
}
LEVELS = ("0.05", "0.1", "0.25", "0.5", "0.75", "0.9", "0.95")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path, maximum: int = 1_000_000):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise ValueError("preview input must be a bounded regular file")
    return json.loads(path.read_bytes())


def write_new_json(path: Path, value) -> None:
    payload = json.dumps(value, indent=2, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path, "x", encoding="utf-8", opener=lambda p, flags: os.open(p, flags, 0o600)) as out:
        out.write(payload)
        out.flush()
        os.fsync(out.fileno())


def snapshot(source: Path, destination: Path) -> dict:
    """Copy and verify bytes; never write or relabel the source workbook."""
    if source.is_symlink() or not source.is_file() or source.stat().st_size > 100_000_000:
        raise ValueError("source must be a regular workbook smaller than 100 MB")
    if source.resolve() == destination.resolve():
        raise ValueError("snapshot must have a separate path")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    original = sha(source)
    try:
        with (
            source.open("rb") as incoming,
            open(destination, "xb", opener=lambda p, flags: os.open(p, flags, 0o600)) as outgoing,
        ):
            shutil.copyfileobj(incoming, outgoing)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if sha(source) != original or sha(destination) != original:
            raise ValueError("workbook changed while copying")
        destination.chmod(0o400)
    except FileExistsError:
        raise
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return {"snapshot": str(destination.resolve()), "sha256": original, "read_only": True}


def readonly_workbook(path: Path) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 100_000_000:
        raise ValueError("use a regular isolated workbook smaller than 100 MB")
    if path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError("use snapshot to create a separate verified read-only workbook first")


def validate_calibration(value: dict) -> None:
    from strathmark.v3.assessors.ml import PITCalibrator

    if not isinstance(value, dict) or set(value) != CALIBRATION_FIELDS:
        raise ValueError("calibration fields differ from the frozen preview schema")
    scale = value["supported_scale"]
    if type(scale) not in (int, float) or not math.isfinite(scale) or scale <= 0:
        raise ValueError("supported scale must be finite and positive")
    for key, minimum in (
        ("supported_rows", 20),
        ("cold_rows", 20),
        ("supported_groups", 5),
        ("cold_groups", 5),
    ):
        if type(value[key]) is not int or value[key] < minimum:
            raise ValueError("frozen calibration has insufficient independent group support")
    for key in ("cold_formula", "cold_ml"):
        fitted = PITCalibrator.from_dict(value[key])
        if float(fitted.interval_log_radius) <= 0:
            raise ValueError("cold calibration requires its conservative positive interval floor")


@dataclass(frozen=True)
class Candidate:
    root: Path
    manifest: dict
    calibration: dict
    bundle: object


def load_candidate(root: Path) -> Candidate:
    if sys.version_info[:2] != (3, 13):
        raise ValueError("accuracy preview requires its designated Python 3.13 runtime")
    import catboost

    from strathmark.v3.contracts.canonical import canonical_digest
    from strathmark.v3.factory.ml_artifacts import load_ml_bundle
    from strathmark.v3.linux_forecasts import load_formula_manifest
    from strathmark.v3.runtime_identity import implementation_digest

    manifest = read_json(root / "manifest.json")
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_FIELDS:
        raise ValueError("candidate manifest differs from the closed preview schema")
    if (
        manifest["schema_version"] != SCHEMA
        or manifest["purpose"] != PURPOSE
        or manifest["eligible_for_official_use"] is not False
    ):
        raise ValueError("candidate cannot grant official or installed competition authority")
    body = {k: v for k, v in manifest.items() if k != "artifact_digest"}
    if canonical_digest(body) != manifest["artifact_digest"]:
        raise ValueError("candidate manifest digest mismatch")
    files = manifest["files"]
    if not isinstance(files, dict) or not files or len(files) > 100:
        raise ValueError("candidate file list is invalid")
    actual = {
        p.relative_to(root).as_posix()
        for directory in (root / "components",)
        for p in directory.rglob("*")
        if p.is_file()
    }
    actual.add("calibration.json")
    if actual != set(files):
        raise ValueError("candidate file coverage changed")
    for relative, digest in files.items():
        path = root / relative
        if (
            Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or any(p.is_symlink() for p in (path, *path.parents))
            or not path.is_file()
            or path.stat().st_size > 100_000_000
            or sha(path) != digest
        ):
            raise ValueError("candidate component bytes changed")
    if implementation_digest() != manifest["implementation_digest"]:
        raise ValueError("native implementation differs from the frozen candidate")
    calibration = read_json(root / "calibration.json")
    validate_calibration(calibration)
    bundle = load_ml_bundle(
        root / "components/ml-bundle",
        installed_catboost_version=catboost.__version__,
        installed_python_abi="cp313",
    )
    if bundle.digest != manifest["baseline_bundle_digest"] or bundle.specialist_models:
        raise ValueError("candidate requires its exact frozen universal-only ML bundle")
    from strathmark.v3.assessors.ml import PITCalibrator

    if float(PITCalibrator.from_dict(calibration["cold_ml"]).interval_log_radius) < float(
        bundle.calibrator.interval_log_radius
    ):
        raise ValueError("cold ML interval floor cannot be narrower than the frozen global floor")
    if load_formula_manifest(root / "components/ml-bundle").digest != manifest["formula_digest"]:
        raise ValueError("candidate Formula identity differs")
    return Candidate(root, manifest, calibration, bundle)


def apply_calibration(formula, ml, raw_ml_quantiles, history_depth: int, calibration: dict):
    """Preserve both original immutable distributions; calibrate copies separately."""
    from strathmark.v3.assessors.ml import PITCalibrator, build_positive_distribution
    from strathmark.v3.contracts.forecasts import (
        AssessorKind,
        PositiveTimeDistribution,
        QuantilePoint,
    )
    from strathmark.v3.domain.pooling import LinearPoolComponent, LinearPooledDistribution

    if type(history_depth) is not int or history_depth < 0:
        raise ValueError("prior same-event history depth must be a nonnegative integer")
    validate_calibration(calibration)
    if history_depth:

        def scaled(distribution):
            return PositiveTimeDistribution(
                tuple(
                    QuantilePoint(
                        q.probability,
                        min(600000, max(1, round(q.time_ms * calibration["supported_scale"]))),
                    )
                    for q in distribution.quantiles
                )
            )

        changed_formula, changed_ml = scaled(formula), scaled(ml)
    else:
        raw_formula = tuple(math.log(formula._at_probability(Decimal(p)) / 1000) for p in LEVELS)
        changed_formula = build_positive_distribution(
            raw_formula, PITCalibrator.from_dict(calibration["cold_formula"])
        )
        changed_ml = build_positive_distribution(
            raw_ml_quantiles, PITCalibrator.from_dict(calibration["cold_ml"])
        )
    pooled = LinearPooledDistribution(
        (
            LinearPoolComponent(AssessorKind.FORMULA, "0.5", changed_formula),
            LinearPoolComponent(AssessorKind.ML, "0.5", changed_ml),
        )
    )
    return changed_formula, changed_ml, pooled


def forecast(
    candidate: Candidate, workbook: Path, cutoff: str, competitor_ids: list[str], context: dict
):
    from strathmark.v3.assessors.ml import _isotonic_non_decreasing, build_positive_distribution
    from strathmark.v3.contracts.evidence import TargetContext
    from strathmark.v3.contracts.forecasts import PositiveTimeDistribution
    from strathmark.v3.factory.ml_training import _features, predict_model_log_quantiles
    from strathmark.v3.factory.workbook_history import load_workbook_history
    from strathmark.v3.infrastructure.integrity import IntegrityTrustStore, P256EphemeralSigner
    from strathmark.v3.linux_forecasts import calculate

    readonly_workbook(workbook)
    history = load_workbook_history(workbook, cutoff_at_utc=cutoff)
    identifiers = dict(history.competitor_ids)
    if (
        not isinstance(competitor_ids, list)
        or not 1 <= len(competitor_ids) <= 128
        or len(set(competitor_ids)) != len(competitor_ids)
        or any(c not in identifiers for c in competitor_ids)
    ):
        raise ValueError("select distinct existing competitor IDs from this snapshot")
    target = TargetContext.from_dict(context)
    signer = P256EphemeralSigner.generate("integrity-key:accuracy-preview")
    baseline = calculate(
        bundle_root=candidate.root / "components/ml-bundle",
        signer=signer,
        trust=IntegrityTrustStore((signer.identity,)),
        scope_id="tournament:accuracy-preview",
        round_id="round:accuracy-preview",
        competitor_ids=competitor_ids,
        target_context=context,
        round_snapshot={
            "history_path": str(workbook),
            "cutoff_at_utc": cutoff,
            "history_sha256": history.source_sha256,
            "live_results": [],
            "frozen_at_utc": cutoff,
            "weights": {"formula": "0.5", "ml": "0.5"},
        },
    )
    if baseline["issued_mark"] is not False or "marks" in baseline:
        raise ValueError("native forecast returned field authority in a cutting-time preview")
    results = []
    for native in baseline["forecasts"]:
        personal = [
            o for o in history.observations if str(o.competitor_id) == native["competitor_id"]
        ]
        features = _features(target, personal, eligible_sequence=len(history.observations))
        normalized, _ = candidate.bundle.normalize_features(features)
        raw = _isotonic_non_decreasing(
            predict_model_log_quantiles(
                candidate.bundle.universal_model,
                [[normalized[n] for n in candidate.bundle.feature_names]],
            )
        )
        formula = PositiveTimeDistribution.from_dict(native["formula"]["forecast"]["distribution"])
        ml = PositiveTimeDistribution.from_dict(native["ml"]["forecast"]["distribution"])
        if build_positive_distribution(raw, candidate.bundle.calibrator).to_dict() != ml.to_dict():
            raise ValueError("reconstructed native ML differs; no approximate substitution allowed")
        changed_formula, changed_ml, pool = apply_calibration(
            formula, ml, raw, features["same_event_history_depth"], candidate.calibration
        )
        results.append(
            {
                "local_competitor_id": native["local_competitor_id"],
                "same_event_history_depth": features["same_event_history_depth"],
                "route": "supported"
                if features["same_event_history_depth"]
                else "first-appearance",
                "baseline_time_ms": native["predicted_time_ms"],
                "candidate_time_ms": pool.median_ms,
                "candidate_interval90_ms": pool.quantile_summary().central_interval("0.05", "0.95"),
                "candidate_formula": changed_formula.to_dict(),
                "candidate_ml": changed_ml.to_dict(),
            }
        )
    if sha(workbook) != history.source_sha256:
        raise ValueError("workbook changed during preview")
    return {
        "schema_version": SCHEMA,
        "purpose": PURPOSE,
        "issued_mark": False,
        "eligible_for_official_use": False,
        "candidate_digest": candidate.manifest["artifact_digest"],
        "cutoff_at_utc": cutoff,
        "history_sha256": history.source_sha256,
        "native_baseline": baseline,
        "forecasts": results,
    }
