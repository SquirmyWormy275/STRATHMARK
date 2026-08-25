from __future__ import annotations

import base64
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
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
    RunOutcome,
)
from strathmark.v3.application.job_ports import (
    MANDATORY_EXTERNAL_FIELD_DEPENDENCIES,
    ReadinessDependencySnapshot,
)
from strathmark.v3.infrastructure import integrity as integrity_module
from strathmark.v3.infrastructure.integrity import (
    IntegrityTrustStore,
    P256EphemeralSigner,
    sign_manifest,
)
from strathmark.v3.infrastructure.sqlite import jobs as jobs_module
from strathmark.v3.infrastructure.sqlite.connection import immediate_transaction, open_v3_connection
from strathmark.v3.infrastructure.sqlite.jobs import (
    DurableJobError,
    DurableJobRepository,
    FailureKind,
    JobConflict,
    JobRecord,
    JobRequest,
    JobState,
    RetryPolicy,
)

A = "a" * 64
B = "b" * 64
C = "c" * 64
T0 = "2026-08-23T10:00:00.000Z"
T1 = "2026-08-23T10:00:01.000Z"
T2 = "2026-08-23T10:00:02.000Z"
T3 = "2026-08-23T10:00:03.000Z"
T10 = "2026-08-23T10:00:10.000Z"
READY = ReadinessDependencySnapshot.all_ready(
    llm_members=("local", "cloud"),
    required_for_field=(*MANDATORY_EXTERNAL_FIELD_DEPENDENCIES, "formula", "ml", "llm:local"),
)


USE = CapacityUse(1, 12, 6, 12, 12, 1024, 4096, 25)


def capacity(*, leased: int = 2) -> CapacityManifest:
    return CapacityManifest(
        "strathmark-v3-job-capacity-v1",
        1,
        48,
        12,
        48,
        48,
        16,
        1_048_576,
        16_777_216,
        100,
        1,
        1,
        1_000,
        5,
        (
            LaneCapacity(JobLane.HOT_FIELD, 4, 2),
            LaneCapacity(JobLane.INFERENCE, 8, leased),
            LaneCapacity(JobLane.LOOKUP_RECOVERY, 4, 2),
            LaneCapacity(JobLane.MAINTENANCE, 4, 1),
        ),
    )


def job(number: int = 1, *, attempts: int = 3, deadline: str = T10) -> JobRequest:
    return JobRequest.create(
        job_id=f"job:p{number}",
        job_revision=1,
        idempotency_key=f"job_request:p{number}",
        job_kind=JobKind.FORMULA_CARD,
        lane=JobLane.INFERENCE,
        priority=JobPriority.PLAUSIBLE_QUALIFIER,
        capacity_use=USE,
        payload={"n": number},
        evidence_digest=A,
        bundle_digest=B,
        retry_policy_version="retry.v1",
        created_at=T0,
        not_before_at=T0,
        hard_deadline_at=deadline,
        max_attempts=attempts,
    )


def repo(tmp_path: Path, *, leased: int = 2) -> DurableJobRepository:
    signer = P256EphemeralSigner.generate("integrity-key:u7-property")
    return open_repository(tmp_path / "property.sqlite3", capacity(leased=leased), signer)


def open_repository(
    database: Path, manifest: CapacityManifest, signer: P256EphemeralSigner
) -> DurableJobRepository:
    return DurableJobRepository(
        database,
        capacity=manifest,
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
    )


def commit_success(
    repository: DurableJobRepository,
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
    return repository.commit_success(
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
    repository: DurableJobRepository,
    lane: JobLane,
    *,
    worker_id: str,
    observed_at: str,
    lease_duration_ms: int,
):
    return repository.claim(
        lane,
        worker_id=worker_id,
        clock=lambda: observed_at,
        lease_duration_ms=lease_duration_ms,
    )


def test_exactly_one_attempt_per_revision_is_publishable_under_repeated_late_workers(
    tmp_path: Path,
) -> None:
    repository = repo(tmp_path)
    repository.enqueue(job())
    first = claim(
        repository,
        JobLane.INFERENCE,
        worker_id="worker:first",
        observed_at=T1,
        lease_duration_ms=500,
    )
    second = claim(
        repository,
        JobLane.INFERENCE,
        worker_id="worker:second",
        observed_at="2026-08-23T10:00:01.500Z",
        lease_duration_ms=1_500,
    )
    publications: list[int] = []
    for worker, token in (("worker:first", first.fencing_token), ("worker:second", 2)):
        try:
            commit_success(
                repository,
                "job:p1",
                1,
                worker_id=worker,
                fencing_token=token,
                observed_at=T2,
                current_evidence_digest=A,
                current_bundle_digest=B,
                result_digest=C,
                publish=lambda _connection, current: publications.append(current.fencing_token),
            )
        except JobConflict:
            pass
    assert second.fencing_token == 2
    assert publications == [2]
    repository.verify()


def test_lane_lease_limit_is_atomic_and_does_not_consume_queued_work(tmp_path: Path) -> None:
    repository = repo(tmp_path, leased=1)
    repository.enqueue(job(1))
    repository.enqueue(job(2))
    assert claim(
        repository,
        JobLane.INFERENCE,
        worker_id="worker:first",
        observed_at=T1,
        lease_duration_ms=2_000,
    )
    assert (
        claim(
            repository,
            JobLane.INFERENCE,
            worker_id="worker:second",
            observed_at=T1,
            lease_duration_ms=2_000,
        )
        is None
    )
    assert repository.get("job:p2", 1).state is JobState.QUEUED


def test_retry_deadline_before_next_attempt_is_terminal(tmp_path: Path) -> None:
    repository = repo(tmp_path)
    repository.enqueue(job(deadline="2026-08-23T10:00:02.050Z"))
    lease = claim(
        repository,
        JobLane.INFERENCE,
        worker_id="worker:first",
        observed_at=T1,
        lease_duration_ms=2_000,
    )
    result = repository.record_failure(
        lease.job_id,
        1,
        worker_id="worker:first",
        fencing_token=1,
        observed_at=T2,
        failure_kind=FailureKind.TRANSPORT,
        reason="transport_timeout",
        policy=RetryPolicy("retry.v1", base_delay_ms=100, maximum_delay_ms=100),
    )
    assert result.state is JobState.PERMANENT_FAILED
    assert result.terminal_reason == "deadline_exceeded"


def test_retryable_projection_exhausted_during_reconcile_is_terminal(tmp_path: Path) -> None:
    repository = repo(tmp_path)
    repository.enqueue(job(attempts=1))
    lease = claim(
        repository,
        JobLane.INFERENCE,
        worker_id="worker:first",
        observed_at=T1,
        lease_duration_ms=2_000,
    )
    with open_v3_connection(repository.database_path) as connection:
        with immediate_transaction(connection):
            connection.execute(
                "UPDATE v3_jobs SET state='retryable-failed', not_before_at=?, lease_owner=NULL, "
                "lease_acquired_at=NULL, lease_expires_at=NULL, terminal_reason='transport_timeout' "
                "WHERE job_id=? AND job_revision=1",
                (T2, lease.job_id),
            )
            current = repository._get_connection(connection, lease.job_id, 1)
            repository._append_history(connection, "retryable-failed", JobState.LEASED, current, T1)
    assert (
        claim(
            repository,
            JobLane.INFERENCE,
            worker_id="worker:next",
            observed_at=T2,
            lease_duration_ms=1_000,
        )
        is None
    )
    assert repository.get(lease.job_id, 1).state is JobState.PERMANENT_FAILED


def test_retryable_projection_crossing_hard_deadline_reports_deadline(tmp_path: Path) -> None:
    repository = repo(tmp_path)
    repository.enqueue(job(deadline=T2))
    lease = claim(
        repository,
        JobLane.INFERENCE,
        worker_id="worker:first",
        observed_at=T1,
        lease_duration_ms=500,
    )
    repository.record_failure(
        lease.job_id,
        1,
        worker_id="worker:first",
        fencing_token=1,
        observed_at="2026-08-23T10:00:01.250Z",
        failure_kind=FailureKind.TRANSPORT,
        reason="transport_timeout",
        policy=RetryPolicy("retry.v1", base_delay_ms=100, maximum_delay_ms=100),
    )
    assert (
        claim(
            repository,
            JobLane.INFERENCE,
            worker_id="worker:next",
            observed_at=T2,
            lease_duration_ms=1_000,
        )
        is None
    )
    assert repository.get(lease.job_id, 1).terminal_reason == "deadline_exceeded"


def test_expired_final_lease_reports_attempt_exhaustion_before_deadline(tmp_path: Path) -> None:
    repository = repo(tmp_path)
    repository.enqueue(job(attempts=1))
    lease = claim(
        repository,
        JobLane.INFERENCE,
        worker_id="worker:first",
        observed_at=T1,
        lease_duration_ms=500,
    )
    assert (
        claim(
            repository,
            JobLane.INFERENCE,
            worker_id="worker:next",
            observed_at="2026-08-23T10:00:01.500Z",
            lease_duration_ms=1_000,
        )
        is None
    )
    assert repository.get(lease.job_id, 1).terminal_reason == "lease_expired_attempts_exhausted"


def test_job_request_retry_and_repository_rejection_matrix(tmp_path: Path) -> None:
    base = job()
    for version in (None, "Bad"):
        with pytest.raises(DurableJobError):
            RetryPolicy(version)
    with pytest.raises(DurableJobError):
        RetryPolicy("retry.v1", base_delay_ms=0)
    with pytest.raises(DurableJobError):
        RetryPolicy("retry.v1", base_delay_ms=2, maximum_delay_ms=1)
    for value in (-1, True, "1"):
        with pytest.raises(DurableJobError):
            RetryPolicy("retry.v1", schema_retry_limit=value)

    fields = list(base.__dataclass_fields__)
    baseline = [getattr(base, field) for field in fields]
    mutations = [
        {"idempotency_key": "job_request:p1"},
        {"job_kind": "formula_card"},
        {"resource_class": "local_cpu"},
        {"job_kind": JobKind.MAINTENANCE},
        {"lane": "inference"},
        {"priority": 300},
        {"capacity_use_json": "{"},
        {"capacity_use_json": "[]"},
        {"capacity_use_json": '{"api_page_size":-1}'},
        {"capacity_use_json": json.dumps(USE.to_dict(), indent=2)},
        {"payload_json": "{"},
        {"payload_json": "[]", "payload_digest": jobs_module.canonical_digest([])},
        {"payload_json": '{"n": 2}'},
        {"not_before_at": T10},
        {"max_attempts": True},
        {"max_attempts": 0},
        {"max_attempts": 33},
    ]
    for mutation in mutations:
        values = list(baseline)
        for field, value in mutation.items():
            values[fields.index(field)] = value
        with pytest.raises(DurableJobError):
            JobRequest(*values)
    with pytest.raises(DurableJobError):
        JobRequest.create(
            job_id="job:x",
            job_revision=1,
            idempotency_key="job_request:x",
            job_kind="kind",
            lane=JobLane.INFERENCE,
            priority=JobPriority.IMMINENT_FIELD,
            capacity_use=USE,
            payload=[],
            evidence_digest=A,
            bundle_digest=B,
            retry_policy_version="retry.v1",
            created_at=T0,
            not_before_at=T0,
            hard_deadline_at=T10,
            max_attempts=1,
        )
    with pytest.raises(DurableJobError):
        JobRequest.create(
            job_id="job:x",
            job_revision=1,
            idempotency_key="job_request:x",
            job_kind=JobKind.FORMULA_CARD,
            lane=JobLane.INFERENCE,
            priority=JobPriority.IMMINENT_FIELD,
            capacity_use=object(),
            payload={},
            evidence_digest=A,
            bundle_digest=B,
            retry_policy_version="retry.v1",
            created_at=T0,
            not_before_at=T0,
            hard_deadline_at=T10,
            max_attempts=1,
        )
    malformed_use = object.__new__(JobRequest)
    object.__setattr__(malformed_use, "capacity_use_json", "[]")
    with pytest.raises(DurableJobError):
        malformed_use.capacity_use()
    signer = P256EphemeralSigner.generate("integrity-key:u7-rejection")
    trust = IntegrityTrustStore((signer.identity,))
    with pytest.raises(DurableJobError):
        DurableJobRepository(True, capacity=capacity(), signer=signer, trust_store=trust)
    with pytest.raises(DurableJobError):
        DurableJobRepository(
            tmp_path / "x.sqlite3", capacity=object(), signer=signer, trust_store=trust
        )
    with pytest.raises(DurableJobError):
        DurableJobRepository(
            tmp_path / "x.sqlite3", capacity=capacity(), signer=object(), trust_store=trust
        )
    with pytest.raises(DurableJobError):
        DurableJobRepository(
            tmp_path / "x.sqlite3", capacity=capacity(), signer=signer, trust_store=object()
        )
    repository = repo(tmp_path)
    with pytest.raises(DurableJobError):
        repository.enqueue(object())
    with pytest.raises(DurableJobError):
        repository.enqueue(base, maintenance_suspended=1)
    with pytest.raises(KeyError):
        repository.get("job:missing", 1)
    with pytest.raises(DurableJobError):
        claim(repository, "inference", worker_id="worker:x", observed_at=T1, lease_duration_ms=1)


def test_lease_publish_failure_and_terminal_guard_matrix(tmp_path: Path) -> None:
    repository = repo(tmp_path)
    repository.enqueue(job())
    lease = claim(
        repository,
        JobLane.INFERENCE,
        worker_id="worker:first",
        observed_at=T1,
        lease_duration_ms=2_000,
    )
    with pytest.raises(DurableJobError):
        commit_success(
            repository,
            lease.job_id,
            1,
            worker_id="worker:first",
            fencing_token=1,
            observed_at=T2,
            current_evidence_digest=A,
            current_bundle_digest=B,
            result_digest=C,
            publish=object(),
        )
    with pytest.raises(DurableJobError):
        repository.commit_success(
            lease.job_id,
            1,
            worker_id="worker:first",
            fencing_token=1,
            result_digest=C,
            current_context=lambda *_: (A, B),
            clock=None,
        )
    with pytest.raises(DurableJobError):
        repository.commit_success(
            lease.job_id,
            1,
            worker_id="worker:first",
            fencing_token=1,
            result_digest=C,
            current_context=lambda *_: A,
            clock=lambda: T2,
        )
    with pytest.raises(DurableJobError):
        repository.record_failure(
            lease.job_id,
            1,
            worker_id="worker:first",
            fencing_token=1,
            observed_at=T2,
            failure_kind="transport",
            reason="failure",
            policy=RetryPolicy("retry.v1"),
        )
    with pytest.raises(JobConflict):
        repository.record_failure(
            lease.job_id,
            1,
            worker_id="worker:first",
            fencing_token=1,
            observed_at=T2,
            failure_kind=FailureKind.TRANSPORT,
            reason="failure",
            policy=RetryPolicy("retry.v2"),
        )
    with open_v3_connection(repository.database_path) as connection:
        with immediate_transaction(connection):
            current = repository._get_connection(connection, lease.job_id, 1)
            with pytest.raises(DurableJobError):
                repository._finish(connection, current, JobState.CANCELLED, T2, reason="bad")
            with pytest.raises(KeyError):
                repository._get_connection(connection, "job:missing", 1)


def test_coordinator_contract_rejection_matrix(tmp_path: Path) -> None:
    repository = repo(tmp_path)
    policy = RetryPolicy("retry.v1")
    for args in ((object(), policy), (repository, object())):
        with pytest.raises(DurableJobError):
            DurableCoordinator(args[0], retry_policy=args[1])
    for digest_field in ("result_digest", "evidence_digest", "bundle_digest"):
        values = {"result_digest": A, "evidence_digest": A, "bundle_digest": B}
        values[digest_field] = "bad"
        with pytest.raises(DurableJobError):
            ProviderResponse(**values, value={})
    with pytest.raises(DurableJobError):
        ProviderFailure("transport", "reason")
    for reason in ("", "Bad", "bad reason", "x" * 129):
        with pytest.raises(DurableJobError):
            ProviderFailure(FailureKind.TRANSPORT, reason)
    with pytest.raises(DurableJobError):
        RunOutcome(1, None)
    with pytest.raises(DurableJobError):
        RunOutcome(True, None)
    coordinator = DurableCoordinator(repository, retry_policy=policy)
    for lane, provider, current, publish, clock in (
        ("inference", object(), lambda _job: (A, B), lambda *_: None, lambda: T1),
        (JobLane.INFERENCE, object(), lambda _job: (A, B), lambda *_: None, lambda: T1),
        (
            JobLane.INFERENCE,
            type("P", (), {"execute": lambda *_: None})(),
            object(),
            lambda *_: None,
            lambda: T1,
        ),
        (
            JobLane.INFERENCE,
            type("P", (), {"execute": lambda *_: None})(),
            lambda _job: (A, B),
            object(),
            lambda: T1,
        ),
        (
            JobLane.INFERENCE,
            type("P", (), {"execute": lambda *_: None})(),
            lambda _job: (A, B),
            lambda *_: None,
            object(),
        ),
    ):
        with pytest.raises(DurableJobError):
            coordinator.run_one(
                lane,
                worker_id="worker:x",
                lease_duration_ms=1,
                provider=provider,
                current_context=current,
                publish=publish,
                clock=clock,
            )


def test_readiness_probe_and_post_lock_clock_contracts_fail_closed(tmp_path: Path) -> None:
    for mutation in (
        {"event_integrity": 1},
        {"llm_members": []},
        {"llm_members": ()},
        {"llm_members": (("Bad", True),)},
        {"llm_members": (("local", 1),)},
        {"llm_members": (("local", True), ("local", False))},
    ):
        with pytest.raises(DurableJobError):
            replace(READY, **mutation)
    with pytest.raises(DurableJobError):
        ReadinessDependencySnapshot.all_ready(
            llm_members=["local"], required_for_field=MANDATORY_EXTERNAL_FIELD_DEPENDENCIES
        )
    with pytest.raises(DurableJobError):
        ReadinessDependencySnapshot.all_ready(
            llm_members=("local",),
            required_for_field=list(MANDATORY_EXTERNAL_FIELD_DEPENDENCIES),
        )
    with pytest.raises(DurableJobError):
        ReadinessDependencySnapshot.all_ready(
            llm_members=("local",),
            required_for_field=(*MANDATORY_EXTERNAL_FIELD_DEPENDENCIES, 1),
        )
    for required in (
        (),
        ("unknown",),
        ("event_integrity", "event_integrity"),
        tuple(name for name in MANDATORY_EXTERNAL_FIELD_DEPENDENCIES if name != "disk_reserve"),
    ):
        with pytest.raises(DurableJobError):
            ReadinessDependencySnapshot.all_ready(
                llm_members=("local",), required_for_field=required
            )
    with pytest.raises(DurableJobError):
        READY.with_dimension("event_integrity", 1)
    with pytest.raises(DurableJobError):
        READY.with_dimension("llm:unknown", False)
    with pytest.raises(DurableJobError):
        READY.with_dimension("unknown", False)

    repository = repo(tmp_path)
    repository.enqueue(job())
    with pytest.raises(DurableJobError):
        repository.claim(
            JobLane.INFERENCE,
            worker_id="worker:no-clock",
            clock=None,
            lease_duration_ms=1,
        )
    with pytest.raises(DurableJobError):
        repository.health(observed_at=T1, dependency_probe=None)
    with pytest.raises(DurableJobError):
        repository.health(observed_at=T1, dependency_probe=lambda _now: object())


def _corrupt(database: Path, statements: tuple[str, ...]) -> None:
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        restore_sql: list[str] = []
        for statement in statements:
            if statement.startswith("DROP TRIGGER "):
                name = statement.removeprefix("DROP TRIGGER ")
                row = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
                ).fetchone()
                assert row is not None
                restore_sql.append(str(row[0]))
            connection.execute(statement)
        for statement in restore_sql:
            connection.execute(statement)
        connection.commit()


def _coherently_rewrite_history(database: Path, update_sql: str) -> None:
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=OFF")
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='v3_job_history_no_update'"
        ).fetchone()
        assert row is not None
        trigger_sql = str(row[0])
        connection.execute("DROP TRIGGER v3_job_history_no_update")
        connection.execute(update_sql)
        prior = jobs_module.ZERO_DIGEST
        sequences = [
            int(item[0])
            for item in connection.execute(
                "SELECT history_sequence FROM v3_job_history ORDER BY history_sequence"
            )
        ]
        for sequence in sequences:
            connection.execute(
                "UPDATE v3_job_history SET prior_history_digest=? WHERE history_sequence=?",
                (prior, sequence),
            )
            item = connection.execute(
                "SELECT * FROM v3_job_history WHERE history_sequence=?", (sequence,)
            ).fetchone()
            digest = jobs_module.canonical_digest(jobs_module._history_value(item))
            connection.execute(
                "UPDATE v3_job_history SET history_digest=? WHERE history_sequence=?",
                (digest, sequence),
            )
            prior = digest
        connection.execute(trigger_sql)
        connection.commit()


def _resign_history(database: Path, signer: P256EphemeralSigner) -> None:
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        trigger_sql = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='v3_job_history_no_update'"
            ).fetchone()[0]
        )
        connection.execute("DROP TRIGGER v3_job_history_no_update")
        prior = jobs_module.ZERO_DIGEST
        for (sequence,) in connection.execute(
            "SELECT history_sequence FROM v3_job_history ORDER BY history_sequence"
        ):
            connection.execute(
                "UPDATE v3_job_history SET prior_history_digest=? WHERE history_sequence=?",
                (prior, sequence),
            )
            row = connection.execute(
                "SELECT * FROM v3_job_history WHERE history_sequence=?", (sequence,)
            ).fetchone()
            value = jobs_module._history_value(row)
            digest = jobs_module.canonical_digest(value)
            authority = sign_manifest(
                "job_transition", value, signer=signer, created_at=str(row["observed_at"])
            )
            connection.execute(
                "UPDATE v3_job_history SET history_digest=?, auth_body_json=?, "
                "auth_body_digest=?, auth_key_id=?, auth_signature_der_b64=? "
                "WHERE history_sequence=?",
                (
                    digest,
                    authority.body_json,
                    authority.body_digest,
                    authority.key_id,
                    authority.signature_der_b64,
                    sequence,
                ),
            )
            prior = digest
        connection.execute(trigger_sql)
        connection.commit()


@pytest.mark.parametrize(
    "statements",
    [
        ("UPDATE v3_jobs SET payload_json='{}'",),
        ("DROP TRIGGER v3_job_history_no_update", "UPDATE v3_job_history SET history_sequence=2"),
        (
            "DROP TRIGGER v3_job_history_no_update",
            f"UPDATE v3_job_history SET history_digest='{'e' * 64}'",
        ),
        ("DROP TRIGGER v3_job_history_no_update", "UPDATE v3_job_history SET job_id='job:unknown'"),
        (
            "DROP TRIGGER v3_job_history_no_update",
            "UPDATE v3_job_history SET operation_kind='leased', from_state='queued'",
        ),
        ("DROP TRIGGER v3_job_history_no_delete", "DELETE FROM v3_job_history"),
        ("UPDATE v3_jobs SET terminal_reason='tampered'",),
    ],
)
def test_restart_verification_rejects_corrupt_job_authority(
    tmp_path: Path, statements: tuple[str, ...]
) -> None:
    database = tmp_path / "corrupt.sqlite3"
    signer = P256EphemeralSigner.generate("integrity-key:u7-corrupt")
    repository = open_repository(database, capacity(), signer)
    repository.enqueue(job())
    _corrupt(database, statements)
    with pytest.raises((DurableJobError, sqlite3.DatabaseError)):
        open_repository(database, capacity(), signer)


def test_verifier_rejects_noncontiguous_illegal_and_publication_corruption(tmp_path: Path) -> None:
    for index, mode in enumerate(
        (
            "noncontiguous",
            "illegal",
            "lease-acquired-projection",
            "missing-publication",
            "bad-publication",
        )
    ):
        database = tmp_path / f"verify-{index}.sqlite3"
        signer = P256EphemeralSigner.generate(f"integrity-key:u7-verify-{index}")
        repository = open_repository(database, capacity(), signer)
        repository.enqueue(job())
        lease = claim(
            repository,
            JobLane.INFERENCE,
            worker_id="worker:first",
            observed_at=T1,
            lease_duration_ms=2_000,
        )
        if mode == "noncontiguous":
            _corrupt(
                database,
                (
                    "DROP TRIGGER v3_job_history_no_update",
                    "UPDATE v3_job_history SET from_state='leased' WHERE history_sequence=2",
                ),
            )
        elif mode == "illegal":
            _corrupt(
                database,
                (
                    "DROP TRIGGER v3_job_history_no_update",
                    "UPDATE v3_job_history SET operation_kind='heartbeat' WHERE history_sequence=2",
                ),
            )
        elif mode == "lease-acquired-projection":
            _corrupt(
                database,
                ("UPDATE v3_jobs SET lease_acquired_at='2026-08-23T10:00:00.500Z'",),
            )
        else:
            commit_success(
                repository,
                lease.job_id,
                1,
                worker_id="worker:first",
                fencing_token=1,
                observed_at=T2,
                current_evidence_digest=A,
                current_bundle_digest=B,
                result_digest=C,
            )
            if mode == "missing-publication":
                _corrupt(
                    database,
                    (
                        "DROP TRIGGER v3_job_publications_no_delete",
                        "DELETE FROM v3_job_publications",
                    ),
                )
            else:
                _corrupt(
                    database,
                    (
                        "DROP TRIGGER v3_job_publications_no_update",
                        f"UPDATE v3_job_publications SET result_digest='{'d' * 64}'",
                    ),
                )
        with pytest.raises(DurableJobError):
            open_repository(database, capacity(), signer)


@pytest.mark.parametrize(
    "update_sql",
    [
        "UPDATE v3_job_history SET job_id='job:unknown' WHERE history_sequence=1",
        "UPDATE v3_job_history SET operation_kind='leased' WHERE history_sequence=1",
        "UPDATE v3_job_history SET from_state='leased' WHERE history_sequence=2",
    ],
)
def test_verifier_rejects_coherently_resealed_semantic_history_attacks(
    tmp_path: Path, update_sql: str
) -> None:
    database = tmp_path / "semantic.sqlite3"
    signer = P256EphemeralSigner.generate("integrity-key:u7-semantic")
    repository = open_repository(database, capacity(), signer)
    repository.enqueue(job())
    claim(
        repository,
        JobLane.INFERENCE,
        worker_id="worker:first",
        observed_at=T1,
        lease_duration_ms=2_000,
    )
    _coherently_rewrite_history(database, update_sql)
    with pytest.raises(DurableJobError):
        open_repository(database, capacity(), signer)


def test_restart_rejects_coherent_sql_forged_legal_lease_and_publication(tmp_path: Path) -> None:
    database = tmp_path / "forged-publication.sqlite3"
    signer = P256EphemeralSigner.generate("integrity-key:u7-forged")
    repository = open_repository(database, capacity(), signer)
    repository.enqueue(job())
    lease = claim(
        repository,
        JobLane.INFERENCE,
        worker_id="worker:first",
        observed_at=T1,
        lease_duration_ms=2_000,
    )
    commit_success(
        repository,
        lease.job_id,
        1,
        worker_id="worker:first",
        fencing_token=lease.fencing_token,
        observed_at=T2,
        current_evidence_digest=A,
        current_bundle_digest=B,
        result_digest=C,
    )
    forged = "d" * 64
    _corrupt(
        database,
        (
            f"UPDATE v3_jobs SET result_digest='{forged}' WHERE job_id='job:p1'",
            "DROP TRIGGER v3_job_publications_no_update",
            f"UPDATE v3_job_publications SET result_digest='{forged}' WHERE job_id='job:p1'",
        ),
    )
    _coherently_rewrite_history(
        database,
        f"UPDATE v3_job_history SET result_digest='{forged}' "
        "WHERE job_id='job:p1' AND operation_kind='succeeded'",
    )
    with pytest.raises(DurableJobError, match="signature|authority"):
        open_repository(database, capacity(), signer)


@pytest.mark.parametrize(
    ("update_sql", "message"),
    [
        ("UPDATE v3_job_history SET job_id='job:unknown'", "unknown work"),
        (f"UPDATE v3_job_history SET job_material_digest='{'e' * 64}'", "bind current"),
        (
            "UPDATE v3_job_history SET operation_kind='leased', from_state=NULL",
            "begin with queued",
        ),
    ],
)
def test_signed_but_semantically_invalid_history_still_fails_closed(
    tmp_path: Path, update_sql: str, message: str
) -> None:
    database = tmp_path / "signed-semantic.sqlite3"
    signer = P256EphemeralSigner.generate("integrity-key:u7-signed-semantic")
    repository = open_repository(database, capacity(), signer)
    repository.enqueue(job())
    _corrupt(database, ("DROP TRIGGER v3_job_history_no_update", update_sql))
    _resign_history(database, signer)
    with pytest.raises(DurableJobError, match=message):
        open_repository(database, capacity(), signer)


@pytest.mark.parametrize(
    ("update_sql", "message"),
    [
        ("UPDATE v3_jobs SET lane='maintenance'", "kind mapping"),
        (
            "UPDATE v3_jobs SET capacity_use_json='"
            '{"api_page_size":25,"blob_bytes":4096,"context_cards":12,"field_entrants":6,'
            '"open_tournaments":2,"plausible_qualifiers":12,"receipt_bytes":1024,'
            '"round_entrants":12}'
            "'",
            "operational capacity",
        ),
    ],
)
def test_restart_rejects_persisted_mapping_or_capacity_bypass(
    tmp_path: Path, update_sql: str, message: str
) -> None:
    database = tmp_path / "persisted-bypass.sqlite3"
    signer = P256EphemeralSigner.generate("integrity-key:u7-persisted-bypass")
    repository = open_repository(database, capacity(), signer)
    repository.enqueue(job())
    _corrupt(database, (update_sql,))
    with pytest.raises(DurableJobError, match=message):
        open_repository(database, capacity(), signer)


def test_signed_noncontiguous_history_and_publication_binding_fail_closed(tmp_path: Path) -> None:
    database = tmp_path / "signed-noncontiguous.sqlite3"
    signer = P256EphemeralSigner.generate("integrity-key:u7-signed-noncontiguous")
    repository = open_repository(database, capacity(), signer)
    repository.enqueue(job())
    claim(
        repository,
        JobLane.INFERENCE,
        worker_id="worker:first",
        observed_at=T1,
        lease_duration_ms=2_000,
    )
    _corrupt(
        database,
        (
            "DROP TRIGGER v3_job_history_no_update",
            "UPDATE v3_job_history SET from_state='leased' WHERE history_sequence=2",
        ),
    )
    _resign_history(database, signer)
    with pytest.raises(DurableJobError, match="contiguous"):
        open_repository(database, capacity(), signer)

    published = tmp_path / "signed-publication.sqlite3"
    signer = P256EphemeralSigner.generate("integrity-key:u7-signed-publication")
    repository = open_repository(published, capacity(), signer)
    repository.enqueue(job())
    lease = claim(
        repository,
        JobLane.INFERENCE,
        worker_id="worker:first",
        observed_at=T1,
        lease_duration_ms=2_000,
    )
    commit_success(
        repository,
        lease.job_id,
        1,
        worker_id="worker:first",
        fencing_token=1,
        observed_at=T2,
        current_evidence_digest=A,
        current_bundle_digest=B,
        result_digest=C,
    )
    with closing(sqlite3.connect(published)) as connection:
        connection.row_factory = sqlite3.Row
        trigger_sql = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='v3_job_publications_no_update'"
            ).fetchone()[0]
        )
        row = connection.execute("SELECT * FROM v3_job_publications").fetchone()
        old_payload = integrity_module.SignedManifest(
            "job_publication",
            str(row["auth_body_json"]),
            str(row["auth_body_digest"]),
            str(row["auth_key_id"]),
            str(row["auth_signature_der_b64"]),
        ).body()["payload"]
        authority = sign_manifest(
            "job_publication",
            {**old_payload, "job_material_digest": "e" * 64},
            signer=signer,
            created_at=T2,
        )
        connection.execute("DROP TRIGGER v3_job_publications_no_update")
        connection.execute(
            "UPDATE v3_job_publications SET auth_body_json=?, auth_body_digest=?, "
            "auth_key_id=?, auth_signature_der_b64=?",
            (
                authority.body_json,
                authority.body_digest,
                authority.key_id,
                authority.signature_der_b64,
            ),
        )
        connection.execute(trigger_sql)
        connection.commit()
    with pytest.raises(DurableJobError, match="authority binding"):
        open_repository(published, capacity(), signer)


def test_publication_authority_parser_rejects_nonobject_and_extra_fields() -> None:
    signer = P256EphemeralSigner.generate("integrity-key:u7-publication-parser")
    base = {
        "job_id": "job:p1",
        "job_revision": 1,
        "fencing_token": 1,
        "result_digest": C,
        "published_at": T2,
    }
    extra = sign_manifest("job_publication", {**base, "extra": True}, signer=signer, created_at=T2)
    row = {
        **base,
        "auth_body_json": extra.body_json,
        "auth_body_digest": extra.body_digest,
        "auth_key_id": extra.key_id,
        "auth_signature_der_b64": extra.signature_der_b64,
    }
    with pytest.raises(DurableJobError, match="fields differ"):
        jobs_module._publication_value(row)

    body = {
        "schema_version": "strathmark-v3-integrity-body-v1",
        "kind": "job_publication",
        "algorithm": integrity_module.SIGNATURE_ALGORITHM,
        "key_id": signer.identity.key_id,
        "created_at": T2,
        "payload": [],
    }
    encoded = jobs_module.canonical_bytes(body)
    row.update(
        auth_body_json=encoded.decode("utf-8"),
        auth_body_digest=jobs_module.canonical_digest(body),
        auth_signature_der_b64=base64.b64encode(signer.sign(encoded)).decode("ascii"),
    )
    with pytest.raises(DurableJobError, match="not an object"):
        jobs_module._publication_value(row)


def test_internal_scalar_and_transition_guards_are_total() -> None:
    with pytest.raises(DurableJobError):
        jobs_module._verify_transition(
            {
                "operation_kind": "unknown",
                "from_state": None,
                "result_state": "queued",
            }
        )
    for value in (None, "x", "g" * 64, "A" * 64):
        with pytest.raises(DurableJobError):
            jobs_module._digest(value, "digest")
    for value in (None, "Bad", "bad token", "x" * 129):
        with pytest.raises(DurableJobError):
            jobs_module._require_token(value, "token")
    for value in (None, "Bad", "bad token", "x" * 129):
        with pytest.raises(DurableJobError):
            jobs_module._require_reason(value)
    for value in (True, "1", 0, -1):
        with pytest.raises(DurableJobError):
            jobs_module._positive(value, "number")
    for value in (True, "0", -1):
        with pytest.raises(DurableJobError):
            jobs_module._nonnegative(value, "number")
    assert jobs_module._nonnegative(0, "number") == 0
    for value in (True, "1", 0, -1, 600_001):
        with pytest.raises(DurableJobError):
            jobs_module._bounded_duration(value)


def test_job_record_payload_rejects_non_object() -> None:
    record = JobRecord(
        "job:x",
        1,
        "job_request:x",
        JobKind.FORMULA_CARD,
        JobLane.INFERENCE,
        JobKind.FORMULA_CARD.resource_class,
        JobPriority.IMMINENT_FIELD,
        jobs_module.canonical_bytes(USE.to_dict()).decode("utf-8"),
        "[]",
        jobs_module.canonical_digest([]),
        A,
        B,
        "retry.v1",
        JobState.QUEUED,
        0,
        1,
        T0,
        T0,
        T10,
        None,
        None,
        None,
        0,
        None,
        None,
        T0,
        T0,
    )
    with pytest.raises(DurableJobError):
        record.payload()
    object.__setattr__(record, "capacity_use_json", "[]")
    with pytest.raises(DurableJobError):
        record.capacity_use()
