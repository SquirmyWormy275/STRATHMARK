"""Transparent development Formula priors from authenticated training-role rows.

Prior values retain authenticated TRAIN lineage. A separate disjoint TUNE grid
selects prior strength and robust scale through the real governor and assessor.
Calibration, audit, other assessor predictions and ensemble weights never fit priors.
"""

from __future__ import annotations

import math
from collections import defaultdict
from itertools import product
from pathlib import Path
from statistics import median

from strathmark.v3.assessors.formula import FormulaManifest
from strathmark.v3.contracts.canonical import canonical_decimal_string, canonical_digest
from strathmark.v3.factory.ml_training import MLDataRole

TRAINED_PRIOR_VERSION = "formula:v2-trained-priors-v1"
PRIOR_POLICY = "training-context-median-mad-exact-five-or-scaled-material-v1"
TUNED_PRIOR_VERSION = "formula:v3-tuned-priors-v1"
TUNING_POLICY = "training-priors-disjoint-tuning-strength-scale-grid-v1"


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


def select_formula_candidate(authority, training_rows, tuning_rows, tuning_packets):
    """Learn prior values from TRAIN; select only strength/scale using TUNE.

    Sealed input packets must match the authenticated rows' source digests.
    Each target is removed before the real Formula governor and assessor run.
    Calibration and audit rows cannot enter this selection.
    """
    authority._verify_rows(training_rows, (MLDataRole.TRAINING,))
    authority._verify_rows(tuning_rows, (MLDataRole.TUNING,))
    if not tuning_rows:
        raise ValueError("Formula tuning requires authenticated tuning rows")
    packets = {p.content_digest: p for p in tuning_packets}
    if any(p.recompute_digest() != p.content_digest for p in packets.values()):
        raise ValueError("Formula tuning requires sealed input packets")
    if any(r.source_packet_digest not in packets for r in tuning_rows):
        raise ValueError("Formula tuning packet differs from authenticated source rows")
    baseline = build_formula_candidate(authority, training_rows)
    inputs = [_formula_tuning_input(r, packets[r.source_packet_digest]) for r in tuning_rows]
    from strathmark.v3.assessors.formula import assess_formula

    candidates, trials = [], []
    for strength, scale in product((1, 3), ("0.05", "0.2", "0.4", "0.8")):
        value = baseline.to_dict()
        value["version"] = TUNED_PRIOR_VERSION
        value["robust_center"]["minimum_scale"] = scale
        for prior in value["context_priors"] + value["discipline_priors"]:
            prior["pseudo_count"] = strength
        value.pop("digest")
        value["digest"] = canonical_digest(value)
        manifest = FormulaManifest.from_dict(value)
        errors = [
            abs(
                assess_formula(packet, manifest).center_ms / 1000
                - math.exp(float(row.target_log_seconds))
            )
            for row, packet in zip(tuning_rows, inputs, strict=True)
        ]
        trial = {
            "context_and_discipline_pseudo_count": strength,
            "minimum_robust_scale": scale,
            "row_count": len(errors),
            "mean_absolute_error_seconds": sum(errors) / len(errors),
            "p90_absolute_error_seconds": sorted(errors)[math.ceil(0.9 * len(errors)) - 1],
            "manifest_digest": manifest.digest,
        }
        trials.append(trial)
        candidates.append(manifest)
    selected = min(
        range(len(trials)),
        key=lambda i: (
            trials[i]["mean_absolute_error_seconds"],
            trials[i]["p90_absolute_error_seconds"],
        ),
    )
    return candidates[selected], trials[selected], trials


def _formula_tuning_input(row, source):
    from strathmark.v3.application.formula_governor import (
        FormulaHistoricalAuthority,
        FormulaProjectionFactory,
        HistoricalCutoverAuthority,
        seal_formula_governor_batch,
    )
    from strathmark.v3.contracts.evidence import EvidencePacket
    from strathmark.v3.contracts.identifiers import StableIdentifier, deterministic_identifier
    from strathmark.v3.domain.epochs import EpochMember, ReactionBarrier, freeze_epoch
    from strathmark.v3.infrastructure.integrity import IntegrityTrustStore, P256EphemeralSigner

    target = next((o for o in source.observations if str(o.evidence_id) == row.row_id), None)
    if (
        target is None
        or str(target.competitor_id) != row.competitor_id
        or target.occurred_at_utc != row.occurred_at_utc
    ):
        raise ValueError("Formula tuning target differs from its authenticated row")
    prior = tuple(
        o
        for o in source.observations
        if o.occurred_at_utc < row.occurred_at_utc
        and o.observation_sequence < row.observation_sequence
    )
    maximum = max((o.observation_sequence for o in prior), default=0)
    keys = {
        str(o.evidence_id): deterministic_identifier(
            "result", {"historical_evidence_id": str(o.evidence_id)}
        )
        for o in prior
    }
    epoch = freeze_epoch(
        round_id=StableIdentifier("round:formula-tuning"),
        epoch_revision=1,
        historical_cutoff_key="history:formula-tuning",
        closed_through_sequence=maximum,
        members=tuple(
            sorted(
                (
                    EpochMember(
                        str(keys[str(o.evidence_id)]),
                        o.result.revision,
                        o.observation_sequence,
                        o.result.status.value == "completion",
                    )
                    for o in prior
                ),
                key=lambda v: v.result_key,
            )
        ),
        barrier=ReactionBarrier.complete_through(maximum),
    )
    packet = EvidencePacket.create(
        competitor_id=target.competitor_id,
        target_context=target.context,
        observations=prior,
        taxonomy_version=target.context.taxonomy_version,
        conversion_version=target.context.conversion_version,
        historical_cutoff_key=epoch.historical_cutoff_key,
        tournament_epoch_id=epoch.epoch_id,
        tournament_event_sequence=maximum,
    )
    cutover = HistoricalCutoverAuthority(
        StableIdentifier("historical_cutover:formula-tuning"),
        epoch.historical_cutoff_key,
        source.content_digest,
    )
    signer = P256EphemeralSigner.generate("integrity-key:formula-development-tuning")
    trust = IntegrityTrustStore((signer.identity,))
    scope = StableIdentifier("tournament:formula-tuning")
    legacy = tuple(sorted({o.tournament_id for o in prior}))
    batch = seal_formula_governor_batch(
        evidence=packet,
        epoch=epoch,
        cutoff_at_utc=target.occurred_at_utc,
        active_tournament_id=scope,
        authoritative_tournament_ids=(),
        legacy_tournament_ids=legacy,
        live_authorities=(),
        historical_authorities=tuple(
            FormulaHistoricalAuthority(o.evidence_id, keys[str(o.evidence_id)], cutover)
            for o in prior
        ),
        signer=signer,
        created_at=target.occurred_at_utc,
    )
    return FormulaProjectionFactory(
        trust_store=trust,
        cutoff_at_utc=target.occurred_at_utc,
        active_tournament_id=scope,
        authoritative_tournament_ids=(),
        legacy_tournament_ids=legacy,
    ).project(evidence=packet, epoch=epoch, sealed_batch=batch)
