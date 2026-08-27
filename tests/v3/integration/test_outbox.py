from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.infrastructure.integrity import (
    CheckpointRegistry,
    IntegrityTrustStore,
    P256EphemeralSigner,
    sign_manifest,
)
from strathmark.v3.infrastructure.sqlite.outbox import (
    DeliveryOutcome,
    OutboxConflict,
    OutboxError,
    OutboxItem,
    OutboxRepository,
    OutboxState,
)

_TEST_OUTBOX_SIGNERS: dict[Path, P256EphemeralSigner] = {}


def _repository(database_path, **kwargs) -> OutboxRepository:
    if isinstance(database_path, (Path, str)) and not isinstance(database_path, bool):
        key = Path(database_path).resolve(strict=False)
    else:
        key = Path("invalid-outbox-test-path")
    signer = _TEST_OUTBOX_SIGNERS.get(key)
    if signer is None:
        signer = P256EphemeralSigner.generate(
            f"integrity-key:outbox-test-{len(_TEST_OUTBOX_SIGNERS) + 1}"
        )
        _TEST_OUTBOX_SIGNERS[key] = signer
    return OutboxRepository(
        database_path,
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
        active_key_id=signer.identity.key_id,
        **kwargs,
    )


def test_outbox_exact_payload_and_transient_backoff_survive_restart(tmp_path: Path) -> None:
    database = tmp_path / "v3.sqlite3"
    repository = _repository(database, base_backoff_ms=100, maximum_backoff_ms=250)
    item = repository.enqueue(
        outbox_id="outbox:one",
        destination="tournament-manager",
        payload={"receipt_id": "receipt:one"},
        created_at="2026-08-23T00:00:00.000Z",
    )
    assert item.payload() == {"receipt_id": "receipt:one"}
    assert (
        repository.enqueue(
            outbox_id="outbox:one",
            destination="tournament-manager",
            payload={"receipt_id": "receipt:one"},
            created_at="2026-08-23T00:00:00.000Z",
        )
        == item
    )

    transient = repository.record_outcome(
        "outbox:one",
        operation_id="outbox_operation:one-transient",
        expected_revision=1,
        outcome=DeliveryOutcome.TRANSIENT,
        observed_at="2026-08-23T00:00:00.100Z",
        reason="network_timeout",
    )
    assert transient.state is OutboxState.TRANSIENT
    assert transient.next_attempt_at == "2026-08-23T00:00:00.200Z"
    restarted = _repository(database, base_backoff_ms=100, maximum_backoff_ms=250)
    assert restarted.get("outbox:one") == transient
    assert restarted.due("2026-08-23T00:00:00.199Z", limit=10) == ()
    assert restarted.due("2026-08-23T00:00:00.200Z", limit=10) == (transient,)


def test_outbox_restart_rejects_direct_mutable_row_rewrite_without_history(
    tmp_path: Path,
) -> None:
    database = tmp_path / "v3.sqlite3"
    repository = _repository(database)
    repository.enqueue(
        outbox_id="outbox:sql-attack",
        destination="mirror",
        payload={"receipt_id": "receipt:one"},
        created_at="2026-08-23T00:00:00.000Z",
    )
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "UPDATE v3_outbox SET state='acknowledged', revision=2, attempt_count=1, "
            "next_attempt_at=NULL, updated_at='2026-08-23T00:00:01.000Z' "
            "WHERE outbox_id='outbox:sql-attack'"
        )
        connection.commit()
    with pytest.raises(OutboxError, match="transition history"):
        _repository(database)


def test_outbox_restart_rejects_fully_rehashed_and_self_signed_sql_forgery(
    tmp_path: Path,
) -> None:
    """Database write access cannot manufacture trusted delivery authority."""

    import strathmark.v3.infrastructure.sqlite.outbox as module

    database = tmp_path / "v3.sqlite3"
    _repository(database).enqueue(
        outbox_id="outbox:forged-ack",
        destination="mirror",
        payload={"receipt_id": "receipt:one"},
        created_at="2026-08-23T00:00:00.000Z",
    )
    operation_id = "outbox_operation:forged-ack"
    observed_at = "2026-08-23T00:00:01.000Z"
    material_digest = module._operation_digest(
        operation_id,
        "outbox:forged-ack",
        1,
        "acknowledged",
        observed_at,
        None,
    )
    transition_value = {
        "schema_version": "strathmark-v3-outbox-transition-v1",
        "transition_sequence": 1,
        "operation_id": operation_id,
        "outbox_id": "outbox:forged-ack",
        "expected_revision": 1,
        "operation_kind": "acknowledged",
        "material_digest": material_digest,
        "from_state": "pending",
        "result_state": "acknowledged",
        "result_revision": 2,
        "result_attempt_count": 1,
        "result_next_attempt_at": None,
        "result_terminal_reason": None,
        "reason": None,
        "observed_at": observed_at,
        "prior_transition_digest": "0" * 64,
    }
    transition_digest = canonical_digest(transition_value)
    attacker = P256EphemeralSigner.generate("integrity-key:attacker")
    forged_manifest = sign_manifest(
        "outbox_transition",
        transition_value,
        signer=attacker,
        created_at=observed_at,
    )
    forged_json = canonical_bytes(forged_manifest.to_dict()).decode("utf-8")
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "INSERT INTO v3_outbox_transitions(transition_sequence, operation_id, "
            "outbox_id, expected_revision, operation_kind, material_digest, from_state, "
            "result_state, result_revision, result_attempt_count, result_next_attempt_at, "
            "result_terminal_reason, reason, observed_at, prior_transition_digest, "
            "transition_digest, signed_transition_json) "
            "VALUES (1, ?, 'outbox:forged-ack', 1, 'acknowledged', ?, 'pending', "
            "'acknowledged', 2, 1, NULL, NULL, NULL, ?, ?, ?, ?)",
            (
                operation_id,
                material_digest,
                observed_at,
                "0" * 64,
                transition_digest,
                forged_json,
            ),
        )
        connection.execute(
            "UPDATE v3_outbox SET state='acknowledged', revision=2, attempt_count=1, "
            "next_attempt_at=NULL, terminal_reason=NULL, updated_at=? "
            "WHERE outbox_id='outbox:forged-ack'",
            (observed_at,),
        )
        connection.commit()
    with pytest.raises(OutboxError, match="signature.*untrusted"):
        _repository(database)


def test_outbox_trust_rotation_survives_restart_and_preserves_old_signatures(
    tmp_path: Path,
) -> None:
    database = tmp_path / "v3.sqlite3"
    old = P256EphemeralSigner.generate("integrity-key:outbox-old")
    old_trust = IntegrityTrustStore((old.identity,))
    first = OutboxRepository(
        database,
        signer=old,
        trust_store=old_trust,
        active_key_id=old.identity.key_id,
    )
    first.enqueue(
        outbox_id="outbox:rotation",
        destination="mirror",
        payload={"receipt_id": "receipt:rotation"},
        created_at="2026-08-23T00:00:00.000Z",
    )
    first.record_outcome(
        "outbox:rotation",
        operation_id="outbox_operation:old-key",
        expected_revision=1,
        outcome=DeliveryOutcome.TRANSIENT,
        observed_at="2026-08-23T00:00:01.000Z",
        reason="timeout",
    )

    new = P256EphemeralSigner.generate("integrity-key:outbox-new")
    registry_root = tmp_path / "integrity-registry"
    registry = CheckpointRegistry(registry_root, bootstrap_identity=old.identity)
    registry.rotate_key(old, new.identity, created_at="2026-08-23T00:00:01.500Z")
    restarted_registry = CheckpointRegistry(
        registry_root,
        bootstrap_identity=old.identity,
    )
    assert restarted_registry.active_identity == new.identity
    rotated_trust = restarted_registry.trust_store
    rotated = OutboxRepository(
        database,
        signer=new,
        trust_store=rotated_trust,
        active_key_id=new.identity.key_id,
    )
    acknowledged = rotated.record_outcome(
        "outbox:rotation",
        operation_id="outbox_operation:new-key",
        expected_revision=2,
        outcome=DeliveryOutcome.ACKNOWLEDGED,
        observed_at="2026-08-23T00:00:02.000Z",
    )
    restarted = OutboxRepository(
        database,
        signer=new,
        trust_store=rotated_trust,
        active_key_id=new.identity.key_id,
    )
    assert restarted.get("outbox:rotation") == acknowledged
    assert [
        json.loads(item.signed_transition_json)["key_id"]
        for item in restarted.history("outbox:rotation")
    ] == [old.identity.key_id, new.identity.key_id]

    with pytest.raises(OutboxError, match="signature.*untrusted"):
        OutboxRepository(
            database,
            signer=new,
            trust_store=IntegrityTrustStore((new.identity,)),
            active_key_id=new.identity.key_id,
        )


def test_outbox_rejects_missing_or_mismatched_external_signing_authority(
    tmp_path: Path,
) -> None:
    database = tmp_path / "v3.sqlite3"
    trusted = P256EphemeralSigner.generate("integrity-key:trusted")
    other = P256EphemeralSigner.generate("integrity-key:other")
    trust = IntegrityTrustStore((trusted.identity,))

    class NoIdentity:
        def sign(self, _payload: bytes) -> bytes:
            return b""

    class NoSign:
        identity = trusted.identity

    invalid_authorities = (
        (trusted, object(), trusted.identity.key_id, "external signer"),
        (NoIdentity(), trust, trusted.identity.key_id, "external signer"),
        (NoSign(), trust, trusted.identity.key_id, "external signer"),
        (trusted, trust, other.identity.key_id, "not externally trusted"),
        (other, trust, trusted.identity.key_id, "not the externally trusted active key"),
    )
    for signer, trust_store, active_key_id, message in invalid_authorities:
        with pytest.raises(OutboxError, match=message):
            OutboxRepository(
                database,
                signer=signer,
                trust_store=trust_store,
                active_key_id=active_key_id,
            )

    with closing(sqlite3.connect(":memory:")) as connection:
        with pytest.raises(OutboxError, match="external typed trust store"):
            __import__(
                "strathmark.v3.infrastructure.sqlite.outbox", fromlist=["x"]
            ).verify_outbox_integrity(connection, trust_store=object())


def test_outbox_restart_rejects_noncanonical_inserted_payload(tmp_path: Path) -> None:
    database = tmp_path / "v3.sqlite3"
    _repository(database)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "INSERT INTO v3_outbox(outbox_id, destination, payload_json, payload_digest, state, "
            "revision, attempt_count, next_attempt_at, created_at, updated_at) "
            "VALUES ('outbox:payload-attack', 'mirror', '{\"b\":2, \"a\":1}', ?, 'pending', "
            "1, 0, '2026-08-23T00:00:00.000Z', '2026-08-23T00:00:00.000Z', "
            "'2026-08-23T00:00:00.000Z')",
            ("0" * 64,),
        )
        connection.commit()
    with pytest.raises(OutboxError, match="canonical"):
        _repository(database)

    nonobject = tmp_path / "nonobject.sqlite3"
    _repository(nonobject)
    with closing(sqlite3.connect(nonobject)) as connection:
        connection.execute(
            "INSERT INTO v3_outbox(outbox_id, destination, payload_json, payload_digest, state, "
            "revision, attempt_count, next_attempt_at, created_at, updated_at) "
            "VALUES ('outbox:list', 'mirror', '[]', ?, 'pending', 1, 0, "
            "'2026-08-23T00:00:00.000Z', '2026-08-23T00:00:00.000Z', "
            "'2026-08-23T00:00:00.000Z')",
            (canonical_digest([]),),
        )
        connection.commit()
    with pytest.raises(OutboxError, match="not an object"):
        _repository(nonobject)


def test_outbox_transition_reconstruction_rejects_every_history_attack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import strathmark.v3.infrastructure.sqlite.outbox as module

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

    class Connection:
        def __init__(self, inner, count: int) -> None:
            self.inner = inner
            self.count = count

        def execute(self, query, parameters=()):
            if "FROM v3_outbox_transitions ORDER BY" in query:
                return Result([object()] * self.count)
            return self.inner.execute(query, parameters)

    def bound(transition, *, prior: str | None = None):
        prior_digest = transition.prior_transition_digest if prior is None else prior
        material = module._operation_digest(
            transition.operation_id,
            transition.outbox_id,
            transition.expected_revision,
            transition.operation_kind,
            transition.observed_at,
            transition.reason,
        )
        value = {
            "schema_version": "strathmark-v3-outbox-transition-v1",
            "transition_sequence": transition.transition_sequence,
            "operation_id": transition.operation_id,
            "outbox_id": transition.outbox_id,
            "expected_revision": transition.expected_revision,
            "operation_kind": transition.operation_kind,
            "material_digest": material,
            "from_state": transition.from_state.value,
            "result_state": transition.result_state.value,
            "result_revision": transition.result_revision,
            "result_attempt_count": transition.result_attempt_count,
            "result_next_attempt_at": transition.result_next_attempt_at,
            "result_terminal_reason": transition.result_terminal_reason,
            "reason": transition.reason,
            "observed_at": transition.observed_at,
            "prior_transition_digest": prior_digest,
        }
        return replace(
            transition,
            material_digest=material,
            prior_transition_digest=prior_digest,
            transition_digest=canonical_digest(value),
        )

    counter = 0

    def repository() -> OutboxRepository:
        nonlocal counter
        counter += 1
        repo = _repository(tmp_path / f"attack-{counter}.sqlite3")
        repo.enqueue(
            outbox_id="outbox:attack",
            destination="mirror",
            payload={},
            created_at="2026-08-23T00:00:00.000Z",
        )
        return repo

    def rejects(repo, transitions, message: str) -> None:
        iterator = iter(transitions)
        with closing(sqlite3.connect(repo.database_path)) as raw:
            raw.row_factory = sqlite3.Row
            with monkeypatch.context() as context:
                context.setattr(module, "_decode_transition", lambda _row: next(iterator))
                with pytest.raises(OutboxError, match=message):
                    module.verify_outbox_integrity(
                        Connection(raw, len(transitions)), trust_store=repo.trust_store
                    )

    transient_repo = repository()
    transient_repo.record_outcome(
        "outbox:attack",
        operation_id="outbox_operation:transient",
        expected_revision=1,
        outcome=DeliveryOutcome.TRANSIENT,
        observed_at="2026-08-23T00:00:01.000Z",
        reason="timeout",
    )
    transient = transient_repo.history("outbox:attack")[0]
    attacks = (
        (replace(transient, transition_sequence=2), "gap"),
        (replace(transient, outbox_id="outbox:unknown"), "unknown payload"),
        (replace(transient, expected_revision=2), "contiguous"),
        (replace(transient, material_digest="0" * 64), "operation digest"),
        (replace(transient, prior_transition_digest="f" * 64), "digest chain"),
        (
            bound(replace(transient, result_next_attempt_at=transient.observed_at)),
            "future retry",
        ),
        (
            bound(replace(transient, result_next_attempt_at=None)),
            "future retry",
        ),
        (
            bound(
                replace(
                    transient,
                    operation_kind="acknowledged",
                    result_state=OutboxState.ACKNOWLEDGED,
                    result_terminal_reason=None,
                    reason=None,
                )
            ),
            "retained a retry",
        ),
        (
            bound(replace(transient, operation_kind="unknown")),
            "kind is unknown",
        ),
        (
            bound(replace(transient, result_attempt_count=99)),
            "result material",
        ),
    )
    for transition, message in attacks:
        rejects(transient_repo, (transition,), message)

    rejects(
        transient_repo,
        (replace(transient, signed_transition_json="[]"),),
        "not an object",
    )
    wrong_kind = json.loads(transient.signed_transition_json)
    wrong_kind["kind"] = "checkpoint"
    rejects(
        transient_repo,
        (
            replace(
                transient,
                signed_transition_json=canonical_bytes(wrong_kind).decode("utf-8"),
            ),
        ),
        "binding differs",
    )
    rejects(
        transient_repo,
        (replace(transient, signed_transition_json=" " + transient.signed_transition_json),),
        "binding differs",
    )
    signed_payload_mismatch = sign_manifest(
        "outbox_transition",
        {"unexpected": True},
        signer=transient_repo.signer,
        created_at=transient.observed_at,
    )
    rejects(
        transient_repo,
        (
            replace(
                transient,
                signed_transition_json=canonical_bytes(signed_payload_mismatch.to_dict()).decode(
                    "utf-8"
                ),
            ),
        ),
        "binding differs",
    )

    permanent_repo = repository()
    permanent_repo.record_outcome(
        "outbox:attack",
        operation_id="outbox_operation:permanent",
        expected_revision=1,
        outcome=DeliveryOutcome.PERMANENT,
        observed_at="2026-08-23T00:00:01.000Z",
        reason="rejected",
    )
    permanent_repo.quarantine(
        "outbox:attack",
        operation_id="outbox_operation:quarantine",
        expected_revision=2,
        observed_at="2026-08-23T00:00:02.000Z",
        reason="review",
    )
    permanent, quarantine = permanent_repo.history("outbox:attack")
    terminal_delivery = bound(
        replace(
            quarantine,
            operation_kind="acknowledged",
            result_state=OutboxState.ACKNOWLEDGED,
            result_attempt_count=2,
            result_terminal_reason=None,
            reason=None,
        ),
        prior=permanent.transition_digest,
    )
    rejects(permanent_repo, (permanent, terminal_delivery), "terminal outbox")
    rejects(
        permanent_repo,
        (
            permanent,
            bound(
                replace(quarantine, result_next_attempt_at="2026-08-23T00:00:03.000Z"),
                prior=permanent.transition_digest,
            ),
        ),
        "quarantine transition retained",
    )

    repaired_repo = repository()
    repaired_repo.record_outcome(
        "outbox:attack",
        operation_id="outbox_operation:repair-base",
        expected_revision=1,
        outcome=DeliveryOutcome.PERMANENT,
        observed_at="2026-08-23T00:00:01.000Z",
        reason="rejected",
    )
    repaired_repo.repair(
        "outbox:attack",
        operation_id="outbox_operation:repair",
        expected_revision=2,
        observed_at="2026-08-23T00:00:02.000Z",
        reason="fixed",
    )
    first, repair = repaired_repo.history("outbox:attack")
    rejects(
        repaired_repo,
        (
            first,
            bound(
                replace(repair, result_next_attempt_at="2026-08-23T00:00:03.000Z"),
                prior=first.transition_digest,
            ),
        ),
        "repair transition retry",
    )

    acknowledged_repo = repository()
    acknowledged_repo.record_outcome(
        "outbox:attack",
        operation_id="outbox_operation:ack",
        expected_revision=1,
        outcome=DeliveryOutcome.ACKNOWLEDGED,
        observed_at="2026-08-23T00:00:01.000Z",
    )
    acknowledged = acknowledged_repo.history("outbox:attack")[0]
    fake_quarantine = bound(
        replace(
            acknowledged,
            transition_sequence=2,
            operation_id="outbox_operation:fake-quarantine",
            expected_revision=2,
            operation_kind="quarantine",
            from_state=OutboxState.ACKNOWLEDGED,
            result_state=OutboxState.QUARANTINED,
            result_revision=3,
            result_attempt_count=1,
            result_terminal_reason="review",
            reason="review",
            observed_at="2026-08-23T00:00:02.000Z",
        ),
        prior=acknowledged.transition_digest,
    )
    rejects(acknowledged_repo, (acknowledged, fake_quarantine), "was quarantined")

    invalid_repair = bound(
        replace(
            transient,
            transition_sequence=2,
            operation_id="outbox_operation:fake-repair",
            expected_revision=2,
            operation_kind="repair",
            from_state=OutboxState.TRANSIENT,
            result_state=OutboxState.REPAIRED,
            result_revision=3,
            result_attempt_count=1,
            result_next_attempt_at="2026-08-23T00:00:02.000Z",
            result_terminal_reason="fixed",
            reason="fixed",
            observed_at="2026-08-23T00:00:02.000Z",
        ),
        prior=transient.transition_digest,
    )
    rejects(transient_repo, (transient, invalid_repair), "did not follow")

    with closing(sqlite3.connect(transient_repo.database_path)) as raw:
        raw.row_factory = sqlite3.Row
        with monkeypatch.context() as context:
            context.setattr(module, "_decode", lambda _row: (_ for _ in ()).throw(ValueError()))
            with pytest.raises(OutboxError, match="cannot be decoded"):
                module.verify_outbox_integrity(raw, trust_store=transient_repo.trust_store)


def test_outbox_terminal_quarantine_repair_and_acknowledgment_are_finite(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "v3.sqlite3")
    repository.enqueue(
        outbox_id="outbox:two",
        destination="mirror",
        payload={"sequence": 7},
        created_at="2026-08-23T00:00:00.000Z",
    )
    permanent = repository.record_outcome(
        "outbox:two",
        operation_id="outbox_operation:two-permanent",
        expected_revision=1,
        outcome=DeliveryOutcome.PERMANENT,
        observed_at="2026-08-23T00:00:01.000Z",
        reason="schema_rejected",
    )
    assert permanent.state is OutboxState.PERMANENT
    quarantined = repository.quarantine(
        "outbox:two",
        operation_id="outbox_operation:two-quarantine",
        expected_revision=2,
        observed_at="2026-08-23T00:00:02.000Z",
        reason="operator_review",
    )
    assert quarantined.state is OutboxState.QUARANTINED
    repaired = repository.repair(
        "outbox:two",
        operation_id="outbox_operation:two-repair",
        expected_revision=3,
        observed_at="2026-08-23T00:00:03.000Z",
        reason="destination_contract_repaired",
    )
    assert repaired.state is OutboxState.REPAIRED
    assert repository.due("2026-08-23T00:00:03.000Z", limit=1) == (repaired,)
    acknowledged = repository.record_outcome(
        "outbox:two",
        operation_id="outbox_operation:two-ack",
        expected_revision=4,
        outcome=DeliveryOutcome.ACKNOWLEDGED,
        observed_at="2026-08-23T00:00:04.000Z",
    )
    assert acknowledged.state is OutboxState.ACKNOWLEDGED
    assert repository.due("2026-08-24T00:00:00.000Z", limit=10) == ()

    with pytest.raises(OutboxConflict, match="immutable|different"):
        repository.enqueue(
            outbox_id="outbox:two",
            destination="mirror",
            payload={"sequence": 8},
            created_at="2026-08-23T00:00:00.000Z",
        )

    assert [entry.reason for entry in repository.history("outbox:two")] == [
        "schema_rejected",
        "operator_review",
        "destination_contract_repaired",
        None,
    ]


def test_outbox_transition_exact_retry_and_crash_prefix_are_atomic(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "v3.sqlite3", base_backoff_ms=100)
    repository.enqueue(
        outbox_id="outbox:three",
        destination="mirror",
        payload={"sequence": 9},
        created_at="2026-08-23T00:00:00.000Z",
    )

    def fail(stage: str) -> None:
        if stage == "after_state_update":
            raise RuntimeError(stage)

    with pytest.raises(RuntimeError, match="after_state_update"):
        repository.record_outcome(
            "outbox:three",
            operation_id="outbox_operation:three-transient",
            expected_revision=1,
            outcome=DeliveryOutcome.TRANSIENT,
            observed_at="2026-08-23T00:00:00.100Z",
            reason="timeout",
            fault_hook=fail,
        )
    assert repository.get("outbox:three").state is OutboxState.PENDING

    first = repository.record_outcome(
        "outbox:three",
        operation_id="outbox_operation:three-transient",
        expected_revision=1,
        outcome=DeliveryOutcome.TRANSIENT,
        observed_at="2026-08-23T00:00:00.100Z",
        reason="timeout",
    )
    duplicate = _repository(tmp_path / "v3.sqlite3", base_backoff_ms=100).record_outcome(
        "outbox:three",
        operation_id="outbox_operation:three-transient",
        expected_revision=1,
        outcome=DeliveryOutcome.TRANSIENT,
        observed_at="2026-08-23T00:00:00.100Z",
        reason="timeout",
    )
    assert duplicate == first
    with pytest.raises(OutboxConflict, match="operation"):
        repository.record_outcome(
            "outbox:three",
            operation_id="outbox_operation:three-transient",
            expected_revision=1,
            outcome=DeliveryOutcome.PERMANENT,
            observed_at="2026-08-23T00:00:00.100Z",
            reason="changed",
        )


def test_outbox_public_validation_and_terminal_state_guards(tmp_path: Path) -> None:
    for arguments, message in (
        ((True,), "database path"),
        ((tmp_path / "a.sqlite3",), "base backoff"),
        ((tmp_path / "b.sqlite3",), "maximum backoff"),
    ):
        kwargs = {}
        if message == "base backoff":
            kwargs["base_backoff_ms"] = 0
        elif message == "maximum backoff":
            kwargs.update(base_backoff_ms=10, maximum_backoff_ms=9)
        with pytest.raises(OutboxError, match=message):
            _repository(*arguments, **kwargs)  # type: ignore[arg-type]

    repository = _repository(tmp_path / "v3.sqlite3")
    with pytest.raises(OutboxError, match="mapping"):
        repository.enqueue(
            outbox_id="outbox:bad-payload",
            destination="mirror",
            payload=[],  # type: ignore[arg-type]
            created_at="2026-08-23T00:00:00.000Z",
        )
    with pytest.raises(OutboxError, match="source global"):
        repository.enqueue(
            outbox_id="outbox:bad-source",
            destination="mirror",
            payload={},
            source_global_sequence=True,
            created_at="2026-08-23T00:00:00.000Z",
        )
    with pytest.raises(KeyError):
        repository.get("outbox:missing")
    for limit in (True, 0, 1_001):
        with pytest.raises(OutboxError, match="page limit"):
            repository.due("2026-08-23T00:00:00.000Z", limit=limit)  # type: ignore[arg-type]

    repository.enqueue(
        outbox_id="outbox:guarded",
        destination="mirror",
        payload={},
        created_at="2026-08-23T00:00:00.000Z",
    )
    with pytest.raises(OutboxError, match="DeliveryOutcome"):
        repository.record_outcome(
            "outbox:guarded",
            operation_id="outbox_operation:bad-outcome",
            expected_revision=1,
            outcome="transient",  # type: ignore[arg-type]
            observed_at="2026-08-23T00:00:00.100Z",
        )
    with pytest.raises(OutboxError, match="fault hook"):
        repository.record_outcome(
            "outbox:guarded",
            operation_id="outbox_operation:bad-hook",
            expected_revision=1,
            outcome=DeliveryOutcome.ACKNOWLEDGED,
            observed_at="2026-08-23T00:00:00.100Z",
            fault_hook="bad",  # type: ignore[arg-type]
        )
    for operation_id, expected_revision, reason, message in (
        ("bad token", 1, "timeout", "bounded opaque"),
        ("outbox_operation:zero", 0, "timeout", "positive integer"),
        ("outbox_operation:stale", 2, "timeout", "stale"),
        ("outbox_operation:missing", 1, "timeout", "outbox:missing"),
    ):
        with pytest.raises((OutboxError, OutboxConflict, KeyError), match=message):
            repository.record_outcome(
                "outbox:missing" if operation_id.endswith("missing") else "outbox:guarded",
                operation_id=operation_id,
                expected_revision=expected_revision,
                outcome=DeliveryOutcome.TRANSIENT,
                observed_at="2026-08-23T00:00:00.100Z",
                reason=reason,
            )
    with pytest.raises(OutboxError, match="reason"):
        repository.record_outcome(
            "outbox:guarded",
            operation_id="outbox_operation:bad-reason",
            expected_revision=1,
            outcome=DeliveryOutcome.TRANSIENT,
            observed_at="2026-08-23T00:00:00.100Z",
            reason="not valid",
        )
    with pytest.raises(OutboxError, match="reason"):
        repository.record_outcome(
            "outbox:guarded",
            operation_id="outbox_operation:missing-reason",
            expected_revision=1,
            outcome=DeliveryOutcome.TRANSIENT,
            observed_at="2026-08-23T00:00:00.100Z",
        )

    permanent = repository.record_outcome(
        "outbox:guarded",
        operation_id="outbox_operation:permanent",
        expected_revision=1,
        outcome=DeliveryOutcome.PERMANENT,
        observed_at="2026-08-23T00:00:00.100Z",
        reason="rejected",
    )
    with pytest.raises(OutboxConflict, match="terminal"):
        repository.record_outcome(
            "outbox:guarded",
            operation_id="outbox_operation:again",
            expected_revision=permanent.revision,
            outcome=DeliveryOutcome.ACKNOWLEDGED,
            observed_at="2026-08-23T00:00:00.200Z",
        )
    quarantine = repository.quarantine(
        "outbox:guarded",
        operation_id="outbox_operation:quarantine",
        expected_revision=permanent.revision,
        observed_at="2026-08-23T00:00:00.200Z",
        reason="review",
    )
    assert (
        repository.quarantine(
            "outbox:guarded",
            operation_id="outbox_operation:quarantine",
            expected_revision=permanent.revision,
            observed_at="2026-08-23T00:00:00.200Z",
            reason="review",
        )
        == quarantine
    )

    pending = repository.enqueue(
        outbox_id="outbox:pending",
        destination="mirror",
        payload={},
        created_at="2026-08-23T00:00:00.000Z",
    )
    with pytest.raises(OutboxConflict, match="terminal failures"):
        repository.repair(
            "outbox:pending",
            operation_id="outbox_operation:repair-pending",
            expected_revision=pending.revision,
            observed_at="2026-08-23T00:00:00.200Z",
            reason="repair",
        )
    acknowledged = repository.record_outcome(
        "outbox:pending",
        operation_id="outbox_operation:ack",
        expected_revision=1,
        outcome=DeliveryOutcome.ACKNOWLEDGED,
        observed_at="2026-08-23T00:00:00.300Z",
    )
    with pytest.raises(OutboxConflict, match="immutable"):
        repository.quarantine(
            "outbox:pending",
            operation_id="outbox_operation:quarantine-ack",
            expected_revision=acknowledged.revision,
            observed_at="2026-08-23T00:00:00.400Z",
            reason="review",
        )

    malformed = OutboxItem(
        "outbox:manual",
        "mirror",
        None,
        "[]",
        "a" * 64,
        OutboxState.PENDING,
        1,
        0,
        "2026-08-23T00:00:00.000Z",
        None,
        "2026-08-23T00:00:00.000Z",
        "2026-08-23T00:00:00.000Z",
    )
    with pytest.raises(OutboxError, match="not an object"):
        malformed.payload()
