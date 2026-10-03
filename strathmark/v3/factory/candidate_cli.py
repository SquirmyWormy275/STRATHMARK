"""Explicit development candidate training from an authorized workbook snapshot."""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
from types import SimpleNamespace

from strathmark.v3.assessors.ml import SpecialistGate
from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.evidence import EvidencePacket, ResultObservation
from strathmark.v3.contracts.identifiers import StableIdentifier
from strathmark.v3.factory.formula_training import PRIOR_POLICY, build_formula_candidate
from strathmark.v3.factory.ml_artifacts import (
    BUNDLE_METADATA_SCHEMA,
    DEPENDENCY_SCHEMA,
    FEATURE_SCHEMA,
    VOCABULARY_SCHEMA,
    export_catboost_json,
    load_ml_bundle,
    write_ml_bundle,
)
from strathmark.v3.factory.ml_training import (
    CATEGORICAL_FEATURES,
    FEATURE_NAMES,
    QUANTILE_LEVELS,
    MLAuthorityEnvironment,
    MLDataRole,
    _compose_ml_audit_authority,
    _compose_ml_candidate_authority,
    mean_pinball_loss,
    predict_model_log_quantiles,
)
from strathmark.v3.factory.workbook_history import load_workbook_history
from strathmark.v3.infrastructure.integrity import (
    IntegrityKeyIdentity,
    P256EphemeralSigner,
    SignedManifest,
    sign_manifest,
)
from strathmark.v3.runtime_identity import implementation_digest, verify_source_revision


def _fit_selected_specialist_gate(
    authority, training_rows, tuning_rows, specialists, settings, universal_oof
):
    predictions = universal_oof
    if specialists:
        # Universal-only tuning selects settings; specialist gating needs the
        # selected hierarchy's chronological, held-out predictions.
        predictions = authority.chronological_holdout_component_predictions(
            training_rows, tuning_rows, include_specialists=True, **settings
        )
    examples = authority.gate_examples_from_oof(predictions, tuning_rows)
    if examples and len({item.fold_id for item in examples}) >= 2:
        return (
            authority.fit_specialist_gate(examples),
            specialists,
            "fitted_from_grouped_tuning_oof",
        )
    return (
        SpecialistGate("0", (("log_history_depth", "0"), ("missing_fraction", "0"))),
        {},
        "insufficient_grouped_gate_evidence; universal_only",
    )


def _select_training_settings(authority, training_rows, tuning_rows):
    """Whole-tournament tuning only. Calibration and audit targets are inaccessible."""
    trials = []
    selected = None
    for depth, iterations, power, transform in product(
        (4, 6),
        (400, 1000),
        (0.0, 0.5, 1.0),
        ("event_history_residual_v1", "material_recent_residual_v1"),
    ):
        settings = {
            "iterations": iterations,
            "depth": depth,
            "learning_rate": 0.03,
            "seed": 20260823,
            "sample_weight_power": power,
            "target_transform": transform,
        }
        predictions = authority.chronological_holdout_component_predictions(
            training_rows, tuning_rows, include_specialists=False, **settings
        )
        targets = {row.row_id: float(row.target_log_seconds) for row in tuning_rows}
        errors = [
            abs(math.exp(item.universal_log_quantiles[3]) - math.exp(targets[item.row_id]))
            for item in predictions
        ]
        losses = [
            mean_pinball_loss(targets[item.row_id], item.universal_log_quantiles)
            for item in predictions
        ]
        trial = {
            "settings": settings,
            "row_count": len(errors),
            "mean_absolute_error_seconds": sum(errors) / len(errors),
            "mean_log_pinball_loss": sum(losses) / len(losses),
        }
        trials.append(trial)
        score = (trial["mean_absolute_error_seconds"], trial["mean_log_pinball_loss"])
        if selected is None or score < selected[0]:
            selected = (score, settings, predictions)
    return selected[1], selected[2], trials


def _build_candidate(payload: dict, output: Path) -> dict:
    """Builder receives only TRAIN/TUNE/CAL facts, never the workbook or audit rows."""
    import catboost

    source_commit = payload["source_commit"]
    cutoff_at_utc = payload["cutoff_at_utc"]
    training_end_year, tuning_end_year, calibration_end_year = payload["role_year_boundaries"]
    source_artifact_digest = verify_source_revision(
        Path(payload["source_repository"]), source_commit
    )
    if source_artifact_digest != payload["source_artifact_digest"]:
        raise ValueError("builder implementation differs from verified coordinator source")
    history = SimpleNamespace(
        observations=tuple(ResultObservation.from_dict(item) for item in payload["observations"]),
        source_sha256=payload["workbook_sha256"],
        excluded=payload["excluded"],
    )
    if any(int(item.occurred_at_utc[:4]) > calibration_end_year for item in history.observations):
        raise ValueError("builder refuses locked-audit observations")

    def role_of(observation):
        year = int(observation.occurred_at_utc[:4])
        return (
            MLDataRole.TRAINING
            if year <= training_end_year
            else MLDataRole.TUNING
            if year <= tuning_end_year
            else MLDataRole.CALIBRATION
            if year <= calibration_end_year
            else MLDataRole.LOCKED_AUDIT
        )

    assignments = {}
    grouped = defaultdict(list)
    for observation in history.observations:
        role = role_of(observation)
        tournament = str(observation.tournament_id)
        if assignments.setdefault(tournament, role) is not role:
            raise ValueError("whole recorded competition crosses role boundaries")
        grouped[(role, str(observation.competitor_id))].append(observation)
    if set(assignments.values()) != {
        MLDataRole.TRAINING,
        MLDataRole.TUNING,
        MLDataRole.CALIBRATION,
    }:
        raise ValueError("builder requires three disjoint candidate roles")
    generation = canonical_digest(
        {
            "workbook_sha256": history.source_sha256,
            "cutoff_at_utc": cutoff_at_utc,
            "role_year_boundaries": [training_end_year, tuning_end_year, calibration_end_year],
        }
    )
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    candidate_signer = P256EphemeralSigner.generate("integrity-key:candidate-builder")

    candidate_manifest = sign_manifest(
        "ml_role_manifest",
        {
            "schema_version": "strathmark-v3-ml-role-manifest-v3",
            "generation_digest": generation,
            "assignments": [
                {"tournament_id": key, "role": role.value}
                for key, role in sorted(assignments.items())
            ],
        },
        signer=candidate_signer,
        created_at=now,
    )
    authority = _compose_ml_candidate_authority(
        candidate_manifest,
        candidate_signer.identity,
        candidate_signer,
        environment=MLAuthorityEnvironment.DEVELOPMENT_CANDIDATE,
    )
    rows = {}
    for role in (MLDataRole.TRAINING, MLDataRole.TUNING, MLDataRole.CALIBRATION):
        eligible = defaultdict(list)
        for observation in history.observations:
            if list(MLDataRole).index(role_of(observation)) <= list(MLDataRole).index(role):
                eligible[str(observation.competitor_id)].append(observation)
        rows[role] = authority.build_development_causal_rows(role, _packets(eligible, generation))
    training_settings, gate_oof, tuning_trials = _select_training_settings(
        authority, rows[MLDataRole.TRAINING], rows[MLDataRole.TUNING]
    )
    universal, specialists, eligibility = authority.train_catboost_hierarchy(
        rows[MLDataRole.TRAINING], **training_settings
    )
    gate, specialists, gate_status = _fit_selected_specialist_gate(
        authority,
        rows[MLDataRole.TRAINING],
        rows[MLDataRole.TUNING],
        specialists,
        training_settings,
        gate_oof,
    )
    calibration_oof = authority.chronological_holdout_component_predictions(
        rows[MLDataRole.TRAINING],
        rows[MLDataRole.CALIBRATION],
        include_specialists=bool(specialists),
        **training_settings,
    )
    calibrator = authority.fit_pit_calibrator(rows[MLDataRole.CALIBRATION], calibration_oof, gate)
    output.mkdir(parents=True, mode=0o700)
    formula_manifest = build_formula_candidate(authority, rows[MLDataRole.TRAINING])
    (output / "formula_manifest.json").write_bytes(canonical_bytes(formula_manifest.to_dict()))
    model_bytes = export_catboost_json(universal, output / "universal-export.json")
    specialist_bytes = {
        key: export_catboost_json(
            model, output / f"specialist-{canonical_digest({'key': key})}.json"
        )
        for key, model in specialists.items()
    }
    vocabulary = {
        name: sorted(
            {"__other__", *(str(row.feature_dict[name]) for row in rows[MLDataRole.TRAINING])}
        )
        for name in CATEGORICAL_FEATURES
    }
    bundle_root = write_ml_bundle(
        output / "ml-bundle",
        universal_model_json=model_bytes,
        specialist_model_json=specialist_bytes,
        gate=gate,
        calibrator=calibrator,
        feature_schema={
            "schema_version": FEATURE_SCHEMA,
            "features": list(FEATURE_NAMES),
            "categorical": list(CATEGORICAL_FEATURES),
            "quantiles": list(QUANTILE_LEVELS),
        },
        category_vocabulary={"schema_version": VOCABULARY_SCHEMA, "values": vocabulary},
        dependency_lock={
            "schema_version": DEPENDENCY_SCHEMA,
            "catboost_version": catboost.__version__,
            "python_abi": "cp313",
        },
        bundle_metadata={
            "schema_version": BUNDLE_METADATA_SCHEMA,
            "code_revision": f"git:{source_commit};implementation:{source_artifact_digest}",
            "training_snapshot_digest": generation,
            "role_manifest_digest": authority.signed_manifest_digest,
            "gate_oof_digest": gate_oof._authorization_envelope.body_digest,
            "calibrator_source_digest": calibrator.source_digest,
            "taxonomy_version": "strathex:v1",
            "conversion_version": "strathex:v1",
        },
        bundle_version="ml:development-candidate",
        specialist_eligibility={key: eligibility[key] for key in specialists},
    )
    loaded = load_ml_bundle(
        bundle_root, installed_catboost_version=catboost.__version__, installed_python_abi="cp313"
    )
    report = {
        "schema_version": "strathmark-v3-development-training-report-v1",
        "created_at_utc": now,
        "evidence_tier": "development_candidate",
        "production_ready": False,
        "promoted": False,
        "source_commit": source_commit,
        "source_artifact_digest": source_artifact_digest,
        "workbook_sha256": history.source_sha256,
        "snapshot_digest": generation,
        "exclusive_cutoff_at_utc": cutoff_at_utc,
        "row_counts": {role.value: len(values) for role, values in rows.items()},
        "group_counts": dict(Counter(role.value for role in assignments.values())),
        "excluded": dict(history.excluded),
        "legacy_grouping": "recorded competition label and calendar year; unlabeled whole dates",
        "limitations": [
            "legacy issue marks and legal field rosters are unknown",
            "historical completions are not authenticated issued results",
            "no production eligibility or bundle promotion is granted",
        ],
        "ml_bundle_digest": loaded.digest,
        "formula_digest": formula_manifest.digest,
        "formula_prior_policy": PRIOR_POLICY,
        "formula_prior_role": MLDataRole.TRAINING.value,
        "catboost_version": catboost.__version__,
        "training_settings": training_settings,
        "training_selection": "minimum raw-seconds MAE on disjoint tuning tournaments",
        "tuning_trials": tuning_trials,
        "specialists": sorted(specialists),
        "specialist_gate": gate_status,
        "tuning_oof_count": len(gate_oof),
        "calibration_oof_count": len(calibration_oof),
        "live_data_modified": False,
    }
    if implementation_digest() != source_artifact_digest:
        raise ValueError("training implementation changed; candidate evidence cannot be finalized")
    for name, value in {
        "candidate-role-manifest.json": candidate_manifest.to_dict(),
        "candidate-public-identity.json": candidate_signer.identity.to_dict(),
        "training-report.json": report,
    }.items():
        (output / name).write_bytes(canonical_bytes(value, max_bytes=20_000_000))
    return report


def _packets(eligible, generation):
    return tuple(
        EvidencePacket.create(
            competitor_id=StableIdentifier(competitor_id),
            target_context=observations[-1].context,
            observations=tuple(observations),
            taxonomy_version="strathex:v1",
            conversion_version="strathex:v1",
            historical_cutoff_key=f"history:{generation}",
            tournament_epoch_id=StableIdentifier(f"epoch:{generation}"),
            tournament_event_sequence=max(item.observation_sequence for item in observations),
        )
        for competitor_id, observations in sorted(eligible.items())
    )


def _evaluate_candidate(payload, output):
    """Separate evaluator opens the already frozen bundle; it cannot train models."""
    import catboost

    report = json.loads((output / "training-report.json").read_text())
    bundle = load_ml_bundle(
        output / "ml-bundle",
        installed_catboost_version=catboost.__version__,
        installed_python_abi="cp313",
    )
    if bundle.digest != payload["frozen_bundle_digest"]:
        raise ValueError("evaluation bundle differs from the frozen builder artifact")
    calibration_end_year = payload["calibration_end_year"]
    observations = tuple(ResultObservation.from_dict(item) for item in payload["observations"])
    audit_assignments = sorted(
        {
            str(item.tournament_id)
            for item in observations
            if int(item.occurred_at_utc[:4]) > calibration_end_year
        }
    )
    signer = P256EphemeralSigner.generate("integrity-key:candidate-auditor")
    candidate_manifest = SignedManifest.from_dict(
        json.loads((output / "candidate-role-manifest.json").read_text())
    )
    candidate_identity = IntegrityKeyIdentity.from_dict(
        json.loads((output / "candidate-public-identity.json").read_text())
    )
    audit_manifest = sign_manifest(
        "ml_audit_role_manifest",
        {
            "schema_version": "strathmark-v3-ml-role-manifest-v3",
            "generation_digest": report["snapshot_digest"],
            "assignments": [
                {"tournament_id": key, "role": MLDataRole.LOCKED_AUDIT.value}
                for key in audit_assignments
            ],
        },
        signer=signer,
        created_at=report["created_at_utc"],
    )
    authority = _compose_ml_audit_authority(
        audit_manifest,
        signer.identity,
        signer,
        environment=MLAuthorityEnvironment.DEVELOPMENT_CANDIDATE,
        historical_manifest=candidate_manifest,
        historical_identity=candidate_identity,
    )
    eligible = defaultdict(list)
    for item in observations:
        eligible[str(item.competitor_id)].append(item)
    rows = authority.build_development_causal_rows(
        MLDataRole.LOCKED_AUDIT, _packets(eligible, report["snapshot_digest"])
    )
    losses, errors = [], []
    for row in rows:
        features, _ = bundle.normalize_features(row.feature_dict)
        prediction = predict_model_log_quantiles(
            bundle.universal_model, [[features[name] for name in FEATURE_NAMES]]
        )
        losses.append(mean_pinball_loss(float(row.target_log_seconds), prediction))
        errors.append(abs(math.exp(float(prediction[3])) - math.exp(float(row.target_log_seconds))))
    if (
        load_ml_bundle(
            output / "ml-bundle",
            installed_catboost_version=catboost.__version__,
            installed_python_abi="cp313",
        ).digest
        != bundle.digest
    ):
        raise ValueError("evaluation changed the frozen candidate")
    report["row_counts"][MLDataRole.LOCKED_AUDIT.value] = len(rows)
    report["group_counts"][MLDataRole.LOCKED_AUDIT.value] = len(audit_assignments)
    report.update(
        diagnostic_holdout_universal_mean_log_pinball_loss=sum(losses) / len(losses),
        diagnostic_holdout_universal_median_time_mae_seconds=sum(errors) / len(errors),
        isolation="separate development builder/evaluator processes; no OS blind-audit qualification",
        builder_received_audit_rows=False,
        evaluation_frozen_bundle_digest=bundle.digest,
    )
    for name, value in {
        "training-report.json": report,
        "audit-role-manifest.json": audit_manifest.to_dict(),
        "audit-public-identity.json": signer.identity.to_dict(),
    }.items():
        (output / name).write_bytes(canonical_bytes(value, max_bytes=20_000_000))
    return report


def _worker(role, payload, output):
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "strathmark.v3.factory.candidate_cli",
            "--worker",
            role,
            "--output",
            str(output),
        ],
        input=canonical_bytes(payload, max_bytes=20_000_000),
        capture_output=True,
        timeout=600,
        env={
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("STRATHMARK_", "STRATHEX_", "PYTHONPATH", "PYTHONHOME"))
        },
    )
    if completed.returncode:
        raise ValueError(
            f"candidate {role} failed: {completed.stderr.decode('utf-8', errors='replace')[-4000:]}"
        )
    return json.loads(completed.stdout)


def train_candidate(
    *,
    workbook: Path,
    output: Path,
    source_commit: str,
    source_repository: Path,
    cutoff_at_utc: str,
    training_end_year=2022,
    tuning_end_year=2023,
    calibration_end_year=2024,
):
    """Coordinate private partitioning, an audit-free builder, then frozen evaluation."""
    if sys.version_info[:2] != (3, 13):
        raise ValueError("V3 candidate training requires Python 3.13")
    if not training_end_year < tuning_end_year < calibration_end_year:
        raise ValueError("role year boundaries must be strictly increasing")
    if output.exists():
        raise FileExistsError("candidate output exists; refusing to overwrite evidence")
    source_digest = verify_source_revision(source_repository, source_commit)
    history = load_workbook_history(workbook, cutoff_at_utc=cutoff_at_utc)
    years = {int(item.occurred_at_utc[:4]) for item in history.observations}
    if not all(
        (
            any(year <= training_end_year for year in years),
            any(training_end_year < year <= tuning_end_year for year in years),
            any(tuning_end_year < year <= calibration_end_year for year in years),
            any(year > calibration_end_year for year in years),
        )
    ):
        raise ValueError("training requires dated evidence in all four disjoint roles")
    common = {
        "source_commit": source_commit,
        "source_repository": str(source_repository.resolve(strict=True)),
        "source_artifact_digest": source_digest,
        "workbook_sha256": history.source_sha256,
        "cutoff_at_utc": cutoff_at_utc,
        "role_year_boundaries": [training_end_year, tuning_end_year, calibration_end_year],
        "excluded": dict(history.excluded),
    }
    report = _worker(
        "builder",
        {
            **common,
            "observations": [
                item.to_dict()
                for item in history.observations
                if int(item.occurred_at_utc[:4]) <= calibration_end_year
            ],
        },
        output,
    )
    report = _worker(
        "evaluator",
        {
            "frozen_bundle_digest": report["ml_bundle_digest"],
            "calibration_end_year": calibration_end_year,
            "observations": [item.to_dict() for item in history.observations],
        },
        output,
    )
    if implementation_digest() != source_digest:
        raise ValueError("coordinator implementation changed during training")
    (output / "history.json").write_bytes(
        canonical_bytes(
            {
                "source_sha256": history.source_sha256,
                "competitor_ids": list(history.competitor_ids),
                "observations": [item.to_dict() for item in history.observations],
            },
            max_bytes=20_000_000,
        )
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=("builder", "evaluator"), help=argparse.SUPPRESS)
    parser.add_argument("--workbook", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-commit")
    parser.add_argument("--source-repository", type=Path)
    parser.add_argument("--cutoff-at-utc")
    parser.add_argument("--training-end-year", type=int, default=2022)
    parser.add_argument("--tuning-end-year", type=int, default=2023)
    parser.add_argument("--calibration-end-year", type=int, default=2024)
    args = parser.parse_args()
    if args.worker:
        raw = sys.stdin.buffer.read(20_000_001)
        if len(raw) > 20_000_000:
            raise ValueError("candidate worker input exceeds byte limit")
        payload = json.loads(raw)
        report = (_build_candidate if args.worker == "builder" else _evaluate_candidate)(
            payload, args.output
        )
    else:
        if not all((args.workbook, args.source_commit, args.source_repository, args.cutoff_at_utc)):
            parser.error("training requires workbook, source commit, source repository, and cutoff")
        report = train_candidate(
            workbook=args.workbook,
            output=args.output,
            source_commit=args.source_commit,
            source_repository=args.source_repository,
            cutoff_at_utc=args.cutoff_at_utc,
            training_end_year=args.training_end_year,
            tuning_end_year=args.tuning_end_year,
            calibration_end_year=args.calibration_end_year,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
