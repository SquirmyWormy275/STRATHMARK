from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal, localcontext

import pytest

from strathmark.v3.composition import V3RuntimeConfig, resolve_runtime_config
from strathmark.v3.contracts.canonical import (
    INT64_MAX,
    INT64_MIN,
    canonical_bytes,
    canonical_decimal_string,
    canonical_digest,
    canonical_expected_versions,
    milliseconds_from_seconds,
)
from strathmark.v3.contracts.errors import (
    CanonicalBoundsError,
    CanonicalNumberError,
    CanonicalTypeError,
    ConfigurationError,
    IdentifierError,
)
from strathmark.v3.contracts.identifiers import (
    IdempotencyKey,
    StableIdentifier,
    deterministic_identifier,
    identifier_namespace,
    require_idempotency_key,
    require_identifier,
)


def test_canonical_bytes_are_utf8_and_independent_of_key_order() -> None:
    left = {"z": [1, 2], "a": "café"}
    right = {"a": "café", "z": [1, 2]}

    expected = b'{"a":"caf\xc3\xa9","z":[1,2]}'
    assert canonical_bytes(left) == expected
    assert canonical_bytes(right) == expected
    assert canonical_digest(left) == (
        "8e4914d8de6e7cd6d582a82e617a2e88258545e93bf41a95e66cd030026324d1"
    )


def test_unicode_is_normalized_before_digesting() -> None:
    composed = {"competitor": "Jos\N{LATIN SMALL LETTER E WITH ACUTE}"}
    decomposed = {"competitor": "Jose\N{COMBINING ACUTE ACCENT}"}

    assert canonical_bytes(composed) == canonical_bytes(decomposed)
    assert canonical_digest(composed) == canonical_digest(decomposed)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        ("0", 0),
        ("1.2344", 1234),
        ("1.2345", 1234),
        ("1.2355", 1236),
        (Decimal("90.001"), 90001),
        (-0.0, 0),
    ],
)
def test_seconds_use_one_millisecond_half_even_quantization(
    seconds: str | Decimal | float, expected: int
) -> None:
    assert milliseconds_from_seconds(seconds) == expected


def test_millisecond_quantization_ignores_ambient_decimal_context() -> None:
    expected = milliseconds_from_seconds("123.4565")

    with localcontext() as context:
        context.prec = 2
        context.rounding = "ROUND_UP"
        observed = milliseconds_from_seconds("123.4565")

    assert observed == expected == 123456


def test_negative_zero_has_one_canonical_decimal_representation() -> None:
    assert canonical_decimal_string(-0.0) == "0"
    assert canonical_decimal_string(Decimal("-0.000")) == "0"
    assert canonical_decimal_string("1.2300") == "1.23"


def test_canonical_decimal_accepts_integer_and_rejects_invalid_forms() -> None:
    assert canonical_decimal_string(12) == "12"
    assert canonical_bytes({"decimal": Decimal("1.250"), "float": 2.5}) == (
        b'{"decimal":"1.25","float":"2.5"}'
    )
    with pytest.raises(CanonicalTypeError, match="boolean"):
        canonical_decimal_string(False)
    with pytest.raises(CanonicalNumberError, match="trimmed"):
        canonical_decimal_string(" 1.2")
    with pytest.raises(CanonicalNumberError, match="finite decimals"):
        canonical_decimal_string("not-a-number")
    with pytest.raises(CanonicalNumberError, match="finite"):
        canonical_decimal_string(Decimal("NaN"))
    with pytest.raises(CanonicalTypeError, match="unsupported"):
        canonical_decimal_string(object())  # type: ignore[arg-type]
    with pytest.raises(CanonicalBoundsError, match="character"):
        canonical_decimal_string("1e200")


def test_millisecond_boundary_rejects_negative_and_overflowing_time() -> None:
    with pytest.raises(CanonicalNumberError, match="non-negative"):
        milliseconds_from_seconds("-0.001")
    with pytest.raises(CanonicalBoundsError, match="signed 64-bit"):
        milliseconds_from_seconds(str(INT64_MAX))


def test_signed_64_bit_integer_boundaries_are_frozen() -> None:
    payload = {"minimum": INT64_MIN, "maximum": INT64_MAX}

    assert canonical_bytes(payload) == (
        b'{"maximum":9223372036854775807,"minimum":-9223372036854775808}'
    )
    with pytest.raises(CanonicalBoundsError, match="signed 64-bit"):
        canonical_bytes({"too_large": INT64_MAX + 1})
    with pytest.raises(CanonicalBoundsError, match="signed 64-bit"):
        canonical_bytes({"too_small": INT64_MIN - 1})


def test_expected_versions_are_validated_and_sorted() -> None:
    assert canonical_expected_versions({"field:z": 4, "field:a": 2}) == (
        ("field:a", 2),
        ("field:z", 4),
    )

    with pytest.raises(CanonicalTypeError, match="boolean"):
        canonical_expected_versions({"field:a": True})
    with pytest.raises(CanonicalBoundsError, match="non-negative"):
        canonical_expected_versions({"field:a": -1})
    with pytest.raises(CanonicalTypeError, match="mapping"):
        canonical_expected_versions([])  # type: ignore[arg-type]
    with pytest.raises(CanonicalTypeError, match="integers"):
        canonical_expected_versions({"field:a": "1"})  # type: ignore[dict-item]
    with pytest.raises(CanonicalBoundsError, match="signed 64-bit"):
        canonical_expected_versions({"field:a": INT64_MAX + 1})


def test_time_zero_and_expected_version_fixture_has_frozen_bytes_and_digest() -> None:
    payload = {
        "zero": canonical_decimal_string(-0.0),
        "elapsed_ms": milliseconds_from_seconds("1.2345"),
        "expected_versions": canonical_expected_versions({"field:z": 4, "field:a": 2}),
    }
    expected = b'{"elapsed_ms":1234,"expected_versions":[["field:a",2],["field:z",4]],"zero":"0"}'

    assert canonical_bytes(payload) == expected
    assert canonical_digest(payload) == (
        "26c08fb1554f8450a634cb8e8155c9a760a49fa796de47f9065a84d2a8a20f4c"
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_rejected(value: float) -> None:
    with pytest.raises(CanonicalNumberError, match="finite"):
        canonical_decimal_string(value)
    with pytest.raises(CanonicalNumberError, match="finite"):
        canonical_bytes({"value": value})


def test_boolean_cannot_cross_integer_numeric_boundaries() -> None:
    with pytest.raises(CanonicalTypeError, match="boolean"):
        milliseconds_from_seconds(True)


def test_unknown_and_non_string_mapping_types_fail_closed() -> None:
    with pytest.raises(CanonicalTypeError, match="unsupported"):
        canonical_bytes({"value": object()})
    with pytest.raises(CanonicalTypeError, match="mapping keys"):
        canonical_bytes({1: "not-a-string-key"})


def test_canonical_depth_and_encoded_size_are_bounded() -> None:
    nested: object = "leaf"
    for _ in range(5):
        nested = [nested]

    with pytest.raises(CanonicalBoundsError, match="depth"):
        canonical_bytes(nested, max_depth=4)
    with pytest.raises(CanonicalBoundsError, match="bytes"):
        canonical_bytes({"payload": "abcdef"}, max_bytes=8)
    with pytest.raises(CanonicalBoundsError, match="item count"):
        canonical_bytes([1, 2], max_items=2)


@pytest.mark.parametrize(
    ("keyword", "value"),
    [("max_depth", 0), ("max_bytes", True), ("max_items", "10")],
)
def test_canonical_bounds_must_be_positive_integers(keyword: str, value: object) -> None:
    with pytest.raises(CanonicalBoundsError, match="positive integer"):
        canonical_bytes({}, **{keyword: value})  # type: ignore[arg-type]


def test_canonical_cycles_and_unicode_key_collisions_fail_closed() -> None:
    sequence: list[object] = []
    sequence.append(sequence)
    with pytest.raises(CanonicalTypeError, match="reference cycle"):
        canonical_bytes(sequence)

    mapping: dict[str, object] = {}
    mapping["self"] = mapping
    with pytest.raises(CanonicalTypeError, match="reference cycle"):
        canonical_bytes(mapping)

    with pytest.raises(CanonicalTypeError, match="collide"):
        canonical_bytes({"é": 1, "e\N{COMBINING ACUTE ACCENT}": 2})


def test_invalid_unicode_and_byte_sequences_fail_closed() -> None:
    with pytest.raises(CanonicalTypeError, match="UTF-8"):
        canonical_bytes({"value": "\ud800"})
    with pytest.raises(CanonicalTypeError, match="unsupported"):
        canonical_bytes(b"bytes-are-not-json")


def test_null_boolean_and_tuple_have_frozen_representations() -> None:
    assert canonical_bytes({"false": False, "null": None, "tuple": (1, 2)}) == (
        b'{"false":false,"null":null,"tuple":[1,2]}'
    )


@pytest.mark.parametrize(
    "value",
    [
        "",
        "field",
        "Field:abc",
        "field:has space",
        "field:-starts-with-dash",
        "a" * 33 + ":abc",
    ],
)
def test_malformed_identifiers_have_a_stable_typed_error(value: str) -> None:
    with pytest.raises(IdentifierError, match="namespaced identifier"):
        require_identifier(value)


def test_identifiers_are_frozen_and_deterministic() -> None:
    identifier = StableIdentifier("field:abc-123")
    assert str(identifier) == "field:abc-123"
    with pytest.raises(FrozenInstanceError):
        identifier.value = "field:changed"  # type: ignore[misc]

    first = deterministic_identifier("receipt", {"field": "field:abc-123", "revision": 4})
    second = deterministic_identifier("receipt", {"revision": 4, "field": "field:abc-123"})
    assert first == second
    assert str(first).startswith("receipt:")


def test_identifier_helpers_keep_stable_and_idempotency_types_distinct() -> None:
    stable = StableIdentifier("field:abc")
    assert require_identifier(stable) is stable
    assert require_identifier(stable, expected_namespace="field") is stable
    assert stable.namespace == "field"
    with pytest.raises(IdentifierError, match="namespace 'round'"):
        require_identifier(stable, expected_namespace="round")

    key = IdempotencyKey("command:req-1")
    assert str(key) == "command:req-1"
    assert key.namespace == "command"
    assert require_idempotency_key(key) is key
    assert require_idempotency_key("command:req-2") == IdempotencyKey("command:req-2")
    assert identifier_namespace(key) == "command"
    assert identifier_namespace(stable) == "field"
    with pytest.raises(IdentifierError, match="lower-case"):
        deterministic_identifier("Bad Namespace", {})
    with pytest.raises(IdentifierError, match="lower-case"):
        require_identifier(stable, expected_namespace="BAD")


def test_runtime_configuration_is_an_immutable_snapshot(tmp_path) -> None:
    environment = {
        "STRATHMARK_V3_DB_PATH": str(tmp_path / "v3.sqlite3"),
        "STRATHMARK_V3_TEMP_PATH": str(tmp_path / "runtime"),
        "STRATHMARK_V3_BLOB_ROOT": str(tmp_path / "blobs"),
        "STRATHMARK_V3_BUNDLE_ROOT": str(tmp_path / "bundles"),
        "STRATHMARK_V3_ARCHIVE_ROOT": str(tmp_path / "archive"),
        "STRATHMARK_V3_BACKUP_ROOT": str(tmp_path / "backup"),
        "STRATHMARK_V3_RECOVERY_ROOT": str(tmp_path / "recovery"),
        "STRATHMARK_V3_INTEGRITY_KEY_ROOT": str(tmp_path / "integrity-keys"),
        "STRATHMARK_TEST_DB": "1",
    }

    config = resolve_runtime_config(environment)

    assert isinstance(config, V3RuntimeConfig)
    assert config.test_mode is True
    assert config.database_path == (tmp_path / "v3.sqlite3").resolve()
    assert config.blob_root == (tmp_path / "blobs").resolve()
    assert config.bundle_root == (tmp_path / "bundles").resolve()
    assert config.archive_root == (tmp_path / "archive").resolve()
    assert config.backup_root == (tmp_path / "backup").resolve()
    assert config.recovery_root == (tmp_path / "recovery").resolve()
    assert config.integrity_key_root == (tmp_path / "integrity-keys").resolve()
    with pytest.raises(FrozenInstanceError):
        config.test_mode = False  # type: ignore[misc]


def test_runtime_configuration_can_resolve_current_environment(monkeypatch) -> None:
    monkeypatch.setenv("STRATHMARK_V3_CANONICAL_MAX_BYTES", "2048")
    monkeypatch.setenv("STRATHMARK_V3_CANONICAL_MAX_DEPTH", "8")

    config = resolve_runtime_config()

    assert config.test_mode is True
    assert config.canonical_max_bytes == 2048
    assert config.canonical_max_depth == 8


def test_runtime_configuration_supports_explicit_non_test_defaults(monkeypatch) -> None:
    monkeypatch.delenv("STRATHMARK_TEST_DB", raising=False)
    monkeypatch.delenv("STRATHMARK_V3_DB_PATH", raising=False)
    monkeypatch.delenv("STRATHMARK_V3_TEMP_PATH", raising=False)
    monkeypatch.delenv("STRATHMARK_V3_BLOB_ROOT", raising=False)
    monkeypatch.delenv("STRATHMARK_V3_BUNDLE_ROOT", raising=False)
    monkeypatch.delenv("STRATHMARK_V3_ARCHIVE_ROOT", raising=False)
    monkeypatch.delenv("STRATHMARK_V3_BACKUP_ROOT", raising=False)
    monkeypatch.delenv("STRATHMARK_V3_RECOVERY_ROOT", raising=False)
    monkeypatch.delenv("STRATHMARK_V3_INTEGRITY_KEY_ROOT", raising=False)

    config = resolve_runtime_config()

    assert config.test_mode is False
    assert config.database_path.name == "strathmark-v3.sqlite3"


@pytest.mark.parametrize(
    "database_path",
    [
        "C:/Users/example/.strathmark/results.db",
        "C:/runtime/production/strathmark-v3.sqlite3",
        "C:/runtime/iordtvxryrdhqvdkfgzf.sqlite3",
    ],
)
def test_test_mode_rejects_known_production_database_identifiers(database_path: str) -> None:
    with pytest.raises(ConfigurationError, match="production"):
        resolve_runtime_config(
            {
                "STRATHMARK_V3_DB_PATH": database_path,
                "STRATHMARK_V3_TEMP_PATH": "C:/runtime/isolated-test-temp",
                "STRATHMARK_TEST_DB": "1",
            }
        )


@pytest.mark.parametrize("flag", ["maybe", "2"])
def test_runtime_configuration_rejects_ambiguous_test_flags(tmp_path, flag: str) -> None:
    with pytest.raises(ConfigurationError, match="boolean flag"):
        resolve_runtime_config(
            {
                "STRATHMARK_V3_DB_PATH": str(tmp_path / "test.sqlite3"),
                "STRATHMARK_V3_TEMP_PATH": str(tmp_path / "temp"),
                "STRATHMARK_TEST_DB": flag,
            }
        )


@pytest.mark.parametrize("value", ["0", "-1", "invalid"])
def test_runtime_configuration_rejects_invalid_canonical_bounds(tmp_path, value: str) -> None:
    with pytest.raises(ConfigurationError, match="positive integer"):
        resolve_runtime_config(
            {
                "STRATHMARK_V3_DB_PATH": str(tmp_path / "test.sqlite3"),
                "STRATHMARK_V3_TEMP_PATH": str(tmp_path / "temp"),
                "STRATHMARK_TEST_DB": "1",
                "STRATHMARK_V3_CANONICAL_MAX_BYTES": value,
            }
        )


@pytest.mark.parametrize("value", ["", " path-with-space ", "bad\x00path"])
def test_runtime_configuration_rejects_invalid_paths(tmp_path, value: str) -> None:
    with pytest.raises(ConfigurationError, match="path|characters"):
        resolve_runtime_config(
            {
                "STRATHMARK_V3_DB_PATH": value,
                "STRATHMARK_V3_TEMP_PATH": str(tmp_path / "temp"),
                "STRATHMARK_TEST_DB": "1",
            }
        )


def test_runtime_configuration_wraps_platform_path_resolution_errors(tmp_path, monkeypatch) -> None:
    def fail_resolution(self, strict=False):
        raise OSError("simulated platform path failure")

    monkeypatch.setattr(type(tmp_path), "resolve", fail_resolution)
    with pytest.raises(ConfigurationError, match="valid filesystem path"):
        resolve_runtime_config(
            {
                "STRATHMARK_V3_DB_PATH": "isolated.sqlite3",
                "STRATHMARK_V3_TEMP_PATH": "runtime",
                "STRATHMARK_TEST_DB": "1",
            }
        )


@pytest.mark.parametrize("nested", [False, True])
def test_database_and_temp_paths_must_be_separate(tmp_path, nested: bool) -> None:
    temp_path = tmp_path / "same"
    database_path = temp_path / "v3.sqlite3" if nested else temp_path
    with pytest.raises(ConfigurationError, match="separate"):
        resolve_runtime_config(
            {
                "STRATHMARK_V3_DB_PATH": str(database_path),
                "STRATHMARK_V3_TEMP_PATH": str(temp_path),
                "STRATHMARK_TEST_DB": "1",
            }
        )


def test_test_mode_rejects_production_temporary_path(tmp_path) -> None:
    with pytest.raises(ConfigurationError, match="production"):
        resolve_runtime_config(
            {
                "STRATHMARK_V3_DB_PATH": str(tmp_path / "test.sqlite3"),
                "STRATHMARK_V3_TEMP_PATH": str(tmp_path / "prod"),
                "STRATHMARK_TEST_DB": "1",
            }
        )


@pytest.mark.parametrize(
    "variable",
    [
        "STRATHMARK_V3_BLOB_ROOT",
        "STRATHMARK_V3_BUNDLE_ROOT",
        "STRATHMARK_V3_ARCHIVE_ROOT",
        "STRATHMARK_V3_BACKUP_ROOT",
        "STRATHMARK_V3_RECOVERY_ROOT",
        "STRATHMARK_V3_INTEGRITY_KEY_ROOT",
    ],
)
def test_test_mode_rejects_production_artifact_roots(tmp_path, variable: str) -> None:
    environment = {
        "STRATHMARK_V3_DB_PATH": str(tmp_path / "test.sqlite3"),
        "STRATHMARK_V3_TEMP_PATH": str(tmp_path / "runtime"),
        "STRATHMARK_V3_BLOB_ROOT": str(tmp_path / "blobs"),
        "STRATHMARK_V3_BUNDLE_ROOT": str(tmp_path / "bundles"),
        "STRATHMARK_V3_ARCHIVE_ROOT": str(tmp_path / "archive"),
        "STRATHMARK_V3_BACKUP_ROOT": str(tmp_path / "backup"),
        "STRATHMARK_V3_RECOVERY_ROOT": str(tmp_path / "recovery"),
        "STRATHMARK_V3_INTEGRITY_KEY_ROOT": str(tmp_path / "integrity-keys"),
        "STRATHMARK_TEST_DB": "1",
    }
    environment[variable] = "C:/runtime/production/artifacts"

    with pytest.raises(ConfigurationError, match="production"):
        resolve_runtime_config(environment)


@pytest.mark.parametrize(
    "variable",
    [
        "STRATHMARK_V3_TEMP_PATH",
        "STRATHMARK_V3_BLOB_ROOT",
        "STRATHMARK_V3_BUNDLE_ROOT",
        "STRATHMARK_V3_ARCHIVE_ROOT",
        "STRATHMARK_V3_BACKUP_ROOT",
        "STRATHMARK_V3_RECOVERY_ROOT",
        "STRATHMARK_V3_INTEGRITY_KEY_ROOT",
    ],
)
def test_mutable_runtime_paths_must_be_distinct(tmp_path, variable: str) -> None:
    database_path = str(tmp_path / "v3.sqlite3")
    environment = {
        "STRATHMARK_V3_DB_PATH": database_path,
        "STRATHMARK_V3_TEMP_PATH": str(tmp_path / "runtime"),
        "STRATHMARK_V3_BLOB_ROOT": str(tmp_path / "blobs"),
        "STRATHMARK_V3_BUNDLE_ROOT": str(tmp_path / "bundles"),
        "STRATHMARK_V3_ARCHIVE_ROOT": str(tmp_path / "archive"),
        "STRATHMARK_V3_BACKUP_ROOT": str(tmp_path / "backup"),
        "STRATHMARK_V3_RECOVERY_ROOT": str(tmp_path / "recovery"),
        "STRATHMARK_V3_INTEGRITY_KEY_ROOT": str(tmp_path / "integrity-keys"),
        "STRATHMARK_TEST_DB": "1",
    }
    environment[variable] = database_path

    with pytest.raises(ConfigurationError, match="separate"):
        resolve_runtime_config(environment)
