"""Explicit development candidate training from an authorized workbook snapshot."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from strathmark.v3.assessors.ml import SpecialistGate
from strathmark.v3.composition import compose_development_ml_authorities
from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.evidence import EvidencePacket
from strathmark.v3.contracts.identifiers import StableIdentifier
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
    MLDataRole,
    mean_pinball_loss,
)
from strathmark.v3.factory.workbook_history import load_workbook_history
from strathmark.v3.infrastructure.integrity import P256EphemeralSigner, sign_manifest
from strathmark.v3.runtime_identity import implementation_digest


def train_candidate(
    *,
    workbook: Path,
    output: Path,
    source_commit: str,
    cutoff_at_utc: str,
    training_end_year: int = 2022,
    tuning_end_year: int = 2023,
    calibration_end_year: int = 2024,
) -> dict:
    """Train, calibrate, export, verify, and measure an unpromoted candidate.

    All roles use disjoint whole legacy annual competition groups. This command
    cannot publish, promote, enable V3, or claim production qualification.
    """
    if sys.version_info[:2] != (3, 13):
        raise ValueError("V3 candidate training requires Python 3.13")
    import catboost

    if not training_end_year < tuning_end_year < calibration_end_year:
        raise ValueError("role year boundaries must be strictly increasing")
    if output.exists():
        raise FileExistsError("candidate output exists; refusing to overwrite evidence")
    source_artifact_digest = implementation_digest()
    history = load_workbook_history(workbook, cutoff_at_utc=cutoff_at_utc)
    if not history.observations:
        raise ValueError("snapshot contains no eligible dated historical completions")

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
    if set(assignments.values()) != set(MLDataRole):
        raise ValueError("training requires dated evidence in all four disjoint roles")
    generation = canonical_digest(
        {
            "workbook_sha256": history.source_sha256,
            "cutoff_at_utc": cutoff_at_utc,
            "role_year_boundaries": [training_end_year, tuning_end_year, calibration_end_year],
        }
    )
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    candidate_signer = P256EphemeralSigner.generate("integrity-key:candidate-builder")
    audit_signer = P256EphemeralSigner.generate("integrity-key:candidate-auditor")

    def role_manifest(audit):
        return sign_manifest(
            "ml_audit_role_manifest" if audit else "ml_role_manifest",
            {
                "schema_version": "strathmark-v3-ml-role-manifest-v3",
                "generation_digest": generation,
                "assignments": [
                    {"tournament_id": key, "role": role.value}
                    for key, role in sorted(assignments.items())
                    if (role is MLDataRole.LOCKED_AUDIT) == audit
                ],
            },
            signer=audit_signer if audit else candidate_signer,
            created_at=now,
        )

    candidate_manifest, audit_manifest = role_manifest(False), role_manifest(True)
    authority, audit_authority = compose_development_ml_authorities(
        candidate_manifest,
        audit_manifest,
        candidate_signer=candidate_signer,
        audit_signer=audit_signer,
    )
    rows = {}
    for role in MLDataRole:
        packets = tuple(
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
            for (assigned_role, competitor_id), observations in sorted(
                grouped.items(), key=lambda item: (item[0][0].value, item[0][1])
            )
            if assigned_role is role
        )
        if role is MLDataRole.LOCKED_AUDIT:
            rows[role] = audit_authority.build_locked_audit_replay_matrix(
                audit_authority.authorize_packets(packets)
            )
        else:
            rows[role] = authority.build_causal_training_matrix(
                authority.authorize_packets(role, packets)
            )
    training_settings = {"iterations": 400, "depth": 4, "learning_rate": 0.03, "seed": 20260823}
    universal, specialists, eligibility = authority.train_catboost_hierarchy(
        rows[MLDataRole.TRAINING], **training_settings
    )
    # An ineligible specialist abstains; the universal model keeps its identity.
    gate_oof = authority.chronological_holdout_component_predictions(
        rows[MLDataRole.TRAINING], rows[MLDataRole.TUNING], **training_settings
    )
    examples = authority.gate_examples_from_oof(gate_oof, rows[MLDataRole.TUNING])
    if examples and len({item.fold_id for item in examples}) >= 2:
        gate = authority.fit_specialist_gate(examples)
        gate_status = "fitted_from_grouped_tuning_oof"
    else:
        gate = SpecialistGate("0", (("log_history_depth", "0"), ("missing_fraction", "0")))
        gate_status = "no_eligible_oof_specialist; universal_only"
    calibration_oof = authority.chronological_holdout_component_predictions(
        rows[MLDataRole.TRAINING], rows[MLDataRole.CALIBRATION], **training_settings
    )
    calibrator = authority.fit_pit_calibrator(rows[MLDataRole.CALIBRATION], calibration_oof, gate)
    output.mkdir(parents=True, mode=0o700)
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
            "code_revision": source_commit,
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
    # Locked-audit targets are first read for scoring after export and activation.
    audit_losses, absolute_errors = [], []
    for row in rows[MLDataRole.LOCKED_AUDIT]:
        features, _ = loaded.normalize_features(row.feature_dict)
        prediction = loaded.universal_model.predict([[features[name] for name in FEATURE_NAMES]])[0]
        audit_losses.append(mean_pinball_loss(float(row.target_log_seconds), prediction))
        absolute_errors.append(
            abs(math.exp(float(prediction[3])) - math.exp(float(row.target_log_seconds)))
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
        "row_counts": {role.value: len(rows[role]) for role in MLDataRole},
        "group_counts": dict(Counter(role.value for role in assignments.values())),
        "excluded": dict(history.excluded),
        "legacy_grouping": "recorded competition label and calendar year; unlabeled whole dates",
        "limitations": [
            "legacy issue marks and legal field rosters are unknown",
            "historical completions are not authenticated issued results",
            "no production eligibility or bundle promotion is granted",
        ],
        "ml_bundle_digest": loaded.digest,
        "catboost_version": catboost.__version__,
        "training_settings": training_settings,
        "specialists": sorted(specialists),
        "specialist_gate": gate_status,
        "tuning_oof_count": len(gate_oof),
        "calibration_oof_count": len(calibration_oof),
        "locked_audit_universal_mean_log_pinball_loss": sum(audit_losses) / len(audit_losses),
        "locked_audit_universal_median_time_mae_seconds": sum(absolute_errors)
        / len(absolute_errors),
        "live_data_modified": False,
    }
    if implementation_digest() != source_artifact_digest:
        raise ValueError("training implementation changed; candidate evidence cannot be finalized")
    for name, value in {
        "candidate-role-manifest.json": candidate_manifest.to_dict(),
        "audit-role-manifest.json": audit_manifest.to_dict(),
        "public-identities.json": [
            candidate_signer.identity.to_dict(),
            audit_signer.identity.to_dict(),
        ],
        "training-report.json": report,
        "history.json": {
            "source_sha256": history.source_sha256,
            "competitor_ids": list(history.competitor_ids),
            "observations": [item.to_dict() for item in history.observations],
        },
    }.items():
        (output / name).write_bytes(canonical_bytes(value, max_bytes=20_000_000))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Train an unpromoted V3 development candidate from an explicitly authorized historical snapshot."
    )
    parser.add_argument("--workbook", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--cutoff-at-utc", required=True)
    parser.add_argument("--training-end-year", type=int, default=2022)
    parser.add_argument("--tuning-end-year", type=int, default=2023)
    parser.add_argument("--calibration-end-year", type=int, default=2024)
    args = parser.parse_args()
    print(
        json.dumps(
            train_candidate(
                workbook=args.workbook,
                output=args.output,
                source_commit=args.source_commit,
                cutoff_at_utc=args.cutoff_at_utc,
                training_end_year=args.training_end_year,
                tuning_end_year=args.tuning_end_year,
                calibration_end_year=args.calibration_end_year,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
