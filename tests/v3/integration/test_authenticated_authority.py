from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from types import SimpleNamespace

import pytest

from strathmark.v3.composition import V3ServiceIdentity, compose_v3_application_gateway
from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.commands import CommandKind
from strathmark.v3.contracts.events import EventKind
from strathmark.v3.infrastructure.event_authority import (
    AuthenticatedEventAuthority,
    bind_event_authority,
)
from strathmark.v3.infrastructure.integrity import (
    CheckpointRegistry,
    CriticalIssueCoordinator,
    CriticalJournal,
    IntegrityError,
    IntegrityTrustStore,
    P256EphemeralSigner,
)
from strathmark.v3.infrastructure.rolling_head_registry import (
    ExternalRollingHeadRegistry,
    RollingHeadRegistryError,
)
from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection
from strathmark.v3.infrastructure.sqlite.event_store import (
    EventStoreIntegrityError,
    SQLiteEventStore,
)
from strathmark.v3.infrastructure.sqlite.jobs import DurableJobRepository
from strathmark.v3.infrastructure.sqlite.projections import SQLiteProjectionStore
from strathmark.v3.infrastructure.terminal_witness import (
    WitnessBinding,
    WitnessError,
    WitnessState,
)
from tests.v3.integration.test_rolling_head_registry import _advance_local
from tests.v3.integration.test_rolling_restart_capacity import _capacity
from tests.v3.system.test_critical_issue_recovery import NOW, _field_request


class MemoryWitness:
    """Synthetic independent failure domain, never shipped as a runtime backend."""

    def __init__(self, *scopes: str) -> None:
        self.states = dict.fromkeys(scopes, WitnessState(0, b""))
        self.lock = Lock()
        self.unavailable = False
        self.fail_after_cas = False

    def read(self, scope):
        if self.unavailable:
            raise OSError("independent service offline")
        return self.states[scope]

    def compare_and_swap(self, scope, expected, proposed):
        with self.lock:
            if self.read(scope) != expected or proposed.generation != expected.generation + 1:
                return False
            self.states[scope] = proposed
            if self.fail_after_cas:
                raise OSError("reply lost after durable witness commit")
            return True


def _copy_database(source: Path, destination: Path) -> None:
    with sqlite3.connect(source) as original, sqlite3.connect(destination) as copy:
        original.backup(copy)


def _tamper(connection, sql, parameters=()):
    # A file writer can remove append-only triggers; restore their exact schema
    # afterward to prove the independent authentication is doing the detection.
    triggers = connection.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
        "AND tbl_name IN ('v3_events','v3_idempotency_records')"
    ).fetchall()
    for name, _definition in triggers:
        connection.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
    connection.execute(sql, parameters)
    for _name, definition in triggers:
        connection.execute(definition)


def _request(suffix="prepare", *, issued=False):
    return _field_request(
        f"command:{suffix}",
        CommandKind.ACKNOWLEDGE_ISSUE if issued else CommandKind.OPTIMIZE_FIELD,
        EventKind.FIELD_ISSUED if issued else EventKind.FIELD_OPTIMIZED,
        expected=1 if issued else 0,
    )


def _authority(database):
    signer = P256EphemeralSigner.generate("integrity-key:authenticated-authority")
    trust = IntegrityTrustStore((signer.identity,))
    witness = MemoryWitness("authority")
    authority = AuthenticatedEventAuthority(
        witness=WitnessBinding(witness, "authority"), signer=signer, trust_store=trust
    )
    authority.provision_empty_database(database, created_at=NOW)
    bind_event_authority(database, authority)
    return authority, witness


def test_operational_gateway_requires_independent_authority():
    with pytest.raises(TypeError, match="event_authority"):
        compose_v3_application_gateway(database_path="unused.sqlite3")
    with pytest.raises(IntegrityError, match="authenticated event authority"):
        compose_v3_application_gateway(event_authority=None, database_path="unused.sqlite3")


def test_gateway_binds_internal_services_and_exact_retries_to_witness(tmp_path):
    database = tmp_path / "gateway.sqlite3"
    authority, witness = _authority(database)
    signer, trust = authority.signer, authority.trust_store
    jobs = DurableJobRepository(database, capacity=_capacity(), signer=signer, trust_store=trust)
    gateway = compose_v3_application_gateway(
        event_authority=authority,
        database_path=database,
        signer=signer,
        trust_store=trust,
        pipeline_builder=lambda _: None,
        job_repository=jobs,
        issue_coordinator=CriticalIssueCoordinator.for_rehearsal(
            CriticalJournal(tmp_path / "journal", signer=signer, trust_store=trust)
        ),
        settlement_reactions=SimpleNamespace(database_path=database, react=lambda _: None),
        clock=lambda: NOW,
        service_identity=V3ServiceIdentity.from_installed_contract(source_commit="a" * 40),
    )
    # Lifecycle and field services create their own event-store objects. All of
    # them must inherit the binding, including objects opened after composition.
    result = gateway._services.lifecycle._events.execute(_request())
    assert witness.read("authority").generation == 2
    assert gateway._services.events.execute(_request()) == result
    assert witness.read("authority").generation == 2
    from fastapi.testclient import TestClient

    from strathmark.v3.api.app import create_v3_app
    from strathmark.v3.api.auth import InMemoryCredentialSecretStore, ServiceCredentialRegistry
    from strathmark.v3.consumer_contract import v3_consumer_contract_digest

    credentials = ServiceCredentialRegistry(
        gateway._services.events, InMemoryCredentialSecretStore()
    )
    credential = credentials.bootstrap_offline(
        principal_id="actor:manager",
        listener_stopped=True,
        credential="smv3.authority-test.authority-secret-1234567890123456789",
    )
    client = TestClient(create_v3_app(gateway=gateway, credentials=credentials))
    payload = {
        "schema_version": "strathmark-v3-snapshot-sync-request-v1",
        "entity_kind": "tournament",
        "entity_id": "tournament:authenticated",
        "upstream_revision": 1,
        "tournament_id": "tournament:authenticated",
        "round_id": None,
        "synchronized_at_utc": NOW,
        "deadline_ms": 10_000,
        "snapshot": {"bundle_id": "bundle:synthetic", "historical_cutoff_key": "history:synthetic"},
        "engine_selection": {
            "schema_version": "strathmark-v3-competition-engine-selection-v1",
            "scope_id": "tournament:authenticated",
            "engine": "v3",
            "mode": "rehearsal",
            "selected_by_actor_id": "actor:manager",
            "selected_at_utc": NOW,
            "reason_code": "security_regression",
            "source_commit": "a" * 40,
            "consumer_contract_digest": v3_consumer_contract_digest(),
        },
    }
    before_http = witness.read("authority").generation
    response = client.post(
        "/v3/snapshots/synchronize",
        json=payload,
        headers={
            "Authorization": f"Bearer {credential.credential}",
            "Idempotency-Key": "snapshot-security",
        },
    )
    assert response.status_code == 200, response.text
    assert witness.read("authority").generation == before_http + 1
    witness.unavailable = True
    with pytest.raises((EventStoreIntegrityError, WitnessError)):
        gateway._services.events.execute(_request())
    with pytest.raises(EventStoreIntegrityError):
        SQLiteEventStore(database)


def test_forged_legal_suffix_after_signed_prefix_is_rejected(tmp_path):
    database = tmp_path / "authority.sqlite3"
    _authority(database)
    store = SQLiteEventStore(database)
    store.execute(_request())
    attacker = tmp_path / "attacker.sqlite3"
    _copy_database(database, attacker)
    # The attacker knows all public hashes and can create a structurally legal
    # suffix in an untrusted low-level forensic copy without the witness key.
    SQLiteEventStore(attacker).execute(_request("forged-issue", issued=True))
    _copy_database(attacker, database)
    with pytest.raises(EventStoreIntegrityError, match="authority"):
        store.verify()
    with pytest.raises(EventStoreIntegrityError):
        store.execute(_request("forged-issue", issued=True))


def test_recomputed_result_digest_and_coherent_database_rollback_are_rejected(tmp_path):
    database = tmp_path / "authority.sqlite3"
    _authority(database)
    store = SQLiteEventStore(database)
    store.execute(_request())
    prior = tmp_path / "prior.sqlite3"
    _copy_database(database, prior)
    forged = {"receipt_id": "receipt:attacker"}
    with open_v3_connection(database) as connection:
        _tamper(
            connection,
            "UPDATE v3_idempotency_records SET result_json=?,result_digest=?",
            (canonical_bytes(forged).decode(), canonical_digest(forged)),
        )
    with pytest.raises(EventStoreIntegrityError):
        store.execute(_request())
    _copy_database(prior, database)
    store.execute(_request("real-issue", issued=True))
    _copy_database(prior, database)
    with pytest.raises(EventStoreIntegrityError):
        SQLiteEventStore(database)


def test_uncertain_witness_commit_rolls_back_sqlite_and_requires_recovery(tmp_path):
    database = tmp_path / "authority.sqlite3"
    _auth, witness = _authority(database)
    store = SQLiteEventStore(database)
    witness.fail_after_cas = True
    with pytest.raises(WitnessError, match="uncertain"):
        store.execute(_request())
    with open_v3_connection(database, read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM v3_events").fetchone()[0] == 0
    assert witness.read("authority").generation == 2
    with pytest.raises(EventStoreIntegrityError):
        SQLiteEventStore(database)


def test_existing_legacy_history_is_never_automatically_adopted(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    SQLiteEventStore(database).execute(_request())
    with pytest.raises(IntegrityError, match="cannot be automatically trusted"):
        _authority(database)
    assert SQLiteEventStore(database).event_count() == 1


def test_reaction_rebuild_rechecks_under_lock_before_any_deletion(tmp_path, monkeypatch):
    import strathmark.v3.infrastructure.sqlite.projections as module

    database = tmp_path / "authority.sqlite3"
    _authority(database)
    from strathmark.v3.application.lifecycle import LifecycleService, SnapshotKind, UpstreamSnapshot
    from strathmark.v3.contracts.identifiers import IdempotencyKey, StableIdentifier

    tournament = StableIdentifier("tournament:preserve-reaction-view")
    LifecycleService(database).ingest_snapshot(
        UpstreamSnapshot(
            SnapshotKind.TOURNAMENT,
            tournament,
            1,
            tournament,
            None,
            {"bundle_id": "bundle:synthetic", "historical_cutoff_key": "history:synthetic"},
        ),
        command_id=IdempotencyKey("command:preserve-reaction-view"),
        actor_id=StableIdentifier("actor:manager"),
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=1,
    )
    projection = SQLiteProjectionStore(database)
    with open_v3_connection(database, read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM v3_ingress_snapshots").fetchone()[0] == 1
        before = projection.projection_digest(connection)
    original = module.immediate_transaction

    @contextmanager
    def tamper_before_writer_lock(connection):
        with open_v3_connection(database) as attacker:
            _tamper(attacker, "UPDATE v3_events SET envelope_json='{}'")
        with original(connection):
            yield connection

    monkeypatch.setattr(module, "immediate_transaction", tamper_before_writer_lock)
    with pytest.raises(EventStoreIntegrityError):
        projection.rebuild_reaction_projection()
    with open_v3_connection(database, read_only=True) as connection:
        assert projection.projection_digest(connection) == before


def test_checkpoint_and_rotation_tail_deletion_fail_against_independent_witness(tmp_path):
    signer = P256EphemeralSigner.generate("integrity-key:registry")
    witness = MemoryWitness("registry/rotations", "registry/checkpoints")
    binding = WitnessBinding(witness, "registry")
    database = tmp_path / "authority.sqlite3"
    store = SQLiteEventStore(database)
    root = tmp_path / "registry"
    registry = CheckpointRegistry(root, bootstrap_identity=signer.identity, witness=binding)
    registry.create_checkpoint(database, signer=signer, created_at=NOW)
    prior = tmp_path / "prior.sqlite3"
    _copy_database(database, prior)
    store.execute(_request())
    registry.create_checkpoint(database, signer=signer, created_at=NOW)
    final_checkpoint = root / "checkpoints" / "0000000000000002.json"
    retained = final_checkpoint.read_bytes()
    final_checkpoint.unlink()
    _copy_database(prior, database)
    with pytest.raises(IntegrityError, match="terminal witness"):
        CheckpointRegistry(root, bootstrap_identity=signer.identity, witness=binding)
    with pytest.raises(IntegrityError, match="terminal witness"):
        registry.verify_database(database, require_current=True)
    final_checkpoint.write_bytes(retained)
    next_signer = P256EphemeralSigner.generate("integrity-key:next")
    registry.rotate_key(signer, next_signer.identity, created_at=NOW)
    (root / "rotations" / "0000000000000001.json").unlink()
    with pytest.raises(IntegrityError, match="terminal witness"):
        registry.rotate_key(signer, signer.identity, created_at=NOW)


def test_rolling_registry_tail_and_database_rollback_fail_together(tmp_path):
    signer = P256EphemeralSigner.generate("integrity-key:rolling")
    trust = IntegrityTrustStore((signer.identity,))
    witness = MemoryWitness("rolling")
    root = tmp_path / "rolling-registry"
    config = dict(
        bootstrap_identity=signer.identity,
        trust_store=trust,
        active_identity=signer.identity,
        signer=signer,
        witness=WitnessBinding(witness, "rolling"),
        refresh_threshold=1,
    )
    registry = ExternalRollingHeadRegistry(root, **config)
    database = tmp_path / "rolling.sqlite3"
    repository = registry.open_repository(
        database, capacity=_capacity(), signer=signer, trust_store=trust, published_at=NOW
    )
    prior = tmp_path / "prior.sqlite3"
    _copy_database(database, prior)
    _advance_local(repository, 1)
    registry.refresh_if_due(
        database, capacity=_capacity(), signer=signer, trust_store=trust, published_at=NOW
    )
    (root / "heads" / "0000000000000002.json").unlink()
    _copy_database(prior, database)
    with pytest.raises(RollingHeadRegistryError, match="terminal witness"):
        ExternalRollingHeadRegistry(root, **config)
    with pytest.raises(RollingHeadRegistryError, match="terminal witness"):
        registry.open_repository(
            database, capacity=_capacity(), signer=signer, trust_store=trust, published_at=NOW
        )


def test_missing_witness_and_forged_witness_signature_fail_closed(tmp_path):
    signer = P256EphemeralSigner.generate("integrity-key:missing-witness")
    with pytest.raises(IntegrityError, match="independent terminal witness"):
        CheckpointRegistry(tmp_path / "registry", bootstrap_identity=signer.identity)
    database = tmp_path / "authority.sqlite3"
    _auth, witness = _authority(database)
    state = witness.read("authority")
    manifest = json.loads(state.value)
    manifest["body_digest"] = "0" * 64
    witness.states["authority"] = WitnessState(state.generation, canonical_bytes(manifest))
    with pytest.raises(EventStoreIntegrityError):
        SQLiteEventStore(database)


def test_unbound_event_writes_cannot_accidentally_select_rehearsal(tmp_path, monkeypatch):
    import strathmark.v3.infrastructure.event_authority as module

    monkeypatch.setattr(module, "_rehearsal_roots", set())
    database = tmp_path / "unbound.sqlite3"
    store = SQLiteEventStore(database)
    with pytest.raises(IntegrityError, match="authenticated authority or explicit rehearsal"):
        store.execute(_request())
    assert store.event_count() == 0


def test_database_commit_failure_after_witness_advance_never_acknowledges_or_resets(
    tmp_path, monkeypatch
):
    import strathmark.v3.infrastructure.sqlite.event_store as module

    database = tmp_path / "commit-failure.sqlite3"
    _auth, witness = _authority(database)
    store = SQLiteEventStore(database)
    original = module.immediate_transaction

    @contextmanager
    def fail_sqlite_commit(connection):
        with original(connection):
            yield connection
            raise sqlite3.OperationalError("injected commit failure after witness advance")

    monkeypatch.setattr(module, "immediate_transaction", fail_sqlite_commit)
    with pytest.raises(sqlite3.OperationalError, match="commit failure"):
        store.execute(_request())
    assert witness.read("authority").generation == 2
    with open_v3_connection(database, read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM v3_events").fetchone()[0] == 0
    monkeypatch.setattr(module, "immediate_transaction", original)
    with pytest.raises(EventStoreIntegrityError):
        SQLiteEventStore(database)
    assert witness.read("authority").generation == 2


def test_reviewed_migration_requires_exact_independent_facts_and_preserves_database(tmp_path):
    from strathmark.v3.infrastructure.event_authority import _facts

    database = tmp_path / "legacy-reviewed.sqlite3"
    SQLiteEventStore(database).execute(_request())
    # The fixture captures known-good facts before simulating operator review;
    # production values must come from independently authenticated evidence.
    with open_v3_connection(database, read_only=True) as connection:
        reviewed = _facts(connection)
    before = database.read_bytes()
    signer = P256EphemeralSigner.generate("integrity-key:reviewed-migration")
    witness = MemoryWitness("reviewed")
    authority = AuthenticatedEventAuthority(
        witness=WitnessBinding(witness, "reviewed"),
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
    )
    arguments = {
        "expected_global_sequence": reviewed["global_sequence"],
        "expected_event_digest": reviewed["event_digest"],
        "expected_command_results_digest": reviewed["command_results_digest"],
        "created_at": NOW,
    }
    with pytest.raises(IntegrityError, match="independently reviewed baseline"):
        authority.provision_reviewed_database(
            database, **{**arguments, "expected_command_results_digest": "f" * 64}
        )
    assert witness.read("reviewed") == WitnessState(0, b"")
    authority.provision_reviewed_database(database, **arguments)
    assert database.read_bytes() == before
    bind_event_authority(database, authority)
    SQLiteEventStore(database).verify()
    with pytest.raises(WitnessError, match="terminal witness differs"):
        authority.provision_reviewed_database(database, **arguments)
    assert witness.read("reviewed").generation == 1


def test_checkpoint_open_cannot_accept_a_suffix_added_after_checkpoint_verification(tmp_path):
    database = tmp_path / "checkpoint-open.sqlite3"
    writer = SQLiteEventStore(database)
    writer.execute(_request())
    signer = P256EphemeralSigner.generate("integrity-key:checkpoint-open")
    witness = MemoryWitness("checkpoint-open/rotations", "checkpoint-open/checkpoints")
    registry = CheckpointRegistry(
        tmp_path / "registry",
        bootstrap_identity=signer.identity,
        witness=WitnessBinding(witness, "checkpoint-open"),
    )
    registry.create_checkpoint(database, signer=signer, created_at=NOW)
    reader = SQLiteEventStore.from_checkpoint_registry(database, registry)
    writer.execute(_request("unsigned-suffix", issued=True))
    with pytest.raises(EventStoreIntegrityError, match="exact current checkpoint"):
        reader.events()
    with pytest.raises(IntegrityError, match="current signed checkpoint"):
        SQLiteEventStore.from_checkpoint_registry(database, registry)
