"""Scoped service authentication and delegated actor attestation verification.

This boundary deliberately uses only the Python standard library.  Credential
material and raw attestations are never returned, persisted, or logged.  The
durable prediction ledger stores only a nonce digest to make replay protection
survive service restarts.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from strathmark.identity import validate_namespaced_identity

ACTOR_ATTESTATION_SCHEMA_VERSION = "strathmark.actor-attestation.v2"
REQUEST_DIGEST_SCHEMA_VERSION = "strathmark.shadow-request-digest.v1"
SHADOW_ATTESTATION_AUDIENCE = "strathmark.shadow.v1"
MAX_ATTESTATION_LIFETIME_SECONDS = 60
MAX_ATTESTATION_CLOCK_SKEW_SECONDS = 5

_SERVICE_CREDENTIALS_ENV = "STRATHMARK_SHADOW_SERVICE_CREDENTIALS"
_ATTESTATION_KEYS_ENV = "STRATHMARK_SHADOW_ATTESTATION_KEYS"
_NONCE = re.compile(r"^[A-Za-z0-9._:-]{16,128}$")
SHADOW_ACTION_ROLES = {
    "shadow.calculate": frozenset({"admin", "judge"}),
    "shadow.receipt.lookup": frozenset({"admin", "judge"}),
    "shadow.status.read": frozenset({"admin", "judge"}),
    "shadow.outcome.apply": frozenset({"admin", "judge", "system-adapter"}),
    "shadow.mirror.replay": frozenset({"admin"}),
    "shadow.drift.read": frozenset({"admin", "judge"}),
}
_ROLE_ACTIONS = SHADOW_ACTION_ROLES
_ALLOWED_ROLES = frozenset({role for roles in _ROLE_ACTIONS.values() for role in roles})
_ATTESTATION_FIELDS = frozenset(
    {
        "schema_version",
        "consumer_id",
        "actor_id",
        "roles",
        "action",
        "subject_revision",
        "request_digest_schema_version",
        "request_digest",
        "audience",
        "nonce",
        "issued_at",
        "expires_at",
    }
)


class NonceLedger(Protocol):
    def claim_actor_attestation_nonce(
        self,
        *,
        consumer_id: str,
        nonce: str,
        actor_id: str,
        action: str,
        subject_revision: str,
        expires_at: int,
    ) -> bool: ...


class ShadowAuthenticationError(ValueError):
    """Authentication failed without exposing credential or assertion material."""


class ShadowAuthorizationError(ValueError):
    """Authenticated caller or actor is not authorized for the requested action."""


class ShadowAttestationReplayError(ValueError):
    """A previously claimed delegated-actor assertion was presented again."""


class ShadowAuthenticationConfigurationError(RuntimeError):
    """The server does not have a valid scoped credential/key configuration."""


@dataclass(frozen=True)
class VerifiedActorAttestation:
    consumer_id: str
    actor_id: str
    roles: tuple[str, ...]
    action: str
    subject_revision: str
    request_digest_schema_version: str
    request_digest: str
    audience: str
    nonce: str
    issued_at: int
    expires_at: int


def verify_shadow_action(
    *,
    authorization: str | None,
    encoded_attestation: str | None,
    expected_consumer_id: str,
    expected_actor_id: str | None,
    expected_action: str,
    expected_subject_revision: str,
    expected_request_digest: str,
    ledger: NonceLedger,
    now: int | None = None,
) -> VerifiedActorAttestation:
    """Authenticate a scoped service, verify its actor assertion, and claim nonce."""

    consumer = validate_namespaced_identity(expected_consumer_id, "consumer_id")
    subject_revision = validate_namespaced_identity(expected_subject_revision, "subject_revision")
    if expected_action not in _ROLE_ACTIONS:
        raise ShadowAuthorizationError("unsupported shadow action")

    credentials = _load_secret_map(_SERVICE_CREDENTIALS_ENV)
    supplied_token = _bearer_token(authorization)
    authenticated_consumers = {
        configured_consumer
        for configured_consumer, configured_token in credentials.items()
        if hmac.compare_digest(supplied_token, configured_token)
    }
    if not authenticated_consumers:
        raise ShadowAuthenticationError("valid scoped service credential required")
    if consumer not in authenticated_consumers:
        raise ShadowAuthorizationError("service credential is not scoped to this consumer")

    keys = _load_secret_map(_ATTESTATION_KEYS_ENV)
    _validate_secret_configuration(credentials, keys)
    signing_key = keys.get(consumer)
    if not signing_key:
        raise ShadowAuthenticationConfigurationError(
            "actor attestation verification is not configured for this consumer"
        )
    payload = _decode_and_verify(encoded_attestation, signing_key)
    attestation = _validate_attestation(payload, now=now)

    if attestation.consumer_id != consumer:
        raise ShadowAuthorizationError("actor attestation consumer scope does not match")
    if _namespace(attestation.actor_id) != _namespace(consumer):
        raise ShadowAuthorizationError("actor attestation namespace does not match consumer")
    if expected_actor_id is not None:
        actor = validate_namespaced_identity(expected_actor_id, "operator_id")
        if not hmac.compare_digest(attestation.actor_id, actor):
            raise ShadowAuthorizationError("attested actor does not match requested operator")
    if attestation.action != expected_action:
        raise ShadowAuthorizationError("actor attestation action does not match")
    if attestation.subject_revision != subject_revision:
        raise ShadowAuthorizationError("actor attestation subject revision does not match")
    request_digest = _validate_digest(expected_request_digest)
    if not hmac.compare_digest(attestation.request_digest, request_digest):
        raise ShadowAuthorizationError("actor attestation request digest does not match")
    if attestation.audience != SHADOW_ATTESTATION_AUDIENCE:
        raise ShadowAuthorizationError("actor attestation audience does not match")
    permitted_roles = _ROLE_ACTIONS[expected_action]
    if not set(attestation.roles).intersection(permitted_roles):
        raise ShadowAuthorizationError("attested role is not authorized for this action")

    try:
        claimed = ledger.claim_actor_attestation_nonce(
            consumer_id=consumer,
            nonce=attestation.nonce,
            actor_id=attestation.actor_id,
            action=attestation.action,
            subject_revision=attestation.subject_revision,
            expires_at=attestation.expires_at,
        )
    except RuntimeError as exc:
        raise ShadowAuthenticationConfigurationError(
            "durable actor replay protection is unavailable"
        ) from exc
    if not claimed:
        raise ShadowAttestationReplayError("actor attestation nonce was already used")
    return attestation


def preauthenticate_shadow_service(authorization: str | None) -> None:
    """Validate that a bearer belongs to some configured service before app work."""

    credentials = _load_secret_map(_SERVICE_CREDENTIALS_ENV)
    keys = _load_secret_map(_ATTESTATION_KEYS_ENV)
    _validate_secret_configuration(credentials, keys)
    supplied_token = _bearer_token(authorization)
    authenticated = False
    for configured_token in credentials.values():
        authenticated = bool(hmac.compare_digest(supplied_token, configured_token)) | authenticated
    if not authenticated:
        raise ShadowAuthenticationError("valid scoped service credential required")


def sign_actor_attestation(payload: Mapping[str, Any], signing_key: str) -> str:
    """Create the canonical compact assertion used by server-side consumers."""

    if not 16 <= len(signing_key) <= 4096 or not signing_key.isascii():
        raise ValueError("signing_key must contain between 16 and 4096 characters")
    attestation = _validate_attestation(payload, now=None)
    if attestation.audience != SHADOW_ATTESTATION_AUDIENCE:
        raise ValueError("actor attestation audience does not match")
    canonical = json.dumps(
        dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    encoded = _b64encode(canonical)
    signature = hmac.new(signing_key.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256)
    return f"{encoded}.{_b64encode(signature.digest())}"


def canonical_shadow_request_digest(payload: Mapping[str, Any]) -> str:
    """Hash one JSON-compatible request under the frozen versioned envelope."""

    if not isinstance(payload, Mapping):
        raise ValueError("shadow request payload must be a mapping")
    envelope = {
        "schema_version": REQUEST_DIGEST_SCHEMA_VERSION,
        "payload": _normalize_canonical_json(dict(payload)),
    }
    try:
        canonical = json.dumps(
            envelope,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("shadow request payload must be canonical JSON") from exc
    return hashlib.sha256(canonical).hexdigest()


def _normalize_canonical_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("shadow request object keys must be strings")
        return {key: _normalize_canonical_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_canonical_json(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("shadow request payload must contain finite numbers")
        return int(value) if value.is_integer() else value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise ValueError("shadow request payload must be canonical JSON")


def shadow_auth_configuration_status() -> str:
    """Return configured/not-configured/invalid without exposing secret metadata."""

    if not os.environ.get(_SERVICE_CREDENTIALS_ENV) and not os.environ.get(_ATTESTATION_KEYS_ENV):
        return "not-configured"
    try:
        credentials = _load_secret_map(_SERVICE_CREDENTIALS_ENV)
        keys = _load_secret_map(_ATTESTATION_KEYS_ENV)
    except ShadowAuthenticationConfigurationError:
        return "invalid"
    try:
        _validate_secret_configuration(credentials, keys)
    except ShadowAuthenticationConfigurationError:
        return "invalid"
    return "configured"


def _validate_secret_configuration(credentials: Mapping[str, str], keys: Mapping[str, str]) -> None:
    credential_values = set(credentials.values())
    key_values = set(keys.values())
    if (
        set(credentials) != set(keys)
        or len(credential_values) != len(credentials)
        or len(key_values) != len(keys)
        or not credential_values.isdisjoint(key_values)
    ):
        raise ShadowAuthenticationConfigurationError(
            "shadow authentication configuration is invalid"
        )


def _load_secret_map(name: str) -> dict[str, str]:
    raw = os.environ.get(name, "")
    if not raw:
        raise ShadowAuthenticationConfigurationError("shadow authentication is not configured")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ShadowAuthenticationConfigurationError(
            "shadow authentication configuration is invalid"
        ) from exc
    if not isinstance(value, dict) or not value:
        raise ShadowAuthenticationConfigurationError(
            "shadow authentication configuration is invalid"
        )
    result: dict[str, str] = {}
    for consumer, secret in value.items():
        try:
            key = validate_namespaced_identity(consumer, "configured consumer_id")
        except ValueError as exc:
            raise ShadowAuthenticationConfigurationError(
                "shadow authentication configuration is invalid"
            ) from exc
        if not isinstance(secret, str) or not 16 <= len(secret) <= 4096 or not secret.isascii():
            raise ShadowAuthenticationConfigurationError(
                "shadow authentication configuration is invalid"
            )
        result[key] = secret
    return result


def _bearer_token(value: str | None) -> str:
    text = str(value or "")
    if not text.startswith("Bearer "):
        raise ShadowAuthenticationError("valid scoped service credential required")
    token = text[7:]
    if not 16 <= len(token) <= 4096 or not token.isascii():
        raise ShadowAuthenticationError("valid scoped service credential required")
    return token


def _decode_and_verify(encoded_attestation: str | None, signing_key: str) -> Mapping[str, Any]:
    text = str(encoded_attestation or "")
    if not 32 <= len(text) <= 8192 or text.count(".") != 1:
        raise ShadowAuthenticationError("valid actor attestation required")
    encoded_payload, encoded_signature = text.split(".", 1)
    try:
        payload_bytes = _b64decode(encoded_payload)
        supplied_signature = _b64decode(encoded_signature)
    except (ValueError, binascii.Error) as exc:
        raise ShadowAuthenticationError("valid actor attestation required") from exc
    expected_signature = hmac.new(
        signing_key.encode("utf-8"), encoded_payload.encode("ascii"), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise ShadowAuthenticationError("valid actor attestation required")
    try:
        payload = json.loads(payload_bytes)
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise ShadowAuthenticationError("valid actor attestation required") from exc
    if not isinstance(payload, dict):
        raise ShadowAuthenticationError("valid actor attestation required")
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    if not hmac.compare_digest(encoded_payload, _b64encode(canonical)):
        raise ShadowAuthenticationError("valid actor attestation required")
    return payload


def _validate_attestation(
    payload: Mapping[str, Any], *, now: int | None
) -> VerifiedActorAttestation:
    if set(payload) != _ATTESTATION_FIELDS:
        raise ShadowAuthenticationError("valid actor attestation required")
    if payload.get("schema_version") != ACTOR_ATTESTATION_SCHEMA_VERSION:
        raise ShadowAuthenticationError("valid actor attestation required")
    try:
        consumer = validate_namespaced_identity(payload.get("consumer_id"), "consumer_id")
        actor = validate_namespaced_identity(payload.get("actor_id"), "actor_id")
        revision = validate_namespaced_identity(payload.get("subject_revision"), "subject_revision")
        request_digest = _validate_digest(payload.get("request_digest"))
    except ValueError as exc:
        raise ShadowAuthenticationError("valid actor attestation required") from exc
    roles = payload.get("roles")
    if (
        not isinstance(roles, list)
        or not 1 <= len(roles) <= 8
        or any(not isinstance(role, str) or role not in _ALLOWED_ROLES for role in roles)
        or len(set(roles)) != len(roles)
    ):
        raise ShadowAuthenticationError("valid actor attestation required")
    action = payload.get("action")
    request_digest_schema = payload.get("request_digest_schema_version")
    audience = payload.get("audience")
    nonce = payload.get("nonce")
    if not isinstance(action, str) or action not in _ROLE_ACTIONS:
        raise ShadowAuthenticationError("valid actor attestation required")
    if request_digest_schema != REQUEST_DIGEST_SCHEMA_VERSION:
        raise ShadowAuthenticationError("valid actor attestation required")
    if not isinstance(audience, str) or not 1 <= len(audience) <= 128:
        raise ShadowAuthenticationError("valid actor attestation required")
    if not isinstance(nonce, str) or not _NONCE.fullmatch(nonce):
        raise ShadowAuthenticationError("valid actor attestation required")
    issued_at = payload.get("issued_at")
    expires_at = payload.get("expires_at")
    if (
        not isinstance(issued_at, int)
        or isinstance(issued_at, bool)
        or not isinstance(expires_at, int)
        or isinstance(expires_at, bool)
    ):
        raise ShadowAuthenticationError("valid actor attestation required")
    current = int(time.time()) if now is None else int(now)
    if issued_at > current + MAX_ATTESTATION_CLOCK_SKEW_SECONDS:
        raise ShadowAuthenticationError("actor attestation is not yet valid")
    if expires_at <= current:
        raise ShadowAuthenticationError("actor attestation has expired")
    if expires_at <= issued_at or expires_at - issued_at > MAX_ATTESTATION_LIFETIME_SECONDS:
        raise ShadowAuthenticationError("actor attestation lifetime is invalid")
    return VerifiedActorAttestation(
        consumer_id=consumer,
        actor_id=actor,
        roles=tuple(roles),
        action=action,
        subject_revision=revision,
        request_digest_schema_version=request_digest_schema,
        request_digest=request_digest,
        audience=audience,
        nonce=nonce,
        issued_at=issued_at,
        expires_at=expires_at,
    )


def _namespace(value: str) -> str:
    return value.split(":", 1)[0]


def _validate_digest(value: Any) -> str:
    text = str(value or "")
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        raise ValueError("request_digest must be a lowercase SHA-256 digest")
    return text


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("invalid base64url")
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)


__all__ = [
    "ACTOR_ATTESTATION_SCHEMA_VERSION",
    "MAX_ATTESTATION_LIFETIME_SECONDS",
    "REQUEST_DIGEST_SCHEMA_VERSION",
    "SHADOW_ACTION_ROLES",
    "SHADOW_ATTESTATION_AUDIENCE",
    "ShadowAttestationReplayError",
    "ShadowAuthenticationConfigurationError",
    "ShadowAuthenticationError",
    "ShadowAuthorizationError",
    "VerifiedActorAttestation",
    "canonical_shadow_request_digest",
    "shadow_auth_configuration_status",
    "preauthenticate_shadow_service",
    "sign_actor_attestation",
    "verify_shadow_action",
]
