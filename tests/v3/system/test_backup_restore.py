from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import replace
from pathlib import Path

import pytest

from strathmark.v3.application.commands import CommandRequest, EventIntent
from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.commands import CommandEnvelope, CommandKind, InlinePayload
from strathmark.v3.contracts.events import AggregateKind, EventKind
from strathmark.v3.contracts.identifiers import IdempotencyKey, StableIdentifier
from strathmark.v3.infrastructure.backup import (
    ArchiveCopy,
    ArchiveEligibilityError,
    ArchiveManager,
    BackupError,
    BackupIntegrityError,
    BackupManager,
    DiskOperation,
    DiskReservePolicy,
    DiskTier,
    IssueExpectation,
    RemoteAttesterAuthorization,
    RemoteAttesterPolicy,
    attest_remote_archive_copy,
)
from strathmark.v3.infrastructure.blobs import (
    BlobIntegrityError,
    BlobMetadata,
    BlobRetention,
    ContentAddressedBlobStore,
)
from strathmark.v3.infrastructure.integrity import (
    CheckpointRegistry,
    CriticalDatabaseCommit,
    CriticalIssueIntent,
    CriticalJournal,
    IntegrityError,
    IntegrityKeyClass,
    IntegrityKeyIdentity,
    IntegrityTrustStore,
    P256EphemeralSigner,
    P256WindowsCNGSigner,
    SignedManifest,
    StorageIdentity,
    sign_manifest,
)
from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore
from strathmark.v3.infrastructure.sqlite.outbox import OutboxRepository

NOW = "2026-08-23T02:00:00.000Z"


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
        classmethod(lambda _cls, _provider_name, _observed_key_name: backend),
    )
    return P256WindowsCNGSigner.open(key_name)


def _blob_payload(label: bytes) -> bytes:
    return label + b"x" * (65_537 - len(label))


def _backup_manager(
    database: Path,
    blobs: ContentAddressedBlobStore,
    signer: P256EphemeralSigner,
    registry_root: Path,
) -> BackupManager:
    return BackupManager(
        database,
        blob_store=blobs,
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
        checkpoint_registry=CheckpointRegistry(registry_root, bootstrap_identity=signer.identity),
        critical_journal=CriticalJournal(
            registry_root.parent / f"{registry_root.name}-journal",
            signer=signer,
            trust_store=IntegrityTrustStore((signer.identity,)),
        ),
    )


def _record_issue_recovery(
    manager: BackupManager, issue: IssueExpectation, receipts: tuple[str, ...]
) -> None:
    intent = manager.critical_journal.record_intent(
        CriticalIssueIntent(
            issue.command_id,
            canonical_digest(
                _request(
                    issue.command_id,
                    CommandKind.ACKNOWLEDGE_ISSUE,
                    EventKind.FIELD_ISSUED,
                    expected=1,
                ).command.to_dict()
            ),
            "a" * 64,
            (("field:one", 1),),
            receipts,
            NOW,
        )
    )
    manager.critical_journal.record_commit(
        issue.command_id,
        CriticalDatabaseCommit(
            issue.last_global_sequence,
            issue.result_digest,
            receipts,
            intent.body_digest,
        ),
        created_at=NOW,
    )


def _request(
    command_id: str,
    kind: CommandKind,
    event: EventKind,
    *,
    expected: int,
) -> CommandRequest:
    field = StableIdentifier("field:one")
    command = CommandEnvelope(
        kind=kind,
        command_id=IdempotencyKey(command_id),
        target_aggregate=field,
        expected_versions=((str(field), expected),),
        actor_id=StableIdentifier("actor:judge"),
        payload=InlinePayload.from_value(
            {
                "field_id": str(field),
                "revision": expected + 1,
                "approval_snapshot_digest": "a" * 64,
            }
        ),
    )
    return CommandRequest(
        principal_id=StableIdentifier("actor:judge"),
        command=command,
        events=(EventIntent(AggregateKind.FIELD, field, event),),
        result_schema_version="strathmark-v3-test-result-v1",
        result={"receipt_id": "receipt:one", "revision": expected + 1},
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=expected + 1,
    )


def _tournament_request(
    command_id: str, kind: CommandKind, event: EventKind, *, expected: int
) -> CommandRequest:
    tournament = StableIdentifier("tournament:closed")
    command = CommandEnvelope(
        kind=kind,
        command_id=IdempotencyKey(command_id),
        target_aggregate=tournament,
        expected_versions=((str(tournament), expected),),
        actor_id=StableIdentifier("actor:judge"),
        payload=InlinePayload.from_value({"tournament_id": str(tournament), "step": expected + 1}),
    )
    return CommandRequest(
        principal_id=StableIdentifier("actor:judge"),
        command=command,
        events=(EventIntent(AggregateKind.TOURNAMENT, tournament, event),),
        result_schema_version="strathmark-v3-test-result-v1",
        result={"accepted": True},
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=expected + 1,
    )


def _tournament_request_for(
    tournament_id: str,
    command_id: str,
    kind: CommandKind,
    event: EventKind,
    *,
    expected: int,
) -> CommandRequest:
    tournament = StableIdentifier(tournament_id)
    command = CommandEnvelope(
        kind=kind,
        command_id=IdempotencyKey(command_id),
        target_aggregate=tournament,
        expected_versions=((str(tournament), expected),),
        actor_id=StableIdentifier("actor:judge"),
        payload=InlinePayload.from_value({"tournament_id": str(tournament), "step": expected + 1}),
    )
    return CommandRequest(
        principal_id=StableIdentifier("actor:judge"),
        command=command,
        events=(EventIntent(AggregateKind.TOURNAMENT, tournament, event),),
        result_schema_version="strathmark-v3-test-result-v1",
        result={"accepted": True},
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=expected + 1,
    )


def test_online_backup_restore_verifies_chain_projection_bundle_blob_and_issue_lookup(
    tmp_path: Path,
) -> None:
    database = tmp_path / "authority.sqlite3"
    store = SQLiteEventStore(database)
    store.execute(
        _request(
            "command:prepare", CommandKind.OPTIMIZE_FIELD, EventKind.FIELD_OPTIMIZED, expected=0
        )
    )
    issued = store.execute(
        _request("command:issue", CommandKind.ACKNOWLEDGE_ISSUE, EventKind.FIELD_ISSUED, expected=1)
    )
    blobs = ContentAddressedBlobStore(tmp_path / "blobs")
    reference = blobs.publish(
        _blob_payload(b"required raw output"),
        metadata=BlobMetadata(
            "application/octet-stream", "strathmark-v3-raw-output-v1", BlobRetention.REQUIRED
        ),
    )
    signer = P256EphemeralSigner.generate("integrity-key:backup")
    manager = _backup_manager(database, blobs, signer, tmp_path / "checkpoints")
    bundle_digest = "b" * 64
    issue = IssueExpectation("command:issue", issued.result_digest, issued.last_global_sequence)
    _record_issue_recovery(manager, issue, ("receipt:one",))
    backup = manager.create_online_backup(
        tmp_path / "backup" / "authority.sqlite3",
        required_blobs=(reference,),
        pinned_bundle_digest=bundle_digest,
        issues=(issue,),
        created_at=NOW,
    )
    # The backup artifact, not the live primary blob root, owns recovery bytes.
    blobs.path_for(reference.digest).unlink()
    restored = tmp_path / "restored" / "authority.sqlite3"
    report = manager.restore_verified(
        backup,
        restored,
        installed_bundle_digest=bundle_digest,
        manager_receipts={"command:issue": ("receipt:one",)},
    )
    assert report.ready is True
    assert SQLiteEventStore(restored).event_count() == 2


def test_backup_cannot_omit_any_acknowledged_issue(tmp_path: Path) -> None:
    database = tmp_path / "authority.sqlite3"
    store = SQLiteEventStore(database)
    store.execute(
        _request(
            "command:prepare", CommandKind.OPTIMIZE_FIELD, EventKind.FIELD_OPTIMIZED, expected=0
        )
    )
    store.execute(
        _request(
            "command:issue",
            CommandKind.ACKNOWLEDGE_ISSUE,
            EventKind.FIELD_ISSUED,
            expected=1,
        )
    )
    blobs = ContentAddressedBlobStore(tmp_path / "blobs")
    signer = P256EphemeralSigner.generate("integrity-key:backup")
    manager = _backup_manager(database, blobs, signer, tmp_path / "checkpoints")
    with pytest.raises(BackupIntegrityError, match="complete acknowledged issue"):
        manager.create_online_backup(
            tmp_path / "backup.sqlite3",
            required_blobs=(),
            pinned_bundle_digest="b" * 64,
            issues=(),
            created_at=NOW,
        )


def test_backup_derives_undelivered_outbox_blob_and_restores_after_primary_loss(
    tmp_path: Path,
) -> None:
    database = tmp_path / "authority.sqlite3"
    SQLiteEventStore(database)
    blobs = ContentAddressedBlobStore(tmp_path / "blobs")
    reference = blobs.publish(
        _blob_payload(b"outbox-only"),
        metadata=BlobMetadata(
            "application/octet-stream",
            "strathmark-v3-outbox-evidence-v1",
            BlobRetention.UNDELIVERED_OUTBOX,
        ),
    )
    signer = P256EphemeralSigner.generate("integrity-key:outbox-backup")
    OutboxRepository(
        database,
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
        active_key_id=signer.identity.key_id,
    ).enqueue(
        outbox_id="outbox:blob-only",
        destination="manager",
        payload={"blob": reference.to_dict()},
        created_at=NOW,
    )
    manager = _backup_manager(database, blobs, signer, tmp_path / "checkpoints")
    backup = manager.create_online_backup(
        tmp_path / "backup",
        required_blobs=(),
        pinned_bundle_digest="b" * 64,
        issues=(),
        created_at=NOW,
    )
    blobs.path_for(reference.digest).unlink()
    report = manager.restore_verified(
        backup,
        tmp_path / "restored.sqlite3",
        installed_bundle_digest="b" * 64,
        manager_receipts={},
    )
    assert report.ready
    assert blobs.read(reference) == _blob_payload(b"outbox-only")


def test_restore_never_replaces_existing_destination_or_mutates_missing_artifact_root(
    tmp_path: Path,
) -> None:
    database = tmp_path / "authority.sqlite3"
    SQLiteEventStore(database)
    signer = P256EphemeralSigner.generate("integrity-key:no-replace")
    manager = _backup_manager(
        database,
        ContentAddressedBlobStore(tmp_path / "blobs"),
        signer,
        tmp_path / "checkpoints",
    )
    backup = manager.create_online_backup(
        tmp_path / "backup",
        required_blobs=(),
        pinned_bundle_digest="b" * 64,
        issues=(),
        created_at=NOW,
    )
    destination = tmp_path / "existing.sqlite3"
    destination.write_bytes(b"existing authority bytes")
    before = destination.read_bytes()
    with pytest.raises(BackupError, match="new and empty"):
        manager.restore_verified(
            backup,
            destination,
            installed_bundle_digest="b" * 64,
            manager_receipts={},
        )
    assert destination.read_bytes() == before
    manifest_before = backup.manifest_path.read_bytes()
    backup.manifest_path.write_bytes(b"{}")
    with pytest.raises(BackupError, match="new and empty"):
        manager.restore_verified(
            backup,
            destination,
            installed_bundle_digest="b" * 64,
            manager_receipts={},
        )
    assert destination.read_bytes() == before
    backup.manifest_path.write_bytes(manifest_before)

    held_blob_root = backup.blob_root.with_name("held-blobs")
    backup.blob_root.rename(held_blob_root)
    missing_destination = tmp_path / "missing-root-restore.sqlite3"
    with pytest.raises(BackupIntegrityError):
        manager.restore_verified(
            backup,
            missing_destination,
            installed_bundle_digest="b" * 64,
            manager_receipts={},
        )
    assert not backup.blob_root.exists()
    assert not missing_destination.exists()


@pytest.mark.parametrize("stage", ["after_database", "after_blobs", "after_manifest"])
def test_failed_backup_generation_never_replaces_prior_verified_generation(
    tmp_path: Path, stage: str
) -> None:
    database = tmp_path / "authority.sqlite3"
    SQLiteEventStore(database)
    blobs = ContentAddressedBlobStore(tmp_path / "blobs")
    signer = P256EphemeralSigner.generate("integrity-key:backup")
    manager = _backup_manager(database, blobs, signer, tmp_path / "checkpoints")
    prior = manager.create_online_backup(
        tmp_path / "prior",
        required_blobs=(),
        pinned_bundle_digest="b" * 64,
        issues=(),
        created_at=NOW,
    )

    def fail(observed: str) -> None:
        if observed == stage:
            raise RuntimeError(stage)

    with pytest.raises(RuntimeError, match=stage):
        manager.create_online_backup(
            tmp_path / "next",
            required_blobs=(),
            pinned_bundle_digest="b" * 64,
            issues=(),
            created_at=NOW,
            fault_hook=fail,
        )
    assert manager.restore_verified(
        prior,
        tmp_path / f"restored-{stage}.sqlite3",
        installed_bundle_digest="b" * 64,
        manager_receipts={},
    ).ready


@pytest.mark.parametrize("failure", ["database", "checkpoint", "blob", "bundle"])
def test_restore_corruption_or_stale_dependency_truthfully_fails_closed(
    tmp_path: Path, failure: str
) -> None:
    database = tmp_path / "authority.sqlite3"
    store = SQLiteEventStore(database)
    store.execute(
        _tournament_request(
            "command:configure-tournament",
            CommandKind.CONFIGURE_TOURNAMENT,
            EventKind.TOURNAMENT_CONFIGURED,
            expected=0,
        )
    )
    store.execute(
        _tournament_request(
            "command:open-tournament",
            CommandKind.OPEN_TOURNAMENT,
            EventKind.TOURNAMENT_OPENED,
            expected=1,
        )
    )
    store.execute(
        _tournament_request(
            "command:close-tournament",
            CommandKind.CLOSE_TOURNAMENT,
            EventKind.TOURNAMENT_CLOSED,
            expected=2,
        )
    )
    blobs = ContentAddressedBlobStore(tmp_path / "blobs")
    reference = blobs.publish(
        _blob_payload(b"required"),
        metadata=BlobMetadata(
            "application/octet-stream", "strathmark-v3-raw-v1", BlobRetention.REQUIRED
        ),
    )
    signer = P256EphemeralSigner.generate("integrity-key:backup")
    manager = _backup_manager(database, blobs, signer, tmp_path / "checkpoints")
    backup = manager.create_online_backup(
        tmp_path / "backup.sqlite3",
        required_blobs=(reference,),
        pinned_bundle_digest="b" * 64,
        issues=(),
        created_at=NOW,
    )
    if failure == "database":
        backup.database_path.write_bytes(b"corrupt")
    elif failure == "checkpoint":
        backup.manifest_path.write_text("{}", encoding="utf-8")
    elif failure == "blob":
        ContentAddressedBlobStore(backup.blob_root).path_for(reference.digest).unlink()
    with pytest.raises(BackupIntegrityError):
        manager.restore_verified(
            backup,
            tmp_path / "restored.sqlite3",
            installed_bundle_digest=("c" * 64 if failure == "bundle" else "b" * 64),
            manager_receipts={},
        )


def test_restore_reconciles_marker_missing_and_rejects_stale_manager_or_intent_only(
    tmp_path: Path,
) -> None:
    database = tmp_path / "authority.sqlite3"
    store = SQLiteEventStore(database)
    store.execute(
        _request(
            "command:prepare", CommandKind.OPTIMIZE_FIELD, EventKind.FIELD_OPTIMIZED, expected=0
        )
    )
    issued = store.execute(
        _request("command:issue", CommandKind.ACKNOWLEDGE_ISSUE, EventKind.FIELD_ISSUED, expected=1)
    )
    signer = P256EphemeralSigner.generate("integrity-key:recovery-restore")
    manager = _backup_manager(
        database,
        ContentAddressedBlobStore(tmp_path / "blobs"),
        signer,
        tmp_path / "checkpoints",
    )
    issue = IssueExpectation("command:issue", issued.result_digest, issued.last_global_sequence)
    intent = manager.critical_journal.record_intent(
        CriticalIssueIntent(
            issue.command_id,
            canonical_digest(
                _request(
                    issue.command_id,
                    CommandKind.ACKNOWLEDGE_ISSUE,
                    EventKind.FIELD_ISSUED,
                    expected=1,
                ).command.to_dict()
            ),
            "a" * 64,
            (("field:one", 1),),
            ("receipt:one",),
            NOW,
        )
    )
    backup = manager.create_online_backup(
        tmp_path / "backup",
        required_blobs=(),
        pinned_bundle_digest="b" * 64,
        issues=(issue,),
        created_at=NOW,
    )
    restored = tmp_path / "restored.sqlite3"
    report = manager.restore_verified(
        backup,
        restored,
        installed_bundle_digest="b" * 64,
        manager_receipts={"command:issue": ("receipt:one",)},
    )
    assert report.ready
    assert manager.critical_journal.marker_path("command:issue").is_file()
    assert intent.body_digest

    stale_destination = tmp_path / "stale-manager.sqlite3"
    with pytest.raises(BackupIntegrityError, match="critical"):
        manager.restore_verified(
            backup,
            stale_destination,
            installed_bundle_digest="b" * 64,
            manager_receipts={"command:issue": ("receipt:other",)},
        )
    assert not stale_destination.exists()

    intent_database = tmp_path / "intent-only.sqlite3"
    SQLiteEventStore(intent_database)
    intent_manager = _backup_manager(
        intent_database,
        ContentAddressedBlobStore(tmp_path / "intent-blobs"),
        P256EphemeralSigner.generate("integrity-key:intent-restore"),
        tmp_path / "intent-checkpoints",
    )
    intent_manager.critical_journal.record_intent(
        CriticalIssueIntent(
            "command:pending-issue",
            "d" * 64,
            "c" * 64,
            (("field:pending", 0),),
            ("receipt:pending",),
            NOW,
        )
    )
    intent_backup = intent_manager.create_online_backup(
        tmp_path / "intent-backup",
        required_blobs=(),
        pinned_bundle_digest="b" * 64,
        issues=(),
        created_at=NOW,
    )
    intent_destination = tmp_path / "intent-restored.sqlite3"
    with pytest.raises(BackupIntegrityError, match="unresolved"):
        intent_manager.restore_verified(
            intent_backup,
            intent_destination,
            installed_bundle_digest="b" * 64,
            manager_receipts={},
        )
    assert not intent_destination.exists()


def test_restore_rejects_backup_older_than_acknowledged_issue_before_publication(
    tmp_path: Path,
) -> None:
    database = tmp_path / "authority.sqlite3"
    store = SQLiteEventStore(database)
    signer = P256EphemeralSigner.generate("integrity-key:later-issue")
    manager = _backup_manager(
        database,
        ContentAddressedBlobStore(tmp_path / "blobs"),
        signer,
        tmp_path / "checkpoints",
    )
    backup = manager.create_online_backup(
        tmp_path / "backup",
        required_blobs=(),
        pinned_bundle_digest="b" * 64,
        issues=(),
        created_at=NOW,
    )
    store.execute(
        _request(
            "command:prepare", CommandKind.OPTIMIZE_FIELD, EventKind.FIELD_OPTIMIZED, expected=0
        )
    )
    issued = store.execute(
        _request("command:issue", CommandKind.ACKNOWLEDGE_ISSUE, EventKind.FIELD_ISSUED, expected=1)
    )
    _record_issue_recovery(
        manager,
        IssueExpectation("command:issue", issued.result_digest, issued.last_global_sequence),
        ("receipt:one",),
    )
    destination = tmp_path / "restored.sqlite3"
    with pytest.raises(BackupIntegrityError, match="critical"):
        manager.restore_verified(
            backup,
            destination,
            installed_bundle_digest="b" * 64,
            manager_receipts={"command:issue": ("receipt:one",)},
        )
    assert not destination.exists()


def test_archive_needs_two_distinct_verified_copies_one_off_host_and_closed_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "authority.sqlite3"
    store = SQLiteEventStore(database)
    blobs = ContentAddressedBlobStore(tmp_path / "blobs")
    archive_reference = blobs.publish(
        _blob_payload(b"archive authority payload"),
        metadata=BlobMetadata(
            "application/octet-stream",
            "strathmark-v3-archive-evidence-v1",
            BlobRetention.ARCHIVABLE,
        ),
    )
    store.execute(
        _tournament_request(
            "command:configure-tournament",
            CommandKind.CONFIGURE_TOURNAMENT,
            EventKind.TOURNAMENT_CONFIGURED,
            expected=0,
        )
    )
    open_request = _tournament_request(
        "command:open-tournament",
        CommandKind.OPEN_TOURNAMENT,
        EventKind.TOURNAMENT_OPENED,
        expected=1,
    )
    store.execute(
        replace(open_request, command=replace(open_request.command, payload=archive_reference))
    )
    store.execute(
        _tournament_request(
            "command:close-tournament",
            CommandKind.CLOSE_TOURNAMENT,
            EventKind.TOURNAMENT_CLOSED,
            expected=2,
        )
    )
    package = tmp_path / "package.bin"
    copy_one = tmp_path / "copy-one.bin"
    copy_two = tmp_path / "copy-two.bin"
    signer = _provider_cng_signer(monkeypatch, "strathmark-archive")
    trust = IntegrityTrustStore((signer.identity,))
    remote_signer = _provider_cng_signer(monkeypatch, "strathmark-remote-archive")
    remote_key_identity = remote_signer.identity
    remote_identity = StorageIdentity("device:remote", "host:archive", "site:other")
    remote_policy = RemoteAttesterPolicy(
        local_trust_store=trust,
        authorizations=(RemoteAttesterAuthorization(remote_key_identity, remote_identity),),
    )
    checkpoint_registry = CheckpointRegistry(
        tmp_path / "archive-checkpoints", bootstrap_identity=signer.identity
    )
    checkpoint_registry.create_checkpoint(database, signer=signer, created_at=NOW)
    archive = ArchiveManager(
        database,
        signer=signer,
        trust_store=trust,
        remote_attester_policy=remote_policy,
        blob_store=blobs,
        checkpoint_registry=checkpoint_registry,
    )
    archive.build_archive_package("tournament:closed", package)
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    copy_one.write_bytes(package.read_bytes())
    copy_two.write_bytes(package.read_bytes())
    remote_attestation = attest_remote_archive_copy(
        package_digest=digest,
        storage_identity=remote_identity,
        signer=remote_signer,
        verified_at=NOW,
    )
    copies = (
        ArchiveCopy.local(copy_one, site_id="site:venue"),
        ArchiveCopy.remote_attested(remote_attestation, remote_policy),
    )
    malformed = tmp_path / "malformed-package.bin"
    malformed.write_bytes(b"")
    with pytest.raises(ArchiveEligibilityError, match="package"):
        archive.prove_two_copy_archive(
            tournament_id="tournament:closed",
            package_path=malformed,
            copies=copies,
            verified_at=NOW,
        )
    blob_path = blobs.path_for(archive_reference.digest)
    held_blob = blob_path.with_suffix(".held")
    blob_path.rename(held_blob)
    with pytest.raises(ArchiveEligibilityError, match="blob|missing"):
        archive.prove_two_copy_archive(
            tournament_id="tournament:closed",
            package_path=package,
            copies=copies,
            verified_at=NOW,
        )
    held_blob.rename(blob_path)
    proof = archive.prove_two_copy_archive(
        tournament_id="tournament:closed",
        package_path=package,
        copies=copies,
        verified_at=NOW,
    )
    assert proof.primary_removal_eligible is True
    assert proof.required_blobs == (archive_reference,)
    assert archive.lookup("tournament:closed") == proof

    with pytest.raises(ArchiveEligibilityError):
        archive.prove_two_copy_archive(
            tournament_id="tournament:open",
            package_path=package,
            copies=(copies[0], copies[0]),
            verified_at=NOW,
        )

    with pytest.raises(Exception, match="overlap|local"):
        alias_identity = IntegrityKeyIdentity(
            "integrity-key:cng-archive-alias",
            IntegrityKeyClass.PRODUCTION_CNG,
            "windows_cng_p256_sha256",
            signer.identity.public_key_der_b64,
        )
        RemoteAttesterPolicy(
            local_trust_store=trust,
            authorizations=(
                RemoteAttesterAuthorization(
                    alias_identity,
                    StorageIdentity("device:fake", "host:fake", "site:fake"),
                ),
            ),
        )
    with pytest.raises(TypeError):
        ArchiveCopy.local(  # type: ignore[call-arg]
            copy_one, host_id="host:fake", site_id="site:venue"
        )
    wrong_scope = attest_remote_archive_copy(
        package_digest=digest,
        storage_identity=StorageIdentity("device:other", "host:archive", "site:other"),
        signer=remote_signer,
        verified_at=NOW,
    )
    with pytest.raises(ArchiveEligibilityError, match="scope"):
        ArchiveCopy.remote_attested(wrong_scope, remote_policy)


def test_archive_prefix_rejects_interleaved_tournament_that_remains_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "authority.sqlite3"
    store = SQLiteEventStore(database)
    store.execute(
        _tournament_request_for(
            "tournament:one",
            "command:configure-one",
            CommandKind.CONFIGURE_TOURNAMENT,
            EventKind.TOURNAMENT_CONFIGURED,
            expected=0,
        )
    )
    store.execute(
        _tournament_request_for(
            "tournament:two",
            "command:configure-two",
            CommandKind.CONFIGURE_TOURNAMENT,
            EventKind.TOURNAMENT_CONFIGURED,
            expected=0,
        )
    )
    store.execute(
        _tournament_request_for(
            "tournament:one",
            "command:open-one",
            CommandKind.OPEN_TOURNAMENT,
            EventKind.TOURNAMENT_OPENED,
            expected=1,
        )
    )
    store.execute(
        _tournament_request_for(
            "tournament:one",
            "command:close-one",
            CommandKind.CLOSE_TOURNAMENT,
            EventKind.TOURNAMENT_CLOSED,
            expected=2,
        )
    )
    signer = _provider_cng_signer(monkeypatch, "strathmark-interleaved")
    identity = signer.identity
    trust = IntegrityTrustStore((identity,))
    remote_dev = P256EphemeralSigner.generate("integrity-key:interleaved-remote-dev")
    remote_identity = IntegrityKeyIdentity(
        "integrity-key:cng-interleaved-remote",
        IntegrityKeyClass.PRODUCTION_CNG,
        "windows_cng_p256_sha256",
        remote_dev.identity.public_key_der_b64,
    )
    policy = RemoteAttesterPolicy(
        local_trust_store=trust,
        authorizations=(
            RemoteAttesterAuthorization(
                remote_identity,
                StorageIdentity("device:remote", "host:remote", "site:remote"),
            ),
        ),
    )
    registry = CheckpointRegistry(tmp_path / "checkpoints", bootstrap_identity=identity)
    registry.create_checkpoint(database, signer=signer, created_at=NOW)
    archive = ArchiveManager(
        database,
        signer=signer,
        trust_store=trust,
        remote_attester_policy=policy,
        blob_store=ContentAddressedBlobStore(tmp_path / "blobs"),
        checkpoint_registry=registry,
    )
    with pytest.raises(ArchiveEligibilityError, match="still open"):
        archive.build_archive_package("tournament:one", tmp_path / "unsafe.package")
    assert not (tmp_path / "unsafe.package").exists()
    store.execute(
        _tournament_request_for(
            "tournament:two",
            "command:open-two",
            CommandKind.OPEN_TOURNAMENT,
            EventKind.TOURNAMENT_OPENED,
            expected=1,
        )
    )
    store.execute(
        _tournament_request_for(
            "tournament:two",
            "command:close-two",
            CommandKind.CLOSE_TOURNAMENT,
            EventKind.TOURNAMENT_CLOSED,
            expected=2,
        )
    )
    registry.create_checkpoint(database, signer=signer, created_at=NOW)
    with pytest.raises(ArchiveEligibilityError, match="checkpoint tip"):
        archive.build_archive_package("tournament:one", tmp_path / "older.package")
    assert not (tmp_path / "older.package").exists()


def test_disk_reserve_degrades_maintenance_before_preserving_open_critical_lane() -> None:
    policy = DiskReservePolicy(
        warning_free_bytes=1_000,
        speculative_free_bytes=750,
        critical_free_bytes=500,
    )
    assert policy.admit(2_000, DiskOperation.FACTORY, tournament_open=False).allowed is True
    assert policy.admit(900, DiskOperation.FACTORY, tournament_open=False).allowed is False
    assert policy.admit(900, DiskOperation.SPECULATIVE_LLM, tournament_open=False).allowed is True
    assert policy.admit(700, DiskOperation.SPECULATIVE_LLM, tournament_open=True).allowed is False
    assert (
        policy.admit(400, DiskOperation.PREPARE_TOURNAMENT, tournament_open=False).allowed is False
    )
    for operation in (
        DiskOperation.RESULT,
        DiskOperation.ISSUE,
        DiskOperation.RECOVERY,
        DiskOperation.RECEIPT_LOOKUP,
        DiskOperation.SUPPORT_EXPORT,
    ):
        assert policy.admit(400, operation, tournament_open=True).allowed is True


def test_backup_public_contract_and_disk_policy_rejection_matrix(tmp_path: Path) -> None:
    import strathmark.v3.infrastructure.backup as module

    with pytest.raises(BackupError, match="sequence"):
        IssueExpectation("command:bad", "a" * 64, 0)
    issue = IssueExpectation("command:ok", "a" * 64, 1)
    with pytest.raises(BackupIntegrityError, match="malformed"):
        IssueExpectation.from_dict({"command_id": "command:ok"})
    assert IssueExpectation.from_dict(issue.to_dict()) == issue

    database = tmp_path / "authority.sqlite3"
    SQLiteEventStore(database)
    signer = P256EphemeralSigner.generate("integrity-key:backup-validation")
    blobs = ContentAddressedBlobStore(tmp_path / "blobs")
    registry = CheckpointRegistry(tmp_path / "registry", bootstrap_identity=signer.identity)
    journal = CriticalJournal(
        tmp_path / "journal",
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
    )
    with pytest.raises(BackupError, match="checkpoint"):
        BackupManager(
            database,
            blob_store=blobs,
            signer=signer,
            trust_store=IntegrityTrustStore((signer.identity,)),
            checkpoint_registry="bad",  # type: ignore[arg-type]
            critical_journal=journal,
        )
    with pytest.raises(BackupError, match="journal"):
        BackupManager(
            database,
            blob_store=blobs,
            signer=signer,
            trust_store=IntegrityTrustStore((signer.identity,)),
            checkpoint_registry=registry,
            critical_journal="bad",  # type: ignore[arg-type]
        )
    manager = BackupManager(
        database,
        blob_store=blobs,
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
        checkpoint_registry=registry,
        critical_journal=journal,
    )
    for kwargs, message in (
        ({"destination": database}, "differ"),
        ({"destination": tmp_path / "bad-tuples", "required_blobs": []}, "tuples"),
        ({"destination": tmp_path / "bad-hook", "fault_hook": "bad"}, "callable"),
    ):
        parameters = {
            "destination": tmp_path / "backup",
            "required_blobs": (),
            "pinned_bundle_digest": "b" * 64,
            "issues": (),
            "created_at": NOW,
        }
        parameters.update(kwargs)
        with pytest.raises(BackupError, match=message):
            manager.create_online_backup(**parameters)  # type: ignore[arg-type]
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(BackupError, match="already exists"):
        manager.create_online_backup(
            existing,
            required_blobs=(),
            pinned_bundle_digest="b" * 64,
            issues=(),
            created_at=NOW,
        )

    for values in ((1, 1, 1), (True, 2, 1), (3, 2, 0)):
        with pytest.raises(BackupError, match="thresholds"):
            DiskReservePolicy(*values)
    policy = DiskReservePolicy(1_000, 750, 500)
    with pytest.raises(BackupError, match="bytes"):
        policy.admit(-1, DiskOperation.RESULT, tournament_open=True)
    with pytest.raises(BackupError, match="typed"):
        policy.admit(1_000, "result", tournament_open=True)  # type: ignore[arg-type]
    assert (
        policy.admit(900, DiskOperation.BACKFILL, tournament_open=False).reason
        == "maintenance_suspended"
    )
    assert (
        policy.admit(700, DiskOperation.RESULT, tournament_open=True).tier
        is DiskTier.SPECULATIVE_SUSPENDED
    )
    assert policy.admit(400, DiskOperation.RESULT, tournament_open=False).allowed is False
    with pytest.raises(BackupError, match="token"):
        module._require_token("BAD TOKEN", "token")
    with pytest.raises(BackupError, match="digest"):
        module._require_digest("A" * 64, "digest")


def test_archive_copy_remote_policy_and_attestation_rejection_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import strathmark.v3.infrastructure.backup as module

    local_dev = P256EphemeralSigner.generate("integrity-key:local-policy")
    local_identity = IntegrityKeyIdentity(
        "integrity-key:local-policy-cng",
        IntegrityKeyClass.PRODUCTION_CNG,
        "windows_cng_p256_sha256",
        local_dev.identity.public_key_der_b64,
    )
    local_trust = IntegrityTrustStore((local_identity,))
    remote_signer = _provider_cng_signer(monkeypatch, "strathmark-remote-policy")
    remote_identity = remote_signer.identity
    storage = StorageIdentity("device:remote", "host:remote", "site:remote")
    authorization = RemoteAttesterAuthorization(remote_identity, storage)
    monkeypatch.setattr(module, "_os_host_id", lambda: "host:local")

    with pytest.raises(ArchiveEligibilityError, match="verified probe"):
        ArchiveCopy(storage)
    with pytest.raises(ArchiveEligibilityError, match="typed public"):
        RemoteAttesterAuthorization("bad", storage)  # type: ignore[arg-type]
    with pytest.raises(ArchiveEligibilityError, match="typed storage"):
        RemoteAttesterAuthorization(remote_identity, "bad")  # type: ignore[arg-type]
    with pytest.raises(ArchiveEligibilityError, match="production CNG"):
        RemoteAttesterAuthorization(
            P256EphemeralSigner.generate("integrity-key:remote-policy-dev").identity, storage
        )
    with pytest.raises(ArchiveEligibilityError, match="requires authorizations"):
        RemoteAttesterPolicy(local_trust_store=local_trust, authorizations=())
    with pytest.raises(ArchiveEligibilityError, match="malformed"):
        RemoteAttesterPolicy(local_trust_store=local_trust, authorizations=("bad",))  # type: ignore[arg-type]
    with pytest.raises(ArchiveEligibilityError, match="local host"):
        RemoteAttesterPolicy(
            local_trust_store=local_trust,
            authorizations=(
                RemoteAttesterAuthorization(
                    remote_identity, StorageIdentity("device:remote", "host:local", "site:remote")
                ),
            ),
        )
    alias = IntegrityKeyIdentity(
        "integrity-key:remote-policy-alias",
        IntegrityKeyClass.PRODUCTION_CNG,
        "windows_cng_p256_sha256",
        remote_identity.public_key_der_b64,
    )
    with pytest.raises(ArchiveEligibilityError, match="public key"):
        RemoteAttesterPolicy(
            local_trust_store=local_trust,
            authorizations=(
                authorization,
                RemoteAttesterAuthorization(
                    alias, StorageIdentity("device:two", "host:two", "site:two")
                ),
            ),
        )
    with pytest.raises(ArchiveEligibilityError, match="more than once"):
        RemoteAttesterPolicy(
            local_trust_store=local_trust,
            authorizations=(authorization, authorization),
        )
    policy = RemoteAttesterPolicy(local_trust_store=local_trust, authorizations=(authorization,))
    wrong_kind = sign_manifest("checkpoint", {}, signer=remote_signer, created_at=NOW)
    with pytest.raises(ArchiveEligibilityError, match="wrong kind"):
        ArchiveCopy.remote_attested(wrong_kind, policy)
    with pytest.raises(ArchiveEligibilityError, match="wrong kind"):
        policy.verify("bad")  # type: ignore[arg-type]
    unauthorized = P256EphemeralSigner.generate("integrity-key:unauthorized")
    with pytest.raises(IntegrityError, match="OS-attested"):
        attest_remote_archive_copy(
            package_digest="a" * 64,
            storage_identity=storage,
            signer=unauthorized,
            verified_at=NOW,
        )
    unauthorized_attestation = sign_manifest(
        "archive_copy_attestation",
        {
            "attester_role": "archive_replica_attester",
            "package_digest": "a" * 64,
            "storage_identity": storage.to_dict(),
        },
        signer=unauthorized,
        created_at=NOW,
    )
    with pytest.raises(ArchiveEligibilityError, match="not authorized"):
        policy.verify(unauthorized_attestation)
    wrong_role = sign_manifest(
        "archive_copy_attestation",
        {
            "attester_role": "other",
            "package_digest": "a" * 64,
            "storage_identity": storage.to_dict(),
        },
        signer=remote_signer,
        created_at=NOW,
    )
    with pytest.raises(ArchiveEligibilityError, match="role"):
        policy.verify(wrong_role)
    malformed_identity = sign_manifest(
        "archive_copy_attestation",
        {
            "attester_role": "archive_replica_attester",
            "package_digest": "a" * 64,
            "storage_identity": [],
        },
        signer=remote_signer,
        created_at=NOW,
    )
    with pytest.raises(ArchiveEligibilityError, match="identity"):
        policy.verify(malformed_identity)


def test_archive_package_codec_rejects_every_damaged_frame(tmp_path: Path) -> None:
    import strathmark.v3.infrastructure.backup as module

    manifest = {
        "schema_version": "test",
        "entries": [{"name": "a.bin", "byte_count": 1, "digest": hashlib.sha256(b"x").hexdigest()}],
    }
    encoded = module._encode_archive_package(manifest, (("a.bin", b"x"),))
    assert module._decode_archive_package(encoded)[1] == {"a.bin": b"x"}
    for entries, message in (
        ((("unsafe name", b"x"),), "safe and sorted"),
        ((("a.bin", "x"),), "immutable bytes"),
        ((("b.bin", b"x"), ("a.bin", b"x")), "safe and sorted"),
    ):
        with pytest.raises(ArchiveEligibilityError, match=message):
            module._encode_archive_package({"entries": []}, entries)  # type: ignore[arg-type]
    with pytest.raises(BackupIntegrityError, match="magic"):
        module._decode_archive_package("bad")  # type: ignore[arg-type]
    magic = module._ARCHIVE_MAGIC
    damaged = (
        (magic, "header"),
        (magic + struct.pack(">Q", 0), "count"),
        (magic + struct.pack(">Q", 1), "entry header"),
        (magic + struct.pack(">Q", 1) + struct.pack(">IQ", 513, 0), "truncated or oversized"),
    )
    for payload, message in damaged:
        with pytest.raises(BackupIntegrityError, match=message):
            module._decode_archive_package(payload)
    bad_utf8 = magic + struct.pack(">Q", 1) + struct.pack(">IQ", 1, 0) + b"\xff"
    with pytest.raises(BackupIntegrityError, match="UTF-8"):
        module._decode_archive_package(bad_utf8)
    unsafe = magic + struct.pack(">Q", 1) + struct.pack(">IQ", 1, 0) + b"!"
    with pytest.raises(BackupIntegrityError, match="unsafe"):
        module._decode_archive_package(unsafe)
    with pytest.raises(BackupIntegrityError, match="trailing"):
        module._decode_archive_package(encoded + b"x")

    no_manifest = module._encode_archive_package({"entries": []}, ())
    # Rename the framed manifest entry while keeping framing otherwise valid.
    offset = len(magic) + 8
    name_len, content_len = struct.unpack_from(">IQ", no_manifest, offset)
    start = offset + 12
    altered = bytearray(no_manifest)
    altered[start : start + name_len] = b"111-manifest.json"
    with pytest.raises(BackupIntegrityError, match="omits"):
        module._decode_archive_package(bytes(altered))

    bad_json = module._encode_archive_package({"entries": []}, ())
    manifest_start = len(magic) + 8 + 12 + len("000-manifest.json")
    bad_json = bad_json[:manifest_start] + b"!" + bad_json[manifest_start + 1 :]
    with pytest.raises(BackupIntegrityError, match="invalid JSON"):
        module._decode_archive_package(bad_json)

    noncanonical_manifest = json.dumps(manifest, indent=2).encode("utf-8")
    framed = bytearray(magic)
    framed.extend(struct.pack(">Q", 1))
    name = b"000-manifest.json"
    framed.extend(struct.pack(">IQ", len(name), len(noncanonical_manifest)))
    framed.extend(name)
    framed.extend(noncanonical_manifest)
    with pytest.raises(BackupIntegrityError, match="not canonical"):
        module._decode_archive_package(bytes(framed))

    incomplete = dict(manifest)
    incomplete["entries"] = []
    with pytest.raises(BackupIntegrityError, match="incomplete"):
        module._decode_archive_package(
            module._encode_archive_package(incomplete, (("a.bin", b"x"),))
        )
    malformed_index = dict(manifest)
    malformed_index["entries"] = [{}]
    with pytest.raises(BackupIntegrityError, match="malformed"):
        module._decode_archive_package(
            module._encode_archive_package(malformed_index, (("a.bin", b"x"),))
        )
    differing = dict(manifest)
    differing["entries"] = [{"name": "a.bin", "byte_count": 2, "digest": "0" * 64}]
    with pytest.raises(BackupIntegrityError, match="differs"):
        module._decode_archive_package(
            module._encode_archive_package(differing, (("a.bin", b"x"),))
        )


def test_backup_helper_collection_host_and_platform_durability_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ctypes

    import strathmark.v3.infrastructure.backup as module

    material: dict[str, object] = {}
    module._collect_blob_references({"nested": [1, {"other": "value"}]}, material)
    assert material == {}
    assert module._receipt_ids_from_result(
        {"receipt_id": "receipt:one", "nested": [{"receipt_ids": ["receipt:two", 3]}]}
    ) == ("receipt:one", "receipt:two")

    unreadable = tmp_path / "unreadable"
    with pytest.raises(BackupIntegrityError, match="cannot be read"):
        module._file_digest(unreadable)
    wrong = sign_manifest(
        "checkpoint",
        {},
        signer=P256EphemeralSigner.generate("integrity-key:manifest-kind"),
        created_at=NOW,
    )
    wrong_path = tmp_path / "wrong.json"
    wrong_path.write_bytes(canonical_bytes(wrong.to_dict()))
    with pytest.raises(BackupIntegrityError, match="wrong kind"):
        module._read_manifest(wrong_path, "backup")

    class FakeCall:
        def __init__(self, result: object) -> None:
            self.result = result
            self.argtypes = None
            self.restype = None

        def __call__(self, *args: object) -> object:
            return self.result

    class FakeKernel:
        def __init__(self, move_result: object) -> None:
            self.MoveFileExW = FakeCall(move_result)

    source = tmp_path / "source"
    source.write_bytes(b"source")
    with monkeypatch.context() as context:
        context.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: FakeKernel(0), raising=False)
        context.setattr(ctypes, "get_last_error", lambda: 5)
        with pytest.raises(BackupError, match="rename failed"):
            module._durable_replace(source, tmp_path / "target")
        with pytest.raises(BackupError, match="activation failed"):
            module._publish_restore_database(source, tmp_path / "restore")
        with pytest.raises(BackupError, match="publication failed"):
            module._publish_generation(source, tmp_path / "generation")
    with monkeypatch.context() as context:
        context.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: FakeKernel(0), raising=False)
        context.setattr(ctypes, "get_last_error", lambda: 80)
        with pytest.raises(BackupError, match="already exists"):
            module._publish_restore_database(source, tmp_path / "restore")
        with pytest.raises(BackupError, match="already exists"):
            module._publish_generation(source, tmp_path / "generation")

    for function, target in (
        (module._durable_replace, tmp_path / "posix-replace"),
        (module._publish_restore_database, tmp_path / "posix-restore"),
        (module._publish_generation, tmp_path / "posix-generation"),
    ):
        current_source = tmp_path / f"source-{target.name}"
        current_source.write_bytes(b"x")
        with monkeypatch.context() as context:
            context.setattr(module.os, "name", "posix")
            context.setattr(module.os, "open", lambda *_args, **_kwargs: 77)
            context.setattr(module.os, "fsync", lambda _descriptor: None)
            context.setattr(module.os, "close", lambda _descriptor: None)
            function(current_source, target)
        assert target.exists()

    existing = tmp_path / "exists"
    existing.write_bytes(b"existing")
    candidate = tmp_path / "candidate"
    candidate.write_bytes(b"candidate")
    with pytest.raises(BackupError, match="already exists"):
        module._publish_generation(candidate, existing)

    # Windows host lookup and POSIX fallback failures are explicit readiness blockers.
    with monkeypatch.context() as context:
        context.setattr(module.os, "name", "posix")
        original_read = Path.read_text
        context.setattr(
            Path, "read_text", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("missing"))
        )
        with pytest.raises(ArchiveEligibilityError, match="cannot be proven"):
            module._os_host_id()
        context.setattr(Path, "read_text", original_read)


def test_backup_manifest_restore_and_database_attack_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import strathmark.v3.infrastructure.backup as module
    from strathmark.v3.infrastructure.sqlite.migrations import EXPECTED_SCHEMA_DIGEST

    database = tmp_path / "authority.sqlite3"
    SQLiteEventStore(database)
    signer = P256EphemeralSigner.generate("integrity-key:restore-matrix")
    manager = _backup_manager(
        database,
        ContentAddressedBlobStore(tmp_path / "blobs"),
        signer,
        tmp_path / "registry",
    )
    artifact = manager.create_online_backup(
        tmp_path / "backup",
        required_blobs=(),
        pinned_bundle_digest="b" * 64,
        issues=(),
        created_at=NOW,
    )
    baseline_manifest = module._read_manifest(artifact.manifest_path, "backup")
    baseline = module.verify_manifest(baseline_manifest, manager.trust_store)

    with pytest.raises(BackupError, match="differ"):
        manager.restore_verified(
            artifact,
            artifact.database_path,
            installed_bundle_digest="b" * 64,
            manager_receipts={},
        )

    def write(payload: dict[str, object]) -> None:
        signed = sign_manifest("backup", payload, signer=signer, created_at=NOW)
        artifact.manifest_path.write_bytes(canonical_bytes(signed.to_dict()))

    cases: tuple[tuple[str, object, str], ...] = (
        ("checkpoint", None, "checkpoint"),
        ("checkpoint", baseline_manifest.to_dict(), "wrong kind"),
        ("snapshot", None, "snapshot"),
        ("database_byte_count", artifact.database_path.stat().st_size + 1, "length"),
    )
    for index, (field, value, message) in enumerate(cases):
        payload = dict(baseline)
        payload[field] = value
        write(payload)
        with pytest.raises(BackupIntegrityError, match=message):
            manager.restore_verified(
                artifact,
                tmp_path / f"restore-invalid-{index}.sqlite3",
                installed_bundle_digest="b" * 64,
                manager_receipts={},
            )

    for index, (field, value, message) in enumerate(
        (
            ("projection_digest", "9" * 64, "external checkpoint"),
            ("required_blobs", None, "blob manifest"),
            ("issues", None, "issue manifest"),
        )
    ):
        payload = dict(baseline)
        snapshot = dict(baseline["snapshot"])
        snapshot[field] = value
        payload["snapshot"] = snapshot
        write(payload)
        with pytest.raises(BackupIntegrityError, match=message):
            manager.restore_verified(
                artifact,
                tmp_path / f"restore-snapshot-{index}.sqlite3",
                installed_bundle_digest="b" * 64,
                manager_receipts={},
            )

    write(dict(baseline))
    trusted = manager.checkpoint_registry.latest_checkpoint()
    with monkeypatch.context() as context:
        context.setattr(
            manager.checkpoint_registry,
            "verify_checkpoint",
            lambda _manifest: replace(trusted, authority_sequence=trusted.authority_sequence + 1),
        )
        with pytest.raises(BackupIntegrityError, match="registry binding"):
            manager.restore_verified(
                artifact,
                tmp_path / "restore-registry.sqlite3",
                installed_bundle_digest="b" * 64,
                manager_receipts={},
            )

    snapshot = dict(baseline["snapshot"])
    report = manager._verify_database(artifact.database_path, snapshot, ())
    original_verify = manager._verify_database
    calls = 0

    def changed_after_activation(
        path: Path, expected: dict[str, object], issues: tuple[IssueExpectation, ...]
    ):
        nonlocal calls
        calls += 1
        observed = original_verify(path, expected, issues)
        return observed if calls == 1 else replace(observed, issue_count=observed.issue_count + 1)

    with monkeypatch.context() as context:
        context.setattr(manager, "_verify_database", changed_after_activation)
        with pytest.raises(BackupIntegrityError, match="restored authority differs"):
            manager.restore_verified(
                artifact,
                tmp_path / "restore-changed.sqlite3",
                installed_bundle_digest="b" * 64,
                manager_receipts={},
            )

    with pytest.raises(BackupIntegrityError, match="receipt map"):
        manager._reconcile_critical_recovery(database, issues=(), manager_receipts=[])  # type: ignore[arg-type]
    with monkeypatch.context() as context:
        context.setattr(manager.critical_journal, "reconcile", lambda **_kwargs: ())
        with pytest.raises(BackupIntegrityError, match="not exact"):
            manager._reconcile_critical_recovery(
                database,
                issues=(IssueExpectation("command:ghost", "a" * 64, 1),),
                manager_receipts={},
            )

    class Result:
        def __init__(self, value: str) -> None:
            self.value = value

        def fetchone(self) -> tuple[str]:
            return (self.value,)

    class BadIntegrityConnection:
        def execute(self, _query: str) -> Result:
            return Result("not-ok")

    class Context:
        def __enter__(self) -> BadIntegrityConnection:
            return BadIntegrityConnection()

        def __exit__(self, *_args: object) -> None:
            return None

    with monkeypatch.context() as context:
        context.setattr(module, "open_v3_connection", lambda *_args, **_kwargs: Context())
        with pytest.raises(BackupIntegrityError, match="integrity check"):
            manager._snapshot_material(
                database,
                required_blobs=(),
                pinned_bundle_digest="b" * 64,
                issues=(),
            )
        with pytest.raises(BackupIntegrityError, match="integrity check"):
            manager._verify_database(database, snapshot, ())

    malformed = dict(snapshot)
    malformed["authority_anchor"] = []
    with pytest.raises(BackupIntegrityError, match="anchor"):
        manager._verify_database(database, malformed, ())
    stale = dict(snapshot)
    stale["schema_digest"] = "0" * 64
    with pytest.raises(BackupIntegrityError, match="schema is stale"):
        manager._verify_database(database, stale, ())
    with monkeypatch.context() as context:
        context.setattr(module, "canonical_schema_digest", lambda _connection: "0" * 64)
        with pytest.raises(BackupIntegrityError, match="schema digest differs"):
            manager._verify_database(database, snapshot, ())
    projection = dict(snapshot)
    projection["projection_digest"] = "0" * 64
    with pytest.raises(BackupIntegrityError, match="projection"):
        manager._verify_database(database, projection, ())
    with monkeypatch.context() as context:
        context.setattr(
            manager, "_derive_issues", lambda _path: (IssueExpectation("command:x", "a" * 64, 1),)
        )
        with pytest.raises(BackupIntegrityError, match="issue lookup"):
            manager._verify_database(database, snapshot, ())

    manager._verify_issues(database, ())
    with pytest.raises(BackupIntegrityError, match="lookup"):
        manager._verify_issues(database, (IssueExpectation("command:missing", "a" * 64, 1),))
    assert EXPECTED_SCHEMA_DIGEST == snapshot["schema_digest"]


def test_backup_creation_detects_tip_issue_and_checkpoint_divergence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "authority.sqlite3"
    SQLiteEventStore(database)
    signer = P256EphemeralSigner.generate("integrity-key:create-attacks")
    manager = _backup_manager(
        database,
        ContentAddressedBlobStore(tmp_path / "blobs"),
        signer,
        tmp_path / "registry",
    )
    snapshot = manager._snapshot_material(
        database,
        required_blobs=(),
        pinned_bundle_digest="b" * 64,
        issues=(),
    )
    wrong_tip = dict(snapshot)
    wrong_tip["authority_anchor"] = {"global_sequence": 0, "event_digest": "1" * 64}
    with monkeypatch.context() as context:
        context.setattr(manager, "_snapshot_material", lambda *_args, **_kwargs: wrong_tip)
        with pytest.raises(BackupIntegrityError, match="source authority tip"):
            manager.create_online_backup(
                tmp_path / "wrong-tip",
                required_blobs=(),
                pinned_bundle_digest="b" * 64,
                issues=(),
                created_at=NOW,
            )
    with monkeypatch.context() as context:
        context.setattr(manager, "_snapshot_material", lambda *_args, **_kwargs: snapshot)
        context.setattr(
            manager,
            "_derive_issues",
            lambda _path: (IssueExpectation("command:unexpected", "a" * 64, 1),),
        )
        with pytest.raises(BackupIntegrityError, match="caller issue set"):
            manager.create_online_backup(
                tmp_path / "wrong-issues",
                required_blobs=(),
                pinned_bundle_digest="b" * 64,
                issues=(),
                created_at=NOW,
            )
    wrong_checkpoint = dict(snapshot)
    wrong_checkpoint["projection_digest"] = "9" * 64
    with monkeypatch.context() as context:
        context.setattr(manager, "_snapshot_material", lambda *_args, **_kwargs: wrong_checkpoint)
        with pytest.raises(BackupIntegrityError, match="checkpoint differs"):
            manager.create_online_backup(
                tmp_path / "wrong-checkpoint",
                required_blobs=(),
                pinned_bundle_digest="b" * 64,
                issues=(),
                created_at=NOW,
            )


def test_archive_proof_and_index_attack_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    import strathmark.v3.infrastructure.backup as module

    database = tmp_path / "archive.sqlite3"
    store = SQLiteEventStore(database)
    blobs = ContentAddressedBlobStore(tmp_path / "archive-blobs")
    payload = _blob_payload(b"shared-archive")
    first_ref = blobs.publish(
        payload,
        metadata=BlobMetadata(
            "application/octet-stream", "strathmark-v3-archive-one-v1", BlobRetention.ARCHIVABLE
        ),
    )
    second_ref = blobs.publish(
        payload,
        metadata=BlobMetadata(
            "application/octet-stream", "strathmark-v3-archive-two-v1", BlobRetention.REQUIRED
        ),
    )
    store.execute(
        _tournament_request(
            "command:archive-configure",
            CommandKind.CONFIGURE_TOURNAMENT,
            EventKind.TOURNAMENT_CONFIGURED,
            expected=0,
        )
    )
    opened = _tournament_request(
        "command:archive-open", CommandKind.OPEN_TOURNAMENT, EventKind.TOURNAMENT_OPENED, expected=1
    )
    store.execute(
        replace(
            opened,
            command=replace(opened.command, payload=first_ref),
            result={"second_blob": second_ref.to_dict()},
        )
    )
    store.execute(
        _tournament_request(
            "command:archive-close",
            CommandKind.CLOSE_TOURNAMENT,
            EventKind.TOURNAMENT_CLOSED,
            expected=2,
        )
    )
    signer = _provider_cng_signer(monkeypatch, "strathmark-archive-matrix-local")
    local_identity = signer.identity
    trust = IntegrityTrustStore((local_identity,))
    remote_signer = _provider_cng_signer(monkeypatch, "strathmark-archive-matrix-remote")
    remote_identity = remote_signer.identity
    storage = StorageIdentity("device:remote", "host:remote", "site:remote")
    monkeypatch.setattr(module, "_os_host_id", lambda: "host:local")
    policy = RemoteAttesterPolicy(
        local_trust_store=trust,
        authorizations=(RemoteAttesterAuthorization(remote_identity, storage),),
    )
    registry = CheckpointRegistry(tmp_path / "archive-registry", bootstrap_identity=local_identity)
    registry.create_checkpoint(database, signer=signer, created_at=NOW)
    with pytest.raises(BackupError, match="typed remote"):
        ArchiveManager(
            database,
            signer=signer,
            trust_store=trust,
            remote_attester_policy="bad",  # type: ignore[arg-type]
            blob_store=blobs,
            checkpoint_registry=registry,
        )
    archive = ArchiveManager(
        database,
        signer=signer,
        trust_store=trust,
        remote_attester_policy=policy,
        blob_store=blobs,
        checkpoint_registry=registry,
    )
    material = archive._archive_package_material("tournament:closed")
    assert len(material.required_blobs) == 2
    assert len([name for name, _value in material.entries if name.startswith("blobs/")]) == 1
    package = archive.build_archive_package("tournament:closed", tmp_path / "package.bin")
    assert archive.build_archive_package("tournament:closed", package) == package
    collision = tmp_path / "collision.bin"
    collision.write_bytes(b"other")
    with pytest.raises(ArchiveEligibilityError, match="other bytes"):
        archive.build_archive_package("tournament:closed", collision)

    local_path = tmp_path / "local-copy.bin"
    local_path.write_bytes(package.read_bytes())
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    attestation = attest_remote_archive_copy(
        package_digest=digest,
        storage_identity=storage,
        signer=remote_signer,
        verified_at=NOW,
    )
    local_copy = ArchiveCopy(
        _token=ArchiveCopy._TOKEN,
        path=local_path,
        identity=StorageIdentity("device:local", "host:local", "site:local"),
    )
    remote_copy = ArchiveCopy.remote_attested(attestation, policy)
    copies = (local_copy, remote_copy)

    with pytest.raises(ArchiveEligibilityError, match="not removal eligible"):
        archive.prove_two_copy_archive(
            tournament_id="tournament:closed",
            package_path=package,
            copies=(local_copy,),
            verified_at=NOW,  # type: ignore[arg-type]
        )
    dev_archive = ArchiveManager(
        database,
        signer=P256EphemeralSigner.generate("integrity-key:archive-matrix-dev"),
        trust_store=trust,
        remote_attester_policy=policy,
        blob_store=blobs,
        checkpoint_registry=registry,
    )
    with pytest.raises(ArchiveEligibilityError, match="production CNG"):
        dev_archive.prove_two_copy_archive(
            tournament_id="tournament:closed", package_path=package, copies=copies, verified_at=NOW
        )
    alternate_manifest = dict(material.manifest)
    alternate_manifest["tournament_id"] = "tournament:other"
    alternate = tmp_path / "alternate.bin"
    alternate.write_bytes(module._encode_archive_package(alternate_manifest, material.entries))
    with pytest.raises(ArchiveEligibilityError, match="exact authority"):
        archive.prove_two_copy_archive(
            tournament_id="tournament:closed",
            package_path=alternate,
            copies=copies,
            verified_at=NOW,
        )
    with monkeypatch.context() as context:
        context.setattr(archive, "_archive_package_material", lambda _tournament: material)
        context.setattr(
            blobs,
            "verify_required",
            lambda _references: (_ for _ in ()).throw(BlobIntegrityError("lost")),
        )
        with pytest.raises(ArchiveEligibilityError, match="missing or corrupt"):
            archive.prove_two_copy_archive(
                tournament_id="tournament:closed",
                package_path=package,
                copies=copies,
                verified_at=NOW,
            )

    same_device = ArchiveCopy(
        _token=ArchiveCopy._TOKEN,
        path=local_path,
        identity=StorageIdentity("device:local", "host:other", "site:other"),
    )
    with pytest.raises(ArchiveEligibilityError, match="distinct physical"):
        archive.prove_two_copy_archive(
            tournament_id="tournament:closed",
            package_path=package,
            copies=(local_copy, same_device),
            verified_at=NOW,
        )
    same_host = ArchiveCopy(
        _token=ArchiveCopy._TOKEN,
        path=local_path,
        identity=StorageIdentity("device:other", "host:local", "site:other"),
    )
    with pytest.raises(ArchiveEligibilityError, match="off-host"):
        archive.prove_two_copy_archive(
            tournament_id="tournament:closed",
            package_path=package,
            copies=(local_copy, same_host),
            verified_at=NOW,
        )
    wrong_remote_attestation = attest_remote_archive_copy(
        package_digest="0" * 64,
        storage_identity=storage,
        signer=remote_signer,
        verified_at=NOW,
    )
    wrong_remote = ArchiveCopy(
        _token=ArchiveCopy._TOKEN,
        identity=storage,
        attested_package_digest=digest,
        attestation=wrong_remote_attestation,
    )
    with pytest.raises(ArchiveEligibilityError, match="binding differs"):
        archive.prove_two_copy_archive(
            tournament_id="tournament:closed",
            package_path=package,
            copies=(local_copy, wrong_remote),
            verified_at=NOW,
        )
    wrong_path = tmp_path / "wrong-copy.bin"
    wrong_path.write_bytes(b"wrong")
    wrong_local = ArchiveCopy(
        _token=ArchiveCopy._TOKEN,
        path=wrong_path,
        identity=StorageIdentity("device:other", "host:other", "site:other"),
    )
    with pytest.raises(ArchiveEligibilityError, match="copy digest"):
        archive.prove_two_copy_archive(
            tournament_id="tournament:closed",
            package_path=package,
            copies=(local_copy, wrong_local),
            verified_at=NOW,
        )

    proof = archive.prove_two_copy_archive(
        tournament_id="tournament:closed", package_path=package, copies=copies, verified_at=NOW
    )
    with pytest.raises(ArchiveEligibilityError, match="already exists"):
        archive.prove_two_copy_archive(
            tournament_id="tournament:closed", package_path=package, copies=copies, verified_at=NOW
        )
    with pytest.raises(KeyError):
        archive.lookup("tournament:missing")

    with sqlite3.connect(database) as connection:
        baseline = connection.execute(
            "SELECT manifest_json, signed_manifest_digest, package_digest FROM v3_archive_index WHERE tournament_id=?",
            ("tournament:closed",),
        ).fetchone()
    assert baseline is not None
    baseline_json, baseline_digest, baseline_package = map(str, baseline)

    def set_index(
        *,
        manifest_json: str = baseline_json,
        manifest_digest: str = baseline_digest,
        package_digest: str = baseline_package,
    ) -> None:
        with sqlite3.connect(database) as connection:
            trigger_sql = str(
                connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='v3_archive_index_no_update'"
                ).fetchone()[0]
            )
            connection.execute("DROP TRIGGER v3_archive_index_no_update")
            connection.execute(
                "UPDATE v3_archive_index SET manifest_json=?, signed_manifest_digest=?, package_digest=? WHERE tournament_id=?",
                (manifest_json, manifest_digest, package_digest, "tournament:closed"),
            )
            connection.execute(trigger_sql)

    set_index(manifest_digest="0" * 64)
    with pytest.raises(BackupIntegrityError, match="manifest digest"):
        archive.lookup("tournament:closed")
    original_manifest = SignedManifest.from_dict(json.loads(baseline_json))
    original_payload = module.verify_manifest(original_manifest, trust)
    for field, value, message in (
        ("copies", [], "copy manifest"),
        ("authority_range", [], "authority range"),
    ):
        changed = dict(original_payload)
        changed[field] = value
        signed = sign_manifest("archive", changed, signer=signer, created_at=NOW)
        signed_json = canonical_bytes(signed.to_dict()).decode("utf-8")
        set_index(manifest_json=signed_json, manifest_digest=canonical_digest(signed.to_dict()))
        with pytest.raises(BackupIntegrityError, match=message):
            archive.lookup("tournament:closed")
    set_index(package_digest="f" * 64)
    with pytest.raises(BackupIntegrityError, match="columns differ"):
        archive.lookup("tournament:closed")
    set_index()
    assert archive.lookup("tournament:closed") == proof

    original_open = module.open_v3_connection

    class FixedResult:
        def __init__(self, value: object) -> None:
            self.value = value

        def fetchone(self) -> object:
            return self.value

    class ConnectionProxy:
        def __init__(self, connection: object, attack: str) -> None:
            self.connection = connection
            self.attack = attack

        def __enter__(self) -> ConnectionProxy:
            return self

        def __exit__(self, *args: object) -> object:
            return self.connection.__exit__(*args)

        def execute(self, query: str, parameters: object = ()) -> object:
            if self.attack == "crossing" and "SELECT 1 FROM v3_idempotency_records" in query:
                return FixedResult((1,))
            if self.attack == "undelivered" and "SELECT COUNT(*) FROM v3_outbox" in query:
                return FixedResult((1,))
            return self.connection.execute(query, parameters)

    for attack, message in (("crossing", "atomic command"), ("undelivered", "undelivered")):
        with monkeypatch.context() as context:
            context.setattr(
                module,
                "open_v3_connection",
                lambda *args, _attack=attack, **kwargs: ConnectionProxy(
                    original_open(*args, **kwargs), _attack
                ),
            )
            with pytest.raises(ArchiveEligibilityError, match=message):
                archive._archive_package_material("tournament:closed")


def test_backup_receipt_fallback_open_archive_host_and_size_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    from types import SimpleNamespace

    import strathmark.v3.infrastructure.backup as module

    database = tmp_path / "issue.sqlite3"
    store = SQLiteEventStore(database)
    store.execute(
        _request(
            "command:fallback-prepare",
            CommandKind.OPTIMIZE_FIELD,
            EventKind.FIELD_OPTIMIZED,
            expected=0,
        )
    )
    issued = store.execute(
        _request(
            "command:fallback-issue",
            CommandKind.ACKNOWLEDGE_ISSUE,
            EventKind.FIELD_ISSUED,
            expected=1,
        )
    )
    signer = P256EphemeralSigner.generate("integrity-key:fallback")
    manager = _backup_manager(
        database,
        ContentAddressedBlobStore(tmp_path / "issue-blobs"),
        signer,
        tmp_path / "issue-registry",
    )
    issue = IssueExpectation(
        "command:fallback-issue", issued.result_digest, issued.last_global_sequence
    )
    _record_issue_recovery(manager, issue, ("receipt:one",))
    with monkeypatch.context() as context:
        context.setattr(module, "_receipt_ids_from_result", lambda _value: ())
        with pytest.raises(BackupIntegrityError, match="independently derived"):
            manager._reconcile_critical_recovery(
                database,
                issues=(issue,),
                manager_receipts={issue.command_id: ("receipt:one",)},
            )

    mismatch_signer = P256EphemeralSigner.generate("integrity-key:mismatched-intent")
    mismatch_manager = _backup_manager(
        database,
        ContentAddressedBlobStore(tmp_path / "mismatch-blobs"),
        mismatch_signer,
        tmp_path / "mismatch-registry",
    )
    mismatch_manager.critical_journal.record_intent(
        CriticalIssueIntent(
            issue.command_id,
            "f" * 64,
            "a" * 64,
            (("field:one", 1),),
            ("receipt:one",),
            NOW,
        )
    )
    with pytest.raises(BackupIntegrityError, match="stored critical issue command"):
        mismatch_manager._reconcile_critical_recovery(
            database,
            issues=(issue,),
            manager_receipts={issue.command_id: ("receipt:one",)},
        )
    manager._verify_issues(database, (issue,))

    open_database = tmp_path / "open.sqlite3"
    open_store = SQLiteEventStore(open_database)
    open_store.execute(
        _tournament_request(
            "command:open-configure",
            CommandKind.CONFIGURE_TOURNAMENT,
            EventKind.TOURNAMENT_CONFIGURED,
            expected=0,
        )
    )
    open_store.execute(
        _tournament_request(
            "command:open-only",
            CommandKind.OPEN_TOURNAMENT,
            EventKind.TOURNAMENT_OPENED,
            expected=1,
        )
    )
    local_signer = _provider_cng_signer(monkeypatch, "strathmark-open-local")
    local_identity = local_signer.identity
    trust = IntegrityTrustStore((local_identity,))
    remote_signer = _provider_cng_signer(monkeypatch, "strathmark-open-remote")
    remote_identity = remote_signer.identity
    with monkeypatch.context() as context:
        context.setattr(module, "_os_host_id", lambda: "host:local")
        policy = RemoteAttesterPolicy(
            local_trust_store=trust,
            authorizations=(
                RemoteAttesterAuthorization(
                    remote_identity, StorageIdentity("device:remote", "host:remote", "site:remote")
                ),
            ),
        )
    registry = CheckpointRegistry(tmp_path / "open-registry", bootstrap_identity=local_identity)
    registry.create_checkpoint(open_database, signer=local_signer, created_at=NOW)
    archive = ArchiveManager(
        open_database,
        signer=local_signer,
        trust_store=trust,
        remote_attester_policy=policy,
        blob_store=ContentAddressedBlobStore(tmp_path / "open-blobs"),
        checkpoint_registry=registry,
    )
    with pytest.raises(ArchiveEligibilityError, match="not closed"):
        archive.build_archive_package("tournament:closed", tmp_path / "open.package")

    with monkeypatch.context() as context:
        context.setattr(module.os, "name", "posix")
        context.setattr(Path, "read_text", lambda *_args, **_kwargs: "machine-id")
        assert module._os_host_id().startswith("host:")
    reads = iter(("", "fallback-machine-id"))
    with monkeypatch.context() as context:
        context.setattr(module.os, "name", "posix")
        context.setattr(Path, "read_text", lambda *_args, **_kwargs: next(reads))
        assert module._os_host_id().startswith("host:")

    class BrokenKey:
        def __enter__(self) -> BrokenKey:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    failing_winreg = SimpleNamespace(
        HKEY_LOCAL_MACHINE=1,
        KEY_READ=1,
        KEY_WOW64_64KEY=2,
        OpenKey=lambda *_args: (_ for _ in ()).throw(OSError("denied")),
        QueryValueEx=lambda *_args: ("", 0),
    )
    with monkeypatch.context() as context:
        context.setattr(module.os, "name", "nt")
        context.setitem(sys.modules, "winreg", failing_winreg)
        with pytest.raises(ArchiveEligibilityError, match="cannot be proven"):
            module._os_host_id()
    empty_winreg = SimpleNamespace(
        HKEY_LOCAL_MACHINE=1,
        KEY_READ=1,
        KEY_WOW64_64KEY=2,
        OpenKey=lambda *_args: BrokenKey(),
        QueryValueEx=lambda *_args: ("", 0),
    )
    with monkeypatch.context() as context:
        context.setattr(module.os, "name", "nt")
        context.setitem(sys.modules, "winreg", empty_winreg)
        with pytest.raises(ArchiveEligibilityError, match="cannot be proven"):
            module._os_host_id()

    encoded = module._encode_archive_package({"entries": []}, ())
    with monkeypatch.context() as context:
        context.setattr(module, "_MAX_ARCHIVE_PACKAGE_BYTES", 1)
        with pytest.raises(ArchiveEligibilityError, match="exceeds"):
            module._encode_archive_package({"entries": []}, ())
        with pytest.raises(BackupIntegrityError, match="exceeds"):
            module._decode_archive_package(encoded)

    for function, source_name, destination_name in (
        (module._publish_restore_database, "restore-source", "restore-existing"),
        (module._publish_generation, "generation-source", "generation-existing"),
    ):
        source = tmp_path / source_name
        source.write_bytes(b"source")
        destination = tmp_path / destination_name
        with monkeypatch.context() as context:
            context.setattr(module.os, "name", "posix")
            if function is module._publish_restore_database:
                context.setattr(
                    module.os, "link", lambda *_args: (_ for _ in ()).throw(FileExistsError())
                )
            else:
                context.setattr(
                    module.os, "rename", lambda *_args: (_ for _ in ()).throw(FileExistsError())
                )
            with pytest.raises(BackupError, match="already exists"):
                function(source, destination)
