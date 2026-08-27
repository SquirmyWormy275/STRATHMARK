"""Service-credential authority for the V3 transport boundary.

Only key-ID digests and the immutable service principal enter the event authority.
Credential material remains behind a secret-store port.  The HTTP adapter authenticates
before reading a request body and receives a :class:`ServicePrincipal`; request metadata
can never replace that identity.
"""

from __future__ import annotations

import ctypes
import hashlib
import hmac
import os
import re
import secrets
import sys
import threading
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol

from strathmark.v3.application.commands import CommandRequest, EventIntent
from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.contracts.commands import CommandEnvelope, CommandKind, InlinePayload
from strathmark.v3.contracts.errors import V3Error
from strathmark.v3.contracts.events import AggregateKind, EventKind
from strathmark.v3.contracts.identifiers import (
    IdempotencyKey,
    StableIdentifier,
    require_identifier,
)
from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore

MAX_CREDENTIAL_OVERLAP_SECONDS = 900
_CREDENTIAL_AUTHORITY_ID = "service_credential:authority"
_TOKEN = re.compile(r"^smv3\.([A-Za-z0-9_-]{3,64})\.([A-Za-z0-9_-]{20,192})$")


class CredentialError(V3Error, RuntimeError):
    """Credential configuration or authentication failed closed."""

    code = "service_credential_error"


class CredentialSecretStore(Protocol):
    """Secret-only storage; implementations must never enumerate credential values."""

    def write(self, key_id: str, secret: str) -> None: ...

    def read(self, key_id: str) -> str | None: ...

    def delete(self, key_id: str) -> None: ...

    def delete_digest(self, key_id_digest: str) -> None: ...


class InMemoryCredentialSecretStore:
    """Explicit test/rehearsal adapter; never selected by production composition."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def write(self, key_id: str, secret: str) -> None:
        _validate_key_and_secret(key_id, secret)
        self._values[key_id] = secret

    def read(self, key_id: str) -> str | None:
        _validate_key_id(key_id)
        return self._values.get(key_id)

    def delete(self, key_id: str) -> None:
        _validate_key_id(key_id)
        self._values.pop(key_id, None)

    def delete_digest(self, key_id_digest: str) -> None:
        _require_digest(key_id_digest)
        for key_id in tuple(self._values):
            if hmac.compare_digest(_key_id_digest(key_id), key_id_digest):
                self._values.pop(key_id, None)


class WindowsCredentialSecretStore:
    """Current-service-identity protected DPAPI storage.

    Ciphertext files are named only by key-ID digest. DPAPI binds decryption to the
    Windows identity running STRATHMARK and installation-specific optional entropy.
    """

    _CRYPTPROTECT_UI_FORBIDDEN = 0x1

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    def __init__(self, installation_id: str, storage_root: Path | str) -> None:
        if sys.platform != "win32":
            raise CredentialError("Windows DPAPI is required in production")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", installation_id):
            raise CredentialError("installation identity is invalid")
        if isinstance(storage_root, bool) or not isinstance(storage_root, (Path, str)):
            raise CredentialError("credential storage root must be an explicit filesystem path")
        self._root = Path(storage_root).expanduser().resolve(strict=False)
        self._root.mkdir(parents=True, exist_ok=True)
        if not self._root.is_dir():
            raise CredentialError("credential storage root is not a directory")
        self._entropy = bytearray(f"STRATHMARK/V3/{installation_id}".encode("utf-8"))
        self._crypt32 = ctypes.WinDLL("Crypt32.dll", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
        self._crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(self._DATA_BLOB),
            wintypes.LPCWSTR,
            ctypes.POINTER(self._DATA_BLOB),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(self._DATA_BLOB),
        ]
        self._crypt32.CryptProtectData.restype = wintypes.BOOL
        self._crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(self._DATA_BLOB),
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(self._DATA_BLOB),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(self._DATA_BLOB),
        ]
        self._crypt32.CryptUnprotectData.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self._kernel32.LocalFree.restype = ctypes.c_void_p

    def write(self, key_id: str, secret: str) -> None:
        _validate_key_and_secret(key_id, secret)
        plaintext = bytearray(secret.encode("utf-8"))
        try:
            protected = self._protect(plaintext)
        finally:
            _zero(plaintext)
        target = self._path(key_id)
        temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(protected)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def read(self, key_id: str) -> str | None:
        _validate_key_id(key_id)
        target = self._path(key_id)
        if not target.is_file():
            return None
        try:
            plaintext = self._unprotect(target.read_bytes())
            try:
                value = plaintext.decode("utf-8")
                _validate_key_and_secret(key_id, value)
                return value
            finally:
                _zero(plaintext)
        except CredentialError:
            raise
        except Exception as exc:
            raise CredentialError("OS-protected credential read failed") from exc

    def delete(self, key_id: str) -> None:
        _validate_key_id(key_id)
        self._path(key_id).unlink(missing_ok=True)

    def delete_digest(self, key_id_digest: str) -> None:
        _require_digest(key_id_digest)
        (self._root / f"{key_id_digest}.dpapi").unlink(missing_ok=True)

    def _path(self, key_id: str) -> Path:
        return self._root / f"{_key_id_digest(key_id)}.dpapi"

    def _protect(self, plaintext: bytearray) -> bytes:
        source, source_buffer = _blob(self._DATA_BLOB, plaintext)
        entropy, entropy_buffer = _blob(self._DATA_BLOB, self._entropy)
        output = self._DATA_BLOB()
        if not self._crypt32.CryptProtectData(
            ctypes.byref(source),
            "STRATHMARK V3 service credential",
            ctypes.byref(entropy),
            None,
            None,
            self._CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output),
        ):
            raise CredentialError("OS-protected credential encryption failed")
        return self._copy_and_free(output)

    def _unprotect(self, protected: bytes) -> bytearray:
        mutable = bytearray(protected)
        source, source_buffer = _blob(self._DATA_BLOB, mutable)
        entropy, entropy_buffer = _blob(self._DATA_BLOB, self._entropy)
        output = self._DATA_BLOB()
        description = wintypes.LPWSTR()
        try:
            if not self._crypt32.CryptUnprotectData(
                ctypes.byref(source),
                ctypes.byref(description),
                ctypes.byref(entropy),
                None,
                None,
                self._CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(output),
            ):
                raise CredentialError("OS-protected credential decryption failed")
            return bytearray(self._copy_and_free(output))
        finally:
            _zero(mutable)
            if description:
                self._kernel32.LocalFree(description)

    def _copy_and_free(self, output: _DATA_BLOB) -> bytes:
        try:
            return ctypes.string_at(output.pbData, int(output.cbData))
        finally:
            if output.pbData:
                ctypes.memset(output.pbData, 0, int(output.cbData))
                self._kernel32.LocalFree(output.pbData)


@dataclass(frozen=True, slots=True)
class IssuedServiceCredential:
    credential: str = field(repr=False)
    key_id_digest: str
    principal_id: StableIdentifier


@dataclass(frozen=True, slots=True)
class ServicePrincipal:
    principal_id: StableIdentifier
    key_id_digest: str

    def __post_init__(self) -> None:
        require_identifier(self.principal_id, expected_namespace="actor")
        _require_digest(self.key_id_digest)

    def idempotency_key(self, external_key: str) -> IdempotencyKey:
        if (
            not isinstance(external_key, str)
            or not 1 <= len(external_key) <= 128
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", external_key) is None
        ):
            raise CredentialError("idempotency key must be a safe bounded token")
        digest = hashlib.sha256(f"{self.principal_id}\0{external_key}".encode("utf-8")).hexdigest()
        return IdempotencyKey(f"command:{digest}")


@dataclass(frozen=True, slots=True)
class _CredentialState:
    principal_id: StableIdentifier
    active_until: dict[str, datetime | None]
    current_digest: str


class ServiceCredentialRegistry:
    """Replayable current/next credentials bound to one immutable service principal."""

    def __init__(
        self,
        authority: SQLiteEventStore,
        secret_store: CredentialSecretStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(authority, SQLiteEventStore):
            raise CredentialError("credential registry requires the V3 event authority")
        self._authority = authority
        self._secrets = secret_store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._state_lock = threading.RLock()
        self._cached_state: _CredentialState | None = None
        self._cached_head: tuple[int, str] | None = None
        self.refresh()

    @property
    def principal_id(self) -> StableIdentifier:
        try:
            return self._cached(required=True).principal_id
        except CredentialError as exc:
            if str(exc) == "service credential authority is not bootstrapped":
                raise CredentialError("service credential has not been bootstrapped") from exc
            raise

    def bootstrap_offline(
        self,
        *,
        principal_id: str,
        listener_stopped: bool,
        credential: str | None = None,
    ) -> IssuedServiceCredential:
        if listener_stopped is not True:
            raise CredentialError("offline bootstrap requires the listener to be stopped")
        if self._state(required=False) is not None:
            raise CredentialError("service credential authority is already bootstrapped")
        principal = require_identifier(principal_id, expected_namespace="actor")
        token, key_id, secret = _credential_parts(credential)
        digest = _key_id_digest(key_id)
        payload = {
            "schema_version": "strathmark-v3-service-credential-bootstrap-v1",
            "principal_id": str(principal),
            "key_id_digest": digest,
            "activated_at_utc": _utc_text(self._now()),
        }
        self._secrets.write(key_id, secret)
        try:
            self._append(
                principal,
                CommandKind.BOOTSTRAP_SERVICE_CREDENTIAL,
                EventKind.SERVICE_CREDENTIAL_BOOTSTRAPPED,
                payload,
            )
        except BaseException:
            self._secrets.delete(key_id)
            raise
        self.refresh()
        return IssuedServiceCredential(token, digest, principal)

    def rotate(
        self,
        principal: ServicePrincipal,
        *,
        overlap_seconds: int = MAX_CREDENTIAL_OVERLAP_SECONDS,
        credential: str | None = None,
        principal_id: str | None = None,
        command_id: IdempotencyKey | None = None,
    ) -> IssuedServiceCredential:
        state = self._require_principal(principal)
        if (
            principal_id is not None
            and require_identifier(principal_id, expected_namespace="actor") != state.principal_id
        ):
            raise CredentialError("credential rotation cannot replace the immutable principal")
        if (
            isinstance(overlap_seconds, bool)
            or not isinstance(overlap_seconds, int)
            or not 1 <= overlap_seconds <= MAX_CREDENTIAL_OVERLAP_SECONDS
        ):
            raise CredentialError("credential overlap must be an integer from 1 to 900 seconds")
        if command_id is not None and not isinstance(command_id, IdempotencyKey):
            raise CredentialError("credential rotation command identity is invalid")
        prior = self._event_for_command(command_id)
        if prior is not None:
            if not isinstance(prior.command.payload, InlinePayload):
                raise CredentialError("credential idempotency key belongs to another command")
            value = prior.command.payload.to_value()
            if (
                prior.kind is not EventKind.SERVICE_CREDENTIAL_ROTATED
                or value.get("principal_id") != str(state.principal_id)
                or value.get("overlap_seconds") != overlap_seconds
            ):
                raise CredentialError("idempotency key already binds different credential input")
            key_id = _rotation_key_id(command_id)
            secret = self._secrets.read(key_id)
            if secret is None:
                raise CredentialError("rotated credential secret is unavailable for exact retry")
            self.refresh()
            return IssuedServiceCredential(
                f"smv3.{key_id}.{secret}", _key_id_digest(key_id), state.principal_id
            )
        if credential is None and command_id is not None:
            key_id = _rotation_key_id(command_id)
            secret = self._secrets.read(key_id) or secrets.token_urlsafe(36)
            token = f"smv3.{key_id}.{secret}"
        else:
            token, key_id, secret = _credential_parts(credential)
        digest = _key_id_digest(key_id)
        if digest in state.active_until:
            raise CredentialError("credential key ID has already been used")
        now = self._now()
        payload = {
            "schema_version": "strathmark-v3-service-credential-rotation-v1",
            "principal_id": str(state.principal_id),
            "previous_key_id_digest": state.current_digest,
            "key_id_digest": digest,
            "activated_at_utc": _utc_text(now),
            "overlap_until_utc": _utc_text(now + timedelta(seconds=overlap_seconds)),
            "overlap_seconds": overlap_seconds,
        }
        self._secrets.write(key_id, secret)
        try:
            self._append(
                state.principal_id,
                CommandKind.ROTATE_SERVICE_CREDENTIAL,
                EventKind.SERVICE_CREDENTIAL_ROTATED,
                payload,
                command_id=command_id,
            )
        except BaseException:
            self._secrets.delete(key_id)
            raise
        self.refresh()
        return IssuedServiceCredential(token, digest, state.principal_id)

    def revoke(
        self,
        principal: ServicePrincipal,
        key_id_digest: str,
        *,
        command_id: IdempotencyKey | None = None,
    ) -> None:
        state = self._require_principal(principal)
        if command_id is not None and not isinstance(command_id, IdempotencyKey):
            raise CredentialError("credential revocation command identity is invalid")
        prior = self._event_for_command(command_id)
        if prior is not None:
            if not isinstance(prior.command.payload, InlinePayload):
                raise CredentialError("credential idempotency key belongs to another command")
            value = prior.command.payload.to_value()
            if (
                prior.kind is not EventKind.SERVICE_CREDENTIAL_REVOKED
                or value.get("principal_id") != str(state.principal_id)
                or value.get("key_id_digest") != key_id_digest
            ):
                raise CredentialError("idempotency key already binds different credential input")
            self.refresh()
            self._secrets.delete_digest(key_id_digest)
            return
        now = self._now()
        active = {
            digest
            for digest, expiry in state.active_until.items()
            if expiry is None or expiry >= now
        }
        if key_id_digest not in active:
            raise CredentialError("credential is already revoked or expired")
        if len(active) == 1:
            raise CredentialError("online commands cannot revoke the final active credential")
        remaining = [digest for digest in state.active_until if digest in active - {key_id_digest}]
        current_after_revoke = (
            remaining[-1] if key_id_digest == state.current_digest else state.current_digest
        )
        payload = {
            "schema_version": "strathmark-v3-service-credential-revocation-v1",
            "principal_id": str(state.principal_id),
            "key_id_digest": key_id_digest,
            "revoked_at_utc": _utc_text(now),
            "current_key_id_digest_after_revoke": current_after_revoke,
        }
        self._append(
            state.principal_id,
            CommandKind.REVOKE_SERVICE_CREDENTIAL,
            EventKind.SERVICE_CREDENTIAL_REVOKED,
            payload,
            command_id=command_id,
        )
        self.refresh()
        self._secrets.delete_digest(key_id_digest)

    def recover_offline(
        self,
        *,
        principal_id: str,
        listener_stopped: bool,
        credential: str | None = None,
    ) -> IssuedServiceCredential:
        if listener_stopped is not True:
            raise CredentialError("offline recovery requires the listener to be stopped")
        state = self._state(required=True)
        principal = require_identifier(principal_id, expected_namespace="actor")
        if principal != state.principal_id:
            raise CredentialError("offline recovery must preserve the immutable principal")
        token, key_id, secret = _credential_parts(credential)
        digest = _key_id_digest(key_id)
        if digest in state.active_until:
            raise CredentialError("recovery requires a new credential key ID")
        payload = {
            "schema_version": "strathmark-v3-service-credential-recovery-v1",
            "principal_id": str(principal),
            "key_id_digest": digest,
            "recovered_at_utc": _utc_text(self._now()),
            "replaced_key_id_digests": sorted(state.active_until),
        }
        self._secrets.write(key_id, secret)
        try:
            self._append(
                principal,
                CommandKind.RECOVER_SERVICE_CREDENTIAL,
                EventKind.SERVICE_CREDENTIAL_RECOVERED,
                payload,
            )
        except BaseException:
            self._secrets.delete(key_id)
            raise
        self.refresh()
        for replaced_digest in state.active_until:
            self._secrets.delete_digest(replaced_digest)
        return IssuedServiceCredential(token, digest, principal)

    def authenticate(
        self,
        authorization_header: str | None,
        *,
        require_certificate: bool = False,
        certificate_principal: str | None = None,
        untrusted_actor_metadata: str | None = None,
    ) -> ServicePrincipal:
        del untrusted_actor_metadata
        if not isinstance(authorization_header, str) or not authorization_header.startswith(
            "Bearer "
        ):
            raise CredentialError("service credential is missing or malformed")
        match = _TOKEN.fullmatch(authorization_header[7:])
        if match is None:
            raise CredentialError("service credential is missing or malformed")
        key_id, presented_secret = match.groups()
        digest = _key_id_digest(key_id)
        state = self._cached(required=True)
        now = self._now()
        expiry = state.active_until.get(digest, datetime.min.replace(tzinfo=timezone.utc))
        if digest not in state.active_until or (expiry is not None and expiry < now):
            if digest in state.active_until:
                self._secrets.delete(key_id)
            raise CredentialError("service credential is revoked or expired")
        expected_secret = self._secrets.read(key_id)
        # Always execute a constant-time comparison, including a missing-store entry.
        compare = expected_secret if expected_secret is not None else secrets.token_urlsafe(32)
        if not hmac.compare_digest(compare.encode(), presented_secret.encode()):
            raise CredentialError("service credential is invalid")
        if require_certificate:
            if certificate_principal is None:
                raise CredentialError("verified client certificate principal is required")
            try:
                certificate_id = require_identifier(
                    certificate_principal, expected_namespace="actor"
                )
            except Exception as exc:
                raise CredentialError("client certificate principal is invalid") from exc
            if certificate_id != state.principal_id:
                raise CredentialError("client certificate principal does not match credential")
        return ServicePrincipal(state.principal_id, digest)

    def _require_principal(self, principal: ServicePrincipal) -> _CredentialState:
        if not isinstance(principal, ServicePrincipal):
            raise CredentialError("authenticated service principal is required")
        state = self._cached(required=True)
        if not hmac.compare_digest(str(principal.principal_id), str(state.principal_id)):
            raise CredentialError("credential principal does not match authority")
        if principal.key_id_digest not in state.active_until:
            raise CredentialError("credential principal is revoked or expired")
        expiry = state.active_until[principal.key_id_digest]
        if expiry is not None and expiry < self._now():
            raise CredentialError("credential principal is revoked or expired")
        return state

    def refresh(self) -> None:
        """Rebuild the in-memory authentication snapshot from verified event authority."""

        with self._state_lock:
            state, head = self._stable_authority_snapshot(required=False)
            self._cached_state = state
            self._cached_head = head

    def _cached(self, *, required: bool) -> _CredentialState:
        with self._state_lock:
            observed_head = self._authority.aggregate_head(_CREDENTIAL_AUTHORITY_ID)
            if observed_head != self._cached_head:
                state, head = self._stable_authority_snapshot(required=required)
                self._cached_state = state
                self._cached_head = head
            state = self._cached_state
        if state is None:
            if required:
                raise CredentialError("service credential authority is not bootstrapped")
            raise CredentialError("service credential authority is not bootstrapped")
        return state

    def _stable_authority_snapshot(
        self, *, required: bool
    ) -> tuple[_CredentialState | None, tuple[int, str] | None]:
        for _attempt in range(3):
            before = self._authority.aggregate_head(_CREDENTIAL_AUTHORITY_ID)
            state = self._state(required=required)
            after = self._authority.aggregate_head(_CREDENTIAL_AUTHORITY_ID)
            if before == after:
                return state, after
        raise CredentialError("service credential authority changed during refresh")

    def _state(self, *, required: bool) -> _CredentialState | None:
        principal: StableIdentifier | None = None
        active: dict[str, datetime | None] = {}
        current = ""
        found = False
        for event in self._authority.events():
            if event.aggregate_kind is not AggregateKind.SERVICE_CREDENTIAL:
                continue
            payload = event.command.payload
            if not isinstance(payload, InlinePayload):
                raise CredentialError("credential authority payload must remain inline")
            value = payload.to_value()
            event_principal = require_identifier(value["principal_id"], expected_namespace="actor")
            if principal is None:
                principal = event_principal
            elif event_principal != principal:
                raise CredentialError("credential authority contains a principal discontinuity")
            found = True
            if event.kind is EventKind.SERVICE_CREDENTIAL_BOOTSTRAPPED:
                current = _require_digest(value["key_id_digest"])
                active = {current: None}
            elif event.kind is EventKind.SERVICE_CREDENTIAL_ROTATED:
                previous = _require_digest(value["previous_key_id_digest"])
                if previous != current or previous not in active:
                    raise CredentialError("credential rotation does not follow current authority")
                active[previous] = _parse_utc(value["overlap_until_utc"])
                current = _require_digest(value["key_id_digest"])
                if current in active:
                    raise CredentialError("credential key ID was reused")
                active[current] = None
            elif event.kind is EventKind.SERVICE_CREDENTIAL_REVOKED:
                revoked = _require_digest(value["key_id_digest"])
                if revoked not in active:
                    raise CredentialError("credential revocation names an unknown key")
                del active[revoked]
                current = _require_digest(value["current_key_id_digest_after_revoke"])
                if current not in active:
                    raise CredentialError("credential revocation leaves no declared current key")
            elif event.kind is EventKind.SERVICE_CREDENTIAL_RECOVERED:
                current = _require_digest(value["key_id_digest"])
                active = {current: None}
            else:
                raise CredentialError("unknown credential lifecycle event")
        if not found:
            if required:
                raise CredentialError("service credential authority is not bootstrapped")
            return None
        assert principal is not None
        return _CredentialState(principal, active, current)

    def _append(
        self,
        principal: StableIdentifier,
        command_kind: CommandKind,
        event_kind: EventKind,
        payload: dict[str, object],
        *,
        command_id: IdempotencyKey | None = None,
    ) -> None:
        # One global credential-authority stream makes a competing bootstrap collide
        # atomically instead of permitting one aggregate per hostile principal.
        aggregate = StableIdentifier(_CREDENTIAL_AUTHORITY_ID)
        head = self._authority.aggregate_head(str(aggregate))
        version = 0 if head is None else head[0]
        inline = InlinePayload.from_value(payload)
        resolved_command_id = command_id or IdempotencyKey(f"command:{canonical_digest(payload)}")
        now = self._now()
        request = CommandRequest(
            principal_id=principal,
            command=CommandEnvelope(
                kind=command_kind,
                command_id=resolved_command_id,
                target_aggregate=aggregate,
                expected_versions=((str(aggregate), version),),
                actor_id=principal,
                payload=inline,
            ),
            events=(EventIntent(AggregateKind.SERVICE_CREDENTIAL, aggregate, event_kind),),
            result_schema_version="strathmark-v3-service-credential-result-v1",
            result={"principal_id": str(principal), "key_id_digest": payload["key_id_digest"]},
            occurred_at_utc=_utc_text(now),
            monotonic_elapsed_ms=0,
        )
        self._authority.execute(request)

    def _event_for_command(self, command_id: IdempotencyKey | None):
        if command_id is None:
            return None
        matches = tuple(
            event for event in self._authority.events() if event.command.command_id == command_id
        )
        if len(matches) > 1:
            raise CredentialError("credential command identity is not unique")
        return None if not matches else matches[0]

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise CredentialError("credential clock must return an aware datetime")
        return value.astimezone(timezone.utc)


def _credential_parts(value: str | None) -> tuple[str, str, str]:
    if value is None:
        key_id = secrets.token_urlsafe(18)
        secret = secrets.token_urlsafe(36)
        return f"smv3.{key_id}.{secret}", key_id, secret
    if not isinstance(value, str):
        raise CredentialError("credential must be text")
    match = _TOKEN.fullmatch(value)
    if match is None:
        raise CredentialError("credential format or entropy is invalid")
    return value, match.group(1), match.group(2)


def _blob(blob_type, value: bytearray):
    if not value:
        raise CredentialError("DPAPI value cannot be empty")
    buffer = (ctypes.c_ubyte * len(value)).from_buffer(value)
    return (
        blob_type(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))),
        buffer,
    )


def _zero(value: bytearray) -> None:
    if value:
        ctypes.memset((ctypes.c_ubyte * len(value)).from_buffer(value), 0, len(value))


def _rotation_key_id(command_id: IdempotencyKey) -> str:
    return "r" + hashlib.sha256(str(command_id).encode("ascii")).hexdigest()[:31]


def _validate_key_id(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{3,64}", value):
        raise CredentialError("credential key ID is invalid")


def _validate_key_and_secret(key_id: str, secret: str) -> None:
    _validate_key_id(key_id)
    if not isinstance(secret, str) or not re.fullmatch(r"[A-Za-z0-9_-]{20,192}", secret):
        raise CredentialError("credential secret is invalid")


def _key_id_digest(key_id: str) -> str:
    _validate_key_id(key_id)
    return hashlib.sha256(key_id.encode("ascii")).hexdigest()


def _require_digest(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise CredentialError("credential authority digest is malformed")
    return value


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CredentialError("credential authority timestamp is malformed")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise CredentialError("credential authority timestamp is malformed") from exc
    return parsed.astimezone(timezone.utc)


__all__ = [
    "CredentialError",
    "CredentialSecretStore",
    "InMemoryCredentialSecretStore",
    "IssuedServiceCredential",
    "MAX_CREDENTIAL_OVERLAP_SECONDS",
    "ServiceCredentialRegistry",
    "ServicePrincipal",
    "WindowsCredentialSecretStore",
]
