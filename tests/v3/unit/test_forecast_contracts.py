from __future__ import annotations

from dataclasses import replace
from decimal import ROUND_DOWN, localcontext

import pytest

import strathmark.v3.contracts.forecasts as forecast_contracts
from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.forecasts import (
    ArtifactIdentity,
    AssessorForecast,
    AssessorKind,
    DependenceInputs,
    DependenceMode,
    DistributionSamples,
    EvidenceSupport,
    ForecastState,
    ForecastWarning,
    LLMMemberAudit,
    PositiveTimeDistribution,
    QuantilePoint,
    SamplingSpec,
    _samples_digest,
    sample_aligned_positive_distributions,
)
from strathmark.v3.contracts.identifiers import StableIdentifier


def _distribution() -> PositiveTimeDistribution:
    return PositiveTimeDistribution(
        quantiles=(
            QuantilePoint("0.1", 24000),
            QuantilePoint("0.5", 30000),
            QuantilePoint("0.9", 42000),
        )
    )


def _committed_forecast() -> AssessorForecast:
    return AssessorForecast.create(
        forecast_id=StableIdentifier("forecast:formula-edge"),
        assessor=AssessorKind.FORMULA,
        state=ForecastState.COMMITTED,
        evidence_digest="d" * 64,
        distribution=_distribution(),
        support=EvidenceSupport(4, "3.5", 2, "history:9", 17),
        warnings=(ForecastWarning.SPARSE_EVIDENCE,),
        artifacts=(ArtifactIdentity("formula", "formula:v1", "e" * 64),),
        abstention_code=None,
    )


def test_positive_distribution_round_trip_and_digest_are_exact() -> None:
    distribution = _distribution()
    restored = PositiveTimeDistribution.from_dict(distribution.to_dict())
    assert restored == distribution
    assert restored.digest == distribution.digest
    assert distribution.median_ms == 30000
    assert distribution.central_interval("0.1", "0.9") == (24000, 42000)


@pytest.mark.parametrize(
    "points",
    [
        (QuantilePoint("0.1", 24000), QuantilePoint("0.9", 42000)),
        (
            QuantilePoint("0.5", 30000),
            QuantilePoint("0.1", 24000),
            QuantilePoint("0.9", 42000),
        ),
        (
            QuantilePoint("0.1", 30000),
            QuantilePoint("0.5", 29000),
            QuantilePoint("0.9", 42000),
        ),
    ],
)
def test_distribution_requires_median_and_ordered_quantiles(
    points: tuple[QuantilePoint, ...],
) -> None:
    with pytest.raises(ContractError):
        PositiveTimeDistribution(points)


@pytest.mark.parametrize(
    ("probability", "time_ms"),
    [("0", 1), ("1", 1), ("0.50", 1), (0.5, 1), ("0.5", 0), ("0.5", 1.0)],
)
def test_quantiles_reject_noncanonical_probabilities_and_numeric_coercion(
    probability: object, time_ms: object
) -> None:
    with pytest.raises(ContractError):
        QuantilePoint(probability, time_ms)  # type: ignore[arg-type]


def test_sampler_is_seed_injected_positive_and_runtime_independent() -> None:
    distribution = _distribution()
    spec = SamplingSpec(seed=20260822, draw_count=32)
    first = distribution.sample(spec)
    second = distribution.sample(spec)

    assert isinstance(first, DistributionSamples)
    assert first == second
    assert len(first.samples_ms) == 32
    assert min(first.samples_ms) > 0
    assert first.algorithm == "splitmix64-inverse-quantile-v1"
    assert first.dependency_version == "stdlib-only-v1"
    assert first.draw_count == 32
    assert first.time_quantum_ms == 1
    assert first.distribution_digest == distribution.digest
    assert first.samples_digest == second.samples_digest
    assert DistributionSamples.from_dict(first.to_dict()) == first
    corrupt = first.to_dict()
    corrupt["samples_digest"] = "0" * 64
    with pytest.raises(ContractError, match="digest"):
        DistributionSamples.from_dict(corrupt)
    assert distribution.sample(SamplingSpec(seed=20260823, draw_count=32)) != first


def test_fresh_distribution_samples_reuse_same_call_numeric_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    distribution = _distribution()
    spec = SamplingSpec(seed=20260822, draw_count=32)

    def reject_revalidation(*_args, **_kwargs) -> None:
        raise AssertionError("fresh generated samples were revalidated")

    monkeypatch.setattr(forecast_contracts, "_require_positive_int", reject_revalidation)
    samples = distribution.sample(spec)

    assert samples.samples_authority_digest == canonical_digest(samples.samples_ms)
    with pytest.raises(AssertionError, match="revalidated"):
        DistributionSamples.from_dict(samples.to_dict())


@pytest.mark.parametrize(
    ("samples_ms", "seed", "distribution_digest", "common_random_map_digest"),
    (
        ((1,), 0, "0" * 64, None),
        ((1, 20_000, 2_000_000_000), 2**63 - 1, "f" * 64, "a" * 64),
        (tuple(range(1, 4_097)), 20260824, "d" * 64, "c" * 64),
    ),
)
def test_sample_digest_matches_the_canonical_receipt_oracle(
    samples_ms: tuple[int, ...],
    seed: int,
    distribution_digest: str,
    common_random_map_digest: str | None,
) -> None:
    expected = canonical_digest(
        {
            "schema_version": "strathmark-v3-distribution-sampling-v1",
            "algorithm": "splitmix64-inverse-quantile-v1",
            "dependency_version": "stdlib-only-v1",
            "seed": seed,
            "draw_count": len(samples_ms),
            "time_quantum_ms": 1,
            "distribution_digest": distribution_digest,
            "samples_ms": list(samples_ms),
            "common_random_map_digest": common_random_map_digest,
        }
    )

    assert (
        _samples_digest(
            samples_ms=samples_ms,
            seed=seed,
            distribution_digest=distribution_digest,
            common_random_map_digest=common_random_map_digest,
        )
        == expected
    )


def test_common_uniforms_control_sampling_without_ambient_rng() -> None:
    distribution = _distribution()
    samples = distribution.sample(
        SamplingSpec(
            seed=1,
            draw_count=3,
            common_uniforms=("0.1", "0.5", "0.9"),
            common_random_map_digest="c" * 64,
        )
    )
    assert samples.samples_ms == (24000, 30000, 42000)
    assert samples.common_random_map_digest == "c" * 64
    with pytest.raises(ContractError, match="draw_count"):
        SamplingSpec(seed=1, draw_count=2, common_uniforms=("0.1",))


def test_common_uniform_sampling_preserves_frozen_decimal_edge_bytes() -> None:
    distribution = PositiveTimeDistribution(
        (
            QuantilePoint("0.01", 1),
            QuantilePoint("0.5", 10_001),
            QuantilePoint("0.99", 2_000_000_000),
        )
    )
    uniforms = (
        "0.0000000000000000000000000001",
        "0.01",
        "0.0100000000000000000000000001",
        "0.123456789012345678901234567890123456789",
        "0.4999999999999999999999999999",
        "0.5",
        "0.5000000000000000000000000001",
        "0.876543210987654321098765432109876543211",
        "0.9899999999999999999999999999",
        "0.99",
        "0.9999999999999999999999999999",
    )
    sample = distribution.sample(SamplingSpec(7, len(uniforms), uniforms, "a" * 64))

    assert sample.samples_ms == (
        1,
        1,
        1,
        2_316,
        10_001,
        10_001,
        10_001,
        1_536_913_381,
        2_000_000_000,
        2_000_000_000,
        2_000_000_000,
    )
    assert sample.samples_digest == (
        "36494dd9ccee23623d3a29df6e4226a99f56ca876eb776e2720ff881d4c6ce5a"
    )


def test_sampling_is_invariant_to_ambient_decimal_context() -> None:
    distribution = _distribution()
    spec = SamplingSpec(seed=20260822, draw_count=64)
    expected = distribution.sample(spec)

    with localcontext() as hostile:
        hostile.prec = 6
        hostile.rounding = ROUND_DOWN
        actual = distribution.sample(spec)

    assert actual == expected


def test_aligned_distribution_batch_sampling_is_byte_exact_to_individual_oracle() -> None:
    distributions = tuple(
        PositiveTimeDistribution(
            (
                QuantilePoint("0.01", median - 8_000),
                QuantilePoint("0.5", median),
                QuantilePoint("0.99", median + width),
            )
        )
        for median, width in ((40_000, 8_000), (55_000, 12_000), (80_000, 20_000))
    )
    uniforms = tuple(f"0.{index:096d}".rstrip("0") for index in range(1, 130)) + (
        "0.5",
        "0.99",
    )
    spec = SamplingSpec(
        seed=41,
        draw_count=len(uniforms),
        common_uniforms=uniforms,
        common_random_map_digest="a" * 64,
    )

    expected = tuple(distribution.sample(spec) for distribution in distributions)
    assert sample_aligned_positive_distributions(distributions, spec) == expected

    mismatched = (
        distributions[0],
        PositiveTimeDistribution(
            (
                QuantilePoint("0.1", 30_000),
                QuantilePoint("0.5", 55_000),
                QuantilePoint("0.9", 90_000),
            )
        ),
    )
    assert sample_aligned_positive_distributions(mismatched, spec) == tuple(
        distribution.sample(spec) for distribution in mismatched
    )


def test_standard_production_grid_routes_through_sealed_sampler(monkeypatch) -> None:
    distributions = (
        _distribution(),
        PositiveTimeDistribution(
            (
                QuantilePoint("0.1", 30_000),
                QuantilePoint("0.5", 45_000),
                QuantilePoint("0.9", 75_000),
            )
        ),
    )
    uniforms = tuple(("0.2", "0.6")[index % 2] for index in range(4096))
    spec = SamplingSpec(17, 4096, uniforms, "a" * 64)
    expected = tuple(
        distribution._sample_scaled_probabilities(
            spec._scaled_common_uniforms,
            spec._common_uniform_exponent,
        )
        for distribution in distributions
    )
    calls = []

    class RecordingSampler:
        def sample_three_quantiles(self, probability_words_le, time_rows, *, draw_count):
            calls.append((probability_words_le, time_rows, draw_count))
            return expected

    monkeypatch.setattr(forecast_contracts, "_NATIVE_STANDARD_SAMPLER", RecordingSampler())
    monkeypatch.setattr(forecast_contracts, "_NATIVE_STANDARD_SAMPLER_INITIALIZED", True)

    actual = sample_aligned_positive_distributions(distributions, spec)

    assert tuple(row.samples_ms for row in actual) == expected
    assert calls == [
        (
            spec._standard_probability_words_le,
            tuple(distribution._times_ms for distribution in distributions),
            4096,
        )
    ]


@pytest.mark.parametrize("seed", (0, 2**63 - 1))
def test_aligned_distribution_batch_sampling_matches_oracle_under_hostile_context(
    seed: int,
) -> None:
    distributions = (
        PositiveTimeDistribution(
            (
                QuantilePoint("0.01", 1),
                QuantilePoint("0.5", 1),
                QuantilePoint("0.99", 2_000_000_000),
            )
        ),
        PositiveTimeDistribution(
            (
                QuantilePoint("0.01", 10_000),
                QuantilePoint("0.5", 10_001),
                QuantilePoint("0.99", 10_001),
            )
        ),
    )
    spec = SamplingSpec(seed=seed, draw_count=257)
    expected = tuple(item.sample(spec) for item in distributions)

    with localcontext() as hostile:
        hostile.prec = 6
        hostile.rounding = ROUND_DOWN
        actual = sample_aligned_positive_distributions(distributions, spec)

    assert actual == expected


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"seed": 2**64, "draw_count": 1}, "unsigned 64-bit"),
        ({"seed": 1, "draw_count": 1_000_001}, "maximum"),
        ({"seed": 1, "draw_count": 1, "common_uniforms": ["0.5"]}, "immutable tuple"),
        (
            {
                "seed": 1,
                "draw_count": 1,
                "common_uniforms": ("0",),
                "common_random_map_digest": "a" * 64,
            },
            "strictly between",
        ),
        ({"seed": 1, "draw_count": 1, "common_uniforms": ("0.5",)}, "require"),
    ],
)
def test_sampling_spec_rejects_unbounded_or_ambient_values(
    arguments: dict[str, object], message: str
) -> None:
    with pytest.raises(ContractError, match=message):
        SamplingSpec(**arguments)  # type: ignore[arg-type]


def test_distribution_sample_record_is_closed_and_self_verifying() -> None:
    sample = _distribution().sample(SamplingSpec(seed=7, draw_count=3))
    for changes, message in (
        ({"samples_ms": ()}, "nonempty"),
        ({"algorithm": "ambient-rng"}, "algorithm"),
        ({"dependency_version": "ambient"}, "dependency"),
        ({"draw_count": 2}, "match"),
        ({"time_quantum_ms": 10}, "quantum"),
    ):
        with pytest.raises(ContractError, match=message):
            replace(sample, **changes)
    encoded = sample.to_dict()
    encoded["samples_ms"] = "not-an-array"
    with pytest.raises(ContractError, match="JSON array"):
        DistributionSamples.from_dict(encoded)


def test_fresh_distribution_samples_hash_the_generated_vector_once(monkeypatch) -> None:
    import strathmark.v3.contracts.forecasts as forecast_contracts

    calls = 0
    original = forecast_contracts._samples_digest

    def counted(**arguments):
        nonlocal calls
        calls += 1
        return original(**arguments)

    monkeypatch.setattr(forecast_contracts, "_samples_digest", counted)

    sample = _distribution().sample(SamplingSpec(seed=7, draw_count=128))

    assert sample.samples_digest
    assert calls == 1


def test_distribution_decoder_and_interval_boundaries_fail_closed() -> None:
    distribution = _distribution()
    with pytest.raises(ContractError, match="QuantilePoint"):
        PositiveTimeDistribution((QuantilePoint("0.1", 1), object(), QuantilePoint("0.5", 2)))  # type: ignore[arg-type]
    with pytest.raises(ContractError, match="median"):
        PositiveTimeDistribution(
            (
                QuantilePoint("0.1", 1),
                QuantilePoint("0.2", 2),
                QuantilePoint("0.9", 3),
            )
        )
    with pytest.raises(ContractError, match="straddle"):
        distribution.central_interval("0.5", "0.9")
    with pytest.raises(ContractError, match="SamplingSpec"):
        distribution.sample(object())  # type: ignore[arg-type]
    encoded = distribution.to_dict()
    encoded["quantiles"] = "not-an-array"
    with pytest.raises(ContractError, match="JSON array"):
        PositiveTimeDistribution.from_dict(encoded)


def test_dependence_inputs_are_explicit_and_default_to_labeled_independence() -> None:
    independent = DependenceInputs.independence(
        field_id=StableIdentifier("field:final-a"), seed=17, draw_count=8
    )
    assert independent.mode is DependenceMode.INDEPENDENCE
    assert independent.fallback_code == "unsupported_context_independence"
    assert independent.effective_sample_size == "0"
    assert DependenceInputs.from_dict(independent.to_dict()) == independent

    with pytest.raises(ContractError, match="parameters_digest"):
        DependenceInputs(
            field_id=StableIdentifier("field:final-a"),
            mode=DependenceMode.GAUSSIAN_COPULA,
            version="dependence:v1",
            seed=17,
            draw_count=8,
            parameters_digest=None,
            effective_sample_size="40",
            fallback_code=None,
        )


def test_learned_dependence_and_fallback_rules_are_closed() -> None:
    learned = DependenceInputs(
        field_id=StableIdentifier("field:final-a"),
        mode=DependenceMode.GAUSSIAN_COPULA,
        version="dependence:v1",
        seed=17,
        draw_count=8,
        parameters_digest="a" * 64,
        effective_sample_size="40",
        fallback_code=None,
    )
    assert DependenceInputs.from_dict(learned.to_dict()) == learned
    with pytest.raises(ContractError, match="fallback"):
        replace(learned, fallback_code="should-not-exist")
    independent = DependenceInputs.independence(
        field_id=StableIdentifier("field:final-a"), seed=17, draw_count=8
    )
    with pytest.raises(ContractError, match="parameters_digest"):
        replace(independent, parameters_digest="a" * 64)
    with pytest.raises(ContractError, match="fallback"):
        replace(independent, fallback_code=None)
    with pytest.raises(ContractError, match="DependenceMode"):
        replace(independent, mode="independence")  # type: ignore[arg-type]
    encoded = independent.to_dict()
    encoded["mode"] = "invented"
    with pytest.raises(ContractError, match="unknown"):
        DependenceInputs.from_dict(encoded)


def test_committed_forecast_is_one_blind_immutable_distribution() -> None:
    forecast = AssessorForecast.create(
        forecast_id=StableIdentifier("forecast:formula-1"),
        assessor=AssessorKind.FORMULA,
        state=ForecastState.COMMITTED,
        evidence_digest="d" * 64,
        distribution=_distribution(),
        support=EvidenceSupport(4, "3.5", 2, "history:9", 17),
        warnings=(ForecastWarning.SPARSE_EVIDENCE,),
        artifacts=(ArtifactIdentity("formula", "formula:v1", "e" * 64),),
        abstention_code=None,
    )
    assert AssessorForecast.from_dict(forecast.to_dict()) == forecast
    assert forecast.commit_digest == forecast.recompute_digest()

    with pytest.raises(ContractError, match="distribution"):
        AssessorForecast.create(
            forecast_id=StableIdentifier("forecast:formula-2"),
            assessor=AssessorKind.FORMULA,
            state=ForecastState.COMMITTED,
            evidence_digest="d" * 64,
            distribution=None,
            support=EvidenceSupport(0, "0", 0, None, 0),
            warnings=(),
            artifacts=(),
            abstention_code=None,
        )


def test_abstention_cannot_smuggle_a_point_estimate_or_distribution() -> None:
    abstained = AssessorForecast.create(
        forecast_id=StableIdentifier("forecast:ml-1"),
        assessor=AssessorKind.ML,
        state=ForecastState.ABSTAINED,
        evidence_digest="d" * 64,
        distribution=None,
        support=EvidenceSupport(0, "0", 0, None, 0),
        warnings=(ForecastWarning.INSUFFICIENT_SUPPORT,),
        artifacts=(),
        abstention_code="insufficient_support",
    )
    assert abstained.distribution is None
    with pytest.raises(ContractError, match="must be absent"):
        AssessorForecast.create(
            forecast_id=StableIdentifier("forecast:ml-2"),
            assessor=AssessorKind.ML,
            state=ForecastState.ABSTAINED,
            evidence_digest="d" * 64,
            distribution=_distribution(),
            support=EvidenceSupport(0, "0", 0, None, 0),
            warnings=(),
            artifacts=(),
            abstention_code="insufficient_support",
        )


def test_support_artifacts_and_committed_forecast_are_strictly_typed() -> None:
    with pytest.raises(ContractError, match="cannot exceed"):
        EvidenceSupport(1, "1", 2, None, 0)
    with pytest.raises(ContractError, match="canonical decimal"):
        EvidenceSupport(1, 1.0, 1, None, 0)  # type: ignore[arg-type]
    with pytest.raises(ContractError, match="canonical decimal"):
        EvidenceSupport(1, "1.0", 1, None, 0)
    with pytest.raises(ContractError, match="non-negative"):
        EvidenceSupport(1, "-1", 1, None, 0)
    with pytest.raises(ContractError, match="nonempty"):
        ArtifactIdentity("", "artifact:v1", "a" * 64)

    forecast = _committed_forecast()
    for changes, message in (
        ({"assessor": "formula"}, "AssessorKind"),
        ({"state": "committed"}, "ForecastState"),
        ({"support": object()}, "EvidenceSupport"),
        ({"warnings": [ForecastWarning.SPARSE_EVIDENCE]}, "immutable"),
        (
            {"warnings": (ForecastWarning.SPARSE_EVIDENCE, ForecastWarning.PRIOR_ONLY)},
            "unique and sorted",
        ),
        ({"artifacts": [forecast.artifacts[0]]}, "immutable"),
        ({"abstention_code": "not-allowed"}, "cannot carry"),
        ({"commit_digest": "0" * 64}, "digest mismatch"),
    ):
        with pytest.raises(ContractError, match=message):
            replace(forecast, **changes)

    encoded = forecast.to_dict()
    encoded["assessor"] = "invented"
    with pytest.raises(ContractError, match="unknown"):
        AssessorForecast.from_dict(encoded)

    with pytest.raises(ContractError, match="abstention code"):
        AssessorForecast.create(
            forecast_id=StableIdentifier("forecast:invalid-no-code"),
            assessor=AssessorKind.ML,
            state=ForecastState.INVALID,
            evidence_digest="d" * 64,
            distribution=None,
            support=EvidenceSupport(0, "0", 0, None, 0),
            warnings=(),
            artifacts=(),
            abstention_code=None,
        )


def test_llm_member_audit_records_replay_facts_without_raw_response() -> None:
    audit = LLMMemberAudit(
        prompt_digest="1" * 64,
        schema_version="llm-output:v1",
        runtime_version="ollama:1.2.3",
        model_digest="2" * 64,
        quantization="Q4_K_M",
        sampling_parameters_digest="3" * 64,
        raw_response_digest="4" * 64,
        validator_code="valid",
        latency_ms=913,
        provider_model_version="qwen3.5:9b-pinned",
        provider_fingerprint="5" * 64,
        api_revision=None,
        canary_digest="6" * 64,
    )
    assert LLMMemberAudit.from_dict(audit.to_dict()) == audit
    value = audit.to_dict()
    value["raw_response"] = "forbidden inline response"
    with pytest.raises(ContractError, match="fields"):
        LLMMemberAudit.from_dict(value)

    without_optional_provider_facts = replace(audit, provider_fingerprint=None, canary_digest=None)
    assert LLMMemberAudit.from_dict(without_optional_provider_facts.to_dict()) == (
        without_optional_provider_facts
    )
    with pytest.raises(ContractError, match="nonempty string"):
        replace(audit, runtime_version="")
    with pytest.raises(ContractError, match="api_revision"):
        replace(audit, api_revision="")
