"""Causal evidence epochs and mandatory derivation-barrier rules."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.identifiers import (
    StableIdentifier,
    deterministic_identifier,
    require_identifier,
)


class MandatoryReaction(str, Enum):
    CAPABILITY = "capability"
    SCORING = "scoring"
    COVERAGE = "coverage"
    WEIGHTS = "weights"
    INVALIDATION = "invalidation"
    READINESS = "readiness"


@dataclass(frozen=True, slots=True, order=True)
class EpochMember:
    result_key: str
    revision: int
    source_sequence: int
    numeric_eligible: bool

    def __post_init__(self) -> None:
        require_identifier(self.result_key, expected_namespace="result")
        for value, label in (
            (self.revision, "revision"),
            (self.source_sequence, "source_sequence"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ContractError(f"epoch member {label} must be positive")
        if not isinstance(self.numeric_eligible, bool):
            raise ContractError("epoch eligibility must be explicit")

    def to_dict(self) -> dict[str, object]:
        return {
            "result_key": self.result_key,
            "revision": self.revision,
            "source_sequence": self.source_sequence,
            "numeric_eligible": self.numeric_eligible,
        }


@dataclass(frozen=True, slots=True)
class ReactionBarrier:
    through_sequence: int
    completed_reactions: frozenset[MandatoryReaction]

    def __post_init__(self) -> None:
        if (
            isinstance(self.through_sequence, bool)
            or not isinstance(self.through_sequence, int)
            or self.through_sequence < 0
        ):
            raise ContractError("barrier sequence must be non-negative")
        if not isinstance(self.completed_reactions, frozenset) or any(
            not isinstance(item, MandatoryReaction) for item in self.completed_reactions
        ):
            raise ContractError("barrier reactions must use the closed vocabulary")

    @classmethod
    def complete_through(cls, sequence: int) -> ReactionBarrier:
        return cls(sequence, frozenset(MandatoryReaction))

    @property
    def complete(self) -> bool:
        return self.completed_reactions == frozenset(MandatoryReaction)


@dataclass(frozen=True, slots=True)
class EvidenceEpoch:
    epoch_id: StableIdentifier
    round_id: StableIdentifier
    epoch_revision: int
    historical_cutoff_key: str
    maximum_tournament_sequence: int
    members: tuple[EpochMember, ...]
    content_digest: str

    def __post_init__(self) -> None:
        require_identifier(self.epoch_id, expected_namespace="epoch")
        require_identifier(self.round_id, expected_namespace="round")
        if (
            isinstance(self.epoch_revision, bool)
            or not isinstance(self.epoch_revision, int)
            or self.epoch_revision <= 0
        ):
            raise ContractError("epoch revision must be positive")
        require_identifier(self.historical_cutoff_key, expected_namespace="history")
        if (
            isinstance(self.maximum_tournament_sequence, bool)
            or not isinstance(self.maximum_tournament_sequence, int)
            or self.maximum_tournament_sequence < 0
        ):
            raise ContractError("epoch maximum sequence must be non-negative")
        if not isinstance(self.members, tuple) or any(
            not isinstance(item, EpochMember) for item in self.members
        ):
            raise ContractError("epoch members must be immutable")
        keys = tuple(item.result_key for item in self.members)
        if keys != tuple(sorted(keys)):
            raise ContractError("epoch members must be sorted by result key")
        if len(keys) != len(set(keys)):
            raise ContractError("epoch cannot contain duplicate result revisions")
        if any(item.source_sequence > self.maximum_tournament_sequence for item in self.members):
            raise ContractError("epoch member exceeds its causal boundary")
        if self.content_digest != canonical_digest(self.content_value()):
            raise ContractError("epoch content digest mismatch")
        if self.epoch_id != deterministic_identifier("epoch", self.content_value()):
            raise ContractError("epoch identity is not content addressed")

    def content_value(self) -> dict[str, object]:
        return {
            "schema_version": "strathmark-v3-evidence-epoch-v1",
            "round_id": str(self.round_id),
            "epoch_revision": self.epoch_revision,
            "historical_cutoff_key": self.historical_cutoff_key,
            "maximum_tournament_sequence": self.maximum_tournament_sequence,
            "members": [item.to_dict() for item in self.members],
        }


def freeze_epoch(
    *,
    round_id: StableIdentifier,
    epoch_revision: int,
    historical_cutoff_key: str,
    closed_through_sequence: int,
    members: tuple[EpochMember, ...],
    barrier: ReactionBarrier,
) -> EvidenceEpoch:
    """Seal one round epoch only after all mandatory reactions cross its boundary."""

    require_identifier(round_id, expected_namespace="round")
    if not isinstance(barrier, ReactionBarrier):
        raise ContractError("epoch freeze requires a reaction barrier")
    if (
        isinstance(closed_through_sequence, bool)
        or not isinstance(closed_through_sequence, int)
        or closed_through_sequence < 0
    ):
        raise ContractError("closed tournament sequence must be non-negative")
    if any(item.source_sequence > closed_through_sequence for item in members):
        raise ContractError("epoch member exceeds the explicit round-closure boundary")
    if not barrier.complete or barrier.through_sequence < closed_through_sequence:
        raise ContractError("mandatory derivation barrier has not crossed the epoch boundary")
    content = {
        "schema_version": "strathmark-v3-evidence-epoch-v1",
        "round_id": str(round_id),
        "epoch_revision": epoch_revision,
        "historical_cutoff_key": historical_cutoff_key,
        "maximum_tournament_sequence": closed_through_sequence,
        "members": [item.to_dict() for item in members],
    }
    return EvidenceEpoch(
        deterministic_identifier("epoch", content),
        round_id,
        epoch_revision,
        historical_cutoff_key,
        closed_through_sequence,
        members,
        canonical_digest(content),
    )


def correction_destination(
    first_issue_sequence: int | None,
    correction_sequence: int,
    current_round_id: StableIdentifier,
    next_round_id: StableIdentifier,
) -> StableIdentifier:
    """Apply the round-level first-issue seal from R2.13."""

    require_identifier(current_round_id, expected_namespace="round")
    require_identifier(next_round_id, expected_namespace="round")
    if (
        isinstance(correction_sequence, bool)
        or not isinstance(correction_sequence, int)
        or correction_sequence <= 0
    ):
        raise ContractError("correction sequence must be positive")
    if first_issue_sequence is None:
        return current_round_id
    if (
        isinstance(first_issue_sequence, bool)
        or not isinstance(first_issue_sequence, int)
        or first_issue_sequence <= 0
    ):
        raise ContractError("first issue sequence must be positive when present")
    return current_round_id if correction_sequence < first_issue_sequence else next_round_id


__all__ = [
    "EpochMember",
    "EvidenceEpoch",
    "MandatoryReaction",
    "ReactionBarrier",
    "correction_destination",
    "freeze_epoch",
]
