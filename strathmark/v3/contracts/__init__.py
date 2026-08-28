"""Standard-library-only public contract primitives for STRATHMARK V3."""

from strathmark.v3.contracts.canonical import (
    CANONICALIZATION_VERSION,
    INT64_MAX,
    INT64_MIN,
    TIME_QUANTUM_MILLISECONDS,
    canonical_bytes,
    canonical_decimal_string,
    canonical_digest,
    canonical_expected_versions,
    milliseconds_from_seconds,
)
from strathmark.v3.contracts.commands import (
    MAX_BLOB_BYTES,
    MAX_INLINE_PAYLOAD_BYTES,
    BlobReference,
    BlobReferenceV2,
    BlobRetentionClass,
    CommandEnvelope,
    CommandKind,
    InlinePayload,
)
from strathmark.v3.contracts.errors import (
    CanonicalBoundsError,
    CanonicalizationError,
    CanonicalNumberError,
    CanonicalTypeError,
    ConfigurationError,
    ContractError,
    IdentifierError,
    V3Error,
)
from strathmark.v3.contracts.events import AggregateKind, EventEnvelope, EventKind
from strathmark.v3.contracts.evidence import (
    ContextProperty,
    EvidencePacket,
    ResultObservation,
    TargetContext,
    require_utc_milliseconds,
)
from strathmark.v3.contracts.forecasts import (
    ArtifactIdentity,
    AssessorForecast,
    AssessorKind,
    DependenceInputs,
    DependenceMode,
    DistributionSamples,
    EvidenceSupport,
    ForecastState,
    ForecastWarning,
    LLMMemberAudit,
    PositiveTimeDistribution,
    PredictiveDistributionContract,
    QuantilePoint,
    SamplingSpec,
)
from strathmark.v3.contracts.identifiers import (
    IdempotencyKey,
    StableIdentifier,
    deterministic_identifier,
    identifier_namespace,
    require_idempotency_key,
    require_identifier,
)
from strathmark.v3.contracts.receipts import (
    BundleIdentity,
    FieldReceipt,
    MarkAssignment,
    PacketIdentity,
    ReceiptSection,
    ReceiptSectionKind,
)
from strathmark.v3.contracts.statuses import (
    AdmittedCompletion,
    AggregateLifecycle,
    LifecycleAggregateKind,
    LifecycleStatus,
    OfficialResult,
    ResultStatus,
    admit_raw_completion,
)

_LAZY_PRE_FIELD_EXPORTS = frozenset(
    {"ForecastSetSnapshot", "PreFieldCompetitorForecast", "PreFieldForecastReceipt"}
)


def __getattr__(name: str):
    """Load integrity-dependent pre-field contracts only when requested."""
    if name not in _LAZY_PRE_FIELD_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from strathmark.v3.contracts.pre_field_forecasts import (
        ForecastSetSnapshot,
        PreFieldCompetitorForecast,
        PreFieldForecastReceipt,
    )

    globals().update(
        {
            "ForecastSetSnapshot": ForecastSetSnapshot,
            "PreFieldCompetitorForecast": PreFieldCompetitorForecast,
            "PreFieldForecastReceipt": PreFieldForecastReceipt,
        }
    )
    return globals()[name]


def __dir__() -> list[str]:
    return sorted((*globals(), *_LAZY_PRE_FIELD_EXPORTS))


__all__ = [
    "CANONICALIZATION_VERSION",
    "INT64_MAX",
    "INT64_MIN",
    "MAX_BLOB_BYTES",
    "MAX_INLINE_PAYLOAD_BYTES",
    "TIME_QUANTUM_MILLISECONDS",
    "AdmittedCompletion",
    "AggregateLifecycle",
    "AggregateKind",
    "ArtifactIdentity",
    "AssessorForecast",
    "AssessorKind",
    "BlobReference",
    "BlobReferenceV2",
    "BlobRetentionClass",
    "BundleIdentity",
    "CanonicalBoundsError",
    "CanonicalNumberError",
    "CanonicalTypeError",
    "CanonicalizationError",
    "ConfigurationError",
    "ContextProperty",
    "ContractError",
    "CommandEnvelope",
    "CommandKind",
    "DependenceInputs",
    "DependenceMode",
    "DistributionSamples",
    "EvidencePacket",
    "EvidenceSupport",
    "EventEnvelope",
    "EventKind",
    "FieldReceipt",
    "ForecastState",
    "ForecastSetSnapshot",
    "ForecastWarning",
    "IdempotencyKey",
    "IdentifierError",
    "InlinePayload",
    "LLMMemberAudit",
    "LifecycleAggregateKind",
    "LifecycleStatus",
    "MarkAssignment",
    "OfficialResult",
    "PacketIdentity",
    "PositiveTimeDistribution",
    "PreFieldCompetitorForecast",
    "PreFieldForecastReceipt",
    "PredictiveDistributionContract",
    "QuantilePoint",
    "ReceiptSection",
    "ReceiptSectionKind",
    "ResultObservation",
    "ResultStatus",
    "SamplingSpec",
    "StableIdentifier",
    "TargetContext",
    "V3Error",
    "canonical_bytes",
    "canonical_decimal_string",
    "canonical_digest",
    "canonical_expected_versions",
    "admit_raw_completion",
    "deterministic_identifier",
    "identifier_namespace",
    "milliseconds_from_seconds",
    "require_idempotency_key",
    "require_identifier",
    "require_utc_milliseconds",
]
