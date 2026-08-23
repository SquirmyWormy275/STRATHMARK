from __future__ import annotations

from dataclasses import replace

import pytest

from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.contracts.evidence import ResultObservation, TargetContext
from strathmark.v3.contracts.identifiers import StableIdentifier
from strathmark.v3.contracts.statuses import OfficialResult, ResultStatus
from strathmark.v3.domain.evidence import (
    AdmissionReason,
    EvidenceSource,
    IssuedFieldFact,
    LiveResultSubmission,
    admit_observation,
    validate_result_revision,
)


def _observation(
    *, status: ResultStatus = ResultStatus.COMPLETION, revision: int = 1
) -> ResultObservation:
    raw = 12_345 if status in {ResultStatus.COMPLETION, ResultStatus.PENALTY} else None
    penalty = 500 if status is ResultStatus.PENALTY else None
    return ResultObservation(
        evidence_id=StableIdentifier(f"evidence:field-a-competitor-a-r{revision}"),
        competitor_id=StableIdentifier("competitor:a"),
        tournament_id=StableIdentifier("tournament:show"),
        round_id=StableIdentifier("round:heat"),
        field_id=StableIdentifier("field:a"),
        context=TargetContext("underhand", 300, "wood", "tax:v1", "convert:v1"),
        observation_sequence=revision,
        occurred_at_utc="2026-08-22T01:02:03.004Z",
        issued_mark=3,
        completion_clock_ms=15_345
        if status in {ResultStatus.COMPLETION, ResultStatus.PENALTY}
        else None,
        placing=1 if status in {ResultStatus.COMPLETION, ResultStatus.PENALTY} else None,
        gap_ms=0 if status in {ResultStatus.COMPLETION, ResultStatus.PENALTY} else None,
        result=OfficialResult(status, raw, penalty, revision, revision - 1 or None),
        source_digest=canonical_digest({"revision": revision, "status": status.value}),
    )


def _issued() -> IssuedFieldFact:
    return IssuedFieldFact(
        field_id=StableIdentifier("field:a"),
        upstream_revision=7,
        competitor_ids=(StableIdentifier("competitor:a"), StableIdentifier("competitor:b")),
        receipt_id=StableIdentifier("receipt:issued-a"),
        tournament_id=StableIdentifier("tournament:show"),
        round_id=StableIdentifier("round:heat"),
        context=TargetContext("underhand", 300, "wood", "tax:v1", "convert:v1"),
        issued_marks=(
            (StableIdentifier("competitor:a"), 3),
            (StableIdentifier("competitor:b"), 5),
        ),
    )


def test_only_exact_issued_live_completion_is_numeric_evidence() -> None:
    admitted = admit_observation(
        _observation(),
        issued_field=_issued(),
        field_revision=7,
        claimed_receipt_id=StableIdentifier("receipt:issued-a"),
    )
    assert admitted.numeric_eligible is True
    assert admitted.reason is AdmissionReason.ELIGIBLE_COMPLETION
    assert admitted.raw_time_ms == 12_345

    for status in (ResultStatus.DNF, ResultStatus.DQ, ResultStatus.DNS, ResultStatus.VOID):
        classified = admit_observation(
            _observation(status=status),
            issued_field=_issued(),
            field_revision=7,
            claimed_receipt_id=StableIdentifier("receipt:issued-a"),
        )
        assert classified.numeric_eligible is False
        assert classified.raw_time_ms is None
        assert classified.reason is AdmissionReason.STATUS_INELIGIBLE

    penalized = admit_observation(
        _observation(status=ResultStatus.PENALTY),
        issued_field=_issued(),
        field_revision=7,
        claimed_receipt_id=StableIdentifier("receipt:issued-a"),
    )
    assert penalized.numeric_eligible is False
    assert penalized.raw_time_ms is None


def test_wrong_revision_nonmember_unissued_and_historical_fail_closed() -> None:
    observation = _observation()
    assert (
        admit_observation(observation, issued_field=None, field_revision=7).reason
        is AdmissionReason.UNISSUED
    )
    assert (
        admit_observation(
            observation,
            issued_field=_issued(),
            field_revision=7,
            claimed_receipt_id=StableIdentifier("receipt:wrong"),
        ).reason
        is AdmissionReason.WRONG_ISSUED_RECEIPT
    )
    assert (
        admit_observation(
            observation,
            issued_field=_issued(),
            field_revision=8,
            claimed_receipt_id=StableIdentifier("receipt:issued-a"),
        ).reason
        is AdmissionReason.WRONG_FIELD_REVISION
    )
    nonmember = replace(observation, competitor_id=StableIdentifier("competitor:outsider"))
    assert (
        admit_observation(
            nonmember,
            issued_field=_issued(),
            field_revision=7,
            claimed_receipt_id=StableIdentifier("receipt:issued-a"),
        ).reason
        is AdmissionReason.NON_MEMBER
    )
    imported = admit_observation(
        observation,
        issued_field=None,
        field_revision=None,
        source=EvidenceSource.HISTORICAL_IMPORT,
    )
    assert imported.reason is AdmissionReason.HISTORICAL_UNCUTOVER
    assert imported.numeric_eligible is False
    wrong_field = replace(observation, field_id=StableIdentifier("field:other"))
    assert (
        admit_observation(
            wrong_field,
            issued_field=_issued(),
            field_revision=7,
            claimed_receipt_id=StableIdentifier("receipt:issued-a"),
        ).reason
        is AdmissionReason.WRONG_FIELD_REVISION
    )
    cutover = admit_observation(
        observation,
        issued_field=None,
        field_revision=None,
        source=EvidenceSource.HISTORICAL_IMPORT,
        historical_cutover_signed=True,
    )
    assert cutover.reason is AdmissionReason.HISTORICAL_CUTOVER
    assert cutover.numeric_eligible is True


def test_tournament_round_and_target_context_must_match_the_exact_issue() -> None:
    for changed, reason in (
        (
            replace(_observation(), tournament_id=StableIdentifier("tournament:other")),
            AdmissionReason.WRONG_FIELD_LINEAGE,
        ),
        (
            replace(_observation(), round_id=StableIdentifier("round:other")),
            AdmissionReason.WRONG_FIELD_LINEAGE,
        ),
        (
            replace(
                _observation(),
                context=TargetContext("underhand", 325, "wood", "tax:v1", "convert:v1"),
            ),
            AdmissionReason.WRONG_TARGET_CONTEXT,
        ),
    ):
        classified = admit_observation(
            changed,
            issued_field=_issued(),
            field_revision=7,
            claimed_receipt_id=StableIdentifier("receipt:issued-a"),
        )
        assert classified.numeric_eligible is False
        assert classified.reason is reason
    wrong_mark = admit_observation(
        replace(_observation(), issued_mark=4),
        issued_field=_issued(),
        field_revision=7,
        claimed_receipt_id=StableIdentifier("receipt:issued-a"),
    )
    assert wrong_mark.reason is AdmissionReason.WRONG_ISSUED_MARK
    assert wrong_mark.numeric_eligible is False


def test_live_submission_contract_has_no_sequence_to_spoof() -> None:
    observation = _observation()
    raw = observation.to_dict()
    raw.pop("observation_sequence")
    raw["schema_version"] = "strathmark-v3-live-result-submission-v1"
    submission = LiveResultSubmission.from_dict(raw)
    assert "observation_sequence" not in submission.to_dict()
    raw["observation_sequence"] = 999
    with pytest.raises(Exception, match="closed"):
        LiveResultSubmission.from_dict(raw)


def test_result_revisions_are_strict_immutable_successors() -> None:
    first = _observation()
    second = _observation(revision=2)
    assert validate_result_revision(None, first) is first
    assert validate_result_revision(first, second) is second
    with pytest.raises(Exception, match="revision"):
        validate_result_revision(first, replace(second, result=replace(second.result, revision=3)))
    with pytest.raises(Exception, match="same live outcome"):
        validate_result_revision(
            first, replace(second, competitor_id=StableIdentifier("competitor:b"))
        )
    with pytest.raises(Exception, match="same live outcome"):
        validate_result_revision(
            first,
            replace(
                second,
                context=TargetContext("standing", 300, "wood", "tax:v1", "convert:v1"),
            ),
        )


@pytest.mark.parametrize("revision", [True, "7", 0])
def test_issued_field_rejects_invalid_revisions(revision) -> None:
    with pytest.raises(Exception, match="revision"):
        IssuedFieldFact(
            StableIdentifier("field:a"),
            revision,
            (StableIdentifier("competitor:a"),),
            StableIdentifier("receipt:a"),
            StableIdentifier("tournament:show"),
            StableIdentifier("round:heat"),
            TargetContext("underhand", 300, "wood", "tax:v1", "convert:v1"),
            ((StableIdentifier("competitor:a"), 3),),
        )


def test_issued_field_rejects_mutable_empty_and_duplicate_rosters() -> None:
    for roster in ([], ()):
        with pytest.raises(Exception, match="roster"):
            IssuedFieldFact(
                StableIdentifier("field:a"),
                1,
                roster,
                StableIdentifier("receipt:a"),
                StableIdentifier("tournament:show"),
                StableIdentifier("round:heat"),
                TargetContext("underhand", 300, "wood", "tax:v1", "convert:v1"),
                tuple((competitor, 3) for competitor in roster),
            )
    with pytest.raises(Exception, match="duplicates"):
        IssuedFieldFact(
            StableIdentifier("field:a"),
            1,
            (StableIdentifier("competitor:a"), StableIdentifier("competitor:a")),
            StableIdentifier("receipt:a"),
            StableIdentifier("tournament:show"),
            StableIdentifier("round:heat"),
            TargetContext("underhand", 300, "wood", "tax:v1", "convert:v1"),
            (
                (StableIdentifier("competitor:a"), 3),
                (StableIdentifier("competitor:a"), 3),
            ),
        )


@pytest.mark.parametrize(
    ("marks", "message"),
    (
        ([], "immutable"),
        ((["competitor:a", 3],), "pairs"),
        (((StableIdentifier("competitor:a"),),), "pairs"),
        (((StableIdentifier("competitor:a"), True),), "positive"),
        (((StableIdentifier("competitor:a"), "3"),), "positive"),
        (((StableIdentifier("competitor:a"), 0),), "positive"),
        (((StableIdentifier("competitor:b"), 3),), "exactly cover"),
    ),
)
def test_issued_field_rejects_malformed_or_nonpositive_marks(marks, message) -> None:
    with pytest.raises(Exception, match=message):
        IssuedFieldFact(
            StableIdentifier("field:a"),
            1,
            (StableIdentifier("competitor:a"),),
            StableIdentifier("receipt:a"),
            StableIdentifier("tournament:show"),
            StableIdentifier("round:heat"),
            TargetContext("underhand", 300, "wood", "tax:v1", "convert:v1"),
            marks,
        )


def test_issued_field_requires_typed_target_context() -> None:
    with pytest.raises(Exception, match="TargetContext"):
        IssuedFieldFact(
            StableIdentifier("field:a"),
            1,
            (StableIdentifier("competitor:a"),),
            StableIdentifier("receipt:a"),
            StableIdentifier("tournament:show"),
            StableIdentifier("round:heat"),
            {},  # type: ignore[arg-type]
            ((StableIdentifier("competitor:a"), 3),),
        )


def test_admission_rejects_open_typed_boundaries_and_signed_noncompletion_stays_ineligible() -> (
    None
):
    observation = _observation()
    with pytest.raises(Exception, match="ResultObservation"):
        admit_observation(None, issued_field=None, field_revision=None)  # type: ignore[arg-type]
    with pytest.raises(Exception, match="closed vocabulary"):
        admit_observation(
            observation,
            issued_field=None,
            field_revision=None,
            source="live",  # type: ignore[arg-type]
        )
    with pytest.raises(Exception, match="explicit"):
        admit_observation(
            observation,
            issued_field=None,
            field_revision=None,
            historical_cutover_signed=1,  # type: ignore[arg-type]
        )
    with pytest.raises(Exception, match="IssuedFieldFact"):
        admit_observation(
            observation,
            issued_field="issued",  # type: ignore[arg-type]
            field_revision=1,
        )
    classified = admit_observation(
        _observation(status=ResultStatus.DNS),
        issued_field=None,
        field_revision=None,
        source=EvidenceSource.HISTORICAL_IMPORT,
        historical_cutover_signed=True,
    )
    assert classified.reason is AdmissionReason.STATUS_INELIGIBLE
    assert classified.raw_time_ms is None


def test_revision_validator_rejects_invalid_genesis_and_open_types() -> None:
    first = _observation()
    with pytest.raises(Exception, match="replacement"):
        validate_result_revision(None, None)  # type: ignore[arg-type]
    with pytest.raises(Exception, match="revision 1"):
        validate_result_revision(None, _observation(revision=2))
    with pytest.raises(Exception, match="previous"):
        validate_result_revision("previous", first)  # type: ignore[arg-type]


def test_live_submission_rejects_wrong_schema_without_extra_fields() -> None:
    raw = LiveResultSubmission(
        StableIdentifier("evidence:a-r1"),
        StableIdentifier("competitor:a"),
        StableIdentifier("tournament:show"),
        StableIdentifier("round:heat"),
        StableIdentifier("field:a"),
        TargetContext("underhand", 300, "wood", "tax:v1", "convert:v1"),
        "2026-08-22T01:02:03.004Z",
        3,
        15_345,
        1,
        0,
        OfficialResult(ResultStatus.COMPLETION, 12_345, None, 1, None),
        canonical_digest({"submission": True}),
    ).to_dict()
    raw["schema_version"] = "wrong"
    with pytest.raises(Exception, match="closed"):
        LiveResultSubmission.from_dict(raw)
