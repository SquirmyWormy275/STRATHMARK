from __future__ import annotations

import math
from dataclasses import dataclass, replace

import pytest

from strathmark.v3.assessors.ml import MLAssessor, PITCalibrator, SpecialistGate
from strathmark.v3.composition import compose_test_ml_audit_authority
from strathmark.v3.contracts.evidence import (
    ContextProperty,
    EvidencePacket,
    ResultObservation,
    TargetContext,
)
from strathmark.v3.contracts.forecasts import AssessorKind, ForecastState
from strathmark.v3.contracts.identifiers import StableIdentifier
from strathmark.v3.contracts.statuses import OfficialResult, ResultStatus
from strathmark.v3.factory.ml_artifacts import LoadedMLBundle
from strathmark.v3.factory.ml_training import (
    AuthorizedMLRows,
    MarkConsequenceFieldInput,
    MarkConsequenceOutcome,
    MarkConsequenceReceipt,
    SpecialistEligibility,
    TrustedMLAuditAuthority,
    _canonical_mean,
    _distribution_probability,
    _quantile_probability,
)
from strathmark.v3.infrastructure.integrity import (
    P256EphemeralSigner,
    sign_manifest,
)


@dataclass
class _Model:
    values: tuple[float, ...]

    def predict(self, _rows: list[list[object]]) -> list[list[float]]:
        return [list(self.values)]


def _packet(event: str, size: int, species: str) -> EvidencePacket:
    context = TargetContext(event, size, species, "taxonomy:v1", "conversion:v1", ())
    return EvidencePacket.create(
        competitor_id=StableIdentifier("competitor:replay-a"),
        target_context=context,
        observations=(),
        taxonomy_version="taxonomy:v1",
        conversion_version="conversion:v1",
        historical_cutoff_key="history:2026-08-01",
        tournament_epoch_id=StableIdentifier("epoch:replay-a"),
        tournament_event_sequence=0,
    )


def _logs(values: tuple[int, ...]) -> tuple[float, ...]:
    return tuple(math.log(item) for item in values)


def _bundle() -> LoadedMLBundle:
    return LoadedMLBundle.for_testing(
        digest="a" * 64,
        version="ml:v1",
        universal_model=_Model(_logs((30, 32, 35, 40, 45, 50, 55))),
        specialist_models={"underhand|300|gum": _Model(_logs((25, 27, 30, 34, 38, 42, 46)))},
        specialist_eligibility={"underhand|300|gum": SpecialistEligibility(500, 30, 10)},
        gate=SpecialistGate("0", (("log_history_depth", "0.1"), ("missing_fraction", "0"))),
        calibrator=PITCalibrator.identity(source_digest="b" * 64),
        feature_names=("event_family", "species", "size_mm"),
        categorical_features=("event_family", "species"),
        vocabulary={
            "event_family": ("__other__", "underhand"),
            "species": ("__other__", "gum"),
        },
        taxonomy_version="taxonomy:v1",
        conversion_version="conversion:v1",
    )


def test_frozen_replay_is_reproducible_and_uses_supported_specialist() -> None:
    assessor = MLAssessor(_bundle())
    packet = _packet("underhand", 300, "gum")
    first = assessor.assess(packet)
    second = assessor.assess(packet)
    assert first == second
    assert first.forecast.assessor is AssessorKind.ML
    assert first.forecast.state is ForecastState.COMMITTED
    assert 0.1 <= first.specialist_weight <= 0.9
    assert first.specialist_key == "underhand|300|gum"
    assert first.forecast.evidence_digest == packet.content_digest


def test_sparse_or_unseen_context_has_explicit_universal_fallback_over_whole_domain() -> None:
    assessor = MLAssessor(_bundle())
    packets = (
        (_packet("underhand", 325, "gum"), False),
        (_packet("invented_event", 999, "invented_species"), True),
    )
    for packet, unseen in packets:
        result = assessor.assess(packet)
        assert result.specialist_weight == 0.0
        assert result.specialist_key is None
        assert result.forecast.distribution is not None
        assert result.forecast.distribution.median_ms == 40_000
        assert bool(result.unseen_categories) is unseen


def test_ml_prediction_has_no_formula_input_or_output_dependency() -> None:
    assessor = MLAssessor(_bundle())
    result = assessor.assess(_packet("underhand", 300, "gum"))
    serialized = str(result.to_dict()).lower()
    assert "formula" not in serialized
    assert "llm" not in serialized
    assert result.universal_quantiles_ms
    assert result.specialist_quantiles_ms


def test_opaque_competitor_rename_cannot_change_numeric_ml_forecast() -> None:
    assessor = MLAssessor(_bundle())
    original = _packet("underhand", 300, "gum")
    renamed = EvidencePacket.create(
        competitor_id=StableIdentifier("competitor:opaque-renamed"),
        target_context=original.target_context,
        observations=(),
        taxonomy_version=original.taxonomy_version,
        conversion_version=original.conversion_version,
        historical_cutoff_key=original.historical_cutoff_key,
        tournament_epoch_id=original.tournament_epoch_id,
        tournament_event_sequence=original.tournament_event_sequence,
    )
    assert (
        assessor.assess(original).forecast.distribution
        == assessor.assess(renamed).forecast.distribution
    )


def test_prediction_rejects_bundle_taxonomy_or_conversion_mismatch() -> None:
    bundle = _bundle()
    incompatible = replace(
        bundle,
        metadata={**bundle.metadata, "taxonomy_version": "taxonomy:wrong"},
    )
    with pytest.raises(ValueError, match="taxonomy or conversion"):
        MLAssessor(incompatible).assess(_packet("underhand", 300, "gum"))


class _ConsequenceSpy:
    def __init__(self) -> None:
        self.inputs: list[MarkConsequenceFieldInput] = []

    def evaluate(self, field_input: MarkConsequenceFieldInput) -> MarkConsequenceReceipt:
        self.inputs.append(field_input)
        outcomes = tuple(
            MarkConsequenceOutcome(
                row_id,
                "1",
                "2",
                "0.1",
                "-0.25",
                "3",
                "0.2",
                "0.5",
            )
            for row_id, _actual in field_input.actual_raw_times_ms
        )
        return MarkConsequenceReceipt.create(
            field_id=field_input.field_id,
            input_digest=field_input.input_digest,
            outcomes=outcomes,
        )


def _authorized_replay_rows(
    *, admitted: bool = True
) -> tuple[TrustedMLAuditAuthority, AuthorizedMLRows]:
    context = TargetContext(
        "underhand",
        300,
        "gum",
        "taxonomy:v1",
        "conversion:v1",
        (ContextProperty("density", "720", "kg_m3", None),),
    )
    observations = tuple(
        ResultObservation(
            StableIdentifier(f"evidence:slice-{index}"),
            StableIdentifier("competitor:slice"),
            StableIdentifier("tournament:slice"),
            StableIdentifier("round:heat"),
            StableIdentifier(f"field:separate-{index}"),
            context,
            index,
            f"2026-01-0{index}T00:00:00.000Z",
            3,
            None,
            None,
            None,
            OfficialResult(
                ResultStatus.COMPLETION if admitted else ResultStatus.DNF,
                round(actual * 1000) if admitted else None,
                None,
                index,
                None,
            ),
            f"{index:064x}",
        )
        for index, actual in enumerate((40.0, 30.0), 1)
    )
    packet = EvidencePacket.create(
        competitor_id=StableIdentifier("competitor:slice"),
        target_context=context,
        observations=observations,
        taxonomy_version="taxonomy:v1",
        conversion_version="conversion:v1",
        historical_cutoff_key="history:replay",
        tournament_epoch_id=StableIdentifier("epoch:replay"),
        tournament_event_sequence=2,
    )
    signer = P256EphemeralSigner.generate("ml-replay-audit")
    signed = sign_manifest(
        "ml_audit_role_manifest",
        {
            "schema_version": "strathmark-v3-ml-role-manifest-v3",
            "generation_digest": "d" * 64,
            "assignments": [{"tournament_id": "tournament:slice", "role": "locked_audit"}],
        },
        signer=signer,
        created_at="2026-01-01T00:00:00.000Z",
    )
    authority = compose_test_ml_audit_authority(signed, signer=signer)
    rows = authority.build_locked_audit_replay_matrix(authority.authorize_packets((packet,)))
    return authority, rows


def test_frozen_rolling_replay_emits_predictive_and_handicap_consequence_slices() -> None:
    authority, authorized = _authorized_replay_rows()
    evaluator = _ConsequenceSpy()
    bundle = replace(
        _bundle(),
        calibrator=PITCalibrator("calibration", (("0", "0"), ("0.5", "0.7"), ("1", "1")), "b" * 64),
    )
    report = authority.evaluate_frozen_replay(authorized, bundle, evaluator)
    assert report.grouped_field_count == 2
    assert [item.field_id for item in evaluator.inputs] == [
        "field:separate-1",
        "field:separate-2",
    ]
    assert all(item.field_distribution_digest for item in evaluator.inputs)
    assert dict(report.consequence_receipt_digests).keys() == {
        "field:separate-1",
        "field:separate-2",
    }
    assert all(field_input.forecasts[0][1].median_ms != 40_000 for field_input in evaluator.inputs)
    all_slice = next(item for item in report.slices if item.name == "all")
    assert all_slice.row_count == 2
    assert float(all_slice.mean_pinball_loss) > 0
    assert float(all_slice.mean_absolute_mark_error_seconds) > 0
    assert {item.name for item in report.slices} >= {
        "history:0",
        "history:1-3",
        "missing:yes",
        "missing:no",
        "event:underhand",
        "size_band:300-324",
        "species:gum",
        "fallback:global",
    }
    assert float(all_slice.counterfactual_spread_error_seconds) == 2
    assert float(all_slice.win_probability_distortion) == 0.1


def test_frozen_replay_scores_post_pit_distribution_and_enforces_audit_versions() -> None:
    authority, rows = _authorized_replay_rows()
    identity = authority.evaluate_frozen_replay(rows, _bundle(), _ConsequenceSpy())
    calibrated_bundle = replace(
        _bundle(),
        calibrator=PITCalibrator("calibration", (("0", "0"), ("0.5", "0.8"), ("1", "1")), "b" * 64),
    )
    calibrated = authority.evaluate_frozen_replay(rows, calibrated_bundle, _ConsequenceSpy())
    identity_all = next(item for item in identity.slices if item.name == "all")
    calibrated_all = next(item for item in calibrated.slices if item.name == "all")
    assert calibrated_all.mean_pinball_loss != identity_all.mean_pinball_loss
    assert 0 <= float(calibrated_all.calibration_error) <= 1

    incompatible = replace(
        _bundle(), metadata={**_bundle().metadata, "taxonomy_version": "taxonomy:wrong"}
    )
    with pytest.raises(ValueError, match="taxonomy or conversion"):
        authority.evaluate_frozen_replay(rows, incompatible, _ConsequenceSpy())


def test_consequence_port_contract_is_canonical_exact_and_digest_bound() -> None:
    with pytest.raises(ValueError, match="canonical"):
        MarkConsequenceOutcome("evidence:a", "1.0", "2", "0.1", "0", "3", "0.2", "0.5")
    with pytest.raises(ValueError, match="nonnegative"):
        MarkConsequenceOutcome("evidence:a", "-1", "2", "0.1", "0", "3", "0.2", "0.5")
    with pytest.raises(ValueError, match="canonical"):
        MarkConsequenceOutcome("evidence:a", "1", "2", "0.1", "0.0", "3", "0.2", "0.5")

    distribution = (
        MLAssessor(_bundle()).assess(_packet("underhand", 300, "gum")).forecast.distribution
    )
    assert distribution is not None
    with pytest.raises(ValueError, match="nonempty field"):
        MarkConsequenceFieldInput.create(field_id="bad", forecasts=(), actual_raw_times_ms=())
    with pytest.raises(ValueError, match="typed distributions"):
        MarkConsequenceFieldInput.create(
            field_id="field:a",
            forecasts=(("evidence:a", object()),),  # type: ignore[arg-type]
            actual_raw_times_ms=(("evidence:a", 40_000),),
        )
    with pytest.raises(ValueError, match="same exact rows"):
        MarkConsequenceFieldInput.create(
            field_id="field:a",
            forecasts=(("evidence:a", distribution),),
            actual_raw_times_ms=(("evidence:b", 40_000),),
        )
    with pytest.raises(ValueError, match="positive integer"):
        MarkConsequenceFieldInput.create(
            field_id="field:a",
            forecasts=(("evidence:a", distribution),),
            actual_raw_times_ms=(("evidence:a", 0),),
        )
    valid_input = MarkConsequenceFieldInput.create(
        field_id="field:a",
        forecasts=(("evidence:a", distribution),),
        actual_raw_times_ms=(("evidence:a", 40_000),),
    )
    outcome = MarkConsequenceOutcome("evidence:a", "1", "2", "0.1", "0", "3", "0.2", "0.5")
    with pytest.raises(ValueError, match="field identity"):
        MarkConsequenceReceipt.create(
            field_id="bad", input_digest=valid_input.input_digest, outcomes=(outcome,)
        )
    with pytest.raises(ValueError, match="typed outcomes"):
        MarkConsequenceReceipt.create(
            field_id=valid_input.field_id,
            input_digest=valid_input.input_digest,
            outcomes=(),
        )
    with pytest.raises(ValueError, match="unique nonempty"):
        MarkConsequenceReceipt.create(
            field_id=valid_input.field_id,
            input_digest=valid_input.input_digest,
            outcomes=(outcome, outcome),
        )
    receipt = MarkConsequenceReceipt.create(
        field_id=valid_input.field_id,
        input_digest=valid_input.input_digest,
        outcomes=(outcome,),
    )
    with pytest.raises(ValueError, match="digest differs"):
        replace(receipt, receipt_digest="f" * 64)


@pytest.mark.parametrize("bad_kind", ["object", "wrong_field", "wrong_input", "wrong_rows"])
def test_frozen_replay_rejects_unbound_consequence_receipts(bad_kind: str) -> None:
    authority, rows = _authorized_replay_rows()

    class BadEvaluator:
        def evaluate(self, field_input: MarkConsequenceFieldInput):
            if bad_kind == "object":
                return object()
            row_id = field_input.actual_raw_times_ms[0][0]
            if bad_kind == "wrong_rows":
                row_id = "evidence:wrong"
            outcome = MarkConsequenceOutcome(row_id, "1", "2", "0.1", "0", "3", "0.2", "0.5")
            return MarkConsequenceReceipt.create(
                field_id=("field:wrong" if bad_kind == "wrong_field" else field_input.field_id),
                input_digest=("f" * 64 if bad_kind == "wrong_input" else field_input.input_digest),
                outcomes=(outcome,),
            )

    with pytest.raises(ValueError, match="exact field input"):
        authority.evaluate_frozen_replay(rows, _bundle(), BadEvaluator())


@pytest.mark.parametrize(
    "tamper",
    [
        "receipt_digest",
        "outcome_value",
        "outcome_member",
        "outcome_object",
        "field_input",
        "field_input_shape",
    ],
)
def test_frozen_replay_recomputes_receipt_values_after_evaluator_returns(
    tamper: str,
) -> None:
    authority, rows = _authorized_replay_rows()

    class TamperingEvaluator(_ConsequenceSpy):
        def evaluate(self, field_input: MarkConsequenceFieldInput) -> MarkConsequenceReceipt:
            receipt = super().evaluate(field_input)
            if tamper == "receipt_digest":
                object.__setattr__(receipt, "receipt_digest", "f" * 64)
            elif tamper == "outcome_value":
                object.__setattr__(receipt.outcomes[0], "mark_error_seconds", "999")
            elif tamper == "outcome_member":
                object.__setattr__(receipt.outcomes[0], "row_id", "evidence:wrong")
            elif tamper == "outcome_object":
                object.__setattr__(receipt, "outcomes", (object(),))
            elif tamper == "field_input":
                row_id, raw_time_ms = field_input.actual_raw_times_ms[0]
                object.__setattr__(
                    field_input,
                    "actual_raw_times_ms",
                    ((row_id, raw_time_ms + 1),),
                )
            else:
                object.__setattr__(field_input, "forecasts", (object(),))
            return receipt

    with pytest.raises(ValueError, match="exact field input|digest|canonical"):
        authority.evaluate_frozen_replay(rows, _bundle(), TamperingEvaluator())


def test_replay_probability_and_metric_helpers_cover_boundaries() -> None:
    quantiles = (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0)
    assert _quantile_probability(0.0, quantiles) == 0.05
    assert _quantile_probability(8.0, quantiles) == 0.95
    assert _quantile_probability(4.5, quantiles) == pytest.approx(0.625)
    distribution = (
        MLAssessor(_bundle()).assess(_packet("underhand", 300, "gum")).forecast.distribution
    )
    assert distribution is not None
    assert _distribution_probability(distribution, 0) == 0.001
    assert _distribution_probability(distribution, 1e12) == 0.999
    assert 0 < _distribution_probability(distribution, 40_500) < 1
    with pytest.raises(ValueError, match="finite and nonempty"):
        _canonical_mean(())
    with pytest.raises(ValueError, match="finite and nonempty"):
        _canonical_mean((math.nan,))


def test_frozen_replay_requires_verified_bundle_and_evaluator() -> None:
    authority, rows = _authorized_replay_rows()
    with pytest.raises(ValueError, match="verified ML bundle"):
        authority.evaluate_frozen_replay(rows, object(), _ConsequenceSpy())
    with pytest.raises(ValueError, match="consequence evaluator"):
        authority.evaluate_frozen_replay(rows, _bundle(), object())

    empty_authority, empty_rows = _authorized_replay_rows(admitted=False)
    with pytest.raises(ValueError, match="nonempty locked-audit"):
        empty_authority.evaluate_frozen_replay(empty_rows, _bundle(), _ConsequenceSpy())
