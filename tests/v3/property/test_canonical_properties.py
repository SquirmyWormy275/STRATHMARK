from __future__ import annotations

import unicodedata
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from strathmark.v3.contracts.canonical import (
    INT64_MAX,
    INT64_MIN,
    canonical_bytes,
    canonical_decimal_string,
    canonical_digest,
    milliseconds_from_seconds,
)

_PROPERTY_SETTINGS = settings(max_examples=100, deadline=None, derandomize=True, database=None)
_SAFE_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    min_size=0,
    max_size=64,
)
_JSON_SCALAR = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=INT64_MIN, max_value=INT64_MAX),
    _SAFE_TEXT,
)
_JSON_VALUE = st.recursive(
    _JSON_SCALAR,
    lambda children: st.one_of(
        st.lists(children, max_size=8),
        st.dictionaries(
            st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=12),
            children,
            max_size=8,
        ),
    ),
    max_leaves=30,
)


@_PROPERTY_SETTINGS
@given(
    st.dictionaries(
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=12),
        _JSON_VALUE,
        max_size=12,
    )
)
def test_mapping_order_never_changes_canonical_bytes_or_digest(payload: dict[str, object]) -> None:
    reversed_payload = dict(reversed(tuple(payload.items())))

    assert canonical_bytes(payload) == canonical_bytes(reversed_payload)
    assert canonical_digest(payload) == canonical_digest(reversed_payload)


@_PROPERTY_SETTINGS
@given(_SAFE_TEXT)
def test_unicode_has_deterministic_normalized_utf8_bytes(value: str) -> None:
    normalized = unicodedata.normalize("NFC", value)
    original_bytes = canonical_bytes({"value": value})
    normalized_bytes = canonical_bytes({"value": normalized})

    assert original_bytes == normalized_bytes
    assert original_bytes.decode("utf-8")
    assert canonical_digest({"value": value}) == canonical_digest({"value": normalized})


@_PROPERTY_SETTINGS
@given(st.integers(min_value=INT64_MIN, max_value=INT64_MAX))
def test_signed_integer_canonicalization_is_deterministic(value: int) -> None:
    payload = {"value": value}

    assert canonical_bytes(payload) == canonical_bytes(payload)
    assert canonical_digest(payload) == canonical_digest(payload)


@_PROPERTY_SETTINGS
@given(
    st.decimals(
        min_value=Decimal("0"),
        max_value=Decimal("1000000"),
        allow_nan=False,
        allow_infinity=False,
        places=6,
    )
)
def test_decimal_and_millisecond_quantization_are_repeatable(value: Decimal) -> None:
    rendered = canonical_decimal_string(value)
    milliseconds = milliseconds_from_seconds(value)

    assert canonical_decimal_string(Decimal(rendered)) == rendered
    assert milliseconds_from_seconds(value) == milliseconds
    assert canonical_bytes({"elapsed_ms": milliseconds, "seconds": rendered}) == canonical_bytes(
        {"seconds": rendered, "elapsed_ms": milliseconds}
    )


@_PROPERTY_SETTINGS
@given(
    st.integers(min_value=-1_000_000_000, max_value=1_000_000_000).map(lambda value: value / 1000.0)
)
def test_finite_float_decimal_projection_is_deterministic(value: float) -> None:
    rendered = canonical_decimal_string(value)

    assert canonical_decimal_string(value) == rendered
    assert canonical_bytes({"value": value}) == canonical_bytes({"value": rendered})
