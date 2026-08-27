"""Executable, restart-safe composition for Formula, ML, and promoted LLM cards.

The scheduler owns work identity.  This module owns execution: it rebuilds each
assessor input from the sealed job payload, retains the exact canonical output before
settlement, and only constructs a signed card from terminal durable publications.
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Mapping, Protocol

from strathmark.v3.application.coordinator import (
    CardKey,
    DurableCoordinator,
    DurableRollingPreparationCoordinator,
    ProviderFailure,
    ProviderResponse,
    RollingCardPublication,
)
from strathmark.v3.application.field_assembly import seal_competitor_card_authority
from strathmark.v3.application.job_ports import (
    DurableJobError,
    FailureKind,
    ProviderAttemptAudit,
    ProviderExecutionAudit,
    ProviderStorageAudit,
    RetryPolicy,
)
from strathmark.v3.assessors.base import FormulaInputPacket
from strathmark.v3.assessors.formula import FormulaManifest, assess_formula
from strathmark.v3.assessors.llm_council import (
    CouncilAvailability,
    MemberAdapter,
    MemberOutcome,
    OperationalCouncilMixture,
    PromotedCouncilAuthority,
    ProviderCallError,
    SignedMemberWeightAuthority,
    aggregate_council,
    member_outcome_from_response,
    replay_sealed_council,
    replay_sealed_member_outcome,
    seal_council_receipt,
    seal_member_outcome,
    unavailable_member_outcome,
    verify_member_weight_authority,
)
from strathmark.v3.assessors.ml import MLAssessment, MLAssessor
from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.evidence import EvidencePacket, require_utc_milliseconds
from strathmark.v3.contracts.forecasts import (
    ArtifactIdentity,
    AssessorForecast,
    AssessorKind,
    EvidenceSupport,
    ForecastState,
    ForecastWarning,
)
from strathmark.v3.contracts.identifiers import deterministic_identifier
from strathmark.v3.infrastructure.blobs import BlobStoreError
from strathmark.v3.infrastructure.integrity import (
    IntegrityError,
    IntegrityTrustStore,
    P256Signer,
    SignedManifest,
    sign_manifest,
)
from strathmark.v3.infrastructure.ollama import (
    ContentAddressedRawOutputSink,
    RawOutputStorageReference,
)
from strathmark.v3.infrastructure.sqlite.jobs import JobRecord, JobState


class FormulaProjectionPort(Protocol):
    """Resolve the signed governor projection for one persisted evidence packet."""

    def __call__(self, evidence: EvidencePacket) -> FormulaInputPacket: ...


class MLAssessorPort(Protocol):
    def assess(self, packet: EvidencePacket) -> MLAssessment: ...


@dataclass(frozen=True, slots=True)
class NumericForecasts:
    formula: AssessorForecast
    ml: AssessorForecast

    def __post_init__(self) -> None:
        if (
            not isinstance(self.formula, AssessorForecast)
            or self.formula.assessor is not AssessorKind.FORMULA
            or not isinstance(self.ml, AssessorForecast)
            or self.ml.assessor is not AssessorKind.ML
        ):
            raise DurableJobError("numeric runtime requires ordered Formula and ML forecasts")


@dataclass(frozen=True, slots=True)
class DurableCouncilResult:
    assessment: OperationalCouncilMixture
    member_receipts: tuple[RawOutputStorageReference | None, ...]
    council_receipt: RawOutputStorageReference
    member_weight_authority: RawOutputStorageReference

    def __post_init__(self) -> None:
        if (
            not isinstance(self.assessment, OperationalCouncilMixture)
            or not isinstance(self.member_receipts, tuple)
            or len(self.member_receipts) != 3
            or not all(
                item is None or isinstance(item, RawOutputStorageReference)
                for item in self.member_receipts
            )
            or not isinstance(self.council_receipt, RawOutputStorageReference)
            or not isinstance(self.member_weight_authority, RawOutputStorageReference)
        ):
            raise DurableJobError("durable council result authority differs")


class DurableForecastOutputStore:
    """Exact canonical component bytes retained inline or content-addressed."""

    def __init__(self, sink: ContentAddressedRawOutputSink) -> None:
        if not isinstance(sink, ContentAddressedRawOutputSink):
            raise DurableJobError("forecast output store requires durable raw storage")
        self._sink = sink
        self._lock = Lock()

    def retain(self, value: Mapping[str, Any]) -> tuple[bytes, RawOutputStorageReference]:
        return self.retain_bytes(canonical_bytes(value))

    def retain_bytes(self, raw: bytes) -> tuple[bytes, RawOutputStorageReference]:
        if not isinstance(raw, bytes) or not raw:
            raise DurableJobError("forecast output storage requires immutable nonempty bytes")
        with self._lock:
            before = len(self._sink.references)
            digest = self._sink.publish(raw)
            if len(self._sink.references) != before + 1:
                raise DurableJobError("forecast output storage did not retain one exact reference")
            reference = self._sink.references[-1]
        if reference.raw_digest != digest or digest != hashlib.sha256(raw).hexdigest():
            raise DurableJobError("forecast output reference differs from exact bytes")
        return raw, reference

    def read(self, reference: RawOutputStorageReference) -> bytes:
        try:
            with self._lock:
                raw = self._sink.read_raw(reference)
        except (BlobStoreError, ValueError) as exc:
            raise DurableJobError("forecast output storage authority differs") from exc
        if hashlib.sha256(raw).hexdigest() != reference.raw_digest:
            raise DurableJobError("forecast output bytes differ from their durable reference")
        return raw


class _NumericProvider:
    def __init__(self, *, assessor: AssessorKind, output_store: DurableForecastOutputStore) -> None:
        if assessor not in {AssessorKind.FORMULA, AssessorKind.ML}:
            raise DurableJobError("numeric provider assessor is closed")
        self._assessor = assessor
        self._outputs = output_store

    def execute(self, job: JobRecord) -> ProviderResponse:
        try:
            packet = _packet_from_job(job, expected_assessor=self._assessor)
            forecast, source_value, input_digest, artifact_digest = self._assess(packet)
            if forecast.assessor is not self._assessor:
                raise DurableJobError("numeric provider returned another assessor identity")
            envelope = {
                "schema_version": "strathmark-v3-durable-forecast-output-v1",
                "assessor": self._assessor.value,
                "evidence_packet_digest": packet.content_digest,
                "input_digest": input_digest,
                "source_output": source_value,
                "forecast_commit_digest": forecast.commit_digest,
            }
            _raw, reference = self._outputs.retain(envelope)
            audit = _provider_audit(
                assessor=self._assessor,
                bundle_digest=job.bundle_digest,
                artifact_digest=artifact_digest,
                reference=reference,
            )
            return ProviderResponse(
                forecast.commit_digest,
                packet.content_digest,
                job.bundle_digest,
                forecast,
                audit,
            )
        except ProviderFailure:
            raise
        except (DurableJobError, TypeError, ValueError) as exc:
            raise ProviderFailure(
                FailureKind.VALIDATION, f"{self._assessor.value}_forecast_invalid"
            ) from exc

    def _assess(
        self, packet: EvidencePacket
    ) -> tuple[AssessorForecast, Mapping[str, Any], str, str]:
        raise NotImplementedError


class FormulaForecastProvider(_NumericProvider):
    def __init__(
        self,
        *,
        projection: FormulaProjectionPort,
        manifest: FormulaManifest,
        output_store: DurableForecastOutputStore,
    ) -> None:
        if not callable(projection) or not isinstance(manifest, FormulaManifest):
            raise DurableJobError("Formula runtime requires projected evidence and a manifest")
        super().__init__(assessor=AssessorKind.FORMULA, output_store=output_store)
        self._projection = projection
        self._manifest = manifest

    def _assess(
        self, packet: EvidencePacket
    ) -> tuple[AssessorForecast, Mapping[str, Any], str, str]:
        projected = self._projection(packet)
        if not isinstance(projected, FormulaInputPacket) or projected.evidence != packet:
            raise DurableJobError("Formula projection differs from the sealed evidence packet")
        result = assess_formula(projected, self._manifest)
        return result.forecast, result.to_dict(), projected.digest, self._manifest.digest


class MLForecastProvider(_NumericProvider):
    def __init__(
        self,
        *,
        assessor: MLAssessorPort,
        output_store: DurableForecastOutputStore,
    ) -> None:
        if not callable(getattr(assessor, "assess", None)):
            raise DurableJobError("ML runtime requires an assessor port")
        super().__init__(assessor=AssessorKind.ML, output_store=output_store)
        self._ml = assessor

    @classmethod
    def from_loaded_assessor(
        cls,
        assessor: MLAssessor,
        *,
        output_store: DurableForecastOutputStore,
    ) -> MLForecastProvider:
        if not isinstance(assessor, MLAssessor):
            raise DurableJobError("ML production runtime requires a loaded MLAssessor")
        return cls(assessor=assessor, output_store=output_store)

    def _assess(
        self, packet: EvidencePacket
    ) -> tuple[AssessorForecast, Mapping[str, Any], str, str]:
        result = self._ml.assess(packet)
        if not isinstance(result, MLAssessment) or result.forecast.evidence_digest != (
            packet.content_digest
        ):
            raise DurableJobError("ML output differs from the sealed evidence packet")
        return result.forecast, result.to_dict(), packet.content_digest, result.bundle_digest


class _CouncilMemberProvider:
    """Turn one provider response into the durable member receipt publication."""

    def __init__(
        self,
        adapter: MemberAdapter,
        authority: PromotedCouncilAuthority,
        reliability_weights: Mapping[str, str],
        context_weights: Mapping[str, str],
        outputs: DurableForecastOutputStore,
    ) -> None:
        self._adapter = adapter
        self._authority = authority
        self._reliability = reliability_weights
        self._context = context_weights
        self._outputs = outputs

    def execute(self, job: JobRecord) -> ProviderResponse:
        try:
            response = self._adapter.execute(job)
        except ProviderCallError as exc:
            exc.bind_execution(self._adapter.member)
            raise
        if not isinstance(response, ProviderResponse) or response.provider_audit is None:
            raise ProviderFailure(FailureKind.VALIDATION, "member_provider_audit_missing")
        outcome = member_outcome_from_response(
            job,
            self._adapter,
            response,
            reliability_weights=self._reliability,
            context_weights=self._context,
        )
        sealed = seal_member_outcome(outcome, authority=self._authority)
        _raw, reference = self._outputs.retain_bytes(sealed)
        original = response.provider_audit
        wrapper = ProviderExecutionAudit(
            original.provider_id,
            original.member_id,
            original.member_pin_json,
            original.member_pin_digest,
            "succeeded",
            None,
            (
                ProviderAttemptAudit(
                    1,
                    reference.raw_digest,
                    "valid_member_receipt",
                    True,
                    ProviderStorageAudit.create(reference),
                ),
            ),
        )
        return ProviderResponse(
            reference.raw_digest,
            response.evidence_digest,
            response.bundle_digest,
            outcome,
            wrapper,
        )


class PublicationReactionPort(Protocol):
    """Wake durable settlement reactions after a rolling card publication."""

    database_path: Path

    def recover_pending(self) -> int: ...


class DurableForecastRuntime:
    """Execute exact rolling numeric jobs and construct cards from durable terminals."""

    def __init__(
        self,
        repository: Any,
        coordinator: DurableRollingPreparationCoordinator,
        *,
        formula_provider: FormulaForecastProvider,
        ml_provider: MLForecastProvider,
        output_store: DurableForecastOutputStore,
        signer: P256Signer,
        trust_store: IntegrityTrustStore,
        retry_policy: RetryPolicy,
        publication_reactions: PublicationReactionPort,
    ) -> None:
        required = ("claim_exact", "records_for_card", "provider_execution", "get")
        if any(not callable(getattr(repository, name, None)) for name in required):
            raise DurableJobError("forecast runtime requires durable execution and audit ports")
        if not isinstance(coordinator, DurableRollingPreparationCoordinator):
            raise DurableJobError("forecast runtime requires rolling card orchestration")
        if not isinstance(formula_provider, FormulaForecastProvider) or not isinstance(
            ml_provider, MLForecastProvider
        ):
            raise DurableJobError("forecast runtime requires Formula and ML providers")
        if (
            not callable(getattr(signer, "sign", None))
            or not isinstance(trust_store, IntegrityTrustStore)
            or not isinstance(retry_policy, RetryPolicy)
        ):
            raise DurableJobError("forecast runtime requires signing and retry authorities")
        if (
            not callable(getattr(publication_reactions, "recover_pending", None))
            or not isinstance(getattr(publication_reactions, "database_path", None), Path)
            or Path(publication_reactions.database_path).resolve()
            != Path(repository.database_path).resolve()
        ):
            raise DurableJobError("forecast runtime requires same-ledger publication reactions")
        self._repository = repository
        self._rolling = coordinator
        self._formula = formula_provider
        self._ml = ml_provider
        self._outputs = output_store
        self._signer = signer
        self._trust_store = trust_store
        self._publication_reactions = publication_reactions
        self._durable = DurableCoordinator(repository, retry_policy=retry_policy)

    def prepare_numeric(
        self,
        key: CardKey,
        *,
        worker_id: str,
        lease_duration_ms: int,
        clock: Callable[[], str],
    ) -> NumericForecasts:
        if not isinstance(key, CardKey) or not callable(clock):
            raise DurableJobError("numeric preparation requires a causal card and clock")
        forecasts = []
        for ordinal, assessor, provider in (
            (1, AssessorKind.FORMULA, self._formula),
            (2, AssessorKind.ML, self._ml),
        ):
            record = self._record(key, ordinal)
            if record.state is not JobState.SUCCEEDED:
                lease = self._repository.claim_exact(
                    record.job_id,
                    record.job_revision,
                    worker_id=f"{worker_id}.{assessor.value}",
                    clock=clock,
                    lease_duration_ms=lease_duration_ms,
                )
                if lease is None:
                    raise DurableJobError(f"{assessor.value} component is not claimable")
                outcome = self._durable.run_claimed(
                    lease,
                    provider=provider,
                    current_context=lambda current: _current_context(current, key),
                    publish=lambda _current, _response: None,
                    clock=clock,
                )
                if outcome.job is None or outcome.job.state is not JobState.SUCCEEDED:
                    raise DurableJobError(f"{assessor.value} component did not succeed")
                record = outcome.job
            forecasts.append(self._load_forecast(record, assessor=assessor, key=key))
        return NumericForecasts(forecasts[0], forecasts[1])

    def prepare_council(
        self,
        key: CardKey,
        *,
        authority: PromotedCouncilAuthority,
        adapters: Mapping[str, MemberAdapter],
        member_weight_authority: SignedMemberWeightAuthority,
        worker_id: str,
        lease_duration_ms: int,
        clock: Callable[[], str],
    ) -> DurableCouncilResult:
        """Overlap cloud with local execution while preserving one local GPU lease."""

        if (
            not isinstance(key, CardKey)
            or not isinstance(authority, PromotedCouncilAuthority)
            or authority.bundle_digest != key.bundle_digest
            or not isinstance(adapters, Mapping)
            or not callable(clock)
        ):
            raise DurableJobError("council preparation requires exact promoted authority")
        members = {item.member_id: item for item in authority.members}
        member_ids = tuple(sorted(members))
        try:
            weight_receipt = verify_member_weight_authority(
                member_weight_authority,
                trust_store=self._trust_store,
                expected_member_ids=member_ids,
                expected_evidence_digest=key.evidence_digest,
                expected_bundle_digest=key.bundle_digest,
                expected_council_component_digest=authority.component_digest,
            )
        except ValueError as exc:
            raise DurableJobError("council member-weight authority differs") from exc
        reliability_weights = dict(weight_receipt.reliability_weights)
        context_weights = dict(weight_receipt.context_weights)
        if set(adapters) != set(members) or any(
            getattr(adapters[name], "member", None) != member for name, member in members.items()
        ):
            raise DurableJobError("council adapters or weights differ from promoted roster")
        records = {
            item.payload()["component_id"]: item
            for item in self._repository.records_for_card(key.card_digest)
            if item.payload().get("component_ordinal") in {3, 4, 5}
        }
        if set(records) != set(members):
            raise DurableJobError("rolling council jobs differ from promoted roster")
        local = tuple(
            item.member_id for item in authority.members if item.provider_kind.value == "local"
        )
        cloud = next(
            item.member_id for item in authority.members if item.provider_kind.value == "cloud"
        )

        cloud_future = None
        cloud_lease = self._claim_member(
            records[cloud],
            member_id=cloud,
            worker_id=worker_id,
            lease_duration_ms=lease_duration_ms,
            clock=clock,
        )
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="strathmark-v3-cloud")
        try:
            if cloud_lease is not None:
                cloud_future = pool.submit(
                    self._execute_member,
                    cloud_lease,
                    adapters[cloud],
                    authority,
                    reliability_weights,
                    context_weights,
                    clock,
                )
            for member_id in local:
                lease = self._claim_member(
                    records[member_id],
                    member_id=member_id,
                    worker_id=worker_id,
                    lease_duration_ms=lease_duration_ms,
                    clock=clock,
                )
                if lease is not None:
                    self._execute_member(
                        lease,
                        adapters[member_id],
                        authority,
                        reliability_weights,
                        context_weights,
                        clock,
                    )
            if cloud_future is not None:
                cloud_future.result()
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

        ordered_records = tuple(
            self._repository.get(
                records[item.member_id].job_id, records[item.member_id].job_revision
            )
            for item in authority.members
        )
        loaded = tuple(
            self._load_member_outcome(
                record,
                member,
                authority=authority,
                reliability_weights=reliability_weights,
                context_weights=context_weights,
            )
            for record, member in zip(ordered_records, authority.members, strict=True)
        )
        outcomes = tuple(item[0] for item in loaded)
        for outcome in outcomes:
            if (
                outcome.reliability_weight != reliability_weights[outcome.member_id]
                or outcome.context_weight != context_weights[outcome.member_id]
            ):
                raise DurableJobError("durable member outcome weights differ from authority")
        assessment = aggregate_council(
            outcomes,
            authority=authority,
            member_weight_receipt=weight_receipt,
        )
        sealed_council = seal_council_receipt(
            assessment,
            authority=authority,
            member_weight_receipt=weight_receipt,
        )
        if (
            replay_sealed_council(
                sealed_council,
                authority=authority,
                member_weight_receipt=weight_receipt,
            )
            != assessment
        ):
            raise DurableJobError("durable council aggregate is not reproducible")
        _raw, council_reference = self._outputs.retain_bytes(sealed_council)
        _weight_raw, weight_reference = self._outputs.retain(
            member_weight_authority.manifest.to_dict()
        )
        return DurableCouncilResult(
            assessment,
            tuple(item[1] for item in loaded),
            council_reference,
            weight_reference,
        )

    def _claim_member(
        self,
        record: JobRecord,
        *,
        member_id: str,
        worker_id: str,
        lease_duration_ms: int,
        clock: Callable[[], str],
    ) -> JobRecord | None:
        current = self._repository.get(record.job_id, record.job_revision)
        if current.state is JobState.SUCCEEDED or current.state in {
            JobState.INVALID,
            JobState.PERMANENT_FAILED,
            JobState.CANCELLED,
            JobState.STALE,
        }:
            return None
        lease = self._repository.claim_exact(
            current.job_id,
            current.job_revision,
            worker_id=f"{worker_id}.{member_id}",
            clock=clock,
            lease_duration_ms=lease_duration_ms,
        )
        if lease is None:
            refreshed = self._repository.get(current.job_id, current.job_revision)
            if refreshed.state in {JobState.QUEUED, JobState.LEASED, JobState.RETRYABLE_FAILED}:
                raise DurableJobError(f"council member {member_id} is not claimable")
            return None
        return lease

    def _execute_member(
        self,
        lease: JobRecord,
        adapter: MemberAdapter,
        authority: PromotedCouncilAuthority,
        reliability_weights: Mapping[str, str],
        context_weights: Mapping[str, str],
        clock: Callable[[], str],
    ) -> None:
        provider = _CouncilMemberProvider(
            adapter,
            authority,
            reliability_weights,
            context_weights,
            self._outputs,
        )
        self._durable.run_claimed(
            lease,
            provider=provider,
            current_context=lambda current: (current.evidence_digest, current.bundle_digest),
            publish=lambda _current, _response: None,
            clock=clock,
        )

    def _load_member_outcome(
        self,
        record: JobRecord,
        member: Any,
        *,
        authority: PromotedCouncilAuthority,
        reliability_weights: Mapping[str, str],
        context_weights: Mapping[str, str],
    ) -> tuple[MemberOutcome, RawOutputStorageReference | None]:
        audit = self._repository.provider_execution(
            record.job_id, record.job_revision, record.fencing_token
        )
        if record.state is JobState.SUCCEEDED:
            if (
                record.result_digest is None
                or audit.status != "succeeded"
                or len(audit.attempts) != 1
                or audit.attempts[0].validator_code != "valid_member_receipt"
                or not audit.attempts[0].accepted
            ):
                raise DurableJobError("successful council member receipt authority differs")
            reference = RawOutputStorageReference.from_dict(
                json.loads(audit.attempts[0].storage_reference.reference_json)
            )
            sealed = self._outputs.read(reference)
            if hashlib.sha256(sealed).hexdigest() != record.result_digest:
                raise DurableJobError("council member receipt differs from terminal result")
            outcome = replay_sealed_member_outcome(sealed, authority=authority)
            self._verify_member_attempt_bytes(outcome)
            return outcome, reference
        if record.state not in {
            JobState.INVALID,
            JobState.PERMANENT_FAILED,
            JobState.CANCELLED,
            JobState.STALE,
        }:
            raise DurableJobError("council member has not reached a terminal state")
        try:
            outcome = unavailable_member_outcome(
                record,
                member,
                audit,
                reliability_weights=reliability_weights,
                context_weights=context_weights,
            )
        except ValueError as exc:
            raise DurableJobError(
                "failed council member lacks durable execution authority"
            ) from exc
        sealed = seal_member_outcome(outcome, authority=authority)
        if replay_sealed_member_outcome(sealed, authority=authority) != outcome:
            raise DurableJobError("failed council member receipt is not reproducible")
        self._verify_member_attempt_bytes(outcome)
        return outcome, None

    def _verify_member_attempt_bytes(self, outcome: MemberOutcome) -> None:
        if len(outcome.attempts) != len(outcome.storage_references):
            raise DurableJobError("council member raw attempt authority differs")
        for attempt, reference in zip(outcome.attempts, outcome.storage_references, strict=True):
            if not isinstance(reference, RawOutputStorageReference):
                raise DurableJobError("council member raw output reference is not durable")
            raw = self._outputs.read(reference)
            if hashlib.sha256(raw).hexdigest() != attempt.output_digest:
                raise DurableJobError("council member raw attempt bytes differ")

    def assemble_and_seal(
        self,
        key: CardKey,
        *,
        numeric: NumericForecasts,
        council: OperationalCouncilMixture | DurableCouncilResult,
        council_authority: PromotedCouncilAuthority,
        council_manifest_digest: str,
        observed_at: str,
    ) -> RollingCardPublication:
        if not isinstance(key, CardKey) or not isinstance(numeric, NumericForecasts):
            raise DurableJobError("card assembly requires causal numeric forecasts")
        now = require_utc_milliseconds(observed_at)
        durable_council = council if isinstance(council, DurableCouncilResult) else None
        assessment = council.assessment if durable_council is not None else council
        if not isinstance(assessment, OperationalCouncilMixture) or not isinstance(
            council_authority, PromotedCouncilAuthority
        ):
            raise DurableJobError("card assembly requires promoted council output")
        weight_receipt = None
        if durable_council is not None:
            try:
                weight_manifest = SignedManifest.from_dict(
                    json.loads(
                        self._outputs.read(durable_council.member_weight_authority).decode("utf-8")
                    )
                )
                weight_receipt = verify_member_weight_authority(
                    SignedMemberWeightAuthority(weight_manifest),
                    trust_store=self._trust_store,
                    expected_member_ids=tuple(
                        sorted(member.member_id for member in council_authority.members)
                    ),
                    expected_evidence_digest=key.evidence_digest,
                    expected_bundle_digest=key.bundle_digest,
                    expected_council_component_digest=council_authority.component_digest,
                )
            except (IntegrityError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                raise DurableJobError("durable member-weight authority differs") from exc
        sealed_council = seal_council_receipt(
            assessment,
            authority=council_authority,
            member_weight_receipt=weight_receipt,
        )
        if (
            replay_sealed_council(
                sealed_council,
                authority=council_authority,
                member_weight_receipt=weight_receipt,
            )
            != assessment
        ):
            raise DurableJobError("council output is not reproducible")
        if assessment.bundle_digest != key.bundle_digest:
            raise DurableJobError("council bundle differs from the causal card")
        council_reference = None
        if durable_council is not None:
            council_reference = durable_council.council_receipt
            if self._outputs.read(council_reference) != sealed_council:
                raise DurableJobError("council aggregate bytes differ from durable authority")
        packet = _packet_from_job(self._record(key, 1), expected_assessor=AssessorKind.FORMULA)
        self._verify_numeric_current(key, numeric)
        member_records = tuple(self._record(key, ordinal) for ordinal in (3, 4, 5))
        if any(
            item.state in {JobState.QUEUED, JobState.LEASED, JobState.RETRYABLE_FAILED}
            for item in member_records
        ):
            raise DurableJobError("council cannot aggregate before every terminal outcome")
        outcomes = {item.member_id: item for item in assessment.outcomes}
        if len(outcomes) != 3:
            raise DurableJobError("council output does not contain three exact members")
        references_by_member = (
            {}
            if durable_council is None
            else {
                member.member_id: reference
                for member, reference in zip(
                    council_authority.members,
                    durable_council.member_receipts,
                    strict=True,
                )
            }
        )
        for record in member_records:
            member_id = record.payload()["component_id"]
            outcome = outcomes.get(member_id)
            if outcome is None or outcome.evidence_digest != key.evidence_digest:
                raise DurableJobError("council output differs from durable member publication")
            if durable_council is None:
                if (
                    outcome.audit is None
                    or record.result_digest != outcome.audit.raw_response_digest
                ):
                    raise DurableJobError("council output differs from durable member publication")
            else:
                reference = references_by_member[member_id]
                if (outcome.validated is not None) != (record.state is JobState.SUCCEEDED):
                    raise DurableJobError("council availability differs from durable terminals")
                if record.state is JobState.SUCCEEDED:
                    if reference is None or record.result_digest != reference.raw_digest:
                        raise DurableJobError("council member receipt binding differs")
                elif reference is not None or record.result_digest is not None:
                    raise DurableJobError("failed council member fabricated a receipt result")
        council_forecast = _council_forecast(
            packet,
            assessment,
            receipt_digest=hashlib.sha256(sealed_council).hexdigest(),
        )
        forecasts = (numeric.formula, numeric.ml, council_forecast)
        card = seal_competitor_card_authority(
            packet,
            forecasts,
            bundle_digest=key.bundle_digest,
            signer=self._signer,
            created_at=now,
        )
        aggregate = self._aggregate_manifest(
            key,
            card,
            council_manifest_digest=council_manifest_digest,
            council_receipt_reference=council_reference,
            observed_at=now,
        )
        publication = self._rolling.seal_card(
            key,
            card,
            council_manifest_digest=council_manifest_digest,
            council_aggregate_authority=aggregate,
            observed_at=now,
        )
        self._publication_reactions.recover_pending()
        return publication

    def _load_forecast(
        self, record: JobRecord, *, assessor: AssessorKind, key: CardKey
    ) -> AssessorForecast:
        if record.state is not JobState.SUCCEEDED or record.result_digest is None:
            raise DurableJobError("forecast output is not a successful durable terminal")
        audit = self._repository.provider_execution(
            record.job_id, record.job_revision, record.fencing_token
        )
        if (
            audit.status != "succeeded"
            or audit.member_id != assessor.value
            or len(audit.attempts) != 1
            or not audit.attempts[0].accepted
        ):
            raise DurableJobError("forecast execution audit differs")
        pin = json.loads(audit.member_pin_json)
        if (
            not isinstance(pin, dict)
            or set(pin) != {"schema_version", "assessor", "bundle_digest", "artifact_digest"}
            or pin["schema_version"] != "strathmark-v3-numeric-provider-pin-v1"
            or pin["assessor"] != assessor.value
            or pin["bundle_digest"] != key.bundle_digest
        ):
            raise DurableJobError("forecast provider pin differs from the causal card")
        reference = RawOutputStorageReference.from_dict(
            json.loads(audit.attempts[0].storage_reference.reference_json)
        )
        raw = self._outputs.read(reference)
        try:
            value = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise DurableJobError("forecast output bytes are not canonical JSON") from exc
        if (
            canonical_bytes(value) != raw
            or not isinstance(value, dict)
            or set(value)
            != {
                "schema_version",
                "assessor",
                "evidence_packet_digest",
                "input_digest",
                "source_output",
                "forecast_commit_digest",
            }
        ):
            raise DurableJobError("forecast output envelope differs")
        source = value["source_output"]
        if not isinstance(source, dict) or not isinstance(source.get("forecast"), dict):
            raise DurableJobError("forecast source output differs")
        forecast = AssessorForecast.from_dict(source["forecast"])
        source_digest = source.get("assessment_digest")
        source_content = {
            name: item for name, item in source.items() if name != "assessment_digest"
        }
        expected_artifact = (
            source.get("manifest_digest")
            if assessor is AssessorKind.FORMULA
            else source.get("bundle_digest")
        )
        if (
            value["schema_version"] != "strathmark-v3-durable-forecast-output-v1"
            or value["assessor"] != assessor.value
            or value["evidence_packet_digest"] != key.evidence_digest
            or value["forecast_commit_digest"] != forecast.commit_digest
            or source_digest != canonical_digest(source_content)
            or pin["artifact_digest"] != expected_artifact
            or record.result_digest != forecast.commit_digest
            or forecast.assessor is not assessor
            or (
                assessor is AssessorKind.FORMULA
                and forecast.evidence_digest != value["input_digest"]
            )
            or (
                assessor is AssessorKind.ML
                and (
                    value["input_digest"] != key.evidence_digest
                    or forecast.evidence_digest != key.evidence_digest
                )
            )
        ):
            raise DurableJobError("forecast output differs from durable job authority")
        return forecast

    def _verify_numeric_current(self, key: CardKey, numeric: NumericForecasts) -> None:
        loaded = NumericForecasts(
            self._load_forecast(self._record(key, 1), assessor=AssessorKind.FORMULA, key=key),
            self._load_forecast(self._record(key, 2), assessor=AssessorKind.ML, key=key),
        )
        if loaded != numeric:
            raise DurableJobError("caller numeric forecasts differ from durable terminals")

    def _record(self, key: CardKey, ordinal: int) -> JobRecord:
        rows = self._repository.records_for_card(key.card_digest)
        matches = tuple(item for item in rows if item.payload().get("component_ordinal") == ordinal)
        if len(matches) != 1 or not isinstance(matches[0], JobRecord):
            raise DurableJobError("rolling card component identity is missing or ambiguous")
        return matches[0]

    def _aggregate_manifest(
        self,
        key: CardKey,
        card: Any,
        *,
        council_manifest_digest: str,
        council_receipt_reference: RawOutputStorageReference | None,
        observed_at: str,
    ) -> SignedManifest:
        records = {
            item.payload()["component_id"]: item
            for item in self._repository.records_for_card(key.card_digest)
        }
        authority_json, authority = self._repository.rolling_council_authority(
            council_manifest_digest
        )
        del authority_json
        payload = authority.body()["payload"]
        members = []
        succeeded = 0
        for member in payload["members"]:
            record = records[member["member_id"]]
            outcome = {
                JobState.SUCCEEDED: "succeeded",
                JobState.CANCELLED: "cancelled",
                JobState.PERMANENT_FAILED: "failed",
                JobState.STALE: "stale",
                JobState.INVALID: "invalid",
            }.get(record.state)
            if outcome is None:
                raise DurableJobError("council aggregate contains active member work")
            succeeded += int(record.state is JobState.SUCCEEDED)
            members.append(
                {
                    "member_id": member["member_id"],
                    "member_manifest_digest": member["member_manifest_digest"],
                    "job_id": record.job_id,
                    "job_revision": record.job_revision,
                    "fencing_token": record.fencing_token,
                    "outcome": outcome,
                    "result_digest": record.result_digest,
                    "terminal_reason_code": record.terminal_reason,
                }
            )
        aggregate_payload = {
            "schema_version": "strathmark-v3-rolling-council-aggregate-v1",
            "purpose": "rolling_card_council_aggregate",
            "card_digest": key.card_digest,
            "council_manifest_digest": council_manifest_digest,
            "member_receipts": members,
            "valid_member_count": succeeded,
            "aggregate_available": succeeded >= 2,
            "aggregate_forecast_commit_digest": card.forecasts[2].commit_digest,
        }
        if council_receipt_reference is not None:
            aggregate_payload["council_receipt_reference"] = council_receipt_reference.to_dict()
        return sign_manifest(
            "rolling_council_aggregate_authority",
            aggregate_payload,
            signer=self._signer,
            created_at=observed_at,
        )


def _packet_from_job(job: JobRecord, *, expected_assessor: AssessorKind) -> EvidencePacket:
    if not isinstance(job, JobRecord):
        raise DurableJobError("forecast execution requires a persisted job record")
    expected_kind = {
        AssessorKind.FORMULA: "formula_card",
        AssessorKind.ML: "ml_card",
    }[expected_assessor]
    payload = job.payload()
    if (
        payload.get("schema_version") != "strathmark-v3-rolling-component-job-v1"
        or payload.get("component_id") != expected_assessor.value
        or job.job_kind.value != expected_kind
        or not isinstance(payload.get("evidence_packet"), dict)
        or not isinstance(payload.get("card_key"), dict)
    ):
        raise DurableJobError("forecast job payload differs from its component kind")
    packet = EvidencePacket.from_dict(payload["evidence_packet"])
    key = payload["card_key"]
    if (
        packet.content_digest != job.evidence_digest
        or packet.content_digest != key.get("evidence_digest")
        or job.bundle_digest != key.get("bundle_digest")
    ):
        raise DurableJobError("forecast job evidence or bundle binding differs")
    return packet


def _current_context(job: JobRecord, key: CardKey) -> tuple[str, str]:
    packet = _packet_from_job(
        job,
        expected_assessor=(
            AssessorKind.FORMULA if job.job_kind.value == "formula_card" else AssessorKind.ML
        ),
    )
    if packet.content_digest != key.evidence_digest or job.bundle_digest != key.bundle_digest:
        raise DurableJobError("forecast commit context is stale")
    return packet.content_digest, job.bundle_digest


def _provider_audit(
    *,
    assessor: AssessorKind,
    bundle_digest: str,
    artifact_digest: str,
    reference: RawOutputStorageReference,
) -> ProviderExecutionAudit:
    pin = {
        "schema_version": "strathmark-v3-numeric-provider-pin-v1",
        "assessor": assessor.value,
        "bundle_digest": bundle_digest,
        "artifact_digest": artifact_digest,
    }
    pin_json = canonical_bytes(pin).decode("utf-8")
    storage = ProviderStorageAudit.create(reference)
    attempt = ProviderAttemptAudit(1, reference.raw_digest, "valid_canonical_output", True, storage)
    return ProviderExecutionAudit(
        f"{assessor.value}.runtime",
        assessor.value,
        pin_json,
        canonical_digest(pin),
        "succeeded",
        None,
        (attempt,),
    )


def _council_forecast(
    packet: EvidencePacket,
    assessment: OperationalCouncilMixture,
    *,
    receipt_digest: str,
) -> AssessorForecast:
    committed = assessment.availability is not CouncilAvailability.UNAVAILABLE
    warnings = (
        (ForecastWarning.DEGRADED_MEMBER_POOL,)
        if assessment.availability is CouncilAvailability.DEGRADED
        else ()
    )
    return AssessorForecast.create(
        forecast_id=deterministic_identifier(
            "forecast",
            {
                "assessor": AssessorKind.LLM_COUNCIL.value,
                "evidence_digest": packet.content_digest,
                "receipt_digest": receipt_digest,
            },
        ),
        assessor=AssessorKind.LLM_COUNCIL,
        state=ForecastState.COMMITTED if committed else ForecastState.ABSTAINED,
        evidence_digest=packet.content_digest,
        distribution=assessment.distribution if committed else None,
        support=EvidenceSupport(
            len(packet.eligible_raw_times_ms),
            str(assessment.valid_member_count),
            sum(
                item.context.digest == packet.target_context.digest
                for item in packet.observations
                if item.result.raw_time_ms is not None
            ),
            packet.historical_cutoff_key,
            packet.tournament_event_sequence,
        ),
        warnings=warnings,
        artifacts=(
            ArtifactIdentity(
                "llm_council_component", "llm-council:v1", assessment.council_component_digest
            ),
            ArtifactIdentity("llm_council_receipt", "llm-council-receipt:v1", receipt_digest),
        ),
        abstention_code=None if committed else "council_unavailable",
    )


__all__ = [
    "DurableCouncilResult",
    "DurableForecastOutputStore",
    "DurableForecastRuntime",
    "FormulaForecastProvider",
    "FormulaProjectionPort",
    "MLAssessorPort",
    "MLForecastProvider",
    "NumericForecasts",
    "PublicationReactionPort",
]
