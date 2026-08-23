"""Closed stable-identity and idempotency primitives for V3 commands."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from strathmark.v3.contracts.errors import IdentifierError

_NAMESPACE = re.compile(r"^[a-z][a-z0-9_.-]{0,31}$")
_NAMESPACED_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,31}:[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,94}$")


@dataclass(frozen=True, slots=True, order=True)
class StableIdentifier:
    """A bounded, explicit-namespace identifier safe for canonical contracts."""

    value: str

    def __post_init__(self) -> None:
        _validate_identifier(self.value)

    @property
    def namespace(self) -> str:
        return self.value.split(":", 1)[0]

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True, order=True)
class IdempotencyKey:
    """A stable command identity kept distinct from aggregate identity by type."""

    value: str

    def __post_init__(self) -> None:
        _validate_identifier(self.value)

    @property
    def namespace(self) -> str:
        return self.value.split(":", 1)[0]

    def __str__(self) -> str:
        return self.value


def require_identifier(
    value: str | StableIdentifier, *, expected_namespace: str | None = None
) -> StableIdentifier:
    """Return a validated stable identifier, optionally bound to one namespace."""

    identifier = value if isinstance(value, StableIdentifier) else StableIdentifier(value)
    if expected_namespace is not None:
        _validate_namespace(expected_namespace)
        if identifier.namespace != expected_namespace:
            raise IdentifierError(
                f"namespaced identifier must use namespace {expected_namespace!r}"
            )
    return identifier


def require_idempotency_key(value: str | IdempotencyKey) -> IdempotencyKey:
    """Return a validated, strongly typed idempotency identity."""

    return value if isinstance(value, IdempotencyKey) else IdempotencyKey(value)


def identifier_namespace(value: str | StableIdentifier | IdempotencyKey) -> str:
    """Return the validated namespace prefix."""

    if isinstance(value, IdempotencyKey):
        return value.namespace
    return require_identifier(value).namespace


def deterministic_identifier(namespace: str, payload: Any) -> StableIdentifier:
    """Derive an opaque SHA-256 identifier from canonical payload bytes."""

    _validate_namespace(namespace)
    from strathmark.v3.contracts.canonical import canonical_bytes

    digest = hashlib.sha256(canonical_bytes(payload)).hexdigest()
    return StableIdentifier(f"{namespace}:{digest}")


def _validate_identifier(value: object) -> None:
    if not isinstance(value, str) or not _NAMESPACED_ID.fullmatch(value):
        raise IdentifierError(
            "value must be a bounded namespaced identifier (for example, 'field:abc-123')"
        )


def _validate_namespace(value: object) -> None:
    if not isinstance(value, str) or not _NAMESPACE.fullmatch(value):
        raise IdentifierError("identifier namespace must be lower-case and bounded")


__all__ = [
    "IdempotencyKey",
    "StableIdentifier",
    "deterministic_identifier",
    "identifier_namespace",
    "require_idempotency_key",
    "require_identifier",
]
