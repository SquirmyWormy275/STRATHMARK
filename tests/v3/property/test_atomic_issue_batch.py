from __future__ import annotations

from dataclasses import replace

import pytest

from strathmark.v3.application.issuance import (
    IssueBatchCommand,
    IssueError,
    IssueFieldSelection,
)


def _selection(field: str, *, call_order: int, version: int = 1) -> IssueFieldSelection:
    return IssueFieldSelection(
        field_id=f"field:{field}",
        receipt_id=f"receipt:{field}",
        receipt_revision=1,
        upstream_field_revision=7,
        expected_field_version=version,
        approval_row_digest=(field[0] * 64),
        call_order=call_order,
        round_id="round:final",
        epoch_id="epoch:final-1",
        competitor_ids=("competitor:a", "competitor:b"),
        issued_marks=(("competitor:a", 3), ("competitor:b", 13)),
    )


def _command(*selections: IssueFieldSelection) -> IssueBatchCommand:
    return IssueBatchCommand.create(
        caller_namespace="manager",
        request_identity="idempotency:issue-round-1",
        tournament_id="tournament:show",
        approval_snapshot_id="approval_snapshot:" + "a" * 64,
        selections=selections,
        actor_id="actor:judge",
        actor_metadata={"station": "ring-one"},
        reason_code="issue-approved-current-fields",
        submitted_at="2026-08-25T18:00:00.000Z",
        monotonic_elapsed_ms=42,
    )


def test_issue_batch_is_canonical_and_every_change_gets_a_new_identity() -> None:
    first = _command(_selection("a", call_order=1), _selection("b", call_order=2))
    replay = IssueBatchCommand.from_dict(first.to_dict())

    assert replay == first
    assert first.issue_batch_id.startswith("issue_batch:")
    assert len(first.command_digest) == 64
    assert first.selections[0].field_id == "field:a"
    assert replace(first.selections[0], expected_field_version=2) not in first.selections
    changed = _command(replace(first.selections[0], expected_field_version=2), first.selections[1])
    assert changed.issue_batch_id != first.issue_batch_id
    assert changed.command_digest != first.command_digest


@pytest.mark.parametrize(
    ("selections", "message"),
    [
        ((), "selection"),
        (
            (_selection("b", call_order=2), _selection("a", call_order=1)),
            "canonically sorted",
        ),
        (
            (_selection("a", call_order=1), _selection("a", call_order=2)),
            "duplicate field",
        ),
        (
            (
                _selection("a", call_order=1),
                replace(_selection("b", call_order=2), receipt_id="receipt:a"),
            ),
            "duplicate receipt",
        ),
    ],
)
def test_issue_batch_rejects_empty_unsorted_or_duplicate_authority(
    selections: tuple[IssueFieldSelection, ...], message: str
) -> None:
    with pytest.raises(IssueError, match=message):
        _command(*selections)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"receipt_revision": 0}, "positive"),
        ({"upstream_field_revision": True}, "positive"),
        ({"expected_field_version": -1}, "nonnegative"),
        ({"approval_row_digest": "A" * 64}, "digest"),
        ({"call_order": -1}, "call order"),
        (
            {"competitor_ids": ("competitor:a", "competitor:a")},
            "roster",
        ),
        ({"issued_marks": (("competitor:a", 2), ("competitor:b", 13))}, "mark"),
    ],
)
def test_issue_selection_closes_revision_version_and_digest_boundaries(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(IssueError, match=message):
        replace(_selection("a", call_order=1), **changes)
