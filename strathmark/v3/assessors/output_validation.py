"""Deterministic validation for untrusted LLM member responses.

The provider output is data, never executable control flow.  This module owns the
closed response schema and deliberately performs no repair: an invalid first attempt
remains invalid even when a separately retained correction attempt succeeds.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.forecasts import PositiveTimeDistribution, QuantilePoint

LLM_OUTPUT_SCHEMA_VERSION = "strathmark-v3-llm-member-output-v1"
MAX_LLM_OUTPUT_BYTES = 64 * 1024
REQUIRED_QUANTILES = ("0.05", "0.1", "0.25", "0.5", "0.75", "0.9", "0.95")
_TOKEN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_FIELDS = {
    "schema_version",
    "state",
    "quantiles",
    "evidence_refs",
    "warnings",
    "fact_codes",
    "abstention_reason",
}
_WARNINGS = frozenset(
    {
        "conflicting_evidence",
        "insufficient_support",
        "missing_context",
        "rapid_change",
        "sparse_evidence",
    }
)
_ABSTENTION_REASONS = frozenset(
    {
        "conflicting_numeric_evidence",
        "insufficient_numeric_evidence",
        "unsupported_context",
    }
)


class LLMOutputError(ValueError):
    """One bounded, machine-readable deterministic validation failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ValidatedMemberOutput:
    valid: bool
    validator_code: str
    distribution: PositiveTimeDistribution | None
    evidence_refs: tuple[str, ...]
    warnings: tuple[str, ...]
    fact_codes: tuple[str, ...]
    abstention_reason: str | None

    @property
    def abstained(self) -> bool:
        return self.distribution is None


def validate_member_output(
    raw: bytes,
    *,
    expected_evidence_refs: Sequence[str],
    allowed_fact_codes: Sequence[str],
) -> ValidatedMemberOutput:
    """Validate exact JSON bytes against the closed semantic response contract."""

    value = _decode(raw)
    if set(value) != _FIELDS:
        raise LLMOutputError("schema_fields")
    if value["schema_version"] != LLM_OUTPUT_SCHEMA_VERSION:
        raise LLMOutputError("schema_version")
    expected = _references(expected_evidence_refs, "expected_evidence_ref")
    allowed_facts = _tokens(allowed_fact_codes, "allowed_fact_code")
    references = _references(value["evidence_refs"], "evidence_ref")
    warnings = _tokens(value["warnings"], "warning")
    facts = _tokens(value["fact_codes"], "fact_code")
    if len(references) != len(set(references)):
        raise LLMOutputError("duplicate_evidence_ref")
    unknown = set(references).difference(expected)
    if unknown:
        raise LLMOutputError("unknown_evidence_ref")
    if any(item not in _WARNINGS for item in warnings):
        raise LLMOutputError("unknown_warning")
    if tuple(sorted(warnings)) != warnings or len(set(warnings)) != len(warnings):
        raise LLMOutputError("warning_order")
    if any(item not in allowed_facts for item in facts):
        raise LLMOutputError("fabricated_fact")
    if tuple(sorted(facts)) != facts or len(set(facts)) != len(facts):
        raise LLMOutputError("fact_order")

    state = value["state"]
    if state == "abstained":
        if value["quantiles"] != [] or references or facts:
            raise LLMOutputError("abstention_payload")
        reason = value["abstention_reason"]
        if reason not in _ABSTENTION_REASONS:
            raise LLMOutputError("abstention_reason")
        return ValidatedMemberOutput(True, "valid_abstention", None, (), warnings, (), reason)
    if state != "committed":
        raise LLMOutputError("state")
    if value["abstention_reason"] is not None:
        raise LLMOutputError("committed_abstention_reason")
    if not expected:
        raise LLMOutputError("committed_without_evidence")
    if references != expected:
        raise LLMOutputError("missing_evidence_ref")
    distribution = _distribution(value["quantiles"])
    return ValidatedMemberOutput(
        True,
        "valid_committed",
        distribution,
        references,
        warnings,
        facts,
        None,
    )


def _decode(raw: bytes) -> Mapping[str, Any]:
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_LLM_OUTPUT_BYTES:
        raise LLMOutputError("response_size")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LLMOutputError("invalid_json") from exc
    if not isinstance(value, dict):
        raise LLMOutputError("json_object")
    return value


def _distribution(value: object) -> PositiveTimeDistribution:
    if not isinstance(value, list) or len(value) != len(REQUIRED_QUANTILES):
        raise LLMOutputError("invalid_quantiles")
    try:
        points = tuple(QuantilePoint.from_dict(item) for item in value)
        if tuple(item.probability for item in points) != REQUIRED_QUANTILES:
            raise LLMOutputError("invalid_quantiles")
        return PositiveTimeDistribution(points)
    except LLMOutputError:
        raise
    except (ContractError, TypeError, ValueError) as exc:
        raise LLMOutputError("invalid_quantiles") from exc


def _references(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) or _REFERENCE.fullmatch(item) is None for item in value
    ):
        raise LLMOutputError(label)
    return tuple(value)


def _tokens(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) or _TOKEN.fullmatch(item) is None for item in value
    ):
        raise LLMOutputError(label)
    return tuple(value)


__all__ = [
    "LLM_OUTPUT_SCHEMA_VERSION",
    "LLMOutputError",
    "MAX_LLM_OUTPUT_BYTES",
    "REQUIRED_QUANTILES",
    "ValidatedMemberOutput",
    "validate_member_output",
]
