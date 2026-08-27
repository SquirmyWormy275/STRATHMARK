"""Online backup, verified restore, archive proof, and disk-reserve policy for V3."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import struct
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping

from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.commands import (
    BLOB_REFERENCE_V2_SCHEMA_VERSION,
    BlobReferenceV2,
    CommandKind,
)
from strathmark.v3.contracts.events import EventEnvelope
from strathmark.v3.contracts.evidence import require_utc_milliseconds
from strathmark.v3.infrastructure.blobs import (
    BlobIntegrityError,
    BlobMetadata,
    ContentAddressedBlobStore,
    StoredBlobReference,
)
from strathmark.v3.infrastructure.integrity import (
    CheckpointRegistry,
    CriticalDatabaseCommit,
    CriticalJournal,
    IntegrityError,
    IntegrityKeyClass,
    IntegrityKeyIdentity,
    IntegrityTrustStore,
    P256Signer,
    RecoveryState,
    SignedManifest,
    StorageIdentity,
    VerifiedRecoveryTopology,
    require_production_cng_signer,
    sign_manifest,
    verify_manifest,
)
from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection
from strathmark.v3.infrastructure.sqlite.event_store import (
    ZERO_DIGEST,
    AuthorityAnchor,
    SQLiteEventStore,
    verify_read_only_authority,
)
from strathmark.v3.infrastructure.sqlite.migrations import (
    EXPECTED_SCHEMA_DIGEST,
    canonical_schema_digest,
    migrate_connection,
)
from strathmark.v3.infrastructure.sqlite.outbox import verify_outbox_integrity
from strathmark.v3.infrastructure.sqlite.projections import SQLiteProjectionStore

_TOKEN = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ARCHIVE_MAGIC = b"STRATHMARK-V3-ARCHIVE-PACKAGE-V1\n"
_ARCHIVE_ENTRY_NAME = re.compile(r"^[a-z0-9][a-z0-9./-]{0,511}$")
_MAX_ARCHIVE_MANIFEST_BYTES = 67_108_864
_MAX_ARCHIVE_PACKAGE_BYTES = 2_147_483_648


class BackupError(RuntimeError):
    """Base backup/archive policy error."""


class BackupIntegrityError(BackupError):
    """A backup or restored authority is incomplete or inconsistent."""


class ArchiveEligibilityError(BackupError):
    """Closed evidence has not met the two-copy removal precondition."""


@dataclass(frozen=True, slots=True)
class IssueExpectation:
    command_id: str
    result_digest: str
    last_global_sequence: int

    def __post_init__(self) -> None:
        _require_token(self.command_id, "issue command id")
        _require_digest(self.result_digest, "issue result digest")
        if (
            isinstance(self.last_global_sequence, bool)
            or not isinstance(self.last_global_sequence, int)
            or self.last_global_sequence <= 0
        ):
            raise BackupError("issue sequence must be positive")

    def to_dict(self) -> dict[str, object]:
        return {
            "command_id": self.command_id,
            "result_digest": self.result_digest,
            "last_global_sequence": self.last_global_sequence,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> IssueExpectation:
        if set(value) != {"command_id", "result_digest", "last_global_sequence"}:
            raise BackupIntegrityError("issue expectation is malformed")
        return cls(value["command_id"], value["result_digest"], value["last_global_sequence"])


@dataclass(frozen=True, slots=True)
class BackupArtifact:
    generation_root: Path
    database_path: Path
    manifest_path: Path
    blob_root: Path


@dataclass(frozen=True, slots=True)
class RestoreReport:
    ready: bool
    authority_anchor: AuthorityAnchor
    projection_digest: str
    issue_count: int


class BackupManager:
    def __init__(
        self,
        database_path: Path | str,
        *,
        blob_store: ContentAddressedBlobStore,
        signer: P256Signer,
        trust_store: IntegrityTrustStore,
        checkpoint_registry: CheckpointRegistry,
        critical_journal: CriticalJournal,
    ) -> None:
        self.database_path = Path(database_path).expanduser().resolve(strict=False)
        self.blob_store = blob_store
        self.signer = signer
        self.trust_store = trust_store
        if not isinstance(checkpoint_registry, CheckpointRegistry):
            raise BackupError("backup manager requires an external checkpoint registry")
        self.checkpoint_registry = checkpoint_registry
        if not isinstance(critical_journal, CriticalJournal):
            raise BackupError("backup manager requires the separate critical issue journal")
        self.critical_journal = critical_journal

    def create_online_backup(
        self,
        destination: Path | str,
        *,
        required_blobs: tuple[StoredBlobReference, ...],
        pinned_bundle_digest: str,
        issues: tuple[IssueExpectation, ...],
        created_at: str,
        fault_hook: Callable[[str], None] | None = None,
    ) -> BackupArtifact:
        generation_root = Path(destination).expanduser().resolve(strict=False)
        if generation_root == self.database_path:
            raise BackupError("backup destination must differ from the authority database")
        _require_digest(pinned_bundle_digest, "pinned bundle digest")
        timestamp = require_utc_milliseconds(created_at)
        if not isinstance(required_blobs, tuple) or not isinstance(issues, tuple):
            raise BackupError("backup blob and issue manifests must be immutable tuples")
        if fault_hook is not None and not callable(fault_hook):
            raise BackupError("backup fault hook must be callable")
        if generation_root.exists():
            raise BackupError("backup generation destination already exists")
        source_store = SQLiteEventStore(self.database_path)
        source_anchor = source_store.current_anchor()
        generation_root.parent.mkdir(parents=True, exist_ok=True)
        staging = generation_root.parent / f".{generation_root.name}.{uuid.uuid4().hex}.tmp"
        staging.mkdir(parents=False, exist_ok=False)
        target = staging / "authority.sqlite3"
        try:
            with open_v3_connection(self.database_path, read_only=True) as source:
                destination_connection = sqlite3.connect(target)
                try:
                    source.backup(destination_connection)
                finally:
                    destination_connection.close()
            _fsync_file(target)
            _fault(fault_hook, "after_database")

            authority_blobs = self._derive_blob_references(target)
            by_descriptor = {
                canonical_digest(reference.to_dict()): reference
                for reference in (*required_blobs, *authority_blobs)
            }
            required_blobs = tuple(
                sorted(by_descriptor.values(), key=lambda item: canonical_bytes(item.to_dict()))
            )
            self.blob_store.verify_required(required_blobs)

            snapshot = self._snapshot_material(
                target,
                required_blobs=required_blobs,
                pinned_bundle_digest=pinned_bundle_digest,
                issues=issues,
            )
            if snapshot["authority_anchor"] != {
                "global_sequence": source_anchor.global_sequence,
                "event_digest": source_anchor.event_digest,
            }:
                raise BackupIntegrityError(
                    "online backup tip differs from the source authority tip"
                )
            if issues != self._derive_issues(target):
                raise BackupIntegrityError(
                    "caller issue set differs from the complete acknowledged issue set"
                )
            backup_blob_root = staging / "blobs"
            backup_blobs = ContentAddressedBlobStore(backup_blob_root)
            for reference in required_blobs:
                backup_blobs.publish(
                    self.blob_store.read(reference),
                    metadata=BlobMetadata.from_reference(reference),
                )
            backup_blobs.verify_required(required_blobs)
            _fault(fault_hook, "after_blobs")
            trusted_checkpoint = self.checkpoint_registry.create_checkpoint(
                target, signer=self.signer, created_at=timestamp
            )
            checkpoint = trusted_checkpoint.manifest
            checkpoint_payload = verify_manifest(checkpoint, self.trust_store)
            for key in (
                "authority_anchor",
                "schema_digest",
                "projection_digest",
                "aggregate_heads_digest",
            ):
                if checkpoint_payload.get(key) != snapshot.get(key):
                    raise BackupIntegrityError(
                        "external checkpoint differs from the backup snapshot"
                    )
            backup_manifest = sign_manifest(
                "backup",
                {
                    "database_digest": snapshot["database_digest"],
                    "database_byte_count": snapshot["database_byte_count"],
                    "checkpoint": checkpoint.to_dict(),
                    "snapshot": {
                        key: value
                        for key, value in snapshot.items()
                        if key not in {"database_digest", "database_byte_count"}
                    },
                },
                signer=self.signer,
                created_at=timestamp,
            )
            manifest_path = staging / "manifest.json"
            _durable_write(manifest_path, canonical_bytes(backup_manifest.to_dict()))
            _fault(fault_hook, "after_manifest")
            _publish_generation(staging, generation_root)
            return BackupArtifact(
                generation_root,
                generation_root / "authority.sqlite3",
                generation_root / "manifest.json",
                generation_root / "blobs",
            )
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def restore_verified(
        self,
        artifact: BackupArtifact,
        destination: Path | str,
        *,
        installed_bundle_digest: str,
        manager_receipts: Mapping[str, tuple[str, ...]],
    ) -> RestoreReport:
        restored = Path(destination).expanduser().resolve(strict=False)
        if restored == artifact.database_path:
            raise BackupError("restore destination must differ from backup source")
        if restored.exists():
            raise BackupError("restore destination must be new and empty")
        try:
            manifest = _read_manifest(artifact.manifest_path, "backup")
            backup_payload = verify_manifest(manifest, self.trust_store)
            checkpoint_value = backup_payload.get("checkpoint")
            if not isinstance(checkpoint_value, dict):
                raise BackupIntegrityError("backup omits its signed checkpoint")
            checkpoint = SignedManifest.from_dict(checkpoint_value)
            if checkpoint.kind != "checkpoint":
                raise BackupIntegrityError("backup checkpoint has the wrong kind")
            trusted_checkpoint = self.checkpoint_registry.verify_checkpoint(checkpoint)
            checkpoint_expected = verify_manifest(checkpoint, self.trust_store)
            expected = backup_payload.get("snapshot")
            if not isinstance(expected, dict):
                raise BackupIntegrityError("backup omits its signed snapshot material")
            if trusted_checkpoint.authority_sequence != checkpoint_expected.get(
                "authority_anchor", {}
            ).get("global_sequence"):
                raise BackupIntegrityError("backup checkpoint registry binding differs")
            for key in (
                "authority_anchor",
                "schema_digest",
                "projection_digest",
                "aggregate_heads_digest",
            ):
                if checkpoint_expected.get(key) != expected.get(key):
                    raise BackupIntegrityError("backup snapshot differs from external checkpoint")
            if _file_digest(artifact.database_path) != backup_payload.get("database_digest"):
                raise BackupIntegrityError("backup database digest differs")
            if artifact.database_path.stat().st_size != backup_payload.get("database_byte_count"):
                raise BackupIntegrityError("backup database length differs")
            _require_digest(installed_bundle_digest, "installed bundle digest")
            if expected.get("pinned_bundle_digest") != installed_bundle_digest:
                raise BackupIntegrityError("installed bundle is stale for this backup")
            references_value = expected.get("required_blobs")
            if not isinstance(references_value, list):
                raise BackupIntegrityError("backup blob manifest is malformed")
            references = tuple(StoredBlobReference.from_dict(value) for value in references_value)
            backup_blobs = ContentAddressedBlobStore(artifact.blob_root, create=False)
            backup_blobs.verify_required(references)
            issues_value = expected.get("issues")
            if not isinstance(issues_value, list):
                raise BackupIntegrityError("backup issue manifest is malformed")
            issues = tuple(IssueExpectation.from_dict(value) for value in issues_value)
            report = self._verify_database(artifact.database_path, expected, issues)
            self._reconcile_critical_recovery(
                artifact.database_path,
                issues=issues,
                manager_receipts=manager_receipts,
            )
        except (BackupIntegrityError, IntegrityError):
            raise
        except Exception as exc:
            raise BackupIntegrityError("backup verification failed closed") from exc

        for reference in references:
            self.blob_store.publish(
                backup_blobs.read(reference),
                metadata=BlobMetadata.from_reference(reference),
            )
        self.blob_store.verify_required(references)
        restored.parent.mkdir(parents=True, exist_ok=True)
        temporary = restored.parent / f".{restored.name}.{uuid.uuid4().hex}.tmp"
        try:
            source = sqlite3.connect(f"file:{artifact.database_path.as_posix()}?mode=ro", uri=True)
            destination_connection = sqlite3.connect(temporary)
            try:
                source.backup(destination_connection)
            finally:
                source.close()
                destination_connection.close()
            _fsync_file(temporary)
            _publish_restore_database(temporary, restored)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        restored_report = self._verify_database(restored, expected, issues)
        if restored_report != report:
            raise BackupIntegrityError("restored authority differs from verified backup")
        return restored_report

    def _reconcile_critical_recovery(
        self,
        database_path: Path,
        *,
        issues: tuple[IssueExpectation, ...],
        manager_receipts: Mapping[str, tuple[str, ...]],
    ) -> None:
        if not isinstance(manager_receipts, Mapping):
            raise BackupIntegrityError("restore requires the tournament-manager receipt map")

        def lookup(command_id: str) -> CriticalDatabaseCommit | None:
            with open_v3_connection(database_path, read_only=True) as connection:
                row = connection.execute(
                    "SELECT record.result_json, record.result_digest, record.last_global_sequence, "
                    "event.envelope_json FROM v3_idempotency_records record "
                    "JOIN v3_events event ON event.global_sequence=record.first_global_sequence "
                    "WHERE record.idempotency_key=?",
                    (command_id,),
                ).fetchone()
            if row is None:
                return None
            intent_manifest = self.critical_journal._read(
                self.critical_journal.intent_path(command_id), "critical_intent"
            )
            intent_payload = verify_manifest(intent_manifest, self.critical_journal.trust_store)
            receipt_ids = _receipt_ids_from_result(json.loads(str(row[0])))
            if not receipt_ids:
                raise BackupIntegrityError(
                    "stored critical issue result has no independently derived receipt identity"
                )
            event = EventEnvelope.from_dict(json.loads(str(row[3])))
            command = event.command
            payload = command.payload
            approval_digest = (
                payload.to_value().get("approval_snapshot_digest")
                if hasattr(payload, "to_value")
                else None
            )
            if (
                intent_payload.get("command_digest") != canonical_digest(command.to_dict())
                or intent_payload.get("expected_versions")
                != [list(item) for item in command.expected_versions]
                or intent_payload.get("approval_snapshot_digest") != approval_digest
                or intent_payload.get("receipt_ids") != list(receipt_ids)
            ):
                raise BackupIntegrityError(
                    "stored critical issue command/result differs from its signed intent"
                )
            return CriticalDatabaseCommit(
                int(row[2]), str(row[1]), receipt_ids, intent_manifest.body_digest
            )

        try:
            records = self.critical_journal.reconcile(
                database_lookup=lookup, manager_receipts=manager_receipts
            )
        except IntegrityError as exc:
            raise BackupIntegrityError("critical issue recovery reconciliation failed") from exc
        issue_commands = {item.command_id for item in issues}
        if issue_commands != set(manager_receipts) or issue_commands != {
            item.command_id for item in records if item.state is RecoveryState.COMMITTED
        }:
            raise BackupIntegrityError(
                "backup, critical journal, and manager issue maps are not exact"
            )
        if any(item.state is not RecoveryState.COMMITTED for item in records):
            raise BackupIntegrityError("critical journal contains an unresolved intent")

    def _snapshot_material(
        self,
        database_path: Path,
        *,
        required_blobs: tuple[StoredBlobReference, ...],
        pinned_bundle_digest: str,
        issues: tuple[IssueExpectation, ...],
    ) -> dict[str, Any]:
        with open_v3_connection(database_path, read_only=True) as connection:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                raise BackupIntegrityError("SQLite integrity check failed")
            verify_outbox_integrity(connection, trust_store=self.trust_store)
            schema_digest = canonical_schema_digest(connection)
            projection_digest = SQLiteProjectionStore.projection_digest(connection)
            row = connection.execute(
                "SELECT global_sequence, event_digest FROM v3_events "
                "ORDER BY global_sequence DESC LIMIT 1"
            ).fetchone()
        anchor = (
            AuthorityAnchor(0, ZERO_DIGEST)
            if row is None
            else AuthorityAnchor(int(row[0]), str(row[1]))
        )
        verify_read_only_authority(database_path, trusted_anchor=anchor)
        if self._derive_issues(database_path) != issues:
            raise BackupIntegrityError(
                "snapshot does not contain the complete acknowledged issue set"
            )
        return {
            "database_digest": _file_digest(database_path),
            "database_byte_count": database_path.stat().st_size,
            "authority_anchor": {
                "global_sequence": anchor.global_sequence,
                "event_digest": anchor.event_digest,
            },
            "schema_digest": schema_digest,
            "projection_digest": projection_digest,
            "aggregate_heads_digest": canonical_digest(
                {
                    "schema_version": "strathmark-v3-aggregate-heads-v1",
                    "heads": [
                        {
                            "aggregate_kind": str(head[0]),
                            "aggregate_id": str(head[1]),
                            "aggregate_version": int(head[2]),
                            "event_digest": str(head[3]),
                        }
                        for head in self._aggregate_heads(database_path)
                    ],
                }
            ),
            "pinned_bundle_digest": pinned_bundle_digest,
            "required_blobs": [reference.to_dict() for reference in required_blobs],
            "issues": [issue.to_dict() for issue in issues],
        }

    def _verify_database(
        self,
        database_path: Path,
        expected: dict[str, Any],
        issues: tuple[IssueExpectation, ...],
    ) -> RestoreReport:
        anchor_value = expected.get("authority_anchor")
        if not isinstance(anchor_value, dict):
            raise BackupIntegrityError("checkpoint authority anchor is malformed")
        anchor = AuthorityAnchor(
            anchor_value.get("global_sequence"), anchor_value.get("event_digest")
        )
        with open_v3_connection(database_path, read_only=True) as connection:
            if str(connection.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
                raise BackupIntegrityError("restored SQLite integrity check failed")
            verify_outbox_integrity(connection, trust_store=self.trust_store)
            signed_schema_digest = expected.get("schema_digest")
            if signed_schema_digest != EXPECTED_SCHEMA_DIGEST:
                raise BackupIntegrityError("signed backup schema is stale for this runtime")
            if canonical_schema_digest(connection) != signed_schema_digest:
                raise BackupIntegrityError("restored schema digest differs")
            projection_digest = SQLiteProjectionStore.projection_digest(connection)
            if projection_digest != expected.get("projection_digest"):
                raise BackupIntegrityError("restored projection digest differs")
        verify_read_only_authority(database_path, trusted_anchor=anchor)
        if self._derive_issues(database_path) != issues:
            raise BackupIntegrityError("restored complete issue lookup set differs")
        return RestoreReport(True, anchor, projection_digest, len(issues))

    @staticmethod
    def _verify_issues(database_path: Path, issues: tuple[IssueExpectation, ...]) -> None:
        with open_v3_connection(database_path, read_only=True) as connection:
            for issue in issues:
                row = connection.execute(
                    "SELECT result_digest, last_global_sequence FROM v3_idempotency_records "
                    "WHERE idempotency_key=?",
                    (issue.command_id,),
                ).fetchone()
                if row is None or (str(row[0]), int(row[1])) != (
                    issue.result_digest,
                    issue.last_global_sequence,
                ):
                    raise BackupIntegrityError("acknowledged issue lookup is missing or different")

    def _derive_issues(self, database_path: Path) -> tuple[IssueExpectation, ...]:
        material: list[IssueExpectation] = []
        with open_v3_connection(database_path, read_only=True) as connection:
            verify_outbox_integrity(connection, trust_store=self.trust_store)
            rows = connection.execute(
                "SELECT record.idempotency_key, record.result_digest, "
                "record.last_global_sequence, event.envelope_json "
                "FROM v3_idempotency_records record JOIN v3_events event "
                "ON event.global_sequence=record.first_global_sequence "
                "ORDER BY record.first_global_sequence"
            ).fetchall()
        for row in rows:
            event = EventEnvelope.from_dict(json.loads(str(row[3])))
            if event.command.kind in {
                CommandKind.ACKNOWLEDGE_ISSUE,
                CommandKind.ACKNOWLEDGE_BATCH_ISSUE,
            }:
                material.append(IssueExpectation(str(row[0]), str(row[1]), int(row[2])))
        return tuple(material)

    @staticmethod
    def _derive_blob_references(database_path: Path) -> tuple[BlobReferenceV2, ...]:
        material: dict[str, BlobReferenceV2] = {}
        with open_v3_connection(database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT envelope_json FROM v3_events ORDER BY global_sequence"
            ).fetchall()
            results = connection.execute(
                "SELECT result_json FROM v3_idempotency_records ORDER BY first_global_sequence"
            ).fetchall()
            outbox_payloads = connection.execute(
                "SELECT payload_json FROM v3_outbox WHERE state!='acknowledged' "
                "ORDER BY created_at, outbox_id"
            ).fetchall()
        for row in (*rows, *results, *outbox_payloads):
            _collect_blob_references(json.loads(str(row[0])), material)
        return tuple(sorted(material.values(), key=lambda item: canonical_bytes(item.to_dict())))

    @staticmethod
    def _aggregate_heads(database_path: Path) -> tuple[sqlite3.Row, ...]:
        with open_v3_connection(database_path, read_only=True) as connection:
            return tuple(
                connection.execute(
                    "SELECT aggregate_kind, aggregate_id, aggregate_version, event_digest "
                    "FROM v3_aggregate_heads ORDER BY aggregate_kind, aggregate_id"
                ).fetchall()
            )


class ArchiveCopy:
    """OS-probed local copy or trusted signed off-host copy attestation."""

    _TOKEN = object()

    def __init__(
        self,
        *args: object,
        _token: object | None = None,
        path: Path | None = None,
        identity: StorageIdentity | None = None,
        attested_package_digest: str | None = None,
        attestation: SignedManifest | None = None,
    ) -> None:
        if _token is not self._TOKEN or args or identity is None:
            raise ArchiveEligibilityError("archive copy must be created by a verified probe")
        self.path = path
        self.identity = identity
        self.attested_package_digest = attested_package_digest
        self.attestation = attestation

    @classmethod
    def local(cls, path: Path, *, site_id: str) -> ArchiveCopy:
        topology = VerifiedRecoveryTopology.probe(
            path.parent, path.parent, host_id=_os_host_id(), site_id=site_id
        )
        return cls(_token=cls._TOKEN, path=path, identity=topology.primary)

    @classmethod
    def remote_attested(
        cls, attestation: SignedManifest, policy: RemoteAttesterPolicy
    ) -> ArchiveCopy:
        if attestation.kind != "archive_copy_attestation":
            raise ArchiveEligibilityError("remote archive copy attestation has the wrong kind")
        identity, digest = policy.verify(attestation)
        return cls(
            _token=cls._TOKEN,
            identity=identity,
            attested_package_digest=digest,
            attestation=attestation,
        )


@dataclass(frozen=True, slots=True)
class RemoteAttesterAuthorization:
    key_identity: IntegrityKeyIdentity
    storage_identity: StorageIdentity

    def __post_init__(self) -> None:
        if not isinstance(self.key_identity, IntegrityKeyIdentity):
            raise ArchiveEligibilityError("remote attester requires a typed public key identity")
        if not isinstance(self.storage_identity, StorageIdentity):
            raise ArchiveEligibilityError("remote attester requires a typed storage identity")
        if (
            self.key_identity.key_class is not IntegrityKeyClass.PRODUCTION_CNG
            or self.key_identity.provider != "windows_cng_p256_sha256"
        ):
            raise ArchiveEligibilityError(
                "remote archive attester requires a production CNG identity"
            )


class RemoteAttesterPolicy:
    """Key-bound authorization for an exact off-host archive replica identity."""

    def __init__(
        self,
        *,
        local_trust_store: IntegrityTrustStore,
        authorizations: tuple[RemoteAttesterAuthorization, ...],
    ) -> None:
        if not isinstance(authorizations, tuple) or not authorizations:
            raise ArchiveEligibilityError("remote attester policy requires authorizations")
        local_keys = {identity.key_id for identity in local_trust_store.identities}
        local_public_keys = {
            identity.public_key_der_b64 for identity in local_trust_store.identities
        }
        local_host_id = _os_host_id()
        material: dict[str, RemoteAttesterAuthorization] = {}
        authorized_public_keys: set[str] = set()
        for authorization in authorizations:
            if not isinstance(authorization, RemoteAttesterAuthorization):
                raise ArchiveEligibilityError("remote attester authorization is malformed")
            key_id = authorization.key_identity.key_id
            if (
                key_id in local_keys
                or authorization.key_identity.public_key_der_b64 in local_public_keys
            ):
                raise ArchiveEligibilityError("remote attester key overlaps local archive trust")
            if authorization.storage_identity.host_id == local_host_id:
                raise ArchiveEligibilityError("remote attester cannot authorize the local host")
            if key_id in material:
                raise ArchiveEligibilityError("remote attester key is authorized more than once")
            if authorization.key_identity.public_key_der_b64 in authorized_public_keys:
                raise ArchiveEligibilityError(
                    "remote attester public key is authorized more than once"
                )
            material[key_id] = authorization
            authorized_public_keys.add(authorization.key_identity.public_key_der_b64)
        self.local_host_id = local_host_id
        self._authorizations = material
        self._trust_store = IntegrityTrustStore(tuple(item.key_identity for item in authorizations))

    def verify(self, attestation: SignedManifest) -> tuple[StorageIdentity, str]:
        if (
            not isinstance(attestation, SignedManifest)
            or attestation.kind != "archive_copy_attestation"
        ):
            raise ArchiveEligibilityError("remote archive copy attestation has the wrong kind")
        try:
            authorization = self._authorizations[attestation.key_id]
        except KeyError as exc:
            raise ArchiveEligibilityError("remote attester key is not authorized") from exc
        payload = verify_manifest(attestation, self._trust_store)
        if payload.get("attester_role") != "archive_replica_attester":
            raise ArchiveEligibilityError("remote attester role is not authorized")
        identity_value = payload.get("storage_identity")
        digest = payload.get("package_digest")
        if not isinstance(identity_value, dict):
            raise ArchiveEligibilityError("remote archive copy identity is malformed")
        identity = StorageIdentity(
            identity_value.get("device_id"),
            identity_value.get("host_id"),
            identity_value.get("site_id"),
        )
        if identity != authorization.storage_identity:
            raise ArchiveEligibilityError("remote attested storage identity exceeds key scope")
        _require_digest(digest, "remote archive package digest")
        return identity, digest


def attest_remote_archive_copy(
    *,
    package_digest: str,
    storage_identity: StorageIdentity,
    signer: P256Signer,
    verified_at: str,
) -> SignedManifest:
    _require_digest(package_digest, "remote archive package digest")
    require_production_cng_signer(signer)
    return sign_manifest(
        "archive_copy_attestation",
        {
            "attester_role": "archive_replica_attester",
            "package_digest": package_digest,
            "storage_identity": storage_identity.to_dict(),
        },
        signer=signer,
        created_at=verified_at,
    )


@dataclass(frozen=True, slots=True)
class ArchiveProof:
    tournament_id: str
    package_digest: str
    signed_manifest_digest: str
    copy_identities: tuple[StorageIdentity, StorageIdentity]
    verified_at: str
    primary_removal_eligible: bool
    first_global_sequence: int
    last_global_sequence: int
    projection_digest: str
    required_blobs: tuple[BlobReferenceV2, ...]


@dataclass(frozen=True, slots=True)
class _ArchivePackageMaterial:
    manifest: dict[str, Any]
    entries: tuple[tuple[str, bytes], ...]
    required_blobs: tuple[BlobReferenceV2, ...]

    def encoded(self) -> bytes:
        return _encode_archive_package(self.manifest, self.entries)


class ArchiveManager:
    def __init__(
        self,
        database_path: Path | str,
        *,
        signer: P256Signer,
        trust_store: IntegrityTrustStore,
        remote_attester_policy: RemoteAttesterPolicy,
        blob_store: ContentAddressedBlobStore,
        checkpoint_registry: CheckpointRegistry,
    ) -> None:
        self.database_path = Path(database_path).expanduser().resolve(strict=False)
        self.signer = signer
        self.trust_store = trust_store
        if not isinstance(remote_attester_policy, RemoteAttesterPolicy):
            raise BackupError("archive manager requires a typed remote attester policy")
        self.remote_attester_policy = remote_attester_policy
        self.blob_store = blob_store
        self.checkpoint_registry = checkpoint_registry
        with open_v3_connection(self.database_path) as connection:
            migrate_connection(connection)
            verify_outbox_integrity(connection, trust_store=self.trust_store)

    def build_archive_package(self, tournament_id: str, destination: Path | str) -> Path:
        """Build one deterministic self-contained contiguous-prefix archive package."""

        _require_token(tournament_id, "archive tournament id")
        path = Path(destination).expanduser().resolve(strict=False)
        material = self._archive_package_material(tournament_id)
        encoded = material.encoded()
        from strathmark.v3.infrastructure.integrity import _publish_no_clobber

        if not _publish_no_clobber(path, encoded):
            if path.read_bytes() != encoded:
                raise ArchiveEligibilityError("archive package path already binds other bytes")
        return path

    def prove_two_copy_archive(
        self,
        *,
        tournament_id: str,
        package_path: Path,
        copies: tuple[ArchiveCopy, ArchiveCopy],
        verified_at: str,
    ) -> ArchiveProof:
        _require_token(tournament_id, "archive tournament id")
        timestamp = require_utc_milliseconds(verified_at)
        if not isinstance(copies, tuple) or len(copies) != 2:
            raise ArchiveEligibilityError("archive evidence is not removal eligible")
        try:
            require_production_cng_signer(self.signer)
        except IntegrityError as exc:
            raise ArchiveEligibilityError(
                "archive removal proof requires an OS-attested production CNG signer"
            ) from exc
        material = self._archive_package_material(tournament_id)
        try:
            observed_manifest, observed_entries = _decode_archive_package(package_path.read_bytes())
        except (OSError, ValueError, BackupIntegrityError) as exc:
            raise ArchiveEligibilityError("archive package is malformed or incomplete") from exc
        if observed_manifest != material.manifest or observed_entries != dict(material.entries):
            raise ArchiveEligibilityError(
                "archive package does not contain the exact authority and blob material"
            )
        range_value = material.manifest["authority_range"]
        first_sequence = int(range_value["first_global_sequence"])
        last_sequence = int(range_value["last_global_sequence"])
        projection_digest = str(material.manifest["projection_digest"])
        required_blobs = material.required_blobs
        try:
            self.blob_store.verify_required(required_blobs)
        except BlobIntegrityError as exc:
            raise ArchiveEligibilityError("required archive blob is missing or corrupt") from exc
        package_digest = _file_digest(package_path)
        first, second = copies
        if first.identity.device_id == second.identity.device_id:
            raise ArchiveEligibilityError("archive copies must use distinct physical devices")
        if first.identity.host_id == second.identity.host_id:
            raise ArchiveEligibilityError("at least one archive copy must be off-host")
        for copy in copies:
            if copy.attestation is not None:
                remote_identity, remote_digest = self.remote_attester_policy.verify(
                    copy.attestation
                )
                if remote_identity != copy.identity or remote_digest != package_digest:
                    raise ArchiveEligibilityError(
                        "remote copy attester role or package binding differs"
                    )
            observed = (
                _file_digest(copy.path) if copy.path is not None else copy.attested_package_digest
            )
            if observed != package_digest:
                raise ArchiveEligibilityError("archive copy digest differs from the package")
        payload = {
            "tournament_id": tournament_id,
            "package_digest": package_digest,
            "authority_range": {
                "first_global_sequence": first_sequence,
                "last_global_sequence": last_sequence,
                "first_prior_digest": range_value["first_prior_digest"],
                "last_event_digest": range_value["last_event_digest"],
                "checkpoint_digest": range_value["checkpoint_digest"],
            },
            "projection_digest": projection_digest,
            "required_blobs": [reference.to_dict() for reference in required_blobs],
            "copies": [
                {
                    "copy_kind": "local" if copy.path is not None else "remote_attested",
                    "path_digest": (
                        canonical_digest({"path": str(copy.path)})
                        if copy.path is not None
                        else None
                    ),
                    **copy.identity.to_dict(),
                }
                for copy in copies
            ],
            "verified_at": timestamp,
            "primary_removal_eligible": True,
        }
        manifest = sign_manifest("archive", payload, signer=self.signer, created_at=timestamp)
        manifest_json = canonical_bytes(manifest.to_dict()).decode("utf-8")
        proof = ArchiveProof(
            tournament_id,
            package_digest,
            canonical_digest(manifest.to_dict()),
            (first.identity, second.identity),
            timestamp,
            True,
            first_sequence,
            last_sequence,
            projection_digest,
            required_blobs,
        )
        with open_v3_connection(self.database_path) as connection:
            try:
                connection.execute(
                    "INSERT INTO v3_archive_index(tournament_id, package_digest, manifest_json, "
                    "signed_manifest_digest, copy_one_identity, copy_two_identity, verified_at, "
                    "primary_removal_eligible) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                    (
                        tournament_id,
                        package_digest,
                        manifest_json,
                        proof.signed_manifest_digest,
                        canonical_bytes(first.identity.to_dict()).decode("utf-8"),
                        canonical_bytes(second.identity.to_dict()).decode("utf-8"),
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ArchiveEligibilityError("archive proof already exists") from exc
        return proof

    def _archive_package_material(self, tournament_id: str) -> _ArchivePackageMaterial:
        checkpoint = self.checkpoint_registry.verify_database(
            self.database_path, require_current=True
        )
        with open_v3_connection(self.database_path, read_only=True) as connection:
            verify_outbox_integrity(connection, trust_store=self.trust_store)
            target_rows = connection.execute(
                "SELECT global_sequence, envelope_json FROM v3_events "
                "WHERE aggregate_kind='tournament' AND aggregate_id=? "
                "ORDER BY global_sequence",
                (tournament_id,),
            ).fetchall()
            if not target_rows:
                raise ArchiveEligibilityError("archive tournament has no authority events")
            target_last = EventEnvelope.from_dict(json.loads(str(target_rows[-1][1])))
            if target_last.kind.value != "tournament_closed":
                raise ArchiveEligibilityError("archive tournament is not closed")
            last_sequence = int(target_rows[-1][0])
            if last_sequence != checkpoint.authority_sequence:
                raise ArchiveEligibilityError(
                    "archive close boundary is not the current signed checkpoint tip"
                )
            event_rows = connection.execute(
                "SELECT global_sequence, event_id, aggregate_kind, aggregate_id, envelope_json, "
                "event_digest, prior_global_digest FROM v3_events "
                "WHERE global_sequence<=? ORDER BY global_sequence",
                (last_sequence,),
            ).fetchall()
            tournament_prefix = connection.execute(
                "SELECT aggregate_id, envelope_json FROM v3_events "
                "WHERE aggregate_kind='tournament' AND global_sequence<=? "
                "ORDER BY global_sequence",
                (last_sequence,),
            ).fetchall()
            last_by_tournament: dict[str, EventEnvelope] = {}
            for row in tournament_prefix:
                last_by_tournament[str(row[0])] = EventEnvelope.from_dict(json.loads(str(row[1])))
            if any(
                event.kind.value != "tournament_closed" for event in last_by_tournament.values()
            ):
                raise ArchiveEligibilityError(
                    "contiguous archive prefix contains a tournament that is still open"
                )
            crossing = connection.execute(
                "SELECT 1 FROM v3_idempotency_records WHERE first_global_sequence<=? "
                "AND last_global_sequence>? LIMIT 1",
                (last_sequence, last_sequence),
            ).fetchone()
            if crossing is not None:
                raise ArchiveEligibilityError("archive prefix cuts through an atomic command")
            result_rows = connection.execute(
                "SELECT idempotency_key, command_digest, result_schema_version, result_json, "
                "result_digest, first_global_sequence, last_global_sequence, event_set_digest, "
                "created_at FROM v3_idempotency_records WHERE last_global_sequence<=? "
                "ORDER BY first_global_sequence",
                (last_sequence,),
            ).fetchall()
            undelivered = int(
                connection.execute(
                    "SELECT COUNT(*) FROM v3_outbox WHERE source_global_sequence<=? "
                    "AND state!='acknowledged'",
                    (last_sequence,),
                ).fetchone()[0]
            )
            projection_digest = SQLiteProjectionStore.projection_digest(connection)
        if not event_rows or int(event_rows[0][0]) != 1 or undelivered:
            raise ArchiveEligibilityError(
                "authority prefix is incomplete or has undelivered evidence"
            )

        entries: list[tuple[str, bytes]] = []
        blob_material: dict[str, BlobReferenceV2] = {}
        event_index: list[dict[str, Any]] = []
        for row in event_rows:
            name = f"events/{int(row[0]):020d}.json"
            payload = str(row[4]).encode("utf-8")
            event = EventEnvelope.from_dict(json.loads(str(row[4])))
            _collect_blob_references(event.to_dict(), blob_material)
            entries.append((name, payload))
            event_index.append(
                {
                    "global_sequence": int(row[0]),
                    "event_id": str(row[1]),
                    "aggregate_kind": str(row[2]),
                    "aggregate_id": str(row[3]),
                    "entry": name,
                    "byte_count": len(payload),
                    "digest": hashlib.sha256(payload).hexdigest(),
                }
            )
        result_index: list[dict[str, Any]] = []
        for row in result_rows:
            result_value = {
                "idempotency_key": str(row[0]),
                "command_digest": str(row[1]),
                "result_schema_version": str(row[2]),
                "result_json": str(row[3]),
                "result_digest": str(row[4]),
                "first_global_sequence": int(row[5]),
                "last_global_sequence": int(row[6]),
                "event_set_digest": str(row[7]),
                "created_at": str(row[8]),
            }
            _collect_blob_references(json.loads(str(row[3])), blob_material)
            payload = canonical_bytes(result_value)
            name = f"results/{canonical_digest({'command_id': str(row[0])})}.json"
            entries.append((name, payload))
            result_index.append(
                {
                    "idempotency_key": str(row[0]),
                    "entry": name,
                    "byte_count": len(payload),
                    "digest": hashlib.sha256(payload).hexdigest(),
                }
            )
        required_blobs = tuple(
            sorted(blob_material.values(), key=lambda item: canonical_bytes(item.to_dict()))
        )
        try:
            self.blob_store.verify_required(required_blobs)
        except BlobIntegrityError as exc:
            raise ArchiveEligibilityError("required archive blob is missing or corrupt") from exc
        blob_index: list[dict[str, Any]] = []
        added_content: set[str] = set()
        for reference in required_blobs:
            name = f"blobs/{reference.digest}.blob"
            payload = self.blob_store.read(reference)
            if reference.digest not in added_content:
                entries.append((name, payload))
                added_content.add(reference.digest)
            blob_index.append(
                {
                    "reference": reference.to_dict(),
                    "entry": name,
                    "byte_count": len(payload),
                    "digest": hashlib.sha256(payload).hexdigest(),
                }
            )
        entries.sort(key=lambda item: item[0])
        manifest = {
            "schema_version": "strathmark-v3-archive-package-v1",
            "tournament_id": tournament_id,
            "authority_range": {
                "first_global_sequence": 1,
                "last_global_sequence": last_sequence,
                "first_prior_digest": str(event_rows[0][6]),
                "last_event_digest": str(event_rows[-1][5]),
                "checkpoint_digest": checkpoint.manifest.body_digest,
            },
            "projection_digest": projection_digest,
            "events": event_index,
            "results": result_index,
            "required_blobs": [reference.to_dict() for reference in required_blobs],
            "blob_entries": blob_index,
            "entries": [
                {
                    "name": name,
                    "byte_count": len(payload),
                    "digest": hashlib.sha256(payload).hexdigest(),
                }
                for name, payload in entries
            ],
        }
        return _ArchivePackageMaterial(manifest, tuple(entries), required_blobs)

    def lookup(self, tournament_id: str) -> ArchiveProof:
        _require_token(tournament_id, "archive tournament id")
        with open_v3_connection(self.database_path, read_only=True) as connection:
            verify_outbox_integrity(connection, trust_store=self.trust_store)
            row = connection.execute(
                "SELECT * FROM v3_archive_index WHERE tournament_id=?", (tournament_id,)
            ).fetchone()
        if row is None:
            raise KeyError(tournament_id)
        manifest = SignedManifest.from_dict(json.loads(str(row["manifest_json"])))
        payload = verify_manifest(manifest, self.trust_store)
        if canonical_digest(manifest.to_dict()) != str(row["signed_manifest_digest"]):
            raise BackupIntegrityError("archive index manifest digest differs")
        copies = payload.get("copies")
        if not isinstance(copies, list) or len(copies) != 2:
            raise BackupIntegrityError("archive index copy manifest is malformed")
        identities = tuple(
            StorageIdentity(item["device_id"], item["host_id"], item["site_id"]) for item in copies
        )
        authority_range = payload.get("authority_range")
        required_value = payload.get("required_blobs")
        if not isinstance(authority_range, dict) or not isinstance(required_value, list):
            raise BackupIntegrityError("archive authority range or blob manifest is malformed")
        required_blobs = tuple(BlobReferenceV2.from_dict(item) for item in required_value)
        projection_digest = _require_digest(
            payload.get("projection_digest"), "archive projection digest"
        )
        if (
            payload.get("tournament_id") != tournament_id
            or payload.get("package_digest") != str(row["package_digest"])
            or payload.get("verified_at") != str(row["verified_at"])
            or payload.get("primary_removal_eligible") is not bool(row["primary_removal_eligible"])
            or canonical_bytes(identities[0].to_dict()).decode("utf-8")
            != str(row["copy_one_identity"])
            or canonical_bytes(identities[1].to_dict()).decode("utf-8")
            != str(row["copy_two_identity"])
        ):
            raise BackupIntegrityError("archive index columns differ from the signed manifest")
        return ArchiveProof(
            tournament_id,
            str(row["package_digest"]),
            str(row["signed_manifest_digest"]),
            identities,  # type: ignore[arg-type]
            str(row["verified_at"]),
            bool(row["primary_removal_eligible"]),
            authority_range.get("first_global_sequence"),
            authority_range.get("last_global_sequence"),
            projection_digest,
            required_blobs,
        )


class DiskTier(str, Enum):
    NORMAL = "normal"
    WARNING = "warning"
    SPECULATIVE_SUSPENDED = "speculative_suspended"
    CRITICAL = "critical"


def _collect_blob_references(value: Any, material: dict[str, BlobReferenceV2]) -> None:
    if isinstance(value, dict):
        if value.get("schema_version") == BLOB_REFERENCE_V2_SCHEMA_VERSION:
            reference = BlobReferenceV2.from_dict(value)
            descriptor_digest = canonical_digest(reference.to_dict())
            # The dictionary key is the canonical digest of this exact frozen descriptor;
            # equal keys therefore represent exact replay of the same descriptor.
            material[descriptor_digest] = reference
            return
        for child in value.values():
            _collect_blob_references(child, material)
    elif isinstance(value, list):
        for child in value:
            _collect_blob_references(child, material)


def _receipt_ids_from_result(value: Any) -> tuple[str, ...]:
    material: set[str] = set()

    def collect(child: Any) -> None:
        if isinstance(child, dict):
            receipt = child.get("receipt_id")
            if isinstance(receipt, str):
                material.add(receipt)
            receipts = child.get("receipt_ids")
            if isinstance(receipts, list):
                material.update(item for item in receipts if isinstance(item, str))
            for nested in child.values():
                collect(nested)
        elif isinstance(child, list):
            for nested in child:
                collect(nested)

    collect(value)
    return tuple(sorted(material))


def _os_host_id() -> str:
    """Return a non-caller-selectable, stable, privacy-preserving machine identity."""

    raw: str | None = None
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
                0,
                winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            ) as key:
                value, _kind = winreg.QueryValueEx(key, "MachineGuid")
            if isinstance(value, str) and value:
                raw = value
        except OSError as exc:
            raise ArchiveEligibilityError("OS machine identity cannot be proven") from exc
    else:
        for candidate in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
            try:
                value = candidate.read_text(encoding="ascii").strip()
            except OSError:
                continue
            if value:
                raw = value
                break
    if raw is None:
        raise ArchiveEligibilityError("OS machine identity cannot be proven")
    return f"host:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


class DiskOperation(str, Enum):
    FACTORY = "factory"
    BACKFILL = "backfill"
    SPECULATIVE_LLM = "speculative_llm"
    PREPARE_TOURNAMENT = "prepare_tournament"
    RESULT = "result"
    ISSUE = "issue"
    RECOVERY = "recovery"
    RECEIPT_LOOKUP = "receipt_lookup"
    SUPPORT_EXPORT = "support_export"


@dataclass(frozen=True, slots=True)
class DiskAdmission:
    tier: DiskTier
    allowed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class DiskReservePolicy:
    warning_free_bytes: int
    speculative_free_bytes: int
    critical_free_bytes: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.warning_free_bytes, bool)
            or isinstance(self.critical_free_bytes, bool)
            or not isinstance(self.warning_free_bytes, int)
            or isinstance(self.speculative_free_bytes, bool)
            or not isinstance(self.speculative_free_bytes, int)
            or not isinstance(self.critical_free_bytes, int)
            or self.warning_free_bytes <= self.speculative_free_bytes
            or self.speculative_free_bytes <= self.critical_free_bytes
            or self.critical_free_bytes <= 0
        ):
            raise BackupError("disk reserve thresholds must be positive and strictly ordered")

    def admit(
        self, free_bytes: int, operation: DiskOperation, *, tournament_open: bool
    ) -> DiskAdmission:
        if isinstance(free_bytes, bool) or not isinstance(free_bytes, int) or free_bytes < 0:
            raise BackupError("free disk bytes must be a non-negative integer")
        if not isinstance(operation, DiskOperation) or not isinstance(tournament_open, bool):
            raise BackupError("disk admission requires typed operation and tournament state")
        if free_bytes >= self.warning_free_bytes:
            return DiskAdmission(DiskTier.NORMAL, True, "capacity_available")
        if free_bytes >= self.speculative_free_bytes:
            allowed = operation not in {DiskOperation.FACTORY, DiskOperation.BACKFILL}
            return DiskAdmission(
                DiskTier.WARNING,
                allowed,
                "warning_reserve_preserves_live_work" if allowed else "maintenance_suspended",
            )
        if free_bytes >= self.critical_free_bytes:
            allowed = operation not in {
                DiskOperation.FACTORY,
                DiskOperation.BACKFILL,
                DiskOperation.SPECULATIVE_LLM,
            }
            return DiskAdmission(
                DiskTier.SPECULATIVE_SUSPENDED,
                allowed,
                "speculative_work_suspended" if not allowed else "live_work_reserved",
            )
        critical_lane = {
            DiskOperation.RESULT,
            DiskOperation.ISSUE,
            DiskOperation.RECOVERY,
            DiskOperation.RECEIPT_LOOKUP,
            DiskOperation.SUPPORT_EXPORT,
        }
        allowed = tournament_open and operation in critical_lane
        return DiskAdmission(
            DiskTier.CRITICAL,
            allowed,
            "open_tournament_critical_lane" if allowed else "critical_reserve_blocks_admission",
        )


def _read_manifest(path: Path, expected_kind: str) -> SignedManifest:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        manifest = SignedManifest.from_dict(value)
    except Exception as exc:
        raise BackupIntegrityError("signed backup manifest cannot be decoded") from exc
    if manifest.kind != expected_kind:
        raise BackupIntegrityError("signed backup manifest has the wrong kind")
    return manifest


def _encode_archive_package(
    manifest: Mapping[str, Any], entries: tuple[tuple[str, bytes], ...]
) -> bytes:
    manifest_bytes = canonical_bytes(
        manifest,
        max_bytes=_MAX_ARCHIVE_MANIFEST_BYTES,
        max_items=2_000_000,
    )
    material = (("000-manifest.json", manifest_bytes), *entries)
    encoded = bytearray(_ARCHIVE_MAGIC)
    encoded.extend(struct.pack(">Q", len(material)))
    previous = ""
    for name, payload in material:
        if (
            not isinstance(name, str)
            or _ARCHIVE_ENTRY_NAME.fullmatch(name) is None
            or name <= previous
        ):
            raise ArchiveEligibilityError("archive package entry names must be safe and sorted")
        if not isinstance(payload, bytes):
            raise ArchiveEligibilityError("archive package entries must be immutable bytes")
        name_bytes = name.encode("utf-8")
        encoded.extend(struct.pack(">IQ", len(name_bytes), len(payload)))
        encoded.extend(name_bytes)
        encoded.extend(payload)
        previous = name
        if len(encoded) > _MAX_ARCHIVE_PACKAGE_BYTES:
            raise ArchiveEligibilityError("archive package exceeds the declared maximum")
    return bytes(encoded)


def _decode_archive_package(payload: bytes) -> tuple[dict[str, Any], dict[str, bytes]]:
    if not isinstance(payload, bytes) or not payload.startswith(_ARCHIVE_MAGIC):
        raise BackupIntegrityError("archive package magic differs")
    if len(payload) > _MAX_ARCHIVE_PACKAGE_BYTES:
        raise BackupIntegrityError("archive package exceeds the declared maximum")
    offset = len(_ARCHIVE_MAGIC)
    if len(payload) < offset + 8:
        raise BackupIntegrityError("archive package header is truncated")
    count = struct.unpack_from(">Q", payload, offset)[0]
    offset += 8
    if not 1 <= count <= 1_000_000:
        raise BackupIntegrityError("archive package entry count is invalid")
    entries: dict[str, bytes] = {}
    previous = ""
    for _index in range(count):
        if len(payload) < offset + 12:
            raise BackupIntegrityError("archive package entry header is truncated")
        name_length, content_length = struct.unpack_from(">IQ", payload, offset)
        offset += 12
        end_name = offset + name_length
        end_content = end_name + content_length
        if name_length > 512 or end_content > len(payload):
            raise BackupIntegrityError("archive package entry is truncated or oversized")
        try:
            name = payload[offset:end_name].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BackupIntegrityError("archive package entry name is not UTF-8") from exc
        if _ARCHIVE_ENTRY_NAME.fullmatch(name) is None or name <= previous:
            raise BackupIntegrityError("archive package entry names are unsafe or unsorted")
        entries[name] = payload[end_name:end_content]
        offset = end_content
        previous = name
    if offset != len(payload):
        raise BackupIntegrityError("archive package has unexplained trailing bytes")
    manifest_bytes = entries.pop("000-manifest.json", None)
    if manifest_bytes is None:
        raise BackupIntegrityError("archive package omits its canonical manifest")
    try:
        manifest = json.loads(manifest_bytes)
    except Exception as exc:
        raise BackupIntegrityError("archive package manifest is invalid JSON") from exc
    if (
        not isinstance(manifest, dict)
        or canonical_bytes(
            manifest,
            max_bytes=_MAX_ARCHIVE_MANIFEST_BYTES,
            max_items=2_000_000,
        )
        != manifest_bytes
    ):
        raise BackupIntegrityError("archive package manifest is not canonical")
    index = manifest.get("entries")
    if not isinstance(index, list) or len(index) != len(entries):
        raise BackupIntegrityError("archive package entry index is incomplete")
    for item in index:
        if not isinstance(item, dict) or set(item) != {"name", "byte_count", "digest"}:
            raise BackupIntegrityError("archive package entry index is malformed")
        content = entries.get(item["name"])
        if (
            content is None
            or len(content) != item["byte_count"]
            or hashlib.sha256(content).hexdigest() != item["digest"]
        ):
            raise BackupIntegrityError("archive package entry differs from its manifest")
    return manifest, entries


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1_048_576):
                digest.update(chunk)
    except OSError as exc:
        raise BackupIntegrityError("required backup/archive file cannot be read") from exc
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("rb+") as handle:
        os.fsync(handle.fileno())


def _durable_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _durable_replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _durable_replace(source: Path, destination: Path) -> None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        move_file = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move_file.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
        move_file.restype = wintypes.BOOL
        if not move_file(str(source), str(destination), 0x1 | 0x8):
            raise BackupError(
                f"Windows write-through backup rename failed ({ctypes.get_last_error()})"
            )
        return
    os.replace(source, destination)
    descriptor = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_restore_database(source: Path, destination: Path) -> None:
    """Atomically activate a verified restore without replacing any existing authority."""

    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        move_file = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move_file.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
        move_file.restype = wintypes.BOOL
        if move_file(str(source), str(destination), 0x8):
            return
        error = ctypes.get_last_error()
        if error in {80, 183}:
            raise BackupError("restore destination already exists")
        raise BackupError(f"Windows restore activation failed ({error})")
    try:
        os.link(source, destination)
    except FileExistsError as exc:
        raise BackupError("restore destination already exists") from exc
    descriptor = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_generation(staging: Path, destination: Path) -> None:
    """Atomically publish one immutable, self-contained backup generation."""

    if destination.exists():
        raise BackupError("backup generation destination already exists")
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        move_file = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move_file.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
        move_file.restype = wintypes.BOOL
        if not move_file(str(staging), str(destination), 0x8):
            error = ctypes.get_last_error()
            if error in {80, 183}:
                raise BackupError("backup generation destination already exists")
            raise BackupError(f"Windows backup generation publication failed ({error})")
        return
    try:
        os.rename(staging, destination)
    except FileExistsError as exc:
        raise BackupError("backup generation destination already exists") from exc
    descriptor = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fault(hook: Callable[[str], None] | None, stage: str) -> None:
    if hook is not None:
        hook(stage)


def _require_token(value: object, label: str) -> str:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise BackupError(f"{label} must be a bounded opaque token")
    return value


def _require_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise BackupError(f"{label} must be a lower-case SHA-256 digest")
    return value


__all__ = [
    "ArchiveCopy",
    "ArchiveEligibilityError",
    "ArchiveManager",
    "ArchiveProof",
    "attest_remote_archive_copy",
    "BackupArtifact",
    "BackupError",
    "BackupIntegrityError",
    "BackupManager",
    "DiskAdmission",
    "DiskOperation",
    "DiskReservePolicy",
    "DiskTier",
    "IssueExpectation",
    "RemoteAttesterAuthorization",
    "RemoteAttesterPolicy",
    "RestoreReport",
]
