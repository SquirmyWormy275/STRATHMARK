"""Immutable pseudonymous evidence and target-context contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from strathmark.v3.contracts.canonical import (
    CANONICALIZATION_VERSION,
    canonical_decimal_string,
    canonical_digest,
)
from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.identifiers import StableIdentifier, require_identifier
from strathmark.v3.contracts.statuses import (
    OfficialResult,
    ResultStatus,
    _require_fields,
    _require_nonnegative_int,
    _require_positive_int,
    _require_schema,
    admit_raw_completion,
)

EVIDENCE_SCHEMA_VERSION = "strathmark-v3-evidence-packet-v1"
CONTEXT_SCHEMA_VERSION = "strathmark-v3-target-context-v1"
OBSERVATION_SCHEMA_VERSION = "strathmark-v3-result-observation-v1"
_TOKEN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_VERSION = re.compile(r"^[a-z][a-z0-9_.-]{0,31}:[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_UTC_MILLISECONDS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True, order=True)
class ContextProperty:
    """One versioned numeric context fact with explicit missingness."""

    code: str
    value: str | None
    unit: str
    missing_reason: str | None

    def __post_init__(self) -> None:
        _require_token(self.code, "property code")
        _require_token(self.unit, "property unit")
        if self.value is None:
            if self.missing_reason is None:
                raise ContractError("missing context values require an explicit missing_reason")
            _require_token(self.missing_reason, "missing_reason")
        else:
            if not isinstance(self.value, str):
                raise ContractError("context property value must be a canonical decimal string")
            if canonical_decimal_string(self.value) != self.value:
                raise ContractError("context property value must be a canonical decimal string")
            if self.missing_reason is not None:
                raise ContractError("missing_reason must be absent when a value is present")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "value": self.value,
            "unit": self.unit,
            "missing_reason": self.missing_reason,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ContextProperty:
        _require_fields(value, {"code", "value", "unit", "missing_reason"})
        return cls(value["code"], value["value"], value["unit"], value["missing_reason"])


@dataclass(frozen=True, slots=True)
class TargetContext:
    """Canonical event/material task context, independent of a field roster."""

    event_code: str
    size_mm: int
    material_code: str
    taxonomy_version: str
    conversion_version: str
    properties: tuple[ContextProperty, ...] = ()

    def __post_init__(self) -> None:
        _require_token(self.event_code, "event_code")
        _require_positive_int(self.size_mm, "size_mm")
        _require_token(self.material_code, "material_code")
        _require_version(self.taxonomy_version, "taxonomy_version")
        _require_version(self.conversion_version, "conversion_version")
        if not isinstance(self.properties, tuple) or not all(
            isinstance(item, ContextProperty) for item in self.properties
        ):
            raise ContractError("properties must be an immutable tuple of ContextProperty")
        codes = tuple(item.code for item in self.properties)
        if codes != tuple(sorted(codes)) or len(codes) != len(set(codes)):
            raise ContractError("context properties must be unique and sorted by code")

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CONTEXT_SCHEMA_VERSION,
            "event_code": self.event_code,
            "size_mm": self.size_mm,
            "material_code": self.material_code,
            "taxonomy_version": self.taxonomy_version,
            "conversion_version": self.conversion_version,
            "properties": [item.to_dict() for item in self.properties],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> TargetContext:
        _require_fields(
            value,
            {
                "schema_version",
                "event_code",
                "size_mm",
                "material_code",
                "taxonomy_version",
                "conversion_version",
                "properties",
            },
        )
        _require_schema(value["schema_version"], CONTEXT_SCHEMA_VERSION)
        properties = value["properties"]
        if not isinstance(properties, list):
            raise ContractError("properties must be a JSON array")
        return cls(
            event_code=value["event_code"],
            size_mm=value["size_mm"],
            material_code=value["material_code"],
            taxonomy_version=value["taxonomy_version"],
            conversion_version=value["conversion_version"],
            properties=tuple(ContextProperty.from_dict(item) for item in properties),
        )


@dataclass(frozen=True, slots=True)
class ResultObservation:
    """One immutable result revision entering the evidence governor."""

    evidence_id: StableIdentifier
    competitor_id: StableIdentifier
    tournament_id: StableIdentifier
    round_id: StableIdentifier
    field_id: StableIdentifier
    context: TargetContext
    observation_sequence: int
    occurred_at_utc: str
    issued_mark: int
    completion_clock_ms: int | None
    placing: int | None
    gap_ms: int | None
    result: OfficialResult
    source_digest: str

    def __post_init__(self) -> None:
        _require_id(self.evidence_id, "evidence")
        _require_id(self.competitor_id, "competitor")
        _require_id(self.tournament_id, "tournament")
        _require_id(self.round_id, "round")
        _require_id(self.field_id, "field")
        if not isinstance(self.context, TargetContext):
            raise ContractError("context must be a TargetContext")
        _require_positive_int(self.observation_sequence, "observation_sequence")
        _require_utc(self.occurred_at_utc)
        _require_positive_int(self.issued_mark, "issued_mark")
        if self.completion_clock_ms is not None:
            _require_positive_int(self.completion_clock_ms, "completion_clock_ms")
        if self.placing is not None:
            _require_positive_int(self.placing, "placing")
        if self.gap_ms is not None:
            _require_nonnegative_int(self.gap_ms, "gap_ms")
        if not isinstance(self.result, OfficialResult):
            raise ContractError("result must be an OfficialResult")
        if self.result.status in {
            ResultStatus.DNF,
            ResultStatus.DQ,
            ResultStatus.DNS,
            ResultStatus.VOID,
        } and any(
            item is not None for item in (self.completion_clock_ms, self.placing, self.gap_ms)
        ):
            raise ContractError(
                "nonfinish and void observations cannot carry completion, placing, or gap facts"
            )
        _require_digest(self.source_digest, "source_digest")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": OBSERVATION_SCHEMA_VERSION,
            "evidence_id": str(self.evidence_id),
            "competitor_id": str(self.competitor_id),
            "tournament_id": str(self.tournament_id),
            "round_id": str(self.round_id),
            "field_id": str(self.field_id),
            "context": self.context.to_dict(),
            "observation_sequence": self.observation_sequence,
            "occurred_at_utc": self.occurred_at_utc,
            "issued_mark": self.issued_mark,
            "completion_clock_ms": self.completion_clock_ms,
            "placing": self.placing,
            "gap_ms": self.gap_ms,
            "result": self.result.to_dict(),
            "source_digest": self.source_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ResultObservation:
        expected = {
            "schema_version",
            "evidence_id",
            "competitor_id",
            "tournament_id",
            "round_id",
            "field_id",
            "context",
            "observation_sequence",
            "occurred_at_utc",
            "issued_mark",
            "completion_clock_ms",
            "placing",
            "gap_ms",
            "result",
            "source_digest",
        }
        _require_fields(value, expected)
        _require_schema(value["schema_version"], OBSERVATION_SCHEMA_VERSION)
        return cls(
            evidence_id=require_identifier(value["evidence_id"], expected_namespace="evidence"),
            competitor_id=require_identifier(
                value["competitor_id"], expected_namespace="competitor"
            ),
            tournament_id=require_identifier(
                value["tournament_id"], expected_namespace="tournament"
            ),
            round_id=require_identifier(value["round_id"], expected_namespace="round"),
            field_id=require_identifier(value["field_id"], expected_namespace="field"),
            context=TargetContext.from_dict(value["context"]),
            observation_sequence=value["observation_sequence"],
            occurred_at_utc=value["occurred_at_utc"],
            issued_mark=value["issued_mark"],
            completion_clock_ms=value["completion_clock_ms"],
            placing=value["placing"],
            gap_ms=value["gap_ms"],
            result=OfficialResult.from_dict(value["result"]),
            source_digest=value["source_digest"],
        )


@dataclass(frozen=True, slots=True)
class EvidencePacket:
    """Sealed assessor input: one competitor, one context, one causal epoch."""

    competitor_id: StableIdentifier
    target_context: TargetContext
    observations: tuple[ResultObservation, ...]
    taxonomy_version: str
    conversion_version: str
    canonicalization_version: str
    historical_cutoff_key: str
    tournament_epoch_id: StableIdentifier
    tournament_event_sequence: int
    content_digest: str
    schema_version: str = EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version, EVIDENCE_SCHEMA_VERSION)
        _require_id(self.competitor_id, "competitor")
        if not isinstance(self.target_context, TargetContext):
            raise ContractError("target_context must be a TargetContext")
        if not isinstance(self.observations, tuple) or not all(
            isinstance(item, ResultObservation) for item in self.observations
        ):
            raise ContractError("observations must be an immutable observation tuple")
        _require_version(self.taxonomy_version, "taxonomy_version")
        _require_version(self.conversion_version, "conversion_version")
        if self.taxonomy_version != self.target_context.taxonomy_version:
            raise ContractError("packet taxonomy_version must match target_context")
        if self.conversion_version != self.target_context.conversion_version:
            raise ContractError("packet conversion_version must match target_context")
        _require_schema(self.canonicalization_version, CANONICALIZATION_VERSION)
        require_identifier(self.historical_cutoff_key, expected_namespace="history")
        _require_id(self.tournament_epoch_id, "epoch")
        _require_nonnegative_int(self.tournament_event_sequence, "tournament_event_sequence")
        sequences = tuple(item.observation_sequence for item in self.observations)
        if sequences != tuple(sorted(sequences)) or len(sequences) != len(set(sequences)):
            raise ContractError("observation sequences must be unique and sorted")
        if sequences and sequences[-1] > self.tournament_event_sequence:
            raise ContractError("observation sequence exceeds the sealed tournament sequence")
        if any(item.competitor_id != self.competitor_id for item in self.observations):
            raise ContractError("every observation must match the packet competitor")
        _require_digest(self.content_digest, "content_digest")
        if self.content_digest != self.recompute_digest():
            raise ContractError("evidence packet content digest mismatch")

    @classmethod
    def create(
        cls,
        *,
        competitor_id: StableIdentifier,
        target_context: TargetContext,
        observations: tuple[ResultObservation, ...],
        taxonomy_version: str,
        conversion_version: str,
        historical_cutoff_key: str,
        tournament_epoch_id: StableIdentifier,
        tournament_event_sequence: int,
    ) -> EvidencePacket:
        arguments = {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "competitor_id": str(competitor_id),
            "target_context": target_context.to_dict(),
            "observations": [item.to_dict() for item in observations],
            "taxonomy_version": taxonomy_version,
            "conversion_version": conversion_version,
            "canonicalization_version": CANONICALIZATION_VERSION,
            "historical_cutoff_key": historical_cutoff_key,
            "tournament_epoch_id": str(tournament_epoch_id),
            "tournament_event_sequence": tournament_event_sequence,
        }
        return cls(
            competitor_id=competitor_id,
            target_context=target_context,
            observations=observations,
            taxonomy_version=taxonomy_version,
            conversion_version=conversion_version,
            canonicalization_version=CANONICALIZATION_VERSION,
            historical_cutoff_key=historical_cutoff_key,
            tournament_epoch_id=tournament_epoch_id,
            tournament_event_sequence=tournament_event_sequence,
            content_digest=canonical_digest(arguments),
        )

    @property
    def eligible_raw_times_ms(self) -> tuple[int, ...]:
        admitted = (admit_raw_completion(item.result) for item in self.observations)
        return tuple(item.raw_time_ms for item in admitted if item is not None)

    def _content_value(self) -> dict[str, Any]:
        value = self.to_dict()
        del value["content_digest"]
        return value

    def recompute_digest(self) -> str:
        return canonical_digest(self._content_value())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "competitor_id": str(self.competitor_id),
            "target_context": self.target_context.to_dict(),
            "observations": [item.to_dict() for item in self.observations],
            "taxonomy_version": self.taxonomy_version,
            "conversion_version": self.conversion_version,
            "canonicalization_version": self.canonicalization_version,
            "historical_cutoff_key": self.historical_cutoff_key,
            "tournament_epoch_id": str(self.tournament_epoch_id),
            "tournament_event_sequence": self.tournament_event_sequence,
            "content_digest": self.content_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EvidencePacket:
        expected = {
            "schema_version",
            "competitor_id",
            "target_context",
            "observations",
            "taxonomy_version",
            "conversion_version",
            "canonicalization_version",
            "historical_cutoff_key",
            "tournament_epoch_id",
            "tournament_event_sequence",
            "content_digest",
        }
        _require_fields(value, expected)
        _require_schema(value["schema_version"], EVIDENCE_SCHEMA_VERSION)
        observations = value["observations"]
        if not isinstance(observations, list):
            raise ContractError("observations must be a JSON array")
        return cls(
            competitor_id=require_identifier(
                value["competitor_id"], expected_namespace="competitor"
            ),
            target_context=TargetContext.from_dict(value["target_context"]),
            observations=tuple(ResultObservation.from_dict(item) for item in observations),
            taxonomy_version=value["taxonomy_version"],
            conversion_version=value["conversion_version"],
            canonicalization_version=value["canonicalization_version"],
            historical_cutoff_key=value["historical_cutoff_key"],
            tournament_epoch_id=require_identifier(
                value["tournament_epoch_id"], expected_namespace="epoch"
            ),
            tournament_event_sequence=value["tournament_event_sequence"],
            content_digest=value["content_digest"],
            schema_version=value["schema_version"],
        )


def _require_token(value: object, label: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ContractError(f"{label} must be a bounded lower-case token")
    return value


def _require_version(value: object, label: str) -> str:
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise ContractError(f"{label} must be a bounded version identifier")
    return value


def _require_id(value: object, namespace: str) -> StableIdentifier:
    if not isinstance(value, StableIdentifier):
        raise ContractError(f"{namespace} identity must be a StableIdentifier")
    return require_identifier(value, expected_namespace=namespace)


def _require_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ContractError(f"{label} must be a lower-case SHA-256 digest")
    return value


def _require_utc(value: object) -> str:
    if not isinstance(value, str) or not _UTC_MILLISECONDS.fullmatch(value):
        raise ContractError("occurred_at_utc must be canonical UTC with milliseconds")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as exc:
        raise ContractError("occurred_at_utc must be a valid canonical UTC instant") from exc
    return value


__all__ = [
    "CONTEXT_SCHEMA_VERSION",
    "EVIDENCE_SCHEMA_VERSION",
    "OBSERVATION_SCHEMA_VERSION",
    "ContextProperty",
    "EvidencePacket",
    "ResultObservation",
    "TargetContext",
]
