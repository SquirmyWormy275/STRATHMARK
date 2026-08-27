from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from random import Random
from types import SimpleNamespace

import numpy as np
import pytest

from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.contracts.forecasts import (
    AssessorKind,
    PositiveTimeDistribution,
    QuantilePoint,
    SamplingSpec,
)
from strathmark.v3.domain import optimizer as optimizer_module
from strathmark.v3.domain.joint_dependence import _rank_uniform
from strathmark.v3.domain.optimizer import (
    OptimizationCompetitor,
    OptimizationField,
    _winner_credits_bitset,
)
from strathmark.v3.domain.optimizer_kernel import (
    KernelIntegrityError,
    NativeKernelContext,
    NativeOptimizerKernel,
    bundled_kernel_identity,
    load_bundled_kernel,
)
from strathmark.v3.domain.pooling import LinearPoolComponent, LinearPooledDistribution

NATIVE_ROOT = Path("strathmark/v3/native")
NATIVE_BINARY = NATIVE_ROOT / "strathmark_v3_optimizer_kernel.dll"
NATIVE_MANIFEST = NATIVE_ROOT / "optimizer_kernel_manifest.json"


def test_bundled_kernel_manifest_is_content_addressed() -> None:
    manifest = json.loads(NATIVE_MANIFEST.read_text("utf-8"))
    body = {key: value for key, value in manifest.items() if key != "manifest_digest"}

    assert manifest["manifest_digest"] == canonical_digest(body)
    assert bundled_kernel_identity() == manifest
    assert manifest["abi_version"] == 1
    assert manifest["algorithm"] == "exact-winner-spread-v1"
    assert manifest["sampling_algorithm"] == "exact-quantile-linear-pool-and-rank-uniform-u256-v4"
    assert manifest["source_sha256"] == _sha256(NATIVE_ROOT / "optimizer_kernel.rs")
    assert manifest["binary_sha256"] == _sha256(NATIVE_BINARY)


@pytest.mark.skipif(sys.platform != "win32", reason="bundled kernel is Windows x86-64")
def test_native_kernel_matches_python_authority_for_ties_and_extremes() -> None:
    random = Random(202_608_24)
    entrant_count = 12
    draw_count = 4096
    credit_scale = 27_720
    samples = np.asarray(
        [
            [
                (
                    40_000
                    if draw % 17 == 0
                    else 2_000_000_000 - entrant
                    if draw == 1
                    else random.randrange(1, 2_000_000_001)
                )
                for entrant in range(entrant_count)
            ]
            for draw in range(draw_count)
        ],
        dtype=np.int32,
    )
    delays = np.asarray(
        [
            [random.randrange(0, 181) * 1000 for _entrant in range(entrant_count)]
            for _candidate in range(73)
        ]
        + [[0] * entrant_count],
        dtype=np.int32,
    )
    expected_credits = _winner_credits_bitset(
        samples,
        delays,
        entrant_count=entrant_count,
        draw_count=draw_count,
        credit_scale=credit_scale,
    )
    finishes = samples[np.newaxis, :, :] + delays[:, np.newaxis, :]
    expected_spreads = np.sum(
        np.max(finishes, axis=2) - np.min(finishes, axis=2),
        axis=1,
        dtype=np.int64,
    )

    kernel = load_bundled_kernel(required=True)
    assert kernel is not None
    with kernel.context(samples) as context:
        spreads, credits = context.evaluate(delays, credit_scale=credit_scale)

    assert np.array_equal(spreads, expected_spreads)
    assert np.array_equal(credits, expected_credits)


@pytest.mark.skipif(sys.platform != "win32", reason="bundled kernel is Windows x86-64")
def test_native_pareto_dominance_matches_exact_python_relation() -> None:
    random = Random(202_608_25)
    sources = np.asarray(
        [[random.randrange(-50_000, 50_001) for _ in range(4)] for _ in range(337)],
        dtype=np.int64,
    )
    targets = np.asarray(
        [[random.randrange(-50_000, 50_001) for _ in range(4)] for _ in range(519)],
        dtype=np.int64,
    )
    nonstrict = np.asarray((0, 1, 0, 0), dtype=np.int64)
    strict = np.asarray((-1, -2, -1, -1), dtype=np.int64)
    initially_dominated = np.asarray(
        [index % 17 == 0 for index in range(len(targets))], dtype=np.bool_
    )
    expected = initially_dominated.copy()
    for target_index, target in enumerate(targets):
        if expected[target_index]:
            continue
        expected[target_index] = any(
            all(
                int(source[column]) <= int(target[column]) + int(nonstrict[column])
                for column in range(4)
            )
            and any(
                int(source[column]) <= int(target[column]) + int(strict[column])
                for column in range(4)
            )
            for source in sources
        )

    kernel = load_bundled_kernel(required=True)
    assert kernel is not None
    actual = initially_dominated.copy()
    kernel.mark_dominated(sources, targets, actual, nonstrict, strict)

    assert np.array_equal(actual, expected)


@pytest.mark.skipif(sys.platform != "win32", reason="bundled kernel is Windows x86-64")
def test_native_standard_quantile_sampler_matches_integer_oracle() -> None:
    pattern = (
        "0.0000000000000000000000000001",
        "0.1",
        "0.1000000000000000000000000001",
        "0.3",
        "0.4999999999999999999999999999",
        "0.5",
        "0.5000000000000000000000000001",
        "0.7",
        "0.9",
        "0.9999999999999999999999999999",
    )
    uniforms = tuple(pattern[index % len(pattern)] for index in range(4096))
    spec = SamplingSpec(17, len(uniforms), uniforms, "a" * 64)
    time_rows = ((24_000, 30_000, 42_000), (1, 1, 2_000_000_000))
    distributions = tuple(
        PositiveTimeDistribution(
            tuple(
                QuantilePoint(probability, time_ms)
                for probability, time_ms in zip(("0.1", "0.5", "0.9"), times, strict=True)
            )
        )
        for times in time_rows
    )
    expected = tuple(
        distribution._sample_scaled_probabilities(
            spec._scaled_common_uniforms,
            spec._common_uniform_exponent,
        )
        for distribution in distributions
    )

    kernel = load_bundled_kernel(required=True)
    assert kernel is not None
    actual = kernel.sample_three_quantiles(
        spec._standard_probability_words_le,
        time_rows,
        draw_count=spec.draw_count,
    )

    assert actual == expected


@pytest.mark.skipif(sys.platform != "win32", reason="bundled kernel is Windows x86-64")
def test_native_standard_linear_pool_sampler_matches_integer_oracle() -> None:
    weight_values = (
        "0.3333333333333333333333333333333333",
        "0.3333333333333333333333333333333333",
        "0.3333333333333333333333333333333334",
    )
    distributions = tuple(
        PositiveTimeDistribution(
            tuple(
                QuantilePoint(probability, time_ms)
                for probability, time_ms in zip(("0.1", "0.5", "0.9"), times, strict=True)
            )
        )
        for times in (
            (23_000, 30_000, 41_000),
            (31_000, 45_000, 70_000),
            (45_000, 65_000, 100_000),
        )
    )
    mixture = LinearPooledDistribution(
        tuple(
            LinearPoolComponent(kind, weight, distribution)
            for kind, weight, distribution in zip(
                (
                    AssessorKind.FORMULA,
                    AssessorKind.ML,
                    AssessorKind.LLM_COUNCIL,
                ),
                weight_values,
                distributions,
                strict=True,
            )
        )
    )
    pattern = (
        "0.0000000000000000000000000001",
        "0.1",
        "0.3333333333333333333333333332",
        "0.3333333333333333333333333334",
        "0.5",
        "0.6666666666666666666666666665",
        "0.6666666666666666666666666667",
        "0.9",
        "0.9999999999999999999999999999",
    )
    uniforms = tuple(pattern[index % len(pattern)] for index in range(4096))
    spec = SamplingSpec(29, 4096, uniforms, "b" * 64)
    expected = mixture._sample_scaled_python(spec).samples_ms

    kernel = load_bundled_kernel(required=True)
    assert kernel is not None
    actual = kernel.sample_linear_pool_three_quantiles(
        spec._standard_probability_words_le,
        weight_values,
        tuple(distribution._times_ms for distribution in distributions),
        draw_count=spec.draw_count,
    )

    assert actual == expected


@pytest.mark.skipif(sys.platform != "win32", reason="bundled kernel is Windows x86-64")
def test_native_seven_quantile_linear_pool_sampler_matches_integer_oracle() -> None:
    weight_values = (
        "0.33333333333333333333333333333333333333333333333333",
        "0.33333333333333333333333333333333333333333333333333",
        "0.33333333333333333333333333333333333333333333333334",
    )
    probability_values = ("0.05", "0.1", "0.25", "0.5", "0.75", "0.9", "0.95")
    distributions = tuple(
        PositiveTimeDistribution(
            tuple(
                QuantilePoint(probability, time_ms)
                for probability, time_ms in zip(probability_values, times, strict=True)
            )
        )
        for times in (
            (20_000, 23_000, 27_000, 31_000, 36_000, 42_000, 47_000),
            (28_000, 31_000, 36_000, 43_000, 52_000, 65_000, 79_000),
            (39_000, 44_000, 51_000, 62_000, 76_000, 95_000, 118_000),
        )
    )
    mixture = LinearPooledDistribution(
        tuple(
            LinearPoolComponent(kind, weight, distribution)
            for kind, weight, distribution in zip(
                (AssessorKind.FORMULA, AssessorKind.ML, AssessorKind.LLM_COUNCIL),
                weight_values,
                distributions,
                strict=True,
            )
        )
    )
    pattern = (
        "0.0000000000000000000000000001",
        "0.01",
        "0.2",
        "0.2000000000000000000000000001",
        "0.5",
        "0.95",
        "0.9999999999999999999999999999",
    )
    spec = SamplingSpec(
        41,
        4096,
        tuple(pattern[index % len(pattern)] for index in range(4096)),
        "d" * 64,
    )
    expected = mixture._sample_scaled_python(spec).samples_ms
    kernel = load_bundled_kernel(required=True)
    assert kernel is not None
    actual = kernel.sample_linear_pool_quantiles(
        spec._standard_probability_words_le,
        weight_values,
        probability_values,
        tuple(distribution._times_ms for distribution in distributions),
        draw_count=spec.draw_count,
    )
    assert actual == expected


@pytest.mark.skipif(sys.platform != "win32", reason="bundled kernel is Windows x86-64")
def test_native_independent_rank_uniforms_match_frozen_sha256_oracle() -> None:
    kernel = load_bundled_kernel(required=True)
    assert kernel is not None
    seed = 9_223_372_036_854_775_000
    actual = kernel.generate_independent_rank_uniforms(seed=seed, draw_count=4096, stream_count=12)
    assert actual == tuple(
        tuple(_rank_uniform(seed, draw, f"crn:{stream}") for draw in range(4096))
        for stream in range(12)
    )


def test_kernel_loader_rejects_binary_substitution_before_loading(tmp_path: Path) -> None:
    binary = tmp_path / NATIVE_BINARY.name
    manifest = tmp_path / NATIVE_MANIFEST.name
    shutil.copyfile(NATIVE_BINARY, binary)
    shutil.copyfile(NATIVE_MANIFEST, manifest)
    with binary.open("ab") as stream:
        stream.write(b"substitution")

    with pytest.raises(KernelIntegrityError, match="digest"):
        NativeOptimizerKernel.from_paths(binary=binary, manifest=manifest)


@pytest.mark.skipif(sys.platform != "win32", reason="bundled kernel is Windows x86-64")
def test_kernel_loader_seals_every_required_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / NATIVE_BINARY.name
    manifest = tmp_path / NATIVE_MANIFEST.name
    shutil.copyfile(NATIVE_BINARY, binary)
    shutil.copyfile(NATIVE_MANIFEST, manifest)

    def abi_version() -> int:
        return 1

    incomplete_library = SimpleNamespace(strathmark_v3_optimizer_kernel_abi_version=abi_version)
    monkeypatch.setattr(
        "strathmark.v3.domain.optimizer_kernel.ctypes.CDLL",
        lambda _path: incomplete_library,
    )

    with pytest.raises(KernelIntegrityError, match="sealed ABI"):
        NativeOptimizerKernel.from_paths(binary=binary, manifest=manifest)


@pytest.mark.skipif(sys.platform != "win32", reason="bundled kernel is Windows x86-64")
def test_large_candidate_evaluation_routes_through_verified_kernel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    samples = tuple(40_000 + (draw * 37) % 5_003 for draw in range(4096))
    field = OptimizationField.create(
        field_id="field:native-routing",
        source_receipt_digest="a" * 64,
        competitors=tuple(
            OptimizationCompetitor(
                f"competitor:native-{entrant}",
                160_000 - entrant * 10_000,
                tuple(value + entrant * 10_000 for value in samples),
                entrant,
            )
            for entrant in range(12)
        ),
    )
    baseline = tuple(3 + entrant * 10 for entrant in range(12))
    candidates = tuple((*baseline[:-1], baseline[-1] + index % 2) for index in range(40))
    kernel = load_bundled_kernel(required=True)
    assert kernel is not None
    monkeypatch.setattr(optimizer_module, "_NATIVE_OPTIMIZER_KERNEL", None)
    python_context = optimizer_module._compile_evaluation_context(field, baseline)
    expected = optimizer_module._evaluate_candidates_impl(
        field,
        candidates,
        baseline,
        3,
        parallel=False,
        raw=True,
        _context=python_context,
    )
    monkeypatch.setattr(optimizer_module, "_NATIVE_OPTIMIZER_KERNEL", kernel)
    calls = 0
    original = NativeKernelContext.evaluate

    def counted(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(NativeKernelContext, "evaluate", counted)
    native_context = optimizer_module._compile_evaluation_context(field, baseline)
    actual = optimizer_module._evaluate_candidates_impl(
        field,
        candidates,
        baseline,
        3,
        parallel=False,
        raw=True,
        _context=native_context,
    )

    assert actual == expected
    assert calls == 1


def _sha256(path: Path) -> str:
    from hashlib import sha256

    return sha256(path.read_bytes()).hexdigest()
