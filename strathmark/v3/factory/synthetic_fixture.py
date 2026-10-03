"""Explicit synthetic native-ML bundle for installed functional verification only.

Never use this fixture as an operator model or model-quality evidence.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path


def build_synthetic_fixture(root):
    import catboost

    from strathmark.v3.assessors.ml import PITCalibrator, SpecialistGate
    from strathmark.v3.factory.ml_artifacts import (
        BUNDLE_METADATA_SCHEMA,
        DEPENDENCY_SCHEMA,
        FEATURE_SCHEMA,
        VOCABULARY_SCHEMA,
        export_catboost_json,
        write_ml_bundle,
    )
    from strathmark.v3.factory.ml_training import (
        CATEGORICAL_FEATURES,
        FEATURE_NAMES,
        CausalTrainingRow,
        _train_catboost_hierarchy,
    )

    root = Path(root)
    root.mkdir(parents=True, exist_ok=False)
    rows = []
    for index in range(40):
        seconds = 28 if index % 2 == 0 else 40
        features = {
            "event_family": "underhand",
            "species": "s01",
            "size_mm": 300,
            "density": 500.0,
            "density_missing": 0,
            "history_depth": index,
            "exact_history_depth": index,
            "history_log_median": math.log(seconds),
            "history_log_spread": 0.1,
            "history_missing": 0,
            "sequence_recency": 0,
            "history_log_trend": 0.0,
            "context_distance": 0.0,
            "eligible_tournament_sequence": index,
            "current_form_log_seconds": math.log(seconds),
            "exact_history_log_median": math.log(seconds),
            "same_material_scaled_log_median": math.log(seconds),
            "same_event_scaled_log_median": math.log(seconds),
            "same_material_history_depth": index,
            "same_event_history_depth": index,
            "same_material_recent_log_median": math.log(seconds),
        }
        rows.append(
            CausalTrainingRow(
                f"evidence:synthetic-{index}",
                f"competitor:synthetic-{index % 2}",
                f"tournament:synthetic-training-{index % 5}",
                "2025-01-02T00:00:00.000Z",
                index + 1,
                "underhand|300|s01",
                tuple((name, features[name]) for name in FEATURE_NAMES),
                str(math.log(seconds)),
                "a" * 64,
                index,
                "2025-01-01T00:00:00.000Z" if index else "0001-01-01T00:00:00.000Z",
                f"field:synthetic-{index}",
                "strathex:v1",
                "strathex:v1",
            )
        )
    universal, specialists, _eligibility = _train_catboost_hierarchy(
        tuple(rows), iterations=12, depth=2
    )
    assert specialists == {}
    calibrator = PITCalibrator.identity(source_digest="e" * 64)
    return write_ml_bundle(
        root / "ml-bundle",
        universal_model_json=export_catboost_json(universal, root / "universal.json"),
        specialist_model_json={},
        gate=SpecialistGate("0", (("log_history_depth", "0"), ("missing_fraction", "0"))),
        calibrator=calibrator,
        feature_schema={
            "schema_version": FEATURE_SCHEMA,
            "features": list(FEATURE_NAMES),
            "categorical": list(CATEGORICAL_FEATURES),
            "quantiles": ["0.05", "0.1", "0.25", "0.5", "0.75", "0.9", "0.95"],
        },
        category_vocabulary={
            "schema_version": VOCABULARY_SCHEMA,
            "values": {"event_family": ["__other__", "underhand"], "species": ["__other__", "s01"]},
        },
        dependency_lock={
            "schema_version": DEPENDENCY_SCHEMA,
            "catboost_version": catboost.__version__,
            "python_abi": "cp313",
        },
        bundle_metadata={
            "schema_version": BUNDLE_METADATA_SCHEMA,
            "code_revision": "synthetic-installed-workflow-verification",
            "training_snapshot_digest": "1" * 64,
            "role_manifest_digest": "2" * 64,
            "gate_oof_digest": "3" * 64,
            "calibrator_source_digest": calibrator.source_digest,
            "taxonomy_version": "strathex:v1",
            "conversion_version": "strathex:v1",
        },
        bundle_version="ml:synthetic-linux-test",
    )


def main():
    parser = argparse.ArgumentParser(
        description="Build synthetic fixture ONLY for isolated functional smoke"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build_synthetic_fixture(args.output)
    print(args.output / "ml-bundle")


if __name__ == "__main__":
    main()
