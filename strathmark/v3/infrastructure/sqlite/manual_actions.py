"""SQLite persistence and transaction-time CAS for manual-action requirements."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from strathmark.v3.application.manual_actions import (
    ManualActionBinding,
    ManualActionConflict,
    ManualActionRequirement,
    ManualActionResolution,
    create_manual_action_resolution,
)
from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.identifiers import StableIdentifier, require_identifier
from strathmark.v3.infrastructure.integrity import IntegrityTrustStore, P256Signer
from strathmark.v3.infrastructure.sqlite.connection import (
    immediate_transaction,
    open_v3_connection,
)
from strathmark.v3.infrastructure.sqlite.migrations import migrate_connection

_CURRENT_SCHEMA = "strathmark-v3-manual-action-current-v1"


class SQLiteManualActionRequirementStore:
    """Durable requirement history with a small rebuildable current pointer."""

    def __init__(
        self,
        database_path: Path | str,
        *,
        signer: P256Signer,
        trust_store: IntegrityTrustStore,
    ) -> None:
        if not isinstance(trust_store, IntegrityTrustStore) or not callable(
            getattr(signer, "sign", None)
        ):
            raise ManualActionConflict(
                "manual-action store requires signer and trust authority"
            )
        trust_store.identity(signer.identity.key_id)
        self._database_path = Path(database_path)
        self._signer = signer
        self._trust_store = trust_store
        with open_v3_connection(self._database_path) as connection:
            migrate_connection(connection)

    @property
    def database_path(self) -> Path:
        return self._database_path

    def publish(self, requirement: ManualActionRequirement) -> ManualActionRequirement:
        if not isinstance(requirement, ManualActionRequirement):
            raise ManualActionConflict("manual-action publication must be typed")
        requirement.verify(self._trust_store)
        with open_v3_connection(self._database_path) as connection:
            with immediate_transaction(connection):
                return self.publish_connection(connection, requirement)

    def publish_connection(
        self,
        connection: sqlite3.Connection,
        requirement: ManualActionRequirement,
    ) -> ManualActionRequirement:
        if not connection.in_transaction:
            raise ManualActionConflict(
                "manual-action publication requires a writer transaction"
            )
        if not isinstance(requirement, ManualActionRequirement):
            raise ManualActionConflict("manual-action publication must be typed")
        requirement.verify(self._trust_store)
        stored = connection.execute(
            "SELECT requirement_json,requirement_manifest_json FROM "
            "v3_manual_action_requirements WHERE requirement_digest=?",
            (requirement.requirement_digest,),
        ).fetchone()
        current = connection.execute(
            "SELECT requirement_digest,upstream_field_revision,current_digest,updated_at "
            "FROM v3_manual_action_current WHERE field_id=?",
            (str(requirement.field_id),),
        ).fetchone()
        resolved = connection.execute(
            "SELECT 1 FROM v3_manual_action_resolutions WHERE requirement_digest=?",
            (requirement.requirement_digest,),
        ).fetchone()
        if resolved is not None:
            raise ManualActionConflict("manual-action requirement is already resolved")
        if stored is not None:
            observed = self._decode_requirement(stored[0], stored[1])
            if (
                observed.content_value() != requirement.content_value()
                or observed.requirement_digest != requirement.requirement_digest
                or observed.manifest.body_digest != requirement.manifest.body_digest
            ):
                raise ManualActionConflict(
                    "stored manual-action requirement material differs"
                )
            if current is None or str(current[0]) != requirement.requirement_digest:
                raise ManualActionConflict(
                    "stored manual-action requirement is no longer current"
                )
            self._verify_current_row(requirement, requirement.field_id, current)
            return observed
        if current is not None:
            current_revision = int(current[1])
            if current_revision == requirement.upstream_field_revision:
                raise ManualActionConflict(
                    "manual action changed within the same field revision"
                )
            if current_revision > requirement.upstream_field_revision:
                raise ManualActionConflict(
                    "manual action cannot replace a newer field revision"
                )
        requirement_bytes = canonical_bytes(
            requirement.to_dict(), max_bytes=1_048_576, max_items=100_000
        ).decode("utf-8")
        manifest_bytes = canonical_bytes(
            requirement.manifest.to_dict(), max_bytes=2_097_152, max_items=10_000
        ).decode("utf-8")
        connection.execute(
            "INSERT INTO v3_manual_action_requirements VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                requirement.requirement_digest,
                str(requirement.requirement_id),
                str(requirement.field_id),
                requirement.upstream_field_revision,
                requirement.field_revision_digest,
                requirement.action.value,
                requirement.hard_deadline_at,
                requirement.created_at,
                requirement_bytes,
                manifest_bytes,
            ),
        )
        pointer_digest = _current_digest(requirement)
        if current is None:
            connection.execute(
                "INSERT INTO v3_manual_action_current VALUES (?, ?, ?, ?, ?)",
                (
                    str(requirement.field_id),
                    requirement.requirement_digest,
                    requirement.upstream_field_revision,
                    pointer_digest,
                    requirement.created_at,
                ),
            )
        else:
            changed = connection.execute(
                "UPDATE v3_manual_action_current SET requirement_digest=?,"
                "upstream_field_revision=?,current_digest=?,updated_at=? "
                "WHERE field_id=? AND requirement_digest=? AND current_digest=?",
                (
                    requirement.requirement_digest,
                    requirement.upstream_field_revision,
                    pointer_digest,
                    requirement.created_at,
                    str(requirement.field_id),
                    str(current[0]),
                    str(current[2]),
                ),
            ).rowcount
            if changed != 1:
                raise ManualActionConflict(
                    "manual-action current pointer changed concurrently"
                )
        return requirement

    def current(
        self, field_id: str | StableIdentifier
    ) -> ManualActionRequirement | None:
        field = require_identifier(field_id, expected_namespace="field")
        with open_v3_connection(self._database_path, read_only=True) as connection:
            return self._current_connection(connection, field)

    def require_current(
        self, binding: ManualActionBinding
    ) -> ManualActionRequirement:
        if not isinstance(binding, ManualActionBinding):
            raise ManualActionConflict("manual-action current check requires a binding")
        with open_v3_connection(self._database_path, read_only=True) as connection:
            return self._require_current_connection(connection, binding)

    def require_current_connection(
        self,
        connection: sqlite3.Connection,
        binding: ManualActionBinding,
    ) -> ManualActionRequirement:
        """CAS one requirement from inside the caller's existing writer transaction."""

        if not connection.in_transaction:
            raise ManualActionConflict(
                "manual-action commit check requires a writer transaction"
            )
        return self._require_current_connection(connection, binding)

    def resolve(
        self,
        binding: ManualActionBinding,
        *,
        receipt_id: str | StableIdentifier,
        receipt_digest: str,
        actor_id: str | StableIdentifier,
        resolved_at: str,
    ) -> ManualActionResolution:
        existing = self._resolution(binding.requirement_digest)
        if existing is not None:
            candidate_requirement = self._requirement_by_digest(
                binding.requirement_digest
            )
            candidate = create_manual_action_resolution(
                candidate_requirement,
                receipt_id=receipt_id,
                receipt_digest=receipt_digest,
                actor_id=actor_id,
                resolved_at=resolved_at,
                signer=self._signer,
            )
            if (
                existing.content_value() != candidate.content_value()
                or existing.resolution_digest != candidate.resolution_digest
            ):
                raise ManualActionConflict(
                    "manual-action requirement already has a different resolution"
                )
            return existing
        requirement = self.require_current(binding)
        resolution = create_manual_action_resolution(
            requirement,
            receipt_id=receipt_id,
            receipt_digest=receipt_digest,
            actor_id=actor_id,
            resolved_at=resolved_at,
            signer=self._signer,
        )
        with open_v3_connection(self._database_path) as connection:
            with immediate_transaction(connection):
                self.resolve_connection(connection, binding, resolution)
        return resolution

    def resolve_connection(
        self,
        connection: sqlite3.Connection,
        binding: ManualActionBinding,
        resolution: ManualActionResolution,
    ) -> None:
        """Resolve the action atomically with the caller's receipt event commit."""

        if not connection.in_transaction:
            raise ManualActionConflict(
                "manual-action resolution requires a writer transaction"
            )
        if not isinstance(resolution, ManualActionResolution):
            raise ManualActionConflict("manual-action resolution must be typed")
        resolution.verify(self._trust_store)
        existing_row = connection.execute(
            "SELECT resolution_json,resolution_manifest_json FROM "
            "v3_manual_action_resolutions WHERE requirement_digest=?",
            (binding.requirement_digest,),
        ).fetchone()
        if existing_row is not None:
            existing = self._decode_resolution(existing_row[0], existing_row[1])
            if (
                existing.content_value() != resolution.content_value()
                or existing.resolution_digest != resolution.resolution_digest
                or existing.manifest.body_digest != resolution.manifest.body_digest
            ):
                raise ManualActionConflict(
                    "manual-action requirement already has a different resolution"
                )
            return
        requirement = self._require_current_connection(connection, binding)
        if (
            resolution.requirement_id != requirement.requirement_id
            or resolution.requirement_digest != requirement.requirement_digest
            or resolution.field_id != requirement.field_id
            or resolution.resolved_at < requirement.created_at
        ):
            raise ManualActionConflict(
                "manual-action resolution differs from current requirement"
            )
        resolution_bytes = canonical_bytes(
            resolution.to_dict(), max_bytes=1_048_576, max_items=100_000
        ).decode("utf-8")
        manifest_bytes = canonical_bytes(
            resolution.manifest.to_dict(), max_bytes=2_097_152, max_items=10_000
        ).decode("utf-8")
        connection.execute(
            "INSERT INTO v3_manual_action_resolutions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                resolution.requirement_digest,
                str(resolution.receipt_id),
                resolution.receipt_digest,
                str(resolution.actor_id),
                resolution.resolved_at,
                resolution.resolution_digest,
                resolution_bytes,
                manifest_bytes,
            ),
        )
        changed = connection.execute(
            "DELETE FROM v3_manual_action_current WHERE field_id=? "
            "AND requirement_digest=? AND current_digest=?",
            (
                str(binding.field_id),
                binding.requirement_digest,
                _current_digest(requirement),
            ),
        ).rowcount
        if changed != 1:
            raise ManualActionConflict(
                "manual-action requirement changed before resolution"
            )

    def _current_connection(
        self, connection: sqlite3.Connection, field_id: StableIdentifier
    ) -> ManualActionRequirement | None:
        row = connection.execute(
            "SELECT requirement_digest,upstream_field_revision,current_digest,updated_at "
            "FROM v3_manual_action_current WHERE field_id=?",
            (str(field_id),),
        ).fetchone()
        if row is None:
            return None
        requirement = self._requirement_by_digest_connection(
            connection, str(row[0])
        )
        self._verify_current_row(requirement, field_id, row)
        if connection.execute(
            "SELECT 1 FROM v3_manual_action_resolutions WHERE requirement_digest=?",
            (requirement.requirement_digest,),
        ).fetchone():
            raise ManualActionConflict(
                "resolved manual-action requirement remains current"
            )
        return requirement

    def _require_current_connection(
        self, connection: sqlite3.Connection, binding: ManualActionBinding
    ) -> ManualActionRequirement:
        row = connection.execute(
            "SELECT requirement_digest,upstream_field_revision,current_digest,updated_at "
            "FROM v3_manual_action_current WHERE field_id=?",
            (str(binding.field_id),),
        ).fetchone()
        if row is None:
            raise ManualActionConflict("manual-action requirement is no longer current")
        requirement = self._requirement_by_digest_connection(
            connection, str(row[0])
        )
        self._verify_current_row(requirement, binding.field_id, row)
        if requirement.binding != binding:
            raise ManualActionConflict("manual-action current binding differs")
        return requirement

    def _verify_current_row(
        self,
        requirement: ManualActionRequirement,
        expected_field_id: StableIdentifier,
        row: sqlite3.Row | tuple[Any, ...],
    ) -> None:
        binding = requirement.binding
        expected = {
            "schema_version": _CURRENT_SCHEMA,
            "field_id": str(binding.field_id),
            "requirement_digest": binding.requirement_digest,
            "upstream_field_revision": binding.upstream_field_revision,
            "updated_at": requirement.created_at,
        }
        if (
            requirement.field_id != expected_field_id
            or str(row[0]) != binding.requirement_digest
            or int(row[1]) != binding.upstream_field_revision
            or str(row[3]) != requirement.created_at
            or str(row[2]) != canonical_digest(expected)
        ):
            raise ManualActionConflict("manual-action requirement is no longer current")

    def _requirement_by_digest(self, digest: str) -> ManualActionRequirement:
        with open_v3_connection(self._database_path, read_only=True) as connection:
            return self._requirement_by_digest_connection(connection, digest)

    def _requirement_by_digest_connection(
        self, connection: sqlite3.Connection, digest: str
    ) -> ManualActionRequirement:
        row = connection.execute(
            "SELECT requirement_json,requirement_manifest_json FROM "
            "v3_manual_action_requirements WHERE requirement_digest=?",
            (digest,),
        ).fetchone()
        if row is None:
            raise ManualActionConflict("manual-action requirement material is missing")
        return self._decode_requirement(row[0], row[1])

    def _resolution(self, digest: str) -> ManualActionResolution | None:
        with open_v3_connection(self._database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT resolution_json,resolution_manifest_json FROM "
                "v3_manual_action_resolutions WHERE requirement_digest=?",
                (digest,),
            ).fetchone()
        if row is None:
            return None
        return self._decode_resolution(row[0], row[1])

    def _decode_requirement(
        self, value_json: object, manifest_json: object
    ) -> ManualActionRequirement:
        try:
            value = json.loads(str(value_json))
            manifest = json.loads(str(manifest_json))
            requirement = ManualActionRequirement.from_dict(value)
        except Exception as exc:
            raise ManualActionConflict(
                "stored manual-action requirement is malformed"
            ) from exc
        if requirement.manifest.to_dict() != manifest:
            raise ManualActionConflict(
                "stored manual-action requirement manifest differs"
            )
        requirement.verify(self._trust_store)
        return requirement

    def _decode_resolution(
        self, value_json: object, manifest_json: object
    ) -> ManualActionResolution:
        try:
            value = json.loads(str(value_json))
            manifest = json.loads(str(manifest_json))
            resolution = ManualActionResolution.from_dict(value)
        except Exception as exc:
            raise ManualActionConflict(
                "stored manual-action resolution is malformed"
            ) from exc
        if resolution.manifest.to_dict() != manifest:
            raise ManualActionConflict(
                "stored manual-action resolution manifest differs"
            )
        resolution.verify(self._trust_store)
        return resolution


def _current_digest(requirement: ManualActionRequirement) -> str:
    return canonical_digest(
        {
            "schema_version": _CURRENT_SCHEMA,
            "field_id": str(requirement.field_id),
            "requirement_digest": requirement.requirement_digest,
            "upstream_field_revision": requirement.upstream_field_revision,
            "updated_at": requirement.created_at,
        }
    )


__all__ = ["SQLiteManualActionRequirementStore"]
