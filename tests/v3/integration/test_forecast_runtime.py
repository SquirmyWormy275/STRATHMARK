from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from threading import Event, Lock

import pytest

from strathmark.v3.application.capacity import (
    CapacityManifest,
    CapacityUse,
    JobLane,
    LaneCapacity,
)
from strathmark.v3.application.coordinator import (
    DurableRollingPreparationCoordinator,
    ExecutableCouncilSchedule,
    PreparationCandidate,
    PreparationClass,
    ProviderResponse,
    RollingLifecycleReactionPlan,
    RollingLifecycleReactionService,
)
from strathmark.v3.application.field_assembly import seal_competitor_card_authority
from strathmark.v3.application.forecast_runtime import (
    DurableForecastOutputStore,
    DurableForecastRuntime,
    FormulaForecastProvider,
    MLForecastProvider,
)
from strathmark.v3.application.formula_governor import (
    FormulaProjectionFactory,
    seal_formula_governor_batch,
)
from strathmark.v3.application.job_ports import DurableJobError, RetryPolicy
from strathmark.v3.assessors.formula import FormulaManifest
from strathmark.v3.assessors.llm_council import (
    DeadlineBudget,
    HMACTokenKey,
    PersistedLeaseAuthority,
    ProviderCallError,
    ProviderKind,
    RawAttempt,
    seal_claimed_llm_job,
    seal_member_weight_authority,
)
from strathmark.v3.assessors.ml import MLAssessment
from strathmark.v3.assessors.output_validation import ValidatedMemberOutput
from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.evidence import EvidencePacket, TargetContext
from strathmark.v3.contracts.forecasts import (
    ArtifactIdentity,
    AssessorForecast,
    AssessorKind,
    EvidenceSupport,
    ForecastState,
    LLMMemberAudit,
    PositiveTimeDistribution,
    QuantilePoint,
)
from strathmark.v3.contracts.identifiers import StableIdentifier
from strathmark.v3.domain.credibility import (
    MemberCredibilityEvidence,
    derive_member_subweights,
)
from strathmark.v3.domain.epochs import ReactionBarrier, freeze_epoch
from strathmark.v3.infrastructure.blobs import ContentAddressedBlobStore
from strathmark.v3.infrastructure.integrity import (
    IntegrityTrustStore,
    P256EphemeralSigner,
    sign_manifest,
)
from strathmark.v3.infrastructure.ollama import ContentAddressedRawOutputSink
from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection
from strathmark.v3.infrastructure.sqlite.jobs import DurableJobRepository, JobState

T0 = "2026-08-25T18:00:00.000Z"
T1 = "2026-08-25T18:00:01.000Z"
DEADLINE = "2026-08-25T18:05:00.000Z"


class _PublicationReactions:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.resolve()
        self.calls = 0

    def recover_pending(self) -> int:
        self.calls += 1
        return 0


def _capacity() -> CapacityManifest:
    return CapacityManifest(
        schema_version="strathmark-v3-job-capacity-v1",
        max_open_tournaments=1,
        max_round_entrants=48,
        max_field_entrants=12,
        max_plausible_qualifiers=48,
        max_context_cards=48,
        max_queued_jobs=16,
        max_receipt_bytes=1_048_576,
        max_blob_bytes=16_777_216,
        max_api_page_size=100,
        reserved_imminent_jobs=1,
        reserved_recovery_jobs=1,
        aging_interval_ms=1_000,
        aging_increment=125,
        lanes=(
            LaneCapacity(JobLane.HOT_FIELD, 2, 1),
            LaneCapacity(JobLane.INFERENCE, 12, 4),
            LaneCapacity(JobLane.LOOKUP_RECOVERY, 2, 1),
            LaneCapacity(JobLane.MAINTENANCE, 2, 1),
        ),
    )


def _packet_and_projection(signer: P256EphemeralSigner):
    context = TargetContext("underhand", 300, "pine", "taxonomy:v1", "conversion:v1")
    epoch = freeze_epoch(
        round_id=StableIdentifier("round:runtime"),
        epoch_revision=1,
        historical_cutoff_key="history:runtime",
        closed_through_sequence=0,
        members=(),
        barrier=ReactionBarrier.complete_through(0),
    )
    packet = EvidencePacket.create(
        competitor_id=StableIdentifier("competitor:runtime"),
        target_context=context,
        observations=(),
        taxonomy_version=context.taxonomy_version,
        conversion_version=context.conversion_version,
        historical_cutoff_key=epoch.historical_cutoff_key,
        tournament_epoch_id=epoch.epoch_id,
        tournament_event_sequence=epoch.maximum_tournament_sequence,
    )
    batch = seal_formula_governor_batch(
        evidence=packet,
        epoch=epoch,
        cutoff_at_utc=T0,
        active_tournament_id=StableIdentifier("tournament:runtime"),
        authoritative_tournament_ids=(),
        legacy_tournament_ids=(),
        live_authorities=(),
        historical_authorities=(),
        signer=signer,
        created_at=T0,
    )
    factory = FormulaProjectionFactory(
        trust_store=IntegrityTrustStore((signer.identity,)),
        cutoff_at_utc=T0,
        active_tournament_id=StableIdentifier("tournament:runtime"),
        authoritative_tournament_ids=(),
        legacy_tournament_ids=(),
    )
    return packet, lambda evidence: factory.project(
        evidence=evidence, epoch=epoch, sealed_batch=batch
    )


class _ML:
    def __init__(self, bundle_digest: str) -> None:
        self.bundle_digest = bundle_digest
        self.calls = 0

    def assess(self, packet: EvidencePacket) -> MLAssessment:
        self.calls += 1
        distribution = PositiveTimeDistribution(
            (
                QuantilePoint("0.1", 38_000),
                QuantilePoint("0.5", 40_000),
                QuantilePoint("0.9", 42_000),
            )
        )
        forecast = AssessorForecast.create(
            forecast_id=StableIdentifier("forecast:runtime-ml"),
            assessor=AssessorKind.ML,
            state=ForecastState.COMMITTED,
            evidence_digest=packet.content_digest,
            distribution=distribution,
            support=EvidenceSupport(
                0, "0", 0, packet.historical_cutoff_key, packet.tournament_event_sequence
            ),
            warnings=(),
            artifacts=(ArtifactIdentity("ml_bundle", "ml-bundle:v1", self.bundle_digest),),
            abstention_code=None,
        )
        return MLAssessment.create(
            forecast=forecast,
            specialist_key=None,
            specialist_weight=0.0,
            universal_quantiles_ms=(38_000, 40_000, 42_000),
            specialist_quantiles_ms=(),
            unseen_categories=(),
            bundle_digest=self.bundle_digest,
        )


def _council_manifest(
    signer: P256EphemeralSigner,
    bundle_digest: str,
    members: list[dict[str, str]] | None = None,
):
    return sign_manifest(
        "rolling_council_roster_authority",
        {
            "schema_version": "strathmark-v3-rolling-council-roster-v1",
            "purpose": "rolling_card_council",
            "bundle_digest": bundle_digest,
            "members": members
            or [
                {
                    "member_id": "local_qwen35_9b",
                    "provider_kind": "local",
                    "family": "qwen3.5",
                    "member_manifest_digest": "1" * 64,
                },
                {
                    "member_id": "local_ministral3_8b",
                    "provider_kind": "local",
                    "family": "ministral3",
                    "member_manifest_digest": "2" * 64,
                },
                {
                    "member_id": "frontier_cloud",
                    "provider_kind": "cloud",
                    "family": "frontier",
                    "member_manifest_digest": "3" * 64,
                },
            ],
        },
        signer=signer,
        created_at=T0,
    )


def _system(
    tmp_path: Path,
    *,
    bad_projection: bool = False,
    bundle_digest: str = "b" * 64,
    council_members: list[dict[str, str]] | None = None,
    promoted=None,
    token_key: HMACTokenKey | None = None,
    member_deadlines: dict[str, DeadlineBudget] | None = None,
):
    signer = P256EphemeralSigner.generate("integrity-key:forecast-runtime")
    trust = IntegrityTrustStore((signer.identity,))
    database = tmp_path / "runtime.sqlite3"
    repository = DurableJobRepository(
        database, capacity=_capacity(), signer=signer, trust_store=trust
    )
    rolling = DurableRollingPreparationCoordinator(repository, signer=signer, trust_store=trust)
    council = _council_manifest(signer, bundle_digest, council_members)
    rolling.install_council_authority(council, installed_at=T0)
    packet, projection = _packet_and_projection(signer)
    if bad_projection:

        def invalid_projection(_evidence: EvidencePacket) -> object:
            return object()

        projection = invalid_projection  # type: ignore[assignment]
    candidate = PreparationCandidate.create(
        competitor_id=str(packet.competitor_id),
        target_context_digest=packet.target_context.digest,
        historical_cutoff_key=packet.historical_cutoff_key,
        tournament_epoch_id=str(packet.tournament_epoch_id),
        bundle_digest=bundle_digest,
        evidence_digest=packet.content_digest,
        dependency_revision=1,
        preparation_class=PreparationClass.IMMINENT_FIELD,
        hard_deadline_at=DEADLINE,
        evidence_packet=packet,
    )
    schedule_arguments = {
        "capacity_use": CapacityUse(1, 1, 1, 1, 1, 100_000, 100_000, 10),
        "council_manifest_digest": council.body_digest,
        "observed_at": T0,
    }
    if promoted is None:
        rolling.schedule((candidate,), **schedule_arguments)
    else:
        assert token_key is not None and member_deadlines is not None
        rolling.schedule_executable(
            (candidate,),
            promoted_council_authority=promoted,
            token_key=token_key,
            member_deadlines=member_deadlines,
            **schedule_arguments,
        )
    sink = ContentAddressedRawOutputSink(ContentAddressedBlobStore(tmp_path / "forecast-blobs"))
    outputs = DurableForecastOutputStore(sink)
    ml = _ML(bundle_digest)
    runtime = DurableForecastRuntime(
        repository,
        rolling,
        formula_provider=FormulaForecastProvider(
            projection=projection,  # type: ignore[arg-type]
            manifest=FormulaManifest.load("benchmarks/v3/formula_manifest.json"),
            output_store=outputs,
        ),
        ml_provider=MLForecastProvider(assessor=ml, output_store=outputs),
        output_store=outputs,
        signer=signer,
        trust_store=trust,
        retry_policy=RetryPolicy("rolling-card-v1"),
        publication_reactions=_PublicationReactions(repository.database_path),
    )
    return runtime, repository, rolling, signer, trust, candidate, ml, council, projection


def _member_weight_authority(signer, candidate, authority):
    receipt = derive_member_subweights(
        members=tuple(
            MemberCredibilityEvidence(
                member.member_id,
                str(index + 1),
                str(index + 1),
                "24",
                "1",
            )
            for index, member in enumerate(
                sorted(authority.members, key=lambda item: item.member_id)
            )
        ),
        council_outer_weight="0.3333333333333333",
        credibility_ledger_digest="8" * 64,
        credibility_policy_digest="9" * 64,
        context_digest=candidate.key.evidence_digest,
        calibration_cutoff_at_utc=T0,
    )
    return seal_member_weight_authority(
        receipt,
        member_ids=tuple(item.member_id for item in receipt.members),
        evidence_digest=candidate.key.evidence_digest,
        bundle_digest=candidate.key.bundle_digest,
        council_component_digest=authority.component_digest,
        signer=signer,
        created_at=T0,
    )


def _promoted_system(tmp_path: Path):
    from strathmark.v3.assessors import llm_council as council_module
    from strathmark.v3.assessors.llm_council import load_promoted_council
    from tests.v3.system.test_operational_llm_council import (
        _council_candidate,
        _members,
    )
    from tests.v3.system.test_promotion_rollback import (
        _register_evaluate_promote,
        _report,
        _service,
    )

    members = _members()
    factory_candidate = _council_candidate(members)
    service, repository, bundle_signer, evaluator_signer, _database = _service(tmp_path / "factory")
    report = _report(
        tmp_path / "factory", factory_candidate, evaluator_signer, generation="runtime-live"
    )
    installed, _receipt = _register_evaluate_promote(
        service,
        repository,
        factory_candidate,
        report,
        bundle_signer,
        key="runtime-live",
    )
    authority = load_promoted_council(service, StableIdentifier("tournament:runtime-live"), members)
    roster = [
        {
            "member_id": member.member_id,
            "provider_kind": member.provider_kind.value,
            "family": member.family,
            "member_manifest_digest": council_module._member_manifest_digest(member),
        }
        for member in authority.members
    ]
    deadlines = {
        member.member_id: DeadlineBudget(30_000, 10_000, 10_000, 5_000, 30_000)
        for member in authority.members
    }
    system = _system(
        tmp_path / "rolling",
        bundle_digest=installed.bundle_digest,
        council_members=roster,
        promoted=authority,
        token_key=HMACTokenKey("key:runtime-live", b"r" * 32),
        member_deadlines=deadlines,
    )
    return (*system, authority, deadlines)


class _RuntimeAdapter:
    def __init__(
        self,
        member,
        repository: DurableJobRepository,
        output_store: DurableForecastOutputStore,
        *,
        center: int,
        cloud_started: Event | None = None,
        cloud_release: Event | None = None,
        local_lock: Lock | None = None,
        local_counts: dict[str, int] | None = None,
        fail: bool = False,
        large_raw: bool = False,
    ) -> None:
        self.member = member
        self.lease_authority = PersistedLeaseAuthority(repository)
        self._outputs = output_store
        self._center = center
        self._cloud_started = cloud_started
        self._cloud_release = cloud_release
        self._local_lock = local_lock
        self._local_counts = local_counts
        self._fail = fail
        self._large_raw = large_raw
        self.calls = 0

    def execute(self, job) -> ProviderResponse:
        from strathmark.v3.assessors import llm_council as council_module
        from strathmark.v3.assessors.llm_council import ExecutedMember

        sealed = seal_claimed_llm_job(job, self.member, self.lease_authority)
        self.calls += 1
        if self.member.provider_kind is ProviderKind.CLOUD:
            assert self._cloud_started is not None and self._cloud_release is not None
            self._cloud_started.set()
            assert self._cloud_release.wait(5), "cloud did not overlap local execution"
        else:
            assert self._cloud_started is not None and self._cloud_started.wait(5)
            assert self._local_lock is not None and self._local_counts is not None
            with self._local_lock:
                self._local_counts["active"] += 1
                self._local_counts["maximum"] = max(
                    self._local_counts["maximum"], self._local_counts["active"]
                )
            try:
                if self._cloud_release is not None:
                    self._cloud_release.set()
            finally:
                with self._local_lock:
                    self._local_counts["active"] -= 1
        raw = (
            (b"{" + b'"padding":"' + (b"x" * 70_000) + b'"}')
            if self._large_raw
            else b'{"provider":"runtime-test"}'
        )
        _raw, reference = self._outputs.retain_bytes(raw)
        attempt = RawAttempt(reference.raw_digest, "valid_committed", True)
        if self._fail:
            raise ProviderCallError(
                "invalid_output_after_correction",
                attempts=(replace(attempt, validator_code="schema_fields", valid=False),),
                storage_references=(reference,),
            )
        distribution = PositiveTimeDistribution(
            (
                QuantilePoint("0.1", self._center - 2_000),
                QuantilePoint("0.5", self._center),
                QuantilePoint("0.9", self._center + 2_000),
            )
        )
        validated = ValidatedMemberOutput(True, "valid_committed", distribution, (), (), (), None)
        audit = LLMMemberAudit(
            prompt_digest=canonical_digest({"prompt": sealed.payload_digest}),
            schema_version="strathmark-v3-llm-output-v1",
            runtime_version=self.member.runtime_version,
            model_digest=self.member.model_digest,
            quantization=self.member.quantization,
            sampling_parameters_digest=self.member.sampling_parameters_digest,
            raw_response_digest=reference.raw_digest,
            validator_code="valid_committed",
            latency_ms=1,
            provider_model_version=self.member.model_id,
            provider_fingerprint=self.member.model_digest,
            api_revision=self.member.runtime_version,
            canary_digest=self.member.runtime_digest,
        )
        executed = ExecutedMember(
            self.member,
            validated,
            (attempt,),
            audit,
            (reference,),
            council_module._provider_execution_audit(
                self.member, "succeeded", None, (attempt,), (reference,)
            ),
        )
        assert executed.execution_audit is not None
        return ProviderResponse(
            reference.raw_digest,
            job.evidence_digest,
            job.bundle_digest,
            executed,
            executed.execution_audit,
        )


def _runtime_adapters(
    runtime: DurableForecastRuntime,
    repository: DurableJobRepository,
    authority,
    *,
    failing_member: str | None = None,
    large_member: str | None = None,
):
    started = Event()
    release = Event()
    lock = Lock()
    counts = {"active": 0, "maximum": 0}
    adapters = {
        member.member_id: _RuntimeAdapter(
            member,
            repository,
            runtime._outputs,
            center=40_000 + index * 1_000,
            cloud_started=started,
            cloud_release=release,
            local_lock=lock,
            local_counts=counts,
            fail=member.member_id == failing_member,
            large_raw=member.member_id == large_member,
        )
        for index, member in enumerate(authority.members)
    }
    return adapters, counts


def test_numeric_workers_persist_exact_outputs_and_restart_without_reexecution(
    tmp_path: Path,
) -> None:
    runtime, repository, _rolling, signer, trust, candidate, ml, _council, projection = _system(
        tmp_path
    )
    numeric = runtime.prepare_numeric(
        candidate.key,
        worker_id="worker:runtime",
        lease_duration_ms=30_000,
        clock=lambda: T1,
    )
    assert numeric.formula.state is ForecastState.COMMITTED
    assert numeric.ml.state is ForecastState.COMMITTED
    assert ml.calls == 1
    for ordinal in (1, 2):
        record = repository.records_for_card(candidate.key.card_digest)[ordinal - 1]
        assert record.state is JobState.SUCCEEDED
        assert record.result_digest in {
            numeric.formula.commit_digest,
            numeric.ml.commit_digest,
        }
        audit = repository.provider_execution(
            record.job_id, record.job_revision, record.fencing_token
        )
        assert audit.attempts[0].accepted
        assert audit.attempts[0].storage_reference.raw_digest == audit.attempts[0].raw_digest

    restarted_repository = DurableJobRepository(
        tmp_path / "runtime.sqlite3",
        capacity=_capacity(),
        signer=signer,
        trust_store=trust,
    )
    restarted_rolling = DurableRollingPreparationCoordinator(
        restarted_repository, signer=signer, trust_store=trust
    )
    restarted_sink = ContentAddressedRawOutputSink(
        ContentAddressedBlobStore(tmp_path / "forecast-blobs")
    )
    restarted_outputs = DurableForecastOutputStore(restarted_sink)
    restarted_ml = _ML("b" * 64)
    restarted = DurableForecastRuntime(
        restarted_repository,
        restarted_rolling,
        formula_provider=FormulaForecastProvider(
            projection=projection,
            manifest=FormulaManifest.load("benchmarks/v3/formula_manifest.json"),
            output_store=restarted_outputs,
        ),
        ml_provider=MLForecastProvider(assessor=restarted_ml, output_store=restarted_outputs),
        output_store=restarted_outputs,
        signer=signer,
        trust_store=trust,
        retry_policy=RetryPolicy("rolling-card-v1"),
        publication_reactions=_PublicationReactions(restarted_repository.database_path),
    )
    assert (
        restarted.prepare_numeric(
            candidate.key,
            worker_id="worker:restart",
            lease_duration_ms=30_000,
            clock=lambda: T1,
        )
        == numeric
    )
    assert restarted_ml.calls == 0


def test_numeric_runtime_fails_closed_on_projection_drift_and_signed_output_tamper(
    tmp_path: Path,
) -> None:
    bad, bad_repository, *_rest = _system(tmp_path / "bad", bad_projection=True)
    with pytest.raises(DurableJobError, match="formula component did not succeed"):
        bad.prepare_numeric(
            _rest[3].key,
            worker_id="worker:bad",
            lease_duration_ms=30_000,
            clock=lambda: T1,
        )
    assert bad_repository.records_for_card(_rest[3].key.card_digest)[0].state is JobState.INVALID

    runtime, repository, _rolling, _signer, _trust, candidate, *_ = _system(tmp_path / "tamper")
    runtime.prepare_numeric(
        candidate.key,
        worker_id="worker:tamper",
        lease_duration_ms=30_000,
        clock=lambda: T1,
    )
    formula = repository.records_for_card(candidate.key.card_digest)[0]
    with open_v3_connection(repository.database_path) as connection:
        connection.execute("DROP TRIGGER v3_job_provider_storage_refs_no_update")
        connection.execute(
            "UPDATE v3_job_provider_storage_refs SET reference_json='{}' "
            "WHERE job_id=? AND job_revision=?",
            (formula.job_id, formula.job_revision),
        )
    with pytest.raises(DurableJobError):
        runtime.prepare_numeric(
            candidate.key,
            worker_id="worker:tamper-replay",
            lease_duration_ms=30_000,
            clock=lambda: T1,
        )


def test_exact_claim_does_not_lease_a_different_component(tmp_path: Path) -> None:
    _runtime, repository, _rolling, _signer, _trust, candidate, *_ = _system(tmp_path)
    records = repository.records_for_card(candidate.key.card_digest)
    ml = records[1]
    lease = repository.claim_exact(
        ml.job_id,
        ml.job_revision,
        worker_id="worker:exact",
        clock=lambda: T1,
        lease_duration_ms=30_000,
    )
    assert lease is not None and lease.job_id == ml.job_id
    assert repository.get(records[0].job_id, records[0].job_revision).state is JobState.QUEUED


def test_output_envelope_digest_is_not_merely_the_forecast_commit(tmp_path: Path) -> None:
    runtime, repository, _rolling, _signer, _trust, candidate, *_ = _system(tmp_path)
    numeric = runtime.prepare_numeric(
        candidate.key,
        worker_id="worker:digest",
        lease_duration_ms=30_000,
        clock=lambda: T1,
    )
    record = repository.records_for_card(candidate.key.card_digest)[0]
    audit = repository.provider_execution(record.job_id, record.job_revision, record.fencing_token)
    assert audit.attempts[0].raw_digest != numeric.formula.commit_digest
    assert audit.member_pin_digest == canonical_digest(
        {
            "schema_version": "strathmark-v3-numeric-provider-pin-v1",
            "assessor": "formula",
            "bundle_digest": candidate.key.bundle_digest,
            "artifact_digest": FormulaManifest.load("benchmarks/v3/formula_manifest.json").digest,
        }
    )


def test_promoted_council_and_durable_numeric_outputs_prepare_a_signed_card(
    tmp_path: Path,
) -> None:
    from strathmark.v3.assessors import llm_council as council_module
    from strathmark.v3.assessors.llm_council import load_promoted_council
    from tests.v3.evals.test_llm_semantics import _member
    from tests.v3.system.test_operational_llm_council import (
        _audit,
        _council_candidate,
        _members,
    )
    from tests.v3.system.test_promotion_rollback import (
        _register_evaluate_promote,
        _report,
        _service,
    )

    promoted_members = _members()
    factory_candidate = _council_candidate(promoted_members)
    service, factory_repository, bundle_signer, evaluator_signer, _factory_database = _service(
        tmp_path / "factory"
    )
    report = _report(
        tmp_path / "factory",
        factory_candidate,
        evaluator_signer,
        generation="runtime-card",
    )
    installed, _receipt = _register_evaluate_promote(
        service,
        factory_repository,
        factory_candidate,
        report,
        bundle_signer,
        key="runtime-card",
    )
    promoted = load_promoted_council(
        service,
        StableIdentifier("tournament:runtime-card"),
        promoted_members,
    )
    roster = [
        {
            "member_id": member.member_id,
            "provider_kind": member.provider_kind.value,
            "family": member.family,
            "member_manifest_digest": council_module._member_manifest_digest(member),
        }
        for member in promoted.members
    ]
    runtime, repository, _rolling, _signer, _trust, candidate, _ml, roster_manifest, _ = _system(
        tmp_path / "rolling",
        bundle_digest=installed.bundle_digest,
        council_members=roster,
    )
    numeric = runtime.prepare_numeric(
        candidate.key,
        worker_id="worker:card",
        lease_duration_ms=30_000,
        clock=lambda: T1,
    )
    outcomes = tuple(
        replace(
            _member(
                member.member_id,
                40_000 + index * 1_000,
                provider_kind=member.provider_kind,
                family=member.family,
            ),
            evidence_digest=candidate.key.evidence_digest,
            audit=_audit(member),
        )
        for index, member in enumerate(promoted.members)
    )
    council = council_module.aggregate_council(outcomes, authority=promoted)
    by_member = {item.member_id: item for item in council.outcomes}
    for record in repository.records_for_card(candidate.key.card_digest)[2:]:
        member_id = record.payload()["component_id"]
        outcome = by_member[member_id]
        assert outcome.audit is not None
        lease = repository.claim_exact(
            record.job_id,
            record.job_revision,
            worker_id=f"worker:{record.payload()['component_ordinal']}",
            clock=lambda: T1,
            lease_duration_ms=30_000,
        )
        assert lease is not None
        repository.commit_success(
            lease.job_id,
            lease.job_revision,
            worker_id=lease.lease_owner,
            fencing_token=lease.fencing_token,
            result_digest=outcome.audit.raw_response_digest,
            current_context=lambda _connection, _current: (
                candidate.key.evidence_digest,
                candidate.key.bundle_digest,
            ),
            clock=lambda: T1,
        )
    publication = runtime.assemble_and_seal(
        candidate.key,
        numeric=numeric,
        council=council,
        council_authority=promoted,
        council_manifest_digest=roster_manifest.body_digest,
        observed_at=T1,
    )
    assert runtime._publication_reactions.calls == 1  # type: ignore[attr-defined]
    assert publication.authority.forecasts[:2] == (numeric.formula, numeric.ml)
    assert publication.authority.forecasts[2].assessor is AssessorKind.LLM_COUNCIL
    assert publication.authority.bundle_digest == installed.bundle_digest
    assert publication.availability == (
        ("formula", "available"),
        ("ml", "available"),
        ("llm_council", "normal_3_of_3"),
    )


def test_executable_schedule_seals_exact_nested_member_payloads_and_rejects_tamper(
    tmp_path: Path,
) -> None:
    (
        _runtime,
        repository,
        _rolling,
        _signer,
        _trust,
        candidate,
        _ml,
        _roster,
        _projection,
        authority,
        _deadlines,
    ) = _promoted_system(tmp_path)
    jobs = repository.records_for_card(candidate.key.card_digest)[2:]
    by_id = {member.member_id: member for member in authority.members}
    assert all(
        set(job.payload())
        == {
            "schema_version",
            "card_key",
            "component_id",
            "component_ordinal",
            "member_manifest_digest",
            "council_manifest_digest",
            "evidence_packet",
            "llm_job_payload",
        }
        for job in jobs
    )
    cloud = next(
        job
        for job in jobs
        if by_id[job.payload()["component_id"]].provider_kind is ProviderKind.CLOUD
    )
    lease = repository.claim_exact(
        cloud.job_id,
        cloud.job_revision,
        worker_id="worker:tamper-envelope",
        clock=lambda: T1,
        lease_duration_ms=30_000,
    )
    assert lease is not None
    sealed = seal_claimed_llm_job(
        lease, by_id[lease.payload()["component_id"]], PersistedLeaseAuthority(repository)
    )
    assert sealed.evidence_digest == candidate.key.evidence_digest

    outer = lease.payload()
    outer["card_key"]["evidence_digest"] = "f" * 64
    forged_outer = replace(
        lease,
        payload_json=canonical_bytes(outer).decode("utf-8"),
        payload_digest=canonical_digest(outer),
    )
    with pytest.raises(ValueError, match="current|differs"):
        seal_claimed_llm_job(
            forged_outer,
            by_id[lease.payload()["component_id"]],
            PersistedLeaseAuthority(repository),
        )

    nested = lease.payload()
    nested["llm_job_payload"]["provider_packet"]["numeric_digest"] = "e" * 64
    forged_nested = replace(
        lease,
        payload_json=canonical_bytes(nested).decode("utf-8"),
        payload_digest=canonical_digest(nested),
    )
    with pytest.raises(ValueError, match="current|differs"):
        seal_claimed_llm_job(
            forged_nested,
            by_id[lease.payload()["component_id"]],
            PersistedLeaseAuthority(repository),
        )


def test_council_runtime_overlaps_cloud_serializes_gpu_and_restarts_without_calls(
    tmp_path: Path,
) -> None:
    (
        runtime,
        repository,
        _rolling,
        signer,
        trust,
        candidate,
        _ml,
        _roster,
        projection,
        authority,
        _deadlines,
    ) = _promoted_system(tmp_path)
    adapters, local_counts = _runtime_adapters(runtime, repository, authority)
    weight_authority = _member_weight_authority(signer, candidate, authority)
    spoofed = _member_weight_authority(
        P256EphemeralSigner.generate("integrity-key:spoofed-member-weights"),
        candidate,
        authority,
    )
    with pytest.raises(DurableJobError, match="member-weight authority"):
        runtime.prepare_council(
            candidate.key,
            authority=authority,
            adapters=adapters,
            member_weight_authority=spoofed,
            worker_id="worker:spoofed-weights",
            lease_duration_ms=30_000,
            clock=lambda: T1,
        )
    council = runtime.prepare_council(
        candidate.key,
        authority=authority,
        adapters=adapters,
        member_weight_authority=weight_authority,
        worker_id="worker:council",
        lease_duration_ms=30_000,
        clock=lambda: T1,
    )
    assert council.assessment.availability.value == "normal"
    assert sum(Decimal(value) for _member, value in council.assessment.member_weights) == (
        Decimal("0.3333333333333333")
    )
    assert council.member_weight_authority.raw_digest == canonical_digest(
        weight_authority.manifest.to_dict()
    )
    assert local_counts == {"active": 0, "maximum": 1}
    for record, reference in zip(
        repository.records_for_card(candidate.key.card_digest)[2:],
        (
            next(
                council.member_receipts[index]
                for index, member in enumerate(authority.members)
                if member.member_id == record.payload()["component_id"]
            )
            for record in repository.records_for_card(candidate.key.card_digest)[2:]
        ),
        strict=True,
    ):
        assert record.state is JobState.SUCCEEDED
        assert reference is not None and record.result_digest == reference.raw_digest
        audit = repository.provider_execution(
            record.job_id, record.job_revision, record.fencing_token
        )
        assert audit.attempts[0].validator_code == "valid_member_receipt"
        assert audit.attempts[0].raw_digest == record.result_digest

    restarted_repository = DurableJobRepository(
        repository.database_path,
        capacity=_capacity(),
        signer=signer,
        trust_store=trust,
    )
    restarted_rolling = DurableRollingPreparationCoordinator(
        restarted_repository, signer=signer, trust_store=trust
    )
    restarted_outputs = DurableForecastOutputStore(
        ContentAddressedRawOutputSink(
            ContentAddressedBlobStore(tmp_path / "rolling" / "forecast-blobs")
        )
    )
    restarted = DurableForecastRuntime(
        restarted_repository,
        restarted_rolling,
        formula_provider=FormulaForecastProvider(
            projection=projection,
            manifest=FormulaManifest.load("benchmarks/v3/formula_manifest.json"),
            output_store=restarted_outputs,
        ),
        ml_provider=MLForecastProvider(
            assessor=_ML(candidate.key.bundle_digest), output_store=restarted_outputs
        ),
        output_store=restarted_outputs,
        signer=signer,
        trust_store=trust,
        retry_policy=RetryPolicy("rolling-card-v1"),
        publication_reactions=_PublicationReactions(restarted_repository.database_path),
    )
    no_call_adapters, _counts = _runtime_adapters(restarted, restarted_repository, authority)
    replayed = restarted.prepare_council(
        candidate.key,
        authority=authority,
        adapters=no_call_adapters,
        member_weight_authority=weight_authority,
        worker_id="worker:restart",
        lease_duration_ms=30_000,
        clock=lambda: T1,
    )
    assert replayed == council
    assert sum(adapter.calls for adapter in no_call_adapters.values()) == 0


def test_live_rolling_reaction_uses_executable_promoted_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (
        _runtime,
        _repository,
        rolling,
        _signer,
        _trust,
        candidate,
        _ml,
        roster,
        _projection,
        authority,
        deadlines,
    ) = _promoted_system(tmp_path)
    token_key = HMACTokenKey("key:runtime-live", b"r" * 32)
    executable = ExecutableCouncilSchedule(
        authority,
        token_key,
        tuple((member.member_id, deadlines[member.member_id]) for member in authority.members),
    )
    plan = RollingLifecycleReactionPlan(
        (candidate,),
        CapacityUse(1, 1, 1, 1, 1, 100_000, 100_000, 10),
        roster.body_digest,
        (),
    )
    calls = []
    original = rolling.schedule_executable

    def observe(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(rolling, "schedule_executable", observe)
    reaction = object.__new__(RollingLifecycleReactionService)
    reaction._coordinator = rolling
    reaction._executable_council = executable

    reaction._schedule_timely((candidate,), plan=plan, observed_at=T1)

    assert len(calls) == 1
    assert calls[0][1]["promoted_council_authority"] is authority
    assert calls[0][1]["token_key"] is token_key
    assert calls[0][1]["member_deadlines"] == deadlines
    assert executable.authority_value()["component_digest"] == authority.component_digest


def test_council_runtime_fails_closed_on_raw_and_member_receipt_tamper(
    tmp_path: Path,
) -> None:
    first = _promoted_system(tmp_path / "raw")
    runtime, repository, *_middle, candidate, _ml, _roster, _projection, authority, _ = first
    large_member = authority.members[0].member_id
    adapters, _counts = _runtime_adapters(runtime, repository, authority, large_member=large_member)
    weight_authority = _member_weight_authority(runtime._signer, candidate, authority)
    council = runtime.prepare_council(
        candidate.key,
        authority=authority,
        adapters=adapters,
        member_weight_authority=weight_authority,
        worker_id="worker:raw-tamper",
        lease_duration_ms=30_000,
        clock=lambda: T1,
    )
    outcome = next(item for item in council.assessment.outcomes if item.member_id == large_member)
    blob = outcome.storage_references[0].blob_reference
    assert blob is not None
    ContentAddressedBlobStore(tmp_path / "raw" / "rolling" / "forecast-blobs").path_for(
        blob.digest
    ).write_bytes(b"tampered")
    with pytest.raises(DurableJobError, match="raw|output|blob"):
        runtime.prepare_council(
            candidate.key,
            authority=authority,
            adapters=adapters,
            member_weight_authority=weight_authority,
            worker_id="worker:raw-replay",
            lease_duration_ms=30_000,
            clock=lambda: T1,
        )

    second = _promoted_system(tmp_path / "receipt")
    runtime, repository, *_middle, candidate, _ml, _roster, _projection, authority, _ = second
    adapters, _counts = _runtime_adapters(runtime, repository, authority)
    weight_authority = _member_weight_authority(runtime._signer, candidate, authority)
    runtime.prepare_council(
        candidate.key,
        authority=authority,
        adapters=adapters,
        member_weight_authority=weight_authority,
        worker_id="worker:receipt-tamper",
        lease_duration_ms=30_000,
        clock=lambda: T1,
    )
    member_record = repository.records_for_card(candidate.key.card_digest)[2]
    with open_v3_connection(repository.database_path) as connection:
        connection.execute("DROP TRIGGER v3_job_provider_storage_refs_no_update")
        connection.execute(
            "UPDATE v3_job_provider_storage_refs SET reference_json='{}' "
            "WHERE job_id=? AND job_revision=?",
            (member_record.job_id, member_record.job_revision),
        )
    with pytest.raises(DurableJobError):
        runtime.prepare_council(
            candidate.key,
            authority=authority,
            adapters=adapters,
            member_weight_authority=weight_authority,
            worker_id="worker:receipt-replay",
            lease_duration_ms=30_000,
            clock=lambda: T1,
        )


def test_formula_ml_and_degraded_council_publish_one_signed_card(tmp_path: Path) -> None:
    (
        runtime,
        repository,
        _rolling,
        _signer,
        _trust,
        candidate,
        _ml,
        roster,
        _projection,
        authority,
        _deadlines,
    ) = _promoted_system(tmp_path)
    failed_member = authority.members[0].member_id
    adapters, local_counts = _runtime_adapters(
        runtime, repository, authority, failing_member=failed_member
    )
    weight_authority = _member_weight_authority(_signer, candidate, authority)
    numeric = runtime.prepare_numeric(
        candidate.key,
        worker_id="worker:numeric-card",
        lease_duration_ms=30_000,
        clock=lambda: T1,
    )
    council = runtime.prepare_council(
        candidate.key,
        authority=authority,
        adapters=adapters,
        member_weight_authority=weight_authority,
        worker_id="worker:degraded-card",
        lease_duration_ms=30_000,
        clock=lambda: T1,
    )
    assert council.assessment.availability.value == "degraded"
    assert council.assessment.valid_member_count == 2
    assert local_counts["maximum"] == 1
    failed = next(
        item
        for item in repository.records_for_card(candidate.key.card_digest)[2:]
        if item.payload()["component_id"] == failed_member
    )
    assert failed.state is JobState.INVALID and failed.result_digest is None

    publication = runtime.assemble_and_seal(
        candidate.key,
        numeric=numeric,
        council=council,
        council_authority=authority,
        council_manifest_digest=roster.body_digest,
        observed_at=T1,
    )
    assert runtime._publication_reactions.calls == 1  # type: ignore[attr-defined]
    assert publication.authority.forecasts[:2] == (numeric.formula, numeric.ml)
    assert publication.authority.forecasts[2].state is ForecastState.COMMITTED
    assert publication.availability == (
        ("formula", "available"),
        ("ml", "available"),
        ("llm_council", "degraded_2_of_3"),
    )


@pytest.mark.parametrize("valid_member_count", (0, 1), ids=("zero-of-three", "one-of-three"))
def test_unavailable_durable_council_publishes_explicit_restart_safe_abstention(
    tmp_path: Path, valid_member_count: int
) -> None:
    (
        runtime,
        repository,
        _rolling,
        signer,
        trust,
        candidate,
        _ml,
        roster,
        projection,
        authority,
        _deadlines,
    ) = _promoted_system(tmp_path)
    adapters, _local_counts = _runtime_adapters(runtime, repository, authority)
    valid_members = {item.member_id for item in authority.members[:valid_member_count]}
    for member_id, adapter in adapters.items():
        adapter._fail = member_id not in valid_members
    weight_authority = _member_weight_authority(signer, candidate, authority)
    numeric = runtime.prepare_numeric(
        candidate.key,
        worker_id="worker:unavailable-numeric",
        lease_duration_ms=30_000,
        clock=lambda: T1,
    )
    council = runtime.prepare_council(
        candidate.key,
        authority=authority,
        adapters=adapters,
        member_weight_authority=weight_authority,
        worker_id="worker:unavailable-council",
        lease_duration_ms=30_000,
        clock=lambda: T1,
    )
    publication = runtime.assemble_and_seal(
        candidate.key,
        numeric=numeric,
        council=council,
        council_authority=authority,
        council_manifest_digest=roster.body_digest,
        observed_at=T1,
    )

    member_records = repository.records_for_card(candidate.key.card_digest)[2:]
    assert council.assessment.valid_member_count == valid_member_count
    assert council.assessment.availability.value == "unavailable"
    assert sum(item.state is JobState.SUCCEEDED for item in member_records) == valid_member_count
    assert all(
        item.state
        in {
            JobState.SUCCEEDED,
            JobState.INVALID,
            JobState.PERMANENT_FAILED,
            JobState.CANCELLED,
            JobState.STALE,
        }
        for item in member_records
    )
    assert all(adapter.calls == 1 for adapter in adapters.values())
    assert tuple(item.assessor for item in publication.authority.forecasts) == (
        AssessorKind.FORMULA,
        AssessorKind.ML,
        AssessorKind.LLM_COUNCIL,
    )
    council_forecast = publication.authority.forecasts[2]
    assert council_forecast.state is ForecastState.ABSTAINED
    assert council_forecast.distribution is None
    assert council_forecast.abstention_code == "council_unavailable"
    assert publication.availability[-1] == (
        "llm_council",
        f"unavailable_{valid_member_count}_of_3",
    )

    restarted_repository = DurableJobRepository(
        repository.database_path,
        capacity=_capacity(),
        signer=signer,
        trust_store=trust,
    )
    restarted_rolling = DurableRollingPreparationCoordinator(
        restarted_repository, signer=signer, trust_store=trust
    )
    restarted_outputs = DurableForecastOutputStore(
        ContentAddressedRawOutputSink(
            ContentAddressedBlobStore(tmp_path / "rolling" / "forecast-blobs")
        )
    )
    restarted_ml = _ML(candidate.key.bundle_digest)
    restarted = DurableForecastRuntime(
        restarted_repository,
        restarted_rolling,
        formula_provider=FormulaForecastProvider(
            projection=projection,
            manifest=FormulaManifest.load("benchmarks/v3/formula_manifest.json"),
            output_store=restarted_outputs,
        ),
        ml_provider=MLForecastProvider(
            assessor=restarted_ml,
            output_store=restarted_outputs,
        ),
        output_store=restarted_outputs,
        signer=signer,
        trust_store=trust,
        retry_policy=RetryPolicy("rolling-card-v1"),
        publication_reactions=_PublicationReactions(restarted_repository.database_path),
    )
    replayed_numeric = restarted.prepare_numeric(
        candidate.key,
        worker_id="worker:unavailable-numeric-restart",
        lease_duration_ms=30_000,
        clock=lambda: T1,
    )
    no_call_adapters, _counts = _runtime_adapters(restarted, restarted_repository, authority)
    replayed_council = restarted.prepare_council(
        candidate.key,
        authority=authority,
        adapters=no_call_adapters,
        member_weight_authority=weight_authority,
        worker_id="worker:unavailable-council-restart",
        lease_duration_ms=30_000,
        clock=lambda: T1,
    )
    replayed_publication = restarted.assemble_and_seal(
        candidate.key,
        numeric=replayed_numeric,
        council=replayed_council,
        council_authority=authority,
        council_manifest_digest=roster.body_digest,
        observed_at=T1,
    )

    assert replayed_numeric == numeric
    assert replayed_council == council
    assert replayed_publication == publication
    assert restarted_ml.calls == 0
    assert all(adapter.calls == 0 for adapter in no_call_adapters.values())

    forged_council_forecast = AssessorForecast.create(
        forecast_id=StableIdentifier("forecast:forged-unavailable-council"),
        assessor=council_forecast.assessor,
        state=council_forecast.state,
        evidence_digest=council_forecast.evidence_digest,
        distribution=council_forecast.distribution,
        support=council_forecast.support,
        warnings=council_forecast.warnings,
        artifacts=council_forecast.artifacts,
        abstention_code="forged_council_unavailable",
    )
    forged_card = seal_competitor_card_authority(
        publication.authority.evidence_packet,
        (numeric.formula, numeric.ml, forged_council_forecast),
        bundle_digest=candidate.key.bundle_digest,
        signer=signer,
        created_at=T1,
    )
    forged_aggregate = restarted._aggregate_manifest(
        candidate.key,
        forged_card,
        council_manifest_digest=roster.body_digest,
        council_receipt_reference=replayed_council.council_receipt,
        observed_at=T1,
    )
    with pytest.raises(DurableJobError, match="existing rolling publication material differs"):
        restarted_rolling.seal_card(
            candidate.key,
            forged_card,
            council_manifest_digest=roster.body_digest,
            council_aggregate_authority=forged_aggregate,
            observed_at=T1,
        )
