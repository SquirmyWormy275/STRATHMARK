"""Signed, field-independent V3 forecasts used only for tournament seeding."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.evidence import (
    TargetContext,
    _require_digest,
    require_utc_milliseconds,
)
from strathmark.v3.contracts.forecasts import PositiveTimeDistribution
from strathmark.v3.contracts.identifiers import (
    StableIdentifier,
    deterministic_identifier,
    require_identifier,
)
from strathmark.v3.contracts.receipts import EngineAuthorityBinding
from strathmark.v3.infrastructure.integrity import (
    IntegrityTrustStore,
    P256Signer,
    SignedManifest,
    sign_manifest,
    verify_manifest,
)

FORECAST_SET_SCHEMA_VERSION = "strathmark-v3-forecast-set-snapshot-v1"
PRE_FIELD_RECEIPT_SCHEMA_VERSION = "strathmark-v3-pre-field-forecast-receipt-v1"
PRE_FIELD_PURPOSE = "pre_field_seeding_only"
_BASIS_KINDS = {"capability_pool", "zero_history_formula_prior"}


def _exact_fields(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ContractError(f"{label} fields differ")


@dataclass(frozen=True, slots=True)
class ForecastSetSnapshot:
    """Immutable round-bound roster and causal authority for pre-field forecasts."""

    forecast_set_id: StableIdentifier
    tournament_id: StableIdentifier
    round_id: StableIdentifier
    forecast_set_revision: int
    ordered_competitor_ids: tuple[StableIdentifier, ...]
    target_context: TargetContext
    historical_cutoff_key: StableIdentifier
    tournament_epoch_id: StableIdentifier
    epoch_digest: str
    maximum_tournament_sequence: int
    bundle_digest: str
    hard_deadline_at: str
    engine_authority: EngineAuthorityBinding
    snapshot_digest: str

    def __post_init__(self) -> None:
        require_identifier(self.forecast_set_id, expected_namespace="forecast_set")
        require_identifier(self.tournament_id, expected_namespace="tournament")
        require_identifier(self.round_id, expected_namespace="round")
        require_identifier(self.historical_cutoff_key, expected_namespace="history")
        require_identifier(self.tournament_epoch_id, expected_namespace="epoch")
        if (
            isinstance(self.forecast_set_revision, bool)
            or not isinstance(self.forecast_set_revision, int)
            or self.forecast_set_revision <= 0
            or isinstance(self.maximum_tournament_sequence, bool)
            or not isinstance(self.maximum_tournament_sequence, int)
            or self.maximum_tournament_sequence < 0
        ):
            raise ContractError("forecast set revisions and sequence must be canonical integers")
        if (
            not isinstance(self.ordered_competitor_ids, tuple)
            or not self.ordered_competitor_ids
            or len(self.ordered_competitor_ids) != len(set(self.ordered_competitor_ids))
            or not all(
                require_identifier(item, expected_namespace="competitor")
                for item in self.ordered_competitor_ids
            )
        ):
            raise ContractError("forecast set ordered roster is invalid")
        if not isinstance(self.target_context, TargetContext):
            raise ContractError("forecast set target context is invalid")
        _require_digest(self.epoch_digest, "forecast epoch")
        _require_digest(self.bundle_digest, "forecast bundle")
        _require_digest(self.snapshot_digest, "forecast set snapshot")
        require_utc_milliseconds(self.hard_deadline_at)
        if (
            not isinstance(self.engine_authority, EngineAuthorityBinding)
            or self.engine_authority.scope_id != self.tournament_id
        ):
            raise ContractError("forecast set engine authority differs from tournament")
        if self.forecast_set_id != deterministic_identifier("forecast_set", self.identity_value()):
            raise ContractError("forecast set identity differs from causal authority")
        if self.snapshot_digest != canonical_digest(self.content_value()):
            raise ContractError("forecast set snapshot digest differs")

    @classmethod
    def create(cls, **values: Any) -> ForecastSetSnapshot:
        normalized = {
            "tournament_id": require_identifier(
                values["tournament_id"], expected_namespace="tournament"
            ),
            "round_id": require_identifier(values["round_id"], expected_namespace="round"),
            "forecast_set_revision": values["forecast_set_revision"],
            "ordered_competitor_ids": tuple(
                require_identifier(item, expected_namespace="competitor")
                for item in values["ordered_competitor_ids"]
            ),
            "target_context": values["target_context"],
            "historical_cutoff_key": require_identifier(
                values["historical_cutoff_key"], expected_namespace="history"
            ),
            "tournament_epoch_id": require_identifier(
                values["tournament_epoch_id"], expected_namespace="epoch"
            ),
            "epoch_digest": values["epoch_digest"],
            "maximum_tournament_sequence": values["maximum_tournament_sequence"],
            "bundle_digest": values["bundle_digest"],
            "hard_deadline_at": values["hard_deadline_at"],
            "engine_authority": values["engine_authority"],
        }
        identity = _forecast_set_identity(normalized)
        content = _forecast_set_content(normalized, identity)
        return cls(
            forecast_set_id=deterministic_identifier("forecast_set", identity),
            **normalized,
            snapshot_digest=canonical_digest(content),
        )

    def identity_value(self) -> dict[str, Any]:
        return _forecast_set_identity(self.creation_values())

    def creation_values(self) -> dict[str, Any]:
        return {
            "tournament_id": self.tournament_id,
            "round_id": self.round_id,
            "forecast_set_revision": self.forecast_set_revision,
            "ordered_competitor_ids": self.ordered_competitor_ids,
            "target_context": self.target_context,
            "historical_cutoff_key": self.historical_cutoff_key,
            "tournament_epoch_id": self.tournament_epoch_id,
            "epoch_digest": self.epoch_digest,
            "maximum_tournament_sequence": self.maximum_tournament_sequence,
            "bundle_digest": self.bundle_digest,
            "hard_deadline_at": self.hard_deadline_at,
            "engine_authority": self.engine_authority,
        }

    def content_value(self) -> dict[str, Any]:
        return _forecast_set_content(self.creation_values(), self.identity_value())

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_value(), "snapshot_digest": self.snapshot_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ForecastSetSnapshot:
        expected = {
            "schema_version",
            "forecast_set_id",
            "tournament_id",
            "round_id",
            "forecast_set_revision",
            "ordered_competitor_ids",
            "target_context",
            "target_context_digest",
            "historical_cutoff_key",
            "tournament_epoch_id",
            "epoch_digest",
            "maximum_tournament_sequence",
            "bundle_digest",
            "hard_deadline_at",
            "engine_authority",
            "snapshot_digest",
        }
        _exact_fields(value, expected, "forecast set snapshot")
        if value["schema_version"] != FORECAST_SET_SCHEMA_VERSION:
            raise ContractError("forecast set snapshot schema differs")
        instance = cls.create(
            tournament_id=value["tournament_id"],
            round_id=value["round_id"],
            forecast_set_revision=value["forecast_set_revision"],
            ordered_competitor_ids=tuple(value["ordered_competitor_ids"]),
            target_context=TargetContext.from_dict(value["target_context"]),
            historical_cutoff_key=value["historical_cutoff_key"],
            tournament_epoch_id=value["tournament_epoch_id"],
            epoch_digest=value["epoch_digest"],
            maximum_tournament_sequence=value["maximum_tournament_sequence"],
            bundle_digest=value["bundle_digest"],
            hard_deadline_at=value["hard_deadline_at"],
            engine_authority=EngineAuthorityBinding.from_dict(value["engine_authority"]),
        )
        if (
            str(instance.forecast_set_id) != value["forecast_set_id"]
            or instance.target_context.digest != value["target_context_digest"]
            or instance.snapshot_digest != value["snapshot_digest"]
        ):
            raise ContractError("forecast set serialized identity differs")
        return instance


@dataclass(frozen=True, slots=True)
class PreFieldCompetitorForecast:
    competitor_id: StableIdentifier
    distribution: PositiveTimeDistribution
    p50_seed_time_ms: int
    basis_kind: str
    evidence_digest: str
    publication_digest: str
    card_digest: str
    capability_binding_digest: str | None
    pool_receipt_digest: str | None
    component_forecast_digests: tuple[str, ...]
    forecast_digest: str

    def __post_init__(self) -> None:
        require_identifier(self.competitor_id, expected_namespace="competitor")
        if not isinstance(self.distribution, PositiveTimeDistribution):
            raise ContractError("pre-field forecast distribution is invalid")
        if self.p50_seed_time_ms != self.distribution.median_ms:
            raise ContractError("pre-field p50 seed time differs from distribution")
        if self.basis_kind not in _BASIS_KINDS:
            raise ContractError("pre-field forecast basis is invalid")
        for digest, label in (
            (self.evidence_digest, "pre-field evidence"),
            (self.publication_digest, "pre-field publication"),
            (self.card_digest, "pre-field card"),
            (self.forecast_digest, "pre-field competitor forecast"),
        ):
            _require_digest(digest, label)
        if not self.component_forecast_digests:
            raise ContractError("pre-field forecast requires component evidence")
        for digest in self.component_forecast_digests:
            _require_digest(digest, "pre-field component forecast")
        if self.basis_kind == "capability_pool":
            if self.capability_binding_digest is None or self.pool_receipt_digest is None:
                raise ContractError("capability pre-field forecast lacks pooled authority")
            _require_digest(self.capability_binding_digest, "pre-field capability")
            _require_digest(self.pool_receipt_digest, "pre-field pool")
        elif self.capability_binding_digest is not None or self.pool_receipt_digest is not None:
            raise ContractError("zero-history pre-field forecast cannot carry pool authority")
        if self.forecast_digest != canonical_digest(self.content_value()):
            raise ContractError("pre-field competitor forecast digest differs")

    @classmethod
    def create(cls, **values: Any) -> PreFieldCompetitorForecast:
        normalized = {
            "competitor_id": require_identifier(
                values["competitor_id"], expected_namespace="competitor"
            ),
            "distribution": values["distribution"],
            "p50_seed_time_ms": values["distribution"].median_ms,
            "basis_kind": values["basis_kind"],
            "evidence_digest": values["evidence_digest"],
            "publication_digest": values["publication_digest"],
            "card_digest": values["card_digest"],
            "capability_binding_digest": values.get("capability_binding_digest"),
            "pool_receipt_digest": values.get("pool_receipt_digest"),
            "component_forecast_digests": tuple(values["component_forecast_digests"]),
        }
        return cls(**normalized, forecast_digest=canonical_digest(_competitor_content(normalized)))

    def content_value(self) -> dict[str, Any]:
        return _competitor_content(
            {
                name: getattr(self, name)
                for name in self.__dataclass_fields__
                if name != "forecast_digest"
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_value(), "forecast_digest": self.forecast_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PreFieldCompetitorForecast:
        expected = {
            "competitor_id",
            "distribution",
            "p50_seed_time_ms",
            "basis_kind",
            "evidence_digest",
            "publication_digest",
            "card_digest",
            "capability_binding_digest",
            "pool_receipt_digest",
            "component_forecast_digests",
            "forecast_digest",
        }
        _exact_fields(value, expected, "pre-field competitor forecast")
        item = cls.create(
            competitor_id=value["competitor_id"],
            distribution=PositiveTimeDistribution.from_dict(value["distribution"]),
            basis_kind=value["basis_kind"],
            evidence_digest=value["evidence_digest"],
            publication_digest=value["publication_digest"],
            card_digest=value["card_digest"],
            capability_binding_digest=value["capability_binding_digest"],
            pool_receipt_digest=value["pool_receipt_digest"],
            component_forecast_digests=tuple(value["component_forecast_digests"]),
        )
        if (
            item.p50_seed_time_ms != value["p50_seed_time_ms"]
            or item.forecast_digest != value["forecast_digest"]
        ):
            raise ContractError("pre-field competitor serialized authority differs")
        return item


@dataclass(frozen=True, slots=True)
class PreFieldForecastReceipt:
    snapshot: ForecastSetSnapshot
    forecasts: tuple[PreFieldCompetitorForecast, ...]
    created_at: str
    manifest: SignedManifest
    receipt_digest: str
    purpose: str = PRE_FIELD_PURPOSE
    issued_mark: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, ForecastSetSnapshot) or not isinstance(
            self.forecasts, tuple
        ):
            raise ContractError("pre-field receipt requires typed immutable authority")
        if (
            tuple(item.competitor_id for item in self.forecasts)
            != self.snapshot.ordered_competitor_ids
        ):
            raise ContractError("pre-field receipt forecasts differ from ordered roster")
        if self.purpose != PRE_FIELD_PURPOSE or self.issued_mark is not False:
            raise ContractError("pre-field receipt cannot issue marks")
        require_utc_milliseconds(self.created_at)
        _require_digest(self.receipt_digest, "pre-field receipt")
        if (
            not isinstance(self.manifest, SignedManifest)
            or self.manifest.kind != "pre_field_forecast_receipt"
        ):
            raise ContractError("pre-field receipt lacks signed authority")
        if self.manifest.body().get("payload") != self.content_value():
            raise ContractError("pre-field signed payload differs")
        if self.receipt_digest != canonical_digest(self.content_value()):
            raise ContractError("pre-field receipt digest differs")

    @classmethod
    def create(
        cls,
        *,
        snapshot: ForecastSetSnapshot,
        forecasts: tuple[PreFieldCompetitorForecast, ...],
        signer: P256Signer,
        created_at: str,
    ) -> PreFieldForecastReceipt:
        values = {
            "snapshot": snapshot,
            "forecasts": forecasts,
            "created_at": created_at,
            "purpose": PRE_FIELD_PURPOSE,
            "issued_mark": False,
        }
        content = _receipt_content(values)
        return cls(
            **values,
            manifest=sign_manifest(
                "pre_field_forecast_receipt", content, signer=signer, created_at=created_at
            ),
            receipt_digest=canonical_digest(content),
        )

    def content_value(self) -> dict[str, Any]:
        return _receipt_content(
            {
                name: getattr(self, name)
                for name in ("snapshot", "forecasts", "created_at", "purpose", "issued_mark")
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_value(),
            "manifest": self.manifest.to_dict(),
            "receipt_digest": self.receipt_digest,
        }

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, trust_store: IntegrityTrustStore
    ) -> PreFieldForecastReceipt:
        expected = {
            "schema_version",
            "purpose",
            "issued_mark",
            "snapshot",
            "forecasts",
            "created_at",
            "manifest",
            "receipt_digest",
        }
        _exact_fields(value, expected, "pre-field receipt")
        if value["schema_version"] != PRE_FIELD_RECEIPT_SCHEMA_VERSION:
            raise ContractError("pre-field receipt schema differs")
        receipt = cls(
            snapshot=ForecastSetSnapshot.from_dict(value["snapshot"]),
            forecasts=tuple(
                PreFieldCompetitorForecast.from_dict(item) for item in value["forecasts"]
            ),
            created_at=value["created_at"],
            manifest=SignedManifest.from_dict(value["manifest"]),
            receipt_digest=value["receipt_digest"],
            purpose=value["purpose"],
            issued_mark=value["issued_mark"],
        )
        if verify_manifest(receipt.manifest, trust_store) != receipt.content_value():
            raise ContractError("pre-field receipt signature differs")
        return receipt


def _forecast_set_identity(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": FORECAST_SET_SCHEMA_VERSION,
        "tournament_id": str(values["tournament_id"]),
        "round_id": str(values["round_id"]),
        "forecast_set_revision": values["forecast_set_revision"],
        "ordered_competitor_ids": [str(item) for item in values["ordered_competitor_ids"]],
        "target_context_digest": values["target_context"].digest,
        "historical_cutoff_key": str(values["historical_cutoff_key"]),
        "tournament_epoch_id": str(values["tournament_epoch_id"]),
        "epoch_digest": values["epoch_digest"],
        "maximum_tournament_sequence": values["maximum_tournament_sequence"],
        "bundle_digest": values["bundle_digest"],
        "hard_deadline_at": values["hard_deadline_at"],
        "engine_authority": values["engine_authority"].to_dict(),
    }


def _forecast_set_content(values: Mapping[str, Any], identity: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **identity,
        "forecast_set_id": str(deterministic_identifier("forecast_set", identity)),
        "target_context": values["target_context"].to_dict(),
    }


def _competitor_content(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "competitor_id": str(values["competitor_id"]),
        "distribution": values["distribution"].to_dict(),
        "p50_seed_time_ms": values["p50_seed_time_ms"],
        "basis_kind": values["basis_kind"],
        "evidence_digest": values["evidence_digest"],
        "publication_digest": values["publication_digest"],
        "card_digest": values["card_digest"],
        "capability_binding_digest": values["capability_binding_digest"],
        "pool_receipt_digest": values["pool_receipt_digest"],
        "component_forecast_digests": list(values["component_forecast_digests"]),
    }


def _receipt_content(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": PRE_FIELD_RECEIPT_SCHEMA_VERSION,
        "purpose": values["purpose"],
        "issued_mark": values["issued_mark"],
        "snapshot": values["snapshot"].to_dict(),
        "forecasts": [item.to_dict() for item in values["forecasts"]],
        "created_at": values["created_at"],
    }


__all__ = [
    "FORECAST_SET_SCHEMA_VERSION",
    "PRE_FIELD_PURPOSE",
    "PRE_FIELD_RECEIPT_SCHEMA_VERSION",
    "ForecastSetSnapshot",
    "PreFieldCompetitorForecast",
    "PreFieldForecastReceipt",
]
