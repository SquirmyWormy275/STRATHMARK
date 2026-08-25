from __future__ import annotations

import hashlib
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import strathmark.v3.api.auth as auth_module
from strathmark.v3.api.auth import (
    CredentialError,
    InMemoryCredentialSecretStore,
    ServiceCredentialRegistry,
    ServicePrincipal,
    WindowsCredentialSecretStore,
)
from strathmark.v3.contracts.events import EventKind
from strathmark.v3.contracts.identifiers import StableIdentifier
from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: int) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def credentials(tmp_path: Path):
    clock = Clock()
    events = SQLiteEventStore(tmp_path / "credentials.sqlite3")
    secrets = InMemoryCredentialSecretStore()
    registry = ServiceCredentialRegistry(events, secrets, clock=clock)
    return registry, events, secrets, clock


def test_offline_bootstrap_authenticates_without_persisting_secret(credentials) -> None:
    registry, events, secrets, _clock = credentials

    issued = registry.bootstrap_offline(
        principal_id="actor:tournament-manager",
        listener_stopped=True,
        credential="smv3.bootstrap-key.bootstrap-secret-1234567890",
    )

    principal = registry.authenticate(f"Bearer {issued.credential}")
    assert str(principal.principal_id) == "actor:tournament-manager"
    assert str(principal.idempotency_key("prepare:heat-7")) == str(
        principal.idempotency_key("prepare:heat-7")
    )
    assert "bootstrap-secret" not in repr(issued)
    assert secrets.read("bootstrap-key") == "bootstrap-secret-1234567890"
    persisted = b"".join(event.recompute_digest().encode() for event in events.events())
    persisted += events.database_path.read_bytes()
    assert b"bootstrap-secret" not in persisted
    event = events.events()[0]
    assert event.kind is EventKind.SERVICE_CREDENTIAL_BOOTSTRAPPED
    payload = event.command.payload.to_value()
    assert payload["principal_id"] == "actor:tournament-manager"
    assert payload["key_id_digest"] == hashlib.sha256(b"bootstrap-key").hexdigest()
    assert "credential" not in payload and "secret" not in payload


def test_missing_invalid_and_body_claims_cannot_spoof_identity(credentials) -> None:
    registry, _events, _secrets, _clock = credentials
    issued = registry.bootstrap_offline(
        principal_id="actor:manager-a",
        listener_stopped=True,
        credential="smv3.key-a.secret-a-1234567890123456",
    )

    for header in (None, "", "Basic abc", "Bearer wrong", "Bearer smv3.key-a.wrong-secret"):
        with pytest.raises(CredentialError, match="credential"):
            registry.authenticate(header)

    principal = registry.authenticate(
        f"Bearer {issued.credential}", untrusted_actor_metadata="actor:manager-b"
    )
    assert str(principal.principal_id) == "actor:manager-a"


def test_rotation_accepts_current_and_next_only_during_bounded_overlap(credentials) -> None:
    registry, events, _secrets, clock = credentials
    current = registry.bootstrap_offline(
        principal_id="actor:manager",
        listener_stopped=True,
        credential="smv3.current-key.current-secret-1234567890",
    )
    principal = registry.authenticate(f"Bearer {current.credential}")
    next_key = registry.rotate(
        principal,
        overlap_seconds=600,
        credential="smv3.next-key.next-secret-12345678901234",
    )

    assert registry.authenticate(f"Bearer {current.credential}") == principal
    assert registry.authenticate(f"Bearer {next_key.credential}").principal_id == (
        principal.principal_id
    )
    assert events.events()[-1].kind is EventKind.SERVICE_CREDENTIAL_ROTATED

    clock.advance(seconds=601)
    with pytest.raises(CredentialError, match="revoked or expired"):
        registry.authenticate(f"Bearer {current.credential}")
    assert registry.authenticate(f"Bearer {next_key.credential}").principal_id == (
        principal.principal_id
    )


@pytest.mark.parametrize("overlap", [-1, 0, 901, True, 1.5])
def test_rotation_rejects_invalid_or_overlong_overlap(credentials, overlap) -> None:
    registry, _events, _secrets, _clock = credentials
    current = registry.bootstrap_offline(
        principal_id="actor:manager",
        listener_stopped=True,
        credential="smv3.current-key.current-secret-1234567890",
    )
    principal = registry.authenticate(f"Bearer {current.credential}")
    with pytest.raises(CredentialError, match="overlap"):
        registry.rotate(principal, overlap_seconds=overlap)


def test_revocation_is_immediate_and_online_final_key_revocation_is_rejected(credentials) -> None:
    registry, events, secrets, _clock = credentials
    current = registry.bootstrap_offline(
        principal_id="actor:manager",
        listener_stopped=True,
        credential="smv3.current-key.current-secret-1234567890",
    )
    principal = registry.authenticate(f"Bearer {current.credential}")
    with pytest.raises(CredentialError, match="final active"):
        registry.revoke(principal, current.key_id_digest)

    next_key = registry.rotate(
        principal,
        overlap_seconds=900,
        credential="smv3.next-key.next-secret-12345678901234",
    )
    registry.revoke(principal, current.key_id_digest)
    assert secrets.read("current-key") is None
    with pytest.raises(CredentialError, match="revoked or expired"):
        registry.authenticate(f"Bearer {current.credential}")
    assert registry.authenticate(f"Bearer {next_key.credential}").principal_id == (
        principal.principal_id
    )
    assert events.events()[-1].kind is EventKind.SERVICE_CREDENTIAL_REVOKED


def test_revoking_next_during_overlap_rolls_current_authority_back_to_old(credentials) -> None:
    registry, events, secrets, _clock = credentials
    current = registry.bootstrap_offline(
        principal_id="actor:manager",
        listener_stopped=True,
        credential="smv3.current-key.current-secret-1234567890",
    )
    principal = registry.authenticate(f"Bearer {current.credential}")
    next_key = registry.rotate(
        principal,
        overlap_seconds=900,
        credential="smv3.next-key.next-secret-12345678901234",
    )
    next_principal = registry.authenticate(f"Bearer {next_key.credential}")
    registry.revoke(next_principal, next_key.key_id_digest)
    assert secrets.read("next-key") is None
    with pytest.raises(CredentialError, match="revoked"):
        registry.authenticate(f"Bearer {next_key.credential}")
    restored = registry.authenticate(f"Bearer {current.credential}")
    replacement = registry.rotate(
        restored,
        overlap_seconds=60,
        credential="smv3.replacement-key.replacement-secret-1234567",
    )
    restarted = ServiceCredentialRegistry(SQLiteEventStore(events.database_path), registry._secrets)
    assert restarted.authenticate(f"Bearer {replacement.credential}").principal_id == (
        restored.principal_id
    )


def test_rotation_and_recovery_cannot_replace_immutable_principal(credentials) -> None:
    registry, _events, _secrets, _clock = credentials
    current = registry.bootstrap_offline(
        principal_id="actor:manager-a",
        listener_stopped=True,
        credential="smv3.current-key.current-secret-1234567890",
    )
    principal = registry.authenticate(f"Bearer {current.credential}")
    with pytest.raises(CredentialError, match="principal"):
        registry.rotate(
            principal,
            principal_id="actor:manager-b",
            credential="smv3.next-key.next-secret-12345678901234",
        )
    with pytest.raises(CredentialError, match="principal"):
        registry.recover_offline(
            principal_id="actor:manager-b",
            listener_stopped=True,
            credential="smv3.recovery-key.recovery-secret-1234567890",
        )


def test_total_loss_recovery_requires_stopped_listener_and_preserves_history(credentials) -> None:
    registry, events, secrets, _clock = credentials
    original = registry.bootstrap_offline(
        principal_id="actor:manager",
        listener_stopped=True,
        credential="smv3.original-key.original-secret-1234567890",
    )
    secrets.delete("original-key")
    with pytest.raises(CredentialError, match="listener"):
        registry.recover_offline(
            principal_id="actor:manager",
            listener_stopped=False,
            credential="smv3.recovery-key.recovery-secret-1234567890",
        )

    recovered = registry.recover_offline(
        principal_id="actor:manager",
        listener_stopped=True,
        credential="smv3.recovery-key.recovery-secret-1234567890",
    )
    assert registry.authenticate(f"Bearer {recovered.credential}").principal_id == (
        registry.principal_id
    )
    with pytest.raises(CredentialError):
        registry.authenticate(f"Bearer {original.credential}")
    assert [event.kind for event in events.events()] == [
        EventKind.SERVICE_CREDENTIAL_BOOTSTRAPPED,
        EventKind.SERVICE_CREDENTIAL_RECOVERED,
    ]


def test_certificate_binding_is_required_and_exact_when_transport_requires_it(credentials) -> None:
    registry, _events, _secrets, _clock = credentials
    issued = registry.bootstrap_offline(
        principal_id="actor:manager",
        listener_stopped=True,
        credential="smv3.current-key.current-secret-1234567890",
    )
    header = f"Bearer {issued.credential}"
    with pytest.raises(CredentialError, match="certificate"):
        registry.authenticate(header, require_certificate=True)
    with pytest.raises(CredentialError, match="certificate"):
        registry.authenticate(
            header,
            require_certificate=True,
            certificate_principal="actor:somebody-else",
        )
    assert (
        registry.authenticate(
            header,
            require_certificate=True,
            certificate_principal="actor:manager",
        ).principal_id
        == registry.principal_id
    )


def test_restart_reconstructs_authoritative_credential_state(credentials) -> None:
    registry, events, secrets, clock = credentials
    current = registry.bootstrap_offline(
        principal_id="actor:manager",
        listener_stopped=True,
        credential="smv3.current-key.current-secret-1234567890",
    )
    principal = registry.authenticate(f"Bearer {current.credential}")
    next_key = registry.rotate(
        principal,
        overlap_seconds=300,
        credential="smv3.next-key.next-secret-12345678901234",
    )

    restarted = ServiceCredentialRegistry(
        SQLiteEventStore(events.database_path), secrets, clock=clock
    )
    assert restarted.authenticate(f"Bearer {current.credential}").principal_id == (
        principal.principal_id
    )
    assert restarted.authenticate(f"Bearer {next_key.credential}").principal_id == (
        principal.principal_id
    )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager adapter")
def test_windows_dpapi_round_trip_is_os_protected_and_deletable(tmp_path: Path) -> None:
    key_id = "pytest-key"
    secret = "synthetic-test-value-1234567890"
    root = tmp_path / "dpapi"
    store = WindowsCredentialSecretStore(f"pytest-{uuid.uuid4().hex}", root)
    try:
        store.write(key_id, secret)
        ciphertexts = tuple(root.glob("*.dpapi"))
        assert len(ciphertexts) == 1
        assert secret.encode() not in ciphertexts[0].read_bytes()
        assert store.read(key_id) == secret
        wrong_identity = WindowsCredentialSecretStore(f"pytest-{uuid.uuid4().hex}", root)
        with pytest.raises(CredentialError, match="decryption"):
            wrong_identity.read(key_id)
        tampered = bytearray(ciphertexts[0].read_bytes())
        tampered[-1] ^= 1
        ciphertexts[0].write_bytes(tampered)
        with pytest.raises(CredentialError, match="decryption"):
            store.read(key_id)
    finally:
        store.delete(key_id)
    assert store.read(key_id) is None


def test_credential_boundary_rejects_invalid_adapters_tokens_and_clocks(tmp_path: Path) -> None:
    with pytest.raises(CredentialError, match="event authority"):
        ServiceCredentialRegistry(object(), InMemoryCredentialSecretStore())  # type: ignore[arg-type]
    if sys.platform != "win32":
        with pytest.raises(CredentialError, match="Windows"):
            WindowsCredentialSecretStore("test-install", tmp_path / "dpapi")
    if sys.platform == "win32":
        with pytest.raises(CredentialError, match="installation"):
            WindowsCredentialSecretStore("x", tmp_path / "dpapi")

    store = InMemoryCredentialSecretStore()
    for key_id, secret in (("x", "valid-secret-value-123456"), ("valid-key", "short")):
        with pytest.raises(CredentialError):
            store.write(key_id, secret)
    with pytest.raises(CredentialError):
        store.read("x")

    events = SQLiteEventStore(tmp_path / "bad-clock.sqlite3")
    registry = ServiceCredentialRegistry(events, store, clock=lambda: datetime(2026, 8, 25, 12, 0))
    with pytest.raises(CredentialError, match="aware"):
        registry.bootstrap_offline(
            principal_id="actor:manager",
            listener_stopped=True,
            credential="smv3.valid-key.valid-secret-value-123456",
        )


def test_registry_rejects_online_bootstrap_duplicate_keys_and_unsafe_idempotency(
    credentials,
) -> None:
    registry, _events, _secrets, _clock = credentials
    with pytest.raises(CredentialError, match="listener"):
        registry.bootstrap_offline(
            principal_id="actor:manager",
            listener_stopped=False,
            credential="smv3.current-key.current-secret-1234567890",
        )
    current = registry.bootstrap_offline(
        principal_id="actor:manager",
        listener_stopped=True,
        credential="smv3.current-key.current-secret-1234567890",
    )
    with pytest.raises(CredentialError, match="already bootstrapped"):
        registry.bootstrap_offline(
            principal_id="actor:manager",
            listener_stopped=True,
            credential="smv3.other-key.other-secret-value-12345678",
        )
    principal = registry.authenticate(f"Bearer {current.credential}")
    with pytest.raises(CredentialError, match="safe bounded"):
        principal.idempotency_key("unsafe key")
    with pytest.raises(CredentialError, match="already been used"):
        registry.rotate(
            principal,
            credential="smv3.current-key.another-secret-value-123456",
        )


def test_generated_credentials_and_direct_principal_guards(credentials) -> None:
    registry, _events, _secrets, clock = credentials
    with pytest.raises(CredentialError, match="not been bootstrapped"):
        _ = registry.principal_id
    generated = registry.bootstrap_offline(principal_id="actor:manager", listener_stopped=True)
    assert generated.credential.startswith("smv3.")
    principal = registry.authenticate(f"Bearer {generated.credential}")
    with pytest.raises(CredentialError, match="authenticated"):
        registry.rotate(object())  # type: ignore[arg-type]
    wrong = ServicePrincipal(StableIdentifier("actor:other"), principal.key_id_digest)
    with pytest.raises(CredentialError, match="does not match"):
        registry.rotate(wrong)
    rotated = registry.rotate(
        principal,
        overlap_seconds=1,
        credential="smv3.next-key.next-secret-12345678901234",
    )
    clock.advance(seconds=2)
    with pytest.raises(CredentialError, match="expired"):
        registry.rotate(principal)
    assert registry.authenticate(f"Bearer {rotated.credential}").principal_id == (
        registry.principal_id
    )


def test_credential_command_id_validation_missing_secret_and_changed_revoke_retry(
    credentials,
) -> None:
    registry, _events, secrets, _clock = credentials
    current = registry.bootstrap_offline(
        principal_id="actor:manager",
        listener_stopped=True,
        credential="smv3.current-key.current-secret-1234567890",
    )
    principal = registry.authenticate(f"Bearer {current.credential}")
    with pytest.raises(CredentialError, match="command identity"):
        registry.rotate(principal, command_id="bad")  # type: ignore[arg-type]
    rotation_id = principal.idempotency_key("rotation-retry")
    rotated = registry.rotate(principal, command_id=rotation_id)
    rotated_key_id = rotated.credential.split(".")[1]
    secrets.delete(rotated_key_id)
    with pytest.raises(CredentialError, match="unavailable"):
        registry.rotate(principal, command_id=rotation_id)
    secrets.write(rotated_key_id, rotated.credential.rsplit(".", 1)[1])

    next_principal = registry.authenticate(f"Bearer {rotated.credential}")
    with pytest.raises(CredentialError, match="command identity"):
        registry.revoke(next_principal, current.key_id_digest, command_id="bad")  # type: ignore[arg-type]
    revoke_id = next_principal.idempotency_key("revoke-retry")
    registry.revoke(next_principal, current.key_id_digest, command_id=revoke_id)
    with pytest.raises(CredentialError, match="different credential input"):
        registry.revoke(next_principal, rotated.key_id_digest, command_id=revoke_id)


def test_secret_staging_is_removed_when_authority_append_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = InMemoryCredentialSecretStore()
    registry = ServiceCredentialRegistry(
        SQLiteEventStore(tmp_path / "append-failure.sqlite3"), secrets
    )

    def fail(*_args, **_kwargs):
        raise RuntimeError("append failed")

    monkeypatch.setattr(registry, "_append", fail)
    with pytest.raises(RuntimeError, match="append failed"):
        registry.bootstrap_offline(
            principal_id="actor:manager",
            listener_stopped=True,
            credential="smv3.staged-key.staged-secret-123456789012",
        )
    assert secrets.read("staged-key") is None


def test_private_contract_helpers_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(CredentialError, match="empty"):
        auth_module._blob(auth_module.WindowsCredentialSecretStore._DATA_BLOB, bytearray())
    with pytest.raises(CredentialError, match="digest"):
        auth_module._require_digest("bad")
    with pytest.raises(CredentialError, match="timestamp"):
        auth_module._parse_utc("not-utc")
    with pytest.raises(CredentialError, match="timestamp"):
        auth_module._parse_utc("2026-99-99T00:00:00.000Z")
    with pytest.raises(CredentialError, match="text"):
        auth_module._credential_parts(7)  # type: ignore[arg-type]
    with pytest.raises(CredentialError, match="entropy"):
        auth_module._credential_parts("smv3.short.bad")
    with pytest.raises(Exception):
        ServicePrincipal(StableIdentifier("actor:manager"), "bad")
    with pytest.raises(CredentialError, match="not bootstrapped"):
        ServiceCredentialRegistry(
            SQLiteEventStore(tmp_path / "unbootstrapped.sqlite3"),
            InMemoryCredentialSecretStore(),
        ).authenticate("Bearer smv3.valid-key.valid-secret-value-123456")
