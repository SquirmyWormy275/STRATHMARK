"""Reproducible, temporal Prediction Engine V2 release gate.

The default command verifies already-published evidence. The two evaluation
phases are deliberately separate: ``--prepare`` freezes selection and
calibration without scoring the locked role, while ``--open-locked-test``
requires that frozen pre-lock record and writes the final report exactly once.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import date
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from strathmark.features import (
    CANONICALIZATION_VERSION,
    build_prior_evidence,
)
from strathmark.loader import load_woodchopping_xlsx
from strathmark.prediction_v2 import ChronologicalCalibrator, PredictionV2Model, history_band
from strathmark.validation import (
    chronological_backtest,
    evaluate_core_promotion,
    fit_chronological_calibration,
    partition_benchmark_roles,
    strict_incumbent_backtest,
)

BENCHMARK_SCHEMA = "prediction-v2-benchmark/v1"
PRELOCK_SCHEMA = "prediction-v2-prelock/v1"
REPORT_SCHEMA = "prediction-v2-validation-report/v1"
ATTESTATION_SCHEMA = "prediction-v2-release-attestation/v1"
ALGORITHM_CONTRACT = "prediction-v2-core-fixed-20260811"
DEFAULT_PRELOCK_PATH = Path("benchmarks/prediction_v2_prelock.json")
DEFAULT_ATTESTATION_PATH = Path("benchmarks/prediction_v2_release_attestation.json")
REQUIRED_ATTESTATION_DIGESTS = {
    "manifest_sha256",
    "prelock_sha256",
    "report_sha256",
    "artifact_sha256",
}
REQUIRED_MANIFEST_FIELDS = {
    "schema_version",
    "source",
    "source_sha256",
    "canonicalization_version",
    "events",
    "eligibility",
    "roles",
    "observed_role_counts_before_modeling",
    "primary_metric",
    "secondary_metrics",
    "minimum_counts",
    "core_gate",
    "residual_gate",
    "locked_test_policy",
}


def load_benchmark_manifest(path: str | Path) -> dict[str, Any]:
    """Load and structurally validate the immutable benchmark manifest."""

    manifest_path = Path(path)
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("benchmark manifest is not readable JSON") from exc
    if not isinstance(value, dict) or set(value) != REQUIRED_MANIFEST_FIELDS:
        raise ValueError("benchmark manifest fields do not match the locked contract")
    if value["schema_version"] != BENCHMARK_SCHEMA:
        raise ValueError("benchmark manifest schema is incompatible")
    if value["canonicalization_version"] != "v2.0.0":
        raise ValueError("benchmark canonicalization version is incompatible")
    if value["events"] != ["SB", "UH"]:
        raise ValueError("benchmark event allowlist is incompatible")
    return value


def load_release_attestation(path: str | Path) -> dict[str, Any]:
    """Load the separately reviewed fixed-digest release trust anchor."""

    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("release attestation is not readable JSON") from exc
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "governance_policy",
        "digests",
    }:
        raise ValueError("release attestation fields do not match the fixed contract")
    if value["schema_version"] != ATTESTATION_SCHEMA:
        raise ValueError("release attestation schema is incompatible")
    digests = value["digests"]
    if not isinstance(digests, dict) or set(digests) != REQUIRED_ATTESTATION_DIGESTS:
        raise ValueError("release attestation digest fields do not match the fixed contract")
    for digest in digests.values():
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("release attestation contains an invalid SHA-256 digest")
    return value


def verify_source_checksum(path: str | Path, expected_sha256: str) -> str:
    """Verify source bytes before loading or evaluating any workbook row."""

    source = Path(path)
    digest = hashlib.sha256()
    try:
        with source.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise ValueError(f"benchmark source is not readable: {source}") from exc
    actual = digest.hexdigest()
    if actual != str(expected_sha256).lower():
        raise ValueError(
            f"benchmark source checksum mismatch: expected {expected_sha256}, got {actual}"
        )
    return actual


def load_benchmark_evidence(
    source_path: str | Path,
    manifest: Mapping[str, Any],
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame], dict[str, Any]]:
    """Load the pinned workbook into the canonical allowlisted evidence frame."""

    source_checksum = verify_source_checksum(source_path, manifest["source_sha256"])
    wood_df, competitor_df, results_df = load_woodchopping_xlsx(str(source_path))
    identities = competitor_df[["CompetitorID", "Gender"]].copy()
    identities["CompetitorID"] = identities["CompetitorID"].astype(str)
    if identities["CompetitorID"].duplicated().any():
        raise ValueError("competitor metadata must be one-to-one by CompetitorID")
    enriched = results_df.copy()
    enriched["CompetitorID"] = enriched["CompetitorID"].astype(str)
    enriched = enriched.merge(identities, on="CompetitorID", how="left", validate="many_to_one")
    locked_end = date.fromisoformat(manifest["roles"]["locked_test"]["end_exclusive"])
    prior = build_prior_evidence(enriched, locked_end, wood_df=wood_df)
    roles = partition_benchmark_roles(prior.rows, manifest)
    expected_counts = manifest["observed_role_counts_before_modeling"]
    actual_counts = {name: len(frame) for name, frame in roles.items()}
    actual_counts["undated_excluded"] = int(prior.diagnostics.excluded_by_reason.get("undated", 0))
    if actual_counts != expected_counts:
        raise ValueError(
            f"benchmark role counts changed: expected {expected_counts}, got {actual_counts}"
        )
    diagnostics = {
        "source_sha256": source_checksum,
        "canonical_builder_version": CANONICALIZATION_VERSION,
        "role_counts": actual_counts,
        "exclusions": dict(prior.diagnostics.excluded_by_reason),
    }
    return prior.rows, roles, diagnostics


def prepare_prelock(
    evidence: pd.DataFrame,
    manifest: Mapping[str, Any],
    *,
    source_diagnostics: Mapping[str, Any],
    manifest_checksum: str,
) -> dict[str, Any]:
    """Freeze fixed selection/calibration evidence without scoring locked rows."""

    selection_start, selection_end = _role_dates(manifest, "selection")
    calibration_start, calibration_end = _role_dates(manifest, "calibration")
    selection_core = chronological_backtest(
        evidence,
        target_start=selection_start,
        target_end_exclusive=selection_end,
    )
    selection_incumbent = strict_incumbent_backtest(
        evidence,
        target_start=selection_start,
        target_end_exclusive=selection_end,
    )
    _assert_common_targets(selection_core.predictions, selection_incumbent.predictions)

    calibration_report = chronological_backtest(
        evidence,
        target_start=calibration_start,
        target_end_exclusive=calibration_end,
    )
    calibrator = fit_chronological_calibration(
        calibration_report.predictions,
        version="prediction-v2-calibration-2025h1",
    )
    minimums = manifest["minimum_counts"]
    return {
        "schema_version": PRELOCK_SCHEMA,
        "algorithm_contract": ALGORITHM_CONTRACT,
        "source_sha256": source_diagnostics["source_sha256"],
        "manifest_sha256": manifest_checksum,
        "role_counts": source_diagnostics["role_counts"],
        "selection": {
            "core": _metric_bundle(selection_core.predictions, minimums),
            "incumbent": _metric_bundle(selection_incumbent.predictions, minimums),
        },
        "calibration": {
            "metrics_before_conformal": _metric_bundle(calibration_report.predictions, minimums),
            "calibrator": calibrator.to_dict(),
        },
        "residual": {
            "promoted": False,
            "status": "inactive_no_prelocked_candidate",
            "reason": "No optional residual candidate was frozen before the locked test.",
        },
        "tuning_frozen": True,
        "locked_test_opened": False,
    }


def open_locked_test_once(
    evidence: pd.DataFrame,
    manifest: Mapping[str, Any],
    prelock: Mapping[str, Any],
    *,
    source_diagnostics: Mapping[str, Any],
    manifest_checksum: str,
) -> tuple[dict[str, Any], PredictionV2Model | None]:
    """Score the locked role using only the previously frozen contract."""

    _validate_prelock(prelock, source_diagnostics, manifest_checksum)
    calibrator = ChronologicalCalibrator.from_dict(prelock["calibration"]["calibrator"])
    locked_start, locked_end = _role_dates(manifest, "locked_test")
    core = chronological_backtest(
        evidence,
        target_start=locked_start,
        target_end_exclusive=locked_end,
        calibration=calibrator,
        model_version="prediction-v2-locked-fold",
    )
    incumbent = strict_incumbent_backtest(
        evidence,
        target_start=locked_start,
        target_end_exclusive=locked_end,
    )
    _assert_common_targets(core.predictions, incumbent.predictions)
    gate = evaluate_core_promotion(
        core.metrics,
        incumbent.metrics,
        minimum_rows=int(manifest["minimum_counts"]["global"]),
        minimum_mae_relative_improvement=float(
            manifest["core_gate"]["minimum_mae_relative_improvement"]
        ),
        maximum_rmse_relative_worsening=float(
            manifest["core_gate"]["maximum_rmse_relative_worsening"]
        ),
    )
    report = {
        "schema_version": REPORT_SCHEMA,
        "algorithm_contract": ALGORITHM_CONTRACT,
        "source_sha256": source_diagnostics["source_sha256"],
        "manifest_sha256": manifest_checksum,
        "canonical_builder_version": source_diagnostics["canonical_builder_version"],
        "role_counts": source_diagnostics["role_counts"],
        "selection": prelock["selection"],
        "calibration": prelock["calibration"],
        "locked_test": {
            "opened_once_after_freeze": True,
            "window": manifest["roles"]["locked_test"],
            "core": _metric_bundle(core.predictions, manifest["minimum_counts"]),
            "incumbent": _metric_bundle(incumbent.predictions, manifest["minimum_counts"]),
            "core_gate": gate,
        },
        "residual": prelock["residual"],
        "promotion": {
            "core_promoted": bool(gate["promoted"]),
            "residual_promoted": False,
            "activation_is_human_driven": True,
        },
    }
    if not gate["promoted"]:
        report["artifact"] = {
            "packaged": False,
            "reason": "locked_core_gate_failed",
        }
        return report, None

    validation_metrics = {
        "locked_count": float(core.metrics["count"]),
        "locked_mae_seconds": float(core.metrics["mae"]),
        "locked_rmse_seconds": float(core.metrics["rmse"]),
        "locked_median_absolute_error_seconds": float(core.metrics["median_absolute_error"]),
        "incumbent_mae_seconds": float(incumbent.metrics["mae"]),
        "incumbent_rmse_seconds": float(incumbent.metrics["rmse"]),
        "mae_relative_improvement": float(gate["mae_relative_improvement"]),
        "rmse_relative_worsening": float(gate["rmse_relative_worsening"]),
    }
    final_model = PredictionV2Model.fit(
        evidence,
        training_cutoff=locked_end,
        model_version="prediction-v2-core-20260207",
        source_checksum=source_diagnostics["source_sha256"],
        validation_metrics=validation_metrics,
    ).with_calibration(calibrator)
    return report, final_model


def write_artifact(model: PredictionV2Model, path: str | Path) -> dict[str, Any]:
    """Write and independently reload a safe JSON core artifact."""

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = model.to_json()
    output.write_text(encoded, encoding="utf-8")
    raw = output.read_bytes()
    restored = PredictionV2Model.from_json(raw)
    if restored != model:
        raise ValueError("written prediction artifact failed deterministic round trip")
    return {
        "packaged": True,
        "path": output.as_posix(),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "model_version": restored.model_version,
        "engine_version": restored.engine_version,
        "training_cutoff_exclusive": restored.training_cutoff.isoformat(),
        "evidence_max_date": restored.evidence_max_date.isoformat(),
        "calibration_version": restored.calibration.version,
        "calibration_max_evidence_date": (
            restored.calibration.max_evidence_date.isoformat()
            if restored.calibration.max_evidence_date
            else None
        ),
    }


def verify_release(
    report_path: str | Path,
    artifact_path: str | Path,
    manifest_path: str | Path,
    source_path: str | Path,
    *,
    prelock_path: str | Path = DEFAULT_PRELOCK_PATH,
    attestation_path: str | Path = DEFAULT_ATTESTATION_PATH,
) -> dict[str, Any]:
    """Validate published report/artifact integrity without reopening test rows."""

    attestation = load_release_attestation(attestation_path)
    manifest = load_benchmark_manifest(manifest_path)
    verify_source_checksum(source_path, manifest["source_sha256"])
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    if report.get("schema_version") != REPORT_SCHEMA:
        raise ValueError("validation report schema is incompatible")
    if report.get("algorithm_contract") != ALGORITHM_CONTRACT:
        raise ValueError("validation report algorithm contract is incompatible")
    if report.get("source_sha256") != manifest["source_sha256"]:
        raise ValueError("validation report source checksum is incompatible")
    if report.get("manifest_sha256") != _file_sha256(manifest_path):
        raise ValueError("validation report manifest checksum is incompatible")
    if report.get("canonical_builder_version") != CANONICALIZATION_VERSION:
        raise ValueError("validation report canonical builder is incompatible")
    if report.get("role_counts") != manifest["observed_role_counts_before_modeling"]:
        raise ValueError("validation report role counts are incompatible")
    artifact_record = report.get("artifact", {})
    if not report.get("promotion", {}).get("core_promoted"):
        if artifact_record.get("packaged"):
            raise ValueError("failed core gate must not have a packaged artifact")
        _verify_release_attestation(
            attestation,
            manifest_path=manifest_path,
            prelock_path=prelock_path,
            report_path=report_path,
            artifact_path=artifact_path,
        )
        return report
    artifact = Path(artifact_path)
    raw = artifact.read_bytes()
    if hashlib.sha256(raw).hexdigest() != artifact_record.get("sha256"):
        raise ValueError("packaged artifact checksum does not match validation report")
    model = PredictionV2Model.from_json(raw)
    if model.source_checksum != manifest["source_sha256"]:
        raise ValueError("packaged artifact source checksum is incompatible")
    locked = report.get("locked_test", {})
    if locked.get("opened_once_after_freeze") is not True:
        raise ValueError("validation report does not prove the locked test was opened after freeze")
    if locked.get("window") != manifest["roles"]["locked_test"]:
        raise ValueError("validation report locked window is incompatible")
    gate = locked.get("core_gate", {})
    if not bool(gate.get("promoted")):
        raise ValueError("packaged artifact lacks a passing locked core gate")
    if report.get("promotion", {}).get("core_promoted") is not True:
        raise ValueError("validation report promotion state is inconsistent")
    core = locked.get("core", {}).get("global", {})
    incumbent = locked.get("incumbent", {}).get("global", {})
    locked_count = int(manifest["observed_role_counts_before_modeling"]["locked_test"])
    if core.get("count") != locked_count or incumbent.get("count") != locked_count:
        raise ValueError("validation report locked row counts are incompatible")
    core_mae = float(core["mae_seconds"])
    incumbent_mae = float(incumbent["mae_seconds"])
    core_rmse = float(core["rmse_seconds"])
    incumbent_rmse = float(incumbent["rmse_seconds"])
    mae_improvement = (incumbent_mae - core_mae) / incumbent_mae
    rmse_worsening = (core_rmse - incumbent_rmse) / incumbent_rmse
    expected_promotion = mae_improvement >= float(
        manifest["core_gate"]["minimum_mae_relative_improvement"]
    ) and rmse_worsening <= float(manifest["core_gate"]["maximum_rmse_relative_worsening"])
    if not expected_promotion:
        raise ValueError("locked metrics do not satisfy the benchmark core gate")
    if not math.isclose(float(gate["mae_relative_improvement"]), mae_improvement, abs_tol=1e-12):
        raise ValueError("locked MAE promotion calculation is inconsistent")
    if not math.isclose(float(gate["rmse_relative_worsening"]), rmse_worsening, abs_tol=1e-12):
        raise ValueError("locked RMSE promotion calculation is inconsistent")
    expected_validation = {
        "locked_count": float(locked_count),
        "locked_mae_seconds": core_mae,
        "locked_rmse_seconds": core_rmse,
        "locked_median_absolute_error_seconds": float(core["median_absolute_error_seconds"]),
        "incumbent_mae_seconds": incumbent_mae,
        "incumbent_rmse_seconds": incumbent_rmse,
        "mae_relative_improvement": mae_improvement,
        "rmse_relative_worsening": rmse_worsening,
    }
    for name, expected in expected_validation.items():
        if not math.isclose(
            float(model.validation_metrics.get(name, math.nan)), expected, abs_tol=1e-12
        ):
            raise ValueError(f"packaged artifact validation metric {name} is inconsistent")
    artifact_metadata = {
        "model_version": model.model_version,
        "engine_version": model.engine_version,
        "training_cutoff_exclusive": model.training_cutoff.isoformat(),
        "evidence_max_date": model.evidence_max_date.isoformat(),
        "calibration_version": model.calibration.version,
        "calibration_max_evidence_date": (
            model.calibration.max_evidence_date.isoformat()
            if model.calibration.max_evidence_date
            else None
        ),
    }
    for name, expected in artifact_metadata.items():
        if artifact_record.get(name) != expected:
            raise ValueError(f"packaged artifact metadata {name} is inconsistent")
    if artifact_record.get("bytes") != len(raw):
        raise ValueError("packaged artifact byte count is inconsistent")
    _verify_release_attestation(
        attestation,
        manifest_path=manifest_path,
        prelock_path=prelock_path,
        report_path=report_path,
        artifact_path=artifact_path,
    )
    return report


def _verify_release_attestation(
    attestation: Mapping[str, Any],
    *,
    manifest_path: str | Path,
    prelock_path: str | Path,
    report_path: str | Path,
    artifact_path: str | Path,
) -> None:
    """Bind all persisted release inputs to the independent fixed digests."""

    paths = {
        "manifest": manifest_path,
        "prelock": prelock_path,
        "report": report_path,
        "artifact": artifact_path,
    }
    digests = attestation["digests"]
    for name, path in paths.items():
        if _file_sha256(path) != digests[f"{name}_sha256"]:
            raise ValueError(f"release attestation digest mismatch for {name}")


def _metric_bundle(
    predictions: pd.DataFrame,
    minimums: Mapping[str, Any],
) -> dict[str, Any]:
    global_metrics = _metrics(predictions)
    global_count = int(global_metrics["count"])
    global_metrics["claim_eligible"] = global_count >= int(minimums["global"])
    global_metrics["sample_label"] = (
        "global_claim_eligible"
        if global_metrics["claim_eligible"]
        else "insufficient_global_sample"
    )
    cohorts: dict[str, dict[str, Any]] = {"event": {}, "history_depth": {}}
    if not predictions.empty:
        for event, subset in predictions.groupby("event", sort=True):
            cohorts["event"][str(event)] = _labeled_cohort(subset, minimums)
        if "history_count" in predictions:
            bands = predictions["history_count"].map(history_band)
            for band in ("0", "1-3", "4+"):
                subset = predictions[bands == band]
                if not subset.empty:
                    cohorts["history_depth"][band] = _labeled_cohort(subset, minimums)
    return {"global": global_metrics, "cohorts": cohorts}


def _labeled_cohort(
    frame: pd.DataFrame,
    minimums: Mapping[str, Any],
) -> dict[str, Any]:
    metrics = _metrics(frame)
    metrics["claim_eligible"] = int(metrics["count"]) >= int(minimums["cohort"])
    metrics["sample_label"] = (
        "cohort_claim_eligible" if metrics["claim_eligible"] else "insufficient_cohort_sample"
    )
    return metrics


def _metrics(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {
            "count": 0,
            "mae_seconds": None,
            "rmse_seconds": None,
            "median_absolute_error_seconds": None,
            "coverage_90": None,
            "mean_interval_width_seconds": None,
        }
    actual = frame["actual_time"].to_numpy(dtype=float)
    predicted = frame["predicted_median"].to_numpy(dtype=float)
    errors = predicted - actual
    metrics: dict[str, Any] = {
        "count": len(frame),
        "mae_seconds": float(np.mean(np.abs(errors))),
        "rmse_seconds": float(math.sqrt(float(np.mean(np.square(errors))))),
        "median_absolute_error_seconds": float(np.median(np.abs(errors))),
        "coverage_90": None,
        "mean_interval_width_seconds": None,
    }
    if {"core_lower", "core_upper"}.issubset(frame.columns):
        lower = frame["core_lower"].to_numpy(dtype=float)
        upper = frame["core_upper"].to_numpy(dtype=float)
        metrics["coverage_90"] = float(np.mean((lower <= actual) & (actual <= upper)))
        metrics["mean_interval_width_seconds"] = float(np.mean(upper - lower))
    return metrics


def _assert_common_targets(core: pd.DataFrame, incumbent: pd.DataFrame) -> None:
    if len(core) != len(incumbent) or core.empty:
        raise ValueError("core and incumbent must score the same non-empty target rows")
    keys = ["competitor_id", "event", "result_date", "actual_time"]
    if not core[keys].reset_index(drop=True).equals(incumbent[keys].reset_index(drop=True)):
        raise ValueError("core and incumbent target rows differ")


def _role_dates(manifest: Mapping[str, Any], role: str) -> tuple[date, date]:
    spec = manifest["roles"][role]
    return date.fromisoformat(spec["start"]), date.fromisoformat(spec["end_exclusive"])


def _validate_prelock(
    prelock: Mapping[str, Any],
    diagnostics: Mapping[str, Any],
    manifest_checksum: str,
) -> None:
    if prelock.get("schema_version") != PRELOCK_SCHEMA:
        raise ValueError("pre-lock report schema is incompatible")
    if prelock.get("algorithm_contract") != ALGORITHM_CONTRACT:
        raise ValueError("pre-lock algorithm contract is incompatible")
    if prelock.get("source_sha256") != diagnostics["source_sha256"]:
        raise ValueError("pre-lock source checksum is incompatible")
    if prelock.get("manifest_sha256") != manifest_checksum:
        raise ValueError("pre-lock manifest checksum is incompatible")
    if prelock.get("tuning_frozen") is not True or prelock.get("locked_test_opened") is not False:
        raise ValueError("pre-lock report does not prove a frozen unopened test")


def _file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write_json(path: str | Path, value: Mapping[str, Any]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _write_markdown(path: str | Path, report: Mapping[str, Any]) -> None:
    locked = report["locked_test"]
    core = locked["core"]["global"]
    incumbent = locked["incumbent"]["global"]
    gate = locked["core_gate"]
    status = "PASSED" if gate["promoted"] else "FAILED"
    lines = [
        "# Prediction Engine V2 Locked Validation",
        "",
        f"Core release gate: **{status}**",
        "",
        "The workbook checksum was verified before evaluation. Selection and calibration were "
        "frozen before the locked 2025-07-01 through 2026-02-06 role was scored.",
        "",
        "| Metric | V2 core | Strict incumbent |",
        "| --- | ---: | ---: |",
        f"| Rows | {core['count']} | {incumbent['count']} |",
        f"| MAE (seconds) | {core['mae_seconds']:.4f} | {incumbent['mae_seconds']:.4f} |",
        f"| RMSE (seconds) | {core['rmse_seconds']:.4f} | {incumbent['rmse_seconds']:.4f} |",
        f"| Median absolute error (seconds) | {core['median_absolute_error_seconds']:.4f} | {incumbent['median_absolute_error_seconds']:.4f} |",
        f"| 90% interval coverage | {core['coverage_90']:.3f} | n/a |",
        f"| Mean interval width (seconds) | {core['mean_interval_width_seconds']:.4f} | n/a |",
        "",
        f"MAE relative improvement: {gate['mae_relative_improvement']:.3%}.",
        f"RMSE relative worsening: {gate['rmse_relative_worsening']:.3%}.",
        "",
        "The optional residual learner is inactive because no candidate was frozen before the "
        "locked test. Cohort measurements and minimum-sample labels are retained in the JSON report.",
    ]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("woodchopping_clean.xlsx"))
    parser.add_argument(
        "--manifest", type=Path, default=Path("benchmarks/prediction_v2_manifest.json")
    )
    parser.add_argument("--prelock-report", type=Path, default=DEFAULT_PRELOCK_PATH)
    parser.add_argument("--report", type=Path, default=Path("benchmarks/prediction_v2_report.json"))
    parser.add_argument(
        "--markdown-report", type=Path, default=Path("benchmarks/prediction_v2_report.md")
    )
    parser.add_argument(
        "--artifact", type=Path, default=Path("strathmark/models/prediction_v2_core.json")
    )
    parser.add_argument("--attestation", type=Path, default=DEFAULT_ATTESTATION_PATH)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--open-locked-test", action="store_true")
    mode.add_argument("--verify-release", action="store_true")
    args = parser.parse_args(argv)

    try:
        manifest = load_benchmark_manifest(args.manifest)
        if not args.prepare and not args.open_locked_test:
            verify_release(
                args.report,
                args.artifact,
                args.manifest,
                args.source,
                prelock_path=args.prelock_report,
                attestation_path=args.attestation,
            )
            print("Prediction V2 release evidence verified without reopening locked rows.")
            return 0

        evidence, _, diagnostics = load_benchmark_evidence(args.source, manifest)
        manifest_checksum = _file_sha256(args.manifest)
        if args.prepare:
            if args.report.exists():
                raise ValueError("final locked report already exists; refusing to prepare again")
            prelock = prepare_prelock(
                evidence,
                manifest,
                source_diagnostics=diagnostics,
                manifest_checksum=manifest_checksum,
            )
            _write_json(args.prelock_report, prelock)
            print(f"Pre-lock selection/calibration frozen: {args.prelock_report}")
            return 0

        if args.report.exists():
            raise ValueError("locked report already exists; refusing to reopen locked test")
        prelock = json.loads(args.prelock_report.read_text(encoding="utf-8"))
        report, model = open_locked_test_once(
            evidence,
            manifest,
            prelock,
            source_diagnostics=diagnostics,
            manifest_checksum=manifest_checksum,
        )
        if model is not None:
            report["artifact"] = write_artifact(model, args.artifact)
        _write_json(args.report, report)
        _write_markdown(args.markdown_report, report)
        gate = report["locked_test"]["core_gate"]
        print(json.dumps(gate, indent=2, sort_keys=True))
        return 0 if gate["promoted"] else 2
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"Prediction V2 validation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
