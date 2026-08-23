from __future__ import annotations

import http.server
import ipaddress
import itertools
import json
import shutil
import sqlite3
import ssl
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

import pytest

import strathmark.v3.assessors.llm_council as council
import strathmark.v3.infrastructure.cloud as cloud_module
import strathmark.v3.infrastructure.ollama as ollama_module
from strathmark.v3.application.capacity import (
    CapacityManifest,
    CapacityUse,
    JobKind,
    JobLane,
    JobPriority,
    LaneCapacity,
)
from strathmark.v3.application.coordinator import (
    DurableCoordinator,
    ProviderFailure,
    RunOutcome,
)
from strathmark.v3.application.job_ports import (
    DurableJobError,
    FailureKind,
    JobConflict,
    ProviderAttemptAudit,
    ProviderExecutionAudit,
    ProviderStorageAudit,
    RetryPolicy,
)
from strathmark.v3.assessors.llm_council import (
    CandidateEvaluationReport,
    CouncilRunner,
    DeadlineBudget,
    EphemeralTestCandidateEvaluationAuthority,
    ExecutedMember,
    LLMMemberSpec,
    MemoryRawOutputSink,
    PersistedLeaseAuthority,
    ProviderCallError,
    ProviderKind,
    ProviderObservation,
    ProviderPacket,
    RawAttempt,
    SealedLLMJob,
    TransportResponse,
    TransportSecurity,
    create_llm_job_payload,
    evaluate_candidate_rotation_receipts,
    execute_response_loop,
)
from strathmark.v3.assessors.llm_council import (
    seal_claimed_llm_job as production_seal_claimed_llm_job,
)
from strathmark.v3.assessors.output_validation import (
    LLM_OUTPUT_SCHEMA_VERSION,
    validate_member_output,
)
from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.commands import MAX_INLINE_PAYLOAD_BYTES, InlinePayload
from strathmark.v3.contracts.evidence import TargetContext
from strathmark.v3.contracts.forecasts import LLMMemberAudit
from strathmark.v3.infrastructure.blobs import ContentAddressedBlobStore
from strathmark.v3.infrastructure.cloud import (
    CloudAdapter as ProductionCloudAdapter,
)
from strathmark.v3.infrastructure.cloud import (
    CloudConfig,
    PinnedHTTPSJSONTransport,
    PolicyEnforcingCloudTransport,
)
from strathmark.v3.infrastructure.integrity import (
    IntegrityTrustStore,
    P256EphemeralSigner,
    sign_manifest,
)
from strathmark.v3.infrastructure.ollama import (
    ContentAddressedRawOutputSink,
    OllamaConfig,
    PinnedLoopbackHTTPTransport,
    RawOutputStorageReference,
)
from strathmark.v3.infrastructure.ollama import (
    OllamaAdapter as ProductionOllamaAdapter,
)
from strathmark.v3.infrastructure.sqlite.jobs import (
    DurableJobRepository,
    JobRecord,
    JobRequest,
    JobState,
)

_CLOUD_TRUSTED_IDENTITIES = []
_TEST_TEMP = tempfile.TemporaryDirectory(prefix="strathmark-u11-")
_TEST_LEASE_AUTHORITY: PersistedLeaseAuthority | None = None
_TEST_REPOSITORIES: dict[str, DurableJobRepository] = {}
_TEST_JOB_SEQUENCE = itertools.count(1)
_TEST_CLOUD_SEQUENCE = itertools.count(1)
_TEST_RAW_SEQUENCE = itertools.count(1)
_TEST_EVALUATIONS = {}


def _audited_executed(member: LLMMemberSpec, center: int = 40_000) -> ExecutedMember:
    payload = _response(center)
    validated = validate_member_output(
        payload,
        expected_evidence_refs=("obs_1",),
        allowed_fact_codes=("observed_raw_time",),
    )
    sink = ContentAddressedRawOutputSink(
        ContentAddressedBlobStore(f"{_TEST_TEMP.name}/raw-{next(_TEST_RAW_SEQUENCE)}")
    )
    digest = sink.publish(payload)
    attempts = (RawAttempt(digest, "valid_committed", True),)
    audit = LLMMemberAudit(
        prompt_digest="1" * 64,
        schema_version=LLM_OUTPUT_SCHEMA_VERSION,
        runtime_version=member.runtime_version,
        model_digest=member.model_digest,
        quantization=member.quantization,
        sampling_parameters_digest=member.sampling_parameters_digest,
        raw_response_digest=digest,
        validator_code="valid_committed",
        latency_ms=1,
        provider_model_version=member.model_id,
        provider_fingerprint=member.model_digest,
        api_revision=member.runtime_version,
        canary_digest=member.runtime_digest,
    )
    references = tuple(sink.references)
    return ExecutedMember(
        member,
        validated,
        attempts,
        audit,
        references,
        council._provider_execution_audit(member, "succeeded", None, attempts, references),
    )


def _response(center: int = 40_000) -> bytes:
    return json.dumps(
        {
            "schema_version": LLM_OUTPUT_SCHEMA_VERSION,
            "state": "committed",
            "quantiles": [
                {"probability": probability, "time_ms": center + offset}
                for probability, offset in (
                    ("0.05", -5_000),
                    ("0.1", -4_000),
                    ("0.25", -2_000),
                    ("0.5", 0),
                    ("0.75", 2_000),
                    ("0.9", 4_000),
                    ("0.95", 5_000),
                )
            ],
            "evidence_refs": ["obs_1"],
            "warnings": [],
            "fact_codes": ["observed_raw_time"],
            "abstention_reason": None,
        }
    ).encode()


@contextmanager
def _provider_server(member: LLMMemberSpec, body: bytes, *, tls_material=None):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("content-length", "0"))
            self.rfile.read(length)
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("x-model-version", member.model_id)
            self.send_header("x-model-digest", member.model_digest)
            self.send_header("x-api-revision", member.runtime_version)
            self.send_header("x-canary-digest", member.runtime_digest)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    if tls_material is not None:
        certificate_path, key_path = tls_material
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(certificate_path, key_path)
        server.socket = server_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _test_tls_material(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    certificate_path = tmp_path / "server-cert.pem"
    key_path = tmp_path / "server-key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    client_context = ssl.create_default_context(cafile=str(certificate_path))
    return certificate_path, key_path, client_context


@dataclass
class FakeTransport:
    responses: list[TransportResponse]
    security_address: str = "203.0.113.7"
    security_hostname: str = "api.example.test"
    certificate_valid: bool = True
    calls: list[object] = field(default_factory=list)
    preflights: list[object] = field(default_factory=list)

    def preflight(self, request):
        self.preflights.append(request)
        from strathmark.v3.assessors.llm_council import TransportSecurity

        return TransportSecurity(
            resolved_address=self.security_address,
            peer_hostname=self.security_hostname,
            certificate_valid=self.certificate_valid,
        )

    def send(self, request):
        self.calls.append(request)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class InvalidSecurityTransport(FakeTransport):
    def preflight(self, request):
        self.preflights.append(request)
        return object()


def _spec(kind: ProviderKind) -> LLMMemberSpec:
    return LLMMemberSpec.candidate(
        member_id="cloud" if kind is ProviderKind.CLOUD else "qwen",
        provider_id="frontier" if kind is ProviderKind.CLOUD else "ollama",
        provider_kind=kind,
        family="frontier" if kind is ProviderKind.CLOUD else "qwen3.5",
        model_id=("frontier-2026-08-01" if kind is ProviderKind.CLOUD else "qwen3.5-9b-q4_k_m"),
        model_digest="2" * 64,
        runtime_version=("api:2026-08" if kind is ProviderKind.CLOUD else "ollama:0.12.3"),
        runtime_digest="3" * 64,
        quantization="provider" if kind is ProviderKind.CLOUD else "Q4_K_M",
        sampling_parameters={"seed": 7, "temperature": "0"},
    )


def _evaluated(member: LLMMemberSpec) -> LLMMemberSpec:
    signer = P256EphemeralSigner.generate(
        f"integrity-key:promotion-{member.member_id}-{next(_TEST_CLOUD_SEQUENCE)}"
    )
    payload = {
        "schema_version": "strathmark-v3-candidate-rotation-receipt-v1",
        "harness": "u19_candidate_harness",
        "candidate_manifest_digest": council._member_manifest_digest(member),
        "provider_id": member.provider_id,
        "member_id": member.member_id,
        "model_id": member.model_id,
        "model_digest": member.model_digest,
        "runtime_version": member.runtime_version,
        "runtime_digest": member.runtime_digest,
        "numeric_packet_digest": "8" * 64,
        "distribution": json.loads(_response())["quantiles"],
    }
    # The gate validates the canonical positive-time distribution object, not raw LLM JSON.
    validated = validate_member_output(
        _response(),
        expected_evidence_refs=("obs_1",),
        allowed_fact_codes=("observed_raw_time",),
    )
    payload["distribution"] = validated.distribution.to_dict()
    receipts = []
    for rotation, digest in (("old", "6" * 64), ("new", "7" * 64)):
        receipts.append(
            sign_manifest(
                "llm_candidate_rotation_result",
                {
                    **payload,
                    "rotation_id": rotation,
                    "token_key_id": f"key:{rotation}",
                    "provider_execution_digest": digest,
                },
                signer=signer,
                created_at="2026-08-23T10:00:00.000Z",
            )
        )
    evaluation = evaluate_candidate_rotation_receipts(
        member,
        receipts[0],
        receipts[1],
        EphemeralTestCandidateEvaluationAuthority(IntegrityTrustStore((signer.identity,))),
    )
    _TEST_EVALUATIONS[council._member_manifest_digest(member)] = evaluation
    return member


def _repinned(member: LLMMemberSpec, **changes) -> LLMMemberSpec:
    return _evaluated(
        replace(member, **changes, promotion=None, status=council.CandidateStatus.CANDIDATE)
    )


class _CandidateEvaluationHarness:
    def __init__(self) -> None:
        self._runner = CouncilRunner()

    def evaluate(self, **kwargs):
        adapters = (*kwargs["local_adapters"], kwargs["cloud_adapter"])
        evaluations = {
            adapter.member.member_id: _TEST_EVALUATIONS.get(
                council._member_manifest_digest(adapter.member), object()
            )
            for adapter in adapters
        }
        return self._runner.run_candidate_evaluation(**kwargs, candidate_evaluations=evaluations)

    def __getattr__(self, name):
        return getattr(self._runner, name)


def _ok(body: bytes, kind: ProviderKind = ProviderKind.LOCAL) -> TransportResponse:
    member = _spec(kind)
    return TransportResponse(
        200,
        body,
        "",
        {},
        provider_model_version=member.model_id,
        provider_fingerprint=member.model_digest,
        api_revision=member.runtime_version,
        canary_digest=member.runtime_digest,
    )


def _packet(kind: ProviderKind, member: LLMMemberSpec | None = None) -> ProviderPacket:
    member = _spec(kind) if member is None else member
    context = TargetContext("underhand", 300, "whitewood", "taxonomy:v1", "conversion:v1")
    row = ProviderObservation("obs_1", 1, 40_000, "underhand", 300, "whitewood", 3, 43_000, 1, 0)
    numeric = {
        "schema_version": "strathmark-v3-llm-provider-packet-v1",
        "target_context": context.to_dict(),
        "observations": [row.numeric_value()],
    }
    return ProviderPacket(
        member.provider_id,
        "scope_opaque",
        "subject_opaque",
        context,
        (row,),
        canonical_digest(numeric),
    )


def _test_lease_authority() -> PersistedLeaseAuthority:
    global _TEST_LEASE_AUTHORITY
    if _TEST_LEASE_AUTHORITY is None:
        signer = P256EphemeralSigner.generate("integrity-key:u11-test-leases")
        capacity = CapacityManifest(
            schema_version="strathmark-v3-job-capacity-v1",
            max_open_tournaments=10_000,
            max_round_entrants=10_000,
            max_field_entrants=10_000,
            max_plausible_qualifiers=10_000,
            max_context_cards=10_000,
            max_queued_jobs=2_048,
            max_receipt_bytes=100_000_000,
            max_blob_bytes=100_000_000,
            max_api_page_size=100,
            reserved_imminent_jobs=1,
            reserved_recovery_jobs=1,
            aging_interval_ms=1_000,
            aging_increment=125,
            lanes=(
                LaneCapacity(JobLane.HOT_FIELD, 2, 1),
                LaneCapacity(JobLane.INFERENCE, 2_048, 2_048),
                LaneCapacity(JobLane.LOOKUP_RECOVERY, 2, 1),
                LaneCapacity(JobLane.MAINTENANCE, 2, 1),
            ),
        )
        bootstrap = DurableJobRepository(
            f"{_TEST_TEMP.name}/jobs.sqlite3",
            capacity=capacity,
            signer=signer,
            trust_store=IntegrityTrustStore((signer.identity,)),
        )

        class TestLeaseAuthority(PersistedLeaseAuthority):
            def current(self, record: object) -> object:
                if not isinstance(record, JobRecord):
                    return super().current(record)
                repository = _TEST_REPOSITORIES.get(record.job_id)
                if repository is None:
                    return super().current(record)
                current = repository.get(record.job_id, record.job_revision)
                if current != record:
                    raise ValueError("LLM lease does not match current persisted repository state")
                return current

            def verify_manifest(self, manifest: object, record: object) -> dict:
                from strathmark.v3.infrastructure.integrity import verify_manifest

                current = self.current(record)
                repository = _TEST_REPOSITORIES[current.job_id]
                return verify_manifest(manifest, repository._trust_store)

            def _repository_for(self, record: object) -> object:
                current = self.current(record)
                return _TEST_REPOSITORIES[current.job_id]

        _TEST_LEASE_AUTHORITY = TestLeaseAuthority(bootstrap)
    return _TEST_LEASE_AUTHORITY


def seal_claimed_llm_job(record, member) -> SealedLLMJob:
    return production_seal_claimed_llm_job(record, member, _test_lease_authority())


def _record(
    kind: ProviderKind,
    *,
    job_id: str = "job:one",
    member: LLMMemberSpec | None = None,
    evidence_digest: str = "a" * 64,
    deadlines: DeadlineBudget | None = None,
    queue_elapsed_ms: int = 0,
    packet: ProviderPacket | None = None,
    payload_override=None,
    claim: bool = True,
) -> JobRecord:
    member = _spec(kind) if member is None else member
    deadlines = deadlines or DeadlineBudget(100, 200, 300, 50, 1_000)
    payload = payload_override or create_llm_job_payload(
        packet or _packet(kind, member), member, deadlines
    )
    job_kind = JobKind.CLOUD_LLM_CARD if kind is ProviderKind.CLOUD else JobKind.LOCAL_LLM_CARD
    authority = _test_lease_authority()
    sequence = next(_TEST_JOB_SEQUENCE)
    persisted_id = f"job:u11test{sequence}"
    signer = P256EphemeralSigner.generate(f"integrity-key:u11-test-lease-{sequence}")
    repository = DurableJobRepository(
        f"{_TEST_TEMP.name}/jobs-{sequence}.sqlite3",
        capacity=authority._repository.capacity,
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity, *_CLOUD_TRUSTED_IDENTITIES)),
    )
    _TEST_REPOSITORIES[persisted_id] = repository
    observed = datetime(2026, 8, 23, 10, 0, tzinfo=timezone.utc) + timedelta(seconds=61 * sequence)
    observed_at = observed.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    queued_at = (
        (observed - timedelta(milliseconds=queue_elapsed_ms))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    queued = repository.enqueue(
        JobRequest.create(
            job_id=persisted_id,
            job_revision=1,
            idempotency_key=f"job_request:u11test{sequence}",
            job_kind=job_kind,
            lane=JobLane.INFERENCE,
            priority=JobPriority.PLAUSIBLE_QUALIFIER,
            capacity_use=CapacityUse(1, 1, 1, 1, 1, 1_024, 4_096, 10),
            payload=payload,
            evidence_digest=evidence_digest,
            bundle_digest="b" * 64,
            retry_policy_version="retry.v1",
            created_at=queued_at,
            not_before_at=queued_at,
            hard_deadline_at="2026-08-24T10:00:00.000Z",
            max_attempts=1,
        )
    )
    if not claim:
        return queued
    claimed = repository.claim(
        JobLane.INFERENCE,
        worker_id="worker:test",
        clock=lambda: observed_at,
        lease_duration_ms=60_000,
    )
    assert claimed is not None and authority.current(claimed) == claimed
    return claimed


def _job(
    kind: ProviderKind,
    *,
    job_id: str = "job:one",
    member: LLMMemberSpec | None = None,
    evidence_digest: str = "a" * 64,
    deadlines: DeadlineBudget | None = None,
) -> SealedLLMJob:
    member = _spec(kind) if member is None else member
    return production_seal_claimed_llm_job(
        _record(
            kind,
            job_id=job_id,
            member=member,
            evidence_digest=evidence_digest,
            deadlines=deadlines,
        ),
        member,
        _test_lease_authority(),
    )


def _cloud_config(*, consent: bool = True, payload_changes=None) -> CloudConfig:
    signer = P256EphemeralSigner.generate(f"integrity-key:cloud-test-{next(_TEST_CLOUD_SEQUENCE)}")
    payload = {
        "origin": "https://api.example.test",
        "hostname": "api.example.test",
        "allowed_addresses": ["203.0.113.7"],
        "api_revision": "api:2026-08",
    }
    payload.update(payload_changes or {})
    manifest = sign_manifest(
        "cloud_llm_deployment",
        payload,
        signer=signer,
        created_at="2026-08-23T10:00:00.000Z",
    )
    _CLOUD_TRUSTED_IDENTITIES.append(signer.identity)
    return CloudConfig(manifest, "secret", consent)


class _TestOnlyCloudAdapter:
    def __init__(self, config, member, transport, sink, authority=None):
        if not isinstance(config, CloudConfig):
            raise ValueError("cloud adapter requires typed configuration")
        if member.provider_kind is not ProviderKind.CLOUD:
            raise ValueError("cloud adapter requires one pinned cloud member")
        self._config = config
        self._member = member
        self._transport = transport
        self._sink = sink
        self._lease_authority = authority or _test_lease_authority()

    @property
    def member(self):
        return self._member

    @property
    def lease_authority(self):
        return self._lease_authority

    def execute(self, job):
        sealed = production_seal_claimed_llm_job(job, self.member, self.lease_authority)
        probe = object.__new__(ProductionCloudAdapter)
        probe._config = self._config
        probe._lease_authority = self.lease_authority
        probe._transport = self._transport
        try:
            config = ProductionCloudAdapter._validated_config(probe, job)
            if config.api_revision != sealed.member.runtime_version:
                raise ProviderCallError("cloud_api_revision_pin_mismatch")
            lifecycle = time.monotonic() - sealed.queue_elapsed_ms / 1_000
            security = ProductionCloudAdapter._preflight(
                probe, sealed, config, lifecycle_started_at=lifecycle
            )
            ProductionCloudAdapter._verify_security(security, config)
            executed = execute_response_loop(
                sealed,
                origin=config.origin,
                headers=(
                    ("authorization", f"Bearer {config.credential}"),
                    ("content-type", "application/json"),
                    ("x-api-revision", config.api_revision),
                ),
                transport=self._transport,
                sink=self._sink,
                connection_id=security.connection_id,
                lifecycle_started_at=lifecycle,
                forbidden_output_tokens=(config.credential.encode(),),
            )
        except ProviderCallError as exc:
            raise exc.bind_execution(self.member)
        return council.ProviderResponse(
            executed.audit.raw_response_digest,
            sealed.evidence_digest,
            sealed.bundle_digest,
            executed,
            executed.execution_audit,
        )


class _TestOnlyOllamaAdapter:
    def __init__(self, config, member, transport, sink, authority=None):
        if not isinstance(config, OllamaConfig):
            raise ValueError("Ollama adapter requires typed configuration")
        if member.provider_kind is not ProviderKind.LOCAL:
            raise ValueError("Ollama adapter requires one pinned local member")
        self._config = config
        self._member = member
        self._transport = transport
        self._sink = sink
        self._lease_authority = authority or _test_lease_authority()

    @property
    def member(self):
        return self._member

    @property
    def lease_authority(self):
        return self._lease_authority

    def execute(self, job):
        sealed = production_seal_claimed_llm_job(job, self.member, self.lease_authority)
        lifecycle = time.monotonic() - sealed.queue_elapsed_ms / 1_000
        try:
            if lifecycle + sealed.deadlines.overall_ms / 1_000 <= time.monotonic():
                raise ProviderCallError("overall_timeout")
            addresses = ollama_module._validate_loopback(self._config)
            security = ollama_module._preflight(
                self._transport,
                council.TransportPreflight(
                    self._config.origin,
                    self._config.hostname,
                    addresses,
                    sealed.deadlines,
                ),
            )
            ollama_module._verify_security(security, self._config.hostname, addresses)
            executed = execute_response_loop(
                sealed,
                origin=self._config.origin,
                headers=(("content-type", "application/json"),),
                transport=self._transport,
                sink=self._sink,
                connection_id=security.connection_id,
                lifecycle_started_at=lifecycle,
            )
        except ProviderCallError as exc:
            raise exc.bind_execution(self.member)
        return council.ProviderResponse(
            executed.audit.raw_response_digest,
            sealed.evidence_digest,
            sealed.bundle_digest,
            executed,
            executed.execution_audit,
        )


def CloudAdapter(config, member, transport, sink):
    return _TestOnlyCloudAdapter(config, member, transport, sink)


def OllamaAdapter(config, member, transport, sink):
    return _TestOnlyOllamaAdapter(config, member, transport, sink)


def test_cloud_requires_consent_and_security_before_payload_egress() -> None:
    transport = FakeTransport([_ok(_response(), ProviderKind.CLOUD)])
    config = _cloud_config(consent=False)
    with pytest.raises(Exception, match="consent"):
        CloudAdapter(config, _spec(ProviderKind.CLOUD), transport, MemoryRawOutputSink()).execute(
            _record(ProviderKind.CLOUD)
        )
    assert transport.preflights == []
    assert transport.calls == []


def test_cloud_transport_cannot_egress_payload_before_verified_connection() -> None:
    backend = FakeTransport([])
    boundary = PolicyEnforcingCloudTransport(backend)
    from strathmark.v3.assessors.llm_council import TransportRequest

    with pytest.raises(ProviderCallError, match="not_preflighted"):
        boundary.send(
            TransportRequest(
                "https://api.example.test",
                b"secret payload",
                (("authorization", "Bearer secret"),),
                DeadlineBudget(1, 1, 1, 1, 10),
                connection_id="missing",
            )
        )
    assert backend.calls == []


def test_cloud_policy_boundary_rejects_proxy_rebinding_and_cross_origin() -> None:
    from strathmark.v3.assessors.llm_council import TransportPreflight, TransportRequest

    with pytest.raises(ValueError, match="bounded connection"):
        PolicyEnforcingCloudTransport(object())  # type: ignore[arg-type]
    backend = FakeTransport([_ok(_response(), ProviderKind.CLOUD)])
    boundary = PolicyEnforcingCloudTransport(backend)
    deadlines = DeadlineBudget(1, 1, 1, 1, 10)
    with pytest.raises(ProviderCallError, match="proxy"):
        boundary.preflight(
            TransportPreflight(
                "https://api.example.test",
                "api.example.test",
                ("203.0.113.7",),
                deadlines,
                use_ambient_proxy=True,
            )
        )
    security = boundary.preflight(
        TransportPreflight(
            "https://api.example.test",
            "api.example.test",
            ("203.0.113.7",),
            deadlines,
        )
    )
    request = TransportRequest(
        "https://evil.test",
        b"payload",
        (),
        deadlines,
        connection_id=security.connection_id,
    )
    with pytest.raises(ProviderCallError, match="policy_mismatch"):
        boundary.send(request)
    backend.responses = [TransportResponse(200, b"x", "https://evil.test", {})]
    with pytest.raises(ProviderCallError, match="redirect"):
        boundary.send(replace(request, origin="https://api.example.test"))
    backend.responses = [object()]  # type: ignore[list-item]
    assert boundary.send(replace(request, origin="https://api.example.test")) is not None
    invalid_security = InvalidSecurityTransport([])
    invalid_boundary = PolicyEnforcingCloudTransport(invalid_security)
    assert (
        invalid_boundary.preflight(
            TransportPreflight(
                "https://api.example.test",
                "api.example.test",
                ("203.0.113.7",),
                deadlines,
            )
        )
        is not None
    )
    with pytest.raises(ProviderCallError, match="not_preflighted"):
        invalid_boundary.send(request)


def test_cloud_manifest_and_member_pins_fail_before_payload_egress() -> None:
    transport = FakeTransport([])
    with pytest.raises(ValueError, match="pinned cloud"):
        CloudAdapter(
            _cloud_config(),
            _spec(ProviderKind.LOCAL),
            transport,
            MemoryRawOutputSink(),
        )
    valid = _cloud_config()
    wrapper = PolicyEnforcingCloudTransport(transport)
    CloudAdapter(valid, _spec(ProviderKind.CLOUD), wrapper, MemoryRawOutputSink())
    with pytest.raises(ProviderCallError, match="revision_pin"):
        CloudAdapter(
            _cloud_config(payload_changes={"api_revision": "api:other"}),
            _spec(ProviderKind.CLOUD),
            transport,
            MemoryRawOutputSink(),
        ).execute(_record(ProviderKind.CLOUD))

    signer = P256EphemeralSigner.generate("integrity-key:bad-cloud-kind")
    base_payload = {
        "origin": "https://api.example.test",
        "hostname": "api.example.test",
        "allowed_addresses": ["203.0.113.7"],
        "api_revision": "api:2026-08",
    }
    wrong_kind = sign_manifest(
        "other_manifest",
        base_payload,
        signer=signer,
        created_at="2026-08-23T10:00:00.000Z",
    )
    wrong_schema = sign_manifest(
        "cloud_llm_deployment",
        {**base_payload, "extra": True},
        signer=signer,
        created_at="2026-08-23T10:00:00.000Z",
    )
    untrusted = P256EphemeralSigner.generate("integrity-key:untrusted-cloud")
    _CLOUD_TRUSTED_IDENTITIES.append(signer.identity)
    wrong_kind_config = CloudConfig(wrong_kind, "secret", True)
    wrong_schema_config = CloudConfig(wrong_schema, "secret", True)
    untrusted_manifest = sign_manifest(
        "cloud_llm_deployment",
        base_payload,
        signer=untrusted,
        created_at="2026-08-23T10:00:00.000Z",
    )
    cases = (
        (wrong_kind_config, "kind"),
        (wrong_schema_config, "schema"),
        (CloudConfig(untrusted_manifest, "secret", True), "manifest_invalid"),
    )
    for config, reason in cases:
        with pytest.raises(ProviderCallError, match=reason):
            CloudAdapter(
                config,
                _spec(ProviderKind.CLOUD),
                transport,
                MemoryRawOutputSink(),
            ).execute(_record(ProviderKind.CLOUD))
    assert transport.calls == []


def test_cloud_authority_is_installation_pinned_not_manifest_selected() -> None:
    record = _record(ProviderKind.CLOUD)
    config = _cloud_config()
    with pytest.raises(ProviderCallError, match="manifest_invalid"):
        CloudAdapter(
            config,
            _spec(ProviderKind.CLOUD),
            FakeTransport([]),
            MemoryRawOutputSink(),
        ).execute(record)


def test_concrete_cloud_transport_binds_direct_tls_socket_without_network(
    monkeypatch,
) -> None:
    deadlines = DeadlineBudget(100, 200, 300, 50, 1_000)
    preflight = council.TransportPreflight(
        "https://api.example.test",
        "api.example.test",
        ("203.0.113.7",),
        deadlines,
    )

    class Socket:
        peer = "203.0.113.7"
        certificate = {"subject": (("CN", "api.example.test"),)}

        def getpeername(self):
            return (self.peer, 443)

        def getpeercert(self):
            return self.certificate

    class HTTPResponse:
        status = 200
        body = b"ok"
        headers = [
            ("X-Model-Version", "model:v1"),
            ("X-Model-Digest", "1" * 64),
            ("X-Api-Revision", "api:v1"),
            ("X-Canary-Digest", "2" * 64),
        ]

        def read(self, _maximum):
            return self.body

        def getheaders(self):
            return self.headers

    class Connection:
        instances = []

        def __init__(self, *_args):
            self.sock = Socket()
            self.closed = False
            self.request_value = None
            self.response = HTTPResponse()
            self.__class__.instances.append(self)

        def connect(self):
            return None

        def close(self):
            self.closed = True

        def request(self, *args, **kwargs):
            self.request_value = (args, kwargs)

        def getresponse(self):
            return self.response

    monkeypatch.setattr(cloud_module, "_DirectHTTPSConnection", Connection)
    transport = PinnedHTTPSJSONTransport()
    with pytest.raises(ProviderCallError, match="direct_connection"):
        transport.preflight(replace(preflight, allowed_addresses=()))
    security = transport.preflight(preflight)
    request = council.TransportRequest(
        preflight.origin,
        b"payload",
        (("authorization", "Bearer test"),),
        deadlines,
        connection_id=security.connection_id,
    )
    response = transport.send(request)
    assert response.body == b"ok"
    assert response.provider_fingerprint == "1" * 64
    assert Connection.instances[-1].closed

    with pytest.raises(ProviderCallError, match="not_preflighted"):
        transport.send(request)
    security = transport.preflight(preflight)
    with pytest.raises(ProviderCallError, match="policy_mismatch"):
        transport.send(replace(request, connection_id=security.connection_id, allow_redirects=True))

    for change, reason in (
        ("missing", "tls_socket"),
        ("peer", "dns_substitution"),
        ("cert", "certificate"),
    ):
        connection = Connection
        original_init = connection.__init__

        def changed_init(self, *args, _change=change):
            original_init(self, *args)
            if _change == "missing":
                self.sock = None
            elif _change == "peer":
                self.sock.peer = "203.0.113.99"
            else:
                self.sock.certificate = {}

        monkeypatch.setattr(connection, "__init__", changed_init)
        with pytest.raises(ProviderCallError, match=reason):
            PinnedHTTPSJSONTransport().preflight(preflight)
        monkeypatch.setattr(connection, "__init__", original_init)

    oversized = PinnedHTTPSJSONTransport()
    security = oversized.preflight(preflight)
    Connection.instances[-1].response.body = b"x" * 1_048_577
    with pytest.raises(ProviderCallError, match="too_large"):
        oversized.send(replace(request, connection_id=security.connection_id))

    redirecting = PinnedHTTPSJSONTransport()
    security = redirecting.preflight(preflight)
    Connection.instances[-1].response.status = 302
    Connection.instances[-1].response.headers = [("Location", "https://evil.test")]
    assert (
        redirecting.send(replace(request, connection_id=security.connection_id)).returned_origin
        == "https://evil.test"
    )


def test_direct_https_connection_uses_exact_ip_and_tls_hostname(monkeypatch) -> None:
    raw_socket = object()

    class Context:
        def wrap_socket(self, value, *, server_hostname):
            assert value is raw_socket
            assert server_hostname == "api.example.test"
            return "tls-socket"

    monkeypatch.setattr(cloud_module.ssl, "create_default_context", lambda: Context())
    monkeypatch.setattr(
        cloud_module.socket,
        "create_connection",
        lambda address, timeout: (
            raw_socket if address == ("203.0.113.7", 443) and timeout == 0.2 else None
        ),
    )
    connection = cloud_module._DirectHTTPSConnection("203.0.113.7", "api.example.test", 443, 0.2)
    connection.connect()
    assert connection.sock == "tls-socket"


def test_concrete_local_transport_binds_loopback_socket_without_network(monkeypatch) -> None:
    deadlines = DeadlineBudget(100, 200, 300, 50, 1_000)
    preflight = council.TransportPreflight(
        "http://127.0.0.1:11434", "127.0.0.1", ("127.0.0.1",), deadlines
    )

    class Socket:
        peer = "127.0.0.1"

        def getpeername(self):
            return (self.peer, 11434)

    class Response:
        status = 200
        body = b"ok"

        def read(self, _maximum):
            return self.body

        def getheaders(self):
            return [("X-Model-Version", "model:v1")]

    class Connection:
        instances = []

        def __init__(self, *_args, **_kwargs):
            self.sock = Socket()
            self.response = Response()
            self.closed = False
            self.__class__.instances.append(self)

        def connect(self):
            return None

        def close(self):
            self.closed = True

        def request(self, *_args, **_kwargs):
            return None

        def getresponse(self):
            return self.response

    monkeypatch.setattr(ollama_module.http.client, "HTTPConnection", Connection)
    transport = PinnedLoopbackHTTPTransport()
    with pytest.raises(ProviderCallError, match="direct_connection"):
        transport.preflight(replace(preflight, allowed_addresses=()))
    with pytest.raises(ProviderCallError, match="loopback_binding"):
        transport.preflight(
            replace(preflight, hostname="192.0.2.1", allowed_addresses=("192.0.2.1",))
        )
    security = transport.preflight(preflight)
    request = council.TransportRequest(
        preflight.origin, b"payload", (), deadlines, connection_id=security.connection_id
    )
    assert transport.send(request).body == b"ok"
    assert Connection.instances[-1].closed
    with pytest.raises(ProviderCallError, match="not_preflighted"):
        transport.send(request)
    security = transport.preflight(preflight)
    with pytest.raises(ProviderCallError, match="policy_mismatch"):
        transport.send(replace(request, connection_id=security.connection_id, allow_redirects=True))
    missing = PinnedLoopbackHTTPTransport()
    original = Connection.__init__

    def missing_socket(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.sock = None

    monkeypatch.setattr(Connection, "__init__", missing_socket)
    with pytest.raises(ProviderCallError, match="socket_substitution"):
        missing.preflight(preflight)
    monkeypatch.setattr(Connection, "__init__", original)
    oversized = PinnedLoopbackHTTPTransport()
    security = oversized.preflight(preflight)
    Connection.instances[-1].response.body = b"x" * 1_048_577
    with pytest.raises(ProviderCallError, match="too_large"):
        oversized.send(replace(request, connection_id=security.connection_id))


def test_cloud_success_uses_signed_pins_and_returns_u7_provider_response() -> None:
    transport = FakeTransport([_ok(_response(), ProviderKind.CLOUD)])
    response = CloudAdapter(
        _cloud_config(),
        _spec(ProviderKind.CLOUD),
        transport,
        MemoryRawOutputSink(),
    ).execute(_record(ProviderKind.CLOUD))
    assert response.evidence_digest == "a" * 64
    with pytest.raises(ValueError, match="job kind"):
        CloudAdapter(
            _cloud_config(),
            _spec(ProviderKind.CLOUD),
            FakeTransport([]),
            MemoryRawOutputSink(),
        ).execute(_record(ProviderKind.LOCAL))
    with pytest.raises(ValueError, match="pinned local"):
        OllamaAdapter(
            OllamaConfig("http://127.0.0.1:11434", "127.0.0.1"),
            _spec(ProviderKind.CLOUD),
            FakeTransport([]),
            MemoryRawOutputSink(),
        )
    with pytest.raises(ValueError, match="job kind"):
        OllamaAdapter(
            OllamaConfig("http://127.0.0.1:11434", "127.0.0.1"),
            _spec(ProviderKind.LOCAL),
            FakeTransport([]),
            MemoryRawOutputSink(),
        ).execute(_record(ProviderKind.CLOUD))


def test_cloud_credential_echo_is_rejected_before_any_raw_persistence(tmp_path) -> None:
    body = b'{"echo":"Bearer secret"}'
    transport = FakeTransport([_ok(body, ProviderKind.CLOUD)])
    sink = ContentAddressedRawOutputSink(ContentAddressedBlobStore(tmp_path / "blobs"))
    with pytest.raises(ProviderCallError, match="credential_echo_rejected"):
        CloudAdapter(
            _cloud_config(),
            _spec(ProviderKind.CLOUD),
            transport,
            sink,
        ).execute(_record(ProviderKind.CLOUD))
    assert sink.references == []
    assert not list((tmp_path / "blobs").rglob("*"))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"origin": "http://api.example.test"}, "https"),
        ({"origin": "https://evil.test"}, "origin"),
        ({"security_address": "203.0.113.99"}, "dns"),
        ({"security_hostname": "evil.test"}, "hostname"),
        ({"certificate_valid": False}, "certificate"),
    ],
)
def test_cloud_origin_tls_hostname_and_dns_fail_closed(change, message: str) -> None:
    transport = FakeTransport([_ok(_response(), ProviderKind.CLOUD)])
    origin = change.get("origin", "https://api.example.test")
    transport.security_address = change.get("security_address", transport.security_address)
    transport.security_hostname = change.get("security_hostname", transport.security_hostname)
    transport.certificate_valid = change.get("certificate_valid", transport.certificate_valid)
    config = _cloud_config(payload_changes={"origin": origin})
    with pytest.raises(Exception, match=message):
        CloudAdapter(config, _spec(ProviderKind.CLOUD), transport, MemoryRawOutputSink()).execute(
            _record(ProviderKind.CLOUD)
        )
    assert transport.calls == []


def test_redirect_never_forwards_credentials_cross_origin() -> None:
    transport = FakeTransport([TransportResponse(302, b"", "https://evil.test", {})])
    config = _cloud_config()
    with pytest.raises(Exception, match="redirect"):
        CloudAdapter(config, _spec(ProviderKind.CLOUD), transport, MemoryRawOutputSink()).execute(
            _record(ProviderKind.CLOUD)
        )
    assert len(transport.calls) == 1


def test_exactly_one_schema_correction_retry_preserves_both_raw_outputs() -> None:
    bad = json.dumps({"bad": True}).encode()
    transport = FakeTransport(
        [_ok(bad), _ok(_response())],
        security_address="127.0.0.1",
        security_hostname="127.0.0.1",
    )
    sink = MemoryRawOutputSink()
    adapter = OllamaAdapter(
        OllamaConfig("http://127.0.0.1:11434", "127.0.0.1"),
        _spec(ProviderKind.LOCAL),
        transport,
        sink,
    )
    result = adapter.execute(_record(ProviderKind.LOCAL)).value
    assert result.validated.distribution is not None
    assert len(result.attempts) == 2
    assert sink.payloads == [bad, _response()]
    assert len(transport.calls) == 2
    assert transport.calls[1].correction_code == "schema_fields"
    assert transport.calls[0].allow_redirects is False
    assert transport.calls[0].use_ambient_proxy is False
    assert transport.preflights[0].use_ambient_proxy is False


def test_raw_attempt_storage_obeys_inline_boundary_without_padding_or_byte_changes(
    tmp_path,
) -> None:
    sink = ContentAddressedRawOutputSink(ContentAddressedBlobStore(tmp_path / "blobs"))
    first = sink.publish(b"invalid")
    second = sink.publish(_response())
    assert first != second
    assert len(sink.references) == 2
    assert all(item.blob_reference is None for item in sink.references)
    assert all(item.inline_parts for item in sink.references)
    assert not list((tmp_path / "blobs").rglob("*.blob"))
    assert sink.read_raw(sink.references[0]) == b"invalid"
    assert sink.read_raw(sink.references[1]) == _response()


def test_raw_blob_sink_rejects_untyped_and_corrupt_references(tmp_path) -> None:
    with pytest.raises(ValueError, match="blob storage"):
        ContentAddressedRawOutputSink(object())  # type: ignore[arg-type]
    sink = ContentAddressedRawOutputSink(ContentAddressedBlobStore(tmp_path / "blobs"))
    with pytest.raises(ValueError, match="bytes"):
        sink.publish("bad")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="reference"):
        sink.read_raw(object())  # type: ignore[arg-type]
    for arguments in (
        ("bad", 1, (), None),
        ("0" * 64, 1, (), None),
        ("0" * 64, 1, (object(),), None),
    ):
        with pytest.raises(ValueError, match="reference"):
            RawOutputStorageReference(*arguments)  # type: ignore[arg-type]
    sink.publish(b"x")
    reference = sink.references[-1]
    with pytest.raises(ValueError, match="storage form"):
        replace(reference, inline_parts=(), blob_reference=None)
    large_sink = ContentAddressedRawOutputSink(ContentAddressedBlobStore(tmp_path / "large"))
    large_sink.publish(b"x" * 65_537)
    with pytest.raises(ValueError, match="blob identity"):
        replace(large_sink.references[0], raw_digest="0" * 64)
    raw_value = reference.to_dict()
    with pytest.raises(ValueError, match="fields"):
        RawOutputStorageReference.from_dict({})
    with pytest.raises(ValueError, match="schema"):
        RawOutputStorageReference.from_dict({**raw_value, "schema_version": "wrong"})
    with pytest.raises(ValueError, match="inline reference"):
        RawOutputStorageReference.from_dict({**raw_value, "inline_parts": {}})
    corrupt = replace(
        reference,
        inline_parts=(
            InlinePayload.from_value(
                {
                    "schema_version": "wrong",
                    "raw_digest": reference.raw_digest,
                    "part_index": 0,
                    "part_count": 1,
                    "raw_base64": "eA==",
                }
            ),
        ),
    )
    with pytest.raises(ValueError, match="chunks"):
        sink.read_raw(corrupt)
    bad_base64 = replace(
        reference,
        inline_parts=(
            InlinePayload.from_value(
                {
                    "schema_version": "strathmark-v3-llm-raw-output-chunk-v1",
                    "raw_digest": reference.raw_digest,
                    "part_index": 0,
                    "part_count": 1,
                    "raw_base64": "!!!",
                }
            ),
        ),
    )
    with pytest.raises(ValueError, match="chunk"):
        sink.read_raw(bad_base64)
    wrong_digest_part = InlinePayload.from_value(
        {
            "schema_version": "strathmark-v3-llm-raw-output-chunk-v1",
            "raw_digest": "0" * 64,
            "part_index": 0,
            "part_count": 1,
            "raw_base64": "eA==",
        }
    )
    with pytest.raises(ValueError, match="digest"):
        sink.read_raw(replace(reference, raw_digest="0" * 64, inline_parts=(wrong_digest_part,)))


def test_large_raw_output_is_stored_as_exact_required_blob(tmp_path) -> None:
    sink = ContentAddressedRawOutputSink(ContentAddressedBlobStore(tmp_path / "blobs"))
    raw = b"x" * (MAX_INLINE_PAYLOAD_BYTES + 1)
    sink.publish(raw)
    reference = sink.references[-1]
    assert reference.inline_parts == ()
    assert reference.blob_reference is not None
    assert reference.blob_reference.retention_class.value == "required"
    assert sink.read_raw(reference) == raw


@pytest.mark.parametrize(
    ("status", "reason"),
    [(429, "rate_limited"), (500, "provider_5xx"), (503, "provider_5xx")],
)
def test_provider_failure_matrix_is_typed_and_bounded(status: int, reason: str) -> None:
    transport = FakeTransport(
        [TransportResponse(status, b"failure", "http://127.0.0.1:11434", {})],
        security_address="127.0.0.1",
        security_hostname="127.0.0.1",
    )
    with pytest.raises(Exception, match=reason):
        OllamaAdapter(
            OllamaConfig("http://127.0.0.1:11434", "127.0.0.1"),
            _spec(ProviderKind.LOCAL),
            transport,
            MemoryRawOutputSink(),
        ).execute(_record(ProviderKind.LOCAL))
    assert len(transport.calls) == 1


def test_adapters_reject_unleased_work_before_provider_call() -> None:
    transport = FakeTransport(
        [_ok(_response())],
        security_address="127.0.0.1",
        security_hostname="127.0.0.1",
    )
    with pytest.raises(Exception, match="persisted repository"):
        OllamaAdapter(
            OllamaConfig("http://127.0.0.1:11434", "127.0.0.1"),
            _spec(ProviderKind.LOCAL),
            transport,
            MemoryRawOutputSink(),
        ).execute(replace(_record(ProviderKind.LOCAL), state=JobState.QUEUED))
    assert transport.calls == []


def test_repository_authority_and_adapter_constructor_defenses() -> None:
    with pytest.raises(ValueError, match="durable U7"):
        PersistedLeaseAuthority(object())
    record = _record(ProviderKind.LOCAL)
    member = _spec(ProviderKind.LOCAL)
    with pytest.raises(ValueError, match="repository-backed"):
        production_seal_claimed_llm_job(record, member, object())  # type: ignore[arg-type]
    record_authority = PersistedLeaseAuthority(_TEST_REPOSITORIES[record.job_id])
    with pytest.raises(ValueError, match="signed"):
        record_authority.verify_manifest(object(), record)
    with pytest.raises(ValueError, match="current persisted"):
        record_authority.verify_manifest(
            _cloud_config().deployment_manifest,
            replace(record, fencing_token=999),
        )
    error = ProviderCallError("read_timeout").bind_execution(member)
    assert error.bind_execution(member).provider_audit is error.provider_audit
    with pytest.raises(ValueError, match="lease authority"):
        ProductionOllamaAdapter(
            OllamaConfig("http://127.0.0.1:11434", "127.0.0.1"),
            object(),  # type: ignore[arg-type]
            member,
            FakeTransport([]),
            MemoryRawOutputSink(),
        )
    with pytest.raises(ValueError, match="concrete pinned loopback"):
        ProductionOllamaAdapter(
            OllamaConfig("http://127.0.0.1:11434", "127.0.0.1"),
            _test_lease_authority(),
            member,
            FakeTransport([]),
            MemoryRawOutputSink(),
        )
    config = _cloud_config()
    transport = PinnedHTTPSJSONTransport()
    with pytest.raises(ValueError, match="lease authority"):
        ProductionCloudAdapter(
            config,
            object(),  # type: ignore[arg-type]
            _spec(ProviderKind.CLOUD),
            transport,
            MemoryRawOutputSink(),
        )
    with pytest.raises(ValueError, match="concrete pinned"):
        ProductionCloudAdapter(
            config,
            _test_lease_authority(),
            _spec(ProviderKind.CLOUD),
            FakeTransport([]),
            MemoryRawOutputSink(),
        )


def test_adapter_overall_deadline_includes_preflight() -> None:
    local = OllamaAdapter(
        OllamaConfig("http://127.0.0.1:11434", "127.0.0.1"),
        _spec(ProviderKind.LOCAL),
        FakeTransport([]),
        MemoryRawOutputSink(),
    )
    cloud = CloudAdapter(
        _cloud_config(),
        _spec(ProviderKind.CLOUD),
        FakeTransport([]),
        MemoryRawOutputSink(),
    )
    with pytest.raises(ProviderCallError, match="overall_timeout"):
        local.execute(
            _record(
                ProviderKind.LOCAL,
                deadlines=DeadlineBudget(1_000, 1, 1, 1, 1_000),
                queue_elapsed_ms=1_000,
            )
        )
    with pytest.raises(ProviderCallError, match="overall_timeout"):
        cloud.execute(
            _record(
                ProviderKind.CLOUD,
                deadlines=DeadlineBudget(1_000, 1, 1, 1, 1_000),
                queue_elapsed_ms=1_000,
            )
        )


def test_ollama_adapter_runs_through_real_u7_claim_and_fenced_commit(tmp_path) -> None:
    signer = P256EphemeralSigner.generate("integrity-key:u11-coordinator")
    capacity = CapacityManifest(
        schema_version="strathmark-v3-job-capacity-v1",
        max_open_tournaments=1,
        max_round_entrants=48,
        max_field_entrants=12,
        max_plausible_qualifiers=48,
        max_context_cards=48,
        max_queued_jobs=8,
        max_receipt_bytes=1_048_576,
        max_blob_bytes=16_777_216,
        max_api_page_size=100,
        reserved_imminent_jobs=1,
        reserved_recovery_jobs=1,
        aging_interval_ms=1_000,
        aging_increment=125,
        lanes=(
            LaneCapacity(JobLane.HOT_FIELD, 2, 1),
            LaneCapacity(JobLane.INFERENCE, 4, 1),
            LaneCapacity(JobLane.LOOKUP_RECOVERY, 2, 1),
            LaneCapacity(JobLane.MAINTENANCE, 2, 1),
        ),
    )
    repository = DurableJobRepository(
        tmp_path / "jobs.sqlite3",
        capacity=capacity,
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
    )
    payload = create_llm_job_payload(
        _packet(ProviderKind.LOCAL),
        _spec(ProviderKind.LOCAL),
        DeadlineBudget(100, 200, 300, 50, 1_000),
    )
    repository.enqueue(
        JobRequest.create(
            job_id="job:u11-real",
            job_revision=1,
            idempotency_key="job_request:u11-real",
            job_kind=JobKind.LOCAL_LLM_CARD,
            lane=JobLane.INFERENCE,
            priority=JobPriority.PLAUSIBLE_QUALIFIER,
            capacity_use=CapacityUse(1, 1, 1, 1, 1, 1024, 4096, 10),
            payload=payload,
            evidence_digest="a" * 64,
            bundle_digest="b" * 64,
            retry_policy_version="retry.v1",
            created_at="2026-08-23T10:00:00.000Z",
            not_before_at="2026-08-23T10:00:00.000Z",
            hard_deadline_at="2026-08-23T10:01:00.000Z",
            max_attempts=2,
        )
    )
    transport = FakeTransport(
        [_ok(_response())], security_address="127.0.0.1", security_hostname="127.0.0.1"
    )
    success_sink = ContentAddressedRawOutputSink(
        ContentAddressedBlobStore(tmp_path / "success-blobs")
    )
    provider = _TestOnlyOllamaAdapter(
        OllamaConfig("http://127.0.0.1:11434", "127.0.0.1"),
        _spec(ProviderKind.LOCAL),
        transport,
        success_sink,
        PersistedLeaseAuthority(repository),
    )
    published = []
    outcome = DurableCoordinator(repository, retry_policy=RetryPolicy("retry.v1")).run_one(
        JobLane.INFERENCE,
        worker_id="worker:u11",
        lease_duration_ms=10_000,
        provider=provider,
        current_context=lambda _job: ("a" * 64, "b" * 64),
        publish=lambda job, response: published.append((job, response)),
        clock=lambda: "2026-08-23T10:00:00.050Z",
    )
    assert outcome.job is not None and outcome.job.state is JobState.SUCCEEDED
    assert len(published) == 1
    succeeded_audit = repository.provider_execution("job:u11-real", 1, 1)
    assert succeeded_audit.status == "succeeded"
    assert succeeded_audit.reason is None
    assert succeeded_audit.provider_id == "ollama"
    assert succeeded_audit.member_id == "qwen"
    assert succeeded_audit.attempts[0].validator_code == "valid_committed"
    assert succeeded_audit.attempts[0].storage_reference.raw_digest == (
        succeeded_audit.attempts[0].raw_digest
    )

    repository.enqueue(
        JobRequest.create(
            job_id="job:u11-real-failure",
            job_revision=1,
            idempotency_key="job_request:u11-real-failure",
            job_kind=JobKind.LOCAL_LLM_CARD,
            lane=JobLane.INFERENCE,
            priority=JobPriority.PLAUSIBLE_QUALIFIER,
            capacity_use=CapacityUse(1, 1, 1, 1, 1, 1024, 4096, 10),
            payload=payload,
            evidence_digest="a" * 64,
            bundle_digest="b" * 64,
            retry_policy_version="retry.v1",
            created_at="2026-08-23T10:00:00.100Z",
            not_before_at="2026-08-23T10:00:00.100Z",
            hard_deadline_at="2026-08-23T10:01:00.000Z",
            max_attempts=1,
        )
    )
    failure_sink = ContentAddressedRawOutputSink(
        ContentAddressedBlobStore(tmp_path / "failure-blobs")
    )
    failure_provider = _TestOnlyOllamaAdapter(
        OllamaConfig("http://127.0.0.1:11434", "127.0.0.1"),
        _spec(ProviderKind.LOCAL),
        FakeTransport(
            [TransportResponse(503, b"down", "", {})],
            security_address="127.0.0.1",
            security_hostname="127.0.0.1",
        ),
        failure_sink,
        PersistedLeaseAuthority(repository),
    )
    failed = DurableCoordinator(repository, retry_policy=RetryPolicy("retry.v1")).run_one(
        JobLane.INFERENCE,
        worker_id="worker:u11",
        lease_duration_ms=10_000,
        provider=failure_provider,
        current_context=lambda _job: ("a" * 64, "b" * 64),
        publish=lambda _job, _response: None,
        clock=lambda: "2026-08-23T10:00:00.150Z",
    )
    assert failed.job is not None and failed.job.terminal_reason == "retry_exhausted"
    assert isinstance(failed.provider_failure, ProviderCallError)
    assert failed.provider_failure.attempts[0].validator_code == "provider_5xx"
    assert len(failed.provider_failure.storage_references) == 1
    assert failure_sink.read_raw(failed.provider_failure.storage_references[0]) == b"down"
    failed_audit = repository.provider_execution("job:u11-real-failure", 1, 1)
    assert failed_audit.status == "failed"
    assert failed_audit.reason == "provider_5xx"
    assert failed_audit.attempts[0].storage_reference.byte_count == 4

    reopened = DurableJobRepository(
        tmp_path / "jobs.sqlite3",
        capacity=capacity,
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
    )
    assert reopened.provider_execution("job:u11-real", 1, 1) == succeeded_audit
    assert reopened.provider_execution("job:u11-real-failure", 1, 1) == failed_audit
    with pytest.raises(DurableJobError, match="fencing token"):
        reopened.provider_execution("job:u11-real", 1, 0)

    def rejects_corruption(name: str, statements: tuple[str, ...], message: str) -> None:
        corrupted = tmp_path / f"corrupt-{name}.sqlite3"
        shutil.copy2(tmp_path / "jobs.sqlite3", corrupted)
        with sqlite3.connect(corrupted) as connection:
            for statement in statements:
                connection.execute(statement)
            connection.commit()
        with pytest.raises(DurableJobError, match=message):
            with sqlite3.connect(corrupted) as connection:
                connection.row_factory = sqlite3.Row
                reopened._verify_connection(connection)

    rejects_corruption(
        "unknown",
        (
            "DROP TRIGGER v3_job_provider_executions_no_update",
            "UPDATE v3_job_provider_executions SET job_id='job:unknown' WHERE job_id='job:u11-real'",
        ),
        "unknown work",
    )
    rejects_corruption(
        "transition",
        (
            "DROP TRIGGER v3_job_provider_executions_no_update",
            "UPDATE v3_job_provider_executions SET fencing_token=99 WHERE job_id='job:u11-real'",
        ),
        "transition",
    )
    rejects_corruption(
        "status",
        (
            "DROP TRIGGER v3_job_provider_executions_no_update",
            "UPDATE v3_job_provider_executions SET status='failed', reason='tampered' "
            "WHERE job_id='job:u11-real'",
        ),
        "status",
    )
    rejects_corruption(
        "json",
        (
            "DROP TRIGGER v3_job_provider_executions_no_update",
            "UPDATE v3_job_provider_executions SET execution_json='{' WHERE job_id='job:u11-real'",
        ),
        "integrity verification",
    )
    rejects_corruption(
        "material",
        (
            "DROP TRIGGER v3_job_provider_executions_no_update",
            "UPDATE v3_job_provider_executions SET member_id='other' WHERE job_id='job:u11-real'",
        ),
        "material differs",
    )
    rejects_corruption(
        "count",
        (
            "DROP TRIGGER v3_job_provider_storage_refs_no_delete",
            "DELETE FROM v3_job_provider_storage_refs WHERE job_id='job:u11-real'",
        ),
        "normalized audit rows",
    )
    rejects_corruption(
        "normalized",
        (
            "DROP TRIGGER v3_job_provider_attempts_no_update",
            "UPDATE v3_job_provider_attempts SET validator_code='tampered' "
            "WHERE job_id='job:u11-real'",
        ),
        "normalized audit material",
    )
    with sqlite3.connect(tmp_path / "jobs.sqlite3") as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO v3_job_provider_executions SELECT * FROM "
                "v3_job_provider_executions WHERE job_id=?",
                ("job:u11-real",),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE v3_job_provider_executions SET reason='tampered' WHERE job_id=?",
                ("job:u11-real-failure",),
            )

    repository.enqueue(
        JobRequest.create(
            job_id="job:u11-rollback",
            job_revision=1,
            idempotency_key="job_request:u11-rollback",
            job_kind=JobKind.LOCAL_LLM_CARD,
            lane=JobLane.INFERENCE,
            priority=JobPriority.PLAUSIBLE_QUALIFIER,
            capacity_use=CapacityUse(1, 1, 1, 1, 1, 1024, 4096, 10),
            payload=payload,
            evidence_digest="a" * 64,
            bundle_digest="b" * 64,
            retry_policy_version="retry.v1",
            created_at="2026-08-23T10:00:00.200Z",
            not_before_at="2026-08-23T10:00:00.200Z",
            hard_deadline_at="2026-08-23T10:01:00.000Z",
            max_attempts=1,
        )
    )
    rollback_sink = ContentAddressedRawOutputSink(
        ContentAddressedBlobStore(tmp_path / "rollback-blobs")
    )
    rollback_provider = _TestOnlyOllamaAdapter(
        OllamaConfig("http://127.0.0.1:11434", "127.0.0.1"),
        _spec(ProviderKind.LOCAL),
        FakeTransport(
            [_ok(_response())],
            security_address="127.0.0.1",
            security_hostname="127.0.0.1",
        ),
        rollback_sink,
        PersistedLeaseAuthority(repository),
    )
    captured = []

    def crash_after_audit(_job, response):
        captured.append(response)
        raise RuntimeError("simulated publication crash")

    with pytest.raises(RuntimeError, match="simulated publication crash"):
        DurableCoordinator(repository, retry_policy=RetryPolicy("retry.v1")).run_one(
            JobLane.INFERENCE,
            worker_id="worker:u11",
            lease_duration_ms=10_000,
            provider=rollback_provider,
            current_context=lambda _job: ("a" * 64, "b" * 64),
            publish=crash_after_audit,
            clock=lambda: "2026-08-23T10:00:00.250Z",
        )
    rollback_record = repository.get("job:u11-rollback", 1)
    assert rollback_record.state is JobState.LEASED
    with pytest.raises(JobConflict, match="does not exist"):
        repository.provider_execution("job:u11-rollback", 1, rollback_record.fencing_token)
    assert captured[0].provider_audit is not None
    commit_arguments = {
        "worker_id": "worker:u11",
        "fencing_token": rollback_record.fencing_token,
        "result_digest": captured[0].result_digest,
        "current_context": lambda _connection, _job: ("a" * 64, "b" * 64),
        "clock": lambda: "2026-08-23T10:00:00.260Z",
    }
    with pytest.raises(DurableJobError, match="typed"):
        repository.commit_success(
            rollback_record.job_id,
            rollback_record.job_revision,
            **commit_arguments,
            provider_audit=object(),
        )
    with pytest.raises(JobConflict, match="outcome"):
        repository.commit_success(
            rollback_record.job_id,
            rollback_record.job_revision,
            **commit_arguments,
            provider_audit=replace(
                captured[0].provider_audit, status="failed", reason="provider_error"
            ),
        )
    with pytest.raises(JobConflict, match="pins"):
        repository.commit_success(
            rollback_record.job_id,
            rollback_record.job_revision,
            **commit_arguments,
            provider_audit=replace(captured[0].provider_audit, provider_id="other"),
        )


def test_queue_deadline_is_enforced_from_persisted_lease_timestamps() -> None:
    record = _record(
        ProviderKind.LOCAL,
        deadlines=DeadlineBudget(100, 100, 100, 100, 1_000),
        queue_elapsed_ms=101,
    )
    with pytest.raises(ValueError, match="queue deadline"):
        seal_claimed_llm_job(record, _spec(ProviderKind.LOCAL))


def test_sealed_job_factory_rejects_every_tampered_durable_boundary() -> None:
    member = _spec(ProviderKind.LOCAL)
    original = _record(ProviderKind.LOCAL)
    with pytest.raises(ValueError, match="current persisted"):
        seal_claimed_llm_job(replace(original, fencing_token=999), member)

    packet_value = _packet(ProviderKind.LOCAL).to_dict()
    malformed_packets = (
        ({}, "packet schema"),
        ({**packet_value, "observations": {}}, "observations"),
        ({**packet_value, "observations": [{}]}, "observation schema"),
        ({**packet_value, "target_context": []}, "target context"),
    )
    for value, reason in malformed_packets:
        with pytest.raises(ValueError, match=reason):
            council._provider_packet_from_dict(value)

    base_payload = create_llm_job_payload(
        _packet(ProviderKind.LOCAL), member, DeadlineBudget(100, 100, 100, 100, 1_000)
    )
    payload_cases = (
        ({**base_payload, "schema_version": "wrong"}, "payload schema"),
        ({**base_payload, "member_manifest_digest": "0" * 64}, "member manifest"),
        (
            {
                **base_payload,
                "provider_packet": {**base_payload["provider_packet"], "provider_id": "other"},
            },
            "provider differs",
        ),
        ({**base_payload, "deadlines": []}, "deadline schema"),
    )
    for payload, reason in payload_cases:
        record = _record(
            ProviderKind.LOCAL,
            member=member,
            payload_override=payload,
        )
        with pytest.raises(ValueError, match=reason):
            seal_claimed_llm_job(record, member)
    with pytest.raises(ValueError, match="persisted leased"):
        council._remaining_overall_ms(object())
    malformed_deadlines = replace(
        original,
        payload_json=canonical_bytes({"deadlines": []}).decode(),
    )
    with pytest.raises(ValueError, match="deadline budgets"):
        council._remaining_overall_ms(malformed_deadlines)

    with pytest.raises(ValueError, match="typed canonical"):
        create_llm_job_payload(object(), member, DeadlineBudget(1, 1, 1, 1, 2))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="provider or deadline"):
        create_llm_job_payload(_packet(ProviderKind.CLOUD), member, DeadlineBudget(1, 1, 1, 1, 2))
    assert TransportResponse.ok(b"x").body == b"x"

    sealed = _job(ProviderKind.LOCAL)
    for field_name, value, reason in (
        ("job_revision", 0, "fencing lease"),
        ("member", object(), "typed member"),
        ("prompt", b"", "prompt"),
        ("expected_evidence_refs", [], "allowlists"),
        ("queue_elapsed_ms", True, "queue duration"),
    ):
        object.__setattr__(sealed, field_name, value)
        with pytest.raises(ValueError, match=reason):
            sealed._validate()
        object.__setattr__(sealed, field_name, getattr(_job(ProviderKind.LOCAL), field_name))

    queued = _record(ProviderKind.LOCAL, member=member, claim=False)
    with pytest.raises(ValueError, match="currently leased"):
        seal_claimed_llm_job(queued, member)


def test_runner_starts_cloud_then_runs_local_models_strictly_sequentially() -> None:
    trace: list[str] = []
    second_local_finished = threading.Event()

    def executed(member: LLMMemberSpec) -> ExecutedMember:
        return _audited_executed(member)

    class Adapter:
        def __init__(self, name: str, member: LLMMemberSpec) -> None:
            self.name = name
            self.member = member

        @property
        def lease_authority(self):
            return _test_lease_authority()

        def execute(self, job: JobRecord):
            trace.append(f"start:{self.name}")
            if self.name == "cloud":
                assert second_local_finished.wait(2)
            trace.append(f"end:{self.name}")
            if self.name == "local2":
                second_local_finished.set()
            value = executed(self.member)
            return council.ProviderResponse(
                value.audit.raw_response_digest,
                job.evidence_digest,
                job.bundle_digest,
                value,
                value.execution_audit,
            )

    cloud_member = _evaluated(_spec(ProviderKind.CLOUD))
    local1_member = _evaluated(_spec(ProviderKind.LOCAL))
    local2_member = _repinned(
        local1_member,
        member_id="ministral",
        family="ministral3",
        provider_id="ollama_ministral",
    )
    cloud = _record(ProviderKind.CLOUD, member=cloud_member)
    local1 = _record(ProviderKind.LOCAL, member=local1_member)
    local2 = _record(ProviderKind.LOCAL, job_id="job:two", member=local2_member)
    result = _CandidateEvaluationHarness().evaluate(
        local_jobs=(local1, local2),
        cloud_job=cloud,
        local_adapters=(Adapter("local1", local1_member), Adapter("local2", local2_member)),
        cloud_adapter=Adapter("cloud", cloud_member),
        reliability_weights={"qwen": "1", "ministral": "1", "cloud": "1"},
        context_weights={"qwen": "1", "ministral": "1", "cloud": "1"},
        clock=lambda job: job.lease_acquired_at,
    )
    assert result.valid_member_count == 3, [
        (item.member_id, item.unavailable_code) for item in result.outcomes
    ]
    assert trace.index("start:cloud") < trace.index("start:local2")
    assert trace.index("end:local1") < trace.index("start:local2")


def test_runner_composes_three_real_durable_provider_adapters(tmp_path, monkeypatch) -> None:
    local1_member = _evaluated(_spec(ProviderKind.LOCAL))
    local2_member = _repinned(
        local1_member,
        member_id="ministral",
        provider_id="ollama_ministral",
        family="ministral3",
    )
    cloud_member = _evaluated(_spec(ProviderKind.CLOUD))
    test_authority = _test_lease_authority()

    def leased(kind, member, name):
        signer = P256EphemeralSigner.generate(f"integrity-key:{name}")
        repository = DurableJobRepository(
            tmp_path / f"{name}.sqlite3",
            capacity=test_authority._repository.capacity,
            signer=signer,
            trust_store=IntegrityTrustStore((signer.identity, *_CLOUD_TRUSTED_IDENTITIES)),
        )
        payload = create_llm_job_payload(
            _packet(kind, member),
            member,
            DeadlineBudget(100, 2_000, 2_000, 500, 5_000),
        )
        repository.enqueue(
            JobRequest.create(
                job_id=f"job:{name}",
                job_revision=1,
                idempotency_key=f"job_request:{name}",
                job_kind=(
                    JobKind.CLOUD_LLM_CARD if kind is ProviderKind.CLOUD else JobKind.LOCAL_LLM_CARD
                ),
                lane=JobLane.INFERENCE,
                priority=JobPriority.PLAUSIBLE_QUALIFIER,
                capacity_use=CapacityUse(1, 1, 1, 1, 1, 1_024, 4_096, 10),
                payload=payload,
                evidence_digest="a" * 64,
                bundle_digest="b" * 64,
                retry_policy_version="retry.v1",
                created_at="2026-08-23T10:00:00.000Z",
                not_before_at="2026-08-23T10:00:00.000Z",
                hard_deadline_at="2026-08-23T10:01:00.000Z",
                max_attempts=1,
            )
        )
        record = repository.claim(
            JobLane.INFERENCE,
            worker_id=f"worker:{name}",
            clock=lambda: "2026-08-23T10:00:00.001Z",
            lease_duration_ms=10_000,
        )
        assert record is not None
        return record, PersistedLeaseAuthority(repository), repository

    local1_record, local1_authority, local1_repository = leased(
        ProviderKind.LOCAL, local1_member, "local1"
    )
    local2_record, local2_authority, local2_repository = leased(
        ProviderKind.LOCAL, local2_member, "local2"
    )
    certificate_path, key_path, client_context = _test_tls_material(tmp_path)
    with (
        _provider_server(local1_member, _response(40_000)) as local1_port,
        _provider_server(local2_member, _response(41_000)) as local2_port,
        _provider_server(
            cloud_member,
            _response(39_000),
            tls_material=(certificate_path, key_path),
        ) as cloud_port,
    ):
        monkeypatch.setattr(ssl, "create_default_context", lambda: client_context)
        local1 = ProductionOllamaAdapter(
            OllamaConfig(f"http://127.0.0.1:{local1_port}", "127.0.0.1"),
            local1_authority,
            local1_member,
            PinnedLoopbackHTTPTransport(),
            ContentAddressedRawOutputSink(ContentAddressedBlobStore(tmp_path / "local1-blobs")),
        )
        local2 = ProductionOllamaAdapter(
            OllamaConfig(f"http://127.0.0.1:{local2_port}", "127.0.0.1"),
            local2_authority,
            local2_member,
            PinnedLoopbackHTTPTransport(),
            ContentAddressedRawOutputSink(ContentAddressedBlobStore(tmp_path / "local2-blobs")),
        )
        cloud_config = _cloud_config(
            payload_changes={
                "origin": f"https://localhost:{cloud_port}",
                "hostname": "localhost",
                "allowed_addresses": ["127.0.0.1"],
            }
        )
        cloud_record, cloud_lease_authority, cloud_repository = leased(
            ProviderKind.CLOUD, cloud_member, "cloud"
        )
        cloud = ProductionCloudAdapter(
            cloud_config,
            cloud_lease_authority,
            cloud_member,
            PinnedHTTPSJSONTransport(),
            ContentAddressedRawOutputSink(ContentAddressedBlobStore(tmp_path / "cloud-blobs")),
        )
        result = _CandidateEvaluationHarness().evaluate(
            local_jobs=(local1_record, local2_record),
            cloud_job=cloud_record,
            local_adapters=(local1, local2),
            cloud_adapter=cloud,
            reliability_weights={"qwen": "1", "ministral": "1", "cloud": "1"},
            context_weights={"qwen": "1", "ministral": "1", "cloud": "1"},
            clock=lambda job: job.lease_acquired_at,
        )
    assert result.valid_member_count == 3, [
        (item.member_id, item.unavailable_code) for item in result.outcomes
    ]
    assert isinstance(result, CandidateEvaluationReport)
    assert result.authority_class == "test_ephemeral"
    assert result.candidate_status is council.CandidateStatus.CANDIDATE
    assert not hasattr(result, "forecast")
    assert {member_id for member_id, _receipt in result.sealed_member_receipts} == {
        "qwen",
        "ministral",
        "cloud",
    }
    with pytest.raises(ValueError, match="DiagnosticCouncilMixture"):
        council.seal_council_receipt(result)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="three declared member outcomes"):
        council.aggregate_council(result)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unavailable until U19"):
        council.aggregate_council(result.outcomes)
    reconstructed = tuple(replace(outcome) for outcome in result.outcomes)
    with pytest.raises(ValueError, match="unavailable until U19"):
        council.aggregate_council(reconstructed)
    assert result.availability.value == "normal"
    for record, repository in (
        (local1_record, local1_repository),
        (local2_record, local2_repository),
        (cloud_record, cloud_repository),
    ):
        assert repository.get(record.job_id, record.job_revision).state is JobState.SUCCEEDED
        assert (
            repository.provider_execution(
                record.job_id, record.job_revision, record.fencing_token
            ).status
            == "succeeded"
        )


def test_transport_response_repr_hides_provider_body_and_headers() -> None:
    response = TransportResponse(200, b"provider-secret", "", {"authorization": "secret"})

    assert "provider-secret" not in repr(response)
    assert "authorization" not in repr(response)
    assert "secret" not in repr(response)


def test_unpromoted_stub_exercises_packet_rotation_but_is_not_promotion_evidence() -> None:
    member = _spec(ProviderKind.LOCAL)
    assert member.status is council.CandidateStatus.CANDIDATE
    base = _packet(ProviderKind.LOCAL, member)
    old = replace(
        base,
        subject_token="subject_oldrotation",
        observations=(replace(base.observations[0], evidence_ref="obs_oldrotation"),),
    )
    new = replace(
        base,
        subject_token="subject_newrotation",
        observations=(replace(base.observations[0], evidence_ref="obs_newrotation"),),
    )

    class TokenAwareCandidateTransport(FakeTransport):
        def __init__(self):
            super().__init__([], security_address="127.0.0.1", security_hostname="127.0.0.1")
            self.tokens = []

        def send(self, request):
            self.calls.append(request)
            outer = json.loads(request.body)
            packet_value = json.loads(outer["prompt"].split("UNTRUSTED_JSON_DATA\n", 1)[1])
            token = packet_value["subject_token"]
            if not token.startswith("subject_"):
                raise AssertionError("candidate received an invalid scoped token")
            self.tokens.append(token)
            observation = packet_value["observations"][0]
            response = json.loads(_response(observation["raw_time_ms"]))
            response["evidence_refs"] = [observation["evidence_ref"]]
            return _ok(canonical_bytes(response))

    distributions = []
    observed_tokens = []
    for packet in (old, new):
        transport = TokenAwareCandidateTransport()
        response = OllamaAdapter(
            OllamaConfig("http://127.0.0.1:11434", "127.0.0.1"),
            member,
            transport,
            MemoryRawOutputSink(),
        ).execute(_record(ProviderKind.LOCAL, member=member, packet=packet))
        distributions.append(response.value.validated.distribution)
        observed_tokens.extend(transport.tokens)
    assert observed_tokens == ["subject_oldrotation", "subject_newrotation"]
    assert distributions[0] == distributions[1]


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (401, "credential_expired"),
        (403, "credential_expired"),
        (404, "provider_http_error"),
        (507, "provider_oom"),
        (True, "invalid_http_status"),
    ],
)
def test_remaining_http_failures_are_closed(status, reason: str) -> None:
    transport = FakeTransport(
        [TransportResponse(status, b"body", "", {})],
        security_address="127.0.0.1",
        security_hostname="127.0.0.1",
    )
    with pytest.raises(ProviderCallError, match=reason):
        OllamaAdapter(
            OllamaConfig("http://127.0.0.1:11434", "127.0.0.1"),
            _spec(ProviderKind.LOCAL),
            transport,
            MemoryRawOutputSink(),
        ).execute(_record(ProviderKind.LOCAL))


def test_response_loop_transport_late_sink_and_second_invalid_failures(
    monkeypatch,
) -> None:
    job = _job(ProviderKind.LOCAL)

    class BadSink:
        def publish(self, payload: bytes) -> str:
            return "0" * 64

    cases = [
        ([object()], MemoryRawOutputSink(), "invalid_transport_response"),
        ([TimeoutError()], MemoryRawOutputSink(), "read_timeout"),
        (
            [ProviderCallError("process_failure")],
            MemoryRawOutputSink(),
            "process_failure",
        ),
        ([OSError()], MemoryRawOutputSink(), "transport_failure"),
        ([_ok(_response())], BadSink(), "raw_output_sink_digest_mismatch"),
        (
            [TransportResponse(200, _response(), "http://evil.test", {})],
            MemoryRawOutputSink(),
            "redirect_rejected",
        ),
        (
            [TransportResponse(200, _response(), "", {}, latency_ms=1_001)],
            MemoryRawOutputSink(),
            "late_response",
        ),
        (
            [_ok(b"{}"), _ok(b"{}")],
            MemoryRawOutputSink(),
            "invalid_output_after_correction",
        ),
    ]
    for responses, sink, reason in cases:
        with pytest.raises(ProviderCallError, match=reason):
            execute_response_loop(
                job,
                origin="http://127.0.0.1:11434",
                headers=(),
                transport=FakeTransport(responses),  # type: ignore[arg-type]
                sink=sink,
            )
    tight = _job(ProviderKind.LOCAL, deadlines=DeadlineBudget(1, 1, 1, 9, 10))
    with pytest.raises(ProviderCallError, match="correction_deadline_exhausted"):
        execute_response_loop(
            tight,
            origin="http://127.0.0.1:11434",
            headers=(),
            transport=FakeTransport([replace(_ok(b"{}"), latency_ms=1)]),
            sink=MemoryRawOutputSink(),
        )
    moments = iter((0.0, 2.0))
    monkeypatch.setattr(council.time, "monotonic", lambda: next(moments))
    with pytest.raises(ProviderCallError, match="overall_timeout"):
        execute_response_loop(
            job,
            origin="http://127.0.0.1:11434",
            headers=(),
            transport=FakeTransport([]),
            sink=MemoryRawOutputSink(),
        )


def test_failed_correction_and_http_errors_return_immutable_attempt_audit(
    tmp_path,
) -> None:
    for responses, expected_count in (
        ([_ok(b"{}"), _ok(b"{}")], 2),
        ([TransportResponse(503, b"down", "", {})], 1),
    ):
        sink = ContentAddressedRawOutputSink(
            ContentAddressedBlobStore(tmp_path / str(expected_count))
        )
        with pytest.raises(ProviderCallError) as captured:
            execute_response_loop(
                _job(ProviderKind.LOCAL),
                origin="http://127.0.0.1:11434",
                headers=(),
                transport=FakeTransport(responses),
                sink=sink,
            )
        assert len(captured.value.attempts) == expected_count
        assert len(captured.value.storage_references) == expected_count
        expected_payloads = (b"{}", b"{}") if expected_count == 2 else (b"down",)
        assert (
            tuple(sink.read_raw(item) for item in captured.value.storage_references)
            == expected_payloads
        )


@pytest.mark.parametrize(("size", "uses_blob"), ((65_535, False), (65_536, False), (65_537, True)))
def test_raw_output_storage_obeys_exact_u7_inline_boundary(
    tmp_path, size: int, uses_blob: bool
) -> None:
    sink = ContentAddressedRawOutputSink(ContentAddressedBlobStore(tmp_path / str(size)))
    raw = b"x" * size
    sink.publish(raw)
    reference = sink.references[0]
    assert (reference.blob_reference is not None) is uses_blob
    assert sink.read_raw(reference) == raw
    assert RawOutputStorageReference.from_dict(reference.to_dict()) == reference


def test_provider_execution_audit_contract_rejects_noncanonical_or_mismatched_material(
    tmp_path,
) -> None:
    sink = ContentAddressedRawOutputSink(ContentAddressedBlobStore(tmp_path / "audit-contract"))
    digest = sink.publish(b"audit")
    reference = sink.references[0]
    storage = ProviderStorageAudit.create(reference)
    attempt = ProviderAttemptAudit(1, digest, "valid_committed", True, storage)
    pin = {"member_manifest_digest": "1" * 64}
    pin_json = canonical_bytes(pin).decode("utf-8")
    execution = ProviderExecutionAudit(
        "ollama", "qwen", pin_json, canonical_digest(pin), "succeeded", None, (attempt,)
    )
    assert ProviderExecutionAudit.from_dict(execution.to_dict()) == execution

    invalid = (
        lambda: ProviderStorageAudit.create(object()),
        lambda: ProviderStorageAudit(
            "0" * 64, True, storage.reference_json, storage.reference_digest
        ),
        lambda: ProviderStorageAudit("bad", 1, storage.reference_json, storage.reference_digest),
        lambda: ProviderStorageAudit("0" * 64, 1, 1, "0" * 64),
        lambda: ProviderStorageAudit("0" * 64, 1, "{", "0" * 64),
        lambda: ProviderStorageAudit("0" * 64, 1, '{"b":1, "a":2}', "0" * 64),
        lambda: ProviderAttemptAudit(1, digest, "BAD", True, storage),
        lambda: ProviderAttemptAudit(1, digest, "valid", 1, storage),
        lambda: ProviderAttemptAudit(1, digest, "valid", True, object()),
        lambda: ProviderAttemptAudit(1, "0" * 64, "valid", True, storage),
        lambda: ProviderExecutionAudit(
            "BAD", "qwen", pin_json, canonical_digest(pin), "failed", "error", (attempt,)
        ),
        lambda: ProviderExecutionAudit(
            "ollama", "qwen", pin_json, canonical_digest(pin), "other", "error", (attempt,)
        ),
        lambda: ProviderExecutionAudit(
            "ollama", "qwen", pin_json, canonical_digest(pin), "succeeded", "error", (attempt,)
        ),
        lambda: ProviderExecutionAudit(
            "ollama", "qwen", pin_json, canonical_digest(pin), "failed", None, (attempt,)
        ),
        lambda: ProviderExecutionAudit(
            "ollama", "qwen", pin_json, canonical_digest(pin), "failed", "BAD", (attempt,)
        ),
        lambda: ProviderExecutionAudit(
            "ollama", "qwen", pin_json, canonical_digest(pin), "failed", "error", []
        ),
        lambda: ProviderExecutionAudit(
            "ollama", "qwen", pin_json, canonical_digest(pin), "succeeded", None, ()
        ),
        lambda: ProviderExecutionAudit(
            "ollama", "qwen", pin_json, canonical_digest(pin), "failed", "error", (object(),)
        ),
        lambda: ProviderExecutionAudit(
            "ollama",
            "qwen",
            pin_json,
            canonical_digest(pin),
            "failed",
            "error",
            (replace(attempt, ordinal=2),),
        ),
        lambda: ProviderExecutionAudit.from_dict({}),
        lambda: ProviderExecutionAudit.from_dict(
            {**execution.to_dict(), "schema_version": "wrong"}
        ),
        lambda: ProviderExecutionAudit.from_dict({**execution.to_dict(), "attempts": {}}),
        lambda: ProviderExecutionAudit.from_dict({**execution.to_dict(), "attempts": [{}]}),
        lambda: ProviderExecutionAudit.from_dict(
            {
                **execution.to_dict(),
                "status": "failed",
                "reason": "error",
                "attempts": [{**execution.to_dict()["attempts"][0], "storage_reference": None}],
            }
        ),
    )
    for construct in invalid:
        with pytest.raises(DurableJobError):
            construct()


def test_provider_metadata_is_recorded_without_alias_substitution() -> None:
    response = TransportResponse(
        200,
        _response(),
        "",
        {},
        latency_ms=7,
        provider_model_version="qwen3.5-9b-q4_k_m",
        provider_fingerprint="2" * 64,
        api_revision="ollama:0.12.3",
        canary_digest="3" * 64,
    )
    transport = FakeTransport(
        [response], security_address="127.0.0.1", security_hostname="127.0.0.1"
    )
    result = (
        OllamaAdapter(
            OllamaConfig("http://127.0.0.1:11434", "127.0.0.1"),
            _spec(ProviderKind.LOCAL),
            transport,
            MemoryRawOutputSink(),
        )
        .execute(_record(ProviderKind.LOCAL))
        .value
    )
    assert result.audit.provider_fingerprint == "2" * 64
    assert result.audit.api_revision == "ollama:0.12.3"
    assert result.audit.canary_digest == "3" * 64
    assert result.audit.latency_ms == 7


@pytest.mark.parametrize(
    "field_name",
    ["provider_model_version", "provider_fingerprint", "api_revision", "canary_digest"],
)
def test_changed_provider_identity_cannot_satisfy_pinned_job(field_name: str) -> None:
    response = replace(_ok(_response()), **{field_name: "changed"})
    transport = FakeTransport(
        [response], security_address="127.0.0.1", security_hostname="127.0.0.1"
    )
    with pytest.raises(ProviderCallError, match=f"{field_name}_mismatch"):
        OllamaAdapter(
            OllamaConfig("http://127.0.0.1:11434", "127.0.0.1"),
            _spec(ProviderKind.LOCAL),
            transport,
            MemoryRawOutputSink(),
        ).execute(_record(ProviderKind.LOCAL))


@pytest.mark.parametrize(
    "config",
    [
        OllamaConfig("https://127.0.0.1:11434", "127.0.0.1"),
        OllamaConfig("http://user@127.0.0.1:11434", "127.0.0.1"),
        OllamaConfig("http://127.0.0.1:11434/path", "127.0.0.1"),
        OllamaConfig("http://127.0.0.1:11434?x=1", "127.0.0.1"),
        OllamaConfig("http://127.0.0.1:11434#x", "127.0.0.1"),
        OllamaConfig("http://127.0.0.1:11434", "localhost"),
        OllamaConfig("http://192.0.2.1:11434", "192.0.2.1"),
    ],
)
def test_ollama_accepts_only_exact_loopback_origin(config: OllamaConfig) -> None:
    with pytest.raises(ProviderCallError):
        OllamaAdapter(
            config, _spec(ProviderKind.LOCAL), FakeTransport([]), MemoryRawOutputSink()
        ).execute(_record(ProviderKind.LOCAL))


def test_ollama_boundary_and_preflight_failures_are_typed() -> None:
    with pytest.raises(ValueError, match="configuration"):
        ProductionOllamaAdapter(
            object(),  # type: ignore[arg-type]
            _test_lease_authority(),
            _spec(ProviderKind.LOCAL),
            PinnedLoopbackHTTPTransport(),
            MemoryRawOutputSink(),
        )
    with pytest.raises(ValueError, match="pinned local"):
        ProductionOllamaAdapter(
            OllamaConfig("http://127.0.0.1:11434", "127.0.0.1"),
            _test_lease_authority(),
            _spec(ProviderKind.CLOUD),
            PinnedLoopbackHTTPTransport(),
            MemoryRawOutputSink(),
        )
    production = ProductionOllamaAdapter(
        OllamaConfig("http://127.0.0.1:11434", "127.0.0.1"),
        _test_lease_authority(),
        _spec(ProviderKind.LOCAL),
        PinnedLoopbackHTTPTransport(),
        MemoryRawOutputSink(),
    )
    with pytest.raises(ProviderCallError, match="overall_timeout"):
        production.execute(
            _record(
                ProviderKind.LOCAL,
                deadlines=DeadlineBudget(1_000, 1, 1, 1, 1_000),
                queue_elapsed_ms=1_000,
            )
        )
    with pytest.raises(ValueError, match="configuration"):
        OllamaAdapter(object(), _spec(ProviderKind.LOCAL), FakeTransport([]), MemoryRawOutputSink())  # type: ignore[arg-type]
    adapter = OllamaAdapter(
        OllamaConfig("http://127.0.0.1:11434", "127.0.0.1"),
        _spec(ProviderKind.LOCAL),
        FakeTransport([]),
        MemoryRawOutputSink(),
    )
    with pytest.raises(ValueError, match="persisted"):
        adapter.execute(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="kind"):
        adapter.execute(_record(ProviderKind.CLOUD))

    class PreflightTransport(FakeTransport):
        failure: BaseException | None = None
        result: object | None = None

        def preflight(self, request):
            if self.failure is not None:
                raise self.failure
            return self.result

    for failure, result, reason in (
        (TimeoutError(), None, "connect_timeout"),
        (ProviderCallError("dead_host"), None, "dead_host"),
        (OSError(), None, "transport_preflight_failure"),
        (None, object(), "invalid_transport_security"),
    ):
        transport = PreflightTransport([])
        transport.failure = failure
        transport.result = result
        with pytest.raises(ProviderCallError, match=reason):
            OllamaAdapter(
                OllamaConfig("http://127.0.0.1:11434", "127.0.0.1"),
                _spec(ProviderKind.LOCAL),
                transport,
                MemoryRawOutputSink(),
            ).execute(_record(ProviderKind.LOCAL))
    for security, reason in (
        (TransportSecurity("127.0.0.2", "127.0.0.1", True), "dns"),
        (TransportSecurity("127.0.0.1", "wrong", True), "hostname"),
        (TransportSecurity("127.0.0.1", "127.0.0.1", True, ""), "connection"),
    ):
        transport = PreflightTransport([])
        transport.result = security
        with pytest.raises(ProviderCallError, match=reason):
            OllamaAdapter(
                OllamaConfig("http://127.0.0.1:11434", "127.0.0.1"),
                _spec(ProviderKind.LOCAL),
                transport,
                MemoryRawOutputSink(),
            ).execute(_record(ProviderKind.LOCAL))


def test_cloud_configuration_and_preflight_failure_matrix() -> None:
    valid = _cloud_config()
    with pytest.raises(ValueError, match="configuration"):
        ProductionCloudAdapter(
            object(),  # type: ignore[arg-type]
            _test_lease_authority(),
            _spec(ProviderKind.CLOUD),
            PinnedHTTPSJSONTransport(),
            MemoryRawOutputSink(),
        )
    with pytest.raises(ValueError, match="pinned cloud"):
        ProductionCloudAdapter(
            valid,
            _test_lease_authority(),
            _spec(ProviderKind.LOCAL),
            PinnedHTTPSJSONTransport(),
            MemoryRawOutputSink(),
        )
    mismatch = _cloud_config(payload_changes={"api_revision": "api:different"})
    with pytest.raises(ProviderCallError, match="revision_pin_mismatch"):
        ProductionCloudAdapter(
            mismatch,
            _test_lease_authority(),
            _spec(ProviderKind.CLOUD),
            PinnedHTTPSJSONTransport(),
            MemoryRawOutputSink(),
        ).execute(_record(ProviderKind.CLOUD))
    with pytest.raises(ValueError, match="configuration"):
        CloudAdapter(object(), _spec(ProviderKind.CLOUD), FakeTransport([]), MemoryRawOutputSink())  # type: ignore[arg-type]
    adapter = CloudAdapter(
        valid, _spec(ProviderKind.CLOUD), FakeTransport([]), MemoryRawOutputSink()
    )
    with pytest.raises(ValueError, match="persisted"):
        adapter.execute(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="kind"):
        adapter.execute(_record(ProviderKind.LOCAL))
    cases = (
        (replace(valid, credential=""), "credential"),
        (_cloud_config(payload_changes={"api_revision": ""}), "revision"),
        (_cloud_config(payload_changes={"allowed_addresses": []}), "allowlist"),
        (_cloud_config(payload_changes={"allowed_addresses": ["bad"]}), "allowlist"),
        (_cloud_config(payload_changes={"origin": 1}), "configuration"),
        (_cloud_config(payload_changes={"hostname": 1}), "configuration"),
        (
            _cloud_config(payload_changes={"origin": "https://user@api.example.test"}),
            "origin",
        ),
        (
            _cloud_config(payload_changes={"origin": "https://api.example.test/path"}),
            "origin",
        ),
        (
            _cloud_config(payload_changes={"origin": "https://api.example.test?x=1"}),
            "origin",
        ),
        (
            _cloud_config(payload_changes={"origin": "https://api.example.test#x"}),
            "origin",
        ),
        (_cloud_config(payload_changes={"hostname": "other.test"}), "origin"),
    )
    for changed, reason in cases:
        with pytest.raises(ProviderCallError, match=reason):
            CloudAdapter(
                changed,
                _spec(ProviderKind.CLOUD),
                FakeTransport([]),
                MemoryRawOutputSink(),
            ).execute(_record(ProviderKind.CLOUD))

    class BrokenPreflight(FakeTransport):
        failure: BaseException | None = None
        result: object | None = None

        def preflight(self, request):
            if self.failure is not None:
                raise self.failure
            return self.result

    for failure, result, reason in (
        (TimeoutError(), None, "connect_timeout"),
        (ProviderCallError("dead_host"), None, "dead_host"),
        (OSError(), None, "transport_preflight_failure"),
        (None, object(), "invalid_transport_security"),
        (
            None,
            TransportSecurity("203.0.113.7", "api.example.test", True, ""),
            "connection",
        ),
    ):
        transport = BrokenPreflight([])
        transport.failure = failure
        transport.result = result
        with pytest.raises(ProviderCallError, match=reason):
            CloudAdapter(
                valid, _spec(ProviderKind.CLOUD), transport, MemoryRawOutputSink()
            ).execute(_record(ProviderKind.CLOUD))


def test_same_origin_redirect_status_is_rejected_without_following() -> None:
    transport = FakeTransport(
        [TransportResponse(302, b"", "", {})],
        security_address="127.0.0.1",
        security_hostname="127.0.0.1",
    )
    with pytest.raises(ProviderCallError, match="redirect"):
        OllamaAdapter(
            OllamaConfig("http://127.0.0.1:11434", "127.0.0.1"),
            _spec(ProviderKind.LOCAL),
            transport,
            MemoryRawOutputSink(),
        ).execute(_record(ProviderKind.LOCAL))
    assert len(transport.calls) == 1


def test_ollama_invalid_type_and_named_loopback_fail_before_preflight() -> None:
    for config in (
        OllamaConfig(1, "127.0.0.1"),  # type: ignore[arg-type]
        OllamaConfig("http://127.0.0.1", 1),  # type: ignore[arg-type]
        OllamaConfig("http://localhost:11434", "localhost"),
    ):
        with pytest.raises(ProviderCallError):
            OllamaAdapter(
                config,
                _spec(ProviderKind.LOCAL),
                FakeTransport([]),
                MemoryRawOutputSink(),
            ).execute(_record(ProviderKind.LOCAL))


def test_runner_guard_and_failure_paths_never_invent_a_member() -> None:
    harness = _CandidateEvaluationHarness()
    local_member = _evaluated(_spec(ProviderKind.LOCAL))
    local2_member = _repinned(
        local_member,
        member_id="ministral",
        provider_id="ollama_ministral",
        family="ministral3",
    )
    cloud_member = _evaluated(_spec(ProviderKind.CLOUD))
    local = _record(ProviderKind.LOCAL, member=local_member)
    local2 = _record(ProviderKind.LOCAL, job_id="job:two", member=local2_member)
    cloud = _record(ProviderKind.CLOUD, member=cloud_member)

    class Good:
        def __init__(self, member):
            self.member = member

        @property
        def lease_authority(self):
            return _test_lease_authority()

        def execute(self, job):
            value = _audited_executed(self.member)
            return council.ProviderResponse(
                value.audit.raw_response_digest,
                job.evidence_digest,
                job.bundle_digest,
                value,
                value.execution_audit,
            )

    weights = {"qwen": "1", "ministral": "1", "cloud": "1"}
    guards = (
        {"local_jobs": []},
        {"clock": None},
        {"local_adapters": []},
        {"local_jobs": (_job(ProviderKind.LOCAL), local2)},
        {"local_jobs": (cloud, local2)},
        {"cloud_job": local},
        {"local_adapters": (Good(cloud_member), Good(local2_member))},
        {"cloud_adapter": Good(local_member)},
        {
            "local_adapters": (
                Good(replace(_spec(ProviderKind.LOCAL), model_digest="0" * 64)),
                Good(local2_member),
            )
        },
        {
            "local_adapters": (
                Good(local_member),
                Good(_repinned(local2_member, family="qwen3.5")),
            )
        },
        {"cloud_job": _record(ProviderKind.CLOUD, evidence_digest="c" * 64)},
    )
    base = {
        "local_jobs": (local, local2),
        "cloud_job": cloud,
        "local_adapters": (Good(local_member), Good(local2_member)),
        "cloud_adapter": Good(cloud_member),
        "reliability_weights": weights,
        "context_weights": weights,
        "clock": lambda job: job.lease_acquired_at,
    }
    with pytest.raises(ValueError, match="unavailable until U19"):
        CouncilRunner().run(**base)
    with pytest.raises(ValueError, match="one evaluation per member"):
        CouncilRunner().run_candidate_evaluation(**base, candidate_evaluations={})
    for index, change in enumerate(guards):
        try:
            harness.evaluate(**{**base, **change})
        except ValueError:
            continue
        pytest.fail(f"runner guard {index} did not fail closed")

    class Failure:
        def __init__(self, member, error):
            self.member = member
            self.error = error

        @property
        def lease_authority(self):
            return _test_lease_authority()

        def execute(self, job):
            raise self.error

    result = harness.evaluate(
        **{
            **base,
            "local_adapters": (
                Failure(local_member, ProviderCallError("local_dead")),
                Failure(local2_member, RuntimeError()),
            ),
            "cloud_adapter": Failure(cloud_member, ProviderCallError("cloud_dead")),
        }
    )
    assert result.valid_member_count == 0
    assert {item.unavailable_code for item in result.outcomes} == {
        "local_dead",
        "provider_runtime_failure",
        "cloud_dead",
    }
    assert all(item.execution_audit is not None for item in result.outcomes)
    for record, outcome in zip((local, local2, cloud), result.outcomes):
        persisted = _TEST_REPOSITORIES[record.job_id].provider_execution(
            record.job_id, record.job_revision, record.fencing_token
        )
        assert persisted.digest == outcome.execution_audit.digest
    with pytest.raises(ValueError, match="DiagnosticCouncilMixture"):
        council.seal_council_receipt(result)  # type: ignore[arg-type]

    def fresh_base():
        fresh_local = _record(ProviderKind.LOCAL, member=local_member)
        fresh_local2 = _record(ProviderKind.LOCAL, member=local2_member)
        fresh_cloud = _record(ProviderKind.CLOUD, member=cloud_member)
        return {
            **base,
            "local_jobs": (fresh_local, fresh_local2),
            "cloud_job": fresh_cloud,
        }

    class Mismatch(Good):
        def execute(self, job):
            response = super().execute(job)
            return replace(response, value=replace(response.value, member=cloud_member))

    result = harness.evaluate(
        **{
            **fresh_base(),
            "local_adapters": (Mismatch(local_member), Good(local2_member)),
            "cloud_adapter": Failure(cloud_member, RuntimeError()),
        }
    )
    assert result.valid_member_count == 1
    assert result.outcomes[0].unavailable_code == "provider_context_mismatch"
    assert result.outcomes[-1].unavailable_code == "provider_runtime_failure"

    class Slow(Good):
        def execute(self, job):
            time.sleep(0.02)
            return super().execute(job)

    tiny_cloud = _record(
        ProviderKind.CLOUD,
        member=cloud_member,
        deadlines=DeadlineBudget(1, 1, 1, 1, 1),
    )
    timed = harness.evaluate(
        **{
            **fresh_base(),
            "cloud_job": tiny_cloud,
            "cloud_adapter": Slow(cloud_member),
        }
    )
    assert timed.outcomes[-1].unavailable_code == "overall_timeout"

    second_called = threading.Event()

    class Second(Good):
        def execute(self, job):
            second_called.set()
            return super().execute(job)

    tiny_local = _record(
        ProviderKind.LOCAL,
        member=local_member,
        deadlines=DeadlineBudget(1, 1, 1, 1, 1),
    )
    fenced = harness.evaluate(
        **{
            **fresh_base(),
            "local_jobs": (tiny_local, _record(ProviderKind.LOCAL, member=local2_member)),
            "local_adapters": (Slow(local_member), Second(local2_member)),
        }
    )
    assert fenced.outcomes[0].unavailable_code == "overall_timeout"
    assert fenced.outcomes[1].unavailable_code == "local_capacity_fenced_after_timeout"
    assert not second_called.is_set()

    class MissingAudit(Good):
        def execute(self, job):
            return replace(super().execute(job), provider_audit=None)

    missing = harness.evaluate(
        **{
            **fresh_base(),
            "local_adapters": (MissingAudit(local_member), Good(local2_member)),
        }
    )
    assert missing.outcomes[0].unavailable_code == "provider_audit_missing"

    class BrokenAuthority(Good):
        @property
        def lease_authority(self):
            raise RuntimeError("test boundary failure")

    broken = harness.evaluate(
        **{
            **fresh_base(),
            "cloud_adapter": BrokenAuthority(cloud_member),
        }
    )
    assert broken.outcomes[-1].unavailable_code == "provider_runtime_failure"

    broken_local_job = _record(ProviderKind.LOCAL, member=local_member)
    broken_local = harness._outcome(
        broken_local_job,
        BrokenAuthority(local_member),
        weights,
        weights,
        lambda job: job.lease_acquired_at,
    )
    assert broken_local.unavailable_code == "provider_runtime_failure"

    class InvalidAuthority(Good):
        @property
        def lease_authority(self):
            return object()

    invalid_authority = harness._outcome(
        _record(ProviderKind.LOCAL, member=local_member),
        InvalidAuthority(local_member),
        weights,
        weights,
        lambda job: job.lease_acquired_at,
    )
    assert invalid_authority.unavailable_code == "provider_runtime_failure"

    direct_job = _record(ProviderKind.LOCAL, member=local_member)
    direct_adapter = Good(local_member)
    invalid = harness._from_settlement(direct_job, direct_adapter, object(), weights, weights)
    assert invalid.unavailable_code == "invalid_settlement_outcome"
    typed = harness._from_settlement(
        direct_job,
        direct_adapter,
        RunOutcome(
            True,
            direct_job,
            ProviderFailure(FailureKind.PERMANENT, "provider_unavailable"),
        ),
        weights,
        weights,
    )
    assert typed.unavailable_code == "provider_unavailable"
    stale = harness._from_settlement(
        direct_job,
        direct_adapter,
        RunOutcome(True, direct_job),
        weights,
        weights,
    )
    assert stale.unavailable_code == "provider_context_mismatch"
