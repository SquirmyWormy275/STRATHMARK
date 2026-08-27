from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from strathmark.v3.application.approval import (
    ApprovalDecisionAction,
    ApprovalDecisionCommand,
    ApprovalDecisionSelection,
)
from strathmark.v3.application.field_assembly import FieldAssemblyService, FrozenFieldRevision
from strathmark.v3.application.issuance import (
    IssuanceService,
    IssueBatchCommand,
    IssueError,
    IssueFieldSelection,
)
from strathmark.v3.application.lifecycle import SnapshotKind, UpstreamSnapshot
from strathmark.v3.contracts.events import EventKind
from strathmark.v3.contracts.identifiers import IdempotencyKey, StableIdentifier
from strathmark.v3.infrastructure.integrity import CriticalIssueCoordinator, CriticalJournal
from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection
from strathmark.v3.infrastructure.sqlite.event_store import EventStoreConflict
from tests.v3.integration.test_field_receipts import NOW, _bootstrap


def _approved_command(path: Path) -> tuple[object, IssueBatchCommand, object]:
    store, field, build, lifecycle = _bootstrap(path)
    assembled = FieldAssemblyService(store).assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:u16-assemble",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=build,
    )
    page = store.approval_page(tournament_id=str(field.tournament_id), offset=0, limit=10)
    row = page.rows[0]
    store.record_approval_decision(
        ApprovalDecisionCommand.create(
            caller_namespace="manager",
            request_identity="idempotency:u16-approve",
            tournament_id=str(field.tournament_id),
            snapshot_id=page.snapshot_id,
            action=ApprovalDecisionAction.INDIVIDUAL_ACCEPT,
            selected=(
                ApprovalDecisionSelection(
                    row.field_id,
                    row.receipt_id,
                    row.receipt_revision,
                    row.upstream_field_revision,
                    row.row_digest,
                    row.call_order,
                ),
            ),
            excluded=(),
            actor_id="actor:judge",
            actor_metadata={"station": "ring-one"},
            reason_code="judge-reviewed-current-sheet",
            submitted_at="2026-08-25T18:00:01.000Z",
        )
    )
    decided = store.approval_page(tournament_id=str(field.tournament_id), offset=0, limit=10)
    decided_row = decided.rows[0]
    receipt = assembled.receipt
    field_version, _digest = store._events.aggregate_head(str(field.field_id))
    command = IssueBatchCommand.create(
        caller_namespace="manager",
        request_identity="idempotency:u16-issue",
        tournament_id=str(field.tournament_id),
        approval_snapshot_id=decided.snapshot_id,
        selections=(
            IssueFieldSelection(
                decided_row.field_id,
                decided_row.receipt_id,
                decided_row.receipt_revision,
                decided_row.upstream_field_revision,
                field_version,
                decided_row.row_digest,
                decided_row.call_order,
                "round:final",
                str(receipt.tournament_epoch_id),
                tuple(str(item) for item in receipt.ordered_competitor_ids),
                tuple((str(item.competitor_id), item.mark) for item in receipt.marks),
            ),
        ),
        actor_id="actor:judge",
        actor_metadata={"station": "ring-one"},
        reason_code="issue-approved-current-fields",
        submitted_at="2026-08-25T18:00:02.000Z",
        monotonic_elapsed_ms=50,
    )
    return store, command, lifecycle


def test_approved_current_field_issues_once_and_exact_retry_returns_original(
    tmp_path: Path,
) -> None:
    path = tmp_path / "issue.sqlite3"
    store, command, _lifecycle = _approved_command(path)
    journal = CriticalJournal(
        tmp_path / "journal", signer=store._signer, trust_store=store._trust_store
    )
    service = IssuanceService(store, coordinator=CriticalIssueCoordinator.for_rehearsal(journal))

    first = service.acknowledge(command)
    retry = service.acknowledge(command)

    assert retry == first
    assert first.receipt_ids == tuple(item.receipt_id for item in command.selections)
    assert first.issue_batch_id == command.issue_batch_id
    assert journal.intent_path(command.request_identity).is_file()
    assert journal.marker_path(command.request_identity).is_file()
    with open_v3_connection(path, read_only=True) as connection:
        issued = connection.execute(
            "SELECT COUNT(*) FROM v3_events WHERE event_kind=?",
            (EventKind.FIELD_ISSUED.value,),
        ).fetchone()
    assert issued is not None and int(issued[0]) == 1


def test_issue_rejects_changed_approval_material_before_intent_or_event(tmp_path: Path) -> None:
    path = tmp_path / "stale.sqlite3"
    store, command, _lifecycle = _approved_command(path)
    journal = CriticalJournal(
        tmp_path / "stale-journal", signer=store._signer, trust_store=store._trust_store
    )
    service = IssuanceService(store, coordinator=CriticalIssueCoordinator.for_rehearsal(journal))
    changed = IssueBatchCommand.create(
        caller_namespace=command.caller_namespace,
        request_identity=command.request_identity,
        tournament_id=command.tournament_id,
        approval_snapshot_id=command.approval_snapshot_id,
        selections=(replace(command.selections[0], approval_row_digest="f" * 64),),
        actor_id=command.actor_id,
        actor_metadata={"station": "ring-one"},
        reason_code=command.reason_code,
        submitted_at=command.submitted_at,
        monotonic_elapsed_ms=command.monotonic_elapsed_ms,
    )

    with pytest.raises(IssueError, match="approved current row"):
        service.acknowledge(changed)
    assert not journal.intent_path(changed.request_identity).exists()
    with open_v3_connection(path, read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM v3_events WHERE event_kind=?",
                (EventKind.FIELD_ISSUED.value,),
            ).fetchone()[0]
            == 0
        )


def test_database_committed_marker_missing_recovers_original_once(tmp_path: Path) -> None:
    path = tmp_path / "ambiguous.sqlite3"
    store, command, _lifecycle = _approved_command(path)
    journal = CriticalJournal(
        tmp_path / "ambiguous-journal", signer=store._signer, trust_store=store._trust_store
    )
    service = IssuanceService(store, coordinator=CriticalIssueCoordinator.for_rehearsal(journal))

    def crash(stage: str) -> None:
        if stage == "after_database_commit":
            raise RuntimeError("simulated response loss")

    with pytest.raises(RuntimeError, match="response loss"):
        service.acknowledge(command, critical_fault_hook=crash)
    assert journal.intent_path(command.request_identity).is_file()
    assert not journal.marker_path(command.request_identity).exists()

    recovered = service.acknowledge(command)
    assert recovered.receipt_ids == (command.selections[0].receipt_id,)
    assert journal.marker_path(command.request_identity).is_file()
    with open_v3_connection(path, read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM v3_events WHERE event_kind=?",
                (EventKind.FIELD_ISSUED.value,),
            ).fetchone()[0]
            == 1
        )

    changed = IssueBatchCommand.create(
        caller_namespace=command.caller_namespace,
        request_identity=command.request_identity,
        tournament_id=command.tournament_id,
        approval_snapshot_id=command.approval_snapshot_id,
        selections=(replace(command.selections[0], expected_field_version=99),),
        actor_id=command.actor_id,
        actor_metadata={"station": "ring-one"},
        reason_code=command.reason_code,
        submitted_at=command.submitted_at,
        monotonic_elapsed_ms=command.monotonic_elapsed_ms,
    )
    with pytest.raises(EventStoreConflict, match="different material"):
        service.acknowledge(changed)


def test_two_field_issue_rolls_back_every_field_on_mid_append_failure(tmp_path: Path) -> None:
    path = tmp_path / "two-field.sqlite3"
    store, first_field, build, lifecycle = _bootstrap(path)
    second_id = StableIdentifier("field:second")
    lifecycle.ingest_snapshot(
        UpstreamSnapshot(
            SnapshotKind.FIELD,
            second_id,
            1,
            first_field.tournament_id,
            first_field.round_id,
            {
                "competitor_ids": [
                    str(item.competitor_id) for item in first_field.ordered_assignments
                ],
                "target_context": first_field.target_context.to_dict(),
                "stand_ids": [str(item.stand_id) for item in first_field.ordered_assignments],
                "capacity_authority_digest": first_field.capacity_authority_digest,
                "max_field_entrants": first_field.max_field_entrants,
                "call_order": 2,
                "scheduled_at": "2026-08-25T18:01:00.000Z",
                "deadline_at": "2026-08-25T18:03:00.000Z",
            },
        ),
        command_id=IdempotencyKey("command:u16-second-field"),
        actor_id=StableIdentifier("actor:manager"),
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=20,
    )
    second_field = FrozenFieldRevision.create(
        tournament_id=first_field.tournament_id,
        round_id=first_field.round_id,
        field_id=second_id,
        field_revision=1,
        assignments=first_field.ordered_assignments,
        target_context=first_field.target_context,
        historical_cutoff_key=first_field.historical_cutoff_key,
        tournament_epoch_id=first_field.tournament_epoch_id,
        tournament_event_sequence=first_field.tournament_event_sequence,
        bundle_digest=first_field.bundle_digest,
        evidence_digest=first_field.evidence_digest,
        capacity_authority_digest=first_field.capacity_authority_digest,
        max_field_entrants=first_field.max_field_entrants,
        call_order=2,
        scheduled_at="2026-08-25T18:01:00.000Z",
        deadline_at="2026-08-25T18:03:00.000Z",
    )
    receipts = tuple(
        FieldAssemblyService(store)
        .assemble(
            field=field,
            caller_namespace="manager",
            request_identity=f"idempotency:u16-assemble-{field.field_id.value.split(':')[1]}",
            actor_id="actor:manager",
            occurred_at=NOW,
            build_pipeline=build,
        )
        .receipt
        for field in (first_field, second_field)
    )
    page = store.approval_page(tournament_id=str(first_field.tournament_id), offset=0, limit=10)
    store.record_approval_decision(
        ApprovalDecisionCommand.create(
            caller_namespace="manager",
            request_identity="idempotency:u16-approve-two",
            tournament_id=str(first_field.tournament_id),
            snapshot_id=page.snapshot_id,
            action=ApprovalDecisionAction.ORDINARY_BATCH_ACCEPT,
            selected=tuple(
                ApprovalDecisionSelection(
                    row.field_id,
                    row.receipt_id,
                    row.receipt_revision,
                    row.upstream_field_revision,
                    row.row_digest,
                    row.call_order,
                )
                for row in page.rows
            ),
            excluded=(),
            actor_id="actor:judge",
            actor_metadata={"station": "ring-one"},
            reason_code="judge-approved-two-current-fields",
            submitted_at="2026-08-25T18:00:01.000Z",
        )
    )
    decided = store.approval_page(tournament_id=str(first_field.tournament_id), offset=0, limit=10)
    receipt_by_field = {str(item.field_id): item for item in receipts}
    selections = []
    for row in decided.rows:
        receipt = receipt_by_field[row.field_id]
        field_version, _digest = store._events.aggregate_head(row.field_id)
        selections.append(
            IssueFieldSelection(
                row.field_id,
                row.receipt_id,
                row.receipt_revision,
                row.upstream_field_revision,
                field_version,
                row.row_digest,
                row.call_order,
                "round:final",
                str(receipt.tournament_epoch_id),
                tuple(str(item) for item in receipt.ordered_competitor_ids),
                tuple((str(item.competitor_id), item.mark) for item in receipt.marks),
            )
        )
    command = IssueBatchCommand.create(
        caller_namespace="manager",
        request_identity="idempotency:u16-issue-two",
        tournament_id=str(first_field.tournament_id),
        approval_snapshot_id=decided.snapshot_id,
        selections=tuple(sorted(selections, key=lambda item: item.field_id)),
        actor_id="actor:judge",
        actor_metadata={"station": "ring-one"},
        reason_code="issue-two-approved-fields",
        submitted_at="2026-08-25T18:00:02.000Z",
        monotonic_elapsed_ms=50,
    )
    journal = CriticalJournal(
        tmp_path / "two-field-journal",
        signer=store._signer,
        trust_store=store._trust_store,
    )
    service = IssuanceService(store, coordinator=CriticalIssueCoordinator.for_rehearsal(journal))

    def fail_mid_append(stage: str) -> None:
        if stage == "after_event:0":
            raise RuntimeError("mid-batch append failure")

    with pytest.raises(RuntimeError, match="mid-batch"):
        service.acknowledge(command, event_fault_hook=fail_mid_append)
    with open_v3_connection(path, read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM v3_events WHERE event_kind=?",
                (EventKind.FIELD_ISSUED.value,),
            ).fetchone()[0]
            == 0
        )

    acknowledgment = service.acknowledge(command)
    assert len(acknowledgment.receipt_ids) == 2
    with open_v3_connection(path, read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM v3_events WHERE event_kind=?",
                (EventKind.FIELD_ISSUED.value,),
            ).fetchone()[0]
            == 2
        )
