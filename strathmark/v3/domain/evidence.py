"""Pure evidence-governor rules for live and imported result revisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.evidence import ResultObservation, TargetContext
from strathmark.v3.contracts.identifiers import StableIdentifier, require_identifier
from strathmark.v3.contracts.statuses import OfficialResult, admit_raw_completion


@dataclass(frozen=True, slots=True)
class LiveResultSubmission:
    """Upstream result facts with no caller-controlled database sequence."""

    evidence_id: StableIdentifier
    competitor_id: StableIdentifier
    tournament_id: StableIdentifier
    round_id: StableIdentifier
    field_id: StableIdentifier
    context: TargetContext
    occurred_at_utc: str
    issued_mark: int
    completion_clock_ms: int | None
    placing: int | None
    gap_ms: int | None
    result: OfficialResult
    source_digest: str

    def to_observation(self, authoritative_sequence: int) -> ResultObservation:
        return ResultObservation(
            self.evidence_id,
            self.competitor_id,
            self.tournament_id,
            self.round_id,
            self.field_id,
            self.context,
            authoritative_sequence,
            self.occurred_at_utc,
            self.issued_mark,
            self.completion_clock_ms,
            self.placing,
            self.gap_ms,
            self.result,
            self.source_digest,
        )

    def __post_init__(self) -> None:
        self.to_observation(1)

    def to_dict(self) -> dict[str, object]:
        value = self.to_observation(1).to_dict()
        del value["observation_sequence"]
        value["schema_version"] = "strathmark-v3-live-result-submission-v1"
        return value

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> LiveResultSubmission:
        expected = set(
            ResultObservation.from_dict(
                {
                    **value,
                    "schema_version": "strathmark-v3-result-observation-v1",
                    "observation_sequence": 1,
                }
            ).to_dict()
        ) - {"schema_version", "observation_sequence"}
        if (
            set(value) != expected | {"schema_version"}
            or value.get("schema_version") != "strathmark-v3-live-result-submission-v1"
        ):
            raise ContractError("live result submission fields are not closed")
        observation = ResultObservation.from_dict(
            {
                **value,
                "schema_version": "strathmark-v3-result-observation-v1",
                "observation_sequence": 1,
            }
        )
        return cls(
            observation.evidence_id,
            observation.competitor_id,
            observation.tournament_id,
            observation.round_id,
            observation.field_id,
            observation.context,
            observation.occurred_at_utc,
            observation.issued_mark,
            observation.completion_clock_ms,
            observation.placing,
            observation.gap_ms,
            observation.result,
            observation.source_digest,
        )


class EvidenceSource(str, Enum):
    LIVE_ISSUED_RACE = "live_issued_race"
    HISTORICAL_IMPORT = "historical_import"


class AdmissionReason(str, Enum):
    ELIGIBLE_COMPLETION = "eligible_completion"
    STATUS_INELIGIBLE = "status_ineligible"
    UNISSUED = "unissued"
    WRONG_FIELD_REVISION = "wrong_field_revision"
    WRONG_ISSUED_RECEIPT = "wrong_issued_receipt"
    NON_MEMBER = "non_member"
    WRONG_FIELD_LINEAGE = "wrong_field_lineage"
    WRONG_TARGET_CONTEXT = "wrong_target_context"
    WRONG_ISSUED_MARK = "wrong_issued_mark"
    HISTORICAL_UNCUTOVER = "historical_uncutover"
    HISTORICAL_CUTOVER = "historical_cutover"


@dataclass(frozen=True, slots=True)
class IssuedFieldFact:
    """The exact acknowledged sheet against which live outcomes are admitted."""

    field_id: StableIdentifier
    upstream_revision: int
    competitor_ids: tuple[StableIdentifier, ...]
    receipt_id: StableIdentifier
    tournament_id: StableIdentifier
    round_id: StableIdentifier
    context: TargetContext
    issued_marks: tuple[tuple[StableIdentifier, int], ...]

    def __post_init__(self) -> None:
        require_identifier(self.field_id, expected_namespace="field")
        require_identifier(self.receipt_id, expected_namespace="receipt")
        require_identifier(self.tournament_id, expected_namespace="tournament")
        require_identifier(self.round_id, expected_namespace="round")
        if not isinstance(self.context, TargetContext):
            raise ContractError("issued field context must be a TargetContext")
        if (
            isinstance(self.upstream_revision, bool)
            or not isinstance(self.upstream_revision, int)
            or self.upstream_revision <= 0
        ):
            raise ContractError("issued field upstream revision must be positive")
        if not isinstance(self.competitor_ids, tuple) or not self.competitor_ids:
            raise ContractError("issued field requires an immutable nonempty roster")
        for competitor_id in self.competitor_ids:
            require_identifier(competitor_id, expected_namespace="competitor")
        if len(set(self.competitor_ids)) != len(self.competitor_ids):
            raise ContractError("issued field roster cannot contain duplicates")
        if not isinstance(self.issued_marks, tuple):
            raise ContractError("issued marks must be immutable")
        marks: list[StableIdentifier] = []
        for item in self.issued_marks:
            if not isinstance(item, tuple) or len(item) != 2:
                raise ContractError("issued marks must be competitor/mark pairs")
            competitor_id, mark = item
            require_identifier(competitor_id, expected_namespace="competitor")
            if isinstance(mark, bool) or not isinstance(mark, int) or mark <= 0:
                raise ContractError("issued mark must be a positive integer")
            marks.append(competitor_id)
        if tuple(marks) != self.competitor_ids:
            raise ContractError("issued marks must follow and exactly cover the roster")


@dataclass(frozen=True, slots=True)
class AdmittedEvidence:
    observation: ResultObservation
    source: EvidenceSource
    numeric_eligible: bool
    raw_time_ms: int | None
    reason: AdmissionReason


def admit_observation(
    observation: ResultObservation,
    *,
    issued_field: IssuedFieldFact | None,
    field_revision: int | None,
    claimed_receipt_id: StableIdentifier | None = None,
    source: EvidenceSource = EvidenceSource.LIVE_ISSUED_RACE,
    historical_cutover_signed: bool = False,
) -> AdmittedEvidence:
    """Classify a result exactly once for all numeric consumers.

    Penalties retain their observed raw time in the immutable source contract but
    are not admitted as model evidence.  No adjusted completion is manufactured.
    """

    if not isinstance(observation, ResultObservation):
        raise ContractError("evidence admission requires a ResultObservation")
    if not isinstance(source, EvidenceSource):
        raise ContractError("evidence source must use the closed vocabulary")
    if not isinstance(historical_cutover_signed, bool):
        raise ContractError("historical cutover state must be explicit")

    if source is EvidenceSource.HISTORICAL_IMPORT:
        if not historical_cutover_signed:
            return AdmittedEvidence(
                observation, source, False, None, AdmissionReason.HISTORICAL_UNCUTOVER
            )
        completion = admit_raw_completion(observation.result)
        return AdmittedEvidence(
            observation,
            source,
            completion is not None,
            None if completion is None else completion.raw_time_ms,
            AdmissionReason.HISTORICAL_CUTOVER
            if completion is not None
            else AdmissionReason.STATUS_INELIGIBLE,
        )

    if issued_field is None:
        return AdmittedEvidence(observation, source, False, None, AdmissionReason.UNISSUED)
    if not isinstance(issued_field, IssuedFieldFact):
        raise ContractError("issued_field must be an IssuedFieldFact")
    if claimed_receipt_id != issued_field.receipt_id:
        return AdmittedEvidence(
            observation, source, False, None, AdmissionReason.WRONG_ISSUED_RECEIPT
        )
    if (
        field_revision != issued_field.upstream_revision
        or observation.field_id != issued_field.field_id
    ):
        return AdmittedEvidence(
            observation, source, False, None, AdmissionReason.WRONG_FIELD_REVISION
        )
    if observation.competitor_id not in issued_field.competitor_ids:
        return AdmittedEvidence(observation, source, False, None, AdmissionReason.NON_MEMBER)
    if (
        observation.tournament_id != issued_field.tournament_id
        or observation.round_id != issued_field.round_id
    ):
        return AdmittedEvidence(
            observation, source, False, None, AdmissionReason.WRONG_FIELD_LINEAGE
        )
    if observation.context != issued_field.context:
        return AdmittedEvidence(
            observation, source, False, None, AdmissionReason.WRONG_TARGET_CONTEXT
        )
    issued_marks = dict(issued_field.issued_marks)
    if observation.issued_mark != issued_marks[observation.competitor_id]:
        return AdmittedEvidence(observation, source, False, None, AdmissionReason.WRONG_ISSUED_MARK)
    completion = admit_raw_completion(observation.result)
    if completion is None:
        return AdmittedEvidence(observation, source, False, None, AdmissionReason.STATUS_INELIGIBLE)
    return AdmittedEvidence(
        observation,
        source,
        True,
        completion.raw_time_ms,
        AdmissionReason.ELIGIBLE_COMPLETION,
    )


def validate_result_revision(
    previous: ResultObservation | None, replacement: ResultObservation
) -> ResultObservation:
    """Require an immutable, consecutive replacement of the same live outcome."""

    if not isinstance(replacement, ResultObservation):
        raise ContractError("replacement must be a ResultObservation")
    if previous is None:
        if replacement.result.revision != 1 or replacement.result.supersedes_revision is not None:
            raise ContractError("the first outcome must be revision 1")
        return replacement
    if not isinstance(previous, ResultObservation):
        raise ContractError("previous must be a ResultObservation")
    identity = (
        "competitor_id",
        "tournament_id",
        "round_id",
        "field_id",
        "context",
    )
    if any(getattr(previous, item) != getattr(replacement, item) for item in identity):
        raise ContractError("a revision must replace the same live outcome")
    expected = previous.result.revision + 1
    if (
        replacement.result.revision != expected
        or replacement.result.supersedes_revision != previous.result.revision
    ):
        raise ContractError("a result revision must be the next exact immutable successor")
    return replacement


__all__ = [
    "AdmissionReason",
    "AdmittedEvidence",
    "EvidenceSource",
    "IssuedFieldFact",
    "LiveResultSubmission",
    "admit_observation",
    "validate_result_revision",
]
