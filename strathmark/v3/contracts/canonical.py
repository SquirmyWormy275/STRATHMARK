"""Bounded deterministic serialization and numeric-unit primitives.

The V3 boundary intentionally accepts a smaller value language than Python or
JSON.  Times cross the boundary as signed-64-bit integer milliseconds; decimal
values cross as explicit normalized strings.  Raw finite floats are converted
to those strings, never emitted as ambient-runtime JSON numbers.
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any

from strathmark.v3.contracts.errors import (
    CanonicalBoundsError,
    CanonicalNumberError,
    CanonicalTypeError,
)

CANONICALIZATION_VERSION = "strathmark-v3-canonical-json-v1"
TIME_QUANTUM_MILLISECONDS = 1
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
DEFAULT_MAX_DEPTH = 32
DEFAULT_MAX_BYTES = 1_048_576
DEFAULT_MAX_ITEMS = 100_000
MAX_DECIMAL_CHARACTERS = 128


def canonical_decimal_string(value: Decimal | int | float | str) -> str:
    """Return one finite, non-exponent decimal spelling, normalizing negative zero."""

    if isinstance(value, bool):
        raise CanonicalTypeError("boolean values cannot be used as canonical numbers")
    if isinstance(value, int):
        decimal_value = Decimal(value)
    elif isinstance(value, Decimal):
        decimal_value = value
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalNumberError("canonical numbers must be finite")
        decimal_value = Decimal(str(value))
    elif isinstance(value, str):
        if not value or value.strip() != value:
            raise CanonicalNumberError("canonical decimal strings must be nonempty and trimmed")
        try:
            decimal_value = Decimal(value)
        except InvalidOperation as exc:
            raise CanonicalNumberError("canonical decimal strings must be finite decimals") from exc
    else:
        raise CanonicalTypeError(
            f"canonical number contains unsupported type {type(value).__name__}"
        )

    if not decimal_value.is_finite():
        raise CanonicalNumberError("canonical numbers must be finite")
    if decimal_value.is_zero():
        return "0"

    rendered = format(decimal_value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    if len(rendered) > MAX_DECIMAL_CHARACTERS:
        raise CanonicalBoundsError("canonical decimal exceeds the character bound")
    return rendered


def milliseconds_from_seconds(value: Decimal | int | float | str) -> int:
    """Quantize non-negative seconds to integer milliseconds using half-even ties."""

    if isinstance(value, bool):
        raise CanonicalTypeError("boolean values cannot cross integer time boundaries")
    try:
        decimal_value = Decimal(canonical_decimal_string(value))
    except InvalidOperation as exc:  # pragma: no cover - guarded by canonical_decimal_string
        raise CanonicalNumberError("time must be a finite decimal") from exc
    if decimal_value < 0:
        raise CanonicalNumberError("time in seconds must be non-negative")
    quantized = (decimal_value * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)
    milliseconds = int(quantized)
    _validate_int64(milliseconds)
    return milliseconds


def canonical_expected_versions(versions: Mapping[str, int]) -> tuple[tuple[str, int], ...]:
    """Validate and sort one multi-aggregate expected-version map."""

    if not isinstance(versions, Mapping):
        raise CanonicalTypeError("expected versions must be a mapping")
    normalized: list[tuple[str, int]] = []
    from strathmark.v3.contracts.identifiers import require_identifier

    for aggregate_id, version in versions.items():
        identifier = require_identifier(aggregate_id)
        if isinstance(version, bool):
            raise CanonicalTypeError("boolean values cannot be expected versions")
        if not isinstance(version, int):
            raise CanonicalTypeError("expected versions must be integers")
        if version < 0:
            raise CanonicalBoundsError("expected versions must be non-negative")
        _validate_int64(version)
        normalized.append((str(identifier), version))
    normalized.sort(key=lambda item: item[0])
    return tuple(normalized)


def canonical_bytes(
    value: Any,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> bytes:
    """Serialize a bounded closed value tree to deterministic UTF-8 JSON bytes."""

    _require_positive_bound(max_depth, "max_depth")
    _require_positive_bound(max_bytes, "max_bytes")
    _require_positive_bound(max_items, "max_items")
    item_count = [0]
    normalized = _normalize(
        value,
        depth=0,
        max_depth=max_depth,
        max_items=max_items,
        item_count=item_count,
        ancestors=set(),
    )
    try:
        encoded = json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (UnicodeEncodeError, ValueError) as exc:
        raise CanonicalTypeError("canonical strings must be valid UTF-8 values") from exc
    if len(encoded) > max_bytes:
        raise CanonicalBoundsError(
            f"canonical payload exceeds maximum bytes ({len(encoded)} > {max_bytes})"
        )
    return encoded


def canonical_digest(value: Any, **bounds: int) -> str:
    """Return the lower-case SHA-256 digest of canonical V3 bytes."""

    return hashlib.sha256(canonical_bytes(value, **bounds)).hexdigest()


def _normalize(
    value: Any,
    *,
    depth: int,
    max_depth: int,
    max_items: int,
    item_count: list[int],
    ancestors: set[int],
) -> Any:
    if depth > max_depth:
        raise CanonicalBoundsError("canonical payload exceeds maximum depth")
    item_count[0] += 1
    if item_count[0] > max_items:
        raise CanonicalBoundsError("canonical payload exceeds maximum item count")

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        _validate_int64(value)
        return value
    if isinstance(value, (Decimal, float)):
        return canonical_decimal_string(value)
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)

    if isinstance(value, Mapping):
        return _normalize_mapping(
            value,
            depth=depth,
            max_depth=max_depth,
            max_items=max_items,
            item_count=item_count,
            ancestors=ancestors,
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        identity = id(value)
        if identity in ancestors:
            raise CanonicalTypeError("canonical payload contains a reference cycle")
        ancestors.add(identity)
        try:
            return [
                _normalize(
                    item,
                    depth=depth + 1,
                    max_depth=max_depth,
                    max_items=max_items,
                    item_count=item_count,
                    ancestors=ancestors,
                )
                for item in value
            ]
        finally:
            ancestors.remove(identity)

    raise CanonicalTypeError(f"canonical payload contains unsupported type {type(value).__name__}")


def _normalize_mapping(
    value: Mapping[Any, Any],
    *,
    depth: int,
    max_depth: int,
    max_items: int,
    item_count: list[int],
    ancestors: set[int],
) -> dict[str, Any]:
    identity = id(value)
    if identity in ancestors:
        raise CanonicalTypeError("canonical payload contains a reference cycle")
    ancestors.add(identity)
    try:
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalTypeError("canonical mapping keys must be strings")
            normalized_key = unicodedata.normalize("NFC", key)
            if normalized_key in normalized:
                raise CanonicalTypeError(
                    "canonical mapping keys collide after Unicode normalization"
                )
            normalized[normalized_key] = _normalize(
                item,
                depth=depth + 1,
                max_depth=max_depth,
                max_items=max_items,
                item_count=item_count,
                ancestors=ancestors,
            )
        return normalized
    finally:
        ancestors.remove(identity)


def _validate_int64(value: int) -> None:
    if value < INT64_MIN or value > INT64_MAX:
        raise CanonicalBoundsError("canonical integers must fit signed 64-bit boundaries")


def _require_positive_bound(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CanonicalBoundsError(f"{label} must be a positive integer")


__all__ = [
    "CANONICALIZATION_VERSION",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_ITEMS",
    "INT64_MAX",
    "INT64_MIN",
    "TIME_QUANTUM_MILLISECONDS",
    "canonical_bytes",
    "canonical_decimal_string",
    "canonical_digest",
    "canonical_expected_versions",
    "milliseconds_from_seconds",
]
