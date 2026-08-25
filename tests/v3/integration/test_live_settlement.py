from __future__ import annotations

from pathlib import Path

import pytest

from strathmark.v3.application.issuance import IssuanceService
from strathmark.v3.application.settlement import SettlementCommand, SettlementService
from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.identifiers import IdempotencyKey, StableIdentifier
from strathmark.v3.contracts.statuses import OfficialResult, ResultStatus
from strathmark.v3.domain.evidence import LiveResultSubmission
from strathmark.v3.infrastructure.integrity import CriticalIssueCoordinator, CriticalJournal
from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection
from tests.v3.integration.test_issue_acknowledgment import _approved_command


def test_only_complete_active_outcome_set_settles_exact_issued_race(tmp_path: Path) -> None:
    path = tmp_path / "settlement.sqlite3"
    store, issue_command, lifecycle = _approved_command(path)
    coordinator = CriticalIssueCoordinator.for_rehearsal(
        CriticalJournal(
            tmp_path / "settlement-journal",
            signer=store._signer,
            trust_store=store._trust_store,
        )
    )
    IssuanceService(store, coordinator=coordinator).acknowledge(issue_command)
    selection = issue_command.selections[0]
    marks = dict(selection.issued_marks)
    receipt = store.receipt(selection.receipt_id)
    for index, competitor_id in enumerate(selection.competitor_ids, start=1):
        lifecycle.record_live_result(
            LiveResultSubmission(
                StableIdentifier(f"evidence:settlement-{index}"),
                StableIdentifier(competitor_id),
                StableIdentifier(issue_command.tournament_id),
                StableIdentifier(selection.round_id),
                StableIdentifier(selection.field_id),
                receipt.target_context,
                "2026-08-25T18:00:03.000Z",
                marks[competitor_id],
                40_000 + index * 1_000,
                index,
                (index - 1) * 1_000,
                OfficialResult(ResultStatus.COMPLETION, 40_000 + index * 1_000, None, 1, None),
                str(index) * 64,
            ),
            field_revision=selection.upstream_field_revision,
            claimed_receipt_id=StableIdentifier(selection.receipt_id),
            command_id=IdempotencyKey(f"command:settlement-result-{index}"),
            actor_id=StableIdentifier("actor:manager"),
            occurred_at_utc="2026-08-25T18:00:03.000Z",
            monotonic_elapsed_ms=60 + index,
        )
    command = SettlementCommand.create(
        field_id=selection.field_id,
        field_revision=selection.upstream_field_revision,
        receipt_id=selection.receipt_id,
        command_id="command:settle-issued-race",
        actor_id="actor:manager",
        occurred_at="2026-08-25T18:00:04.000Z",
        monotonic_elapsed_ms=70,
    )
    service = SettlementService(lifecycle)

    first = service.settle(command)
    retry = service.settle(command)

    assert retry == first
    assert first.field_id == selection.field_id
    assert first.receipt_id == selection.receipt_id
    assert len(first.result_revisions) == len(selection.competitor_ids)
    with open_v3_connection(path, read_only=True) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_result_revisions WHERE settled_global_sequence IS NOT NULL"
        ).fetchone()[0] == len(selection.competitor_ids)


def test_settlement_rejects_wrong_receipt_and_incomplete_outcome_set(tmp_path: Path) -> None:
    path = tmp_path / "incomplete.sqlite3"
    store, issue_command, lifecycle = _approved_command(path)
    IssuanceService(
        store,
        coordinator=CriticalIssueCoordinator.for_rehearsal(
            CriticalJournal(
                tmp_path / "incomplete-journal",
                signer=store._signer,
                trust_store=store._trust_store,
            )
        ),
    ).acknowledge(issue_command)
    selection = issue_command.selections[0]
    service = SettlementService(lifecycle)

    with pytest.raises(ContractError, match="exact acknowledged receipt"):
        service.settle(
            SettlementCommand.create(
                field_id=selection.field_id,
                field_revision=selection.upstream_field_revision,
                receipt_id="receipt:wrong",
                command_id="command:settle-wrong-receipt",
                actor_id="actor:manager",
                occurred_at="2026-08-25T18:00:04.000Z",
                monotonic_elapsed_ms=70,
            )
        )
    with pytest.raises(ContractError, match="one active outcome revision per issued entrant"):
        service.settle(
            SettlementCommand.create(
                field_id=selection.field_id,
                field_revision=selection.upstream_field_revision,
                receipt_id=selection.receipt_id,
                command_id="command:settle-missing-results",
                actor_id="actor:manager",
                occurred_at="2026-08-25T18:00:04.000Z",
                monotonic_elapsed_ms=70,
            )
        )
