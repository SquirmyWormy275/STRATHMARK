"""Deterministic consequence disagreement and manual-estimate audit primitives."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from itertools import combinations
from typing import Any

from strathmark.v3.contracts.canonical import canonical_decimal_string, canonical_digest
from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.forecasts import AssessorKind, PositiveTimeDistribution
from strathmark.v3.contracts.identifiers import StableIdentifier, require_identifier


class ConsequenceColor(str, Enum):
    GREEN = "green"
    AMBER = "amber"
    RED = "red"


class OverrideScope(str, Enum):
    UPCOMING_RACE = "upcoming_race"
    REMAINING_EVENT_CONFIGURATION = "remaining_event_configuration"
    REMAINING_TOURNAMENT = "remaining_tournament"


class CouncilMemberStatus(str, Enum):
    VALID = "valid"
    FAILED = "failed"
    INVALID = "invalid"


class OptimizerVerificationStatus(str, Enum):
    PENDING = "pending_u14_verifier"
    VERIFIED = "verified"


@dataclass(frozen=True, slots=True)
class DisagreementThresholds:
    threshold_id: str
    green_median_delta_ms: int
    red_median_delta_ms: int
    green_interval_endpoint_delta_ms: int
    red_interval_endpoint_delta_ms: int
    green_mark_delta: int
    red_mark_delta: int
    green_win_probability_delta: str
    red_win_probability_delta: str
    green_spread_delta_ms: int
    red_spread_delta_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.threshold_id, str) or not self.threshold_id.startswith(
            "thresholds:"
        ):
            raise ContractError("disagreement threshold identity is invalid")
        _validate_threshold_values(self)

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "threshold_id": self.threshold_id,
            **{
                name: getattr(self, name)
                for name in self.__dataclass_fields__
                if name != "threshold_id"
            },
        }


@dataclass(frozen=True, slots=True)
class ThresholdReplayObservation:
    observation_id: str
    median_delta_ms: int
    interval_endpoint_delta_ms: int
    mark_delta: int
    win_probability_delta: str
    spread_delta_ms: int
    ordering_reversal: bool
    expected_color: ConsequenceColor

    def __post_init__(self) -> None:
        if not isinstance(self.observation_id, str) or not self.observation_id:
            raise ContractError("threshold replay observation identity is required")
        for value, label in (
            (self.median_delta_ms, "replay median delta"),
            (self.interval_endpoint_delta_ms, "replay interval delta"),
            (self.mark_delta, "replay mark delta"),
            (self.spread_delta_ms, "replay spread delta"),
        ):
            _nonnegative_int(value, label)
        _probability(self.win_probability_delta, "replay win probability delta")
        if not isinstance(self.ordering_reversal, bool) or not isinstance(
            self.expected_color, ConsequenceColor
        ):
            raise ContractError("threshold replay label must be explicit and typed")

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "observation_id": self.observation_id,
            "median_delta_ms": self.median_delta_ms,
            "interval_endpoint_delta_ms": self.interval_endpoint_delta_ms,
            "mark_delta": self.mark_delta,
            "win_probability_delta": self.win_probability_delta,
            "spread_delta_ms": self.spread_delta_ms,
            "ordering_reversal": self.ordering_reversal,
            "expected_color": self.expected_color.value,
        }


@dataclass(frozen=True, slots=True)
class HistoricalThresholdSelection:
    candidates: tuple[DisagreementThresholds, ...]
    replay_observations: tuple[ThresholdReplayObservation, ...]
    candidate_scores: tuple[tuple[str, int], ...]
    selected_threshold_id: str
    selection_digest: str

    def __post_init__(self) -> None:
        if not self.candidates or self.candidates != tuple(
            sorted(self.candidates, key=lambda item: item.threshold_id)
        ):
            raise ContractError("historical threshold candidates must be sorted and nonempty")
        if not self.replay_observations or self.replay_observations != tuple(
            sorted(self.replay_observations, key=lambda item: item.observation_id)
        ):
            raise ContractError("historical replay observations must be sorted and nonempty")
        if len({item.observation_id for item in self.replay_observations}) != len(
            self.replay_observations
        ):
            raise ContractError("historical replay observation identities must be unique")
        expected_scores = _threshold_scores(self.candidates, self.replay_observations)
        if self.candidate_scores != expected_scores:
            raise ContractError("historical threshold scores differ from replay")
        best_score = max(score for _identity, score in expected_scores)
        selected = min(identity for identity, score in expected_scores if score == best_score)
        if self.selected_threshold_id != selected:
            raise ContractError("historical threshold selection is not deterministic")
        _digest(self.selection_digest, "historical threshold selection")
        if self.selection_digest != canonical_digest(self.body()):
            raise ContractError("historical threshold selection digest differs")

    @property
    def selected_thresholds(self) -> DisagreementThresholds:
        return next(
            item for item in self.candidates if item.threshold_id == self.selected_threshold_id
        )

    def body(self) -> dict[str, object]:
        return {
            "schema_version": "strathmark-v3-historical-threshold-selection-v1",
            "candidates": [item.to_dict() for item in self.candidates],
            "replay_observations": [item.to_dict() for item in self.replay_observations],
            "candidate_scores": [list(item) for item in self.candidate_scores],
            "selected_threshold_id": self.selected_threshold_id,
        }


@dataclass(frozen=True, slots=True)
class DisjointThresholdVerification:
    selection_digest: str
    selected_threshold_id: str
    training_observation_ids: tuple[str, ...]
    holdout_observations: tuple[ThresholdReplayObservation, ...]
    accuracy: str
    minimum_accuracy: str
    passed: bool
    verification_digest: str

    def __post_init__(self) -> None:
        _digest(self.selection_digest, "threshold verification selection")
        if set(self.training_observation_ids) & {
            item.observation_id for item in self.holdout_observations
        }:
            raise ContractError("threshold holdout is not disjoint from historical replay")
        accuracy = _probability(self.accuracy, "threshold holdout accuracy")
        minimum = _probability(self.minimum_accuracy, "minimum threshold accuracy")
        if self.passed is not (accuracy >= minimum):
            raise ContractError("threshold holdout outcome differs")
        _digest(self.verification_digest, "threshold disjoint verification")
        if self.verification_digest != canonical_digest(self.body()):
            raise ContractError("threshold verification digest differs")

    def body(self) -> dict[str, object]:
        return {
            "schema_version": "strathmark-v3-disjoint-threshold-verification-v1",
            "selection_digest": self.selection_digest,
            "selected_threshold_id": self.selected_threshold_id,
            "training_observation_ids": list(self.training_observation_ids),
            "holdout_observations": [item.to_dict() for item in self.holdout_observations],
            "accuracy": self.accuracy,
            "minimum_accuracy": self.minimum_accuracy,
            "passed": self.passed,
        }


def _validate_threshold_values(value: object) -> None:
    pairs = (
        (value.green_median_delta_ms, value.red_median_delta_ms),
        (value.green_interval_endpoint_delta_ms, value.red_interval_endpoint_delta_ms),
        (value.green_mark_delta, value.red_mark_delta),
        (value.green_spread_delta_ms, value.red_spread_delta_ms),
    )
    if any(
        isinstance(low, bool)
        or not isinstance(low, int)
        or isinstance(high, bool)
        or not isinstance(high, int)
        or low < 0
        or high <= low
        for low, high in pairs
    ):
        raise ContractError("green/red integer threshold pairs must be ordered")
    if value.red_mark_delta < 2:
        raise ContractError("red mark threshold cannot be below two seconds")
    if _probability(value.red_win_probability_delta, "red win probability threshold") <= (
        _probability(value.green_win_probability_delta, "green win probability threshold")
    ):
        raise ContractError("green/red probability thresholds must be ordered")


def _classify_threshold(
    thresholds: DisagreementThresholds, observation: ThresholdReplayObservation
) -> ConsequenceColor:
    red = observation.ordering_reversal or any(
        (
            observation.mark_delta >= thresholds.red_mark_delta,
            Decimal(observation.win_probability_delta)
            >= Decimal(thresholds.red_win_probability_delta),
            observation.spread_delta_ms >= thresholds.red_spread_delta_ms,
        )
    )
    green = all(
        (
            observation.median_delta_ms <= thresholds.green_median_delta_ms,
            observation.interval_endpoint_delta_ms <= thresholds.green_interval_endpoint_delta_ms,
            observation.mark_delta <= thresholds.green_mark_delta,
            Decimal(observation.win_probability_delta)
            <= Decimal(thresholds.green_win_probability_delta),
            observation.spread_delta_ms <= thresholds.green_spread_delta_ms,
        )
    )
    return (
        ConsequenceColor.RED if red else ConsequenceColor.GREEN if green else ConsequenceColor.AMBER
    )


def _threshold_scores(
    candidates: tuple[DisagreementThresholds, ...],
    observations: tuple[ThresholdReplayObservation, ...],
) -> tuple[tuple[str, int], ...]:
    return tuple(
        (
            candidate.threshold_id,
            sum(
                _classify_threshold(candidate, observation) is observation.expected_color
                for observation in observations
            ),
        )
        for candidate in candidates
    )


def select_historical_thresholds(
    candidates: tuple[DisagreementThresholds, ...],
    replay_observations: tuple[ThresholdReplayObservation, ...],
) -> HistoricalThresholdSelection:
    ordered_candidates = tuple(sorted(candidates, key=lambda item: item.threshold_id))
    ordered_replay = tuple(sorted(replay_observations, key=lambda item: item.observation_id))
    scores = _threshold_scores(ordered_candidates, ordered_replay)
    if not scores:
        raise ContractError("historical threshold selection requires candidates")
    best = max(score for _identity, score in scores)
    selected = min(identity for identity, score in scores if score == best)
    body = {
        "schema_version": "strathmark-v3-historical-threshold-selection-v1",
        "candidates": [item.to_dict() for item in ordered_candidates],
        "replay_observations": [item.to_dict() for item in ordered_replay],
        "candidate_scores": [list(item) for item in scores],
        "selected_threshold_id": selected,
    }
    return HistoricalThresholdSelection(
        ordered_candidates,
        ordered_replay,
        scores,
        selected,
        canonical_digest(body),
    )


def verify_disjoint_thresholds(
    selection: HistoricalThresholdSelection,
    holdout_observations: tuple[ThresholdReplayObservation, ...],
    *,
    minimum_accuracy: str,
) -> DisjointThresholdVerification:
    if not isinstance(selection, HistoricalThresholdSelection):
        raise ContractError("threshold verification requires historical selection authority")
    ordered = tuple(sorted(holdout_observations, key=lambda item: item.observation_id))
    training_ids = tuple(item.observation_id for item in selection.replay_observations)
    if not ordered or set(training_ids) & {item.observation_id for item in ordered}:
        raise ContractError("threshold holdout must be nonempty and disjoint")
    correct = sum(
        _classify_threshold(selection.selected_thresholds, item) is item.expected_color
        for item in ordered
    )
    accuracy = canonical_decimal_string(Decimal(correct) / Decimal(len(ordered)))
    passed = Decimal(accuracy) >= _probability(minimum_accuracy, "minimum threshold accuracy")
    body = {
        "schema_version": "strathmark-v3-disjoint-threshold-verification-v1",
        "selection_digest": selection.selection_digest,
        "selected_threshold_id": selection.selected_threshold_id,
        "training_observation_ids": list(training_ids),
        "holdout_observations": [item.to_dict() for item in ordered],
        "accuracy": accuracy,
        "minimum_accuracy": minimum_accuracy,
        "passed": passed,
    }
    return DisjointThresholdVerification(
        selection.selection_digest,
        selection.selected_threshold_id,
        training_ids,
        ordered,
        accuracy,
        minimum_accuracy,
        passed,
        canonical_digest(body),
    )


def freeze_disagreement_policy(
    selection: HistoricalThresholdSelection,
    verification: DisjointThresholdVerification,
) -> DisagreementPolicy:
    if (
        not isinstance(selection, HistoricalThresholdSelection)
        or not isinstance(verification, DisjointThresholdVerification)
        or verification.selection_digest != selection.selection_digest
        or verification.selected_threshold_id != selection.selected_threshold_id
        or not verification.passed
    ):
        raise ContractError("disagreement policy requires passing disjoint threshold authority")
    value = selection.selected_thresholds
    return DisagreementPolicy(
        "disagreement:v1",
        value.green_median_delta_ms,
        value.red_median_delta_ms,
        value.green_interval_endpoint_delta_ms,
        value.red_interval_endpoint_delta_ms,
        value.green_mark_delta,
        value.red_mark_delta,
        value.green_win_probability_delta,
        value.red_win_probability_delta,
        value.green_spread_delta_ms,
        value.red_spread_delta_ms,
        selection.selection_digest,
        verification.verification_digest,
    )


@dataclass(frozen=True, slots=True, order=True)
class CouncilMemberAudit:
    member_id: StableIdentifier
    status: CouncilMemberStatus
    outcome_digest: str
    receipt_digest: str

    def __post_init__(self) -> None:
        require_identifier(self.member_id, expected_namespace="llm_member")
        if not isinstance(self.status, CouncilMemberStatus):
            raise ContractError("council member status must be typed")
        _digest(self.outcome_digest, "council member outcome digest")
        _digest(self.receipt_digest, "council member receipt digest")

    def to_dict(self) -> dict[str, Any]:
        return {
            "member_id": str(self.member_id),
            "status": self.status.value,
            "outcome_digest": self.outcome_digest,
            "receipt_digest": self.receipt_digest,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CouncilMemberAudit:
        if set(value) != {"member_id", "status", "outcome_digest", "receipt_digest"}:
            raise ContractError("council member audit fields differ")
        try:
            status = CouncilMemberStatus(value["status"])
        except (TypeError, ValueError) as exc:
            raise ContractError("council member status is unknown") from exc
        return cls(
            require_identifier(value["member_id"], expected_namespace="llm_member"),
            status,
            value["outcome_digest"],
            value["receipt_digest"],
        )


@dataclass(frozen=True, slots=True, order=True)
class CounterfactualCompetitor:
    competitor_id: StableIdentifier
    median_ms: int
    lower_ms: int
    upper_ms: int
    mark: int
    win_probability: str

    def __post_init__(self) -> None:
        require_identifier(self.competitor_id, expected_namespace="competitor")
        for value, label in (
            (self.median_ms, "median_ms"),
            (self.lower_ms, "lower_ms"),
            (self.upper_ms, "upper_ms"),
            (self.mark, "mark"),
        ):
            _positive_int(value, label)
        if not self.lower_ms <= self.median_ms <= self.upper_ms:
            raise ContractError("forecast interval must contain its median")
        if self.mark < 3:
            raise ContractError("counterfactual marks cannot be below Mark 3")
        _probability(self.win_probability, "win_probability")

    def to_dict(self) -> dict[str, Any]:
        return {
            "competitor_id": str(self.competitor_id),
            "median_ms": self.median_ms,
            "lower_ms": self.lower_ms,
            "upper_ms": self.upper_ms,
            "mark": self.mark,
            "win_probability": self.win_probability,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CounterfactualCompetitor:
        if set(value) != {
            "competitor_id",
            "median_ms",
            "lower_ms",
            "upper_ms",
            "mark",
            "win_probability",
        }:
            raise ContractError("counterfactual competitor fields differ")
        return cls(
            require_identifier(value["competitor_id"], expected_namespace="competitor"),
            value["median_ms"],
            value["lower_ms"],
            value["upper_ms"],
            value["mark"],
            value["win_probability"],
        )


@dataclass(frozen=True, slots=True)
class CounterfactualSheet:
    source: AssessorKind | str
    competitors: tuple[CounterfactualCompetitor, ...]
    expected_spread_ms: int
    joint_draw_digest: str
    optimizer_digest: str
    optimizer_verification_status: OptimizerVerificationStatus
    sheet_digest: str

    def __post_init__(self) -> None:
        _source(self.source)
        if not isinstance(self.competitors, tuple) or not self.competitors:
            raise ContractError("counterfactual sheet requires a complete immutable field")
        if not all(isinstance(item, CounterfactualCompetitor) for item in self.competitors):
            raise ContractError("counterfactual competitors must be typed")
        identities = tuple(str(item.competitor_id) for item in self.competitors)
        if identities != tuple(sorted(identities)) or len(identities) != len(set(identities)):
            raise ContractError("counterfactual competitor roster must be unique and sorted")
        _nonnegative_int(self.expected_spread_ms, "expected_spread_ms")
        _digest(self.joint_draw_digest, "joint_draw_digest")
        _digest(self.optimizer_digest, "optimizer_digest")
        if self.optimizer_verification_status is not OptimizerVerificationStatus.PENDING:
            raise ContractError("U14 typed optimizer verifier is required for VERIFIED status")
        _digest(self.sheet_digest, "sheet_digest")
        if (
            sum(
                (Fraction(item.win_probability) for item in self.competitors),
                Fraction(0),
            )
            != 1
        ):
            raise ContractError("field win probabilities must sum exactly to one")
        if self.sheet_digest != canonical_digest(self.content_value()):
            raise ContractError("counterfactual sheet digest mismatch")

    @classmethod
    def create(
        cls,
        *,
        source: AssessorKind | str,
        competitors: tuple[CounterfactualCompetitor, ...],
        expected_spread_ms: int,
        joint_draw_digest: str,
        optimizer_digest: str,
        optimizer_verification_status: OptimizerVerificationStatus,
    ) -> CounterfactualSheet:
        ordered = tuple(sorted(competitors, key=lambda item: str(item.competitor_id)))
        values = {
            "source": source,
            "competitors": ordered,
            "expected_spread_ms": expected_spread_ms,
            "joint_draw_digest": joint_draw_digest,
            "optimizer_digest": optimizer_digest,
            "optimizer_verification_status": optimizer_verification_status,
        }
        content = {
            "schema_version": "strathmark-v3-counterfactual-sheet-v1",
            "source": source.value if isinstance(source, AssessorKind) else source,
            "competitors": [item.to_dict() for item in ordered],
            "expected_spread_ms": expected_spread_ms,
            "joint_draw_digest": joint_draw_digest,
            "optimizer_digest": optimizer_digest,
            "optimizer_verification_status": optimizer_verification_status.value,
        }
        return cls(**values, sheet_digest=canonical_digest(content))

    def content_value(self) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-counterfactual-sheet-v1",
            "source": (self.source.value if isinstance(self.source, AssessorKind) else self.source),
            "competitors": [item.to_dict() for item in self.competitors],
            "expected_spread_ms": self.expected_spread_ms,
            "joint_draw_digest": self.joint_draw_digest,
            "optimizer_digest": self.optimizer_digest,
            "optimizer_verification_status": self.optimizer_verification_status.value,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_value(), "sheet_digest": self.sheet_digest}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CounterfactualSheet:
        expected = {
            "schema_version",
            "source",
            "competitors",
            "expected_spread_ms",
            "joint_draw_digest",
            "optimizer_digest",
            "optimizer_verification_status",
            "sheet_digest",
        }
        if (
            set(value) != expected
            or value["schema_version"] != "strathmark-v3-counterfactual-sheet-v1"
        ):
            raise ContractError("counterfactual sheet fields or schema differ")
        source_value = value["source"]
        if source_value == "pooled":
            source: AssessorKind | str = "pooled"
        else:
            try:
                source = AssessorKind(source_value)
            except (TypeError, ValueError) as exc:
                raise ContractError("counterfactual source is unknown") from exc
        competitors = value["competitors"]
        if not isinstance(competitors, list):
            raise ContractError("counterfactual competitors are invalid")
        try:
            verification = OptimizerVerificationStatus(value["optimizer_verification_status"])
        except (TypeError, ValueError) as exc:
            raise ContractError("optimizer verification status is unknown") from exc
        return cls(
            source,
            tuple(CounterfactualCompetitor.from_dict(item) for item in competitors),
            value["expected_spread_ms"],
            value["joint_draw_digest"],
            value["optimizer_digest"],
            verification,
            value["sheet_digest"],
        )


@dataclass(frozen=True, slots=True)
class CouncilAudit:
    aggregate_sheet_digest: str
    aggregate_forecast_digest: str
    evidence_digest: str
    evidence_epoch_id: StableIdentifier
    members: tuple[CouncilMemberAudit, ...]
    audit_digest: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.aggregate_sheet_digest, "council aggregate sheet digest"),
            (self.aggregate_forecast_digest, "council aggregate forecast digest"),
            (self.evidence_digest, "council evidence digest"),
            (self.audit_digest, "council audit digest"),
        ):
            _digest(value, label)
        require_identifier(self.evidence_epoch_id, expected_namespace="epoch")
        if (
            not isinstance(self.members, tuple)
            or len(self.members) != 3
            or not all(isinstance(item, CouncilMemberAudit) for item in self.members)
        ):
            raise ContractError("council audit requires exactly three typed member outcomes")
        identities = tuple(str(item.member_id) for item in self.members)
        if len(identities) != len(set(identities)):
            raise ContractError("council member ids must be unique")
        if identities != tuple(sorted(identities)):
            raise ContractError("council member ids must be canonically ordered")
        if self.audit_digest != canonical_digest(self.content_value()):
            raise ContractError("council audit digest mismatch")

    def content_value(self) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-council-audit-v1",
            "aggregate_sheet_digest": self.aggregate_sheet_digest,
            "aggregate_forecast_digest": self.aggregate_forecast_digest,
            "evidence_digest": self.evidence_digest,
            "evidence_epoch_id": str(self.evidence_epoch_id),
            "members": [item.to_dict() for item in self.members],
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_value(), "audit_digest": self.audit_digest}

    @classmethod
    def create(
        cls,
        *,
        aggregate_sheet: CounterfactualSheet,
        aggregate_forecast_digest: str,
        evidence_digest: str,
        evidence_epoch_id: StableIdentifier,
        members: tuple[CouncilMemberAudit, ...],
    ) -> CouncilAudit:
        if (
            not isinstance(aggregate_sheet, CounterfactualSheet)
            or aggregate_sheet.source is not AssessorKind.LLM_COUNCIL
        ):
            raise ContractError("council audit requires the council aggregate sheet")
        ordered = tuple(sorted(members, key=lambda item: str(item.member_id)))
        values = {
            "aggregate_sheet_digest": aggregate_sheet.sheet_digest,
            "aggregate_forecast_digest": aggregate_forecast_digest,
            "evidence_digest": evidence_digest,
            "evidence_epoch_id": evidence_epoch_id,
            "members": ordered,
        }
        content = {
            "schema_version": "strathmark-v3-council-audit-v1",
            "aggregate_sheet_digest": aggregate_sheet.sheet_digest,
            "aggregate_forecast_digest": aggregate_forecast_digest,
            "evidence_digest": evidence_digest,
            "evidence_epoch_id": str(evidence_epoch_id),
            "members": [item.to_dict() for item in ordered],
        }
        return cls(**values, audit_digest=canonical_digest(content))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CouncilAudit:
        if (
            set(value)
            != {
                "schema_version",
                "aggregate_sheet_digest",
                "aggregate_forecast_digest",
                "evidence_digest",
                "evidence_epoch_id",
                "members",
                "audit_digest",
            }
            or value.get("schema_version") != "strathmark-v3-council-audit-v1"
        ):
            raise ContractError("council audit fields or schema differ")
        if not isinstance(value["members"], list):
            raise ContractError("council audit members must be an array")
        return cls(
            value["aggregate_sheet_digest"],
            value["aggregate_forecast_digest"],
            value["evidence_digest"],
            require_identifier(value["evidence_epoch_id"], expected_namespace="epoch"),
            tuple(CouncilMemberAudit.from_dict(item) for item in value["members"]),
            value["audit_digest"],
        )


@dataclass(frozen=True, slots=True)
class DisagreementPolicy:
    version: str
    green_median_delta_ms: int
    red_median_delta_ms: int
    green_interval_endpoint_delta_ms: int
    red_interval_endpoint_delta_ms: int
    green_mark_delta: int
    red_mark_delta: int
    green_win_probability_delta: str
    red_win_probability_delta: str
    green_spread_delta_ms: int
    red_spread_delta_ms: int
    replay_evidence_digest: str
    disjoint_verification_digest: str

    def __post_init__(self) -> None:
        if self.version != "disagreement:v1":
            raise ContractError("disagreement policy version is not supported")
        pairs = (
            (self.green_median_delta_ms, self.red_median_delta_ms),
            (
                self.green_interval_endpoint_delta_ms,
                self.red_interval_endpoint_delta_ms,
            ),
            (self.green_mark_delta, self.red_mark_delta),
            (self.green_spread_delta_ms, self.red_spread_delta_ms),
        )
        if any(
            isinstance(low, bool)
            or not isinstance(low, int)
            or isinstance(high, bool)
            or not isinstance(high, int)
            or low < 0
            or high <= low
            for low, high in pairs
        ):
            raise ContractError("green/red integer threshold pairs must be ordered")
        if self.red_mark_delta < 2:
            raise ContractError("red mark threshold cannot be below two seconds")
        green_probability = _probability(
            self.green_win_probability_delta, "green_win_probability_delta"
        )
        red_probability = _probability(self.red_win_probability_delta, "red_win_probability_delta")
        if red_probability <= green_probability:
            raise ContractError("green/red probability thresholds must be ordered")
        _digest(self.replay_evidence_digest, "replay_evidence_digest")
        _digest(self.disjoint_verification_digest, "disjoint_verification_digest")

    @property
    def digest(self) -> str:
        return canonical_digest({name: getattr(self, name) for name in self.__dataclass_fields__})

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DisagreementPolicy:
        if set(value) != set(cls.__dataclass_fields__):
            raise ContractError("disagreement policy fields differ")
        return cls(**value)


@dataclass(frozen=True, slots=True)
class ConsequenceComparison:
    source: str
    median_delta_ms: int
    interval_endpoint_delta_ms: int
    mark_delta: int
    win_probability_delta: str
    spread_delta_ms: int
    ordering_reversal: bool
    color: ConsequenceColor

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source:
            raise ContractError("consequence comparison source is required")
        for value, label in (
            (self.median_delta_ms, "median delta"),
            (self.interval_endpoint_delta_ms, "interval endpoint delta"),
            (self.mark_delta, "mark delta"),
            (self.spread_delta_ms, "spread delta"),
        ):
            _nonnegative_int(value, label)
        _probability(self.win_probability_delta, "win probability delta")
        if not isinstance(self.ordering_reversal, bool):
            raise ContractError("ordering reversal must be explicit")
        if not isinstance(self.color, ConsequenceColor):
            raise ContractError("consequence comparison color must be typed")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "median_delta_ms": self.median_delta_ms,
            "interval_endpoint_delta_ms": self.interval_endpoint_delta_ms,
            "mark_delta": self.mark_delta,
            "win_probability_delta": self.win_probability_delta,
            "spread_delta_ms": self.spread_delta_ms,
            "ordering_reversal": self.ordering_reversal,
            "color": self.color.value,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ConsequenceComparison:
        expected = {
            "source",
            "median_delta_ms",
            "interval_endpoint_delta_ms",
            "mark_delta",
            "win_probability_delta",
            "spread_delta_ms",
            "ordering_reversal",
            "color",
        }
        if set(value) != expected:
            raise ContractError("consequence comparison fields differ")
        try:
            color = ConsequenceColor(value["color"])
        except (TypeError, ValueError) as exc:
            raise ContractError("consequence color is unknown") from exc
        return cls(
            value["source"],
            value["median_delta_ms"],
            value["interval_endpoint_delta_ms"],
            value["mark_delta"],
            value["win_probability_delta"],
            value["spread_delta_ms"],
            value["ordering_reversal"],
            color,
        )


@dataclass(frozen=True, slots=True)
class DisagreementDecision:
    color: ConsequenceColor
    operational_status: OptimizerVerificationStatus
    manual_review_required: bool
    pooled_sheet: CounterfactualSheet
    component_sheets: tuple[CounterfactualSheet, ...]
    comparisons: tuple[ConsequenceComparison, ...]
    council_audit: CouncilAudit | None
    policy: DisagreementPolicy
    assessor_availability: tuple[tuple[AssessorKind, bool], ...]
    decision_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.color, ConsequenceColor):
            raise ContractError("disagreement color must be typed")
        if (
            self.operational_status is not OptimizerVerificationStatus.PENDING
            or not self.manual_review_required
        ):
            raise ContractError(
                "U14 verified optimizer receipt is required for an operational consequence color"
            )
        if (
            not isinstance(self.pooled_sheet, CounterfactualSheet)
            or self.pooled_sheet.source != "pooled"
        ):
            raise ContractError("disagreement decision requires its pooled reference sheet")
        if not isinstance(self.component_sheets, tuple) or not isinstance(self.comparisons, tuple):
            raise ContractError("disagreement evidence must be immutable")
        if not all(
            isinstance(item, CounterfactualSheet) for item in self.component_sheets
        ) or not all(isinstance(item, ConsequenceComparison) for item in self.comparisons):
            raise ContractError("disagreement evidence must be typed")
        sources = tuple(_source_name(item.source) for item in self.component_sheets)
        if sources != tuple(item.source for item in self.comparisons):
            raise ContractError("disagreement comparisons must bind every component sheet")
        if not isinstance(self.policy, DisagreementPolicy):
            raise ContractError("disagreement decision requires its frozen policy")
        expected_comparisons = tuple(
            _compare(self.pooled_sheet, item, self.policy) for item in self.component_sheets
        )
        if self.comparisons != expected_comparisons:
            raise ContractError("disagreement comparisons differ from deterministic replay")
        expected_availability = tuple(
            (item, item.value in sources)
            for item in (
                AssessorKind.FORMULA,
                AssessorKind.ML,
                AssessorKind.LLM_COUNCIL,
            )
        )
        if self.assessor_availability != expected_availability:
            raise ContractError("disagreement availability differs from component sheets")
        expected_color = max((item.color for item in self.comparisons), key=_color_rank)
        if self.color is not expected_color:
            raise ContractError("disagreement color differs from deterministic comparisons")
        council_sheet = next(
            (item for item in self.component_sheets if item.source is AssessorKind.LLM_COUNCIL),
            None,
        )
        if council_sheet is None:
            if self.council_audit is not None:
                raise ContractError("council audit cannot appear when council is unavailable")
        elif (
            not isinstance(self.council_audit, CouncilAudit)
            or self.council_audit.aggregate_sheet_digest != council_sheet.sheet_digest
        ):
            raise ContractError("available council requires its exact three-member audit")
        _digest(self.decision_digest, "decision_digest")
        if self.decision_digest != canonical_digest(self.content_value()):
            raise ContractError("disagreement decision digest mismatch")

    def content_value(self) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-disagreement-decision-v1",
            "color": self.color.value,
            "operational_status": self.operational_status.value,
            "manual_review_required": self.manual_review_required,
            "pooled_sheet_digest": self.pooled_sheet.sheet_digest,
            "component_sheet_digests": [item.sheet_digest for item in self.component_sheets],
            "comparisons": [item.to_dict() for item in self.comparisons],
            "council_audit_digest": (
                None if self.council_audit is None else self.council_audit.audit_digest
            ),
            "policy": self.policy.to_dict(),
            "policy_digest": self.policy.digest,
            "assessor_availability": [
                (item.value, available) for item, available in self.assessor_availability
            ],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-disagreement-decision-v1",
            "color": self.color.value,
            "operational_status": self.operational_status.value,
            "manual_review_required": self.manual_review_required,
            "pooled_sheet": self.pooled_sheet.to_dict(),
            "component_sheets": [item.to_dict() for item in self.component_sheets],
            "comparisons": [item.to_dict() for item in self.comparisons],
            "council_audit": (None if self.council_audit is None else self.council_audit.to_dict()),
            "policy": self.policy.to_dict(),
            "policy_digest": self.policy.digest,
            "assessor_availability": [
                [item.value, available] for item, available in self.assessor_availability
            ],
            "decision_digest": self.decision_digest,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DisagreementDecision:
        expected = {
            "schema_version",
            "color",
            "operational_status",
            "manual_review_required",
            "pooled_sheet",
            "component_sheets",
            "comparisons",
            "council_audit",
            "policy",
            "policy_digest",
            "assessor_availability",
            "decision_digest",
        }
        if (
            set(value) != expected
            or value["schema_version"] != "strathmark-v3-disagreement-decision-v1"
        ):
            raise ContractError("disagreement decision fields or schema differ")
        try:
            color = ConsequenceColor(value["color"])
            operational_status = OptimizerVerificationStatus(value["operational_status"])
            availability = tuple(
                (AssessorKind(item[0]), item[1]) for item in value["assessor_availability"]
            )
        except (TypeError, ValueError) as exc:
            raise ContractError("disagreement decision vocabulary is unknown") from exc
        policy_value = value["policy"]
        if not isinstance(policy_value, dict):
            raise ContractError("disagreement decision policy is invalid")
        policy = DisagreementPolicy.from_dict(policy_value)
        if value["policy_digest"] != policy.digest:
            raise ContractError("disagreement decision policy digest mismatch")
        return cls(
            color,
            operational_status,
            value["manual_review_required"],
            CounterfactualSheet.from_dict(value["pooled_sheet"]),
            tuple(CounterfactualSheet.from_dict(item) for item in value["component_sheets"]),
            tuple(ConsequenceComparison.from_dict(item) for item in value["comparisons"]),
            (
                None
                if value["council_audit"] is None
                else CouncilAudit.from_dict(value["council_audit"])
            ),
            policy,
            availability,
            value["decision_digest"],
        )

    @property
    def policy_digest(self) -> str:
        return self.policy.digest


def classify_disagreement(
    pooled_sheet: CounterfactualSheet,
    component_sheets: tuple[CounterfactualSheet, ...],
    council_audit: CouncilAudit | None,
    policy: DisagreementPolicy,
    *,
    available_assessors: tuple[AssessorKind, ...],
) -> DisagreementDecision:
    if not isinstance(pooled_sheet, CounterfactualSheet) or not isinstance(
        policy, DisagreementPolicy
    ):
        raise ContractError("disagreement classification requires typed inputs")
    if (
        not isinstance(component_sheets, tuple)
        or not component_sheets
        or not all(isinstance(item, CounterfactualSheet) for item in component_sheets)
    ):
        raise ContractError("disagreement requires immutable component sheets")
    if pooled_sheet.source != "pooled":
        raise ContractError("disagreement reference sheet must be the pooled sheet")
    expected_available = tuple(
        item
        for item in (AssessorKind.FORMULA, AssessorKind.ML, AssessorKind.LLM_COUNCIL)
        if item in available_assessors
    )
    if (
        not isinstance(available_assessors, tuple)
        or available_assessors != expected_available
        or len(available_assessors) < 2
    ):
        raise ContractError(
            "disagreement availability must explicitly name two or three outer assessors"
        )
    expected_roster = tuple(item.competitor_id for item in pooled_sheet.competitors)
    if any(
        tuple(row.competitor_id for row in item.competitors) != expected_roster
        for item in component_sheets
    ):
        raise ValueError("counterfactual sheets must cover the identical roster")
    sources = tuple(_source_name(item.source) for item in component_sheets)
    if sources != tuple(item.value for item in available_assessors):
        raise ValueError("component sheets must exactly match explicit assessor availability")
    council_sheet = next(
        (item for item in component_sheets if item.source is AssessorKind.LLM_COUNCIL),
        None,
    )
    if council_sheet is None:
        if council_audit is not None:
            raise ContractError("council audit cannot appear when council is unavailable")
    elif (
        not isinstance(council_audit, CouncilAudit)
        or council_audit.aggregate_sheet_digest != council_sheet.sheet_digest
    ):
        raise ContractError("available LLM council requires its exact three-member audit")
    comparisons = tuple(_compare(pooled_sheet, item, policy) for item in component_sheets)
    color = max((item.color for item in comparisons), key=_color_rank)
    values = {
        "color": color,
        "operational_status": OptimizerVerificationStatus.PENDING,
        "manual_review_required": True,
        "pooled_sheet": pooled_sheet,
        "component_sheets": component_sheets,
        "comparisons": comparisons,
        "council_audit": council_audit,
        "policy": policy,
        "assessor_availability": tuple(
            (item, item in available_assessors)
            for item in (
                AssessorKind.FORMULA,
                AssessorKind.ML,
                AssessorKind.LLM_COUNCIL,
            )
        ),
    }
    return DisagreementDecision(
        **values, decision_digest=canonical_digest(_decision_content(values))
    )


def _compare(
    pooled: CounterfactualSheet,
    component: CounterfactualSheet,
    policy: DisagreementPolicy,
) -> ConsequenceComparison:
    pairs = tuple(zip(pooled.competitors, component.competitors))
    median = max(abs(left.median_ms - right.median_ms) for left, right in pairs)
    interval = max(
        max(abs(left.lower_ms - right.lower_ms), abs(left.upper_ms - right.upper_ms))
        for left, right in pairs
    )
    mark = max(abs(left.mark - right.mark) for left, right in pairs)
    probability = max(
        abs(Decimal(left.win_probability) - Decimal(right.win_probability)) for left, right in pairs
    )
    spread = abs(pooled.expected_spread_ms - component.expected_spread_ms)
    reversal = _ordering_reversal(pooled.competitors, component.competitors)
    red = reversal or any(
        (
            mark >= policy.red_mark_delta,
            probability >= Decimal(policy.red_win_probability_delta),
            spread >= policy.red_spread_delta_ms,
        )
    )
    green = all(
        (
            median <= policy.green_median_delta_ms,
            interval <= policy.green_interval_endpoint_delta_ms,
            mark <= policy.green_mark_delta,
            probability <= Decimal(policy.green_win_probability_delta),
            spread <= policy.green_spread_delta_ms,
        )
    )
    color = (
        ConsequenceColor.RED if red else ConsequenceColor.GREEN if green else ConsequenceColor.AMBER
    )
    return ConsequenceComparison(
        _source_name(component.source),
        median,
        interval,
        mark,
        canonical_decimal_string(probability),
        spread,
        reversal,
        color,
    )


def _ordering_reversal(
    pooled: tuple[CounterfactualCompetitor, ...],
    component: tuple[CounterfactualCompetitor, ...],
) -> bool:
    by_id = {item.competitor_id: item for item in component}
    for left, right in combinations(pooled, 2):
        pooled_delta = left.median_ms - right.median_ms
        component_delta = by_id[left.competitor_id].median_ms - by_id[right.competitor_id].median_ms
        if pooled_delta * component_delta < 0:
            return True
    return False


@dataclass(frozen=True, slots=True)
class ZeroHistoryPolicy:
    interval_lower_probability: str
    interval_upper_probability: str
    minimum_interval_width_ms: int
    version: str

    def __post_init__(self) -> None:
        lower = _probability(self.interval_lower_probability, "interval_lower_probability")
        upper = _probability(self.interval_upper_probability, "interval_upper_probability")
        if not 0 < lower < Decimal("0.5") < upper < 1:
            raise ContractError("zero-history interval probabilities must straddle the median")
        _positive_int(self.minimum_interval_width_ms, "minimum_interval_width_ms")
        if self.version != "zero-history:v1":
            raise ContractError("zero-history policy version is not supported")

    @property
    def digest(self) -> str:
        return canonical_digest({name: getattr(self, name) for name in self.__dataclass_fields__})


@dataclass(frozen=True, slots=True)
class ZeroHistoryEstimate:
    competitor_id: StableIdentifier
    target_context_digest: str
    distribution: PositiveTimeDistribution
    population_prior_digest: str
    policy_digest: str
    review_color: ConsequenceColor
    manual_acceptance_required: bool
    maximum_honest_uncertainty: bool
    estimate_digest: str

    def __post_init__(self) -> None:
        require_identifier(self.competitor_id, expected_namespace="competitor")
        _digest(self.target_context_digest, "target_context_digest")
        if not isinstance(self.distribution, PositiveTimeDistribution):
            raise ContractError("zero-history estimate requires a positive distribution")
        _digest(self.population_prior_digest, "population_prior_digest")
        _digest(self.policy_digest, "policy_digest")
        if (
            self.review_color is not ConsequenceColor.RED
            or not self.manual_acceptance_required
            or not self.maximum_honest_uncertainty
        ):
            raise ContractError("zero-history estimate must remain red/manual/max-uncertainty")
        _digest(self.estimate_digest, "estimate_digest")
        if self.estimate_digest != canonical_digest(self.content_value()):
            raise ContractError("zero-history estimate digest mismatch")

    def content_value(self) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-zero-history-estimate-v1",
            "competitor_id": str(self.competitor_id),
            "target_context_digest": self.target_context_digest,
            "distribution": self.distribution.to_dict(),
            "population_prior_digest": self.population_prior_digest,
            "policy_digest": self.policy_digest,
            "review_color": self.review_color.value,
            "manual_acceptance_required": self.manual_acceptance_required,
            "maximum_honest_uncertainty": self.maximum_honest_uncertainty,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_value(), "estimate_digest": self.estimate_digest}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ZeroHistoryEstimate:
        expected = {
            "schema_version",
            "competitor_id",
            "target_context_digest",
            "distribution",
            "population_prior_digest",
            "policy_digest",
            "review_color",
            "manual_acceptance_required",
            "maximum_honest_uncertainty",
            "estimate_digest",
        }
        if (
            set(value) != expected
            or value["schema_version"] != "strathmark-v3-zero-history-estimate-v1"
        ):
            raise ContractError("zero-history estimate fields or schema differ")
        try:
            color = ConsequenceColor(value["review_color"])
        except (TypeError, ValueError) as exc:
            raise ContractError("zero-history review color is unknown") from exc
        return cls(
            require_identifier(value["competitor_id"], expected_namespace="competitor"),
            value["target_context_digest"],
            PositiveTimeDistribution.from_dict(value["distribution"]),
            value["population_prior_digest"],
            value["policy_digest"],
            color,
            value["manual_acceptance_required"],
            value["maximum_honest_uncertainty"],
            value["estimate_digest"],
        )


def create_zero_history_estimate(
    competitor_id: StableIdentifier,
    target_context_digest: str,
    population_prior: PositiveTimeDistribution,
    population_prior_digest: str,
    policy: ZeroHistoryPolicy,
) -> ZeroHistoryEstimate:
    require_identifier(competitor_id, expected_namespace="competitor")
    _digest(target_context_digest, "target_context_digest")
    _digest(population_prior_digest, "population_prior_digest")
    if not isinstance(population_prior, PositiveTimeDistribution) or not isinstance(
        policy, ZeroHistoryPolicy
    ):
        raise ContractError("zero-history estimate requires a typed prior and policy")
    lower, upper = population_prior.central_interval(
        policy.interval_lower_probability, policy.interval_upper_probability
    )
    if upper - lower < policy.minimum_interval_width_ms:
        raise ValueError("zero-history population prior is not broad enough")
    values = {
        "competitor_id": competitor_id,
        "target_context_digest": target_context_digest,
        "distribution": population_prior,
        "population_prior_digest": population_prior_digest,
        "policy_digest": policy.digest,
        "review_color": ConsequenceColor.RED,
        "manual_acceptance_required": True,
        "maximum_honest_uncertainty": True,
    }
    content = {
        "schema_version": "strathmark-v3-zero-history-estimate-v1",
        "competitor_id": str(competitor_id),
        "target_context_digest": target_context_digest,
        "distribution": population_prior.to_dict(),
        "population_prior_digest": population_prior_digest,
        "policy_digest": policy.digest,
        "review_color": ConsequenceColor.RED.value,
        "manual_acceptance_required": True,
        "maximum_honest_uncertainty": True,
    }
    return ZeroHistoryEstimate(**values, estimate_digest=canonical_digest(content))


@dataclass(frozen=True, slots=True)
class ExpectedTimeOverrideRequest:
    override_id: StableIdentifier
    competitor_id: StableIdentifier
    target_context_digest: str
    expected_raw_time_ms: int
    scope: OverrideScope
    scope_boundary_id: StableIdentifier
    actor: str
    reason: str
    supersedes_override_id: StableIdentifier | None
    request_digest: str

    def __post_init__(self) -> None:
        require_identifier(self.override_id, expected_namespace="override")
        require_identifier(self.competitor_id, expected_namespace="competitor")
        _digest(self.target_context_digest, "target_context_digest")
        _positive_int(self.expected_raw_time_ms, "expected_raw_time_ms")
        if not isinstance(self.scope, OverrideScope):
            raise ContractError("override scope must be deliberately selected")
        _validate_scope_boundary(self.scope, self.scope_boundary_id)
        if (
            not isinstance(self.actor, str)
            or not self.actor.strip()
            or self.actor != self.actor.strip()
        ):
            raise ContractError("override actor must be canonical and nonempty")
        if (
            not isinstance(self.reason, str)
            or not self.reason.strip()
            or self.reason != self.reason.strip()
        ):
            raise ContractError("override reason must be canonical and nonempty")
        if self.supersedes_override_id is not None:
            require_identifier(self.supersedes_override_id, expected_namespace="override")
            if self.supersedes_override_id == self.override_id:
                raise ContractError("override cannot supersede itself")
        _digest(self.request_digest, "request_digest")
        if self.request_digest != canonical_digest(self.content_value()):
            raise ContractError("override request digest mismatch")

    def content_value(self) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-expected-time-override-request-v1",
            "override_id": str(self.override_id),
            "competitor_id": str(self.competitor_id),
            "target_context_digest": self.target_context_digest,
            "expected_raw_time_ms": self.expected_raw_time_ms,
            "scope": self.scope.value,
            "scope_boundary_id": str(self.scope_boundary_id),
            "actor": self.actor,
            "reason": self.reason,
            "supersedes_override_id": (
                str(self.supersedes_override_id) if self.supersedes_override_id else None
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_value(), "request_digest": self.request_digest}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ExpectedTimeOverrideRequest:
        expected = {
            "schema_version",
            "override_id",
            "competitor_id",
            "target_context_digest",
            "expected_raw_time_ms",
            "scope",
            "scope_boundary_id",
            "actor",
            "reason",
            "supersedes_override_id",
            "request_digest",
        }
        if (
            set(value) != expected
            or value["schema_version"] != "strathmark-v3-expected-time-override-request-v1"
        ):
            raise ContractError("override request fields or schema differ")
        try:
            scope = OverrideScope(value["scope"])
        except (TypeError, ValueError) as exc:
            raise ContractError("override scope is unknown") from exc
        return cls(
            require_identifier(value["override_id"], expected_namespace="override"),
            require_identifier(value["competitor_id"], expected_namespace="competitor"),
            value["target_context_digest"],
            value["expected_raw_time_ms"],
            scope,
            require_identifier(value["scope_boundary_id"]),
            value["actor"],
            value["reason"],
            (
                require_identifier(value["supersedes_override_id"], expected_namespace="override")
                if value["supersedes_override_id"] is not None
                else None
            ),
            value["request_digest"],
        )

    @classmethod
    def create(
        cls,
        override_id: StableIdentifier,
        competitor_id: StableIdentifier,
        target_context_digest: str,
        expected_raw_time_ms: int,
        scope: OverrideScope,
        scope_boundary_id: StableIdentifier,
        actor: str,
        reason: str,
        supersedes_override_id: StableIdentifier | None,
    ) -> ExpectedTimeOverrideRequest:
        require_identifier(override_id, expected_namespace="override")
        require_identifier(competitor_id, expected_namespace="competitor")
        _digest(target_context_digest, "target_context_digest")
        _positive_int(expected_raw_time_ms, "expected_raw_time_ms")
        if not isinstance(scope, OverrideScope):
            raise ContractError("override scope must be deliberately selected")
        _validate_scope_boundary(scope, scope_boundary_id)
        if not isinstance(actor, str) or not actor.strip():
            raise ContractError("override actor is required")
        if not isinstance(reason, str) or not reason.strip():
            raise ContractError("override reason is required")
        if supersedes_override_id is not None:
            require_identifier(supersedes_override_id, expected_namespace="override")
            if supersedes_override_id == override_id:
                raise ContractError("override cannot supersede itself")
        values = {
            "override_id": override_id,
            "competitor_id": competitor_id,
            "target_context_digest": target_context_digest,
            "expected_raw_time_ms": expected_raw_time_ms,
            "scope": scope,
            "scope_boundary_id": scope_boundary_id,
            "actor": actor.strip(),
            "reason": reason.strip(),
            "supersedes_override_id": supersedes_override_id,
        }
        content = {
            "schema_version": "strathmark-v3-expected-time-override-request-v1",
            "override_id": str(override_id),
            "competitor_id": str(competitor_id),
            "target_context_digest": target_context_digest,
            "expected_raw_time_ms": expected_raw_time_ms,
            "scope": scope.value,
            "scope_boundary_id": str(scope_boundary_id),
            "actor": actor.strip(),
            "reason": reason.strip(),
            "supersedes_override_id": (
                str(supersedes_override_id) if supersedes_override_id else None
            ),
        }
        return cls(**values, request_digest=canonical_digest(content))


@dataclass(frozen=True, slots=True)
class FieldSheetSnapshot:
    field_id: StableIdentifier
    expected_times_ms: tuple[tuple[StableIdentifier, int], ...]
    marks: tuple[tuple[StableIdentifier, int], ...]
    pool_receipt_digest: str
    optimizer_receipt_digest: str
    optimizer_verification_status: OptimizerVerificationStatus
    sheet_digest: str

    def __post_init__(self) -> None:
        require_identifier(self.field_id, expected_namespace="field")
        expected = _field_values(self.expected_times_ms, "expected time")
        marks = _field_values(self.marks, "mark")
        if expected != self.expected_times_ms or marks != self.marks:
            raise ContractError("field snapshot values must be canonical and sorted")
        if _roster(self) != tuple(item for item, _ in marks):
            raise ContractError("field snapshot times and marks must cover the whole field")
        if min(value for _, value in marks) != 3:
            raise ContractError("field snapshot must be rebased to Mark 3")
        _digest(self.pool_receipt_digest, "pool_receipt_digest")
        _digest(self.optimizer_receipt_digest, "optimizer_receipt_digest")
        if self.optimizer_verification_status is not OptimizerVerificationStatus.PENDING:
            raise ContractError("U14 typed optimizer verifier is required for VERIFIED status")
        _digest(self.sheet_digest, "sheet_digest")
        if self.sheet_digest != canonical_digest(self.content_value()):
            raise ContractError("field snapshot digest mismatch")

    def content_value(self) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-field-sheet-snapshot-v1",
            "field_id": str(self.field_id),
            "expected_times_ms": [(str(key), value) for key, value in self.expected_times_ms],
            "marks": [(str(key), value) for key, value in self.marks],
            "pool_receipt_digest": self.pool_receipt_digest,
            "optimizer_receipt_digest": self.optimizer_receipt_digest,
            "optimizer_verification_status": self.optimizer_verification_status.value,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_value(), "sheet_digest": self.sheet_digest}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> FieldSheetSnapshot:
        expected = {
            "schema_version",
            "field_id",
            "expected_times_ms",
            "marks",
            "pool_receipt_digest",
            "optimizer_receipt_digest",
            "optimizer_verification_status",
            "sheet_digest",
        }
        if (
            set(value) != expected
            or value["schema_version"] != "strathmark-v3-field-sheet-snapshot-v1"
        ):
            raise ContractError("field snapshot fields or schema differ")
        try:
            times = tuple(
                (require_identifier(item[0], expected_namespace="competitor"), item[1])
                for item in value["expected_times_ms"]
            )
            marks = tuple(
                (require_identifier(item[0], expected_namespace="competitor"), item[1])
                for item in value["marks"]
            )
        except (TypeError, IndexError) as exc:
            raise ContractError("field snapshot arrays are invalid") from exc
        try:
            verification = OptimizerVerificationStatus(value["optimizer_verification_status"])
        except (TypeError, ValueError) as exc:
            raise ContractError("optimizer verification status is unknown") from exc
        return cls(
            require_identifier(value["field_id"], expected_namespace="field"),
            times,
            marks,
            value["pool_receipt_digest"],
            value["optimizer_receipt_digest"],
            verification,
            value["sheet_digest"],
        )

    @classmethod
    def create(
        cls,
        *,
        field_id: StableIdentifier,
        expected_times_ms: tuple[tuple[StableIdentifier, int], ...],
        marks: tuple[tuple[StableIdentifier, int], ...],
        pool_receipt_digest: str,
        optimizer_receipt_digest: str,
        optimizer_verification_status: OptimizerVerificationStatus,
    ) -> FieldSheetSnapshot:
        require_identifier(field_id, expected_namespace="field")
        expected = _field_values(expected_times_ms, "expected time")
        mark_values = _field_values(marks, "mark")
        if tuple(item for item, _ in expected) != tuple(item for item, _ in mark_values):
            raise ContractError("expected times and marks must cover the whole identical field")
        if min(value for _, value in mark_values) != 3:
            raise ContractError("field sheet must be rebased to Mark 3")
        _digest(pool_receipt_digest, "pool_receipt_digest")
        _digest(optimizer_receipt_digest, "optimizer_receipt_digest")
        values = {
            "field_id": field_id,
            "expected_times_ms": expected,
            "marks": mark_values,
            "pool_receipt_digest": pool_receipt_digest,
            "optimizer_receipt_digest": optimizer_receipt_digest,
            "optimizer_verification_status": optimizer_verification_status,
        }
        digest = canonical_digest(
            {
                "schema_version": "strathmark-v3-field-sheet-snapshot-v1",
                "field_id": str(field_id),
                "expected_times_ms": [(str(key), value) for key, value in expected],
                "marks": [(str(key), value) for key, value in mark_values],
                "pool_receipt_digest": pool_receipt_digest,
                "optimizer_receipt_digest": optimizer_receipt_digest,
                "optimizer_verification_status": optimizer_verification_status.value,
            }
        )
        return cls(**values, sheet_digest=digest)


@dataclass(frozen=True, slots=True)
class OverrideRecomputationProof:
    field_id: StableIdentifier
    before_sheet_digest: str
    after_sheet_digest: str
    before_pool_receipt_digest: str
    after_pool_receipt_digest: str
    before_optimizer_receipt_digest: str
    after_optimizer_receipt_digest: str
    whole_field_recomputed: bool
    rebased_to_mark_3: bool
    verification_status: OptimizerVerificationStatus
    reoptimized_verified: bool
    proof_digest: str

    def __post_init__(self) -> None:
        require_identifier(self.field_id, expected_namespace="field")
        for value, label in (
            (self.before_sheet_digest, "before_sheet_digest"),
            (self.after_sheet_digest, "after_sheet_digest"),
            (self.before_pool_receipt_digest, "before_pool_receipt_digest"),
            (self.after_pool_receipt_digest, "after_pool_receipt_digest"),
            (self.before_optimizer_receipt_digest, "before_optimizer_receipt_digest"),
            (self.after_optimizer_receipt_digest, "after_optimizer_receipt_digest"),
        ):
            _digest(value, label)
        if self.before_sheet_digest == self.after_sheet_digest:
            raise ContractError("override proof must change the field sheet")
        if self.before_pool_receipt_digest == self.after_pool_receipt_digest:
            raise ContractError("override proof must bind a new pool receipt")
        if self.before_optimizer_receipt_digest == self.after_optimizer_receipt_digest:
            raise ContractError("override proof must bind a new optimizer receipt")
        if not self.whole_field_recomputed or not self.rebased_to_mark_3:
            raise ContractError("override proof flags must attest whole-field recompute and rebase")
        if (
            self.verification_status is not OptimizerVerificationStatus.PENDING
            or self.reoptimized_verified
        ):
            raise ContractError("U14 typed optimizer verifier is required to verify reoptimization")
        _digest(self.proof_digest, "proof_digest")
        if self.proof_digest != canonical_digest(self.content_value()):
            raise ContractError("override recomputation proof digest mismatch")

    def content_value(self) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-override-recomputation-proof-v1",
            "field_id": str(self.field_id),
            "before_sheet_digest": self.before_sheet_digest,
            "after_sheet_digest": self.after_sheet_digest,
            "before_pool_receipt_digest": self.before_pool_receipt_digest,
            "after_pool_receipt_digest": self.after_pool_receipt_digest,
            "before_optimizer_receipt_digest": self.before_optimizer_receipt_digest,
            "after_optimizer_receipt_digest": self.after_optimizer_receipt_digest,
            "whole_field_recomputed": self.whole_field_recomputed,
            "rebased_to_mark_3": self.rebased_to_mark_3,
            "verification_status": self.verification_status.value,
            "reoptimized_verified": self.reoptimized_verified,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_value(), "proof_digest": self.proof_digest}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> OverrideRecomputationProof:
        expected = {
            "schema_version",
            "field_id",
            "before_sheet_digest",
            "after_sheet_digest",
            "before_pool_receipt_digest",
            "after_pool_receipt_digest",
            "before_optimizer_receipt_digest",
            "after_optimizer_receipt_digest",
            "whole_field_recomputed",
            "rebased_to_mark_3",
            "verification_status",
            "reoptimized_verified",
            "proof_digest",
        }
        if (
            set(value) != expected
            or value["schema_version"] != "strathmark-v3-override-recomputation-proof-v1"
        ):
            raise ContractError("override proof fields or schema differ")
        try:
            verification = OptimizerVerificationStatus(value["verification_status"])
        except (TypeError, ValueError) as exc:
            raise ContractError("override proof verification status is unknown") from exc
        return cls(
            require_identifier(value["field_id"], expected_namespace="field"),
            value["before_sheet_digest"],
            value["after_sheet_digest"],
            value["before_pool_receipt_digest"],
            value["after_pool_receipt_digest"],
            value["before_optimizer_receipt_digest"],
            value["after_optimizer_receipt_digest"],
            value["whole_field_recomputed"],
            value["rebased_to_mark_3"],
            verification,
            value["reoptimized_verified"],
            value["proof_digest"],
        )

    @classmethod
    def create(
        cls, before: FieldSheetSnapshot, after: FieldSheetSnapshot
    ) -> OverrideRecomputationProof:
        if not isinstance(before, FieldSheetSnapshot) or not isinstance(after, FieldSheetSnapshot):
            raise ContractError("override proof requires typed before and after sheets")
        if before.field_id != after.field_id or _roster(before) != _roster(after):
            raise ValueError("override must reprocess the whole identical field")
        if before.pool_receipt_digest == after.pool_receipt_digest:
            raise ValueError("override field was not re-pooled")
        if before.optimizer_receipt_digest == after.optimizer_receipt_digest:
            raise ValueError("override field was not re-optimized")
        values = {
            "field_id": before.field_id,
            "before_sheet_digest": before.sheet_digest,
            "after_sheet_digest": after.sheet_digest,
            "before_pool_receipt_digest": before.pool_receipt_digest,
            "after_pool_receipt_digest": after.pool_receipt_digest,
            "before_optimizer_receipt_digest": before.optimizer_receipt_digest,
            "after_optimizer_receipt_digest": after.optimizer_receipt_digest,
            "whole_field_recomputed": True,
            "rebased_to_mark_3": True,
            "verification_status": OptimizerVerificationStatus.PENDING,
            "reoptimized_verified": False,
        }
        content = {
            "schema_version": "strathmark-v3-override-recomputation-proof-v1",
            **values,
            "field_id": str(before.field_id),
        }
        return cls(**values, proof_digest=canonical_digest(content))


@dataclass(frozen=True, slots=True)
class ExpectedTimeOverrideReceipt:
    override_id: StableIdentifier
    request_digest: str
    competitor_id: StableIdentifier
    scope: OverrideScope
    scope_boundary_id: StableIdentifier
    actor: str
    reason: str
    target_context_digest: str
    before_time_ms: int
    after_time_ms: int
    before_sheet: FieldSheetSnapshot
    after_sheet: FieldSheetSnapshot
    affected_competitors: tuple[StableIdentifier, ...]
    recomputation_proof: OverrideRecomputationProof
    assessor_outputs_digest: str
    consensus_digest: str
    evidence_digest: str
    evidence_epoch_id: StableIdentifier
    supersedes_override_id: StableIdentifier | None
    is_result_evidence: bool
    is_training_evidence: bool
    becomes_starting_estimate: bool
    permanently_fixed: bool
    completion_status: OptimizerVerificationStatus
    receipt_digest: str

    def __post_init__(self) -> None:
        require_identifier(self.override_id, expected_namespace="override")
        _digest(self.request_digest, "request_digest")
        require_identifier(self.competitor_id, expected_namespace="competitor")
        if not isinstance(self.scope, OverrideScope):
            raise ContractError("override receipt scope must be typed")
        _validate_scope_boundary(self.scope, self.scope_boundary_id)
        if (
            not isinstance(self.actor, str)
            or not self.actor.strip()
            or self.actor != self.actor.strip()
        ):
            raise ContractError("override receipt actor must be canonical and nonempty")
        if (
            not isinstance(self.reason, str)
            or not self.reason.strip()
            or self.reason != self.reason.strip()
        ):
            raise ContractError("override receipt reason must be canonical and nonempty")
        if (
            self.scope is OverrideScope.UPCOMING_RACE
            and self.scope_boundary_id != self.before_sheet.field_id
        ):
            raise ContractError("upcoming-race scope boundary must bind the affected field")
        if (
            not isinstance(self.before_sheet, FieldSheetSnapshot)
            or not isinstance(self.after_sheet, FieldSheetSnapshot)
            or not isinstance(self.recomputation_proof, OverrideRecomputationProof)
        ):
            raise ContractError("override receipt sheet/proof evidence must be typed")
        if (
            self.before_sheet.field_id != self.after_sheet.field_id
            or self.recomputation_proof.field_id != self.before_sheet.field_id
            or self.recomputation_proof.before_sheet_digest != self.before_sheet.sheet_digest
            or self.recomputation_proof.after_sheet_digest != self.after_sheet.sheet_digest
            or self.recomputation_proof.before_pool_receipt_digest
            != self.before_sheet.pool_receipt_digest
            or self.recomputation_proof.after_pool_receipt_digest
            != self.after_sheet.pool_receipt_digest
            or self.recomputation_proof.before_optimizer_receipt_digest
            != self.before_sheet.optimizer_receipt_digest
            or self.recomputation_proof.after_optimizer_receipt_digest
            != self.after_sheet.optimizer_receipt_digest
        ):
            raise ContractError(
                "override receipt proof does not bind sheet/pool/optimizer authority"
            )
        request_content = {
            "schema_version": "strathmark-v3-expected-time-override-request-v1",
            "override_id": str(self.override_id),
            "competitor_id": str(self.competitor_id),
            "target_context_digest": self.target_context_digest,
            "expected_raw_time_ms": self.after_time_ms,
            "scope": self.scope.value,
            "scope_boundary_id": str(self.scope_boundary_id),
            "actor": self.actor,
            "reason": self.reason,
            "supersedes_override_id": (
                str(self.supersedes_override_id) if self.supersedes_override_id else None
            ),
        }
        if self.request_digest != canonical_digest(request_content):
            raise ContractError("override receipt request digest differs from metadata")
        _positive_int(self.before_time_ms, "before_time_ms")
        _positive_int(self.after_time_ms, "after_time_ms")
        if self.before_time_ms == self.after_time_ms:
            raise ContractError("override receipt must change expected raw time")
        before_times = dict(self.before_sheet.expected_times_ms)
        after_times = dict(self.after_sheet.expected_times_ms)
        before_marks = dict(self.before_sheet.marks)
        after_marks = dict(self.after_sheet.marks)
        if (
            before_times.get(self.competitor_id) != self.before_time_ms
            or after_times.get(self.competitor_id) != self.after_time_ms
            or any(
                before_times[item] != after_times[item]
                for item in before_times
                if item != self.competitor_id
            )
        ):
            raise ContractError("override receipt times differ from whole-field snapshots")
        expected_affected = tuple(
            sorted(
                (
                    item
                    for item in before_times
                    if item == self.competitor_id or before_marks[item] != after_marks[item]
                ),
                key=str,
            )
        )
        if self.affected_competitors != expected_affected:
            raise ContractError("override receipt affected competitors are incomplete")
        for value, label in (
            (self.assessor_outputs_digest, "assessor_outputs_digest"),
            (self.consensus_digest, "consensus_digest"),
            (self.evidence_digest, "evidence_digest"),
        ):
            _digest(value, label)
        require_identifier(self.evidence_epoch_id, expected_namespace="epoch")
        if not all(
            type(value) is bool
            for value in (
                self.is_result_evidence,
                self.is_training_evidence,
                self.becomes_starting_estimate,
                self.permanently_fixed,
            )
        ):
            raise ContractError("override evidence/training/starting-estimate flags must be bool")
        if (
            self.is_result_evidence
            or self.is_training_evidence
            or not self.becomes_starting_estimate
            or self.permanently_fixed
        ):
            raise ContractError("override evidence/training/starting-estimate flags are invalid")
        if self.completion_status is not OptimizerVerificationStatus.PENDING:
            raise ContractError("U14 typed optimizer verifier is required to complete override")
        _digest(self.receipt_digest, "receipt_digest")
        if self.receipt_digest != canonical_digest(self.content_value()):
            raise ContractError("override receipt digest mismatch")

    def content_value(self) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-expected-time-override-receipt-v1",
            "override_id": str(self.override_id),
            "request_digest": self.request_digest,
            "competitor_id": str(self.competitor_id),
            "scope": self.scope.value,
            "scope_boundary_id": str(self.scope_boundary_id),
            "actor": self.actor,
            "reason": self.reason,
            "target_context_digest": self.target_context_digest,
            "before_time_ms": self.before_time_ms,
            "after_time_ms": self.after_time_ms,
            "before_sheet_digest": self.before_sheet.sheet_digest,
            "after_sheet_digest": self.after_sheet.sheet_digest,
            "affected_competitors": [str(item) for item in self.affected_competitors],
            "recomputation_proof_digest": self.recomputation_proof.proof_digest,
            "assessor_outputs_digest": self.assessor_outputs_digest,
            "consensus_digest": self.consensus_digest,
            "evidence_digest": self.evidence_digest,
            "evidence_epoch_id": str(self.evidence_epoch_id),
            "supersedes_override_id": (
                str(self.supersedes_override_id) if self.supersedes_override_id else None
            ),
            "is_result_evidence": self.is_result_evidence,
            "is_training_evidence": self.is_training_evidence,
            "becomes_starting_estimate": self.becomes_starting_estimate,
            "permanently_fixed": self.permanently_fixed,
            "completion_status": self.completion_status.value,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_value(),
            "before_sheet": self.before_sheet.to_dict(),
            "after_sheet": self.after_sheet.to_dict(),
            "recomputation_proof": self.recomputation_proof.to_dict(),
            "receipt_digest": self.receipt_digest,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ExpectedTimeOverrideReceipt:
        expected = {
            "schema_version",
            "override_id",
            "request_digest",
            "competitor_id",
            "scope",
            "scope_boundary_id",
            "actor",
            "reason",
            "target_context_digest",
            "before_time_ms",
            "after_time_ms",
            "before_sheet_digest",
            "after_sheet_digest",
            "affected_competitors",
            "recomputation_proof_digest",
            "assessor_outputs_digest",
            "consensus_digest",
            "evidence_digest",
            "evidence_epoch_id",
            "supersedes_override_id",
            "is_result_evidence",
            "is_training_evidence",
            "becomes_starting_estimate",
            "permanently_fixed",
            "completion_status",
            "before_sheet",
            "after_sheet",
            "recomputation_proof",
            "receipt_digest",
        }
        if (
            set(value) != expected
            or value["schema_version"] != "strathmark-v3-expected-time-override-receipt-v1"
        ):
            raise ContractError("override receipt fields or schema differ")
        try:
            scope = OverrideScope(value["scope"])
            completion = OptimizerVerificationStatus(value["completion_status"])
        except (TypeError, ValueError) as exc:
            raise ContractError("override receipt scope is unknown") from exc
        return cls(
            override_id=require_identifier(value["override_id"], expected_namespace="override"),
            request_digest=value["request_digest"],
            competitor_id=require_identifier(
                value["competitor_id"], expected_namespace="competitor"
            ),
            scope=scope,
            scope_boundary_id=require_identifier(value["scope_boundary_id"]),
            actor=value["actor"],
            reason=value["reason"],
            target_context_digest=value["target_context_digest"],
            before_time_ms=value["before_time_ms"],
            after_time_ms=value["after_time_ms"],
            before_sheet=FieldSheetSnapshot.from_dict(value["before_sheet"]),
            after_sheet=FieldSheetSnapshot.from_dict(value["after_sheet"]),
            affected_competitors=tuple(
                require_identifier(item, expected_namespace="competitor")
                for item in value["affected_competitors"]
            ),
            recomputation_proof=OverrideRecomputationProof.from_dict(value["recomputation_proof"]),
            assessor_outputs_digest=value["assessor_outputs_digest"],
            consensus_digest=value["consensus_digest"],
            evidence_digest=value["evidence_digest"],
            evidence_epoch_id=require_identifier(
                value["evidence_epoch_id"], expected_namespace="epoch"
            ),
            supersedes_override_id=(
                require_identifier(value["supersedes_override_id"], expected_namespace="override")
                if value["supersedes_override_id"] is not None
                else None
            ),
            is_result_evidence=value["is_result_evidence"],
            is_training_evidence=value["is_training_evidence"],
            becomes_starting_estimate=value["becomes_starting_estimate"],
            permanently_fixed=value["permanently_fixed"],
            completion_status=completion,
            receipt_digest=value["receipt_digest"],
        )


def create_override_receipt(
    request: ExpectedTimeOverrideRequest,
    before: FieldSheetSnapshot,
    after: FieldSheetSnapshot,
    proof: OverrideRecomputationProof,
    assessor_outputs_digest: str,
    consensus_digest: str,
    evidence_digest: str,
    evidence_epoch_id: StableIdentifier,
) -> ExpectedTimeOverrideReceipt:
    if not all(
        (
            isinstance(request, ExpectedTimeOverrideRequest),
            isinstance(before, FieldSheetSnapshot),
            isinstance(after, FieldSheetSnapshot),
            isinstance(proof, OverrideRecomputationProof),
        )
    ):
        raise ContractError("override receipt requires typed request, sheets, and proof")
    if before.field_id != after.field_id or _roster(before) != _roster(after):
        raise ValueError("override must cover the whole identical field")
    if request.competitor_id not in _roster(before):
        raise ValueError("override competitor is absent from the field")
    if (
        proof.before_sheet_digest != before.sheet_digest
        or proof.after_sheet_digest != after.sheet_digest
    ):
        raise ValueError("override recomputation proof does not bind the supplied sheets")
    before_times, after_times = dict(before.expected_times_ms), dict(after.expected_times_ms)
    if after_times[request.competitor_id] != request.expected_raw_time_ms:
        raise ValueError("override must change expected raw time to the requested value")
    if before_times[request.competitor_id] == after_times[request.competitor_id]:
        raise ValueError("override must change the starting estimate")
    if any(
        before_times[item] != after_times[item]
        for item in before_times
        if item != request.competitor_id
    ):
        raise ValueError("one expected-time override cannot rewrite another competitor")
    for value, label in (
        (assessor_outputs_digest, "assessor_outputs_digest"),
        (consensus_digest, "consensus_digest"),
        (evidence_digest, "evidence_digest"),
    ):
        _digest(value, label)
    require_identifier(evidence_epoch_id, expected_namespace="epoch")
    before_marks, after_marks = dict(before.marks), dict(after.marks)
    affected = tuple(
        sorted(
            (
                item
                for item in before_times
                if item == request.competitor_id or before_marks[item] != after_marks[item]
            ),
            key=str,
        )
    )
    values = {
        "override_id": request.override_id,
        "request_digest": request.request_digest,
        "competitor_id": request.competitor_id,
        "scope": request.scope,
        "scope_boundary_id": request.scope_boundary_id,
        "actor": request.actor,
        "reason": request.reason,
        "target_context_digest": request.target_context_digest,
        "before_time_ms": before_times[request.competitor_id],
        "after_time_ms": after_times[request.competitor_id],
        "before_sheet": before,
        "after_sheet": after,
        "affected_competitors": affected,
        "recomputation_proof": proof,
        "assessor_outputs_digest": assessor_outputs_digest,
        "consensus_digest": consensus_digest,
        "evidence_digest": evidence_digest,
        "evidence_epoch_id": evidence_epoch_id,
        "supersedes_override_id": request.supersedes_override_id,
        "is_result_evidence": False,
        "is_training_evidence": False,
        "becomes_starting_estimate": True,
        "permanently_fixed": False,
        "completion_status": OptimizerVerificationStatus.PENDING,
    }
    return ExpectedTimeOverrideReceipt(
        **values, receipt_digest=canonical_digest(_override_receipt_content(values))
    )


def _field_values(
    values: tuple[tuple[StableIdentifier, int], ...], label: str
) -> tuple[tuple[StableIdentifier, int], ...]:
    if not isinstance(values, tuple) or not values:
        raise ContractError(f"{label} field values must be a nonempty immutable tuple")
    checked = []
    for key, value in values:
        require_identifier(key, expected_namespace="competitor")
        _positive_int(value, label)
        checked.append((key, value))
    ordered = tuple(sorted(checked, key=lambda item: str(item[0])))
    if len(ordered) != len({item for item, _ in ordered}):
        raise ContractError(f"{label} competitor identities must be unique")
    return ordered


def _roster(sheet: FieldSheetSnapshot) -> tuple[StableIdentifier, ...]:
    return tuple(item for item, _ in sheet.expected_times_ms)


def _source(value: AssessorKind | str) -> None:
    if isinstance(value, AssessorKind):
        if value is AssessorKind.LLM_MEMBER:
            raise ContractError("LLM member is dissent, not an outer counterfactual sheet")
    elif value != "pooled":
        raise ContractError("counterfactual source must be an outer assessor or pooled")


def _source_name(value: AssessorKind | str) -> str:
    _source(value)
    return value.value if isinstance(value, AssessorKind) else value


def _color_rank(value: ConsequenceColor) -> int:
    return {
        ConsequenceColor.GREEN: 0,
        ConsequenceColor.AMBER: 1,
        ConsequenceColor.RED: 2,
    }[value]


def _decision_content(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "strathmark-v3-disagreement-decision-v1",
        "color": values["color"].value,
        "operational_status": values["operational_status"].value,
        "manual_review_required": values["manual_review_required"],
        "pooled_sheet_digest": values["pooled_sheet"].sheet_digest,
        "component_sheet_digests": [item.sheet_digest for item in values["component_sheets"]],
        "comparisons": [item.to_dict() for item in values["comparisons"]],
        "council_audit_digest": (
            None if values["council_audit"] is None else values["council_audit"].audit_digest
        ),
        "policy": values["policy"].to_dict(),
        "policy_digest": values["policy"].digest,
        "assessor_availability": [
            (item.value, available) for item, available in values["assessor_availability"]
        ],
    }


def _override_receipt_content(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "strathmark-v3-expected-time-override-receipt-v1",
        "override_id": str(values["override_id"]),
        "request_digest": values["request_digest"],
        "competitor_id": str(values["competitor_id"]),
        "scope": values["scope"].value,
        "scope_boundary_id": str(values["scope_boundary_id"]),
        "actor": values["actor"],
        "reason": values["reason"],
        "target_context_digest": values["target_context_digest"],
        "before_time_ms": values["before_time_ms"],
        "after_time_ms": values["after_time_ms"],
        "before_sheet_digest": values["before_sheet"].sheet_digest,
        "after_sheet_digest": values["after_sheet"].sheet_digest,
        "affected_competitors": [str(item) for item in values["affected_competitors"]],
        "recomputation_proof_digest": values["recomputation_proof"].proof_digest,
        "assessor_outputs_digest": values["assessor_outputs_digest"],
        "consensus_digest": values["consensus_digest"],
        "evidence_digest": values["evidence_digest"],
        "evidence_epoch_id": str(values["evidence_epoch_id"]),
        "supersedes_override_id": (
            str(values["supersedes_override_id"]) if values["supersedes_override_id"] else None
        ),
        "is_result_evidence": values["is_result_evidence"],
        "is_training_evidence": values["is_training_evidence"],
        "becomes_starting_estimate": values["becomes_starting_estimate"],
        "permanently_fixed": values["permanently_fixed"],
        "completion_status": values["completion_status"].value,
    }


def _validate_scope_boundary(scope: OverrideScope, boundary: StableIdentifier) -> None:
    expected_namespace = {
        OverrideScope.UPCOMING_RACE: "field",
        OverrideScope.REMAINING_EVENT_CONFIGURATION: "event_config",
        OverrideScope.REMAINING_TOURNAMENT: "tournament",
    }[scope]
    require_identifier(boundary, expected_namespace=expected_namespace)


def _probability(value: str, label: str) -> Decimal:
    number = _decimal(value, label)
    if not 0 <= number <= 1:
        raise ContractError(f"{label} must be between zero and one")
    return number


def _decimal(value: str, label: str) -> Decimal:
    if not isinstance(value, str) or canonical_decimal_string(value) != value:
        raise ContractError(f"{label} must be a canonical decimal string")
    return Decimal(value)


def _positive_int(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ContractError(f"{label} must be a positive integer")


def _nonnegative_int(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractError(f"{label} must be a nonnegative integer")


def _digest(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(item not in "0123456789abcdef" for item in value)
    ):
        raise ContractError(f"{label} must be a lowercase SHA-256 digest")


__all__ = [
    "CouncilAudit",
    "CouncilMemberAudit",
    "CouncilMemberStatus",
    "ConsequenceColor",
    "ConsequenceComparison",
    "CounterfactualCompetitor",
    "CounterfactualSheet",
    "DisagreementThresholds",
    "DisjointThresholdVerification",
    "DisagreementDecision",
    "DisagreementPolicy",
    "ExpectedTimeOverrideReceipt",
    "ExpectedTimeOverrideRequest",
    "FieldSheetSnapshot",
    "OverrideRecomputationProof",
    "OverrideScope",
    "OptimizerVerificationStatus",
    "HistoricalThresholdSelection",
    "ThresholdReplayObservation",
    "ZeroHistoryEstimate",
    "ZeroHistoryPolicy",
    "classify_disagreement",
    "create_override_receipt",
    "create_zero_history_estimate",
    "freeze_disagreement_policy",
    "select_historical_thresholds",
    "verify_disjoint_thresholds",
]
