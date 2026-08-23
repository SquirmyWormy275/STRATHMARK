from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

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
    ProviderResponse,
)
from strathmark.v3.application.job_ports import (
    MANDATORY_EXTERNAL_FIELD_DEPENDENCIES,
    ReadinessDependencySnapshot,
)
from strathmark.v3.infrastructure.integrity import IntegrityTrustStore, P256EphemeralSigner
from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection
from strathmark.v3.infrastructure.sqlite.jobs import (
    DurableJobError,
    DurableJobRepository,
    FailureKind,
    JobAdmissionRejected,
    JobConflict,
    JobDeadlineExceeded,
    JobRequest,
    JobState,
    RetryPolicy,
)

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64
DIGEST_D = "d" * 64
T0 = "2026-08-23T10:00:00.000Z"
T1 = "2026-08-23T10:00:01.000Z"
T2 = "2026-08-23T10:00:02.000Z"
T3 = "2026-08-23T10:00:03.000Z"
T4 = "2026-08-23T10:00:04.000Z"
T10 = "2026-08-23T10:00:10.000Z"
READY = ReadinessDependencySnapshot.all_ready(
    llm_members=("local", "cloud"),
    required_for_field=(*MANDATORY_EXTERNAL_FIELD_DEPENDENCIES, "formula", "ml", "llm:local"),
)


def capacity_use(**changes: int) -> CapacityUse:
    values = {
        "open_tournaments": 1,
        "round_entrants": 12,
        "field_entrants": 6,
        "plausible_qualifiers": 12,
        "context_cards": 12,
        "receipt_bytes": 1024,
        "blob_bytes": 4096,
        "api_page_size": 25,
    }
    values.update(changes)
    return CapacityUse(**values)


def manifest(
    *, global_jobs: int = 16, inference_queued: int = 8, inference_leased: int = 2
) -> CapacityManifest:
    return CapacityManifest(
        schema_version="strathmark-v3-job-capacity-v1",
        max_open_tournaments=1,
        max_round_entrants=48,
        max_field_entrants=12,
        max_plausible_qualifiers=48,
        max_context_cards=48,
        max_queued_jobs=global_jobs,
        max_receipt_bytes=1_048_576,
        max_blob_bytes=16_777_216,
        max_api_page_size=100,
        reserved_imminent_jobs=1,
        reserved_recovery_jobs=1,
        aging_interval_ms=1_000,
        aging_increment=125,
        lanes=(
            LaneCapacity(JobLane.HOT_FIELD, 4, 2),
            LaneCapacity(JobLane.INFERENCE, inference_queued, inference_leased),
            LaneCapacity(JobLane.LOOKUP_RECOVERY, 4, 2),
            LaneCapacity(JobLane.MAINTENANCE, 4, 1),
        ),
    )


def request(
    number: int,
    *,
    lane: JobLane = JobLane.INFERENCE,
    priority: JobPriority = JobPriority.PLAUSIBLE_QUALIFIER,
    created_at: str = T0,
    not_before_at: str = T0,
    deadline: str = T10,
    attempts: int = 3,
    revision: int = 1,
    job_kind: JobKind | None = None,
    use: CapacityUse | None = None,
) -> JobRequest:
    if job_kind is None:
        job_kind = {
            JobLane.HOT_FIELD: JobKind.HOT_FIELD_ASSEMBLY,
            JobLane.INFERENCE: JobKind.FORMULA_CARD,
            JobLane.LOOKUP_RECOVERY: JobKind.RECEIPT_LOOKUP,
            JobLane.MAINTENANCE: JobKind.MAINTENANCE,
        }[lane]
    return JobRequest.create(
        job_id=f"job:j{number}",
        job_revision=revision,
        idempotency_key=f"job_request:r{number}-{revision}",
        job_kind=job_kind,
        lane=lane,
        priority=priority,
        capacity_use=capacity_use() if use is None else use,
        payload={"competitor_id": f"competitor:c{number}"},
        evidence_digest=DIGEST_A,
        bundle_digest=DIGEST_B,
        retry_policy_version="retry.v1",
        created_at=created_at,
        not_before_at=not_before_at,
        hard_deadline_at=deadline,
        max_attempts=attempts,
    )


def repository(tmp_path: Path, **manifest_kwargs) -> DurableJobRepository:
    signer = P256EphemeralSigner.generate("integrity-key:u7-integration")
    return DurableJobRepository(
        tmp_path / "jobs.sqlite3",
        capacity=manifest(**manifest_kwargs),
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
    )


def open_repository(
    database: Path, capacity: CapacityManifest, signer: P256EphemeralSigner
) -> DurableJobRepository:
    return DurableJobRepository(
        database,
        capacity=capacity,
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
    )


def commit_success(
    repo: DurableJobRepository,
    job_id: str,
    revision: int,
    *,
    worker_id: str,
    fencing_token: int,
    observed_at: str,
    current_evidence_digest: str,
    current_bundle_digest: str,
    result_digest: str,
    publish=None,
):
    return repo.commit_success(
        job_id,
        revision,
        worker_id=worker_id,
        fencing_token=fencing_token,
        result_digest=result_digest,
        current_context=lambda _connection, _job: (
            current_evidence_digest,
            current_bundle_digest,
        ),
        clock=lambda: observed_at,
        publish=publish,
    )


def claim(
    repo: DurableJobRepository,
    lane: JobLane,
    *,
    worker_id: str,
    observed_at: str,
    lease_duration_ms: int,
):
    return repo.claim(
        lane,
        worker_id=worker_id,
        clock=lambda: observed_at,
        lease_duration_ms=lease_duration_ms,
    )


def test_enqueue_is_durable_exactly_idempotent_and_capacity_admitted(tmp_path: Path) -> None:
    repo = repository(tmp_path, global_jobs=3, inference_queued=2, inference_leased=1)
    first = repo.enqueue(request(1, priority=JobPriority.IMMINENT_FIELD))
    assert first.state is JobState.QUEUED
    assert first == repo.enqueue(request(1, priority=JobPriority.IMMINENT_FIELD))
    with pytest.raises(JobConflict):
        repo.enqueue(request(1, priority=JobPriority.SCHEDULED_ENTRANT))
    duplicate = JobRequest.create(
        job_id="job:j1",
        job_revision=1,
        idempotency_key="job_request:different",
        job_kind=JobKind.FORMULA_CARD,
        lane=JobLane.INFERENCE,
        priority=JobPriority.IMMINENT_FIELD,
        capacity_use=capacity_use(),
        payload={"competitor_id": "competitor:c1"},
        evidence_digest=DIGEST_A,
        bundle_digest=DIGEST_B,
        retry_policy_version="retry.v1",
        created_at=T0,
        not_before_at=T0,
        hard_deadline_at=T10,
        max_attempts=3,
    )
    with pytest.raises(JobConflict):
        repo.enqueue(duplicate)
    with pytest.raises(JobAdmissionRejected, match="imminent_capacity_reserved"):
        repo.enqueue(request(2, priority=JobPriority.SCHEDULED_ENTRANT))
    repo.enqueue(request(2, priority=JobPriority.IMMINENT_FIELD))
    with pytest.raises(JobAdmissionRejected, match="lane_queue_full"):
        repo.enqueue(request(3, priority=JobPriority.IMMINENT_FIELD))
    with pytest.raises(JobAdmissionRejected, match="open_tournaments_capacity_exceeded"):
        repo.enqueue(request(99, use=capacity_use(open_tournaments=2)))


def test_maintenance_suspension_and_recovery_lane_ignore_inference_exhaustion(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path, global_jobs=3, inference_queued=2, inference_leased=1)
    with pytest.raises(JobAdmissionRejected, match="maintenance_suspended"):
        repo.enqueue(request(1, lane=JobLane.MAINTENANCE), maintenance_suspended=True)
    repo.enqueue(request(1, priority=JobPriority.IMMINENT_FIELD))
    repo.enqueue(request(2, priority=JobPriority.IMMINENT_FIELD))
    repo.enqueue(request(3, lane=JobLane.LOOKUP_RECOVERY, priority=JobPriority.RECOVERY))
    assert claim(
        repo, JobLane.INFERENCE, worker_id="worker:infer", observed_at=T1, lease_duration_ms=5_000
    )
    assert (
        claim(
            repo,
            JobLane.LOOKUP_RECOVERY,
            worker_id="worker:recover",
            observed_at=T1,
            lease_duration_ms=5_000,
        ).job_id
        == "job:j3"
    )


def test_atomic_claim_heartbeat_expiry_reclaim_and_old_fence_rejection(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    repo.enqueue(request(1, attempts=3))
    first = claim(
        repo, JobLane.INFERENCE, worker_id="worker:first", observed_at=T1, lease_duration_ms=1_000
    )
    assert first.fencing_token == 1 and first.attempt_count == 1
    heart = repo.heartbeat(
        first.job_id,
        1,
        worker_id="worker:first",
        fencing_token=1,
        observed_at="2026-08-23T10:00:01.500Z",
        extend_ms=500,
    )
    assert heart.lease_expires_at == T2
    reclaimed = claim(
        repo, JobLane.INFERENCE, worker_id="worker:second", observed_at=T2, lease_duration_ms=2_000
    )
    assert reclaimed.fencing_token == 2 and reclaimed.attempt_count == 2
    with pytest.raises(JobConflict):
        commit_success(
            repo,
            first.job_id,
            1,
            worker_id="worker:first",
            fencing_token=1,
            observed_at=T3,
            current_evidence_digest=DIGEST_A,
            current_bundle_digest=DIGEST_B,
            result_digest=DIGEST_C,
        )
    succeeded = commit_success(
        repo,
        reclaimed.job_id,
        1,
        worker_id="worker:second",
        fencing_token=2,
        observed_at=T3,
        current_evidence_digest=DIGEST_A,
        current_bundle_digest=DIGEST_B,
        result_digest=DIGEST_C,
    )
    assert succeeded.state is JobState.SUCCEEDED
    assert (
        commit_success(
            repo,
            reclaimed.job_id,
            1,
            worker_id="worker:second",
            fencing_token=2,
            observed_at=T4,
            current_evidence_digest=DIGEST_A,
            current_bundle_digest=DIGEST_B,
            result_digest=DIGEST_C,
        )
        == succeeded
    )
    with pytest.raises(JobConflict):
        commit_success(
            repo,
            reclaimed.job_id,
            1,
            worker_id="worker:second",
            fencing_token=2,
            observed_at=T4,
            current_evidence_digest=DIGEST_A,
            current_bundle_digest=DIGEST_B,
            result_digest=DIGEST_D,
        )


def test_concurrent_claims_return_exactly_one_fencing_lease(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    repo.enqueue(request(1))
    barrier = threading.Barrier(2)

    def claim_concurrently(worker: str):
        barrier.wait()
        return claim(
            repo,
            JobLane.INFERENCE,
            worker_id=worker,
            observed_at=T1,
            lease_duration_ms=2_000,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(claim_concurrently, ("worker:first", "worker:second")))
    leases = [item for item in results if item is not None]
    assert len(leases) == 1
    assert leases[0].fencing_token == 1
    assert repo.get("job:j1", 1) == leases[0]


def test_publish_hook_and_job_success_are_one_transaction(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    repo.enqueue(request(1))
    lease = claim(
        repo, JobLane.INFERENCE, worker_id="worker:one", observed_at=T1, lease_duration_ms=5_000
    )

    def fail_after_write(connection: sqlite3.Connection, _job) -> None:
        connection.execute("CREATE TABLE u7_forecasts(value TEXT NOT NULL)")
        connection.execute("INSERT INTO u7_forecasts VALUES ('would-leak')")
        raise RuntimeError("injected crash")

    with pytest.raises(RuntimeError, match="injected crash"):
        commit_success(
            repo,
            lease.job_id,
            1,
            worker_id="worker:one",
            fencing_token=lease.fencing_token,
            observed_at=T2,
            current_evidence_digest=DIGEST_A,
            current_bundle_digest=DIGEST_B,
            result_digest=DIGEST_C,
            publish=fail_after_write,
        )
    assert repo.get(lease.job_id, 1).state is JobState.LEASED
    with open_v3_connection(repo.database_path, read_only=True) as connection:
        assert (
            connection.execute("SELECT 1 FROM sqlite_master WHERE name='u7_forecasts'").fetchone()
            is None
        )

    def publish(connection: sqlite3.Connection, job) -> None:
        connection.execute("CREATE TABLE u7_forecasts(value TEXT NOT NULL)")
        connection.execute("INSERT INTO u7_forecasts VALUES (?)", (job.job_id,))

    result = commit_success(
        repo,
        lease.job_id,
        1,
        worker_id="worker:one",
        fencing_token=lease.fencing_token,
        observed_at=T2,
        current_evidence_digest=DIGEST_A,
        current_bundle_digest=DIGEST_B,
        result_digest=DIGEST_C,
        publish=publish,
    )
    assert result.state is JobState.SUCCEEDED
    with open_v3_connection(repo.database_path, read_only=True) as connection:
        assert connection.execute("SELECT value FROM u7_forecasts").fetchone()[0] == "job:j1"
        assert connection.execute("SELECT COUNT(*) FROM v3_job_publications").fetchone()[0] == 1


def test_stale_digests_discard_without_publication(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    repo.enqueue(request(1))
    lease = claim(
        repo, JobLane.INFERENCE, worker_id="worker:one", observed_at=T1, lease_duration_ms=5_000
    )
    called = []
    result = commit_success(
        repo,
        lease.job_id,
        1,
        worker_id="worker:one",
        fencing_token=lease.fencing_token,
        observed_at=T2,
        current_evidence_digest=DIGEST_C,
        current_bundle_digest=DIGEST_B,
        result_digest=DIGEST_D,
        publish=lambda *_: called.append(True),
    )
    assert result.state is JobState.STALE
    assert result.terminal_reason == "evidence_changed"
    assert called == []
    repo.enqueue(request(2))
    lease = claim(
        repo, JobLane.INFERENCE, worker_id="worker:two", observed_at=T1, lease_duration_ms=5_000
    )
    result = commit_success(
        repo,
        lease.job_id,
        1,
        worker_id="worker:two",
        fencing_token=lease.fencing_token,
        observed_at=T2,
        current_evidence_digest=DIGEST_A,
        current_bundle_digest=DIGEST_C,
        result_digest=DIGEST_D,
    )
    assert result.terminal_reason == "bundle_changed"


@pytest.mark.parametrize(
    ("method", "state"),
    [
        ("mark_invalid", JobState.INVALID),
        ("mark_stale", JobState.STALE),
        ("mark_permanent_failure", JobState.PERMANENT_FAILED),
    ],
)
def test_explicit_terminal_paths(tmp_path: Path, method: str, state: JobState) -> None:
    repo = repository(tmp_path)
    repo.enqueue(request(1))
    lease = claim(
        repo, JobLane.INFERENCE, worker_id="worker:one", observed_at=T1, lease_duration_ms=5_000
    )
    result = getattr(repo, method)(
        lease.job_id,
        1,
        worker_id="worker:one",
        fencing_token=lease.fencing_token,
        observed_at=T2,
        reason="explicit_terminal",
    )
    assert result.state is state


@pytest.mark.parametrize("initial", ["queued", "leased", "retryable"])
def test_cancel_every_active_state_invalidates_any_worker(tmp_path: Path, initial: str) -> None:
    repo = repository(tmp_path)
    repo.enqueue(request(1))
    lease = None
    if initial != "queued":
        lease = claim(
            repo,
            JobLane.INFERENCE,
            worker_id="worker:one",
            observed_at=T1,
            lease_duration_ms=5_000,
        )
    if initial == "retryable":
        repo.record_failure(
            lease.job_id,
            1,
            worker_id="worker:one",
            fencing_token=lease.fencing_token,
            observed_at=T2,
            failure_kind=FailureKind.TRANSPORT,
            reason="transport_timeout",
            policy=RetryPolicy("retry.v1"),
        )
    cancelled = repo.cancel("job:j1", 1, observed_at=T3, reason="operator_cancelled")
    assert cancelled.state is JobState.CANCELLED
    if lease is not None:
        with pytest.raises(JobConflict):
            commit_success(
                repo,
                lease.job_id,
                1,
                worker_id="worker:one",
                fencing_token=lease.fencing_token,
                observed_at=T4,
                current_evidence_digest=DIGEST_A,
                current_bundle_digest=DIGEST_B,
                result_digest=DIGEST_C,
            )
    with pytest.raises(JobConflict):
        repo.cancel("job:j1", 1, observed_at=T4, reason="again")


def test_retryable_schema_transport_process_and_terminal_failures(tmp_path: Path) -> None:
    policy = RetryPolicy(
        "retry.v1",
        base_delay_ms=100,
        maximum_delay_ms=200,
        schema_retry_limit=1,
        transport_attempt_limit=2,
        process_attempt_limit=2,
    )
    for number, kind in enumerate(
        (FailureKind.SCHEMA, FailureKind.TRANSPORT, FailureKind.PROCESS), start=1
    ):
        signer = P256EphemeralSigner.generate(f"integrity-key:u7-retry-{number}")
        repo = open_repository(tmp_path / f"retry-{number}.sqlite3", manifest(), signer)
        repo.enqueue(request(number))
        lease = claim(
            repo,
            JobLane.INFERENCE,
            worker_id="worker:one",
            observed_at=T1,
            lease_duration_ms=5_000,
        )
        failed = repo.record_failure(
            lease.job_id,
            1,
            worker_id="worker:one",
            fencing_token=lease.fencing_token,
            observed_at=T2,
            failure_kind=kind,
            reason="typed_failure",
            policy=policy,
        )
        assert failed.state is JobState.RETRYABLE_FAILED
        assert T2 < failed.not_before_at < T3
        assert (
            claim(
                repo,
                JobLane.INFERENCE,
                worker_id="worker:early",
                observed_at=T2,
                lease_duration_ms=1_000,
            )
            is None
        )
        retry = claim(
            repo,
            JobLane.INFERENCE,
            worker_id="worker:two",
            observed_at=T3,
            lease_duration_ms=2_000,
        )
        assert retry.attempt_count == 2 and retry.fencing_token == 2
        terminal = repo.record_failure(
            retry.job_id,
            1,
            worker_id="worker:two",
            fencing_token=retry.fencing_token,
            observed_at=T4,
            failure_kind=kind,
            reason="typed_failure",
            policy=policy,
        )
        assert terminal.state is JobState.PERMANENT_FAILED

    signer = P256EphemeralSigner.generate("integrity-key:u7-terminal")
    repo = open_repository(tmp_path / "terminal.sqlite3", manifest(), signer)
    for number, kind, expected in (
        (10, FailureKind.VALIDATION, JobState.INVALID),
        (11, FailureKind.PERMANENT, JobState.PERMANENT_FAILED),
    ):
        repo.enqueue(request(number))
        lease = claim(
            repo,
            JobLane.INFERENCE,
            worker_id=f"worker:w{number}",
            observed_at=T1,
            lease_duration_ms=5_000,
        )
        assert (
            repo.record_failure(
                lease.job_id,
                1,
                worker_id=f"worker:w{number}",
                fencing_token=lease.fencing_token,
                observed_at=T2,
                failure_kind=kind,
                reason="terminal_failure",
                policy=policy,
            ).state
            is expected
        )


def test_hard_deadline_and_attempt_exhaustion_are_finite(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    repo.enqueue(request(1, deadline=T2, attempts=1))
    lease = claim(
        repo, JobLane.INFERENCE, worker_id="worker:one", observed_at=T1, lease_duration_ms=1_000
    )
    with pytest.raises(JobDeadlineExceeded):
        repo.heartbeat(
            lease.job_id,
            1,
            worker_id="worker:one",
            fencing_token=lease.fencing_token,
            observed_at=T2,
            extend_ms=1_000,
        )
    assert (
        claim(
            repo, JobLane.INFERENCE, worker_id="worker:two", observed_at=T2, lease_duration_ms=1_000
        )
        is None
    )
    assert repo.get("job:j1", 1).state is JobState.PERMANENT_FAILED
    repo.enqueue(request(2, deadline=T1))
    assert (
        claim(
            repo, JobLane.INFERENCE, worker_id="worker:two", observed_at=T1, lease_duration_ms=1_000
        )
        is None
    )
    assert repo.get("job:j2", 1).terminal_reason == "deadline_exceeded"


def test_aging_is_bounded_within_priority_class(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    repo.enqueue(
        request(
            1,
            priority=JobPriority.MAINTENANCE,
            created_at="2026-08-23T09:59:50.000Z",
            not_before_at="2026-08-23T09:59:50.000Z",
        )
    )
    repo.enqueue(request(2, priority=JobPriority.IMMINENT_FIELD))
    claimed = claim(
        repo, JobLane.INFERENCE, worker_id="worker:one", observed_at=T1, lease_duration_ms=2_000
    )
    assert claimed.job_id == "job:j2"
    repo.cancel("job:j2", 1, observed_at=T2, reason="test_complete")
    repo.enqueue(request(3, priority=JobPriority.MAINTENANCE, created_at=T0))
    assert (
        claim(
            repo, JobLane.INFERENCE, worker_id="worker:two", observed_at=T3, lease_duration_ms=2_000
        ).job_id
        == "job:j1"
    )


def test_restart_before_and_after_provider_response_and_forecast_commit(tmp_path: Path) -> None:
    database = tmp_path / "restart.sqlite3"
    signer = P256EphemeralSigner.generate("integrity-key:u7-restart")
    repo = open_repository(database, manifest(), signer)
    repo.enqueue(request(1))
    abandoned = claim(
        repo, JobLane.INFERENCE, worker_id="worker:old", observed_at=T1, lease_duration_ms=1_000
    )
    response = ProviderResponse(DIGEST_C, DIGEST_A, DIGEST_B, {"forecast": 42})
    restarted = open_repository(database, manifest(), signer)
    recovered = claim(
        restarted,
        JobLane.INFERENCE,
        worker_id="worker:new",
        observed_at=T2,
        lease_duration_ms=2_000,
    )
    assert recovered.fencing_token > abandoned.fencing_token
    commit_success(
        restarted,
        recovered.job_id,
        1,
        worker_id="worker:new",
        fencing_token=recovered.fencing_token,
        observed_at=T3,
        current_evidence_digest=DIGEST_A,
        current_bundle_digest=DIGEST_B,
        result_digest=response.result_digest,
    )
    after_commit = open_repository(database, manifest(), signer)
    assert after_commit.get("job:j1", 1).state is JobState.SUCCEEDED
    assert (
        claim(
            after_commit,
            JobLane.INFERENCE,
            worker_id="worker:any",
            observed_at=T4,
            lease_duration_ms=1_000,
        )
        is None
    )


def test_coordinator_never_calls_provider_before_durable_lease(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    coordinator = DurableCoordinator(repo, retry_policy=RetryPolicy("retry.v1"))
    calls: list[str] = []

    class Provider:
        def execute(self, job):
            calls.append(job.job_id)
            return ProviderResponse(DIGEST_C, DIGEST_A, DIGEST_B, {"ok": True})

    assert not coordinator.run_one(
        JobLane.INFERENCE,
        worker_id="worker:one",
        lease_duration_ms=2_000,
        provider=Provider(),
        current_context=lambda _job: (DIGEST_A, DIGEST_B),
        publish=lambda *_: calls.append("published"),
        clock=lambda: T1,
    ).claimed
    assert calls == []
    repo.enqueue(request(1))
    times = iter((T1, T2))
    outcome = coordinator.run_one(
        JobLane.INFERENCE,
        worker_id="worker:one",
        lease_duration_ms=2_000,
        provider=Provider(),
        current_context=lambda _job: (DIGEST_A, DIGEST_B),
        publish=lambda *_: calls.append("published"),
        clock=lambda: next(times),
    )
    assert outcome.job.state is JobState.SUCCEEDED
    assert calls == ["job:j1", "published"]
    health = coordinator.health(observed_at=T2, dependency_probe=lambda _now: READY)
    assert health.oldest_job_at is None


def test_coordinator_settles_an_existing_persisted_lease_and_returns_response(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    repo.enqueue(request(1))
    lease = claim(
        repo,
        JobLane.INFERENCE,
        worker_id="worker:one",
        observed_at=T1,
        lease_duration_ms=2_000,
    )
    assert lease is not None
    response = ProviderResponse(DIGEST_C, DIGEST_A, DIGEST_B, {"forecast": 42})

    class Provider:
        def execute(self, job):
            assert job == lease
            return response

    outcome = DurableCoordinator(repo, retry_policy=RetryPolicy("retry.v1")).run_claimed(
        lease,
        provider=Provider(),
        current_context=lambda _job: (DIGEST_A, DIGEST_B),
        publish=lambda *_: None,
        clock=lambda: T2,
    )

    assert outcome.job.state is JobState.SUCCEEDED
    assert outcome.provider_response is response


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        (lambda: type("Invalid", (), {"execute": lambda self, _job: object()})(), JobState.INVALID),
        (
            lambda: type(
                "Typed",
                (),
                {
                    "execute": lambda self, _job: (_ for _ in ()).throw(
                        ProviderFailure(FailureKind.PERMANENT, "provider_unavailable")
                    )
                },
            )(),
            JobState.PERMANENT_FAILED,
        ),
        (
            lambda: type(
                "Crash",
                (),
                {"execute": lambda self, _job: (_ for _ in ()).throw(RuntimeError("secret"))},
            )(),
            JobState.RETRYABLE_FAILED,
        ),
    ],
)
def test_coordinator_classifies_provider_failures(
    tmp_path: Path, provider, expected: JobState
) -> None:
    repo = repository(tmp_path)
    repo.enqueue(request(1))
    coordinator = DurableCoordinator(repo, retry_policy=RetryPolicy("retry.v1"))
    times = iter((T1, T2))
    result = coordinator.run_one(
        JobLane.INFERENCE,
        worker_id="worker:one",
        lease_duration_ms=2_000,
        provider=provider(),
        current_context=lambda _job: (DIGEST_A, DIGEST_B),
        publish=lambda *_: pytest.fail("failed providers cannot publish"),
        clock=lambda: next(times),
    ).job
    assert result.state is expected
    assert "secret" not in (result.terminal_reason or "")


def test_coordinator_rejects_provider_context_mismatch(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    repo.enqueue(request(1))
    coordinator = DurableCoordinator(repo, retry_policy=RetryPolicy("retry.v1"))

    class Provider:
        def execute(self, _job):
            return ProviderResponse(DIGEST_C, DIGEST_D, DIGEST_B, {})

    times = iter((T1, T2))
    result = coordinator.run_one(
        JobLane.INFERENCE,
        worker_id="worker:one",
        lease_duration_ms=2_000,
        provider=Provider(),
        current_context=lambda _job: (DIGEST_A, DIGEST_B),
        publish=lambda *_: pytest.fail("stale provider result cannot publish"),
        clock=lambda: next(times),
    ).job
    assert result.state is JobState.STALE


def test_context_is_reread_after_provider_completion_before_locked_publication(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    repo.enqueue(request(1))
    with open_v3_connection(repo.database_path) as connection:
        connection.execute(
            "CREATE TABLE u7_authority(job_id TEXT PRIMARY KEY, evidence TEXT, bundle TEXT)"
        )
        connection.execute(
            "INSERT INTO u7_authority VALUES (?, ?, ?)", ("job:j1", DIGEST_A, DIGEST_B)
        )
        connection.commit()
    provider_completed = threading.Event()
    authority_updated = threading.Event()

    class Provider:
        def execute(self, _job):
            response = ProviderResponse(DIGEST_C, DIGEST_A, DIGEST_B, {"forecast": 42})
            provider_completed.set()
            assert authority_updated.wait(timeout=5)
            return response

    def current_context(job):
        with open_v3_connection(repo.database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT evidence, bundle FROM u7_authority WHERE job_id=?", (job.job_id,)
            ).fetchone()
            return str(row[0]), str(row[1])

    coordinator = DurableCoordinator(repo, retry_policy=RetryPolicy("retry.v1"))
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            coordinator.run_one,
            JobLane.INFERENCE,
            worker_id="worker:barrier",
            lease_duration_ms=5_000,
            provider=Provider(),
            current_context=current_context,
            publish=lambda *_: pytest.fail("stale result cannot publish"),
            clock=lambda: T1,
        )
        assert provider_completed.wait(timeout=5)
        with open_v3_connection(repo.database_path) as connection:
            connection.execute(
                "UPDATE u7_authority SET evidence=? WHERE job_id=?", (DIGEST_D, "job:j1")
            )
            connection.commit()
        authority_updated.set()
        outcome = future.result(timeout=5)
    assert outcome.job.state is JobState.STALE
    with open_v3_connection(repo.database_path, read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM v3_job_publications").fetchone()[0] == 0


def test_clock_is_sampled_after_writer_lock_wait_and_blocks_expired_commit(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    repo.enqueue(request(1, deadline=T3))
    lease = claim(
        repo, JobLane.INFERENCE, worker_id="worker:clock", observed_at=T1, lease_duration_ms=2_000
    )
    sampled = {"now": T2}
    started = threading.Event()

    def attempt_commit() -> None:
        started.set()
        repo.commit_success(
            lease.job_id,
            1,
            worker_id="worker:clock",
            fencing_token=lease.fencing_token,
            result_digest=DIGEST_C,
            current_context=lambda _connection, _job: (DIGEST_A, DIGEST_B),
            clock=lambda: sampled["now"],
        )

    with open_v3_connection(repo.database_path) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(attempt_commit)
            assert started.wait(timeout=5)
            sampled["now"] = T3
            blocker.rollback()
            with pytest.raises(JobDeadlineExceeded):
                future.result(timeout=5)
    assert repo.get(lease.job_id, 1).state is JobState.LEASED


def test_job_mapping_and_local_gpu_serialization_leave_cloud_independent(tmp_path: Path) -> None:
    repo = repository(tmp_path, inference_leased=3)
    with pytest.raises(DurableJobError, match="mapping"):
        JobRequest.create(
            job_id="job:bad-map",
            job_revision=1,
            idempotency_key="job_request:bad-map",
            job_kind=JobKind.MAINTENANCE,
            lane=JobLane.INFERENCE,
            priority=JobPriority.MAINTENANCE,
            capacity_use=capacity_use(),
            payload={},
            evidence_digest=DIGEST_A,
            bundle_digest=DIGEST_B,
            retry_policy_version="retry.v1",
            created_at=T0,
            not_before_at=T0,
            hard_deadline_at=T10,
            max_attempts=1,
        )
    repo.enqueue(request(1, job_kind=JobKind.LOCAL_LLM_CARD))
    repo.enqueue(request(2, job_kind=JobKind.CLOUD_LLM_CARD))
    repo.enqueue(request(3, job_kind=JobKind.LOCAL_LLM_CARD))
    first = claim(
        repo, JobLane.INFERENCE, worker_id="worker:gpu", observed_at=T1, lease_duration_ms=5_000
    )
    second = claim(
        repo, JobLane.INFERENCE, worker_id="worker:cloud", observed_at=T1, lease_duration_ms=5_000
    )
    assert first.resource_class.value == "local_gpu"
    assert second.resource_class.value == "cloud"
    assert (
        claim(
            repo,
            JobLane.INFERENCE,
            worker_id="worker:blocked",
            observed_at=T1,
            lease_duration_ms=5_000,
        )
        is None
    )


def test_health_applies_effective_expiry_and_declares_field_dependency_subset(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    repo.enqueue(request(1))
    claim(repo, JobLane.INFERENCE, worker_id="worker:expiry", observed_at=T1, lease_duration_ms=500)
    repo.enqueue(request(2, deadline=T1))
    health = repo.health(
        observed_at=T2, dependency_probe=lambda _now: READY, deadline_risk_window_ms=500
    )
    assert dict(health.leased_by_lane)["inference"] == 0
    assert dict(health.depth_by_lane)["inference"] == 1
    assert health.effective_expired_leases == 1
    assert health.required_field_dependencies == (
        "durable_store_integrity",
        "queue_within_capacity",
        "hot_field_capacity",
        "recovery_capacity",
        "deadline_safe",
        *READY.required_for_field,
    )
    readiness = dict(health.dependency_readiness)
    assert set(health.required_field_dependencies) <= readiness.keys()
    assert health.field_ready == all(readiness[name] for name in health.required_field_dependencies)


def test_claim_samples_clock_only_after_writer_lock_and_skips_provider_after_deadline(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    repo.enqueue(request(1, deadline=T3))
    calls: list[str] = []
    sampled = {"now": T2}
    started = threading.Event()
    coordinator = DurableCoordinator(repo, retry_policy=RetryPolicy("retry.v1"))

    def run():
        started.set()
        return coordinator.run_one(
            JobLane.INFERENCE,
            worker_id="worker:post-lock",
            lease_duration_ms=1_000,
            provider=type(
                "Provider",
                (),
                {"execute": lambda self, _job: calls.append("provider")},
            )(),
            current_context=lambda _job: (DIGEST_A, DIGEST_B),
            publish=lambda *_: calls.append("publication"),
            clock=lambda: sampled["now"],
        )

    with open_v3_connection(repo.database_path) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(run)
            assert started.wait(timeout=5)
            sampled["now"] = T3
            blocker.rollback()
            outcome = future.result(timeout=5)
    assert not outcome.claimed
    assert calls == []
    assert repo.get("job:j1", 1).terminal_reason == "deadline_exceeded"


def test_cross_lane_expired_gpu_lease_is_reconciled_before_local_llm_claim(tmp_path: Path) -> None:
    database = tmp_path / "cross-lane-gpu.sqlite3"
    signer = P256EphemeralSigner.generate("integrity-key:u7-cross-lane")
    repo = open_repository(database, manifest(), signer)
    repo.enqueue(request(1, lane=JobLane.MAINTENANCE, job_kind=JobKind.MODEL_FACTORY))
    factory = repo.claim(
        JobLane.MAINTENANCE,
        worker_id="worker:factory",
        clock=lambda: T1,
        lease_duration_ms=500,
    )
    restarted = open_repository(database, manifest(), signer)
    restarted.enqueue(request(2, job_kind=JobKind.LOCAL_LLM_CARD))
    llm = restarted.claim(
        JobLane.INFERENCE,
        worker_id="worker:llm",
        clock=lambda: T2,
        lease_duration_ms=2_000,
    )
    assert llm.job_id == "job:j2"
    with pytest.raises(JobConflict):
        restarted.commit_success(
            factory.job_id,
            1,
            worker_id="worker:factory",
            fencing_token=factory.fencing_token,
            result_digest=DIGEST_C,
            current_context=lambda *_: (DIGEST_A, DIGEST_B),
            clock=lambda: T3,
        )


def test_complete_request_scoped_readiness_graph_and_integrity_failure_health(
    tmp_path: Path,
) -> None:
    snapshot = ReadinessDependencySnapshot.all_ready(
        llm_members=("local_reasoning", "local_generalist", "cloud_frontier"),
        required_for_field=(
            *MANDATORY_EXTERNAL_FIELD_DEPENDENCIES,
            "blob_integrity",
            "pinned_bundle",
            "formula",
            "ml",
            "llm:local_reasoning",
            "llm:local_generalist",
            "pool_degradation_mode",
            "backup_age",
        ),
    )
    repo = repository(tmp_path)
    repo.enqueue(request(50, deadline="2026-08-23T11:00:00.000Z"))
    health = repo.health(observed_at=T1, dependency_probe=lambda _now: snapshot)
    required = dict(health.dependency_readiness)
    assert required["durable_store_integrity"]
    for name in snapshot.required_for_field:
        assert required[name]
        changed = snapshot.with_dimension(name, False)
        degraded = repo.health(observed_at=T1, dependency_probe=lambda _now: changed)
        assert not dict(degraded.dependency_readiness)[name]
        assert not degraded.field_ready

    for optional in ("llm:cloud_frontier", "cloud_consent"):
        changed = snapshot.with_dimension(optional, False)
        report_only = repo.health(observed_at=T1, dependency_probe=lambda _now: changed)
        assert not dict(report_only.dependency_readiness)[optional]
        assert optional not in report_only.required_field_dependencies
        assert report_only.field_ready

    with open_v3_connection(repo.database_path) as connection:
        trigger_sql = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='v3_job_history_no_update'"
            ).fetchone()[0]
        )
        connection.execute("DROP TRIGGER v3_job_history_no_update")
        connection.execute("UPDATE v3_job_history SET auth_signature_der_b64='AA=='")
        connection.execute(trigger_sql)
        connection.commit()
    damaged = repo.health(observed_at=T1, dependency_probe=lambda _now: snapshot)
    assert not dict(damaged.dependency_readiness)["durable_store_integrity"]
    assert not damaged.field_ready


def test_health_is_fresh_request_scoped_and_reports_deadline_risk(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    repo.enqueue(request(1, deadline=T3))
    first = repo.health(
        observed_at=T1, dependency_probe=lambda _now: READY, deadline_risk_window_ms=500
    )
    second = repo.health(
        observed_at=T2, dependency_probe=lambda _now: READY, deadline_risk_window_ms=1_000
    )
    assert first.deadline_risk_count == 0
    assert second.deadline_risk_count == 1
    assert dict(second.depth_by_lane)["inference"] == 1
    assert dict(second.leased_by_lane)["inference"] == 0
    assert first is not second
