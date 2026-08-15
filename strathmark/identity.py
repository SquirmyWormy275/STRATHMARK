"""Small standard-library identity contract shared by trusted boundaries."""

from __future__ import annotations

import re
from typing import Any

IDENTITY_SCHEMA_VERSION = "strathmark.namespaced-identity.v1"
NAMESPACED_ID_PATTERN = r"^[a-z][a-z0-9_.-]{0,31}:[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,94}$"

_NAMESPACED_ID = re.compile(NAMESPACED_ID_PATTERN)


def validate_namespaced_identity(value: Any, label: str) -> str:
    """Return one bounded namespaced identity or reject it."""

    text = str(value or "").strip()
    if len(text) > 128:
        raise ValueError(f"{label} must be at most 128 characters")
    if not _NAMESPACED_ID.fullmatch(text):
        raise ValueError(f"{label} must be namespaced as 'namespace:value'")
    return text


__all__ = [
    "IDENTITY_SCHEMA_VERSION",
    "NAMESPACED_ID_PATTERN",
    "validate_namespaced_identity",
]
