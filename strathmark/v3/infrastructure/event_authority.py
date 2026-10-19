"""Independently authenticated current event and command-result authority.

The external witness retains the signed current state. No database column, local
counter, filename, or signature from an older prefix can replace that state.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from threading import RLock

from strathmark.v3.contracts.canonical import canonical_bytes
from strathmark.v3.infrastructure.integrity import (
    IntegrityError,
    IntegrityKeyClass,
    IntegrityTrustStore,
    P256Signer,
    SignedManifest,
    sign_manifest,
    verify_manifest,
)
from strathmark.v3.infrastructure.terminal_witness import WitnessBinding, WitnessState

ZERO_DIGEST = "0" * 64
MANIFEST_KIND = "event_command_authority"
_bindings: dict[Path, AuthenticatedEventAuthority] = {}
_binding_lock = RLock()
_rehearsal_roots: set[Path] = set()


def _facts(connection: sqlite3.Connection) -> dict[str, object]:
    tip = connection.execute(
        "SELECT global_sequence,event_digest FROM v3_events ORDER BY global_sequence DESC LIMIT 1"
    ).fetchone()
    # Stream length-delimited canonical rows, including exact result bytes and
    # principal identity. Recomputing SQLite's unkeyed result digest is insufficient.
    digest = hashlib.sha256()
    for row in connection.execute(
        "SELECT principal_id,idempotency_key,command_digest,result_schema_version,"
        "result_json,result_digest,first_global_sequence,last_global_sequence,"
        "event_set_digest,created_at FROM v3_idempotency_records "
        "ORDER BY principal_id,idempotency_key"
    ):
        encoded = canonical_bytes(list(row), max_bytes=2_097_152)
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return {
        "global_sequence": 0 if tip is None else int(tip[0]),
        "event_digest": ZERO_DIGEST if tip is None else str(tip[1]),
        "command_results_digest": digest.hexdigest(),
    }


class AuthenticatedEventAuthority:
    """Sign under the writer lock and witness before acknowledging a DB commit.

    If the DB commit subsequently fails, the witness is deliberately ahead:
    reopening fails closed. An operator must recover the exact acknowledged
    generation from independent evidence, never reset or silently adopt the DB.
    """

    def __init__(
        self, *, witness: WitnessBinding, signer: P256Signer, trust_store: IntegrityTrustStore
    ) -> None:
        if not isinstance(witness, WitnessBinding):
            raise IntegrityError("event authority requires an independent terminal witness")
        if not isinstance(trust_store, IntegrityTrustStore):
            raise IntegrityError("event authority requires independently pinned signer trust")
        if trust_store.identity(signer.identity.key_id) != signer.identity:
            raise IntegrityError("event authority signer differs from provisioned trust")
        self.witness = witness
        self.signer = signer
        self.trust_store = trust_store

    def _signed_state(
        self, connection: sqlite3.Connection, previous: WitnessState, *, created_at: str
    ) -> WitnessState:
        manifest = sign_manifest(
            MANIFEST_KIND,
            {
                "scope": self.witness.scope,
                "generation": previous.generation + 1,
                "previous_state_digest": previous.digest,
                **_facts(connection),
            },
            signer=self.signer,
            created_at=created_at,
        )
        return WitnessState(previous.generation + 1, canonical_bytes(manifest.to_dict()))

    def provision_empty_database(self, database_path: Path | str, *, created_at: str) -> None:
        """Explicit genesis only; existing histories require independent review."""
        from strathmark.v3.infrastructure.sqlite.connection import (
            immediate_transaction,
            open_v3_connection,
        )
        from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore

        store = SQLiteEventStore(database_path)
        with open_v3_connection(store.database_path) as connection:
            with immediate_transaction(connection):
                store._verify_connection(connection)
                if connection.execute("SELECT 1 FROM v3_events LIMIT 1").fetchone() is not None:
                    raise IntegrityError("existing event history cannot be automatically trusted")
                previous = WitnessState(0, b"")
                self.witness.require(previous)
                self.witness.advance(
                    previous, self._signed_state(connection, previous, created_at=created_at)
                )

    def verify_connection(self, connection: sqlite3.Connection) -> WitnessState:
        current = self.witness.read()
        if current.generation == 0:
            raise IntegrityError("event authority has not been explicitly provisioned")
        try:
            manifest = SignedManifest.from_dict(json.loads(current.value))
            if (
                canonical_bytes(manifest.to_dict()) != current.value
                or manifest.kind != MANIFEST_KIND
            ):
                raise IntegrityError("event authority witness manifest differs")
            payload = verify_manifest(manifest, self.trust_store)
        except Exception as exc:
            raise IntegrityError(
                "event authority witness is not independently authenticated"
            ) from exc
        if (
            set(payload)
            != {
                "scope",
                "generation",
                "previous_state_digest",
                "global_sequence",
                "event_digest",
                "command_results_digest",
            }
            or payload["scope"] != self.witness.scope
            or payload["generation"] != current.generation
            or any(payload[key] != value for key, value in _facts(connection).items())
        ):
            raise IntegrityError("database differs from current authenticated event authority")
        return current

    def provision_reviewed_database(
        self,
        database_path: Path | str,
        *,
        expected_global_sequence: int,
        expected_event_digest: str,
        expected_command_results_digest: str,
        created_at: str,
    ) -> None:
        """Explicit offline migration using independently reviewed exact facts.

        These expected values are trust inputs, not values to discover from the
        database being adopted. This method is never called by runtime startup.
        A current/past witness generation cannot be replaced or reset here.
        """
        from strathmark.v3.infrastructure.sqlite.connection import (
            immediate_transaction,
            open_v3_connection,
        )
        from strathmark.v3.infrastructure.sqlite.event_store import (
            AuthorityAnchor,
            SQLiteEventStore,
        )
        from strathmark.v3.infrastructure.sqlite.migrations import (
            EXPECTED_SCHEMA_DIGEST,
            canonical_schema_digest,
        )

        anchor = AuthorityAnchor(expected_global_sequence, expected_event_digest)
        expected = {
            "global_sequence": anchor.global_sequence,
            "event_digest": anchor.event_digest,
            "command_results_digest": expected_command_results_digest,
        }
        # Deliberately avoid constructor migration/checkpoint writes to legacy
        # evidence. Replay against the supplied anchor under one writer lock.
        store = object.__new__(SQLiteEventStore)
        store._database_path = Path(database_path).expanduser().resolve(strict=True)
        store._trusted_anchor = anchor
        with open_v3_connection(store.database_path) as connection:
            with immediate_transaction(connection):
                if canonical_schema_digest(connection) != EXPECTED_SCHEMA_DIGEST:
                    raise IntegrityError(
                        "reviewed baseline requires exact current schema without migration"
                    )
                store._verify_connection(connection)
                if _facts(connection) != expected:
                    raise IntegrityError(
                        "legacy database differs from independently reviewed baseline"
                    )
                previous = WitnessState(0, b"")
                self.witness.require(previous)
                self.witness.advance(
                    previous, self._signed_state(connection, previous, created_at=created_at)
                )

    def advance_connection(
        self, connection: sqlite3.Connection, previous: WitnessState, *, created_at: str
    ) -> None:
        self.witness.advance(
            previous, self._signed_state(connection, previous, created_at=created_at)
        )


def bind_event_authority(database_path: Path | str, authority: AuthenticatedEventAuthority) -> None:
    """Bind every internal event-store caller on this DB before gateway creation."""
    from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection

    if not isinstance(authority, AuthenticatedEventAuthority):
        raise IntegrityError("operational gateway requires authenticated event authority")
    database = Path(database_path).expanduser().resolve(strict=False)
    with _binding_lock:
        existing = _bindings.get(database)
        if existing is not None and existing is not authority:
            raise IntegrityError("database already binds a different event authority")
        with open_v3_connection(database, read_only=True) as connection:
            connection.execute("BEGIN")
            authority.verify_connection(connection)
        _bindings[database] = authority


def event_authority_for(database_path: Path) -> AuthenticatedEventAuthority | None:
    with _binding_lock:
        return _bindings.get(database_path)


def enable_unwitnessed_rehearsal(root: Path | str, *, signer: P256Signer) -> None:
    """Explicit synthetic-only scope, never selected by operational composition.

    No environment variable or discovered local file enables this exception.
    The caller must supply a development signer and select an isolated root.
    """
    if signer.identity.key_class is not IntegrityKeyClass.DEVELOPMENT_EPHEMERAL:
        raise IntegrityError("unwitnessed rehearsal requires an ephemeral development signer")
    path = Path(root).expanduser().resolve(strict=False)
    if path == Path(path.anchor) or path == Path.home():
        raise IntegrityError("unwitnessed rehearsal requires a narrow isolated root")
    with _binding_lock:
        _rehearsal_roots.add(path)


def is_unwitnessed_rehearsal(database_path: Path) -> bool:
    with _binding_lock:
        return any(database_path.is_relative_to(root) for root in _rehearsal_roots)


def require_event_authority(database_path: Path) -> AuthenticatedEventAuthority | None:
    authority = event_authority_for(database_path)
    if authority is None and not is_unwitnessed_rehearsal(database_path):
        raise IntegrityError("event writes require authenticated authority or explicit rehearsal")
    return authority
