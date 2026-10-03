"""Transparent development Formula priors from authenticated training-role rows.

The frozen bootstrap arithmetic and conversion policy are retained. Only context
priors change; their lineage binds the authorized row envelope and exact targets.
No tuning, calibration, audit, assessor prediction or ensemble weight is admitted.
"""

from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path
from statistics import median

from strathmark.v3.assessors.formula import FormulaManifest
from strathmark.v3.contracts.canonical import canonical_decimal_string, canonical_digest
from strathmark.v3.factory.ml_training import MLDataRole

TRAINED_PRIOR_VERSION = "formula:v2-trained-priors-v1"
PRIOR_POLICY = "training-context-median-mad-exact-five-or-scaled-material-v1"


def build_formula_candidate(authority, training_rows) -> FormulaManifest:
    authority._verify_rows(training_rows, (MLDataRole.TRAINING,))
    return _build_formula_prior_values(
        training_rows, training_rows._authorization_envelope.body_digest
    )


def _build_formula_prior_values(rows, source_digest) -> FormulaManifest:
    """Pure arithmetic; the public factory verifies the signed role boundary."""
    template = Path(__file__).parents[1] / "contracts/formula_manifest.json"
    value = FormulaManifest.load(template).to_dict()
    groups = defaultdict(list)
    for row in rows:
        features = row.feature_dict
        event, material, size = features["event_family"], features["species"], features["size_mm"]
        if event not in value["event_size_exponents"]:
            raise ValueError("Formula training event has no declared conversion policy")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise ValueError("Formula training diameter must be a positive integer")
        target = float(row.target_log_seconds)
        if not math.isfinite(target):
            raise ValueError("Formula training target must be finite")
        groups[(event, material)].append((row, size, target))
    if not groups:
        raise ValueError("Formula training requires admitted training rows")
    priors = []
    for (event, material), group in sorted(groups.items()):
        exponent = float(value["event_size_exponents"][event])
        sizes = {size for _row, size, _target in group} | set(range(225, 501, 25))
        for size in sorted(sizes):
            exact = [item for item in group if item[1] == size]
            selected = exact if len(exact) >= 5 else group
            logs = [
                target + exponent * math.log(size / diameter) for _, diameter, target in selected
            ]
            center = median(logs)
            variance = max(0.04, (1.4826 * median(abs(v - center) for v in logs)) ** 2)
            lineage = canonical_digest(
                {
                    "policy": PRIOR_POLICY,
                    "training_rows_envelope": source_digest,
                    "target": [event, size, material],
                    "source_rows": sorted(row.row_id for row, _, _ in selected),
                    "diameter_exponent": value["event_size_exponents"][event],
                    "exact_context_support": len(exact),
                }
            )
            priors.append(
                {
                    "event_code": event,
                    "material_code": material,
                    "size_mm": size,
                    "median_seconds": _decimal(math.exp(center)),
                    "log_variance": _decimal(variance),
                    "pseudo_count": 3,
                    "lineage_digest": lineage,
                }
            )
    value["version"] = TRAINED_PRIOR_VERSION
    value["context_priors"] = sorted(
        priors, key=lambda item: f"{item['event_code']}|{item['size_mm']}|{item['material_code']}"
    )
    value.pop("digest")
    value["digest"] = canonical_digest(value)
    return FormulaManifest.from_dict(value)


def _decimal(value):
    return canonical_decimal_string(str(round(value, 12)))
