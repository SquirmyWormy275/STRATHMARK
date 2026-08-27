from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from strathmark.v3.api.app import create_v3_app  # noqa: E402
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
from strathmark.v3.composition import (  # noqa: E402
    V3ServiceIdentity,
    compose_v3_application_gateway,
)
from strathmark.v3.consumer_contract import v3_consumer_contract_digest  # noqa: E402
from strathmark.v3.contracts.canonical import canonical_digest  # noqa: E402
from strathmark.v3.contracts.events import CompetitionEngineSelection, EventKind  # noqa: E402
from strathmark.v3.contracts.evidence import (
    EvidencePacket,
    ResultObservation,
)  # noqa: E402
from strathmark.v3.contracts.identifiers import (  # noqa: E402
    IdempotencyKey,
    StableIdentifier,
    deterministic_identifier,
)
from strathmark.v3.contracts.statuses import (  # noqa: E402
    EngineExecutionMode,
    OfficialResult,
    PredictionEngine,
    ResultStatus,
)
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


def _pre_field_payload(field) -> dict[str, object]:
    return {
        "schema_version": "strathmark-v3-pre-field-forecast-request-v1",
        "tournament_id": str(field.tournament_id),
        "round_id": str(field.round_id),
        "forecast_set_revision": 1,
        "ordered_competitor_ids": [str(item.competitor_id) for item in field.ordered_assignments],
        "target_context": field.target_context.to_dict(),
        "hard_deadline_at": field.deadline_at,
        "requested_at_utc": NOW,
        "deadline_ms": 10_000,
    }


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
    bind_selection: bool = True,
    service_source_commit: str | None = "c468e2f59eb42ba1affe0f1669c7a4fb57570d6f",
    persist_field_snapshot: bool = True,
    schedule_cards: bool = True,
):
    database = tmp_path / "runtime.sqlite3"
    store, field, build, _lifecycle = _bootstrap(
        database,
        engine_selection=(
            CompetitionEngineSelection(
                scope_id=StableIdentifier("tournament:show"),
                engine=PredictionEngine.V3,
                mode=EngineExecutionMode.REHEARSAL,
                selected_by_actor_id=StableIdentifier("actor:manager"),
                selected_at_utc=NOW,
                reason_code="runtime_contract_proof",
                consumer_contract_digest=v3_consumer_contract_digest(),
                source_commit="c468e2f59eb42ba1affe0f1669c7a4fb57570d6f",
            )
            if bind_selection
            else None
        ),
        persist_field_snapshot=persist_field_snapshot,
    )
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
    scheduled = (
        coordinator.schedule(
            candidates,
            capacity_use=CapacityUse(1, 2, 2, 2, 2, 1_024, 4_096, 25),
            council_manifest_digest=council.body_digest,
            observed_at=NOW,
        )
        if schedule_cards
        else ()
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
    for candidate in candidates if schedule_cards else ():
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
        service_identity=(
            None
            if service_source_commit is None
            else V3ServiceIdentity.from_installed_contract(source_commit=service_source_commit)
        ),
    )
    if verified_cutover is not None:
        gateway = V3ApplicationGateway(
            gateway._services,
            clock=lambda: "2026-08-25T18:00:00.000Z",
            verified_cutover=lambda: verified_cutover,
            service_identity=gateway._service_identity,
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


def test_pre_field_forecast_reuses_cards_without_any_field_snapshot(tmp_path: Path) -> None:
    client, credential, _store, field, _reactions, repository = _runtime(
        tmp_path,
        persist_field_snapshot=False,
    )
    with open_v3_connection(repository.database_path, read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM v3_ingress_snapshots WHERE entity_kind='field'"
            ).fetchone()[0]
            == 0
        )

    response = client.post(
        "/v3/forecasts/pre-field",
        headers=_headers(credential, "forecast-no-field"),
        json=_pre_field_payload(field),
    )

    assert response.status_code == 200, response.text
    receipt = json.loads(response.json()["canonical_receipt_json"])
    assert receipt["purpose"] == "pre_field_seeding_only"
    assert receipt["issued_mark"] is False
    assert "field_id" not in response.request.content.decode("utf-8")
    assert all("mark" not in item for item in receipt["forecasts"])


def test_pre_field_forecast_schedules_five_jobs_each_without_a_field(tmp_path: Path) -> None:
    client, credential, _store, field, _reactions, repository = _runtime(
        tmp_path,
        persist_field_snapshot=False,
        schedule_cards=False,
    )

    response = client.post(
        "/v3/forecasts/pre-field",
        headers=_headers(credential, "forecast-schedule-no-field"),
        json=_pre_field_payload(field),
    )

    assert response.status_code == 409, response.text
    with open_v3_connection(repository.database_path, read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM v3_ingress_snapshots WHERE entity_kind='field'"
            ).fetchone()[0]
            == 0
        )
        jobs = connection.execute("SELECT payload_json FROM v3_jobs ORDER BY job_id").fetchall()
    assert len(jobs) == len(field.ordered_assignments) * 5
    assert {json.loads(str(row[0]))["component_id"] for row in jobs} == {
        "formula",
        "ml",
        "local_qwen35_9b",
        "local_ministral3_8b",
        "frontier_cloud",
    }
    assert all("field_id" not in json.loads(str(row[0])) for row in jobs)


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
    assert command.status_code == 404, command.text
    assert command.json()["code"] == "route_not_found"
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

    forecast = client.post(
        "/v3/forecasts/pre-field",
        headers=_headers(credential, "forecast-before-field"),
        json={
            "schema_version": "strathmark-v3-pre-field-forecast-request-v1",
            "tournament_id": str(field.tournament_id),
            "round_id": str(field.round_id),
            "forecast_set_revision": 1,
            "ordered_competitor_ids": [
                str(item.competitor_id) for item in field.ordered_assignments
            ],
            "target_context": field.target_context.to_dict(),
            "hard_deadline_at": field.deadline_at,
            "requested_at_utc": NOW,
            "deadline_ms": 10_000,
        },
    )
    assert forecast.status_code == 200, forecast.text
    forecast_receipt = json.loads(forecast.json()["canonical_receipt_json"])
    assert forecast_receipt["purpose"] == "pre_field_seeding_only"
    assert forecast_receipt["issued_mark"] is False
    assert [item["competitor_id"] for item in forecast_receipt["forecasts"]] == [
        str(item.competitor_id) for item in field.ordered_assignments
    ]
    assert all("mark" not in item for item in forecast_receipt["forecasts"])
    retry = client.post(
        "/v3/forecasts/pre-field",
        headers=_headers(credential, "forecast-before-field"),
        json={
            "schema_version": "strathmark-v3-pre-field-forecast-request-v1",
            "tournament_id": str(field.tournament_id),
            "round_id": str(field.round_id),
            "forecast_set_revision": 1,
            "ordered_competitor_ids": [
                str(item.competitor_id) for item in field.ordered_assignments
            ],
            "target_context": field.target_context.to_dict(),
            "hard_deadline_at": field.deadline_at,
            "requested_at_utc": NOW,
            "deadline_ms": 10_000,
        },
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["disposition"] == "recovered"
    assert retry.json()["receipt_digest"] == forecast.json()["receipt_digest"]

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
    receipt_value = json.loads(assembled.json()["canonical_receipt_json"])
    assert receipt_value["engine_authority"] == {
        "scope_id": "tournament:show",
        "engine": "v3",
        "mode": "rehearsal",
        "selection_digest": CompetitionEngineSelection(
            scope_id=StableIdentifier("tournament:show"),
            engine=PredictionEngine.V3,
            mode=EngineExecutionMode.REHEARSAL,
            selected_by_actor_id=StableIdentifier("actor:manager"),
            selected_at_utc=NOW,
            reason_code="runtime_contract_proof",
            consumer_contract_digest=v3_consumer_contract_digest(),
            source_commit="c468e2f59eb42ba1affe0f1669c7a4fb57570d6f",
        ).selection_digest,
        "consumer_contract_digest": v3_consumer_contract_digest(),
        "source_commit": "c468e2f59eb42ba1affe0f1669c7a4fb57570d6f",
    }
    receipt_id = assembled.json()["receipt_id"]
    assert assembled.json()["disposition"] == "prepared"

    page = store.approval_page(tournament_id=str(field.tournament_id), offset=0, limit=10)
    row = page.rows[0]
    if row.decision_state.value == "undecided":
        approval_body = {
            "schema_version": "strathmark-v3-approval-decision-request-v1",
            "tournament_id": str(field.tournament_id),
            "snapshot_id": page.snapshot_id,
            "action": "individual_accept",
            "selected": [
                {
                    "field_id": row.field_id,
                    "receipt_id": row.receipt_id,
                    "receipt_digest": assembled.json()["receipt_digest"],
                    "receipt_revision": row.receipt_revision,
                    "upstream_field_revision": row.upstream_field_revision,
                    "row_digest": row.row_digest,
                    "call_order": row.call_order,
                }
            ],
            "excluded": [],
            "actor_metadata": {"station": "runtime-proof"},
            "reason_code": "judge-reviewed-runtime-sheet",
            "superseded_receipt_id": None,
            "decided_at_utc": "2026-08-25T18:00:01.000Z",
            "deadline_ms": 10_000,
        }
        approval_headers = {
            **_headers(credential, "runtime-approve"),
            "X-STRATHMARK-Upstream-Actor": "actor:judge",
            "X-STRATHMARK-Upstream-Action": "approve_field",
            "X-STRATHMARK-Upstream-Trace": "trace:runtime-approve",
        }
        prior_event_count = SQLiteEventStore(reactions.database_path).event_count()
        approved = client.post("/v3/approvals/decide", headers=approval_headers, json=approval_body)
        retried = client.post(
            "/v3/approvals/decide",
            headers=approval_headers,
            json=approval_body,
        )
        assert approved.status_code == retried.status_code == 200, approved.text
        assert approved.json() == retried.json()
        assert approved.json()["decisions"] == [
            {"receipt_id": receipt_id, "decision_state": "accepted"}
        ]
        event_count = SQLiteEventStore(reactions.database_path).event_count()
        assert event_count == prior_event_count + 1
        changed_retry = client.post(
            "/v3/approvals/decide",
            headers=approval_headers,
            json={**approval_body, "actor_metadata": {"station": "changed"}},
        )
        assert changed_retry.status_code == 409
        assert changed_retry.json()["code"] == "approval_decision_conflicts"
        assert SQLiteEventStore(reactions.database_path).event_count() == event_count
        tampered = client.post(
            "/v3/approvals/decide",
            headers=_headers(credential, "runtime-approve-tampered"),
            json={
                **approval_body,
                "selected": [{**approval_body["selected"][0], "receipt_digest": "0" * 64}],
            },
        )
        assert tampered.status_code == 409
        assert tampered.json()["code"] == "approval_receipt_digest_differs"
        assert SQLiteEventStore(reactions.database_path).event_count() == event_count
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


def test_public_field_assembly_rejects_an_unselected_legacy_scope(tmp_path: Path) -> None:
    client, credential, _store, field, _reactions, _repository = _runtime(
        tmp_path, bind_selection=False
    )
    response = client.post(
        "/v3/fields/assemble",
        headers=_headers(credential, "assemble-unselected"),
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
    assert response.status_code == 409
    assert response.json()["code"] == "field_scope_has_no_engine_authority"


def test_status_and_numeric_work_fail_closed_without_exact_service_identity(
    tmp_path: Path,
) -> None:
    client, credential, _store, field, _reactions, _repository = _runtime(
        tmp_path, service_source_commit=None
    )
    status = client.get("/v3/status", headers=_headers(credential, "status-missing")).json()
    assert status["v3_option_state"] == "ineligible"
    assert status["source_commit"] is None
    assert status["eligibility_reason_codes"] == ["service_identity_unavailable"]

    response = client.post(
        "/v3/fields/assemble",
        headers=_headers(credential, "assemble-missing-identity"),
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
    assert response.status_code == 409
    assert response.json()["code"] == "service_identity_unavailable"


def test_numeric_work_rejects_scope_pinned_to_different_service_source(tmp_path: Path) -> None:
    client, credential, _store, field, _reactions, _repository = _runtime(
        tmp_path, service_source_commit="d" * 40
    )
    response = client.post(
        "/v3/fields/assemble",
        headers=_headers(credential, "assemble-source-mismatch"),
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
    assert response.status_code == 409
    assert response.json()["code"] == "scope_service_identity_mismatch"


@pytest.mark.parametrize(
    ("service_source", "selection_source", "selection_contract"),
    (
        (
            "d" * 40,
            "c468e2f59eb42ba1affe0f1669c7a4fb57570d6f",
            v3_consumer_contract_digest(),
        ),
        (
            "c468e2f59eb42ba1affe0f1669c7a4fb57570d6f",
            "c468e2f59eb42ba1affe0f1669c7a4fb57570d6f",
            "0" * 64,
        ),
    ),
)
def test_scope_open_rejects_a_selection_pinned_to_different_service_identity(
    tmp_path: Path,
    service_source: str,
    selection_source: str,
    selection_contract: str,
) -> None:
    client, credential, _store, _field, _reactions, _repository = _runtime(
        tmp_path, service_source_commit=service_source
    )
    response = client.post(
        "/v3/scopes/open",
        headers=_headers(credential, "open-source-mismatch"),
        json={
            "schema_version": "strathmark-v3-scope-open-request-v1",
            "scope_id": "tournament:different-show",
            "bundle_id": "bundle:current",
            "historical_cutoff_key": "history:before-show",
            "root_round_ids": ["round:different-root"],
            "engine_selection": {
                "schema_version": "strathmark-v3-competition-engine-selection-v1",
                "scope_id": "tournament:different-show",
                "engine": "v3",
                "mode": "rehearsal",
                "selected_by_actor_id": "actor:judge",
                "selected_at_utc": "2026-08-25T17:59:00.000Z",
                "reason_code": "new_competition",
                "consumer_contract_digest": selection_contract,
                "source_commit": selection_source,
            },
            "opened_at_utc": "2026-08-25T18:00:00.000Z",
            "deadline_ms": 1_000,
        },
    )
    assert response.status_code == 409
    assert response.json()["code"] == "scope_service_identity_mismatch"


def test_service_identity_rejects_malformed_or_unverified_installation_evidence() -> None:
    with pytest.raises(ValueError, match="source commit"):
        V3ServiceIdentity.from_installed_contract(source_commit="not-a-commit")
    with pytest.raises(ValueError, match="installed evidence"):
        V3ServiceIdentity(
            source_commit="d" * 40,
            consumer_contract_version="strathmark.v3-consumer-contract.v6",
            consumer_contract_digest="0" * 64,
        )


def test_concrete_gateway_exposes_scope_snapshot_freeze_and_close_lifecycle(
    tmp_path: Path,
) -> None:
    client, credential, *_rest = _runtime(tmp_path)
    scope_id = "tournament:public-lifecycle"
    round_id = "round:public-root"
    selection = {
        "schema_version": "strathmark-v3-competition-engine-selection-v1",
        "scope_id": scope_id,
        "engine": "v3",
        "mode": "rehearsal",
        "selected_by_actor_id": "actor:judge-seven",
        "selected_at_utc": "2026-08-25T17:59:54.000Z",
        "reason_code": "new_competition",
        "consumer_contract_digest": v3_consumer_contract_digest(),
        "source_commit": "c468e2f59eb42ba1affe0f1669c7a4fb57570d6f",
    }
    common = {
        "upstream_revision": 1,
        "tournament_id": scope_id,
        "synchronized_at_utc": "2026-08-25T17:59:55.000Z",
        "deadline_ms": 10_000,
    }
    snapshots = (
        {
            **common,
            "schema_version": "strathmark-v3-snapshot-sync-request-v1",
            "entity_kind": "tournament",
            "entity_id": scope_id,
            "round_id": None,
            "engine_selection": selection,
            "snapshot": {
                "bundle_id": "bundle:public-current",
                "historical_cutoff_key": "history:public-cutoff",
            },
        },
        {
            **common,
            "schema_version": "strathmark-v3-snapshot-sync-request-v1",
            "entity_kind": "round",
            "entity_id": round_id,
            "round_id": round_id,
            "engine_selection": selection,
            "snapshot": {
                "round_ordinal": 1,
                "predecessor_round_ids": [],
                "successor_round_ids": [],
            },
        },
    )
    unbound = {key: value for key, value in snapshots[0].items() if key != "engine_selection"}
    rejected = client.post(
        "/v3/snapshots/synchronize",
        headers=_headers(credential, "public-unbound-snapshot"),
        json=unbound,
    )
    assert rejected.status_code == 422
    with open_v3_connection(_rest[2].database_path, read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM v3_ingress_snapshots WHERE entity_id=?", (scope_id,)
            ).fetchone()
            is None
        )
    for index, payload in enumerate(snapshots):
        response = client.post(
            "/v3/snapshots/synchronize",
            headers=_headers(credential, f"public-snapshot-{index}"),
            json=payload,
        )
        assert response.status_code == 200, response.text

    open_payload = {
        "schema_version": "strathmark-v3-scope-open-request-v1",
        "scope_id": scope_id,
        "bundle_id": "bundle:public-current",
        "historical_cutoff_key": "history:public-cutoff",
        "root_round_ids": [round_id],
        "engine_selection": selection,
        "opened_at_utc": "2026-08-25T17:59:58.000Z",
        "deadline_ms": 10_000,
    }
    mismatched = client.post(
        "/v3/scopes/open",
        headers=_headers(credential, "public-open-mismatch"),
        json={
            **open_payload,
            "engine_selection": {**selection, "reason_code": "pre_lock_correction"},
        },
    )
    assert mismatched.status_code == 409
    with open_v3_connection(_rest[2].database_path, read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM v3_aggregate_heads "
                "WHERE aggregate_kind='tournament' AND aggregate_id=?",
                (scope_id,),
            ).fetchone()
            is None
        )
    opened = client.post(
        "/v3/scopes/open",
        headers=_headers(credential, "public-open"),
        json=open_payload,
    )
    assert opened.status_code == 200, opened.text
    opened_event = next(
        event
        for event in SQLiteEventStore(_rest[2].database_path).events()
        if event.kind is EventKind.TOURNAMENT_OPENED and str(event.aggregate_id) == scope_id
    )
    assert str(opened_event.command.actor_id) == "actor:tournament-manager"
    assert (
        opened_event.command.payload.to_value()["engine_selection"]["selected_by_actor_id"]
        == "actor:judge-seven"
    )
    frozen = client.post(
        "/v3/rounds/freeze",
        headers=_headers(credential, "public-freeze"),
        json={
            "schema_version": "strathmark-v3-round-freeze-request-v1",
            "round_id": round_id,
            "epoch_revision": 1,
            "historical_cutoff_key": "history:public-cutoff",
            "closure_ids": [],
            "frozen_at_utc": "2026-08-25T17:59:59.000Z",
            "deadline_ms": 10_000,
        },
    )
    assert frozen.status_code == 200, frozen.text
    page = client.get(
        f"/v3/approvals/page?tournament_id={scope_id}&offset=0&limit=25",
        headers={"Authorization": f"Bearer {credential}"},
    )
    assert page.status_code == 200, page.text
    assert page.json()["rows"] == []
    for path, key, body in (
        (
            "/v3/rounds/close",
            "public-close-round",
            {
                "schema_version": "strathmark-v3-round-close-request-v1",
                "round_id": round_id,
                "closed_at_utc": "2026-08-25T18:00:04.000Z",
                "deadline_ms": 10_000,
            },
        ),
        (
            "/v3/scopes/close",
            "public-close-scope",
            {
                "schema_version": "strathmark-v3-scope-close-request-v1",
                "scope_id": scope_id,
                "closed_at_utc": "2026-08-25T18:00:05.000Z",
                "deadline_ms": 10_000,
            },
        ),
    ):
        response = client.post(path, headers=_headers(credential, key), json=body)
        assert response.status_code == 200, response.text


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
    assert status["v3_option_state"] == "rehearsal_ready"
    assert status["rehearsal_eligible"] is True
    assert status["production_eligible"] is False
    assert status["source_commit"] == "c468e2f59eb42ba1affe0f1669c7a4fb57570d6f"
    assert status["consumer_contract_version"] == "strathmark.v3-consumer-contract.v6"
    assert status["consumer_contract_digest"] == v3_consumer_contract_digest()
    assert len(status["event_checkpoint_digest"]) == 64
    assert len(status["field_checkpoint_digest"]) == 64
    assert len(status["job_checkpoint_digest"]) == 64


def test_public_approval_page_and_detail_share_one_projection_identity(tmp_path: Path) -> None:
    client, credential, store, field, *_rest = _runtime(tmp_path)
    assembled = client.post(
        "/v3/fields/assemble",
        headers=_headers(credential, "public-approval-assemble"),
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
    page = client.get(
        f"/v3/approvals/page?tournament_id={field.tournament_id}&offset=0&limit=25",
        headers={"Authorization": f"Bearer {credential}"},
    )
    assert page.status_code == 200, page.text
    row = next(
        item for item in page.json()["rows"] if item["receipt_id"] == assembled.json()["receipt_id"]
    )
    detail = client.get(
        "/v3/approvals/detail"
        f"?tournament_id={field.tournament_id}&snapshot_id={page.json()['snapshot_id']}"
        f"&receipt_id={row['receipt_id']}",
        headers={"Authorization": f"Bearer {credential}"},
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["snapshot_id"] == page.json()["snapshot_id"]
    assert detail.json()["detail"]["row"] == row
    assert (
        store.approval_page(tournament_id=str(field.tournament_id), offset=0, limit=25).snapshot_id
        == page.json()["snapshot_id"]
    )


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

    wrong_rolling = _TrackingSettlementReactions(tmp_path / "wrong-rolling.sqlite3")
    with pytest.raises(ValueError, match="rolling reactions"):
        compose_v3_application_gateway(
            database_path=store.database_path,
            signer=store._signer,
            trust_store=store._trust_store,
            pipeline_builder=lambda _field: None,
            job_repository=repository,
            issue_coordinator=CriticalIssueCoordinator.for_rehearsal(
                CriticalJournal(
                    tmp_path / "wrong-rolling-journal",
                    signer=store._signer,
                    trust_store=store._trust_store,
                )
            ),
            settlement_reactions=_TrackingSettlementReactions(store.database_path),
            rolling_reactions=wrong_rolling,
            clock=lambda: "2026-08-25T18:00:00.000Z",
        )

    rolling = _TrackingSettlementReactions(store.database_path)
    gateway = compose_v3_application_gateway(
        database_path=store.database_path,
        signer=store._signer,
        trust_store=store._trust_store,
        pipeline_builder=lambda _field: None,
        job_repository=repository,
        issue_coordinator=CriticalIssueCoordinator.for_rehearsal(
            CriticalJournal(
                tmp_path / "rolling-wired-journal",
                signer=store._signer,
                trust_store=store._trust_store,
            )
        ),
        settlement_reactions=_TrackingSettlementReactions(store.database_path),
        rolling_reactions=rolling,
        clock=lambda: "2026-08-25T18:00:00.000Z",
    )
    assert gateway._services.lifecycle._reaction_port is rolling
