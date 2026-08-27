"""Stable error vocabulary shared by V3 domain contracts."""

from __future__ import annotations


class V3Error(Exception):
    """Base class for failures defined by the V3 contract."""

    code = "v3_error"


class ContractError(V3Error, ValueError):
    """A caller supplied a value outside a closed V3 contract."""

    code = "contract_error"


class CanonicalizationError(ContractError):
    """A value cannot receive the canonical V3 byte representation."""

    code = "canonicalization_error"


class CanonicalTypeError(CanonicalizationError):
    """A value has a type forbidden by canonical V3 JSON."""

    code = "canonical_type_error"


class CanonicalNumberError(CanonicalizationError):
    """A numeric value is non-finite or otherwise non-canonical."""

    code = "canonical_number_error"


class CanonicalBoundsError(CanonicalizationError):
    """A canonical value exceeds a declared size, depth, or numeric bound."""

    code = "canonical_bounds_error"


class IdentifierError(ContractError):
    """A stable or idempotency identifier is malformed."""

    code = "identifier_error"


class ConfigurationError(V3Error, RuntimeError):
    """The immutable V3 runtime configuration is unsafe or incomplete."""

    code = "configuration_error"


__all__ = [
    "CanonicalBoundsError",
    "CanonicalNumberError",
    "CanonicalTypeError",
    "CanonicalizationError",
    "ConfigurationError",
    "ContractError",
    "IdentifierError",
    "V3Error",
]
