from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from strathmark.v3.api.app import TransportError, create_v3_app  # noqa: E402
from strathmark.v3.api.auth import (  # noqa: E402
    InMemoryCredentialSecretStore,
    ServiceCredentialRegistry,
)
from strathmark.v3.application.approval import (  # noqa: E402
    ApprovalDecisionAction,
    ApprovalDecisionCommand,
    ApprovalDecisionSelection,
)
from strathmark.v3.application.capability_reactions import (  # noqa: E402
    CapabilityAdmissionVerifier,
    CapabilityCapacityEnvelope,
    CapabilityCapacityVerifier,
    CapabilityReactionService,
    seal_capability_admission,
    seal_capability_capacity,
)
from strathmark.v3.application.capacity import CapacityUse  # noqa: E402
from strathmark.v3.application.coordinator import (  # noqa: E402
    DurableRollingPreparationCoordinator,
    PreparationCandidate,
    PreparationClass,
)
from strathmark.v3.application.gateway import (  # noqa: E402
    V3ApplicationGateway,
    VerifiedV3CutoverState,
)
from strathmark.v3.application.issuance import IssuanceService  # noqa: E402
from strathmark.v3.application.lifecycle import LifecycleService  # noqa: E402
from strathmark.v3.application.pipeline_builder import (  # noqa: E402
    CoordinatorRollingFieldInputSource,
    RollingFieldPipelineBuilder,
    SQLiteCapabilityStateResolver,
)
from strathmark.v3.assessors.formula import FormulaManifest  # noqa: E402
from strathmark.v3.composition import compose_v3_application_gateway  # noqa: E402
from strathmark.v3.contracts.canonical import canonical_digest  # noqa: E402
from strathmark.v3.contracts.evidence import (
    EvidencePacket,
    ResultObservation,
)  # noqa: E402
from strathmark.v3.contracts.identifiers import (  # noqa: E402
    IdempotencyKey,
    StableIdentifier,
    deterministic_identifier,
)
from strathmark.v3.contracts.statuses import OfficialResult, ResultStatus  # noqa: E402
from strathmark.v3.domain.capability import CapabilityPrior  # noqa: E402
from strathmark.v3.domain.credibility import WeightReceipt  # noqa: E402
from strathmark.v3.domain.disagreement import (
    DisagreementPolicy,
    ZeroHistoryPolicy,
)  # noqa: E402
from strathmark.v3.domain.evidence import (  # noqa: E402
    AdmissionReason,
    AdmittedEvidence,
    EvidenceSource,
    LiveResultSubmission,
)
from strathmark.v3.infrastructure.integrity import (  # noqa: E402
    CriticalIssueCoordinator,
    CriticalJournal,
)
from strathmark.v3.infrastructure.sqlite.connection import (
    open_v3_connection,
)  # noqa: E402
from strathmark.v3.infrastructure.sqlite.event_store import (
    SQLiteEventStore,
)  # noqa: E402
from strathmark.v3.infrastructure.sqlite.jobs import DurableJobRepository  # noqa: E402
from tests.v3.integration.test_field_receipts import (  # noqa: E402
    NOW,
    _bootstrap,
    _capacity_manifest,
)
from tests.v3.integration.test_rolling_preparation import (  # noqa: E402
    T1,
    T2,
    _aggregate_manifest,
    _council_manifest,
)


def _headers(credential: str, key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {credential}", "Idempotency-Key": key}


@dataclass
class _CapabilitySourceAuthority:
    accepted: set[int]

    def verify_source(self, evidence) -> None:
        assert evidence.source_global_sequence in self.accepted

    def invalidated_unissued_work(self, _evidence):
        return ()

    def mandatory_reaction_count(self, _evidence, _lineage, _invalidated):
        return 0

    def verify_source_at_commit(self, _connection, evidence) -> None:
        self.verify_source(evidence)


class _TrackingSettlementReactions:
    def __init__(self, database_path: Path, *, fail_once: bool = False) -> None:
        self.database_path = database_path.resolve()
        self.fail_once = fail_once
        self.calls = 0

    def react(self, _stored) -> None:
        self.calls += 1
        if self.fail_once and self.calls == 1:
            raise RuntimeError("injected post-commit reaction failure")


def _runtime(
    tmp_path: Path,
    *,
    fail_reactions_once: bool = False,
    schedule_cross_epoch_decoy: bool = False,
    verified_cutover: VerifiedV3CutoverState | None = None,
):
    database = tmp_path / "runtime.sqlite3"
    store, field, build, _lifecycle = _bootstrap(database)
    pipeline = build(field)
    formula_manifest = FormulaManifest.load("benchmarks/v3/formula_manifest.json")
    source_sequences = set(range(101, 101 + len(pipeline.pools)))
    capacity = seal_capability_capacity(
        CapabilityCapacityEnvelope(), signer=store._signer, created_at=NOW
    )
    reaction_service = CapabilityReactionService(
        database,
        verifier=CapabilityAdmissionVerifier(store._trust_store),
        capacity=capacity,
        capacity_verifier=CapabilityCapacityVerifier(store._trust_store),
        authority=_CapabilitySourceAuthority(source_sequences),
    )
    runtime_cards = []
    for index, pool in enumerate(pipeline.pools):
        card = pool.card
        raw_time_ms = 50_000 + index * 10_000
        source_sequence = 101 + index
        result_key = StableIdentifier(f"result:gateway-{index}")
        observation = ResultObservation(
            evidence_id=StableIdentifier(f"evidence:gateway-{index}"),
            competitor_id=card.competitor_id,
            tournament_id=field.tournament_id,
            round_id=field.round_id,
            field_id=field.field_id,
            context=field.target_context,
            observation_sequence=1,
            occurred_at_utc=NOW,
            issued_mark=3,
            completion_clock_ms=raw_time_ms + 3_000,
            placing=index + 1,
            gap_ms=index * 1_000,
            result=OfficialResult(ResultStatus.COMPLETION, raw_time_ms, None, 1, None),
            source_digest=canonical_digest(
                {"result_key": str(result_key), "raw_time_ms": raw_time_ms}
            ),
        )
        admitted = AdmittedEvidence(
            observation,
            EvidenceSource.LIVE_ISSUED_RACE,
            True,
            raw_time_ms,
            AdmissionReason.ELIGIBLE_COMPLETION,
        )
        sealed = seal_capability_admission(
            admitted=admitted,
            result_key=result_key,
            source_global_sequence=source_sequence,
            authority_digest="e" * 64,
            prior=CapabilityPrior.from_median_seconds("40", calibrated_beta="0.12"),
            evidence_log_variance="0.0025",
            conversion_log_variance="0",
            effective_weight="1",
            historical_binding=None,
            signer=store._signer,
            created_at=NOW,
        )
        reaction_service.react(
            sealed,
            command_id=IdempotencyKey(f"command:gateway-capability-{index}"),
            actor_id=StableIdentifier("actor:system"),
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=index,
            complete_derivation_barrier=False,
        )
        runtime_cards.append(card)
    repository = DurableJobRepository(
        database,
        capacity=_capacity_manifest(),
        signer=store._signer,
        trust_store=store._trust_store,
    )
    coordinator = DurableRollingPreparationCoordinator(
        repository,
        signer=store._signer,
        trust_store=store._trust_store,
    )
    council = _council_manifest(store._signer, bundle_digest=field.bundle_digest)
    coordinator.install_council_authority(council, installed_at=NOW)
    candidates = tuple(
        PreparationCandidate.create(
            competitor_id=str(card.evidence_packet.competitor_id),
            target_context_digest=card.evidence_packet.target_context.digest,
            historical_cutoff_key=str(card.evidence_packet.historical_cutoff_key),
            tournament_epoch_id=str(card.evidence_packet.tournament_epoch_id),
            bundle_digest=card.bundle_digest,
            evidence_digest=card.evidence_packet.content_digest,
            dependency_revision=101 + index,
            preparation_class=PreparationClass.IMMINENT_FIELD,
            hard_deadline_at=field.deadline_at,
            evidence_packet=card.evidence_packet,
        )
        for index, card in enumerate(runtime_cards)
    )
    scheduled = coordinator.schedule(
        candidates,
        capacity_use=CapacityUse(1, 2, 2, 2, 2, 1_024, 4_096, 25),
        council_manifest_digest=council.body_digest,
        observed_at=NOW,
    )
    candidate_by_digest = {item.key.card_digest: item for item in candidates}
    card_by_digest = {
        candidate.key.card_digest: card
        for candidate, card in zip(candidates, runtime_cards, strict=True)
    }
    pending = len(scheduled)
    ordinal = 0
    while pending:
        claimed = None
        for lane in {item.lane for item in scheduled}:
            claimed = repository.claim(
                lane,
                worker_id=f"worker:gateway-{ordinal}",
                clock=lambda: T1,
                lease_duration_ms=60_000,
            )
            if claimed is not None:
                break
        assert claimed is not None
        ordinal += 1
        card = card_by_digest[claimed.payload()["card_key"]["card_digest"]]
        result_digest = {
            "formula": card.forecasts[0].commit_digest,
            "ml": card.forecasts[1].commit_digest,
            "local_qwen35_9b": "4" * 64,
            "local_ministral3_8b": "5" * 64,
            "frontier_cloud": "6" * 64,
        }[claimed.payload()["component_id"]]
        candidate = candidate_by_digest[claimed.payload()["card_key"]["card_digest"]]
        repository.commit_success(
            claimed.job_id,
            claimed.job_revision,
            worker_id=f"worker:gateway-{ordinal - 1}",
            fencing_token=claimed.fencing_token,
            result_digest=result_digest,
            current_context=lambda _connection, _record, candidate=candidate: (
                candidate.key.evidence_digest,
                candidate.key.bundle_digest,
            ),
            clock=lambda: T2,
        )
        pending -= 1
    for candidate in candidates:
        card = card_by_digest[candidate.key.card_digest]
        coordinator.seal_card(
            candidate.key,
            card,
            council_manifest_digest=council.body_digest,
            council_aggregate_authority=_aggregate_manifest(
                store._signer,
                candidate,
                card,
                council,
                repository,
            ),
            observed_at=T2,
        )
    if schedule_cross_epoch_decoy:
        current = candidates[0]
        packet = current.evidence_packet
        assert isinstance(packet, EvidencePacket)
        decoy_packet = EvidencePacket.create(
            competitor_id=packet.competitor_id,
            target_context=packet.target_context,
            observations=packet.observations,
            taxonomy_version=packet.taxonomy_version,
            conversion_version=packet.conversion_version,
            historical_cutoff_key="history:decoy",
            tournament_epoch_id=StableIdentifier("epoch:decoy"),
            tournament_event_sequence=packet.tournament_event_sequence,
        )
        decoy = PreparationCandidate.create(
            competitor_id=str(decoy_packet.competitor_id),
            target_context_digest=decoy_packet.target_context.digest,
            historical_cutoff_key=decoy_packet.historical_cutoff_key,
            tournament_epoch_id=str(decoy_packet.tournament_epoch_id),
            bundle_digest=current.key.bundle_digest,
            evidence_digest=decoy_packet.content_digest,
            dependency_revision=current.key.dependency_revision + 1,
            preparation_class=PreparationClass.IMMINENT_FIELD,
            hard_deadline_at=current.hard_deadline_at,
            evidence_packet=decoy_packet,
        )
        coordinator.schedule(
            (decoy,),
            capacity_use=CapacityUse(1, 2, 2, 2, 2, 1_024, 4_096, 25),
            council_manifest_digest=council.body_digest,
            observed_at=NOW,
        )
    weight_receipt = WeightReceipt(
        pipeline.weight_authority.context,
        pipeline.weight_authority.weights,
        (),
        pipeline.weight_authority.calibration_cutoff_at_utc,
        pipeline.weight_authority.policy_digest,
        pipeline.weight_authority.weight_receipt_digest,
    )
    disagreement_policy = DisagreementPolicy(
        "disagreement:v1",
        2_000,
        20_000,
        5_000,
        20_000,
        1,
        2,
        "0.2",
        "0.49",
        10_000,
        50_000,
        "6" * 64,
        "7" * 64,
    )
    source = CoordinatorRollingFieldInputSource(
        coordinator,
        authority_verifier=store,
        capability_resolver=SQLiteCapabilityStateResolver(database, trust_store=store._trust_store),
        weight_receipt=weight_receipt,
        operational_weight_authority=pipeline.operational_weight_authority,
        dependence_artifact=pipeline.dependence_artifact,
        disagreement_policy=disagreement_policy,
        formula_manifest=formula_manifest,
        zero_history_policy=ZeroHistoryPolicy("0.05", "0.95", 10_000, "zero-history:v1"),
    )
    rolling_builder = RollingFieldPipelineBuilder(
        source,
        signer=store._signer,
        trust_store=store._trust_store,
        clock=lambda: NOW,
    )
    issue_coordinator = CriticalIssueCoordinator.for_rehearsal(
        CriticalJournal(
            tmp_path / "issue-journal",
            signer=store._signer,
            trust_store=store._trust_store,
        )
    )
    reactions = _TrackingSettlementReactions(database, fail_once=fail_reactions_once)
    gateway = compose_v3_application_gateway(
        database_path=database,
        signer=store._signer,
        trust_store=store._trust_store,
        pipeline_builder=rolling_builder,
        job_repository=repository,
        issue_coordinator=issue_coordinator,
        settlement_reactions=reactions,
        clock=lambda: "2026-08-25T18:00:00.000Z",
    )
    if verified_cutover is not None:
        gateway = V3ApplicationGateway(
            gateway._services,
            clock=lambda: "2026-08-25T18:00:00.000Z",
            verified_cutover=lambda: verified_cutover,
        )
    registry = ServiceCredentialRegistry(
        SQLiteEventStore(database), InMemoryCredentialSecretStore()
    )
    credential = registry.bootstrap_offline(
        principal_id="actor:tournament-manager",
        listener_stopped=True,
        credential="smv3.runtime-key.runtime-secret-12345678901234",
    )
    return (
        TestClient(
            create_v3_app(gateway=gateway, credentials=registry),
            raise_server_exceptions=not fail_reactions_once,
        ),
        credential.credential,
        store,
        field,
        reactions,
        repository,
    )


def _assemble_approve_and_issue(client, credential, store, field, *, key: str):
    assembled = client.post(
        "/v3/fields/assemble",
        headers=_headers(credential, f"assemble-{key}"),
        json={
            "schema_version": "strathmark-v3-field-assembly-request-v1",
            "field_id": str(field.field_id),
            "upstream_field_revision": field.field_revision,
            "ordered_competitor_ids": [
                str(item.competitor_id) for item in field.ordered_assignments
            ],
            "deadline_ms": 10_000,
        },
    )
    assert assembled.status_code == 200, assembled.text
    receipt_id = assembled.json()["receipt_id"]
    page = store.approval_page(tournament_id=str(field.tournament_id), offset=0, limit=10)
    row = next(item for item in page.rows if item.receipt_id == receipt_id)
    if row.decision_state.value == "undecided":
        store.record_approval_decision(
            ApprovalDecisionCommand.create(
                caller_namespace="manager",
                request_identity=f"idempotency:{key}-approve",
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
                actor_metadata={"station": key},
                reason_code="judge-reviewed-runtime-sheet",
                submitted_at="2026-08-25T18:00:01.000Z",
            )
        )
    issued = client.post(
        "/v3/issues/acknowledge",
        headers=_headers(credential, f"issue-{key}"),
        json={
            "schema_version": "strathmark-v3-issue-acknowledgment-request-v1",
            "upstream_issue_id": f"upstream_issue:{key}",
            "receipt_bindings": [
                {
                    "receipt_id": receipt_id,
                    "receipt_digest": assembled.json()["receipt_digest"],
                }
            ],
            "issued_at_utc": "2026-08-25T18:00:02.000Z",
            "deadline_ms": 10_000,
        },
    )
    assert issued.status_code == 200, issued.text
    return receipt_id, issued.json()["issue_batch_id"]


def _settlement_body(store, field, receipt_id: str, issue_batch_id: str):
    receipt = store.receipt(receipt_id)
    return {
        "schema_version": "strathmark-v3-settlement-request-v1",
        "issue_batch_id": issue_batch_id,
        "receipt_id": receipt_id,
        "results": [
            {
                "competitor_id": str(item),
                "status": "completion",
                "raw_time_ms": 40_000 + index * 1_000,
                "penalty_ms": None,
                "source_revision": 1,
            }
            for index, item in enumerate(receipt.ordered_competitor_ids)
        ],
        "observed_at_utc": "2026-08-25T18:00:03.000Z",
        "deadline_ms": 10_000,
    }


def test_concrete_gateway_executes_prepare_assemble_issue_lookup_and_settlement(
    tmp_path: Path,
) -> None:
    client, credential, store, field, reactions, _repository = _runtime(tmp_path)
    live_control_payload = {
        "reason_code": "runtime_contract_proof",
        "schema_version": "strathmark-v3-suspend-live-v1",
    }
    command_body = {
        "schema_version": "strathmark-v3-command-execution-request-v1",
        "command_kind": "suspend_live",
        "target_aggregate": "weights:runtime-proof",
        "expected_versions": [{"aggregate_id": "weights:runtime-proof", "version": 0}],
        "payload_schema_version": live_control_payload["schema_version"],
        "canonical_payload_json": (
            '{"reason_code":"runtime_contract_proof",'
            '"schema_version":"strathmark-v3-suspend-live-v1"}'
        ),
        "payload_digest": canonical_digest(live_control_payload),
        "deadline_ms": 5_000,
    }
    command = client.post(
        "/v3/commands/execute",
        headers=_headers(credential, "suspend-runtime"),
        json=command_body,
    )
    assert command.status_code == 422, command.text
    assert command.json()["code"] == "request_validation_failed"
    assert all(
        event.command.kind.value != "suspend_live"
        for event in SQLiteEventStore(reactions.database_path).events()
    )

    first_competitor = str(field.ordered_assignments[0].competitor_id)
    prepared = client.post(
        "/v3/cards/prepare",
        headers=_headers(credential, "prepare-a"),
        json={
            "schema_version": "strathmark-v3-card-preparation-request-v1",
            "tournament_id": str(field.tournament_id),
            "round_id": str(field.round_id),
            "field_id": str(field.field_id),
            "competitor_id": first_competitor,
            "source_revision": field.field_revision,
            "target_context_digest": field.target_context.digest,
            "deadline_ms": 5_000,
        },
    )
    assert prepared.status_code == 202, prepared.text
    assert prepared.json()["status"] == "ready"

    assembled = client.post(
        "/v3/fields/assemble",
        headers=_headers(credential, "assemble-final"),
        json={
            "schema_version": "strathmark-v3-field-assembly-request-v1",
            "field_id": str(field.field_id),
            "upstream_field_revision": field.field_revision,
            "ordered_competitor_ids": [
                str(item.competitor_id) for item in field.ordered_assignments
            ],
            "deadline_ms": 10_000,
        },
    )
    assert assembled.status_code == 200, assembled.text
    receipt_id = assembled.json()["receipt_id"]
    assert assembled.json()["disposition"] == "prepared"

    page = store.approval_page(tournament_id=str(field.tournament_id), offset=0, limit=10)
    row = page.rows[0]
    if row.decision_state.value == "undecided":
        store.record_approval_decision(
            ApprovalDecisionCommand.create(
                caller_namespace="manager",
                request_identity="idempotency:runtime-approve",
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
                actor_metadata={"station": "runtime-proof"},
                reason_code="judge-reviewed-runtime-sheet",
                submitted_at="2026-08-25T18:00:01.000Z",
            )
        )
    assert store.approval_page(tournament_id=str(field.tournament_id), offset=0, limit=10).rows[
        0
    ].decision_state.value in {"accepted", "override-submitted"}

    issued = client.post(
        "/v3/issues/acknowledge",
        headers=_headers(credential, "issue-final"),
        json={
            "schema_version": "strathmark-v3-issue-acknowledgment-request-v1",
            "upstream_issue_id": "upstream_issue:runtime-final",
            "receipt_bindings": [
                {
                    "receipt_id": receipt_id,
                    "receipt_digest": assembled.json()["receipt_digest"],
                }
            ],
            "issued_at_utc": "2026-08-25T18:00:02.000Z",
            "deadline_ms": 10_000,
        },
    )
    assert issued.status_code == 200, issued.text

    lookup = client.post(
        "/v3/receipts/lookup",
        headers=_headers(credential, "lookup-final"),
        json={
            "schema_version": "strathmark-v3-receipt-lookup-request-v1",
            "request_identity": assembled.json()["receipt_id"].replace("receipt:", "command:"),
            "receipt_id": receipt_id,
            "deadline_ms": 5_000,
        },
    )
    assert lookup.status_code == 200, lookup.text
    assert lookup.json()["canonical_receipt_json"] == assembled.json()["canonical_receipt_json"]

    marks = {str(item.competitor_id): item.mark for item in store.receipt(receipt_id).marks}
    results = [
        {
            "competitor_id": str(item.competitor_id),
            "status": "completion",
            "raw_time_ms": 40_000 + index * 1_000,
            "penalty_ms": None,
            "source_revision": 1,
        }
        for index, item in enumerate(field.ordered_assignments)
    ]
    assert set(marks) == {item["competitor_id"] for item in results}
    settled = client.post(
        "/v3/results/settle",
        headers=_headers(credential, "settle-final"),
        json={
            "schema_version": "strathmark-v3-settlement-request-v1",
            "issue_batch_id": issued.json()["issue_batch_id"],
            "receipt_id": receipt_id,
            "results": results,
            "observed_at_utc": "2026-08-25T18:00:03.000Z",
            "deadline_ms": 10_000,
        },
    )
    assert settled.status_code == 200, settled.text
    assert settled.json()["status"] == "recorded"
    retry = client.post(
        "/v3/results/settle",
        headers=_headers(credential, "settle-final"),
        json={
            "schema_version": "strathmark-v3-settlement-request-v1",
            "issue_batch_id": issued.json()["issue_batch_id"],
            "receipt_id": receipt_id,
            "results": results,
            "observed_at_utc": "2026-08-25T18:00:03.000Z",
            "deadline_ms": 10_000,
        },
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "recovered"
    assert reactions.calls == 2
    assert (
        client.get("/v3/status", headers=_headers(credential, "ignored")).json()[
            "open_tournament_count"
        ]
        == 1
    )


@pytest.mark.parametrize("command_kind", ("promote_bundle", "rollback_bundle", "record_monitoring"))
def test_gateway_defense_in_depth_rejects_factory_control_commands(
    command_kind: str,
) -> None:
    gateway = object.__new__(V3ApplicationGateway)

    with pytest.raises(TransportError) as raised:
        gateway.execute_command(
            {"command_kind": command_kind},
            SimpleNamespace(),
        )

    assert getattr(raised.value, "status_code", None) == 409
    assert getattr(raised.value, "code", None) == "command_requires_specialized_service"


def test_status_separates_candidate_health_from_production_authority(tmp_path) -> None:
    client, credential, *_rest = _runtime(tmp_path)

    status = client.get("/v3/status", headers=_headers(credential, "status")).json()

    assert status["service"] == "ready"
    assert status["v3_readiness"] == "candidate"
    assert status["production_authority"] == "v2"
    assert status["engine_authority"] == "v2"
    assert status["cutover_receipt_digest"] is None
    assert status["cutover_verified_at_utc"] is None
    assert status["deep_verification_state"] == "verified"
    assert len(status["event_checkpoint_digest"]) == 64
    assert len(status["field_checkpoint_digest"]) == 64
    assert len(status["job_checkpoint_digest"]) == 64


def test_status_uses_bounded_checkpoints_after_explicit_startup_deep_verify(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, credential, store, _field, _reactions, repository = _runtime(tmp_path)

    def forbidden_deep_verify(*_args, **_kwargs):
        raise AssertionError("status polling must not perform a lifetime deep verification")

    monkeypatch.setattr(SQLiteEventStore, "verify", forbidden_deep_verify)
    monkeypatch.setattr(type(store), "verify", forbidden_deep_verify)
    monkeypatch.setattr(type(repository), "verify", forbidden_deep_verify)

    response = client.get("/v3/status", headers=_headers(credential, "bounded-status"))

    assert response.status_code == 200, response.text
    status = response.json()
    assert status["deep_verification_state"] == "verified"
    for field_name in (
        "event_last_deep_verified_at_utc",
        "field_last_deep_verified_at_utc",
        "job_last_deep_verified_at_utc",
    ):
        assert status[field_name].endswith("Z")


def test_status_does_not_overclaim_missing_field_deep_verification(tmp_path) -> None:
    store, _field, _build, _lifecycle = _bootstrap(tmp_path / "empty-status.sqlite3")
    checkpoint = {
        "authority_sequence": 0,
        "authority_digest": "0" * 64,
        "checkpoint_digest": "a" * 64,
        "last_deep_verified_at": "2026-08-25T18:00:00.000Z",
    }
    missing_field_checkpoint = {
        "authority_sequence": 0,
        "authority_digest": "0" * 64,
        "projection_digest": "0" * 64,
        "checkpoint_digest": "0" * 64,
        "last_deep_verified_at": "1970-01-01T00:00:00.000Z",
    }
    gateway = object.__new__(V3ApplicationGateway)
    gateway._services = SimpleNamespace(
        events=SimpleNamespace(
            database_path=store.database_path,
            integrity_checkpoint_status=lambda: checkpoint,
        ),
        fields=SimpleNamespace(integrity_checkpoint_status=lambda: missing_field_checkpoint),
        jobs=SimpleNamespace(integrity_checkpoint_status=lambda: checkpoint),
    )
    gateway._verified_cutover = lambda: None

    status = gateway.status(SimpleNamespace())

    assert status.deep_verification_state == "unavailable"
    assert status.field_last_deep_verified_at_utc == "1970-01-01T00:00:00.000Z"
    assert status.field_checkpoint_digest == "0" * 64


def test_receipt_lookup_is_namespace_bound_and_uses_composite_index(tmp_path) -> None:
    client, credential, store, field, *_rest = _runtime(tmp_path)
    assembled = client.post(
        "/v3/fields/assemble",
        headers=_headers(credential, "cross-namespace-assemble"),
        json={
            "schema_version": "strathmark-v3-field-assembly-request-v1",
            "field_id": str(field.field_id),
            "upstream_field_revision": field.field_revision,
            "ordered_competitor_ids": [
                str(item.competitor_id) for item in field.ordered_assignments
            ],
            "deadline_ms": 10_000,
        },
    )
    assert assembled.status_code == 200, assembled.text
    receipt_id = assembled.json()["receipt_id"]
    with open_v3_connection(store.database_path) as connection:
        connection.execute(
            "UPDATE v3_field_receipts SET caller_namespace=?,request_identity=? WHERE receipt_id=?",
            ("other", "command:cross-namespace", receipt_id),
        )
        connection.commit()
    headers = _headers(credential, "cross-namespace-lookup")
    by_request = client.post(
        "/v3/receipts/lookup",
        headers=headers,
        json={
            "schema_version": "strathmark-v3-receipt-lookup-request-v1",
            "request_identity": "command:cross-namespace",
            "receipt_id": None,
            "deadline_ms": 250,
        },
    )
    by_receipt = client.post(
        "/v3/receipts/lookup",
        headers={**headers, "Idempotency-Key": "cross-namespace-id"},
        json={
            "schema_version": "strathmark-v3-receipt-lookup-request-v1",
            "request_identity": "command:unused",
            "receipt_id": receipt_id,
            "deadline_ms": 250,
        },
    )

    assert by_request.status_code == by_receipt.status_code == 200
    assert by_request.json()["found"] is False
    assert by_receipt.json()["found"] is False
    with open_v3_connection(store.database_path, read_only=True) as connection:
        detail = " ".join(
            str(row[3])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT receipt_id FROM v3_field_receipts "
                "WHERE caller_namespace=? AND request_identity=?",
                ("api", "command:cross-namespace"),
            )
        )
    assert "USING INDEX" in detail
    assert "caller_namespace=? AND request_identity=?" in detail


def test_status_accepts_only_explicit_verified_cutover_authority(tmp_path) -> None:
    cutover = VerifiedV3CutoverState(
        receipt_digest="a" * 64,
        verified_at_utc="2026-08-25T17:59:00.000Z",
    )
    client, credential, *_rest = _runtime(tmp_path, verified_cutover=cutover)

    status = client.get("/v3/status", headers=_headers(credential, "status")).json()

    assert status["service"] == "ready"
    assert status["v3_readiness"] == "production"
    assert status["production_authority"] == "v3"
    assert status["engine_authority"] == "v3"
    assert status["cutover_receipt_digest"] == "a" * 64
    assert status["cutover_verified_at_utc"] == "2026-08-25T17:59:00.000Z"
    assert status["deep_verification_state"] == "verified"


def test_prepare_card_rejects_newer_same_context_card_from_another_epoch(
    tmp_path: Path,
) -> None:
    client, credential, _store, field, _reactions, repository = _runtime(
        tmp_path, schedule_cross_epoch_decoy=True
    )
    competitor_id = str(field.ordered_assignments[0].competitor_id)
    broad = repository.current_rolling_card_key(competitor_id, field.target_context.digest)
    assert broad is not None
    assert broad["tournament_epoch_id"] == "epoch:decoy"

    response = client.post(
        "/v3/cards/prepare",
        headers=_headers(credential, "prepare-exact-epoch"),
        json={
            "schema_version": "strathmark-v3-card-preparation-request-v1",
            "tournament_id": str(field.tournament_id),
            "round_id": str(field.round_id),
            "field_id": str(field.field_id),
            "competitor_id": competitor_id,
            "source_revision": field.field_revision,
            "target_context_digest": field.target_context.digest,
            "deadline_ms": 5_000,
        },
    )

    assert response.status_code == 202, response.text
    assert response.json()["status"] == "ready"


def test_issue_finds_approved_receipt_beyond_first_64_rows_and_uses_one_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, credential, store, field, _reactions, _repository = _runtime(tmp_path)
    assembled = client.post(
        "/v3/fields/assemble",
        headers=_headers(credential, "assemble-page-65"),
        json={
            "schema_version": "strathmark-v3-field-assembly-request-v1",
            "field_id": str(field.field_id),
            "upstream_field_revision": field.field_revision,
            "ordered_competitor_ids": [
                str(item.competitor_id) for item in field.ordered_assignments
            ],
            "deadline_ms": 10_000,
        },
    )
    assert assembled.status_code == 200, assembled.text
    receipt_id = assembled.json()["receipt_id"]
    page = store.approval_page(tournament_id=str(field.tournament_id), offset=0, limit=1)
    row = page.rows[0]
    store.record_approval_decision(
        ApprovalDecisionCommand.create(
            caller_namespace="manager",
            request_identity="idempotency:page-65-approve",
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
            actor_metadata={"station": "page-65"},
            reason_code="page-65-reviewed",
            submitted_at="2026-08-25T18:00:01.000Z",
        )
    )
    current = store.approval_page(tournament_id=str(field.tournament_id), offset=0, limit=1)
    approved_row = current.rows[0]
    calls: list[tuple[str, str]] = []

    def approval_page(self, *, tournament_id, offset, limit, snapshot_id=None):
        assert offset == 0 and limit == 1 and snapshot_id is None
        return replace(current, total=65, rows=())

    def approval_detail(self, *, tournament_id, snapshot_id, receipt_id):
        calls.append((snapshot_id, receipt_id))
        assert tournament_id == str(field.tournament_id)
        return {"row": approved_row.to_dict()}

    def acknowledge(_self, command):
        assert command.approval_snapshot_id == current.snapshot_id
        return SimpleNamespace(
            issue_batch_id="issue_batch:page-65",
            receipt_ids=(receipt_id,),
            last_global_sequence=store._events.current_anchor().global_sequence,
            result_digest="a" * 64,
        )

    monkeypatch.setattr(type(store), "approval_page", approval_page)
    monkeypatch.setattr(type(store), "approval_detail", approval_detail)
    monkeypatch.setattr(IssuanceService, "acknowledge", acknowledge)
    issued = client.post(
        "/v3/issues/acknowledge",
        headers=_headers(credential, "issue-page-65"),
        json={
            "schema_version": "strathmark-v3-issue-acknowledgment-request-v1",
            "upstream_issue_id": "upstream_issue:page-65",
            "receipt_bindings": [
                {
                    "receipt_id": receipt_id,
                    "receipt_digest": assembled.json()["receipt_digest"],
                }
            ],
            "issued_at_utc": "2026-08-25T18:00:02.000Z",
            "deadline_ms": 10_000,
        },
    )
    assert issued.status_code == 200, issued.text
    assert calls == [(current.snapshot_id, receipt_id)]


def test_exact_settlement_retry_redrives_post_commit_reactions_without_restart(
    tmp_path: Path,
) -> None:
    client, credential, store, field, reactions, _repository = _runtime(
        tmp_path, fail_reactions_once=True
    )
    receipt_id, issue_batch_id = _assemble_approve_and_issue(
        client, credential, store, field, key="reaction-retry"
    )
    body = _settlement_body(store, field, receipt_id, issue_batch_id)

    first = client.post(
        "/v3/results/settle",
        headers=_headers(credential, "reaction-retry-settlement"),
        json=body,
    )
    assert first.status_code == 500
    retry = client.post(
        "/v3/results/settle",
        headers=_headers(credential, "reaction-retry-settlement"),
        json=body,
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "recovered"
    assert reactions.calls == 2


def test_original_settlement_retry_recovers_after_authorized_result_correction(
    tmp_path: Path,
) -> None:
    client, credential, store, field, reactions, _repository = _runtime(tmp_path)
    receipt_id, issue_batch_id = _assemble_approve_and_issue(
        client, credential, store, field, key="correction-retry"
    )
    body = _settlement_body(store, field, receipt_id, issue_batch_id)
    original = client.post(
        "/v3/results/settle",
        headers=_headers(credential, "original-settlement"),
        json=body,
    )
    assert original.status_code == 200, original.text

    competitor_id = str(field.ordered_assignments[0].competitor_id)
    result_key = deterministic_identifier(
        "result",
        {
            "field_id": str(field.field_id),
            "field_revision": field.field_revision,
            "competitor_id": competitor_id,
        },
    )
    with open_v3_connection(store.database_path, read_only=True) as connection:
        observed = connection.execute(
            "SELECT observation_json FROM v3_result_revisions WHERE result_key=? "
            "ORDER BY revision DESC LIMIT 1",
            (str(result_key),),
        ).fetchone()
    assert observed is not None
    previous = ResultObservation.from_dict(json.loads(str(observed[0])))
    corrected = LiveResultSubmission(
        StableIdentifier("evidence:authorized-correction"),
        previous.competitor_id,
        previous.tournament_id,
        previous.round_id,
        previous.field_id,
        previous.context,
        "2026-08-25T18:00:04.000Z",
        previous.issued_mark,
        (previous.completion_clock_ms or 0) + 500,
        previous.placing,
        (previous.gap_ms or 0) + 500,
        OfficialResult(
            ResultStatus.COMPLETION,
            (previous.result.raw_time_ms or 0) + 500,
            None,
            2,
            1,
        ),
        canonical_digest({"authorized_correction": str(result_key)}),
    )
    LifecycleService(store.database_path).record_live_result(
        corrected,
        field_revision=field.field_revision,
        claimed_receipt_id=StableIdentifier(receipt_id),
        command_id=IdempotencyKey("command:authorized-correction"),
        actor_id=StableIdentifier("actor:tournament-manager"),
        occurred_at_utc="2026-08-25T18:00:04.000Z",
        monotonic_elapsed_ms=0,
    )

    retry = client.post(
        "/v3/results/settle",
        headers=_headers(credential, "original-settlement"),
        json=body,
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "recovered"
    assert retry.json()["settlement_id"] == original.json()["settlement_id"]
    assert reactions.calls == 2


def test_gateway_composition_requires_same_database_settlement_reactions(
    tmp_path: Path,
) -> None:
    _client, _credential, store, _field, _reactions, repository = _runtime(tmp_path)
    with pytest.raises((TypeError, ValueError), match="settlement reactions"):
        compose_v3_application_gateway(
            database_path=store.database_path,
            signer=store._signer,
            trust_store=store._trust_store,
            pipeline_builder=lambda _field: None,
            job_repository=repository,
            issue_coordinator=CriticalIssueCoordinator.for_rehearsal(
                CriticalJournal(
                    tmp_path / "missing-reactions-journal",
                    signer=store._signer,
                    trust_store=store._trust_store,
                )
            ),
            settlement_reactions=None,
            clock=lambda: "2026-08-25T18:00:00.000Z",
        )
