from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from strathmark.v3.application.approval import (
    _VERIFIED_RECEIPT_AUTHORITY,
    ApprovalConflict,
    ApprovalConflictChange,
    ApprovalDecisionAction,
    ApprovalDecisionCommand,
    ApprovalDecisionReceipt,
    ApprovalDecisionSelection,
    ApprovalError,
    ApprovalFacts,
    ApprovalLane,
    ApprovalManualMode,
    ApprovalProjection,
    ApprovalRow,
    AvailabilityMode,
    DecisionState,
    FreshnessState,
    IntegrityState,
    QueueEmptyReason,
    derive_approval_lane,
    derive_receipt_approval_facts,
)
from strathmark.v3.application.commands import CommandRequest, EventIntent
from strathmark.v3.application.field_assembly import (
    FieldAssemblyService,
    render_verified_receipt_explanation,
)
from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.commands import CommandEnvelope, CommandKind, InlinePayload
from strathmark.v3.contracts.events import AggregateKind, EventKind
from strathmark.v3.contracts.evidence import TargetContext
from strathmark.v3.contracts.identifiers import IdempotencyKey, StableIdentifier
from strathmark.v3.contracts.receipts import (
    BundleIdentity,
    FieldReceipt,
    MarkAssignment,
    PacketIdentity,
    ReceiptSection,
    ReceiptSectionKind,
)
from strathmark.v3.domain.disagreement import ConsequenceColor, OverrideScope
from strathmark.v3.infrastructure.sqlite.connection import (
    immediate_transaction,
    open_v3_connection,
)
from strathmark.v3.infrastructure.sqlite.event_store import (
    EventStoreConflict,
    SQLiteEventStore,
)
from strathmark.v3.infrastructure.sqlite.projections import (
    ProjectionError,
    SQLiteFieldProjectionStore,
    _prioritized_approval_conflict_fields,
)


def _append_lifecycle_event(
    lifecycle: object,
    *,
    command_kind: CommandKind,
    event_kind: EventKind,
    aggregate_kind: AggregateKind,
    target: StableIdentifier,
    payload: dict[str, object],
    command_id: str,
    occurred_at: str,
) -> None:
    database_path = lifecycle.projections.database_path
    event_store = SQLiteEventStore(database_path)
    head = event_store.aggregate_head(str(target))
    command = CommandEnvelope(
        command_kind,
        IdempotencyKey(command_id),
        target,
        ((str(target), 0 if head is None else head[0]),),
        StableIdentifier("actor:manager"),
        InlinePayload.from_value(payload),
    )
    event_store.execute(
        CommandRequest(
            StableIdentifier("actor:manager"),
            command,
            (EventIntent(aggregate_kind, target, event_kind),),
            "strathmark-v3-approval-test-result-v1",
            {"accepted": True},
            occurred_at,
            50,
        ),
        projection_hook=lifecycle.projections.apply_events,
    )


def _facts(**changes: object) -> ApprovalFacts:
    values: dict[str, object] = {
        "integrity": IntegrityState.VERIFIED,
        "freshness": FreshnessState.CURRENT,
        "availability": AvailabilityMode.NORMAL_THREE,
        "consequence": ConsequenceColor.GREEN,
        "zero_history": False,
        "council_degraded": False,
        "manual_construction": False,
        "reason_codes": (),
        "availability_counts": (),
        "manual_mode": ApprovalManualMode.NONE,
        "flagged_competitor_ids": (),
        "flag_reason_tokens": (),
    }
    values.update(changes)
    if (
        "availability" in changes
        and "manual_construction" not in changes
        and changes["availability"]
        in {AvailabilityMode.MANUAL_SINGLE, AvailabilityMode.MANUAL_ZERO}
    ):
        values["manual_construction"] = True
        values["manual_mode"] = (
            ApprovalManualMode.EXACT_SINGLE_SURVIVOR
            if changes["availability"] is AvailabilityMode.MANUAL_SINGLE
            else ApprovalManualMode.COMPLETE_EXPECTED_TIME
        )
    return ApprovalFacts(**values)  # type: ignore[arg-type]


def _row(field: str, revision: int, facts: ApprovalFacts) -> ApprovalRow:
    prior = None
    history = []
    for historical_revision in range(1, revision):
        prior = _receipt(field, historical_revision, 7, prior)
        history.append(prior)
    current = _receipt(field, revision, 8, prior)
    store = _ReceiptStore(current, *history, facts=facts)
    return ApprovalRow.from_verified_receipt(
        store=store,
        receipt_id=str(current.receipt_id),
        call_order=revision,
        deadline_at="2026-08-24T18:02:00.000Z",
    )


class _ReceiptStore:
    def __init__(self, *receipts: FieldReceipt, facts: ApprovalFacts) -> None:
        self._receipts = {str(item.receipt_id): item for item in receipts}
        self._facts = facts
        self._approval_authority_token = _VERIFIED_RECEIPT_AUTHORITY

    def verified_receipt(self, receipt_id: str) -> FieldReceipt:
        return self._receipts[receipt_id]

    def approval_facts(self, receipt_id: str) -> ApprovalFacts:
        self.verified_receipt(receipt_id)
        return self._facts


def _receipt(
    field: str,
    revision: int,
    second_mark: int,
    prior: FieldReceipt | None,
) -> FieldReceipt:
    return _receipt_for(
        field,
        revision,
        (("competitor:a", 3), ("competitor:b", second_mark)),
        prior,
    )


def _receipt_for(
    field: str,
    revision: int,
    roster_marks: tuple[tuple[str, int], ...],
    prior: FieldReceipt | None,
    *,
    upstream_revision: int | None = None,
) -> FieldReceipt:
    competitors = tuple(StableIdentifier(item[0]) for item in roster_marks)
    context = TargetContext(
        event_code="underhand",
        size_mm=300,
        material_code="pine",
        taxonomy_version="taxonomy:v1",
        conversion_version="conversion:v1",
    )
    sections = tuple(
        ReceiptSection(
            kind,
            InlinePayload.from_value(
                {
                    "schema_version": f"strathmark-v3-test-{kind.value}-v1",
                    "value": kind.value,
                }
            ),
        )
        for kind in ReceiptSectionKind
    )
    return FieldReceipt.create(
        caller_namespace="manager",
        request_identity=IdempotencyKey(f"idempotency:{field}-{revision}"),
        field_id=StableIdentifier(f"field:{field}"),
        upstream_field_revision=(revision if upstream_revision is None else upstream_revision),
        receipt_revision=revision,
        supersedes_receipt_id=None if prior is None else prior.receipt_id,
        ordered_competitor_ids=competitors,
        target_context=context,
        target_context_digest=context.digest,
        historical_cutoff_key="history:cutoff",
        tournament_epoch_id=StableIdentifier("epoch:final"),
        tournament_event_sequence=1,
        packet_identities=tuple(
            PacketIdentity(item, canonical_digest({"packet": str(item)})) for item in competitors
        ),
        sections=sections,
        marks=tuple(
            MarkAssignment(competitor, mark)
            for competitor, (_identity, mark) in zip(competitors, roster_marks, strict=True)
        ),
        warning_codes=(),
        total_latency_ms=1,
        bundles=(BundleIdentity("runtime", "bundle:v1", "a" * 64),),
    )


@pytest.mark.parametrize(
    ("facts", "lane", "ordinary", "degraded"),
    [
        (_facts(), ApprovalLane.NORMAL_GREEN, True, False),
        (
            _facts(consequence=ConsequenceColor.AMBER),
            ApprovalLane.NORMAL_AMBER,
            True,
            False,
        ),
        (
            _facts(availability=AvailabilityMode.DEGRADED_TWO),
            ApprovalLane.DEGRADED_TWO,
            False,
            True,
        ),
        (
            _facts(council_degraded=True),
            ApprovalLane.DEGRADED_COUNCIL,
            False,
            True,
        ),
        (
            _facts(availability=AvailabilityMode.MANUAL_SINGLE),
            ApprovalLane.MANUAL_SINGLE,
            False,
            False,
        ),
        (
            _facts(availability=AvailabilityMode.MANUAL_ZERO),
            ApprovalLane.MANUAL_ZERO,
            False,
            False,
        ),
        (_facts(zero_history=True), ApprovalLane.ZERO_HISTORY, False, False),
        (
            _facts(consequence=ConsequenceColor.RED),
            ApprovalLane.RED,
            False,
            False,
        ),
        (
            _facts(freshness=FreshnessState.STALE),
            ApprovalLane.STALE,
            False,
            False,
        ),
        (
            _facts(
                integrity=IntegrityState.BLOCKED,
                freshness=FreshnessState.STALE,
                consequence=ConsequenceColor.GREEN,
            ),
            ApprovalLane.INTEGRITY_BLOCKED,
            False,
            False,
        ),
    ],
)
def test_approval_precedence_has_no_default_and_never_leaks_degraded_work(
    facts: ApprovalFacts,
    lane: ApprovalLane,
    ordinary: bool,
    degraded: bool,
) -> None:
    row = _row("a", 1, facts)
    assert row.lane is lane
    assert row.ordinary_batch_eligible is ordinary
    assert row.degraded_batch_eligible is degraded
    assert row.decision_state is (
        DecisionState.BLOCKED if lane is ApprovalLane.INTEGRITY_BLOCKED else DecisionState.UNDECIDED
    )


def test_snapshot_pagination_mass_selection_and_targeted_conflict_refresh() -> None:
    row_a = _row("a", 1, _facts())
    row_b = _row("b", 2, _facts(consequence=ConsequenceColor.AMBER))
    row_c = _row("c", 3, _facts(availability=AvailabilityMode.DEGRADED_TWO))
    projection = ApprovalProjection.create(
        tournament_id="tournament:show",
        rows=(row_a, row_b, row_c),
        global_sequence=19,
        lifecycle_state="open",
        preparation_completed=3,
        preparation_total=3,
        projection_current=True,
    )
    first = projection.page(offset=0, limit=2)
    second = projection.page(offset=2, limit=2)
    assert first.snapshot_id == second.snapshot_id == projection.snapshot_id
    assert tuple(row.field_id for row in first.rows) == ("field:a", "field:b")
    assert tuple(row.field_id for row in second.rows) == ("field:c",)
    assert projection.empty_reason is None

    accepted = projection.acknowledge_batch(
        snapshot_id=projection.snapshot_id,
        included=((row_a.receipt_id, 1), (row_b.receipt_id, 2)),
        excluded=((row_c.receipt_id, 3),),
        actor_metadata={"actor_id": "judge:one"},
        decided_at="2026-08-24T18:00:30.000Z",
        degraded=False,
    )
    assert accepted.included_receipts == (row_a.receipt_id, row_b.receipt_id)
    assert accepted.excluded_receipts == (row_c.receipt_id,)

    changed_a = _row("a", 2, _facts())
    changed = ApprovalProjection.create(
        tournament_id="tournament:show",
        rows=(changed_a, projection.rows[1], projection.rows[2]),
        global_sequence=20,
        lifecycle_state="open",
        preparation_completed=3,
        preparation_total=3,
        projection_current=True,
    )
    with pytest.raises(ApprovalConflict) as caught:
        changed.acknowledge_batch(
            snapshot_id=projection.snapshot_id,
            included=((row_a.receipt_id, 1), (row_b.receipt_id, 2)),
            excluded=((row_c.receipt_id, 3),),
            actor_metadata={"actor_id": "judge:one"},
            decided_at="2026-08-24T18:00:31.000Z",
            degraded=False,
        )
    assert caught.value.replacements == (("field:a", changed_a.receipt_id, 2),)


@pytest.mark.parametrize(
    ("rows", "completed", "total", "lifecycle", "reason"),
    [
        ((), 0, 0, "open", QueueEmptyReason.NO_SCHEDULED_FIELDS),
        ((), 1, 2, "open", QueueEmptyReason.STILL_PREPARING),
        (
            (_row("a", 1, _facts(availability=AvailabilityMode.MANUAL_ZERO)),),
            1,
            1,
            "open",
            QueueEmptyReason.NO_BATCH_ELIGIBLE_FIELDS,
        ),
        (
            (_row("a", 1, _facts(integrity=IntegrityState.BLOCKED)),),
            1,
            1,
            "open",
            QueueEmptyReason.ALL_BLOCKED,
        ),
        ((), 1, 1, "all_issued", QueueEmptyReason.ALL_ISSUED),
    ],
)
def test_queue_empty_reasons_are_explicit(
    rows: tuple[ApprovalRow, ...],
    completed: int,
    total: int,
    lifecycle: str,
    reason: QueueEmptyReason,
) -> None:
    projection = ApprovalProjection.create(
        tournament_id="tournament:show",
        rows=rows,
        global_sequence=1,
        lifecycle_state=lifecycle,
        preparation_completed=completed,
        preparation_total=total,
        projection_current=True,
    )
    assert projection.empty_reason is reason


@pytest.mark.parametrize(
    "changes",
    [
        {"manual_construction": True},
        {
            "availability": AvailabilityMode.MANUAL_SINGLE,
            "manual_construction": False,
        },
        {"availability": AvailabilityMode.MANUAL_ZERO, "manual_construction": False},
    ],
)
def test_manual_construction_facts_cannot_contradict_availability(
    changes: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "integrity": IntegrityState.VERIFIED,
        "freshness": FreshnessState.CURRENT,
        "availability": AvailabilityMode.NORMAL_THREE,
        "consequence": ConsequenceColor.GREEN,
        "zero_history": False,
        "council_degraded": False,
        "manual_construction": False,
        "reason_codes": (),
        "availability_counts": (),
        "manual_mode": ApprovalManualMode.NONE,
    }
    values.update(changes)
    with pytest.raises(ApprovalError, match="manual construction"):
        ApprovalFacts(**values)  # type: ignore[arg-type]


def test_direct_forgery_and_stale_projection_cannot_acknowledge() -> None:
    row = _row("a", 1, _facts())
    with pytest.raises(ApprovalError):
        replace(row, proposed_marks=(("competitor:a", 3), ("competitor:b", 99)))
    projection = ApprovalProjection.create(
        tournament_id="tournament:show",
        rows=(row,),
        global_sequence=1,
        lifecycle_state="open",
        preparation_completed=1,
        preparation_total=1,
        projection_current=False,
    )
    with pytest.raises(ApprovalConflict, match="not current"):
        projection.acknowledge_batch(
            snapshot_id=projection.snapshot_id,
            included=((row.receipt_id, row.receipt_revision),),
            excluded=(),
            actor_metadata={"actor_id": "judge:one"},
            decided_at="2026-08-24T18:00:30.000Z",
            degraded=False,
        )


@pytest.mark.parametrize("overlap", [False, True])
def test_batch_rejects_duplicate_or_overlapping_receipts(
    overlap: bool,
) -> None:
    row = _row("a", 1, _facts())
    projection = ApprovalProjection.create(
        tournament_id="tournament:show",
        rows=(row,),
        global_sequence=1,
        lifecycle_state="open",
        preparation_completed=1,
        preparation_total=1,
        projection_current=True,
    )
    pair = (row.receipt_id, 1)
    included = (pair,) if overlap else (pair, pair)
    excluded = (pair,) if overlap else ()
    with pytest.raises(ApprovalConflict, match="duplicate|overlap"):
        projection.acknowledge_batch(
            snapshot_id=projection.snapshot_id,
            included=included,
            excluded=excluded,
            actor_metadata={"actor_id": "judge:one"},
            decided_at="2026-08-24T18:00:30.000Z",
            degraded=False,
        )


def test_receipt_backed_row_derives_whole_field_scratch_substitution_diff() -> None:
    prior = _receipt_for(
        "scratch",
        1,
        (("competitor:a", 3), ("competitor:b", 7)),
        None,
    )
    current = _receipt_for(
        "scratch",
        2,
        (("competitor:a", 3), ("competitor:c", 8)),
        prior,
    )
    row = ApprovalRow.from_verified_receipt(
        store=_ReceiptStore(prior, current, facts=_facts()),
        receipt_id=str(current.receipt_id),
        call_order=1,
        deadline_at="2026-08-24T18:02:00.000Z",
    )
    assert row.proposed_marks == (("competitor:a", 3), ("competitor:c", 8))
    assert row.changed_marks == (
        ("competitor:c", None, 8),
        ("competitor:b", 7, None),
    )
    assert row.affected_competitors == ()


def test_approval_row_binds_receipt_and_upstream_revisions_independently() -> None:
    prior = _receipt_for(
        "judge-supersession",
        1,
        (("competitor:a", 3), ("competitor:b", 7)),
        None,
        upstream_revision=4,
    )
    current = _receipt_for(
        "judge-supersession",
        2,
        (("competitor:a", 3), ("competitor:b", 8)),
        prior,
        upstream_revision=4,
    )

    row = ApprovalRow.from_verified_receipt(
        store=_ReceiptStore(prior, current, facts=_facts()),
        receipt_id=str(current.receipt_id),
        call_order=1,
        deadline_at="2026-08-24T18:02:00.000Z",
    )
    decoded = ApprovalRow.from_dict(row.to_dict(), _authority=_VERIFIED_RECEIPT_AUTHORITY)

    assert row.receipt_revision == 2
    assert row.upstream_field_revision == 4
    assert decoded == row
    assert ApprovalDecisionSelection(
        row.field_id,
        row.receipt_id,
        row.receipt_revision,
        row.upstream_field_revision,
        row.row_digest,
        row.call_order,
    ).to_dict() == {
        "field_id": row.field_id,
        "receipt_id": row.receipt_id,
        "receipt_revision": 2,
        "upstream_field_revision": 4,
        "row_digest": row.row_digest,
        "call_order": 1,
    }


def test_causal_flags_are_independent_from_initial_or_unchanged_marks() -> None:
    initial = _row("causal", 1, _facts(consequence=ConsequenceColor.RED))
    prior = _receipt("causal-stable", 1, 8, None)
    current = _receipt("causal-stable", 2, 8, prior)
    unchanged = ApprovalRow.from_verified_receipt(
        store=_ReceiptStore(
            prior,
            current,
            facts=_facts(consequence=ConsequenceColor.RED),
        ),
        receipt_id=str(current.receipt_id),
        call_order=2,
        deadline_at="2026-08-24T18:02:00.000Z",
    )

    assert initial.changed_marks
    assert initial.affected_competitors == ("competitor:a", "competitor:b")
    assert unchanged.changed_marks == ()
    assert unchanged.affected_competitors == ("competitor:a", "competitor:b")


def test_decision_command_binds_namespace_snapshot_actor_reason_and_exact_revisions() -> None:
    row = _row("decision", 1, _facts())
    command = ApprovalDecisionCommand.create(
        caller_namespace="manager-primary",
        request_identity="idempotency:approval-decision-1",
        tournament_id="tournament:show",
        snapshot_id="approval_snapshot:" + "a" * 64,
        action=ApprovalDecisionAction.ORDINARY_BATCH_ACCEPT,
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
        reason_code="judge-approved-current-sheet",
        submitted_at="2026-08-24T18:00:30.000Z",
    )

    assert command.command_digest == canonical_digest(command.content_value())
    assert command.selected[0].receipt_id == row.receipt_id
    assert command.actor_metadata_digest == canonical_digest({"station": "ring-one"})


@pytest.mark.parametrize(
    ("selected", "excluded", "message"),
    [
        ((), (), "selection"),
        (
            (
                ApprovalDecisionSelection("field:a", "receipt:a", 1, 1, "a" * 64, 1),
                ApprovalDecisionSelection("field:a", "receipt:a", 1, 1, "a" * 64, 1),
            ),
            (),
            "duplicate",
        ),
        (
            (ApprovalDecisionSelection("field:a", "receipt:a", 1, 1, "a" * 64, 1),),
            (ApprovalDecisionSelection("field:a", "receipt:a", 1, 1, "a" * 64, 1),),
            "overlap",
        ),
    ],
)
def test_decision_command_rejects_empty_duplicate_or_overlapping_material(
    selected: tuple[ApprovalDecisionSelection, ...],
    excluded: tuple[ApprovalDecisionSelection, ...],
    message: str,
) -> None:
    with pytest.raises(ApprovalError, match=message):
        ApprovalDecisionCommand.create(
            caller_namespace="manager-primary",
            request_identity="idempotency:approval-invalid",
            tournament_id="tournament:show",
            snapshot_id="approval_snapshot:" + "a" * 64,
            action=ApprovalDecisionAction.INDIVIDUAL_ACCEPT,
            selected=selected,
            excluded=excluded,
            actor_id="actor:judge",
            actor_metadata={},
            reason_code="reviewed",
            submitted_at="2026-08-24T18:00:30.000Z",
        )


def test_batch_decision_order_is_field_canonical_not_receipt_hash_order() -> None:
    command = ApprovalDecisionCommand.create(
        caller_namespace="manager",
        request_identity="idempotency:approval-order",
        tournament_id="tournament:show",
        snapshot_id="approval_snapshot:" + "a" * 64,
        action=ApprovalDecisionAction.ORDINARY_BATCH_ACCEPT,
        selected=(
            ApprovalDecisionSelection("field:z", "receipt:a", 1, 1, "a" * 64, 0),
            ApprovalDecisionSelection("field:a", "receipt:z", 1, 1, "b" * 64, 1),
        ),
        excluded=(),
        actor_id="actor:judge",
        actor_metadata={},
        reason_code="approve-ordinary-ready-fields",
        submitted_at="2026-08-24T18:00:30.000Z",
    )

    decision = ApprovalDecisionReceipt.create(command)

    assert tuple(item.field_id for item in command.selected) == ("field:z", "field:a")
    assert tuple(item[0] for item in decision.decisions) == (
        "receipt:a",
        "receipt:z",
    )


def test_batch_records_named_exclusions_without_requiring_every_queue_row() -> None:
    command = ApprovalDecisionCommand.create(
        caller_namespace="manager",
        request_identity="idempotency:approval-batch-exception",
        tournament_id="tournament:show",
        snapshot_id="approval_snapshot:" + "a" * 64,
        action=ApprovalDecisionAction.ORDINARY_BATCH_ACCEPT,
        selected=(ApprovalDecisionSelection("field:a", "receipt:a", 1, 1, "a" * 64, 1),),
        excluded=(ApprovalDecisionSelection("field:b", "receipt:b", 1, 1, "b" * 64, 2),),
        actor_id="actor:judge",
        actor_metadata={},
        reason_code="approve-and-deliberately-exclude-ready-field",
        submitted_at="2026-08-24T18:00:30.000Z",
    )

    assert ApprovalDecisionReceipt.create(command).decisions == (
        ("receipt:a", DecisionState.ACCEPTED),
        ("receipt:b", DecisionState.EXCLUDED),
    )


def _typed_approval_receipt(
    *,
    available_count: int | None = None,
    available_counts: tuple[int, ...] | None = None,
    color: str,
    council_valid_count: int = 3,
) -> FieldReceipt:
    if available_counts is None:
        if available_count is None:
            raise AssertionError("typed fixture requires availability")
        available_counts = (available_count, available_count)
    elif available_count is not None:
        raise AssertionError("typed fixture availability is ambiguous")
    roster_marks = tuple(
        (f"competitor:{chr(ord('a') + index)}", 3 + index * 4)
        for index in range(len(available_counts))
    )
    base = _receipt_for("typed", 1, roster_marks, None)
    available_count = min(available_counts)
    field_revision_digest = "f" * 64
    optimizer_verification_digest = "a" * 64
    policy_digest = "b" * 64
    decision_digest = "c" * 64
    sources = ("formula", "ml", "llm_council")
    sections = []
    for section in base.sections:
        value: dict[str, object]
        if section.kind is ReceiptSectionKind.COMPONENT_OUTPUTS:
            value = {
                "schema_version": "strathmark-v3-receipt-components-v1",
                "competitors": [
                    {
                        "competitor_id": str(competitor),
                        "components": [
                            {
                                "assessor": source,
                                "state": ("committed" if index < count else "abstained"),
                            }
                            for index, source in enumerate(sources)
                        ],
                    }
                    for competitor, count in zip(
                        base.ordered_competitor_ids, available_counts, strict=True
                    )
                ],
            }
        elif section.kind is ReceiptSectionKind.POOLED_DISTRIBUTION:
            value = {
                "schema_version": "strathmark-v3-receipt-prediction-distributions-v1",
                "bases": [
                    [str(competitor), {"basis_kind": "typed_test_basis"}]
                    for competitor in base.ordered_competitor_ids
                ],
                "joint_regeneration": {},
            }
        elif section.kind is ReceiptSectionKind.MEMBER_OUTPUTS:
            value = {
                "schema_version": "strathmark-v3-receipt-members-v1",
                "council_audit": {
                    "members": [
                        {"status": "valid" if index < council_valid_count else "failed"}
                        for index in range(3)
                    ]
                },
            }
        elif section.kind is ReceiptSectionKind.VALIDATIONS:
            manual_authority = None
            if available_count < 2:
                manual_mode = (
                    "exact_single_survivor"
                    if all(count == 1 for count in available_counts)
                    else "complete_expected_time"
                )
                manual_content = {
                    "schema_version": "strathmark-v3-manual-field-authority-v1",
                    "mode": manual_mode,
                    "field_revision_digest": field_revision_digest,
                    "estimates": [
                        {
                            "competitor_id": str(competitor),
                            "distribution": {
                                "schema_version": "strathmark-v3-positive-time-distribution-v1",
                                "quantiles": [],
                            },
                            "source_assessor": (
                                "formula" if manual_mode == "exact_single_survivor" else None
                            ),
                        }
                        for competitor in base.ordered_competitor_ids
                    ],
                    "actor_id": "actor:judge",
                    "reason_code": (
                        "judge_single_survivor_acceptance"
                        if manual_mode == "exact_single_survivor"
                        else "judge_complete_expected_time_construction"
                    ),
                    "scope": "upcoming_race",
                    "created_at": "2026-08-24T18:00:00.000Z",
                }
                manual_authority = {
                    **manual_content,
                    "authority_digest": canonical_digest(manual_content),
                }
            value = {
                "schema_version": "strathmark-v3-receipt-validations-v1",
                "field_revision_digest": field_revision_digest,
                "optimizer_verification_digest": optimizer_verification_digest,
                "manual_authority": manual_authority,
                "flagged_competitor_ids": [
                    str(competitor)
                    for competitor, count in zip(
                        base.ordered_competitor_ids,
                        available_counts,
                        strict=True,
                    )
                    if count < 3 or color != "green" or council_valid_count == 2
                ],
                "flag_reason_tokens": [
                    [
                        str(competitor),
                        sorted(
                            {
                                *([f"assessor_availability_{count}_of_3"] if count < 3 else []),
                                *([f"consequence_{color}"] if color != "green" else []),
                                *(
                                    ["council_degraded_two_of_three"]
                                    if council_valid_count == 2
                                    else []
                                ),
                            }
                        ),
                    ]
                    for competitor, count in zip(
                        base.ordered_competitor_ids,
                        available_counts,
                        strict=True,
                    )
                    if count < 3 or color != "green" or council_valid_count == 2
                ],
            }
        elif section.kind is ReceiptSectionKind.DISAGREEMENT:
            if available_count < 2:
                value = {
                    "schema_version": "strathmark-v3-receipt-disagreement-v1",
                    "decision": None,
                    "operational_receipt": None,
                }
            else:
                decision = {
                    "schema_version": "strathmark-v3-disagreement-decision-v1",
                    "operational_status": "pending_u14_verifier",
                    "manual_review_required": True,
                    "color": color,
                    "policy_digest": policy_digest,
                    "component_sheets": [
                        {"source": source, "joint_draw_digest": str(index + 4) * 64}
                        for index, source in enumerate(sources)
                    ],
                    "council_audit": {"verified": True},
                    "decision_digest": decision_digest,
                }
                operational_content = {
                    "schema_version": "strathmark-v3-operational-disagreement-receipt-v1",
                    "field_revision_digest": field_revision_digest,
                    "decision_digest": decision_digest,
                    "color": color,
                    "policy_digest": policy_digest,
                    "pooled_optimizer_verification_digest": optimizer_verification_digest,
                    "component_optimizer_verification_digests": [
                        [source, str(index + 1) * 64] for index, source in enumerate(sources)
                    ],
                    "component_joint_draw_digests": [
                        [source, str(index + 4) * 64] for index, source in enumerate(sources)
                    ],
                    "policy_manifest_digest": "7" * 64,
                    "council_manifest_digest": "8" * 64,
                }
                value = {
                    "schema_version": "strathmark-v3-receipt-disagreement-v1",
                    "decision": decision,
                    "operational_receipt": {
                        **operational_content,
                        "verification_status": "verified",
                        "receipt_digest": canonical_digest(operational_content),
                    },
                }
        else:
            value = section.payload.to_value()
        sections.append(ReceiptSection(section.kind, InlinePayload.from_value(value)))
    return FieldReceipt.create(**{**base.creation_arguments(), "sections": tuple(sections)})


@pytest.mark.parametrize(
    ("available", "color", "council", "expected_lane"),
    [
        (3, "green", 3, ApprovalLane.NORMAL_GREEN),
        (3, "amber", 3, ApprovalLane.NORMAL_AMBER),
        (3, "green", 2, ApprovalLane.DEGRADED_COUNCIL),
        (2, "green", 3, ApprovalLane.DEGRADED_TWO),
        (1, "green", 3, ApprovalLane.MANUAL_SINGLE),
        (0, "green", 3, ApprovalLane.MANUAL_ZERO),
    ],
)
def test_typed_receipt_facts_not_warning_claims_drive_approval_lane(
    available: int, color: str, council: int, expected_lane: ApprovalLane
) -> None:
    receipt = _typed_approval_receipt(
        available_count=available, color=color, council_valid_count=council
    )
    forged_warning = FieldReceipt.create(
        **{
            **receipt.creation_arguments(),
            "warning_codes": ("red_consequence",),
        }
    )
    facts = derive_receipt_approval_facts(forged_warning, u5_current=True, integrity_verified=True)

    assert derive_approval_lane(facts) is expected_lane


def test_typed_validation_flags_are_causal_and_canonical() -> None:
    receipt = _typed_approval_receipt(available_counts=(3, 2), color="amber", council_valid_count=3)

    facts = derive_receipt_approval_facts(receipt, u5_current=True, integrity_verified=True)

    assert facts.flagged_competitor_ids == ("competitor:a", "competitor:b")
    assert facts.flag_reason_tokens == (
        ("competitor:a", ("consequence_amber",)),
        (
            "competitor:b",
            ("assessor_availability_2_of_3", "consequence_amber"),
        ),
    )


def test_missing_operational_disagreement_blocks_even_green_warning() -> None:
    receipt = _typed_approval_receipt(available_count=3, color="green")
    sections = tuple(
        ReceiptSection(
            section.kind,
            InlinePayload.from_value(
                {
                    "schema_version": "strathmark-v3-receipt-disagreement-v1",
                    "decision": None,
                    "operational_receipt": None,
                }
                if section.kind is ReceiptSectionKind.DISAGREEMENT
                else section.payload.to_value()
            ),
        )
        for section in receipt.sections
    )
    missing = FieldReceipt.create(
        **{**receipt.creation_arguments(), "sections": sections, "warning_codes": ()}
    )

    facts = derive_receipt_approval_facts(missing, u5_current=True, integrity_verified=True)

    assert derive_approval_lane(facts) is ApprovalLane.INTEGRITY_BLOCKED
    assert "operational_disagreement_unavailable" in facts.reason_codes


@pytest.mark.parametrize(
    ("target", "replacement"),
    [
        ("verification_status", "pending"),
        ("decision_digest", "d" * 64),
        ("color", "red"),
        ("policy_digest", "d" * 64),
        ("field_revision_digest", "d" * 64),
        ("pooled_optimizer_verification_digest", "d" * 64),
        ("receipt_digest", "d" * 64),
    ],
)
def test_operational_disagreement_wrapper_must_bind_exact_raw_and_u14_authority(
    target: str, replacement: str
) -> None:
    receipt = _typed_approval_receipt(available_count=3, color="green")
    sections = []
    for section in receipt.sections:
        value = section.payload.to_value()
        if section.kind is ReceiptSectionKind.DISAGREEMENT:
            value["operational_receipt"][target] = replacement
        sections.append(ReceiptSection(section.kind, InlinePayload.from_value(value)))
    tampered = FieldReceipt.create(**{**receipt.creation_arguments(), "sections": tuple(sections)})

    facts = derive_receipt_approval_facts(tampered, u5_current=True, integrity_verified=True)

    assert derive_approval_lane(facts) is ApprovalLane.INTEGRITY_BLOCKED
    assert "receipt_typed_facts_invalid" in facts.reason_codes


@pytest.mark.parametrize(
    "counts",
    [
        (3, 2, 1),
        (3, 1, 0),
    ],
)
def test_mixed_roster_requires_complete_manual_authority_not_single_survivor(
    counts: tuple[int, ...],
) -> None:
    receipt = _typed_approval_receipt(available_counts=counts, color="red")

    facts = derive_receipt_approval_facts(receipt, u5_current=True, integrity_verified=True)

    assert facts.integrity is IntegrityState.VERIFIED
    assert facts.availability is (
        AvailabilityMode.MANUAL_ZERO if 0 in counts else AvailabilityMode.MANUAL_SINGLE
    )
    assert derive_approval_lane(facts) is (
        ApprovalLane.MANUAL_ZERO if 0 in counts else ApprovalLane.MANUAL_COMPLETE
    )
    assert facts.availability_counts == tuple(
        (f"competitor:{chr(ord('a') + index)}", count) for index, count in enumerate(counts)
    )


def test_real_assembled_receipt_projects_detail_decision_restart_and_exact_rebuild(
    tmp_path: Path,
) -> None:
    from tests.v3.integration.test_field_receipts import NOW, _bootstrap

    path = tmp_path / "approval.sqlite3"
    store, field, build, _lifecycle = _bootstrap(path)
    result = FieldAssemblyService(store).assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:approval-real-field",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=build,
    )

    page = store.approval_page(tournament_id=str(field.tournament_id), offset=0, limit=10)
    assert page.total == 1
    row = page.rows[0]
    assert row.receipt_id == str(result.receipt.receipt_id)
    assert row.lane is not ApprovalLane.INTEGRITY_BLOCKED
    detail = store.approval_detail(
        tournament_id=str(field.tournament_id),
        snapshot_id=page.snapshot_id,
        receipt_id=row.receipt_id,
    )
    assert detail["receipt"] == result.receipt.to_dict()
    assert detail["explanation"] == render_verified_receipt_explanation(result.receipt).to_dict()
    assert len(detail["explanation"]["text"].encode("utf-8")) <= 4096
    with open_v3_connection(path) as connection:
        snapshot_row = connection.execute(
            "SELECT row_digest FROM v3_approval_snapshot_rows WHERE snapshot_id=? AND field_id=?",
            (page.snapshot_id, row.field_id),
        ).fetchone()
    assert snapshot_row is not None
    assert snapshot_row[0] != row.decision_state.value
    assert len(str(snapshot_row[0])) == 64

    command = ApprovalDecisionCommand.create(
        caller_namespace="manager",
        request_identity="idempotency:approval-real-decision",
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
        submitted_at="2026-08-24T18:00:30.000Z",
    )
    decision = store.record_approval_decision(command)
    assert store.record_approval_decision(command) == decision
    changed_retry = ApprovalDecisionCommand.create(
        caller_namespace=command.caller_namespace,
        request_identity=command.request_identity,
        tournament_id=command.tournament_id,
        snapshot_id=command.snapshot_id,
        action=command.action,
        selected=command.selected,
        excluded=(),
        actor_id=command.actor_id,
        actor_metadata={"station": "ring-two"},
        reason_code=command.reason_code,
        submitted_at=command.submitted_at,
    )
    with pytest.raises(EventStoreConflict):
        store.record_approval_decision(changed_retry)
    other_namespace = ApprovalDecisionCommand.create(
        caller_namespace="manager-secondary",
        request_identity=command.request_identity,
        tournament_id=command.tournament_id,
        snapshot_id=command.snapshot_id,
        action=command.action,
        selected=command.selected,
        excluded=(),
        actor_id=command.actor_id,
        actor_metadata={"station": "ring-one"},
        reason_code=command.reason_code,
        submitted_at=command.submitted_at,
    )
    with pytest.raises(ApprovalConflict):
        store.record_approval_decision(other_namespace)
    decided = store.approval_page(tournament_id=str(field.tournament_id), offset=0, limit=10)
    assert decision.decisions == ((row.receipt_id, DecisionState.ACCEPTED),)
    assert decided.rows[0].decision_state is DecisionState.ACCEPTED
    assert decided.empty_reason is QueueEmptyReason.NO_BATCH_ELIGIBLE_FIELDS
    assert decided.earliest_deadline_at is None

    restarted = SQLiteFieldProjectionStore(
        path, signer=store._signer, trust_store=store._trust_store
    )
    restarted_page = restarted.approval_page(
        tournament_id=str(field.tournament_id), offset=0, limit=10
    )
    assert restarted_page == decided
    with open_v3_connection(path) as connection:
        with immediate_transaction(connection):
            connection.execute("DELETE FROM v3_approval_decision_projection")
            connection.execute("DELETE FROM v3_approval_command_projection")
            connection.execute("DELETE FROM v3_approval_details")
            connection.execute("DELETE FROM v3_approval_queue_rows")
            connection.execute("DELETE FROM v3_approval_projection_meta")
    rebuilt_snapshot = restarted.rebuild_approval_projection(
        tournament_id=str(field.tournament_id),
        rebuilt_at="2026-08-24T18:01:00.000Z",
    )
    rebuilt = restarted.approval_page(tournament_id=str(field.tournament_id), offset=0, limit=10)
    assert rebuilt_snapshot == decided.snapshot_id
    assert rebuilt == decided

    with open_v3_connection(path) as connection:
        with immediate_transaction(connection):
            stored = connection.execute("SELECT row_json FROM v3_approval_queue_rows").fetchone()
            assert stored is not None
            import json

            row_value = json.loads(str(stored[0]))
            row_value["call_order"] += 1
            row_value.pop("row_digest")
            row_value["row_digest"] = canonical_digest(row_value)
            connection.execute(
                "UPDATE v3_approval_queue_rows SET call_order=?, row_json=?, row_digest=?",
                (
                    row_value["call_order"],
                    canonical_bytes(row_value).decode(),
                    row_value["row_digest"],
                ),
            )
    with pytest.raises(ProjectionError, match="approval projection"):
        SQLiteFieldProjectionStore(path, signer=store._signer, trust_store=store._trust_store)


def test_constructed_zero_assessor_receipt_can_be_approved_as_a_separate_act(
    tmp_path: Path,
) -> None:
    from strathmark.v3.application.field_assembly import ManualConstructionSubmission
    from strathmark.v3.domain.disagreement import OverrideScope
    from tests.v3.integration.test_field_receipts import (
        ACTOR,
        NOW,
        _bootstrap,
        _pipeline_with_supersession,
    )

    store, field, build, _lifecycle = _bootstrap(
        tmp_path / "approval-construction.sqlite3",
        available_assessors=(),
    )
    service = FieldAssemblyService(store)
    initial = service.assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:approval-zero-candidate",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=build,
    )
    candidate = build(field)
    assert candidate.manual_authority is not None
    submission = ManualConstructionSubmission.create(
        prior_receipt_id=initial.receipt.receipt_id,
        prior_receipt_digest=initial.receipt.content_digest,
        upstream_field_revision=field.field_revision,
        field_revision_digest=field.revision_digest,
        manual_authority_digest=candidate.manual_authority.authority_digest,
        actor_id=ACTOR,
        reason_code="judge_complete_expected_time_construction",
        scope=OverrideScope.UPCOMING_RACE,
        submitted_at="2026-08-24T18:00:01.000Z",
    )
    constructed = service.submit_construction(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:approval-submit-construction",
        actor_id="actor:manager",
        occurred_at="2026-08-24T18:00:01.000Z",
        build_pipeline=lambda _field: _pipeline_with_supersession(
            candidate, construction_submission=submission
        ),
    )

    page = store.approval_page(tournament_id=str(field.tournament_id), offset=0, limit=10)
    assert page.total == 1
    row = page.rows[0]
    assert row.receipt_id == str(constructed.receipt.receipt_id)
    assert row.receipt_revision == 2
    assert row.upstream_field_revision == 1
    assert row.lane is ApprovalLane.MANUAL_ZERO
    command = ApprovalDecisionCommand.create(
        caller_namespace="manager",
        request_identity="idempotency:approval-constructed-receipt",
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
        reason_code="judge-approved-deliberate-construction",
        submitted_at="2026-08-24T18:00:02.000Z",
    )

    decision = store.record_approval_decision(command)

    assert decision.decisions == ((row.receipt_id, DecisionState.ACCEPTED),)


@pytest.mark.parametrize(
    "scope",
    (
        OverrideScope.UPCOMING_RACE,
        OverrideScope.REMAINING_EVENT_CONFIGURATION,
        OverrideScope.REMAINING_TOURNAMENT,
    ),
)
def test_raw_time_override_with_unchanged_mark_uses_exact_typed_authority(
    tmp_path: Path,
    scope: OverrideScope,
) -> None:
    from strathmark.v3.application.field_assembly import (
        OperationalExpectedTimeOverrideAuthority,
    )
    from strathmark.v3.domain.disagreement import (
        ExpectedTimeOverrideRequest,
        FieldSheetSnapshot,
        OptimizerVerificationStatus,
        OverrideRecomputationProof,
        create_override_receipt,
    )
    from tests.v3.integration.test_field_receipts import (
        NOW,
        _bootstrap,
        _pipeline_with_supersession,
    )

    manual_medians = {"competitor:b": 45_000, "competitor:a": 55_000}
    path = tmp_path / f"approval-raw-time-override-{scope.value}.sqlite3"
    store, field, build, _lifecycle = _bootstrap(
        path,
        available_assessors=(),
        manual_medians=manual_medians,
    )
    scope_boundary_id = {
        OverrideScope.UPCOMING_RACE: field.field_id,
        OverrideScope.REMAINING_EVENT_CONFIGURATION: StableIdentifier(
            "event_config:approval-underhand-300-pine"
        ),
        OverrideScope.REMAINING_TOURNAMENT: field.tournament_id,
    }[scope]

    def applicable_override(
        projection: SQLiteFieldProjectionStore, *, in_scope: bool
    ) -> object | None:
        tournament_id = field.tournament_id
        field_id = StableIdentifier("field:later-round")
        target_context_digest = field.target_context.digest
        if scope is OverrideScope.UPCOMING_RACE:
            field_id = field.field_id if in_scope else StableIdentifier("field:later-round")
        elif scope is OverrideScope.REMAINING_EVENT_CONFIGURATION:
            target_context_digest = field.target_context.digest if in_scope else "f" * 64
        elif not in_scope:
            tournament_id = StableIdentifier("tournament:other")
        return projection.active_expected_time_override(
            tournament_id=tournament_id,
            field_id=field_id,
            target_context_digest=target_context_digest,
            call_order=field.call_order + 1,
            competitor_id=StableIdentifier("competitor:b"),
        )

    service = FieldAssemblyService(store)
    before_pipeline = build(field)
    before = service.assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:approval-override-before",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=lambda _field: before_pipeline,
    )
    before_sheet = FieldSheetSnapshot.create(
        field_id=field.field_id,
        expected_times_ms=tuple(
            (item.competitor_id, item.expected_time_ms)
            for item in before_pipeline.optimizer.field.competitors
        ),
        marks=tuple((item.competitor_id, item.mark) for item in before.receipt.marks),
        pool_receipt_digest=before_pipeline.optimizer.field.pool_receipt_digest,
        optimizer_receipt_digest=before_pipeline.optimizer.receipt.receipt_digest,
        optimizer_verification_status=OptimizerVerificationStatus.PENDING,
    )
    manual_medians["competitor:b"] = 45_100
    after_pipeline = build(field)
    after_sheet = FieldSheetSnapshot.create(
        field_id=field.field_id,
        expected_times_ms=tuple(
            (item.competitor_id, item.expected_time_ms)
            for item in after_pipeline.optimizer.field.competitors
        ),
        marks=tuple(
            zip(
                after_pipeline.optimizer.receipt.competitor_ids,
                after_pipeline.optimizer.receipt.selected_marks,
                strict=True,
            )
        ),
        pool_receipt_digest=after_pipeline.optimizer.field.pool_receipt_digest,
        optimizer_receipt_digest=after_pipeline.optimizer.receipt.receipt_digest,
        optimizer_verification_status=OptimizerVerificationStatus.PENDING,
    )
    request = ExpectedTimeOverrideRequest.create(
        override_id=StableIdentifier("override:approval-competitor-b"),
        competitor_id=StableIdentifier("competitor:b"),
        target_context_digest=field.target_context.digest,
        expected_raw_time_ms=45_100,
        scope=scope,
        scope_boundary_id=scope_boundary_id,
        actor="actor:manager",
        reason="judge corrected the starting estimate",
        supersedes_override_id=None,
    )
    after_sections = after_pipeline.section_values()
    override_receipt = create_override_receipt(
        request,
        before_sheet,
        after_sheet,
        OverrideRecomputationProof.create(before_sheet, after_sheet),
        canonical_digest(
            next(
                value for kind, value in after_sections.items() if kind.value == "component_outputs"
            )
        ),
        canonical_digest(
            next(
                value
                for kind, value in after_sections.items()
                if kind.value == "pooled_distribution"
            )
        ),
        field.evidence_digest,
        field.tournament_epoch_id,
    )
    authority = OperationalExpectedTimeOverrideAuthority.create(
        prior_receipt_id=before.receipt.receipt_id,
        prior_receipt_digest=before.receipt.content_digest,
        upstream_field_revision=field.field_revision,
        field_revision_digest=field.revision_digest,
        override_receipt=override_receipt,
        after_optimizer_verification_digest=after_pipeline.optimizer.verification_digest,
    )
    overridden = service.submit_expected_time_override(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:approval-override-submit",
        actor_id="actor:manager",
        occurred_at="2026-08-24T18:00:01.000Z",
        build_pipeline=lambda _field: _pipeline_with_supersession(
            after_pipeline, expected_time_override=authority
        ),
    )
    page = store.approval_page(tournament_id=str(field.tournament_id), offset=0, limit=10)
    row = page.rows[0]
    assert row.receipt_id == str(overridden.receipt.receipt_id)
    assert row.changed_marks == ()
    command = ApprovalDecisionCommand.create(
        caller_namespace="manager",
        request_identity="idempotency:approval-override-decision",
        tournament_id=str(field.tournament_id),
        snapshot_id=page.snapshot_id,
        action=ApprovalDecisionAction.OVERRIDE_SUBMITTED,
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
        actor_id="actor:manager",
        actor_metadata={"station": "ring-one"},
        reason_code="expected-time-override-accepted",
        superseded_receipt_id=str(before.receipt.receipt_id),
        submitted_at="2026-08-24T18:00:02.000Z",
    )

    decision = store.record_approval_decision(command)

    assert decision.decisions == ((row.receipt_id, DecisionState.OVERRIDE_SUBMITTED),)
    state = applicable_override(store, in_scope=True)
    assert state is not None
    assert state.override_id == StableIdentifier("override:approval-competitor-b")
    assert state.expected_raw_time_ms == 45_100
    assert state.is_result_evidence is False
    assert state.is_training_evidence is False

    manual_medians["competitor:b"] = 45_200
    superseding_pipeline = build(field)
    superseding_sheet = FieldSheetSnapshot.create(
        field_id=field.field_id,
        expected_times_ms=tuple(
            (item.competitor_id, item.expected_time_ms)
            for item in superseding_pipeline.optimizer.field.competitors
        ),
        marks=tuple(
            zip(
                superseding_pipeline.optimizer.receipt.competitor_ids,
                superseding_pipeline.optimizer.receipt.selected_marks,
                strict=True,
            )
        ),
        pool_receipt_digest=superseding_pipeline.optimizer.field.pool_receipt_digest,
        optimizer_receipt_digest=superseding_pipeline.optimizer.receipt.receipt_digest,
        optimizer_verification_status=OptimizerVerificationStatus.PENDING,
    )
    superseding_request = ExpectedTimeOverrideRequest.create(
        override_id=StableIdentifier("override:approval-competitor-b-next"),
        competitor_id=StableIdentifier("competitor:b"),
        target_context_digest=field.target_context.digest,
        expected_raw_time_ms=45_200,
        scope=scope,
        scope_boundary_id=scope_boundary_id,
        actor="actor:manager",
        reason="superseded starting estimate",
        supersedes_override_id=override_receipt.override_id,
    )
    superseding_sections = superseding_pipeline.section_values()
    superseding_receipt = create_override_receipt(
        superseding_request,
        after_sheet,
        superseding_sheet,
        OverrideRecomputationProof.create(after_sheet, superseding_sheet),
        canonical_digest(
            next(
                value
                for kind, value in superseding_sections.items()
                if kind.value == "component_outputs"
            )
        ),
        canonical_digest(
            next(
                value
                for kind, value in superseding_sections.items()
                if kind.value == "pooled_distribution"
            )
        ),
        field.evidence_digest,
        field.tournament_epoch_id,
    )
    superseding_authority = OperationalExpectedTimeOverrideAuthority.create(
        prior_receipt_id=overridden.receipt.receipt_id,
        prior_receipt_digest=overridden.receipt.content_digest,
        upstream_field_revision=field.field_revision,
        field_revision_digest=field.revision_digest,
        override_receipt=superseding_receipt,
        after_optimizer_verification_digest=(superseding_pipeline.optimizer.verification_digest),
    )
    superseded = service.submit_expected_time_override(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:approval-override-supersede",
        actor_id="actor:manager",
        occurred_at="2026-08-24T18:00:03.000Z",
        build_pipeline=lambda _field: _pipeline_with_supersession(
            superseding_pipeline,
            expected_time_override=superseding_authority,
        ),
    )
    superseding_page = store.approval_page(
        tournament_id=str(field.tournament_id), offset=0, limit=10
    )
    superseding_row = superseding_page.rows[0]
    store.record_approval_decision(
        ApprovalDecisionCommand.create(
            caller_namespace="manager",
            request_identity="idempotency:approval-override-supersede-decision",
            tournament_id=str(field.tournament_id),
            snapshot_id=superseding_page.snapshot_id,
            action=ApprovalDecisionAction.OVERRIDE_SUBMITTED,
            selected=(
                ApprovalDecisionSelection(
                    superseding_row.field_id,
                    superseding_row.receipt_id,
                    superseding_row.receipt_revision,
                    superseding_row.upstream_field_revision,
                    superseding_row.row_digest,
                    superseding_row.call_order,
                ),
            ),
            excluded=(),
            actor_id="actor:manager",
            actor_metadata={"station": "ring-one"},
            reason_code="expected-time-override-superseded",
            superseded_receipt_id=str(overridden.receipt.receipt_id),
            submitted_at="2026-08-24T18:00:04.000Z",
        )
    )
    current = applicable_override(store, in_scope=True)
    assert current is not None
    assert current.override_id == superseding_receipt.override_id
    assert current.supersedes_override_id == override_receipt.override_id
    assert current.expected_raw_time_ms == 45_200
    assert superseded.receipt.receipt_revision == 3

    restarted = SQLiteFieldProjectionStore(
        path, signer=store._signer, trust_store=store._trust_store
    )
    restarted_in_scope = applicable_override(restarted, in_scope=True)
    assert restarted_in_scope is not None
    assert restarted_in_scope.override_id == superseding_receipt.override_id
    assert restarted_in_scope.supersedes_override_id == override_receipt.override_id
    assert applicable_override(restarted, in_scope=False) is None
    with open_v3_connection(path, read_only=True) as connection:
        before_rebuild_rows = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT override_id,active,superseded_by_override_id,state_digest "
                "FROM v3_expected_time_override_states ORDER BY source_global_sequence"
            )
        )
    assert tuple(row[:3] for row in before_rebuild_rows) == (
        (
            str(override_receipt.override_id),
            0,
            str(superseding_receipt.override_id),
        ),
        (
            str(superseding_receipt.override_id),
            1,
            None,
        ),
    )
    assert all(len(str(row[3])) == 64 for row in before_rebuild_rows)
    before_rebuild_in_scope = restarted_in_scope.to_dict()

    restarted.rebuild_approval_projection(
        tournament_id=str(field.tournament_id),
        rebuilt_at="2026-08-24T18:01:00.000Z",
    )

    rebuilt_in_scope = applicable_override(restarted, in_scope=True)
    assert rebuilt_in_scope is not None
    assert rebuilt_in_scope.to_dict() == before_rebuild_in_scope
    assert applicable_override(restarted, in_scope=False) is None
    with open_v3_connection(path, read_only=True) as connection:
        after_rebuild_rows = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT override_id,active,superseded_by_override_id,state_digest "
                "FROM v3_expected_time_override_states ORDER BY source_global_sequence"
            )
        )
    assert after_rebuild_rows == before_rebuild_rows

    from strathmark.v3.application.field_assembly import AssemblyConflict
    from strathmark.v3.application.pipeline_builder import SQLiteCapabilityStateResolver

    resolver = SQLiteCapabilityStateResolver(path, trust_store=store._trust_store)
    assert (
        resolver.resolve_override_current(field, StableIdentifier("competitor:b"))
        == rebuilt_in_scope
    )

    forged = rebuilt_in_scope.to_dict()
    forged["expected_raw_time_ms"] += 12_345
    forged["state_digest"] = canonical_digest(
        {key: value for key, value in forged.items() if key != "state_digest"}
    )
    with open_v3_connection(path) as connection:
        connection.execute(
            "UPDATE v3_expected_time_override_states SET state_json=?,state_digest=? "
            "WHERE override_id=?",
            (
                canonical_bytes(forged).decode(),
                forged["state_digest"],
                forged["override_id"],
            ),
        )

    with pytest.raises(ProjectionError, match="override.*authority"):
        applicable_override(restarted, in_scope=True)
    with pytest.raises(AssemblyConflict, match="override.*authority"):
        resolver.resolve_override_current(field, StableIdentifier("competitor:b"))


def test_old_snapshot_row_tamper_is_rejected_before_stale_recovery(
    tmp_path: Path,
) -> None:
    from tests.v3.integration.test_field_receipts import NOW, _bootstrap

    path = tmp_path / "approval-old-history-tamper.sqlite3"
    store, field, build, _lifecycle = _bootstrap(path)
    FieldAssemblyService(store).assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:approval-history-field",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=build,
    )
    page = store.approval_page(tournament_id=str(field.tournament_id), offset=0, limit=10)
    row = page.rows[0]
    command = ApprovalDecisionCommand.create(
        caller_namespace="manager",
        request_identity="idempotency:approval-history-decision",
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
        actor_metadata={},
        reason_code="judge-approved-history-test",
        submitted_at="2026-08-24T18:00:30.000Z",
    )
    store.record_approval_decision(command)
    with open_v3_connection(path) as connection:
        original_history = [
            tuple(item)
            for item in connection.execute(
                "SELECT snapshot_id, tournament_id, field_id, receipt_id, "
                "receipt_revision, upstream_field_revision, row_digest "
                "FROM v3_approval_snapshot_rows ORDER BY snapshot_id, field_id"
            )
        ]
        with immediate_transaction(connection):
            connection.execute(
                "UPDATE v3_approval_snapshot_rows SET row_digest=? WHERE snapshot_id=?",
                ("f" * 64, page.snapshot_id),
            )

    with pytest.raises(ProjectionError, match="snapshot rows"):
        SQLiteFieldProjectionStore(path, signer=store._signer, trust_store=store._trust_store)

    store.rebuild_approval_projection(
        tournament_id=str(field.tournament_id),
        rebuilt_at="2026-08-24T18:01:00.000Z",
    )
    with open_v3_connection(path, read_only=True) as connection:
        rebuilt_history = [
            tuple(item)
            for item in connection.execute(
                "SELECT snapshot_id, tournament_id, field_id, receipt_id, "
                "receipt_revision, upstream_field_revision, row_digest "
                "FROM v3_approval_snapshot_rows ORDER BY snapshot_id, field_id"
            )
        ]
    assert rebuilt_history == original_history
    store.verify()


def test_sqlite_page_exposes_tournament_scoped_preparing_readiness(
    tmp_path: Path,
) -> None:
    from tests.v3.integration.test_field_receipts import _bootstrap

    store, field, _build, _lifecycle = _bootstrap(tmp_path / "preparing.sqlite3")

    page = store.approval_page(tournament_id=str(field.tournament_id), offset=0, limit=10)

    assert page.tournament_id == str(field.tournament_id)
    assert page.total == 0
    assert page.preparation_total == 1
    assert page.preparation_completed == 0
    assert page.preparing_count == 1
    assert page.ready_count == page.blocked_count == page.issued_count == 0
    assert page.empty_reason is QueueEmptyReason.STILL_PREPARING
    assert page.source_global_sequence > 0
    assert page.lane_counts == ()
    assert page.earliest_deadline_at == field.deadline_at
    assert page.retry_guidance == (
        "reload_snapshot_on_conflict",
        "poll_while_preparing",
        "open_verified_detail_for_exceptions",
    )


def test_issued_then_closed_tournament_has_durable_empty_envelopes(
    tmp_path: Path,
) -> None:
    from strathmark.v3.contracts.statuses import OfficialResult, ResultStatus
    from strathmark.v3.domain.epochs import MandatoryReaction
    from strathmark.v3.domain.evidence import LiveResultSubmission
    from tests.v3.integration.test_field_receipts import NOW, _bootstrap

    path = tmp_path / "issued-closed.sqlite3"
    store, field, build, lifecycle = _bootstrap(path)
    assembled = FieldAssemblyService(store).assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:approval-issued-field",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=build,
    )
    _append_lifecycle_event(
        lifecycle,
        command_kind=CommandKind.ACKNOWLEDGE_ISSUE,
        event_kind=EventKind.FIELD_ISSUED,
        aggregate_kind=AggregateKind.FIELD,
        target=field.field_id,
        payload={
            "round_id": str(field.round_id),
            "epoch_id": str(field.tournament_epoch_id),
            "field_revision": field.field_revision,
            "receipt_id": str(assembled.receipt.receipt_id),
            "competitor_ids": [str(item) for item in assembled.receipt.ordered_competitor_ids],
            "issued_marks": {
                str(item.competitor_id): item.mark for item in assembled.receipt.marks
            },
        },
        command_id="command:approval-issue-current-field",
        occurred_at="2026-08-24T18:00:30.000Z",
    )

    issued = store.approval_page(tournament_id=str(field.tournament_id), offset=0, limit=10)
    assert issued.total == 0
    assert issued.issued_count == 1
    assert issued.lifecycle_state == "all_issued"
    assert issued.empty_reason is QueueEmptyReason.ALL_ISSUED
    assert issued.earliest_deadline_at is None

    marks = {str(item.competitor_id): item.mark for item in assembled.receipt.marks}
    result_sources: list[tuple[int, int]] = []
    for placing, competitor_id in enumerate(assembled.receipt.ordered_competitor_ids, start=1):
        raw_time_ms = 12_000 + placing * 100
        stored_result = lifecycle.record_live_result(
            LiveResultSubmission(
                StableIdentifier(f"evidence:approval-{placing}"),
                competitor_id,
                field.tournament_id,
                field.round_id,
                field.field_id,
                field.target_context,
                "2026-08-24T18:00:30.000Z",
                marks[str(competitor_id)],
                15_000 + placing * 100,
                placing,
                0,
                OfficialResult(ResultStatus.COMPLETION, raw_time_ms, None, 1, None),
                canonical_digest(
                    {
                        "field_id": str(field.field_id),
                        "competitor_id": str(competitor_id),
                        "raw_time_ms": raw_time_ms,
                    }
                ),
            ),
            field_revision=field.field_revision,
            claimed_receipt_id=assembled.receipt.receipt_id,
            command_id=IdempotencyKey(f"command:approval-result-{placing}"),
            actor_id=StableIdentifier("actor:manager"),
            occurred_at_utc="2026-08-24T18:00:30.000Z",
            monotonic_elapsed_ms=50 + placing,
        )
        result_sources.append((placing, stored_result.first_global_sequence))
    lifecycle.settle_live_race(
        field.field_id,
        field_revision=field.field_revision,
        claimed_receipt_id=assembled.receipt.receipt_id,
        command_id=IdempotencyKey("command:approval-settle-field"),
        actor_id=StableIdentifier("actor:manager"),
        occurred_at_utc="2026-08-24T18:00:31.000Z",
        monotonic_elapsed_ms=70,
    )
    for placing, source_sequence in result_sources:
        for reaction in MandatoryReaction:
            lifecycle.complete_derivation_reaction(
                source_sequence,
                reaction,
                canonical_digest(
                    {
                        "source": source_sequence,
                        "reaction": reaction.value,
                    }
                ),
                command_id=IdempotencyKey(f"command:approval-result-{placing}-{reaction.value}"),
                actor_id=StableIdentifier("actor:manager"),
                occurred_at_utc="2026-08-24T18:00:30.000Z",
                monotonic_elapsed_ms=60 + placing,
            )

    _append_lifecycle_event(
        lifecycle,
        command_kind=CommandKind.BEGIN_ROUND_CLOSING,
        event_kind=EventKind.ROUND_CLOSING_STARTED,
        aggregate_kind=AggregateKind.ROUND,
        target=field.round_id,
        payload={"closing": True},
        command_id="command:approval-begin-close-final-round",
        occurred_at="2026-08-24T18:00:32.000Z",
    )
    lifecycle.close_evidence_round(
        field.round_id,
        command_id=IdempotencyKey("command:approval-close-final-round"),
        actor_id=StableIdentifier("actor:manager"),
        occurred_at_utc="2026-08-24T18:00:33.000Z",
        monotonic_elapsed_ms=51,
    )
    lifecycle.close_tournament(
        field.tournament_id,
        command_id=IdempotencyKey("command:approval-close-tournament"),
        actor_id=StableIdentifier("actor:manager"),
        occurred_at_utc="2026-08-24T18:00:34.000Z",
        monotonic_elapsed_ms=52,
    )
    closed = store.approval_page(tournament_id=str(field.tournament_id), offset=0, limit=10)
    assert closed.total == 0
    assert closed.lifecycle_state == "closed"
    assert closed.empty_reason is QueueEmptyReason.TOURNAMENT_CLOSED
    assert closed.earliest_deadline_at is None

    restarted = SQLiteFieldProjectionStore(
        path, signer=store._signer, trust_store=store._trust_store
    )
    assert (
        restarted.approval_page(tournament_id=str(field.tournament_id), offset=0, limit=10)
        == closed
    )


def test_two_tournaments_are_snapshot_isolated_and_close_independently(
    tmp_path: Path,
) -> None:
    from strathmark.v3.application.lifecycle import SnapshotKind, UpstreamSnapshot
    from tests.v3.integration.test_field_receipts import NOW, _bootstrap

    path = tmp_path / "two-tournaments.sqlite3"
    store, first_field, _build, lifecycle = _bootstrap(path)
    first_before = store.approval_page(
        tournament_id=str(first_field.tournament_id), offset=0, limit=10
    )
    second_tournament = StableIdentifier("tournament:second-show")
    second_round = StableIdentifier("round:second-final")
    second_field = StableIdentifier("field:second-final")
    lifecycle.ingest_snapshot(
        UpstreamSnapshot(
            SnapshotKind.TOURNAMENT,
            second_tournament,
            1,
            second_tournament,
            None,
            {
                "bundle_id": "bundle:verified",
                "historical_cutoff_key": "history:second-cutoff",
            },
        ),
        command_id=IdempotencyKey("command:second-tournament-snapshot"),
        actor_id=StableIdentifier("actor:manager"),
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=60,
    )
    lifecycle.ingest_snapshot(
        UpstreamSnapshot(
            SnapshotKind.ROUND,
            second_round,
            1,
            second_tournament,
            second_round,
            {
                "round_ordinal": 1,
                "predecessor_round_ids": [],
                "successor_round_ids": [],
            },
        ),
        command_id=IdempotencyKey("command:second-round-snapshot"),
        actor_id=StableIdentifier("actor:manager"),
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=61,
    )
    _append_lifecycle_event(
        lifecycle,
        command_kind=CommandKind.CONFIGURE_TOURNAMENT,
        event_kind=EventKind.TOURNAMENT_CONFIGURED,
        aggregate_kind=AggregateKind.TOURNAMENT,
        target=second_tournament,
        payload={"configured": True},
        command_id="command:configure-second-tournament",
        occurred_at=NOW,
    )
    _append_lifecycle_event(
        lifecycle,
        command_kind=CommandKind.CONFIGURE_ROUND,
        event_kind=EventKind.ROUND_CONFIGURED,
        aggregate_kind=AggregateKind.ROUND,
        target=second_round,
        payload={"configured": True},
        command_id="command:configure-second-round",
        occurred_at=NOW,
    )
    lifecycle.open_tournament(
        second_tournament,
        bundle_id=StableIdentifier("bundle:verified"),
        historical_cutoff_key="history:second-cutoff",
        root_round_ids=(second_round,),
        command_id=IdempotencyKey("command:open-second-tournament"),
        actor_id=StableIdentifier("actor:manager"),
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=62,
    )
    lifecycle.freeze_round_epoch(
        second_round,
        epoch_revision=1,
        historical_cutoff_key="history:second-cutoff",
        closure_ids=(),
        command_id=IdempotencyKey("command:freeze-second-round"),
        actor_id=StableIdentifier("actor:manager"),
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=63,
    )
    lifecycle.ingest_snapshot(
        UpstreamSnapshot(
            SnapshotKind.FIELD,
            second_field,
            1,
            second_tournament,
            second_round,
            {
                "competitor_ids": ["competitor:c", "competitor:d"],
                "target_context": first_field.target_context.to_dict(),
                "stand_ids": ["stand:second-left", "stand:second-right"],
                "capacity_authority_digest": first_field.capacity_authority_digest,
                "max_field_entrants": first_field.max_field_entrants,
                "call_order": 1,
                "scheduled_at": "2026-08-24T18:03:00.000Z",
                "deadline_at": "2026-08-24T18:05:00.000Z",
            },
        ),
        command_id=IdempotencyKey("command:second-field-snapshot"),
        actor_id=StableIdentifier("actor:manager"),
        occurred_at_utc="2026-08-24T18:00:01.000Z",
        monotonic_elapsed_ms=64,
    )

    first_after_second_ingress = store.approval_page(
        tournament_id=str(first_field.tournament_id), offset=0, limit=10
    )
    second_open = store.approval_page(tournament_id=str(second_tournament), offset=0, limit=10)
    assert first_after_second_ingress == first_before
    assert second_open.preparation_total == 1
    assert second_open.empty_reason is QueueEmptyReason.STILL_PREPARING

    _append_lifecycle_event(
        lifecycle,
        command_kind=CommandKind.BEGIN_ROUND_CLOSING,
        event_kind=EventKind.ROUND_CLOSING_STARTED,
        aggregate_kind=AggregateKind.ROUND,
        target=second_round,
        payload={"closing": True},
        command_id="command:begin-close-second-round",
        occurred_at="2026-08-24T18:00:02.000Z",
    )
    lifecycle.close_evidence_round(
        second_round,
        command_id=IdempotencyKey("command:close-second-round"),
        actor_id=StableIdentifier("actor:manager"),
        occurred_at_utc="2026-08-24T18:00:03.000Z",
        monotonic_elapsed_ms=65,
    )
    lifecycle.close_tournament(
        second_tournament,
        command_id=IdempotencyKey("command:close-second-tournament"),
        actor_id=StableIdentifier("actor:manager"),
        occurred_at_utc="2026-08-24T18:00:04.000Z",
        monotonic_elapsed_ms=66,
    )
    second_closed = store.approval_page(tournament_id=str(second_tournament), offset=0, limit=10)
    first_after_close = store.approval_page(
        tournament_id=str(first_field.tournament_id), offset=0, limit=10
    )
    assert second_closed.lifecycle_state == "closed"
    assert second_closed.empty_reason is QueueEmptyReason.TOURNAMENT_CLOSED
    assert second_closed.total == 0
    assert first_after_close == first_before

    restarted = SQLiteFieldProjectionStore(
        path, signer=store._signer, trust_store=store._trust_store
    )
    assert (
        restarted.approval_page(tournament_id=str(first_field.tournament_id), offset=0, limit=10)
        == first_before
    )
    assert (
        restarted.approval_page(tournament_id=str(second_tournament), offset=0, limit=10)
        == second_closed
    )


def test_stale_snapshot_reports_new_unselected_scheduled_field(
    tmp_path: Path,
) -> None:
    from strathmark.v3.application.lifecycle import SnapshotKind, UpstreamSnapshot
    from tests.v3.integration.test_field_receipts import NOW, _bootstrap

    path = tmp_path / "added-field.sqlite3"
    store, field, build, lifecycle = _bootstrap(path)
    FieldAssemblyService(store).assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:approval-before-added-field",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=build,
    )
    before = store.approval_page(tournament_id=str(field.tournament_id), offset=0, limit=10)
    row = before.rows[0]
    lifecycle.ingest_snapshot(
        UpstreamSnapshot(
            SnapshotKind.FIELD,
            StableIdentifier("field:next"),
            1,
            field.tournament_id,
            field.round_id,
            {
                "competitor_ids": ["competitor:c", "competitor:d"],
                "target_context": field.target_context.to_dict(),
                "stand_ids": ["stand:next-left", "stand:next-right"],
                "capacity_authority_digest": field.capacity_authority_digest,
                "max_field_entrants": field.max_field_entrants,
                "call_order": 2,
                "scheduled_at": "2026-08-24T18:03:00.000Z",
                "deadline_at": "2026-08-24T18:05:00.000Z",
            },
        ),
        command_id=IdempotencyKey("command:add-next-field"),
        actor_id=StableIdentifier("actor:manager"),
        occurred_at_utc="2026-08-24T18:00:31.000Z",
        monotonic_elapsed_ms=31,
    )
    command = ApprovalDecisionCommand.create(
        caller_namespace="manager",
        request_identity="idempotency:approval-stale-added-field",
        tournament_id=str(field.tournament_id),
        snapshot_id=before.snapshot_id,
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
        actor_metadata={},
        reason_code="stale-after-new-unselected-field",
        submitted_at="2026-08-24T18:00:32.000Z",
    )

    with pytest.raises(ApprovalConflict) as caught:
        store.record_approval_decision(command)

    assert caught.value.changes == (
        ApprovalConflictChange("field:next", None, None, 1, "added_field"),
    )
    current = store.approval_page(tournament_id=str(field.tournament_id), offset=0, limit=10)
    current_row = current.rows[0]
    store.record_approval_decision(
        ApprovalDecisionCommand.create(
            caller_namespace="manager",
            request_identity="idempotency:approval-after-added-field",
            tournament_id=str(field.tournament_id),
            snapshot_id=current.snapshot_id,
            action=ApprovalDecisionAction.INDIVIDUAL_ACCEPT,
            selected=(
                ApprovalDecisionSelection(
                    current_row.field_id,
                    current_row.receipt_id,
                    current_row.receipt_revision,
                    current_row.upstream_field_revision,
                    current_row.row_digest,
                    current_row.call_order,
                ),
            ),
            excluded=(),
            actor_id="actor:judge",
            actor_metadata={},
            reason_code="accept-while-next-field-prepares",
            submitted_at="2026-08-24T18:00:33.000Z",
        )
    )
    after_accept = store.approval_page(tournament_id=str(field.tournament_id), offset=0, limit=10)
    assert after_accept.preparing_count == 1
    assert after_accept.empty_reason is QueueEmptyReason.STILL_PREPARING
    assert after_accept.earliest_deadline_at == "2026-08-24T18:05:00.000Z"


def test_live_self_redigested_lane_tamper_cannot_append_approval(
    tmp_path: Path,
) -> None:
    import json

    from strathmark.v3.contracts.events import EventKind
    from tests.v3.integration.test_field_receipts import NOW, _bootstrap

    path = tmp_path / "approval-tamper.sqlite3"
    store, field, build, _lifecycle = _bootstrap(path)
    FieldAssemblyService(store).assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:approval-tamper-field",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=build,
    )
    page = store.approval_page(tournament_id=str(field.tournament_id), offset=0, limit=10)
    assert page.rows[0].ordinary_batch_eligible is True
    with open_v3_connection(path) as connection:
        with immediate_transaction(connection):
            stored = connection.execute("SELECT row_json FROM v3_approval_queue_rows").fetchone()
            assert stored is not None
            forged = json.loads(str(stored[0]))
            forged["facts"]["availability"] = AvailabilityMode.DEGRADED_TWO.value
            forged["facts"]["availability_counts"] = [
                [competitor_id, 2]
                for competitor_id, _count in forged["facts"]["availability_counts"]
            ]
            forged["lane"] = ApprovalLane.DEGRADED_TWO.value
            forged["ordinary_batch_eligible"] = False
            forged["degraded_batch_eligible"] = True
            forged.pop("row_digest")
            forged["row_digest"] = canonical_digest(forged)
            connection.execute(
                "UPDATE v3_approval_queue_rows SET lane=?, "
                "ordinary_batch_eligible=0, degraded_batch_eligible=1, "
                "row_json=?, row_digest=?",
                (
                    ApprovalLane.DEGRADED_TWO.value,
                    canonical_bytes(forged).decode(),
                    forged["row_digest"],
                ),
            )
    with pytest.raises(ProjectionError, match="canonical authority"):
        store.approval_page(tournament_id=str(field.tournament_id), offset=0, limit=10)
    with open_v3_connection(path, read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM v3_events WHERE event_kind=?",
                (EventKind.APPROVAL_DECISION_RECORDED.value,),
            ).fetchone()[0]
            == 0
        )


def test_stale_conflict_prioritizes_selected_field_beyond_global_bound() -> None:
    old = {f"field:{index:03d}": ("old",) for index in range(101)}
    current = {f"field:{index:03d}": ("new",) for index in range(101)}

    fields, total = _prioritized_approval_conflict_fields(
        old,
        current,
        priority_field_ids=("field:100",),
        limit=100,
    )

    assert fields[0] == "field:100"
    assert len(fields) == 100
    assert total == 101
    conflict = ApprovalConflict("stale", changes=(), total_changes=total)
    assert conflict.total_changes == 101
    assert conflict.changes_truncated is True
    with pytest.raises(ApprovalError, match="change count"):
        ApprovalConflict("stale", changes=(), total_changes=-1)
