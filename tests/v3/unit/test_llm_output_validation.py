from __future__ import annotations

import json

import pytest

from strathmark.v3.assessors.output_validation import (
    LLM_OUTPUT_SCHEMA_VERSION,
    LLMOutputError,
    validate_member_output,
)


def _valid() -> dict[str, object]:
    return {
        "schema_version": LLM_OUTPUT_SCHEMA_VERSION,
        "state": "committed",
        "quantiles": [
            {"probability": probability, "time_ms": time_ms}
            for probability, time_ms in (
                ("0.05", 30_000),
                ("0.1", 32_000),
                ("0.25", 35_000),
                ("0.5", 40_000),
                ("0.75", 46_000),
                ("0.9", 52_000),
                ("0.95", 58_000),
            )
        ],
        "evidence_refs": ["obs_a", "obs_b"],
        "warnings": ["rapid_change"],
        "fact_codes": ["observed_raw_time", "target_context"],
        "abstention_reason": None,
    }


def test_strict_member_schema_returns_typed_distribution() -> None:
    result = validate_member_output(
        json.dumps(_valid()).encode(),
        expected_evidence_refs=("obs_a", "obs_b"),
        allowed_fact_codes=("observed_raw_time", "target_context"),
    )
    assert result.valid
    assert result.validator_code == "valid_committed"
    assert result.distribution is not None
    assert result.distribution.median_ms == 40_000
    assert result.evidence_refs == ("obs_a", "obs_b")
    assert result.warnings == ("rapid_change",)


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda value: value.update(extra=True), "schema_fields"),
        (lambda value: value.pop("warnings"), "schema_fields"),
        (
            lambda value: value.update(evidence_refs=["obs_a", "obs_a"]),
            "duplicate_evidence_ref",
        ),
        (lambda value: value.update(evidence_refs=["obs_a"]), "missing_evidence_ref"),
        (
            lambda value: value.update(evidence_refs=["obs_a", "obs_b", "obs_x"]),
            "unknown_evidence_ref",
        ),
        (
            lambda value: value["quantiles"][3].update(time_ms=29_000),
            "invalid_quantiles",
        ),
        (
            lambda value: value.update(fact_codes=["invented_coaching_story"]),
            "fabricated_fact",
        ),
    ],
)
def test_invalid_outputs_fail_without_repair(mutation, code: str) -> None:
    value = _valid()
    mutation(value)
    with pytest.raises(LLMOutputError) as captured:
        validate_member_output(
            json.dumps(value).encode(),
            expected_evidence_refs=("obs_a", "obs_b"),
            allowed_fact_codes=("observed_raw_time", "target_context"),
        )
    assert captured.value.code == code


def test_semantic_abstention_is_explicit_and_has_no_numeric_output() -> None:
    value = _valid()
    value.update(
        state="abstained",
        quantiles=[],
        evidence_refs=[],
        warnings=["insufficient_support"],
        fact_codes=[],
        abstention_reason="insufficient_numeric_evidence",
    )
    result = validate_member_output(
        json.dumps(value).encode(),
        expected_evidence_refs=("obs_a", "obs_b"),
        allowed_fact_codes=("observed_raw_time", "target_context"),
    )
    assert result.valid
    assert result.distribution is None
    assert result.abstention_reason == "insufficient_numeric_evidence"
    assert result.validator_code == "valid_abstention"
    assert result.abstained


@pytest.mark.parametrize(
    "payload",
    [b"not-json", b"[]", b'{"schema_version":'],
)
def test_malformed_or_nonobject_json_is_rejected(payload: bytes) -> None:
    with pytest.raises(LLMOutputError):
        validate_member_output(payload, expected_evidence_refs=(), allowed_fact_codes=())


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"schema_version": "wrong"}, "schema_version"),
        ({"warnings": ["outside_story"]}, "unknown_warning"),
        ({"warnings": ["rapid_change", "missing_context"]}, "warning_order"),
        ({"warnings": ["rapid_change", "rapid_change"]}, "warning_order"),
        ({"fact_codes": ["target_context", "observed_raw_time"]}, "fact_order"),
        ({"fact_codes": ["target_context", "target_context"]}, "fact_order"),
        ({"state": "maybe"}, "state"),
        (
            {"abstention_reason": "insufficient_numeric_evidence"},
            "committed_abstention_reason",
        ),
        ({"quantiles": []}, "invalid_quantiles"),
    ],
)
def test_closed_schema_rejects_every_other_semantic_failure(change, code: str) -> None:
    value = _valid()
    value.update(change)
    with pytest.raises(LLMOutputError) as captured:
        validate_member_output(
            json.dumps(value).encode(),
            expected_evidence_refs=("obs_a", "obs_b"),
            allowed_fact_codes=("observed_raw_time", "target_context"),
        )
    assert captured.value.code == code


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (
            {"quantiles": [{"probability": "0.06", "time_ms": 30_000}] * 7},
            "invalid_quantiles",
        ),
        ({"evidence_refs": "obs_a"}, "evidence_ref"),
        ({"evidence_refs": ["bad ref"]}, "evidence_ref"),
        ({"warnings": "rapid_change"}, "warning"),
        ({"warnings": ["BAD"]}, "warning"),
        ({"fact_codes": "target_context"}, "fact_code"),
    ],
)
def test_nested_values_must_have_exact_json_types(change, code: str) -> None:
    value = _valid()
    value.update(change)
    with pytest.raises(LLMOutputError) as captured:
        validate_member_output(
            json.dumps(value).encode(),
            expected_evidence_refs=("obs_a", "obs_b"),
            allowed_fact_codes=("observed_raw_time", "target_context"),
        )
    assert captured.value.code == code


def test_expected_allowlists_are_typed_and_response_size_is_bounded() -> None:
    raw = json.dumps(_valid()).encode()
    with pytest.raises(LLMOutputError, match="expected_evidence_ref"):
        validate_member_output(
            raw,
            expected_evidence_refs=("bad ref",),
            allowed_fact_codes=("observed_raw_time",),
        )
    with pytest.raises(LLMOutputError, match="allowed_fact_code"):
        validate_member_output(
            raw,
            expected_evidence_refs=("obs_a", "obs_b"),
            allowed_fact_codes=("BAD",),
        )
    for payload in (b"", b"x" * (64 * 1024 + 1)):
        with pytest.raises(LLMOutputError, match="response_size"):
            validate_member_output(payload, expected_evidence_refs=(), allowed_fact_codes=())
    with pytest.raises(LLMOutputError, match="response_size"):
        validate_member_output("not-bytes", expected_evidence_refs=(), allowed_fact_codes=())  # type: ignore[arg-type]


def test_committed_forecast_cannot_claim_zero_evidence() -> None:
    value = _valid()
    value["evidence_refs"] = []
    with pytest.raises(LLMOutputError, match="committed_without_evidence"):
        validate_member_output(
            json.dumps(value).encode(),
            expected_evidence_refs=(),
            allowed_fact_codes=("observed_raw_time", "target_context"),
        )


@pytest.mark.parametrize("change", [{"quantiles": [1]}, {"evidence_refs": [1]}, {"warnings": [1]}])
def test_non_string_nested_members_are_rejected(change) -> None:
    value = _valid()
    value.update(change)
    with pytest.raises(LLMOutputError):
        validate_member_output(
            json.dumps(value).encode(),
            expected_evidence_refs=("obs_a", "obs_b"),
            allowed_fact_codes=("observed_raw_time", "target_context"),
        )


@pytest.mark.parametrize(
    "change",
    [
        {"quantiles": _valid()["quantiles"]},
        {"evidence_refs": ["obs_a"]},
        {"fact_codes": ["target_context"]},
        {"abstention_reason": "invented_reason"},
    ],
)
def test_abstention_cannot_smuggle_numeric_or_narrative_content(change) -> None:
    value = _valid()
    value.update(
        state="abstained",
        quantiles=[],
        evidence_refs=[],
        warnings=[],
        fact_codes=[],
        abstention_reason="insufficient_numeric_evidence",
    )
    value.update(change)
    with pytest.raises(LLMOutputError):
        validate_member_output(
            json.dumps(value).encode(),
            expected_evidence_refs=("obs_a", "obs_b"),
            allowed_fact_codes=("observed_raw_time", "target_context"),
        )
