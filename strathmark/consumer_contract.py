"""Load and verify the frozen STRATHMARK shadow consumer contract.

The OpenAPI document is packaged with the wheel.  Consumers may pin the exposed
SHA-256 digest before scaffolding their adapter, while runtime code can verify that
the installed artifact still matches the reviewed contract.
"""

from __future__ import annotations

import hashlib
import json
import re
from importlib.resources import files
from typing import Any

SHADOW_CONSUMER_CONTRACT_VERSION = "strathmark.shadow-consumer-contract.v1"
EXPECTED_SHADOW_CONSUMER_PATHS = frozenset(
    {
        "/health",
        "/v1/shadow/calculate",
        "/v1/shadow/drift",
        "/v1/shadow/mirror/replay",
        "/v1/shadow/outcomes/apply",
        "/v1/shadow/receipts/lookup",
        "/v1/shadow/status",
    }
)
_CONTRACT_RESOURCE = "contracts/shadow_consumer_v1.openapi.json"
_CHECKSUM_RESOURCE = "contracts/shadow_consumer_v1.openapi.sha256"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ShadowConsumerContractIntegrityError(RuntimeError):
    """The installed consumer contract does not match its frozen checksum."""


def _resource_text(resource_name: str) -> str:
    return files("strathmark").joinpath(resource_name).read_text(encoding="utf-8")


def load_shadow_consumer_contract() -> dict[str, Any]:
    """Return the verified packaged OpenAPI 3.1 consumer contract."""

    try:
        document = json.loads(_resource_text(_CONTRACT_RESOURCE))
    except (OSError, ValueError, TypeError) as exc:
        raise ShadowConsumerContractIntegrityError(
            "Installed shadow consumer contract is missing or malformed."
        ) from exc
    if not isinstance(document, dict):
        raise ShadowConsumerContractIntegrityError(
            "Installed shadow consumer contract must be a JSON object."
        )
    version = (document.get("info") or {}).get("x-strathmark-contract-version")
    if version != SHADOW_CONSUMER_CONTRACT_VERSION:
        raise ShadowConsumerContractIntegrityError(
            "Installed shadow consumer contract has an unsupported version."
        )
    if set(document.get("paths") or {}) != EXPECTED_SHADOW_CONSUMER_PATHS:
        raise ShadowConsumerContractIntegrityError(
            "Installed shadow consumer contract has an unexpected route surface."
        )
    shadow_consumer_contract_digest(document=document)
    return document


def shadow_consumer_contract_bytes(*, document: dict[str, Any] | None = None) -> bytes:
    """Return the deterministic canonical bytes used for contract pinning."""

    if document is None:
        try:
            parsed = json.loads(_resource_text(_CONTRACT_RESOURCE))
        except (OSError, ValueError, TypeError) as exc:
            raise ShadowConsumerContractIntegrityError(
                "Installed shadow consumer contract is missing or malformed."
            ) from exc
    else:
        parsed = document
    return (json.dumps(parsed, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def shadow_consumer_contract_digest(*, document: dict[str, Any] | None = None) -> str:
    """Verify and return the reviewed SHA-256 digest for the installed contract."""

    expected = _resource_text(_CHECKSUM_RESOURCE).strip().lower()
    if not _SHA256.fullmatch(expected):
        raise ShadowConsumerContractIntegrityError(
            "Installed shadow consumer contract checksum is malformed."
        )
    observed = hashlib.sha256(shadow_consumer_contract_bytes(document=document)).hexdigest()
    if observed != expected:
        raise ShadowConsumerContractIntegrityError(
            "Installed shadow consumer contract does not match its reviewed checksum."
        )
    return observed


__all__ = [
    "EXPECTED_SHADOW_CONSUMER_PATHS",
    "SHADOW_CONSUMER_CONTRACT_VERSION",
    "ShadowConsumerContractIntegrityError",
    "load_shadow_consumer_contract",
    "shadow_consumer_contract_bytes",
    "shadow_consumer_contract_digest",
]
