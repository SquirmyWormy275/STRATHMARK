from __future__ import annotations

import base64
import builtins
import struct
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path, PureWindowsPath

import pytest

from strathmark.v3.application.commands import CommandRequest, EventIntent
from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.commands import CommandEnvelope, CommandKind, InlinePayload
from strathmark.v3.contracts.events import AggregateKind, EventKind
from strathmark.v3.contracts.identifiers import IdempotencyKey, StableIdentifier
from strathmark.v3.infrastructure.integrity import (
    CheckpointRegistry,
    CriticalDatabaseCommit,
    CriticalIssueCoordinator,
    CriticalIssueIntent,
    CriticalJournal,
    IntegrityError,
    IntegrityKeyClass,
    IntegrityKeyIdentity,
    IntegrityTrustStore,
    P256EphemeralSigner,
    P256ExternalSigner,
    P256WindowsCNGSigner,
    RecoveryState,
    StorageIdentity,
    VerifiedRecoveryTopology,
    apply_key_rotation,
    issue_recovery_readiness,
    sign_key_rotation,
    sign_manifest,
    verify_manifest,
)
from strathmark.v3.infrastructure.sqlite.event_store import (
    AuthorityAnchor,
    EventStoreConflict,
    EventStoreError,
    EventStoreIntegrityError,
    SQLiteEventStore,
    verify_read_only_authority,
)
from strathmark.v3.infrastructure.sqlite.outbox import OutboxRepository
from strathmark.v3.infrastructure.sqlite.projections import SQLiteProjectionStore

NOW = "2026-08-23T01:00:00.000Z"
DIGEST = "a" * 64


def _provider_cng_signer(monkeypatch: pytest.MonkeyPatch, key_name: str) -> P256WindowsCNGSigner:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, utils

    import strathmark.v3.infrastructure.integrity as module

    private = ec.generate_private_key(ec.SECP256R1())

    class FakeProviderKey(module._WindowsCNGProviderKey):
        def attest_public_key(self) -> bytes:
            return private.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )

        def sign_digest(self, digest: bytes) -> bytes:
            return private.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))

    backend = object.__new__(FakeProviderKey)
    monkeypatch.setattr(
        module._WindowsCNGProviderKey,
        "open",
        classmethod(lambda cls, provider_name, observed_key_name: backend),
    )
    return P256WindowsCNGSigner.open(key_name)


def _intent(
    command: str = "command:issue-one", *, command_digest: str = "c" * 64
) -> CriticalIssueIntent:
    return CriticalIssueIntent(
        command_id=command,
        command_digest=command_digest,
        approval_snapshot_digest="b" * 64,
        expected_versions=(("field:one", 1),),
        receipt_ids=("receipt:one",),
        created_at=NOW,
    )


def _field_request(
    command_id: str, kind: CommandKind, event: EventKind, *, expected: int
) -> CommandRequest:
    field = StableIdentifier("field:one")
    command = CommandEnvelope(
        kind,
        IdempotencyKey(command_id),
        field,
        ((str(field), expected),),
        StableIdentifier("actor:judge"),
        InlinePayload.from_value(
            {
                "field_id": str(field),
                "revision": expected + 1,
                "approval_snapshot_digest": "b" * 64,
            }
        ),
    )
    return CommandRequest(
        StableIdentifier("actor:judge"),
        command,
        (EventIntent(AggregateKind.FIELD, field, event),),
        "strathmark-v3-test-result-v1",
        {"receipt_id": "receipt:one"},
        NOW,
        expected + 1,
    )


def test_p256_manifests_rotate_keys_without_forgetting_prior_verification() -> None:
    old = P256EphemeralSigner.generate("integrity-key:old")
    new = P256EphemeralSigner.generate("integrity-key:new")
    trust = IntegrityTrustStore((old.identity,))
    checkpoint = sign_manifest("checkpoint", {"tip": DIGEST}, signer=old, created_at=NOW)
    assert verify_manifest(checkpoint, trust) == {"tip": DIGEST}

    rotation = sign_key_rotation(old, new.identity, created_at=NOW)
    rotated = apply_key_rotation(trust, rotation)
    assert verify_manifest(checkpoint, rotated) == {"tip": DIGEST}
    assert verify_manifest(
        sign_manifest("checkpoint", {"tip": "c" * 64}, signer=new, created_at=NOW),
        rotated,
    ) == {"tip": "c" * 64}


def test_dev_or_same_physical_device_truthfully_fails_production_issue_readiness() -> None:
    dev = P256EphemeralSigner.generate("integrity-key:dev")
    primary = StorageIdentity("device:one", "host:race", "site:venue")
    recovery_same = StorageIdentity("device:one", "host:race", "site:venue")
    assert issue_recovery_readiness(primary, recovery_same, dev).ready is False
    assert set(issue_recovery_readiness(primary, recovery_same, dev).reasons) == {
        "development_signing_key",
        "recovery_device_not_distinct",
    }

    production_identity = IntegrityKeyIdentity(
        key_id="integrity-key:cng",
        key_class=IntegrityKeyClass.PRODUCTION_CNG,
        provider="windows_cng_p256_sha256",
        public_key_der_b64=dev.identity.public_key_der_b64,
    )
    distinct = StorageIdentity("device:two", "host:race", "site:venue")
    with pytest.raises(IntegrityError, match="cannot assert production"):
        P256ExternalSigner(production_identity, dev.sign)
    assert issue_recovery_readiness(primary, distinct, dev).ready is False


def test_production_cng_capability_cannot_be_fabricated_or_opened_as_rehearsal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import strathmark.v3.infrastructure.integrity as module

    with pytest.raises(IntegrityError, match="OS provider"):
        P256WindowsCNGSigner(object())
    for key_name in ("", "x" * 513, "bad\x00name"):
        with pytest.raises(IntegrityError, match="key name"):
            P256WindowsCNGSigner.open(key_name)
    with pytest.raises(IntegrityError, match="provider"):
        P256WindowsCNGSigner.open("strathmark", provider_name="Caller Fabricated Provider")
    with monkeypatch.context() as context:
        context.setattr(module.os, "name", "posix")
        with pytest.raises(IntegrityError, match="unavailable"):
            P256WindowsCNGSigner.open("strathmark")

    production = _provider_cng_signer(monkeypatch, "strathmark-attested")
    assert production.sign(b"provider-bound")
    assert issue_recovery_readiness(
        StorageIdentity("device:one", "host:race", "site:venue"),
        StorageIdentity("device:two", "host:race", "site:venue"),
        production,
    ).ready


def test_windows_cng_native_provider_success_and_failure_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils

    import strathmark.v3.infrastructure.integrity as module

    private = ec.generate_private_key(ec.SECP256R1())
    numbers = private.public_key().public_numbers()
    public_blob = (
        struct.pack("<II", 0x31534345, 32)
        + numbers.x.to_bytes(32, "big")
        + numbers.y.to_bytes(32, "big")
    )

    class FakeNcrypt:
        def __init__(self, attack: str = "") -> None:
            self.attack = attack
            self.freed: list[int] = []

        def NCryptOpenStorageProvider(self, handle, _provider, _flags):
            if self.attack == "provider":
                return 5
            handle._obj.value = 11
            return 0

        def NCryptOpenKey(self, _provider, handle, _name, _legacy, _flags):
            if self.attack == "key":
                return 6
            handle._obj.value = 22
            return 0

        def NCryptFreeObject(self, handle):
            self.freed.append(int(handle.value))
            return 0

        def NCryptGetProperty(self, _key, _name, policy, size, returned, _flags):
            policy._obj.value = 1 if self.attack == "exportable" else 0
            returned._obj.value = 0 if self.attack == "policy-size" else size
            return 7 if self.attack == "policy-status" else 0

        def NCryptExportKey(
            self, _key, _export, _kind, _parameters, buffer, size, returned, _flags
        ):
            if buffer is None:
                returned._obj.value = 71 if self.attack == "public-size" else 72
                return 8 if self.attack == "public-sizing-status" else 0
            if self.attack == "public-status":
                return 9
            blob = (
                struct.pack("<II", 0, 32) + public_blob[8:]
                if self.attack == "public-magic"
                else public_blob
            )
            for index, value in enumerate(blob):
                buffer[index] = value
            returned._obj.value = len(blob)
            return 0

        def NCryptSignHash(self, _key, _padding, digest, _length, buffer, _size, returned, _flags):
            if buffer is None:
                returned._obj.value = 63 if self.attack == "signature-size" else 64
                return 10 if self.attack == "signature-sizing-status" else 0
            if self.attack == "signature-status":
                return 11
            der = private.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
            r, s = utils.decode_dss_signature(der)
            raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
            for index, value in enumerate(raw):
                buffer[index] = value
            returned._obj.value = len(raw)
            return 0

    def open_with(fake: FakeNcrypt) -> P256WindowsCNGSigner:
        with monkeypatch.context() as context:
            context.setattr(module.os, "name", "nt")
            context.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: fake, raising=False)
            return P256WindowsCNGSigner.open("strathmark-native")

    signer = open_with(FakeNcrypt())
    assert signer.sign(b"native-provider")
    with pytest.raises(IntegrityError, match="immutable bytes"):
        signer.sign("bad")  # type: ignore[arg-type]
    signer._backend.attest_public_key = lambda: b"changed"
    with pytest.raises(IntegrityError, match="changed"):
        signer.attest_provider()

    failures = (
        ("provider", "provider open"),
        ("key", "key open"),
        ("exportable", "exportable"),
        ("policy-size", "exportable"),
        ("policy-status", "exportable"),
        ("public-size", "public identity"),
        ("public-sizing-status", "public identity"),
        ("public-status", "public-key export"),
        ("public-magic", "P-256"),
    )
    for attack, message in failures:
        with pytest.raises(IntegrityError, match=message):
            open_with(FakeNcrypt(attack))

    for attack, message in (
        ("signature-size", "sizing"),
        ("signature-sizing-status", "sizing"),
        ("signature-status", "signing failed"),
    ):
        failing = open_with(FakeNcrypt(attack))
        with pytest.raises(IntegrityError, match=message):
            failing.sign(b"native-provider")


@pytest.mark.parametrize("stage", ["after_intent", "after_database_commit", "after_marker"])
def test_every_critical_issue_crash_prefix_reconciles_without_duplicate_issue(
    tmp_path: Path, stage: str
) -> None:
    signer = P256EphemeralSigner.generate("integrity-key:dev")
    trust = IntegrityTrustStore((signer.identity,))
    journal = CriticalJournal(tmp_path / "recovery", signer=signer, trust_store=trust)
    coordinator = CriticalIssueCoordinator.for_rehearsal(journal)
    committed: dict[str, CriticalDatabaseCommit] = {}

    def database_commit(intent_digest: str) -> CriticalDatabaseCommit:
        return committed.setdefault(
            "command:issue-one",
            CriticalDatabaseCommit(7, "d" * 64, ("receipt:one",), intent_digest),
        )

    def fail(observed: str) -> None:
        if observed == stage:
            raise RuntimeError(stage)

    with pytest.raises(RuntimeError, match=stage):
        coordinator.execute(_intent(), database_commit=database_commit, fault_hook=fail)

    reconciled = journal.reconcile(
        database_lookup=lambda command: committed.get(command),
        manager_receipts={"command:issue-one": ("receipt:one",)} if committed else {},
    )
    if stage == "after_intent":
        assert reconciled[0].state is RecoveryState.INTENT_ONLY
        exact = coordinator.execute(_intent(), database_commit=database_commit)
        assert exact.global_sequence == 7
    else:
        assert reconciled[0].state is RecoveryState.COMMITTED
    assert len(committed) <= 1


def test_damaged_journal_or_manager_receipt_disagreement_blocks_recovery(tmp_path: Path) -> None:
    signer = P256EphemeralSigner.generate("integrity-key:dev")
    journal = CriticalJournal(
        tmp_path / "recovery",
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
    )
    coordinator = CriticalIssueCoordinator.for_rehearsal(journal)
    commit = coordinator.execute(
        _intent(),
        database_commit=lambda digest: CriticalDatabaseCommit(
            1, "d" * 64, ("receipt:one",), digest
        ),
    )
    journal.marker_path("command:issue-one").write_text("{}", encoding="utf-8")
    with pytest.raises(IntegrityError, match="signed|manifest"):
        journal.reconcile(database_lookup=lambda _command: commit, manager_receipts={})

    clean = CriticalJournal(
        tmp_path / "clean",
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
    )
    CriticalIssueCoordinator.for_rehearsal(clean).execute(
        _intent(),
        database_commit=lambda digest: CriticalDatabaseCommit(
            1, "d" * 64, ("receipt:one",), digest
        ),
    )
    with pytest.raises(IntegrityError, match="manager"):
        clean.reconcile(
            database_lookup=lambda _command: commit,
            manager_receipts={"command:issue-one": ("receipt:other",)},
        )


def test_critical_contracts_reject_duplicate_or_unvalidated_authority() -> None:
    with pytest.raises(IntegrityError, match="expected versions"):
        CriticalIssueIntent(
            "command:bad",
            "c" * 64,
            "b" * 64,
            (("not an id", 1),),
            ("receipt:one",),
            NOW,
        )
    with pytest.raises(IntegrityError, match="repeat"):
        CriticalIssueIntent(
            "command:bad",
            "c" * 64,
            "b" * 64,
            (("field:one", 1), ("field:one", 1)),
            ("receipt:one",),
            NOW,
        )
    with pytest.raises(IntegrityError, match="repeat"):
        CriticalDatabaseCommit(1, "d" * 64, ("receipt:one", "receipt:one"), "e" * 64)


def test_orphan_marker_and_unverified_production_topology_fail_closed(tmp_path: Path) -> None:
    signer = P256EphemeralSigner.generate("integrity-key:dev")
    journal = CriticalJournal(
        tmp_path / "recovery",
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
    )
    journal.marker_path("command:orphan").write_text("{}", encoding="utf-8")
    with pytest.raises(IntegrityError, match="orphan|unexplained"):
        journal.reconcile(database_lookup=lambda _command: None, manager_receipts={})

    primary = tmp_path / "primary"
    recovery = tmp_path / "recovery-volume"
    primary.mkdir()
    recovery.mkdir()
    topology = VerifiedRecoveryTopology.probe(
        primary, recovery, host_id="host:race", site_id="site:venue"
    )
    assert topology.distinct_physical_devices is False
    with pytest.raises(IntegrityError, match="production"):
        CriticalIssueCoordinator.for_production(
            journal,
            authority_database_path=primary,
            host_id="host:race",
            site_id="site:venue",
        )


def test_production_topology_cannot_be_fabricated_from_caller_tokens() -> None:
    with pytest.raises(IntegrityError, match="probe"):
        VerifiedRecoveryTopology(
            StorageIdentity("device:one", "host:race", "site:venue"),
            StorageIdentity("device:two", "host:race", "site:venue"),
            True,
        )


def test_concurrent_critical_publish_is_no_clobber_for_intent_and_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import strathmark.v3.infrastructure.integrity as module

    signer = P256EphemeralSigner.generate("integrity-key:dev")
    journal = CriticalJournal(
        tmp_path / "recovery",
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
    )
    barrier = threading.Barrier(2)
    original = module._publish_no_clobber

    def synchronized(path: Path, payload: bytes) -> bool:
        barrier.wait(timeout=5)
        return original(path, payload)

    monkeypatch.setattr(module, "_publish_no_clobber", synchronized)
    intents = [_intent(), _intent()]
    intents[1] = CriticalIssueIntent(
        intents[1].command_id,
        intents[1].command_digest,
        "c" * 64,
        intents[1].expected_versions,
        intents[1].receipt_ids,
        intents[1].created_at,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(journal.record_intent, intent) for intent in intents]
    outcomes = []
    for result in results:
        try:
            outcomes.append(result.result())
        except IntegrityError:
            outcomes.append(None)
    assert sum(item is not None for item in outcomes) == 1
    winner = journal._read(journal.intent_path("command:issue-one"), "critical_intent")
    commits = (
        CriticalDatabaseCommit(7, "d" * 64, ("receipt:one",), winner.body_digest),
        CriticalDatabaseCommit(7, "e" * 64, ("receipt:one",), winner.body_digest),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            pool.submit(journal.record_commit, "command:issue-one", commit, created_at=NOW)
            for commit in commits
        ]
    outcomes = []
    for result in results:
        try:
            outcomes.append(result.result())
        except IntegrityError:
            outcomes.append(None)
    assert sum(item is not None for item in outcomes) == 1


def test_event_store_issue_protocol_recovers_database_committed_marker_missing_exactly_once(
    tmp_path: Path,
) -> None:
    store = SQLiteEventStore(tmp_path / "authority.sqlite3")
    store.execute(
        _field_request(
            "command:prepare", CommandKind.OPTIMIZE_FIELD, EventKind.FIELD_OPTIMIZED, expected=0
        )
    )
    request = _field_request(
        "command:issue-one",
        CommandKind.ACKNOWLEDGE_ISSUE,
        EventKind.FIELD_ISSUED,
        expected=1,
    )
    signer = P256EphemeralSigner.generate("integrity-key:dev")
    journal = CriticalJournal(
        tmp_path / "recovery",
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
    )
    coordinator = CriticalIssueCoordinator.for_rehearsal(journal)

    def fail(stage: str) -> None:
        if stage == "after_database_commit":
            raise RuntimeError(stage)

    with pytest.raises(RuntimeError, match="after_database_commit"):
        store.execute_critical_issue(
            request,
            intent=_intent(command_digest=canonical_digest(request.command.to_dict())),
            coordinator=coordinator,
            critical_fault_hook=fail,
        )
    assert store.event_count() == 2
    recovered = store.execute_critical_issue(
        request,
        intent=_intent(command_digest=canonical_digest(request.command.to_dict())),
        coordinator=coordinator,
    )
    assert recovered.last_global_sequence == 2
    assert store.event_count() == 2
    assert journal.marker_path("command:issue-one").is_file()


def test_critical_issue_rejects_each_signed_binding_mismatch_before_journal_or_commit(
    tmp_path: Path,
) -> None:
    store = SQLiteEventStore(tmp_path / "binding.sqlite3")
    store.execute(
        _field_request(
            "command:binding-prepare",
            CommandKind.OPTIMIZE_FIELD,
            EventKind.FIELD_OPTIMIZED,
            expected=0,
        )
    )
    request = _field_request(
        "command:binding-issue",
        CommandKind.ACKNOWLEDGE_ISSUE,
        EventKind.FIELD_ISSUED,
        expected=1,
    )
    signer = P256EphemeralSigner.generate("integrity-key:binding")
    journal = CriticalJournal(
        tmp_path / "binding-journal",
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
    )
    coordinator = CriticalIssueCoordinator.for_rehearsal(journal)
    valid = _intent(
        "command:binding-issue",
        command_digest=canonical_digest(request.command.to_dict()),
    )
    mutations = (
        (replace(valid, command_digest="d" * 64), "command digest"),
        (replace(valid, expected_versions=(("field:one", 0),)), "expected versions"),
        (replace(valid, approval_snapshot_digest="d" * 64), "approval snapshot"),
        (replace(valid, receipt_ids=("receipt:other",)), "receipts"),
    )
    for intent, message in mutations:
        with pytest.raises(EventStoreError, match=message):
            store.execute_critical_issue(request, intent=intent, coordinator=coordinator)
        assert store.event_count() == 1
        assert not journal.intent_path(valid.command_id).exists()


def test_checkpoint_registry_detects_database_tail_loss_and_replays_exact_bytes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "authority.sqlite3"
    store = SQLiteEventStore(database)
    stale = tmp_path / "stale-authority.sqlite3"
    with (
        __import__("sqlite3").connect(database) as source,
        __import__("sqlite3").connect(stale) as destination,
    ):
        source.backup(destination)
    store.execute(
        _field_request(
            "command:prepare", CommandKind.OPTIMIZE_FIELD, EventKind.FIELD_OPTIMIZED, expected=0
        )
    )
    signer = P256EphemeralSigner.generate("integrity-key:checkpoint")
    outbox = OutboxRepository(
        database,
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
        active_key_id=signer.identity.key_id,
    )
    registry = CheckpointRegistry(
        tmp_path / "checkpoint-registry",
        bootstrap_identity=signer.identity,
    )
    checkpoint = registry.create_checkpoint(database, signer=signer, created_at=NOW, outbox=outbox)
    assert registry.verify_database(database, require_current=True) == checkpoint
    exported = outbox.get(f"checkpoint:{checkpoint.manifest.body_digest}")
    assert base64.b64decode(exported.payload()["signed_checkpoint_b64"]) == canonical_bytes(
        checkpoint.manifest.to_dict()
    )
    assert SQLiteEventStore.from_checkpoint_registry(database, registry).event_count() == 1

    with pytest.raises(IntegrityError, match="checkpoint|authority"):
        registry.verify_database(stale, require_current=False)


def test_checkpoint_registry_rotation_restart_gap_and_old_checkpoint_verification(
    tmp_path: Path,
) -> None:
    database = tmp_path / "authority.sqlite3"
    SQLiteEventStore(database)
    old = P256EphemeralSigner.generate("integrity-key:old-checkpoint")
    new = P256EphemeralSigner.generate("integrity-key:new-checkpoint")
    root = tmp_path / "checkpoint-registry"
    registry = CheckpointRegistry(root, bootstrap_identity=old.identity)
    projections = SQLiteProjectionStore(database)
    projections.capture_projection_checkpoint()
    first = registry.create_checkpoint(database, signer=old, created_at=NOW)
    assert projections.rebuild_from_checkpoint_registry(registry)
    registry.rotate_key(old, new.identity, created_at=NOW)
    restarted = CheckpointRegistry(root, bootstrap_identity=old.identity)
    assert restarted.verify_checkpoint(first.manifest) == first
    second = restarted.create_checkpoint(database, signer=new, created_at=NOW)
    assert restarted.latest_checkpoint() == second

    rotation = next((root / "rotations").glob("*.json"))
    rotation.rename(rotation.with_name("0000000000000002.json"))
    with pytest.raises(IntegrityError, match="gap|sequence"):
        CheckpointRegistry(root, bootstrap_identity=old.identity)


def test_checkpoint_advance_rejects_rollback_and_same_height_fork_after_restart(
    tmp_path: Path,
) -> None:
    import sqlite3

    database = tmp_path / "authority.sqlite3"
    store = SQLiteEventStore(database)
    stale = tmp_path / "stale.sqlite3"
    with (
        closing(sqlite3.connect(database)) as source,
        closing(sqlite3.connect(stale)) as destination,
    ):
        source.backup(destination)
    store.execute(
        _field_request(
            "command:ancestor", CommandKind.OPTIMIZE_FIELD, EventKind.FIELD_OPTIMIZED, expected=0
        )
    )
    fork = tmp_path / "fork.sqlite3"
    fork_store = SQLiteEventStore(fork)
    fork_store.execute(
        _field_request(
            "command:fork", CommandKind.OPTIMIZE_FIELD, EventKind.FIELD_OPTIMIZED, expected=0
        )
    )
    signer = P256EphemeralSigner.generate("integrity-key:ancestry")
    root = tmp_path / "ancestry-registry"
    CheckpointRegistry(root, bootstrap_identity=signer.identity).create_checkpoint(
        database, signer=signer, created_at=NOW
    )
    restarted = CheckpointRegistry(root, bootstrap_identity=signer.identity)
    with pytest.raises(IntegrityError, match="rolls back"):
        restarted.create_checkpoint(stale, signer=signer, created_at=NOW)
    with pytest.raises(IntegrityError, match="does not descend"):
        restarted.create_checkpoint(fork, signer=signer, created_at=NOW)
    store.execute(
        _field_request(
            "command:descendant",
            CommandKind.ACKNOWLEDGE_ISSUE,
            EventKind.FIELD_ISSUED,
            expected=1,
        )
    )
    advanced = CheckpointRegistry(root, bootstrap_identity=signer.identity).create_checkpoint(
        database, signer=signer, created_at=NOW
    )
    assert advanced.authority_sequence == 2


def test_checkpoint_rejects_same_sequence_fact_drift_and_inconsistent_ancestry_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import strathmark.v3.infrastructure.integrity as module
    import strathmark.v3.infrastructure.sqlite.event_store as event_store_module

    database = tmp_path / "same-sequence.sqlite3"
    SQLiteEventStore(database)
    signer = P256EphemeralSigner.generate("integrity-key:same-sequence")
    registry = CheckpointRegistry(
        tmp_path / "same-sequence-registry", bootstrap_identity=signer.identity
    )
    registry.create_checkpoint(database, signer=signer, created_at=NOW)
    facts = module._database_checkpoint_facts(database)
    changed = {**facts, "projection_digest": "f" * 64}
    with monkeypatch.context() as context:
        context.setattr(module, "_database_checkpoint_facts", lambda *_args, **_kwargs: changed)
        with pytest.raises(IntegrityError, match="unchanged authority sequence"):
            registry.create_checkpoint(database, signer=signer, created_at=NOW)

    with monkeypatch.context() as context:
        context.setattr(
            event_store_module,
            "verify_read_only_authority",
            lambda *_args, **_kwargs: AuthorityAnchor(1, "f" * 64),
        )
        with pytest.raises(IntegrityError, match="different authority tip"):
            module._database_checkpoint_facts(database, required_ancestor=(0, "0" * 64))


def test_concurrent_checkpoint_and_rotation_publish_exact_retry_or_material_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import strathmark.v3.infrastructure.integrity as module

    signer = P256EphemeralSigner.generate("integrity-key:registry-race")
    database = tmp_path / "authority.sqlite3"
    SQLiteEventStore(database)

    def race_pair(callable_one, callable_two):
        barrier = threading.Barrier(2)
        original = module._publish_no_clobber

        def synchronized(path: Path, payload: bytes) -> bool:
            barrier.wait(timeout=5)
            return original(path, payload)

        with monkeypatch.context() as context:
            context.setattr(module, "_publish_no_clobber", synchronized)
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(callable_one), pool.submit(callable_two)]
                outcomes = []
                for future in futures:
                    try:
                        outcomes.append(future.result())
                    except IntegrityError:
                        outcomes.append(None)
        return outcomes

    checkpoint_root = tmp_path / "checkpoint-race"
    first = CheckpointRegistry(checkpoint_root, bootstrap_identity=signer.identity)
    second = CheckpointRegistry(checkpoint_root, bootstrap_identity=signer.identity)
    same = race_pair(
        lambda: first.create_checkpoint(database, signer=signer, created_at=NOW),
        lambda: second.create_checkpoint(database, signer=signer, created_at=NOW),
    )
    assert same[0] == same[1]

    changed_database = tmp_path / "changed.sqlite3"
    changed_store = SQLiteEventStore(changed_database)
    changed_store.execute(
        _field_request(
            "command:changed", CommandKind.OPTIMIZE_FIELD, EventKind.FIELD_OPTIMIZED, expected=0
        )
    )
    conflict_root = tmp_path / "checkpoint-conflict"
    left = CheckpointRegistry(conflict_root, bootstrap_identity=signer.identity)
    right = CheckpointRegistry(conflict_root, bootstrap_identity=signer.identity)
    different = race_pair(
        lambda: left.create_checkpoint(database, signer=signer, created_at=NOW),
        lambda: right.create_checkpoint(changed_database, signer=signer, created_at=NOW),
    )
    assert sum(item is not None for item in different) == 1
    assert CheckpointRegistry(conflict_root, bootstrap_identity=signer.identity).latest_checkpoint()

    next_key = P256EphemeralSigner.generate("integrity-key:next-same")
    rotation_root = tmp_path / "rotation-race"
    first_rotation = CheckpointRegistry(rotation_root, bootstrap_identity=signer.identity)
    second_rotation = CheckpointRegistry(rotation_root, bootstrap_identity=signer.identity)
    same_rotation = race_pair(
        lambda: first_rotation.rotate_key(signer, next_key.identity, created_at=NOW),
        lambda: second_rotation.rotate_key(signer, next_key.identity, created_at=NOW),
    )
    assert same_rotation[0] == same_rotation[1]

    other_key = P256EphemeralSigner.generate("integrity-key:next-other")
    rotation_conflict_root = tmp_path / "rotation-conflict"
    first_rotation = CheckpointRegistry(rotation_conflict_root, bootstrap_identity=signer.identity)
    second_rotation = CheckpointRegistry(rotation_conflict_root, bootstrap_identity=signer.identity)
    different_rotation = race_pair(
        lambda: first_rotation.rotate_key(signer, next_key.identity, created_at=NOW),
        lambda: second_rotation.rotate_key(signer, other_key.identity, created_at=NOW),
    )
    assert sum(item is not None for item in different_rotation) == 1


def test_registry_record_read_retries_transient_os_failure_but_remains_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import strathmark.v3.infrastructure.integrity as module

    signer = P256EphemeralSigner.generate("integrity-key:registry-read")
    database = tmp_path / "authority.sqlite3"
    SQLiteEventStore(database)
    root = tmp_path / "registry"
    CheckpointRegistry(root, bootstrap_identity=signer.identity).create_checkpoint(
        database, signer=signer, created_at=NOW
    )
    record = (root / "checkpoints" / "0000000000000001.json").resolve()
    original_open = Path.open
    transient_attempts = 0

    def transient_open(path: Path, *args: object, **kwargs: object):
        nonlocal transient_attempts
        if path.resolve() == record and transient_attempts < 2:
            transient_attempts += 1
            raise PermissionError("simulated Windows sharing violation")
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(Path, "open", transient_open)
        checkpoint = CheckpointRegistry(
            root, bootstrap_identity=signer.identity
        ).latest_checkpoint()
    assert checkpoint.checkpoint_sequence == 1
    assert transient_attempts == 2

    permanent_attempts = 0

    def permanently_unreadable(path: Path, *args: object, **kwargs: object):
        nonlocal permanent_attempts
        if path.resolve() == record:
            permanent_attempts += 1
            raise PermissionError("simulated permanent denial")
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(Path, "open", permanently_unreadable)
        with pytest.raises(IntegrityError, match="temporarily unreadable"):
            CheckpointRegistry(root, bootstrap_identity=signer.identity)
    assert permanent_attempts == module._REGISTRY_READ_ATTEMPTS

    record.write_bytes(b"x" * (module._REGISTRY_RECORD_MAX_BYTES + 1))
    with pytest.raises(IntegrityError, match="bounded size limit"):
        CheckpointRegistry(root, bootstrap_identity=signer.identity)


def _manifest_with_body(
    signer: P256EphemeralSigner,
    *,
    kind: str,
    payload: object,
    body_kind: str | None = None,
) -> object:
    """Build a correctly signed envelope around deliberately adversarial body material."""

    from strathmark.v3.infrastructure.integrity import SignedManifest

    body = {
        "schema_version": "strathmark-v3-integrity-body-v1",
        "kind": body_kind or kind,
        "algorithm": "ecdsa-p256-sha256",
        "key_id": signer.identity.key_id,
        "created_at": NOW,
        "payload": payload,
    }
    encoded = canonical_bytes(body)
    return SignedManifest(
        kind,
        encoded.decode("utf-8"),
        canonical_digest(body),
        signer.identity.key_id,
        base64.b64encode(signer.sign(encoded)).decode("ascii"),
    )


def test_integrity_contract_and_signature_rejection_matrix(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    import strathmark.v3.infrastructure.integrity as module

    signer = P256EphemeralSigner.generate("integrity-key:coverage")
    identity = signer.identity
    trust = IntegrityTrustStore((identity,))

    with pytest.raises(IntegrityError, match="class"):
        IntegrityKeyIdentity(identity.key_id, "bad", identity.provider, identity.public_key_der_b64)  # type: ignore[arg-type]
    with pytest.raises(IntegrityError, match="base64"):
        IntegrityKeyIdentity(identity.key_id, identity.key_class, identity.provider, "!")
    with pytest.raises(IntegrityError, match="length"):
        IntegrityKeyIdentity(
            identity.key_id,
            identity.key_class,
            identity.provider,
            base64.b64encode(b"short").decode("ascii"),
        )
    invalid_identity = identity.to_dict()
    invalid_identity["key_class"] = "unknown"
    with pytest.raises(IntegrityError, match="unknown"):
        IntegrityKeyIdentity.from_dict(invalid_identity)
    with pytest.raises(IntegrityError, match="identity"):
        P256ExternalSigner("bad", signer.sign)  # type: ignore[arg-type]
    with pytest.raises(IntegrityError, match="callable"):
        P256ExternalSigner(identity, None)  # type: ignore[arg-type]
    with pytest.raises(IntegrityError, match="invalid signature"):
        P256ExternalSigner(identity, lambda _payload: b"").sign(b"payload")
    external = P256ExternalSigner(identity, signer.sign)
    assert external.identity == identity
    assert external.sign(b"payload")
    with pytest.raises(IntegrityError, match="immutable bytes"):
        signer.sign("payload")  # type: ignore[arg-type]

    signed = sign_manifest("checkpoint", {"tip": DIGEST}, signer=signer, created_at=NOW)
    with pytest.raises(IntegrityError, match="canonical JSON"):
        module.SignedManifest("checkpoint", "{", DIGEST, identity.key_id, "AA==")
    with pytest.raises(IntegrityError, match="digest or encoding"):
        module.SignedManifest("checkpoint", "{}", DIGEST, identity.key_id, "AA==")
    with pytest.raises(IntegrityError, match="base64"):
        module.SignedManifest(signed.kind, signed.body_json, signed.body_digest, signed.key_id, "!")
    with pytest.raises(IntegrityError, match="empty"):
        module.SignedManifest(signed.kind, signed.body_json, signed.body_digest, signed.key_id, "")
    with pytest.raises(IntegrityError, match="version"):
        module.SignedManifest(
            signed.kind,
            signed.body_json,
            signed.body_digest,
            signed.key_id,
            signed.signature_der_b64,
            "future",
        )
    scalar = module.SignedManifest(
        "checkpoint",
        "[]",
        canonical_digest([]),
        identity.key_id,
        "AA==",
    )
    with pytest.raises(IntegrityError, match="object"):
        scalar.body()

    for identities in ((), [identity], ("bad",)):
        with pytest.raises(IntegrityError, match="trust store"):
            IntegrityTrustStore(identities)  # type: ignore[arg-type]
    with pytest.raises(IntegrityError, match="repeat"):
        IntegrityTrustStore((identity, identity))
    with pytest.raises(IntegrityError, match="not trusted"):
        trust.identity("integrity-key:unknown")
    with pytest.raises(IntegrityError, match="already exists"):
        trust.add(identity)
    with pytest.raises(IntegrityError, match="mapping"):
        sign_manifest("checkpoint", [], signer=signer, created_at=NOW)  # type: ignore[arg-type]
    with pytest.raises(IntegrityError, match="typed"):
        verify_manifest("bad", trust)  # type: ignore[arg-type]
    mismatch = _manifest_with_body(signer, kind="checkpoint", body_kind="key_rotation", payload={})
    with pytest.raises(IntegrityError, match="binding"):
        verify_manifest(mismatch, trust)  # type: ignore[arg-type]
    non_object = _manifest_with_body(signer, kind="checkpoint", payload=[])
    with pytest.raises(IntegrityError, match="payload"):
        verify_manifest(non_object, trust)  # type: ignore[arg-type]
    tampered = module.SignedManifest(
        signed.kind,
        signed.body_json,
        signed.body_digest,
        signed.key_id,
        base64.b64encode(b"invalid-der-signature").decode("ascii"),
    )
    with pytest.raises(IntegrityError, match="signature"):
        verify_manifest(tampered, trust)

    with pytest.raises(IntegrityError, match="key-rotation"):
        apply_key_rotation(trust, signed)
    rotation_without_key = sign_manifest("key_rotation", {}, signer=signer, created_at=NOW)
    with pytest.raises(IntegrityError, match="next key"):
        apply_key_rotation(trust, rotation_without_key)

    rsa_public = rsa.generate_private_key(public_exponent=65_537, key_size=2048).public_key()
    rsa_der = rsa_public.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    with pytest.raises(IntegrityError, match="P-256"):
        IntegrityKeyIdentity(
            "integrity-key:rsa",
            IntegrityKeyClass.DEVELOPMENT_EPHEMERAL,
            "cryptography_ephemeral_p256_sha256",
            base64.b64encode(rsa_der).decode("ascii"),
        )
    with pytest.raises(IntegrityError, match="DER is invalid"):
        module._load_public_key(b"x" * 80)

    original_import = builtins.__import__

    def missing_crypto(name: str, *args: object, **kwargs: object) -> object:
        if name.startswith("cryptography"):
            raise ImportError("missing")
        return original_import(name, *args, **kwargs)

    with pytest.MonkeyPatch.context() as context:
        context.setattr(builtins, "__import__", missing_crypto)
        with pytest.raises(IntegrityError, match="cryptography"):
            module._crypto()
    with pytest.raises(IntegrityError, match="token"):
        module._require_token("BAD TOKEN", "token")
    with pytest.raises(IntegrityError, match="digest"):
        module._require_digest("A" * 64, "digest")


def test_checkpoint_registry_rejection_and_tamper_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    import strathmark.v3.infrastructure.integrity as module

    signer = P256EphemeralSigner.generate("integrity-key:registry-coverage")
    other = P256EphemeralSigner.generate("integrity-key:registry-other")
    with pytest.raises(IntegrityError, match="bootstrap"):
        CheckpointRegistry(tmp_path / "bad", bootstrap_identity="bad")  # type: ignore[arg-type]

    empty = CheckpointRegistry(tmp_path / "empty", bootstrap_identity=signer.identity)
    assert empty.trust_store.identity(signer.identity.key_id) == signer.identity
    assert empty.active_identity == signer.identity
    with pytest.raises(IntegrityError, match="no trusted"):
        empty.latest_checkpoint()
    with pytest.raises(IntegrityError, match="active"):
        empty.rotate_key(other, other.identity, created_at=NOW)

    database = tmp_path / "authority.sqlite3"
    store = SQLiteEventStore(database)
    with pytest.raises(IntegrityError, match="active"):
        empty.create_checkpoint(database, signer=other, created_at=NOW)
    checkpoint = empty.create_checkpoint(database, signer=signer, created_at=NOW)
    unknown = sign_manifest("checkpoint", {"tip": DIGEST}, signer=signer, created_at=NOW)
    with pytest.raises(IntegrityError, match="not present"):
        empty.verify_checkpoint(unknown)
    store.execute(
        _field_request(
            "command:coverage-tail",
            CommandKind.OPTIMIZE_FIELD,
            EventKind.FIELD_OPTIMIZED,
            expected=0,
        )
    )
    with pytest.raises(IntegrityError, match="differs"):
        empty.verify_database(database, require_current=True)
    assert empty.verify_database(database, require_current=False) == checkpoint

    conflicting = tmp_path / "bootstrap-conflict"
    CheckpointRegistry(conflicting, bootstrap_identity=signer.identity)
    with pytest.raises(IntegrityError, match="differs"):
        CheckpointRegistry(conflicting, bootstrap_identity=other.identity)

    original_read_bytes = Path.read_bytes

    def unreadable(path: Path) -> bytes:
        if path.name == "bootstrap-key.json":
            raise OSError("denied")
        return original_read_bytes(path)

    with monkeypatch.context() as context:
        context.setattr(Path, "read_bytes", unreadable)
        with pytest.raises(IntegrityError, match="cannot be read"):
            CheckpointRegistry(tmp_path / "unreadable", bootstrap_identity=signer.identity)

    # Every signed rotation/checkpoint linkage field fails closed independently.
    def write_rotation(root: Path, payload: dict[str, object], *, key=P256EphemeralSigner) -> None:
        del key
        manifest = sign_manifest("key_rotation", payload, signer=signer, created_at=NOW)
        (root / "rotations" / "0000000000000001.json").write_bytes(
            canonical_bytes(manifest.to_dict())
        )

    cases = (
        (
            {
                "rotation_sequence": 2,
                "previous_rotation_digest": "0" * 64,
                "next_key": other.identity.to_dict(),
            },
            "sequence",
        ),
        (
            {
                "rotation_sequence": 1,
                "previous_rotation_digest": "1" * 64,
                "next_key": other.identity.to_dict(),
            },
            "digest",
        ),
        (
            {"rotation_sequence": 1, "previous_rotation_digest": "0" * 64, "next_key": []},
            "next key",
        ),
    )
    for index, (payload, message) in enumerate(cases):
        root = tmp_path / f"rotation-invalid-{index}"
        CheckpointRegistry(root, bootstrap_identity=signer.identity)
        write_rotation(root, payload)
        with pytest.raises(IntegrityError, match=message):
            CheckpointRegistry(root, bootstrap_identity=signer.identity)
    wrong_signer_root = tmp_path / "rotation-wrong-signer"
    CheckpointRegistry(wrong_signer_root, bootstrap_identity=signer.identity)
    wrong = sign_manifest(
        "key_rotation",
        {
            "rotation_sequence": 1,
            "previous_rotation_digest": "0" * 64,
            "next_key": other.identity.to_dict(),
        },
        signer=other,
        created_at=NOW,
    )
    (wrong_signer_root / "rotations" / "0000000000000001.json").write_bytes(
        canonical_bytes(wrong.to_dict())
    )
    with pytest.raises(IntegrityError, match="active key|trusted"):
        CheckpointRegistry(wrong_signer_root, bootstrap_identity=signer.identity)

    checkpoint_payload = verify_manifest(checkpoint.manifest, empty.trust_store)
    for index, (field, value, message) in enumerate(
        (
            ("checkpoint_sequence", 2, "sequence"),
            ("previous_checkpoint_digest", "1" * 64, "digest"),
        )
    ):
        root = tmp_path / f"checkpoint-invalid-{index}"
        CheckpointRegistry(root, bootstrap_identity=signer.identity)
        payload = dict(checkpoint_payload)
        payload[field] = value
        bad = sign_manifest("checkpoint", payload, signer=signer, created_at=NOW)
        (root / "checkpoints" / "0000000000000001.json").write_bytes(canonical_bytes(bad.to_dict()))
        with pytest.raises(IntegrityError, match=message):
            CheckpointRegistry(root, bootstrap_identity=signer.identity)

    schema_bad = tmp_path / "schema-bad.sqlite3"
    SQLiteEventStore(schema_bad)
    with sqlite3.connect(schema_bad) as connection:
        connection.execute("ALTER TABLE v3_events ADD COLUMN unexpected TEXT")
    with pytest.raises(IntegrityError, match="schema"):
        module._database_checkpoint_facts(schema_bad)

    # A stale registry instance must reject a sequence already won by different DB facts.
    conflict_root = tmp_path / "deterministic-checkpoint-conflict"
    winner = CheckpointRegistry(conflict_root, bootstrap_identity=signer.identity)
    stale_writer = CheckpointRegistry(conflict_root, bootstrap_identity=signer.identity)
    winner.create_checkpoint(database, signer=signer, created_at=NOW)
    alternate_database = tmp_path / "alternate-authority.sqlite3"
    SQLiteEventStore(alternate_database)
    with pytest.raises(IntegrityError, match="different material"):
        stale_writer.create_checkpoint(alternate_database, signer=signer, created_at=NOW)


def test_registry_record_and_checkpoint_payload_parser_rejections(tmp_path: Path) -> None:
    import strathmark.v3.infrastructure.integrity as module

    signer = P256EphemeralSigner.generate("integrity-key:parser")
    root = tmp_path / "records"
    root.mkdir()
    (root / "junk.json").write_text("{}", encoding="utf-8")
    with pytest.raises(IntegrityError, match="unexplained"):
        module._numbered_registry_paths(root, "checkpoint")
    (root / "junk.json").unlink()
    (root / "0000000000000001.json").write_text("[]", encoding="utf-8")
    with pytest.raises(IntegrityError, match="JSON object"):
        module._read_signed_registry_record(root / "0000000000000001.json", "checkpoint")
    (root / "0000000000000001.json").write_text("{", encoding="utf-8")
    with pytest.raises(IntegrityError, match="damaged"):
        module._read_signed_registry_record(root / "0000000000000001.json", "checkpoint")
    wrong = sign_manifest("key_rotation", {}, signer=signer, created_at=NOW)
    path = root / "0000000000000001.json"
    path.write_bytes(canonical_bytes(wrong.to_dict()))
    with pytest.raises(IntegrityError, match="wrong kind"):
        module._read_signed_registry_record(path, "checkpoint")
    correct = sign_manifest("checkpoint", {}, signer=signer, created_at=NOW)
    path.write_bytes(canonical_bytes(correct.to_dict()) + b" ")
    with pytest.raises(IntegrityError, match="canonical"):
        module._read_signed_registry_record(path, "checkpoint")

    base = {
        "checkpoint_sequence": 1,
        "previous_checkpoint_digest": "0" * 64,
        "authority_anchor": {"global_sequence": 0, "event_digest": "0" * 64},
        "schema_digest": "1" * 64,
        "projection_digest": "2" * 64,
        "aggregate_heads_digest": "3" * 64,
    }
    manifest = sign_manifest("checkpoint", base, signer=signer, created_at=NOW)
    mutations = (
        ({**base, "checkpoint_sequence": 0}, "sequence"),
        ({**base, "authority_anchor": []}, "anchor"),
        (
            {**base, "authority_anchor": {"global_sequence": -1, "event_digest": "0" * 64}},
            "authority sequence",
        ),
    )
    for payload, message in mutations:
        with pytest.raises(IntegrityError, match=message):
            module._trusted_checkpoint(manifest, payload)


def test_critical_recovery_validation_and_disagreement_matrix(tmp_path: Path) -> None:
    import strathmark.v3.infrastructure.integrity as module

    signer = P256EphemeralSigner.generate("integrity-key:critical-coverage")
    trust = IntegrityTrustStore((signer.identity,))
    journal = CriticalJournal(tmp_path / "journal", signer=signer, trust_store=trust)
    valid = _intent("command:coverage")

    invalid_intents = (
        (("field:two", 0), ("field:one", 0)),
        (),
        (("field:one", -1),),
    )
    for versions in invalid_intents:
        with pytest.raises(IntegrityError, match="expected versions"):
            CriticalIssueIntent(
                valid.command_id,
                valid.command_digest,
                valid.approval_snapshot_digest,
                versions,
                valid.receipt_ids,
                NOW,
            )
    with pytest.raises(IntegrityError, match="receipt identities"):
        CriticalIssueIntent(
            valid.command_id,
            valid.command_digest,
            valid.approval_snapshot_digest,
            valid.expected_versions,
            (),
            NOW,
        )
    with pytest.raises(IntegrityError, match="repeat a receipt"):
        CriticalIssueIntent(
            valid.command_id,
            valid.command_digest,
            valid.approval_snapshot_digest,
            valid.expected_versions,
            ("receipt:one", "receipt:one"),
            NOW,
        )
    with pytest.raises(IntegrityError, match="sequence"):
        CriticalDatabaseCommit(0, DIGEST, ("receipt:one",), DIGEST)
    with pytest.raises(IntegrityError, match="required"):
        CriticalDatabaseCommit(1, DIGEST, (), DIGEST)

    with pytest.raises(IntegrityError, match="lookup"):
        journal.reconcile(database_lookup=None, manager_receipts={})  # type: ignore[arg-type]
    (journal.root / "unexplained.json").write_text("{}", encoding="utf-8")
    with pytest.raises(IntegrityError, match="unexplained"):
        journal.reconcile(database_lookup=lambda _command: None, manager_receipts={})
    (journal.root / "unexplained.json").unlink()

    intent_manifest = journal.record_intent(valid)
    with pytest.raises(IntegrityError, match="manager claims"):
        journal.reconcile(
            database_lookup=lambda _command: None,
            manager_receipts={valid.command_id: valid.receipt_ids},
        )
    mismatch = CriticalDatabaseCommit(1, DIGEST, valid.receipt_ids, "f" * 64)
    with pytest.raises(IntegrityError, match="differs"):
        journal.reconcile(database_lookup=lambda _command: mismatch, manager_receipts={})
    commit = CriticalDatabaseCommit(1, DIGEST, valid.receipt_ids, intent_manifest.body_digest)
    marker = journal.record_commit(valid.command_id, commit, created_at=NOW)
    bad_marker = sign_manifest(
        "critical_commit",
        {
            "command_id": valid.command_id,
            **CriticalDatabaseCommit(
                2, DIGEST, valid.receipt_ids, intent_manifest.body_digest
            ).to_dict(),
        },
        signer=signer,
        created_at=NOW,
    )
    journal.marker_path(valid.command_id).write_bytes(canonical_bytes(bad_marker.to_dict()))
    with pytest.raises(IntegrityError, match="marker differs"):
        journal.reconcile(database_lookup=lambda _command: commit, manager_receipts={})
    journal.marker_path(valid.command_id).write_bytes(canonical_bytes(marker.to_dict()))
    with pytest.raises(IntegrityError, match="without a signed"):
        journal.reconcile(
            database_lookup=lambda _command: commit,
            manager_receipts={"command:other": ("receipt:other",)},
        )

    wrong_path = tmp_path / "wrong-kind.json"
    wrong_path.write_bytes(
        canonical_bytes(sign_manifest("checkpoint", {}, signer=signer, created_at=NOW).to_dict())
    )
    with pytest.raises(IntegrityError, match="wrong kind"):
        journal._read(wrong_path, "critical_intent")

    coordinator = CriticalIssueCoordinator.for_rehearsal(journal)
    with pytest.raises(IntegrityError, match="invalid commit"):
        coordinator.execute(valid, database_commit=lambda _digest: "bad")  # type: ignore[arg-type]
    mismatch_journal = CriticalJournal(tmp_path / "mismatch", signer=signer, trust_store=trust)
    with pytest.raises(IntegrityError, match="different issue"):
        CriticalIssueCoordinator.for_rehearsal(mismatch_journal).execute(
            valid,
            database_commit=lambda digest: CriticalDatabaseCommit(
                1, DIGEST, ("receipt:other",), digest
            ),
        )

    malformed = valid.to_dict()
    malformed["expected_versions"] = {}
    with pytest.raises(IntegrityError, match="arrays"):
        module._intent_from_payload(malformed)


def test_production_probe_and_durable_publication_failure_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ctypes

    import strathmark.v3.infrastructure.integrity as module

    signer = P256EphemeralSigner.generate("integrity-key:platform")
    production_identity = IntegrityKeyIdentity(
        "integrity-key:platform-cng",
        IntegrityKeyClass.PRODUCTION_CNG,
        "windows_cng_p256_sha256",
        signer.identity.public_key_der_b64,
    )
    non_cng = IntegrityKeyIdentity(
        "integrity-key:platform-noncng",
        IntegrityKeyClass.PRODUCTION_CNG,
        "other-provider",
        signer.identity.public_key_der_b64,
    )
    assert issue_recovery_readiness(
        StorageIdentity("device:a", "host:a", "site:a"),
        StorageIdentity("device:b", "host:a", "site:a"),
        non_cng,
    ).reasons == ("development_signing_key",)
    assert production_identity.to_dict()["provider"] == "windows_cng_p256_sha256"
    assert IntegrityTrustStore((production_identity,)).identities == (production_identity,)

    class FakeCall:
        def __init__(self, result: object) -> None:
            self.result = result
            self.argtypes = None
            self.restype = None

        def __call__(self, *args: object) -> object:
            return self.result

    class FakeKernel:
        def __init__(
            self, create_result: object, io_result: object, move_result: object = 0
        ) -> None:
            self.CreateFileW = FakeCall(create_result)
            self.DeviceIoControl = FakeCall(io_result)
            self.CloseHandle = FakeCall(1)
            self.MoveFileExW = FakeCall(move_result)

    invalid_handle = ctypes.wintypes.HANDLE(-1).value
    windows_root = PureWindowsPath("C:/")
    with monkeypatch.context() as context:
        context.setattr(module.os, "name", "nt")
        context.setattr(
            ctypes,
            "WinDLL",
            lambda *_args, **_kwargs: FakeKernel(invalid_handle, 0),
            raising=False,
        )
        context.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
        with pytest.raises(IntegrityError, match="could not open"):
            module._physical_device_id(windows_root)  # type: ignore[arg-type]
    with monkeypatch.context() as context:
        context.setattr(module.os, "name", "nt")
        context.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: FakeKernel(1, 0), raising=False)
        context.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
        with pytest.raises(IntegrityError, match="probe failed"):
            module._physical_device_id(windows_root)  # type: ignore[arg-type]
    with monkeypatch.context() as context:
        context.setattr(module.os, "name", "nt")
        with pytest.raises(IntegrityError, match="no probeable"):
            module._physical_device_id(Path("relative"))
    with monkeypatch.context() as context:
        context.setattr(module.os, "name", "posix")
        assert module._physical_device_id(tmp_path).startswith("posix-device:")

    source = tmp_path / "source"
    source.write_bytes(b"x")
    with monkeypatch.context() as context:
        context.setattr(
            ctypes,
            "WinDLL",
            lambda *_args, **_kwargs: FakeKernel(1, 1, 0),
            raising=False,
        )
        context.setattr(ctypes, "get_last_error", lambda: 80, raising=False)
        assert module._windows_write_through_no_clobber(source, tmp_path / "destination") is False
    with monkeypatch.context() as context:
        context.setattr(
            ctypes,
            "WinDLL",
            lambda *_args, **_kwargs: FakeKernel(1, 1, 0),
            raising=False,
        )
        context.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
        with pytest.raises(IntegrityError, match="rename failed"):
            module._windows_write_through_no_clobber(source, tmp_path / "destination")

    # Exercise POSIX no-clobber publication and its concurrent-winner branch under fakes.
    with monkeypatch.context() as context:
        context.setattr(module.os, "name", "posix")
        context.setattr(module.os, "open", lambda *_args, **_kwargs: 77)
        context.setattr(module.os, "fsync", lambda _descriptor: None)
        context.setattr(module.os, "close", lambda _descriptor: None)
        assert module._publish_no_clobber(tmp_path / "posix-winner", b"one") is True
        assert module._publish_no_clobber(tmp_path / "posix-winner", b"two") is False

    production_signer = _provider_cng_signer(monkeypatch, "strathmark-production")
    assert production_signer.sign(b"external-signature")
    journal = CriticalJournal(
        tmp_path / "production-journal",
        signer=production_signer,
        trust_store=IntegrityTrustStore((production_signer.identity,)),
    )
    topology = object.__new__(VerifiedRecoveryTopology)
    topology.primary = StorageIdentity("device:a", "host:a", "site:a")
    topology.recovery = StorageIdentity("device:b", "host:a", "site:a")
    topology.distinct_physical_devices = True
    with monkeypatch.context() as context:
        context.setattr(
            VerifiedRecoveryTopology, "probe", classmethod(lambda cls, *args, **kwargs: topology)
        )
        coordinator = CriticalIssueCoordinator.for_production(
            journal,
            authority_database_path=tmp_path,
            host_id="host:a",
            site_id="site:a",
        )
    assert coordinator.rehearsal is False


def test_event_store_recovery_boundary_rejection_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    database = tmp_path / "event-store-boundaries.sqlite3"
    store = SQLiteEventStore(database)
    prepare = _field_request(
        "command:boundary-prepare",
        CommandKind.OPTIMIZE_FIELD,
        EventKind.FIELD_OPTIMIZED,
        expected=0,
    )
    stored = store.execute(prepare)
    with pytest.raises(EventStoreError, match="projection_hook"):
        store.execute(prepare, projection_hook="bad")  # type: ignore[arg-type]
    with pytest.raises(EventStoreError, match="CheckpointRegistry"):
        SQLiteEventStore.from_checkpoint_registry(database, "bad")

    signer = P256EphemeralSigner.generate("integrity-key:event-boundary")
    journal = CriticalJournal(
        tmp_path / "event-journal",
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
    )
    coordinator = CriticalIssueCoordinator.for_rehearsal(journal)
    with pytest.raises(EventStoreError, match="only issue"):
        store.execute_critical_issue(
            prepare, intent=_intent(prepare.command.command_id.value), coordinator=coordinator
        )
    issue = _field_request(
        "command:boundary-issue", CommandKind.ACKNOWLEDGE_ISSUE, EventKind.FIELD_ISSUED, expected=1
    )
    valid_issue_intent = _intent(
        "command:boundary-issue",
        command_digest=canonical_digest(issue.command.to_dict()),
    )
    with pytest.raises(EventStoreError, match="intent command differs"):
        store.execute_critical_issue(
            issue, intent=_intent("command:different"), coordinator=coordinator
        )
    with pytest.raises(EventStoreError, match="coordinator"):
        store.execute_critical_issue(
            issue, intent=_intent("command:boundary-issue"), coordinator="bad"
        )  # type: ignore[arg-type]
    with monkeypatch.context() as context:
        context.setattr(coordinator, "execute", lambda *_args, **_kwargs: None)
        with pytest.raises(EventStoreIntegrityError, match="did not resolve"):
            store.execute_critical_issue(
                issue,
                intent=valid_issue_intent,
                coordinator=coordinator,
            )

    lookup = {
        "idempotency_key": str(prepare.command.command_id),
        "command_kind": prepare.command.kind,
        "target_aggregate": str(prepare.command.target_aggregate),
        "payload_digest": prepare.command.payload_digest,
    }
    with pytest.raises(EventStoreConflict, match="another principal"):
        store.lookup_exact_retry(principal_id="actor:other", **lookup)
    with pytest.raises(EventStoreConflict, match="different material"):
        store.lookup_exact_retry(
            principal_id="actor:judge",
            **{**lookup, "payload_digest": "0" * 64},
        )
    assert store.lookup_exact_retry(principal_id="actor:judge", **lookup) == stored

    with pytest.raises(EventStoreError, match="AuthorityAnchor"):
        verify_read_only_authority(database, trusted_anchor="bad")  # type: ignore[arg-type]
    stale = tmp_path / "stale-schema.sqlite3"
    SQLiteEventStore(stale)
    with sqlite3.connect(stale) as connection:
        connection.execute("ALTER TABLE v3_events ADD COLUMN unexpected TEXT")
    with pytest.raises(EventStoreIntegrityError, match="schema"):
        verify_read_only_authority(stale, trusted_anchor=AuthorityAnchor(0, "0" * 64))
    with pytest.raises(EventStoreIntegrityError, match="verification failed"):
        verify_read_only_authority(
            tmp_path / "missing.sqlite3", trusted_anchor=AuthorityAnchor(0, "0" * 64)
        )


def test_critical_issue_binding_extractors_reject_missing_material() -> None:
    from types import SimpleNamespace

    import strathmark.v3.infrastructure.sqlite.event_store as module

    with pytest.raises(EventStoreError, match="must inline"):
        module._critical_approval_snapshot_digest(
            SimpleNamespace(command=SimpleNamespace(payload=object()))
        )
    for value in ({}, {"approval_snapshot_digest": "A" * 64}):
        with pytest.raises(EventStoreError, match="canonical approval"):
            module._critical_approval_snapshot_digest(
                SimpleNamespace(command=SimpleNamespace(payload=InlinePayload.from_value(value)))
            )
    assert module._critical_receipt_ids(
        {
            "receipt_id": 3,
            "receipt_ids": ["receipt:two", 4],
            "nested": [{"receipt_id": "receipt:one"}],
            "tuple": {"receipt_ids": ("receipt:three",)},
        }
    ) == ("receipt:one", "receipt:three", "receipt:two")
    with pytest.raises(EventStoreError, match="no receipt"):
        module._critical_receipt_ids({"receipt_ids": [3], "nested": [None]})
