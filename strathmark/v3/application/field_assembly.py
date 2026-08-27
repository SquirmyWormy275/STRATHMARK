"""Current-revision field assembly over verified U13/U14 authority."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from enum import Enum
from typing import Any, Callable, Mapping

from strathmark.v3.application.capacity import CapacityManifest
from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.contracts.commands import InlinePayload
from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.evidence import (
    EvidencePacket,
    TargetContext,
    require_utc_milliseconds,
)
from strathmark.v3.contracts.forecasts import (
    AssessorForecast,
    AssessorKind,
    ForecastState,
    PositiveTimeDistribution,
)
from strathmark.v3.contracts.identifiers import (
    StableIdentifier,
    deterministic_identifier,
    require_idempotency_key,
    require_identifier,
)
from strathmark.v3.contracts.receipts import (
    BundleIdentity,
    EngineAuthorityBinding,
    FieldReceipt,
    MarkAssignment,
    PacketIdentity,
    ReceiptSection,
    ReceiptSectionKind,
)
from strathmark.v3.domain.credibility import ContextNode
from strathmark.v3.domain.disagreement import (
    AcceptedExpectedTimeOverrideState,
    ConsequenceColor,
    CouncilMemberStatus,
    CounterfactualCompetitor,
    CounterfactualSheet,
    DisagreementDecision,
    ExpectedTimeOverrideReceipt,
    FieldSheetSnapshot,
    OptimizerVerificationStatus,
    OverrideScope,
    ZeroHistoryEstimate,
)
from strathmark.v3.domain.joint_dependence import (
    DependenceArtifact,
    FieldCompetitorForecast,
    JointDraws,
    bind_field_dependence,
    generate_joint_draws,
    has_fresh_joint_generation_proof,
    regenerate_joint_uniforms_for_replay,
)
from strathmark.v3.domain.optimizer import (
    OptimizationField,
    OptimizerFallback,
    VerifiedOptimizerReceipt,
)
from strathmark.v3.domain.pooling import (
    PoolMode,
    PoolResult,
    WeightAuthorityBinding,
)
from strathmark.v3.infrastructure.integrity import (
    P256Signer,
    SignedManifest,
    sign_manifest,
)


class AssemblyError(ValueError):
    pass


class AssemblyConflict(AssemblyError):
    pass


class OperationalWeightKind(str, Enum):
    ROOT_BASELINE = "root_baseline"
    LIVE_ROUND_FREEZE = "live_round_freeze"


@dataclass(frozen=True, slots=True)
class FieldCapacityAuthority:
    capacity: CapacityManifest
    bundle_digest: str
    manifest: SignedManifest
    authority_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.capacity, CapacityManifest) or not isinstance(
            self.manifest, SignedManifest
        ):
            raise AssemblyError("field capacity authority must be typed")
        _digest(self.bundle_digest, "capacity bundle")
        expected = {
            "schema_version": "strathmark-v3-field-capacity-authority-v1",
            "purpose": "field_assembly_capacity",
            "bundle_digest": self.bundle_digest,
            "capacity_manifest": self.capacity.to_dict(),
            "capacity_manifest_digest": self.capacity.digest,
        }
        if (
            self.manifest.kind != "field_capacity_authority"
            or self.manifest.body().get("payload") != expected
            or self.authority_digest != canonical_digest(expected)
        ):
            raise AssemblyError("field capacity manifest authority differs")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-installed-field-capacity-v1",
            "capacity": self.capacity.to_dict(),
            "bundle_digest": self.bundle_digest,
            "manifest": self.manifest.to_dict(),
            "authority_digest": self.authority_digest,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> FieldCapacityAuthority:
        if (
            set(value)
            != {
                "schema_version",
                "capacity",
                "bundle_digest",
                "manifest",
                "authority_digest",
            }
            or value.get("schema_version") != "strathmark-v3-installed-field-capacity-v1"
        ):
            raise AssemblyError("installed field capacity fields differ")
        return cls(
            CapacityManifest.from_dict(value["capacity"]),
            value["bundle_digest"],
            SignedManifest.from_dict(value["manifest"]),
            value["authority_digest"],
        )


def seal_field_capacity_authority(
    capacity: CapacityManifest,
    *,
    bundle_digest: str,
    signer: P256Signer,
    created_at: str,
) -> FieldCapacityAuthority:
    if not isinstance(capacity, CapacityManifest):
        raise AssemblyError("field capacity manifest must be typed")
    _digest(bundle_digest, "capacity bundle")
    manifest = sign_manifest(
        "field_capacity_authority",
        {
            "schema_version": "strathmark-v3-field-capacity-authority-v1",
            "purpose": "field_assembly_capacity",
            "bundle_digest": bundle_digest,
            "capacity_manifest": capacity.to_dict(),
            "capacity_manifest_digest": capacity.digest,
        },
        signer=signer,
        created_at=created_at,
    )
    return FieldCapacityAuthority(
        capacity, bundle_digest, manifest, canonical_digest(manifest.body()["payload"])
    )


@dataclass(frozen=True, slots=True)
class OperationalWeightAuthority:
    """Closed U12 event authority for the weight binding used by U13.

    ``WeightAuthorityBinding`` intentionally remains a pending U13 hand-off.  This
    wrapper is operational only when the SQLite boundary has replayed the exact
    U12 baseline/freeze event and its U5 round/epoch authority.
    """

    kind: OperationalWeightKind
    binding: WeightAuthorityBinding
    tournament_id: StableIdentifier
    round_id: StableIdentifier
    epoch_id: StableIdentifier
    epoch_digest: str
    frozen_tournament_sequence: int
    authority_event_sequence: int
    authority_event_digest: str
    completed_round_id: StableIdentifier | None
    round_close_event_digest: str | None
    baseline_receipt_digest: str
    control_event_sequence: int | None
    control_event_digest: str | None
    authority_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, OperationalWeightKind) or not isinstance(
            self.binding, WeightAuthorityBinding
        ):
            raise AssemblyError("operational weight authority must be typed")
        for value, namespace in (
            (self.tournament_id, "tournament"),
            (self.round_id, "round"),
            (self.epoch_id, "epoch"),
        ):
            require_identifier(value, expected_namespace=namespace)
        for value, label in (
            (self.epoch_digest, "weight epoch"),
            (self.authority_event_digest, "weight event"),
            (self.baseline_receipt_digest, "baseline receipt"),
            (self.authority_digest, "operational weight authority"),
        ):
            _digest(value, label)
        for value, label in (
            (self.frozen_tournament_sequence, "frozen tournament sequence"),
            (self.authority_event_sequence, "weight authority event sequence"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise AssemblyError(f"{label} must be positive")
        if self.binding.tournament_event_sequence != self.frozen_tournament_sequence:
            raise AssemblyError("weight binding differs from its frozen boundary")
        if self.kind is OperationalWeightKind.ROOT_BASELINE:
            if (
                self.completed_round_id is not None
                or self.round_close_event_digest is not None
                or self.control_event_sequence is not None
                or self.control_event_digest is not None
            ):
                raise AssemblyError("root baseline cannot claim predecessor closure")
            if self.baseline_receipt_digest != self.binding.weight_receipt_digest:
                raise AssemblyError("root baseline receipt differs from its U13 binding")
        else:
            if self.completed_round_id is None or self.round_close_event_digest is None:
                raise AssemblyError("live round freeze requires predecessor closure")
            require_identifier(self.completed_round_id, expected_namespace="round")
            _digest(self.round_close_event_digest, "round close event")
            if (
                isinstance(self.control_event_sequence, bool)
                or not isinstance(self.control_event_sequence, int)
                or self.control_event_sequence < self.authority_event_sequence
            ):
                raise AssemblyError("live freeze control sequence is invalid")
            _digest(self.control_event_digest, "live control event")
        if self.authority_digest != canonical_digest(self.content_value()):
            raise AssemblyError("operational weight authority digest differs")

    @classmethod
    def create(cls, **values: Any) -> OperationalWeightAuthority:
        normalized = {
            "kind": OperationalWeightKind(values["kind"]),
            "binding": values["binding"],
            "tournament_id": require_identifier(
                values["tournament_id"], expected_namespace="tournament"
            ),
            "round_id": require_identifier(values["round_id"], expected_namespace="round"),
            "epoch_id": require_identifier(values["epoch_id"], expected_namespace="epoch"),
            "epoch_digest": values["epoch_digest"],
            "frozen_tournament_sequence": values["frozen_tournament_sequence"],
            "authority_event_sequence": values["authority_event_sequence"],
            "authority_event_digest": values["authority_event_digest"],
            "completed_round_id": (
                None
                if values.get("completed_round_id") is None
                else require_identifier(values["completed_round_id"], expected_namespace="round")
            ),
            "round_close_event_digest": values.get("round_close_event_digest"),
            "baseline_receipt_digest": values["baseline_receipt_digest"],
            "control_event_sequence": values.get("control_event_sequence"),
            "control_event_digest": values.get("control_event_digest"),
        }
        return cls(
            **normalized,
            authority_digest=canonical_digest(_operational_weight_content(normalized)),
        )

    def content_value(self) -> dict[str, Any]:
        return _operational_weight_content(
            {
                name: getattr(self, name)
                for name in self.__dataclass_fields__
                if name != "authority_digest"
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_value(), "authority_digest": self.authority_digest}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> OperationalWeightAuthority:
        expected = {
            "schema_version",
            "kind",
            "binding",
            "tournament_id",
            "round_id",
            "epoch_id",
            "epoch_digest",
            "frozen_tournament_sequence",
            "authority_event_sequence",
            "authority_event_digest",
            "completed_round_id",
            "round_close_event_digest",
            "baseline_receipt_digest",
            "control_event_sequence",
            "control_event_digest",
            "authority_digest",
        }
        if set(value) != expected or value.get("schema_version") != (
            "strathmark-v3-operational-weight-authority-v1"
        ):
            raise AssemblyError("operational weight authority fields differ")
        binding = value["binding"]
        if not isinstance(binding, dict):
            raise AssemblyError("operational weight binding is invalid")
        return cls(
            OperationalWeightKind(value["kind"]),
            WeightAuthorityBinding.from_dict(binding),
            StableIdentifier(value["tournament_id"]),
            StableIdentifier(value["round_id"]),
            StableIdentifier(value["epoch_id"]),
            value["epoch_digest"],
            value["frozen_tournament_sequence"],
            value["authority_event_sequence"],
            value["authority_event_digest"],
            (
                None
                if value["completed_round_id"] is None
                else StableIdentifier(value["completed_round_id"])
            ),
            value["round_close_event_digest"],
            value["baseline_receipt_digest"],
            value["control_event_sequence"],
            value["control_event_digest"],
            value["authority_digest"],
        )


@dataclass(frozen=True, slots=True, order=True)
class FrozenEntrantAssignment:
    competitor_id: StableIdentifier
    stand_id: StableIdentifier
    crn_index: int

    def __post_init__(self) -> None:
        require_identifier(self.competitor_id, expected_namespace="competitor")
        require_identifier(self.stand_id, expected_namespace="stand")
        if (
            isinstance(self.crn_index, bool)
            or not isinstance(self.crn_index, int)
            or self.crn_index < 0
        ):
            raise AssemblyError("frozen CRN index must be nonnegative")

    @classmethod
    def create(
        cls,
        competitor_id: str | StableIdentifier,
        stand_id: str | StableIdentifier,
        crn_index: int,
    ) -> FrozenEntrantAssignment:
        return cls(
            require_identifier(competitor_id, expected_namespace="competitor"),
            require_identifier(stand_id, expected_namespace="stand"),
            crn_index,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "competitor_id": str(self.competitor_id),
            "stand_id": str(self.stand_id),
            "crn_index": self.crn_index,
        }


@dataclass(frozen=True, slots=True)
class FrozenFieldRevision:
    tournament_id: StableIdentifier
    round_id: StableIdentifier
    field_id: StableIdentifier
    field_revision: int
    ordered_assignments: tuple[FrozenEntrantAssignment, ...]
    target_context: TargetContext
    historical_cutoff_key: StableIdentifier
    tournament_epoch_id: StableIdentifier
    tournament_event_sequence: int
    bundle_digest: str
    evidence_digest: str
    capacity_authority_digest: str
    max_field_entrants: int
    call_order: int
    scheduled_at: str
    deadline_at: str
    revision_digest: str

    def __post_init__(self) -> None:
        for value, namespace in (
            (self.tournament_id, "tournament"),
            (self.round_id, "round"),
            (self.field_id, "field"),
            (self.historical_cutoff_key, "history"),
            (self.tournament_epoch_id, "epoch"),
        ):
            require_identifier(value, expected_namespace=namespace)
        if (
            isinstance(self.field_revision, bool)
            or not isinstance(self.field_revision, int)
            or self.field_revision <= 0
        ):
            raise AssemblyError("field revision must be positive")
        if not isinstance(self.target_context, TargetContext):
            raise AssemblyError("field target context must be typed")
        if not self.ordered_assignments:
            raise AssemblyError("field requires at least one frozen entrant assignment")
        if tuple(item.crn_index for item in self.ordered_assignments) != tuple(
            range(len(self.ordered_assignments))
        ):
            raise AssemblyError("frozen CRN indices must be contiguous and ordered")
        if len({item.competitor_id for item in self.ordered_assignments}) != len(
            self.ordered_assignments
        ) or len({item.stand_id for item in self.ordered_assignments}) != len(
            self.ordered_assignments
        ):
            raise AssemblyError("field competitor and stand assignments must be one-to-one")
        if (
            isinstance(self.tournament_event_sequence, bool)
            or not isinstance(self.tournament_event_sequence, int)
            or self.tournament_event_sequence < 0
        ):
            raise AssemblyError("tournament event sequence must be nonnegative")
        if (
            isinstance(self.call_order, bool)
            or not isinstance(self.call_order, int)
            or self.call_order < 0
        ):
            raise AssemblyError("field call order must be nonnegative")
        _digest(self.bundle_digest, "bundle")
        _digest(self.evidence_digest, "evidence")
        _digest(self.capacity_authority_digest, "field capacity authority")
        if (
            isinstance(self.max_field_entrants, bool)
            or not isinstance(self.max_field_entrants, int)
            or self.max_field_entrants <= 0
            or len(self.ordered_assignments) > self.max_field_entrants
        ):
            raise AssemblyError("field exceeds its declared entrant capacity")
        require_utc_milliseconds(self.scheduled_at)
        require_utc_milliseconds(self.deadline_at)
        if self.deadline_at <= self.scheduled_at:
            raise AssemblyError("field deadline must follow its scheduled instant")
        _digest(self.revision_digest, "field revision")
        if self.revision_digest != canonical_digest(self.content_value()):
            raise AssemblyError("field revision digest differs from frozen upstream assignment")

    @classmethod
    def create(cls, **values: Any) -> FrozenFieldRevision:
        assignments = tuple(values["assignments"])
        ordered = tuple(sorted(assignments, key=lambda item: item.crn_index))
        normalized = {
            "tournament_id": require_identifier(
                values["tournament_id"], expected_namespace="tournament"
            ),
            "round_id": require_identifier(values["round_id"], expected_namespace="round"),
            "field_id": require_identifier(values["field_id"], expected_namespace="field"),
            "field_revision": values["field_revision"],
            "ordered_assignments": ordered,
            "target_context": values["target_context"],
            "historical_cutoff_key": require_identifier(
                values["historical_cutoff_key"], expected_namespace="history"
            ),
            "tournament_epoch_id": require_identifier(
                values["tournament_epoch_id"], expected_namespace="epoch"
            ),
            "tournament_event_sequence": values["tournament_event_sequence"],
            "bundle_digest": values["bundle_digest"],
            "evidence_digest": values["evidence_digest"],
            "capacity_authority_digest": values["capacity_authority_digest"],
            "max_field_entrants": values["max_field_entrants"],
            "call_order": values["call_order"],
            "scheduled_at": values["scheduled_at"],
            "deadline_at": values["deadline_at"],
        }
        content = _field_content(normalized)
        return cls(**normalized, revision_digest=canonical_digest(content))

    def content_value(self) -> dict[str, Any]:
        return _field_content(
            {
                name: getattr(self, name)
                for name in self.__dataclass_fields__
                if name != "revision_digest"
            }
        )


CARD_AUTHORITY_MANIFEST_KIND = "field_competitor_card_authority"
_OUTER_ASSESSORS = (
    AssessorKind.FORMULA,
    AssessorKind.ML,
    AssessorKind.LLM_COUNCIL,
)


@dataclass(frozen=True, slots=True)
class CompetitorCardAuthority:
    evidence_packet: EvidencePacket
    forecasts: tuple[AssessorForecast, ...]
    bundle_digest: str
    manifest: SignedManifest

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_packet, EvidencePacket):
            raise AssemblyError("competitor card requires a canonical evidence packet")
        if (
            not isinstance(self.forecasts, tuple)
            or tuple(item.assessor for item in self.forecasts) != _OUTER_ASSESSORS
            or not all(isinstance(item, AssessorForecast) for item in self.forecasts)
        ):
            raise AssemblyError("competitor card requires three ordered outer forecasts")
        _digest(self.bundle_digest, "competitor card bundle")
        if (
            not isinstance(self.manifest, SignedManifest)
            or self.manifest.kind != CARD_AUTHORITY_MANIFEST_KIND
        ):
            raise AssemblyError("competitor card requires signed producer authority")

    @property
    def competitor_id(self) -> StableIdentifier:
        return self.evidence_packet.competitor_id

    @property
    def packet_digest(self) -> str:
        return self.evidence_packet.content_digest

    def content_value(self) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-competitor-card-authority-v1",
            "purpose": "field_card_operational",
            "evidence_packet": self.evidence_packet.to_dict(),
            "forecasts": [item.to_dict() for item in self.forecasts],
            "bundle_digest": self.bundle_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_value(),
            "manifest": self.manifest.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CompetitorCardAuthority:
        expected = {
            "schema_version",
            "purpose",
            "evidence_packet",
            "forecasts",
            "bundle_digest",
            "manifest",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise AssemblyError("competitor card authority fields differ")
        if (
            value["schema_version"] != "strathmark-v3-competitor-card-authority-v1"
            or value["purpose"] != "field_card_operational"
            or not isinstance(value["evidence_packet"], dict)
            or not isinstance(value["forecasts"], list)
            or not isinstance(value["manifest"], dict)
        ):
            raise AssemblyError("competitor card authority schema differs")
        card = cls(
            EvidencePacket.from_dict(value["evidence_packet"]),
            tuple(AssessorForecast.from_dict(item) for item in value["forecasts"]),
            value["bundle_digest"],
            SignedManifest.from_dict(value["manifest"]),
        )
        body = card.manifest.body()
        if body.get("payload") != card.content_value():
            raise AssemblyError("competitor card signed payload differs")
        return card


def seal_competitor_card_authority(
    evidence_packet: EvidencePacket,
    forecasts: tuple[AssessorForecast, ...],
    *,
    bundle_digest: str,
    signer: P256Signer,
    created_at: str,
) -> CompetitorCardAuthority:
    payload = {
        "schema_version": "strathmark-v3-competitor-card-authority-v1",
        "purpose": "field_card_operational",
        "evidence_packet": evidence_packet.to_dict(),
        "forecasts": [item.to_dict() for item in forecasts],
        "bundle_digest": bundle_digest,
    }
    manifest = sign_manifest(
        CARD_AUTHORITY_MANIFEST_KIND,
        payload,
        signer=signer,
        created_at=created_at,
    )
    return CompetitorCardAuthority(evidence_packet, forecasts, bundle_digest, manifest)


@dataclass(frozen=True, slots=True)
class RollingPublicationBinding:
    """Immutable field-receipt link to one current rolling card publication."""

    competitor_id: StableIdentifier
    target_context_digest: str
    historical_cutoff_key: StableIdentifier
    tournament_epoch_id: StableIdentifier
    bundle_digest: str
    evidence_digest: str
    dependency_revision: int
    card_digest: str
    card_idempotency_key: str
    card_manifest_digest: str
    publication_digest: str
    publication_manifest_digest: str
    component_refs_digest: str
    availability: tuple[tuple[str, str], ...]
    council_manifest_digest: str
    council_aggregate_manifest_digest: str
    hard_deadline_at: str
    sealed_at: str
    binding_digest: str

    def __post_init__(self) -> None:
        require_identifier(self.competitor_id, expected_namespace="competitor")
        require_identifier(self.historical_cutoff_key, expected_namespace="history")
        require_identifier(self.tournament_epoch_id, expected_namespace="epoch")
        for value, label in (
            (self.target_context_digest, "rolling publication context"),
            (self.bundle_digest, "rolling publication bundle"),
            (self.evidence_digest, "rolling publication evidence"),
            (self.card_digest, "rolling publication card"),
            (self.card_manifest_digest, "rolling publication card manifest"),
            (self.publication_digest, "rolling publication"),
            (
                self.publication_manifest_digest,
                "rolling publication manifest",
            ),
            (self.component_refs_digest, "rolling publication components"),
            (self.council_manifest_digest, "rolling council manifest"),
            (
                self.council_aggregate_manifest_digest,
                "rolling council aggregate manifest",
            ),
            (self.binding_digest, "rolling publication binding"),
        ):
            _digest(value, label)
        require_idempotency_key(self.card_idempotency_key)
        if (
            not isinstance(self.availability, tuple)
            or tuple(item[0] for item in self.availability) != ("formula", "ml", "llm_council")
            or any(
                not isinstance(item, tuple)
                or len(item) != 2
                or not all(isinstance(value, str) and value for value in item)
                for item in self.availability
            )
        ):
            raise AssemblyError("rolling publication availability differs")
        if (
            isinstance(self.dependency_revision, bool)
            or not isinstance(self.dependency_revision, int)
            or self.dependency_revision <= 0
        ):
            raise AssemblyError("rolling publication dependency revision is invalid")
        require_utc_milliseconds(self.hard_deadline_at)
        require_utc_milliseconds(self.sealed_at)
        card_key = self.card_key_value()
        expected_card_digest = canonical_digest(
            {
                "schema_version": "strathmark-v3-capability-card-key-v1",
                "competitor_id": card_key["competitor_id"],
                "target_context_digest": card_key["target_context_digest"],
                "historical_cutoff_key": card_key["historical_cutoff_key"],
                "tournament_epoch_id": card_key["tournament_epoch_id"],
                "bundle_digest": card_key["bundle_digest"],
                "evidence_digest": card_key["evidence_digest"],
                "dependency_revision": card_key["dependency_revision"],
            }
        )
        expected_idempotency = f"card:{expected_card_digest}"
        if (
            self.card_digest != expected_card_digest
            or self.card_idempotency_key != expected_idempotency
        ):
            raise AssemblyError("rolling publication card key digest differs")
        if self.binding_digest != canonical_digest(self.content_value()):
            raise AssemblyError("rolling publication binding digest differs")

    @classmethod
    def create(
        cls,
        *,
        card_key: Mapping[str, Any],
        card_manifest_digest: str,
        publication_digest: str,
        publication_manifest_digest: str,
        component_refs_digest: str,
        availability: tuple[tuple[str, str], ...],
        council_manifest_digest: str,
        council_aggregate_manifest_digest: str,
        hard_deadline_at: str,
        sealed_at: str,
    ) -> RollingPublicationBinding:
        expected = {
            "schema_version",
            "competitor_id",
            "target_context_digest",
            "historical_cutoff_key",
            "tournament_epoch_id",
            "bundle_digest",
            "evidence_digest",
            "dependency_revision",
            "card_digest",
            "idempotency_key",
        }
        if (
            not isinstance(card_key, Mapping)
            or set(card_key) != expected
            or card_key.get("schema_version") != "strathmark-v3-capability-card-key-v1"
        ):
            raise AssemblyError("rolling publication card key fields differ")
        values = {
            "competitor_id": require_identifier(
                card_key["competitor_id"], expected_namespace="competitor"
            ),
            "target_context_digest": card_key["target_context_digest"],
            "historical_cutoff_key": require_identifier(
                card_key["historical_cutoff_key"], expected_namespace="history"
            ),
            "tournament_epoch_id": require_identifier(
                card_key["tournament_epoch_id"], expected_namespace="epoch"
            ),
            "bundle_digest": card_key["bundle_digest"],
            "evidence_digest": card_key["evidence_digest"],
            "dependency_revision": card_key["dependency_revision"],
            "card_digest": card_key["card_digest"],
            "card_idempotency_key": card_key["idempotency_key"],
            "card_manifest_digest": card_manifest_digest,
            "publication_digest": publication_digest,
            "publication_manifest_digest": publication_manifest_digest,
            "component_refs_digest": component_refs_digest,
            "availability": availability,
            "council_manifest_digest": council_manifest_digest,
            "council_aggregate_manifest_digest": council_aggregate_manifest_digest,
            "hard_deadline_at": hard_deadline_at,
            "sealed_at": sealed_at,
        }
        provisional = cls(
            **values,
            binding_digest=canonical_digest(_rolling_publication_binding_content(values)),
        )
        return provisional

    def card_key_value(self) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-capability-card-key-v1",
            "competitor_id": str(self.competitor_id),
            "target_context_digest": self.target_context_digest,
            "historical_cutoff_key": str(self.historical_cutoff_key),
            "tournament_epoch_id": str(self.tournament_epoch_id),
            "bundle_digest": self.bundle_digest,
            "evidence_digest": self.evidence_digest,
            "dependency_revision": self.dependency_revision,
            "card_digest": self.card_digest,
            "idempotency_key": self.card_idempotency_key,
        }

    def content_value(self) -> dict[str, Any]:
        return _rolling_publication_binding_content(
            {
                name: getattr(self, name)
                for name in self.__dataclass_fields__
                if name != "binding_digest"
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_value(), "binding_digest": self.binding_digest}


@dataclass(frozen=True, slots=True)
class RollingCapabilityBinding:
    """Causal current-state anchor used by the field capability operator."""

    competitor_id: StableIdentifier
    context_digest: str
    state_revision: int
    state_digest: str
    aggregate_id: StableIdentifier
    aggregate_version: int
    aggregate_event_digest: str
    binding_digest: str

    def __post_init__(self) -> None:
        require_identifier(self.competitor_id, expected_namespace="competitor")
        require_identifier(self.aggregate_id, expected_namespace="competitor")
        for value, label in (
            (self.context_digest, "rolling capability context"),
            (self.state_digest, "rolling capability state"),
            (self.aggregate_event_digest, "rolling capability aggregate event"),
            (self.binding_digest, "rolling capability binding"),
        ):
            _digest(value, label)
        for value, label in (
            (self.state_revision, "rolling capability state revision"),
            (self.aggregate_version, "rolling capability aggregate version"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise AssemblyError(f"{label} must be positive")
        expected_aggregate = deterministic_identifier(
            "competitor",
            {
                "competitor_id": str(self.competitor_id),
                "context_digest": self.context_digest,
            },
        )
        if self.aggregate_id != expected_aggregate:
            raise AssemblyError("rolling capability aggregate identity differs")
        if self.binding_digest != canonical_digest(self.content_value()):
            raise AssemblyError("rolling capability binding digest differs")

    @classmethod
    def create(
        cls,
        *,
        competitor_id: str | StableIdentifier,
        context_digest: str,
        state_revision: int,
        state_digest: str,
        aggregate_version: int,
        aggregate_event_digest: str,
    ) -> RollingCapabilityBinding:
        competitor = require_identifier(competitor_id, expected_namespace="competitor")
        values = {
            "competitor_id": competitor,
            "context_digest": context_digest,
            "state_revision": state_revision,
            "state_digest": state_digest,
            "aggregate_id": deterministic_identifier(
                "competitor",
                {
                    "competitor_id": str(competitor),
                    "context_digest": context_digest,
                },
            ),
            "aggregate_version": aggregate_version,
            "aggregate_event_digest": aggregate_event_digest,
        }
        return cls(
            **values,
            binding_digest=canonical_digest(_rolling_capability_binding_content(values)),
        )

    def content_value(self) -> dict[str, Any]:
        return _rolling_capability_binding_content(
            {
                name: getattr(self, name)
                for name in self.__dataclass_fields__
                if name != "binding_digest"
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_value(), "binding_digest": self.binding_digest}


@dataclass(frozen=True, slots=True)
class CompetitorPoolEvidence:
    card: CompetitorCardAuthority
    pool: PoolResult

    def __post_init__(self) -> None:
        if not isinstance(self.card, CompetitorCardAuthority):
            raise AssemblyError("competitor pool requires typed card authority")
        if not isinstance(self.pool, PoolResult):
            raise AssemblyError("competitor pool evidence must be a typed U13 result")
        if self.pool.receipt.pooled_distribution != self.pool.distribution:
            raise AssemblyError("pool result distribution differs from its receipt")
        by_assessor = {item.assessor: item for item in self.card.forecasts}
        for component in self.pool.receipt.components:
            forecast = by_assessor[component.assessor]
            if (
                component.forecast_id != str(forecast.forecast_id)
                or component.forecast_commit_digest != forecast.commit_digest
                or component.original_distribution != forecast.distribution
            ):
                raise AssemblyError("pool component differs from signed competitor card forecast")

    @property
    def competitor_id(self) -> StableIdentifier:
        return self.card.competitor_id

    @property
    def packet_digest(self) -> str:
        return self.card.packet_digest


@dataclass(frozen=True, slots=True)
class CapabilityPoolBasis:
    pool: PoolResult
    capability_binding: RollingCapabilityBinding

    def __post_init__(self) -> None:
        if not isinstance(self.pool, PoolResult) or not isinstance(
            self.capability_binding, RollingCapabilityBinding
        ):
            raise AssemblyError("capability pool basis must be typed")
        if (
            self.pool.receipt.pooled_distribution != self.pool.distribution
            or self.pool.receipt.capability_state_digest != self.capability_binding.state_digest
        ):
            raise AssemblyError("capability pool basis authority differs")
        expected = {
            PoolMode.NORMAL: (3, True),
            PoolMode.DEGRADED_TWO: (2, True),
            PoolMode.MANUAL_SINGLE: (1, False),
        }.get(self.pool.receipt.mode)
        if expected != (
            self.pool.receipt.available_count,
            self.pool.receipt.is_ensemble,
        ):
            raise AssemblyError("capability pool basis mode/count differs")

    @property
    def distribution(self) -> PositiveTimeDistribution:
        return self.pool.distribution

    def to_dict(self) -> dict[str, Any]:
        return {
            "basis_kind": "capability_pool",
            "pool_receipt_digest": self.pool.receipt.receipt_digest,
            "capability_binding": self.capability_binding.to_dict(),
            "distribution_digest": self.pool.distribution.digest,
        }


def _zero_history_prior_content(values: Mapping[str, Any]) -> dict[str, Any]:
    estimate = values["estimate"]
    if not isinstance(estimate, ZeroHistoryEstimate):
        raise AssemblyError("zero-history prior requires a typed estimate")
    return {
        "schema_version": "strathmark-v3-rolling-zero-history-authority-v1",
        "estimate": estimate.to_dict(),
        "publication_binding_digest": values["publication_binding_digest"],
        "formula_forecast_digest": values["formula_forecast_digest"],
        "formula_component_result_digest": values["formula_component_result_digest"],
        "formula_component_payload_digest": values["formula_component_payload_digest"],
        "formula_manifest_digest": values["formula_manifest_digest"],
        "prior_lineage_digest": values["prior_lineage_digest"],
        "zero_history_policy_digest": values["zero_history_policy_digest"],
    }


@dataclass(frozen=True, slots=True)
class ZeroHistoryPriorBasis:
    estimate: ZeroHistoryEstimate
    publication_binding_digest: str
    formula_forecast_digest: str
    formula_component_result_digest: str
    formula_component_payload_digest: str
    formula_manifest_digest: str
    prior_lineage_digest: str
    zero_history_policy_digest: str
    manifest: SignedManifest
    authority_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.estimate, ZeroHistoryEstimate) or not isinstance(
            self.manifest, SignedManifest
        ):
            raise AssemblyError("zero-history prior basis must be typed")
        for value, label in (
            (self.publication_binding_digest, "zero-history publication"),
            (self.formula_forecast_digest, "zero-history formula forecast"),
            (self.formula_component_result_digest, "zero-history component result"),
            (self.formula_component_payload_digest, "zero-history component payload"),
            (self.formula_manifest_digest, "zero-history Formula manifest"),
            (self.prior_lineage_digest, "zero-history prior lineage"),
            (self.zero_history_policy_digest, "zero-history policy"),
            (self.authority_digest, "zero-history authority"),
        ):
            _digest(value, label)
        content = self.content_value()
        if (
            self.manifest.kind != "rolling_zero_history_authority"
            or self.manifest.body().get("payload") != content
            or self.authority_digest != canonical_digest(content)
            or self.estimate.population_prior_digest != self.prior_lineage_digest
            or self.estimate.policy_digest != self.zero_history_policy_digest
        ):
            raise AssemblyError("zero-history signed authority differs")

    @classmethod
    def create(
        cls,
        *,
        estimate: ZeroHistoryEstimate,
        publication_binding_digest: str,
        formula_forecast_digest: str,
        formula_component_result_digest: str,
        formula_component_payload_digest: str,
        formula_manifest_digest: str,
        prior_lineage_digest: str,
        zero_history_policy_digest: str,
        signer: P256Signer,
        created_at: str,
    ) -> ZeroHistoryPriorBasis:
        values = {
            "estimate": estimate,
            "publication_binding_digest": publication_binding_digest,
            "formula_forecast_digest": formula_forecast_digest,
            "formula_component_result_digest": formula_component_result_digest,
            "formula_component_payload_digest": formula_component_payload_digest,
            "formula_manifest_digest": formula_manifest_digest,
            "prior_lineage_digest": prior_lineage_digest,
            "zero_history_policy_digest": zero_history_policy_digest,
        }
        content = _zero_history_prior_content(values)
        return cls(
            **values,
            manifest=sign_manifest(
                "rolling_zero_history_authority",
                content,
                signer=signer,
                created_at=created_at,
            ),
            authority_digest=canonical_digest(content),
        )

    @property
    def distribution(self) -> PositiveTimeDistribution:
        return self.estimate.distribution

    def content_value(self) -> dict[str, Any]:
        return _zero_history_prior_content(
            {
                name: getattr(self, name)
                for name in self.__dataclass_fields__
                if name not in {"manifest", "authority_digest"}
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "basis_kind": "zero_history_prior",
            **self.content_value(),
            "manifest": self.manifest.to_dict(),
            "authority_digest": self.authority_digest,
        }


@dataclass(frozen=True, slots=True)
class OverrideStartingEstimateBasis:
    state: AcceptedExpectedTimeOverrideState
    distribution: PositiveTimeDistribution
    source_basis: CapabilityPoolBasis | ZeroHistoryPriorBasis
    current_capability_revision: int
    later_evidence_applied: bool
    basis_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.state, AcceptedExpectedTimeOverrideState) or not isinstance(
            self.source_basis, (CapabilityPoolBasis, ZeroHistoryPriorBasis)
        ):
            raise AssemblyError("override starting estimate requires typed authority")
        if not isinstance(self.distribution, PositiveTimeDistribution):
            raise AssemblyError("override starting estimate requires a distribution")
        expected_revision = (
            self.source_basis.capability_binding.state_revision
            if isinstance(self.source_basis, CapabilityPoolBasis)
            else 0
        )
        if (
            self.state.competitor_id
            != (
                self.source_basis.capability_binding.competitor_id
                if isinstance(self.source_basis, CapabilityPoolBasis)
                else self.source_basis.estimate.competitor_id
            )
            or self.current_capability_revision != expected_revision
            or self.later_evidence_applied
            != (expected_revision > self.state.accepted_capability_revision)
            or self.distribution
            != self.state.effective_distribution(
                self.source_basis.distribution,
                current_capability_revision=expected_revision,
            )
        ):
            raise AssemblyError("override starting estimate differs from current evidence")
        _digest(self.basis_digest, "override starting estimate basis")
        if self.basis_digest != canonical_digest(self.content_value()):
            raise AssemblyError("override starting estimate basis digest differs")

    @classmethod
    def create(
        cls,
        state: AcceptedExpectedTimeOverrideState,
        source_basis: CapabilityPoolBasis | ZeroHistoryPriorBasis,
    ) -> OverrideStartingEstimateBasis:
        revision = (
            source_basis.capability_binding.state_revision
            if isinstance(source_basis, CapabilityPoolBasis)
            else 0
        )
        values = {
            "state": state,
            "distribution": state.effective_distribution(
                source_basis.distribution,
                current_capability_revision=revision,
            ),
            "source_basis": source_basis,
            "current_capability_revision": revision,
            "later_evidence_applied": revision > state.accepted_capability_revision,
        }
        return cls(**values, basis_digest=canonical_digest(_override_basis_content(values)))

    def content_value(self) -> dict[str, Any]:
        return _override_basis_content(
            {
                name: getattr(self, name)
                for name in self.__dataclass_fields__
                if name != "basis_digest"
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_value(), "basis_digest": self.basis_digest}


@dataclass(frozen=True, slots=True)
class ManualExpectedTimeBasis:
    competitor_id: StableIdentifier
    distribution: PositiveTimeDistribution
    manual_authority_digest: str
    source_assessor: AssessorKind | None
    basis_digest: str

    def __post_init__(self) -> None:
        require_identifier(self.competitor_id, expected_namespace="competitor")
        if not isinstance(self.distribution, PositiveTimeDistribution):
            raise AssemblyError("manual prediction basis requires a distribution")
        _digest(self.manual_authority_digest, "manual prediction authority")
        if self.source_assessor is not None and not isinstance(self.source_assessor, AssessorKind):
            raise AssemblyError("manual prediction source assessor is invalid")
        _digest(self.basis_digest, "manual prediction basis")
        if self.basis_digest != canonical_digest(self.content_value()):
            raise AssemblyError("manual prediction basis digest differs")

    def content_value(self) -> dict[str, Any]:
        return {
            "basis_kind": "manual_expected_time",
            "competitor_id": str(self.competitor_id),
            "distribution": self.distribution.to_dict(),
            "manual_authority_digest": self.manual_authority_digest,
            "source_assessor": (
                None if self.source_assessor is None else self.source_assessor.value
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_value(), "basis_digest": self.basis_digest}


PredictionBasis = (
    CapabilityPoolBasis
    | ZeroHistoryPriorBasis
    | OverrideStartingEstimateBasis
    | ManualExpectedTimeBasis
)


@dataclass(frozen=True, slots=True)
class CompetitorPredictionEvidence:
    card: CompetitorCardAuthority
    publication: RollingPublicationBinding
    basis: PredictionBasis

    def __post_init__(self) -> None:
        if (
            not isinstance(self.card, CompetitorCardAuthority)
            or not isinstance(self.publication, RollingPublicationBinding)
            or not isinstance(
                self.basis,
                (
                    CapabilityPoolBasis,
                    ZeroHistoryPriorBasis,
                    OverrideStartingEstimateBasis,
                    ManualExpectedTimeBasis,
                ),
            )
            or self.card.competitor_id != self.publication.competitor_id
            or self.card.packet_digest != self.publication.evidence_digest
        ):
            raise AssemblyError("competitor prediction evidence authority differs")
        if isinstance(self.basis, CapabilityPoolBasis):
            CompetitorPoolEvidence(self.card, self.basis.pool)
            if self.basis.capability_binding.competitor_id != self.card.competitor_id:
                raise AssemblyError("capability basis competitor differs")
        elif isinstance(self.basis, ZeroHistoryPriorBasis):
            formula = next(
                (item for item in self.card.forecasts if item.assessor is AssessorKind.FORMULA),
                None,
            )
            if (
                self.basis.estimate.competitor_id != self.card.competitor_id
                or self.basis.publication_binding_digest != self.publication.binding_digest
                or formula is None
                or formula.state is not ForecastState.COMMITTED
                or formula.distribution != self.basis.estimate.distribution
                or formula.support.eligible_count != 0
                or formula.support.exact_context_count != 0
                or formula.commit_digest != self.basis.formula_forecast_digest
                or not any(
                    item.role == "formula_manifest"
                    and item.digest == self.basis.formula_manifest_digest
                    for item in formula.artifacts
                )
            ):
                raise AssemblyError("zero-history basis competitor differs")
        elif isinstance(self.basis, OverrideStartingEstimateBasis):
            CompetitorPredictionEvidence(self.card, self.publication, self.basis.source_basis)
            if self.basis.state.competitor_id != self.card.competitor_id:
                raise AssemblyError("override starting estimate competitor differs")
        elif self.basis.competitor_id != self.card.competitor_id:
            raise AssemblyError("manual basis competitor differs")

    @property
    def competitor_id(self) -> StableIdentifier:
        return self.card.competitor_id

    @property
    def packet_digest(self) -> str:
        return self.card.packet_digest

    @property
    def distribution(self) -> PositiveTimeDistribution:
        return self.basis.distribution

    def to_dict(self) -> dict[str, Any]:
        return {
            "competitor_id": str(self.competitor_id),
            "card_manifest_digest": self.card.manifest.body_digest,
            "packet_digest": self.packet_digest,
            "publication": self.publication.to_dict(),
            "basis": self.basis.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class OperationalDisagreementReceipt:
    """Operational consequence color after exact U14 replay of every sheet."""

    field_revision_digest: str
    decision: DisagreementDecision
    pooled_optimizer: VerifiedOptimizerReceipt
    component_optimizers: tuple[tuple[AssessorKind, VerifiedOptimizerReceipt], ...]
    component_joint_draws: tuple[tuple[AssessorKind, JointDraws], ...]
    policy_manifest: SignedManifest
    council_manifest: SignedManifest | None
    receipt_digest: str

    def __post_init__(self) -> None:
        _digest(self.field_revision_digest, "disagreement field revision")
        if not isinstance(self.decision, DisagreementDecision) or not isinstance(
            self.pooled_optimizer, VerifiedOptimizerReceipt
        ):
            raise AssemblyError("operational disagreement authority must be typed")
        expected_sources = tuple(item.source for item in self.decision.component_sheets)
        if (
            not isinstance(self.component_optimizers, tuple)
            or tuple(item for item, _receipt in self.component_optimizers) != expected_sources
            or not all(
                isinstance(item, AssessorKind) and isinstance(receipt, VerifiedOptimizerReceipt)
                for item, receipt in self.component_optimizers
            )
        ):
            raise AssemblyError(
                "operational disagreement requires every ordered U14 component replay"
            )
        if tuple(
            item for item, _draws in self.component_joint_draws
        ) != expected_sources or not all(
            isinstance(draws, JointDraws) for _source, draws in self.component_joint_draws
        ):
            raise AssemblyError(
                "operational disagreement requires every typed component joint replay"
            )
        if (
            not isinstance(self.policy_manifest, SignedManifest)
            or self.policy_manifest.kind != "field_disagreement_policy_authority"
        ):
            raise AssemblyError("disagreement policy lacks installed authority")
        if self.decision.council_audit is None:
            if self.council_manifest is not None:
                raise AssemblyError("unavailable council cannot carry field audit authority")
        elif (
            not isinstance(self.council_manifest, SignedManifest)
            or self.council_manifest.kind != "field_council_audit_authority"
        ):
            raise AssemblyError("council disagreement lacks signed U11 audit authority")
        if self.decision.pooled_sheet != counterfactual_sheet_from_optimizer(
            "pooled", self.pooled_optimizer
        ):
            raise AssemblyError("pooled disagreement sheet differs from U14 replay")
        pooled_field = self.pooled_optimizer.field
        for sheet, (source, receipt), (draw_source, draws) in zip(
            self.decision.component_sheets,
            self.component_optimizers,
            self.component_joint_draws,
            strict=True,
        ):
            if sheet != counterfactual_sheet_from_optimizer(source, receipt):
                raise AssemblyError("component disagreement sheet differs from U14 replay")
            if (
                draw_source is not source
                or receipt.field.joint_samples_digest != draws.joint_samples_digest
                or receipt.receipt.joint_samples_digest != draws.joint_samples_digest
            ):
                raise AssemblyError("component U14 replay differs from typed U13 joint draws")
            if receipt.field.field_id != pooled_field.field_id or tuple(
                item.competitor_id for item in receipt.field.competitors
            ) != tuple(item.competitor_id for item in pooled_field.competitors):
                raise AssemblyError("component U14 replay differs from pooled field roster")
        _digest(self.receipt_digest, "operational disagreement")
        if self.receipt_digest != canonical_digest(self.content_value()):
            raise AssemblyError("operational disagreement receipt digest differs")

    @classmethod
    def create(
        cls,
        *,
        field_revision_digest: str,
        decision: DisagreementDecision,
        pooled_optimizer: VerifiedOptimizerReceipt,
        component_optimizers: tuple[tuple[AssessorKind, VerifiedOptimizerReceipt], ...],
        component_joint_draws: tuple[tuple[AssessorKind, JointDraws], ...],
        policy_manifest: SignedManifest,
        council_manifest: SignedManifest | None,
    ) -> OperationalDisagreementReceipt:
        values = {
            "field_revision_digest": field_revision_digest,
            "decision": decision,
            "pooled_optimizer": pooled_optimizer,
            "component_optimizers": component_optimizers,
            "component_joint_draws": component_joint_draws,
            "policy_manifest": policy_manifest,
            "council_manifest": council_manifest,
        }
        return cls(
            **values,
            receipt_digest=canonical_digest(_operational_disagreement_content(values)),
        )

    @property
    def color(self) -> ConsequenceColor:
        return self.decision.color

    def content_value(self) -> dict[str, Any]:
        return _operational_disagreement_content(
            {
                name: getattr(self, name)
                for name in self.__dataclass_fields__
                if name != "receipt_digest"
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_value(),
            "verification_status": "verified",
            "receipt_digest": self.receipt_digest,
        }

    def to_authority_dict(self) -> dict[str, Any]:
        """Return the complete deterministic replay body for durable storage."""

        common_random_plan = _component_common_random_plan_authority(self.component_joint_draws)
        component_optimizers = [
            [source.value, receipt.to_authority_dict()]
            for source, receipt in self.component_optimizers
        ]
        return {
            "schema_version": "strathmark-v3-operational-disagreement-authority-v3",
            "field_revision_digest": self.field_revision_digest,
            "decision": self.decision.to_dict(),
            "pooled_optimizer": self.pooled_optimizer.to_authority_dict(),
            "component_optimizers": component_optimizers,
            "component_common_random_plan": common_random_plan,
            "component_joint_draws": [
                [source.value, _compact_component_joint_draw(draws)]
                for source, draws in self.component_joint_draws
            ],
            "policy_manifest": self.policy_manifest.to_dict(),
            "council_manifest": (
                None if self.council_manifest is None else self.council_manifest.to_dict()
            ),
            "receipt_digest": self.receipt_digest,
        }

    @property
    def canonical_authority_payload(self) -> bytes:
        """Serialize this fully typed authority without a second tree validation."""

        try:
            encoded = json.dumps(
                self.to_authority_dict(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise AssemblyError("operational disagreement authority is not canonical JSON") from exc
        if len(encoded) > 16_777_216:
            raise AssemblyError("operational disagreement authority exceeds capacity")
        return encoded

    @classmethod
    def from_authority_dict(cls, value: dict[str, Any]) -> OperationalDisagreementReceipt:
        common_expected = {
            "schema_version",
            "field_revision_digest",
            "decision",
            "pooled_optimizer",
            "component_optimizers",
            "component_joint_draws",
            "policy_manifest",
            "council_manifest",
            "receipt_digest",
        }
        schema_version = value.get("schema_version") if isinstance(value, dict) else None
        expected = (
            common_expected | {"component_common_random_plan"}
            if schema_version
            in {
                "strathmark-v3-operational-disagreement-authority-v2",
                "strathmark-v3-operational-disagreement-authority-v3",
            }
            else common_expected
        )
        if (
            not isinstance(value, dict)
            or set(value) != expected
            or schema_version
            not in {
                "strathmark-v3-operational-disagreement-authority-v1",
                "strathmark-v3-operational-disagreement-authority-v2",
                "strathmark-v3-operational-disagreement-authority-v3",
            }
            or not isinstance(value.get("decision"), dict)
            or not isinstance(value.get("pooled_optimizer"), dict)
            or not isinstance(value.get("component_optimizers"), list)
            or not isinstance(value.get("component_joint_draws"), list)
            or not isinstance(value.get("policy_manifest"), dict)
        ):
            raise AssemblyConflict("operational disagreement authority blob schema differs")
        council_value = value["council_manifest"]
        if council_value is not None and not isinstance(council_value, dict):
            raise AssemblyConflict("operational disagreement council authority blob differs")
        optimizer_rows = value["component_optimizers"]
        draw_rows = value["component_joint_draws"]
        if not 2 <= len(optimizer_rows) <= 3 or len(draw_rows) != len(optimizer_rows):
            raise AssemblyConflict("operational disagreement component cardinality differs")
        if any(
            not isinstance(item, list)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], dict)
            for item in (*optimizer_rows, *draw_rows)
        ):
            raise AssemblyConflict("operational disagreement component authority shape differs")
        optimizer_sources = tuple(item[0] for item in optimizer_rows)
        draw_sources = tuple(item[0] for item in draw_rows)
        allowed_sources = {item.value for item in _OUTER_ASSESSORS}
        if (
            optimizer_sources != draw_sources
            or len(set(optimizer_sources)) != len(optimizer_sources)
            or not set(optimizer_sources) <= allowed_sources
        ):
            raise AssemblyConflict("operational disagreement component sources differ")
        component_optimizers: tuple[tuple[AssessorKind, VerifiedOptimizerReceipt], ...] | None = (
            None
        )
        if schema_version == "strathmark-v3-operational-disagreement-authority-v2":
            draw_rows = _inflate_component_joint_draw_authorities(
                value["component_common_random_plan"], draw_rows
            )
        elif schema_version == "strathmark-v3-operational-disagreement-authority-v3":
            try:
                component_optimizers = tuple(
                    (
                        AssessorKind(item[0]),
                        VerifiedOptimizerReceipt.from_authority_dict(item[1]),
                    )
                    for item in optimizer_rows
                )
            except (ContractError, KeyError, TypeError, ValueError) as exc:
                raise AssemblyConflict(
                    "operational disagreement optimizer authority blob differs"
                ) from exc
            draw_rows = _inflate_component_joint_draw_authorities(
                value["component_common_random_plan"],
                draw_rows,
                optimizer_receipts=component_optimizers,
            )
        try:
            if component_optimizers is None:
                component_optimizers = tuple(
                    (
                        AssessorKind(item[0]),
                        VerifiedOptimizerReceipt.from_authority_dict(item[1]),
                    )
                    for item in optimizer_rows
                    if isinstance(item, list) and len(item) == 2
                )
            component_draws = tuple(
                (AssessorKind(item[0]), JointDraws.from_dict(item[1]))
                for item in draw_rows
                if isinstance(item, list) and len(item) == 2
            )
            if len(component_optimizers) != len(optimizer_rows):
                raise AssemblyConflict("operational disagreement optimizer authority blob differs")
            if len(component_draws) != len(draw_rows):
                raise AssemblyConflict("operational disagreement draw authority blob differs")
            authority = cls(
                value["field_revision_digest"],
                DisagreementDecision.from_dict(value["decision"]),
                VerifiedOptimizerReceipt.from_authority_dict(value["pooled_optimizer"]),
                component_optimizers,
                component_draws,
                SignedManifest.from_dict(value["policy_manifest"]),
                (None if council_value is None else SignedManifest.from_dict(council_value)),
                value["receipt_digest"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AssemblyConflict("operational disagreement authority blob differs") from exc
        return authority

    @classmethod
    def from_dict(
        cls,
        value: dict[str, Any],
        *,
        authority_resolver: Callable[[str], OperationalDisagreementReceipt | None],
    ) -> OperationalDisagreementReceipt:
        """Decode a bounded summary through content-addressed verified authority.

        The 4096-draw U14 replay bodies remain outside the judge-facing field
        receipt.  A VERIFIED decode therefore always requires the durable blob
        resolver; summary bytes alone never mint operational authority.
        """

        expected = {
            "schema_version",
            "field_revision_digest",
            "decision_digest",
            "color",
            "policy_digest",
            "pooled_optimizer_verification_digest",
            "component_optimizer_verification_digests",
            "component_joint_draw_digests",
            "policy_manifest_digest",
            "council_manifest_digest",
            "verification_status",
            "receipt_digest",
        }
        if (
            not isinstance(value, dict)
            or set(value) != expected
            or value.get("schema_version") != "strathmark-v3-operational-disagreement-receipt-v1"
            or value.get("verification_status") != "verified"
        ):
            raise AssemblyConflict("operational disagreement summary schema differs")
        digest = value.get("receipt_digest")
        try:
            _digest(digest, "operational disagreement receipt")
            resolved = authority_resolver(digest)
        except (TypeError, ValueError, AssemblyError) as exc:
            raise AssemblyConflict("operational disagreement authority resolver failed") from exc
        if not isinstance(resolved, cls):
            raise AssemblyConflict("operational disagreement authority blob is missing")
        if resolved.receipt_digest != digest or resolved.to_dict() != value:
            raise AssemblyConflict(
                "operational disagreement summary differs from resolved authority"
            )
        return resolved


def counterfactual_sheet_from_optimizer(
    source: AssessorKind | str,
    verified: VerifiedOptimizerReceipt,
) -> CounterfactualSheet:
    with localcontext() as context:
        context.prec = 96
        context.rounding = ROUND_HALF_EVEN
        return _counterfactual_sheet_from_optimizer(source, verified)


def _counterfactual_sheet_from_optimizer(
    source: AssessorKind | str,
    verified: VerifiedOptimizerReceipt,
) -> CounterfactualSheet:
    if source != "pooled" and not isinstance(source, AssessorKind):
        raise AssemblyError("counterfactual source is invalid")
    if not isinstance(verified, VerifiedOptimizerReceipt):
        raise AssemblyError("counterfactual sheet requires verified U14 authority")
    from strathmark.v3.domain.optimizer import _win_probabilities

    probabilities = _win_probabilities(verified.field, verified.receipt.selected_marks)
    competitors = []
    for row, mark, probability in zip(
        verified.field.competitors,
        verified.receipt.selected_marks,
        probabilities,
        strict=True,
    ):
        ordered = tuple(sorted(row.samples_ms))
        competitors.append(
            CounterfactualCompetitor(
                row.competitor_id,
                row.expected_time_ms,
                ordered[(len(ordered) - 1) // 10],
                ordered[((len(ordered) - 1) * 9) // 10],
                mark,
                probability,
            )
        )
    with localcontext() as context:
        context.prec = 96
        context.rounding = ROUND_HALF_EVEN
        spread = int(
            Decimal(verified.receipt.selected_objectives.expected_finish_spread_ms).quantize(
                Decimal(1)
            )
        )
    return CounterfactualSheet.create(
        source=source,
        competitors=tuple(competitors),
        expected_spread_ms=spread,
        joint_draw_digest=verified.field.joint_samples_digest,
        optimizer_digest=verified.receipt.receipt_digest,
        optimizer_verification_status=OptimizerVerificationStatus.PENDING,
    )


class ManualConstructionMode(str, Enum):
    EXACT_SINGLE_SURVIVOR = "exact_single_survivor"
    COMPLETE_EXPECTED_TIME = "complete_expected_time"


def seal_disagreement_policy_authority(
    policy: Any,
    *,
    bundle_digest: str,
    signer: P256Signer,
    created_at: str,
) -> SignedManifest:
    _digest(bundle_digest, "disagreement policy bundle")
    if not hasattr(policy, "to_dict") or not hasattr(policy, "digest"):
        raise AssemblyError("disagreement policy authority must be typed")
    return sign_manifest(
        "field_disagreement_policy_authority",
        {
            "schema_version": "strathmark-v3-field-disagreement-policy-authority-v1",
            "purpose": "field_disagreement_operational",
            "bundle_digest": bundle_digest,
            "policy": policy.to_dict(),
            "policy_digest": policy.digest,
        },
        signer=signer,
        created_at=created_at,
    )


def seal_council_field_audit_authority(
    council_audit: Any,
    *,
    field_revision_digest: str,
    card_manifest_digests: tuple[str, ...],
    signer: P256Signer,
    created_at: str,
) -> SignedManifest:
    _digest(field_revision_digest, "council field revision")
    for value in card_manifest_digests:
        _digest(value, "council card manifest")
    if not hasattr(council_audit, "to_dict") or not hasattr(council_audit, "audit_digest"):
        raise AssemblyError("council field audit authority must be typed")
    return sign_manifest(
        "field_council_audit_authority",
        {
            "schema_version": "strathmark-v3-field-council-audit-authority-v1",
            "purpose": "field_council_operational",
            "field_revision_digest": field_revision_digest,
            "card_manifest_digests": list(card_manifest_digests),
            "council_audit": council_audit.to_dict(),
            "council_audit_digest": council_audit.audit_digest,
        },
        signer=signer,
        created_at=created_at,
    )


@dataclass(frozen=True, slots=True, order=True)
class ManualCompetitorEstimate:
    competitor_id: StableIdentifier
    distribution: PositiveTimeDistribution
    source_assessor: AssessorKind | None

    def __post_init__(self) -> None:
        require_identifier(self.competitor_id, expected_namespace="competitor")
        if not isinstance(self.distribution, PositiveTimeDistribution):
            raise AssemblyError("manual estimate requires a positive distribution")
        if self.source_assessor is not None and self.source_assessor not in _OUTER_ASSESSORS:
            raise AssemblyError("manual estimate survivor assessor is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "competitor_id": str(self.competitor_id),
            "distribution": self.distribution.to_dict(),
            "source_assessor": (
                None if self.source_assessor is None else self.source_assessor.value
            ),
        }


@dataclass(frozen=True, slots=True)
class ManualFieldAuthority:
    mode: ManualConstructionMode
    field_revision_digest: str
    estimates: tuple[ManualCompetitorEstimate, ...]
    actor_id: StableIdentifier
    reason_code: str
    scope: OverrideScope
    created_at: str
    authority_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.mode, ManualConstructionMode):
            raise AssemblyError("manual field mode must be typed")
        _digest(self.field_revision_digest, "manual field revision")
        if (
            not isinstance(self.estimates, tuple)
            or not self.estimates
            or not all(isinstance(item, ManualCompetitorEstimate) for item in self.estimates)
        ):
            raise AssemblyError("manual field authority requires complete typed estimates")
        identities = tuple(str(item.competitor_id) for item in self.estimates)
        if identities != tuple(sorted(identities)) or len(identities) != len(set(identities)):
            raise AssemblyError("manual field estimates must be unique and sorted")
        require_identifier(self.actor_id, expected_namespace="actor")
        expected_reason = {
            ManualConstructionMode.EXACT_SINGLE_SURVIVOR: "judge_single_survivor_acceptance",
            ManualConstructionMode.COMPLETE_EXPECTED_TIME: "judge_complete_expected_time_construction",
        }[self.mode]
        if self.reason_code != expected_reason:
            raise AssemblyError("manual field reason does not match deliberate mode")
        if self.scope is not OverrideScope.UPCOMING_RACE:
            raise AssemblyError("manual field authority is restricted to its exact race")
        require_utc_milliseconds(self.created_at)
        _digest(self.authority_digest, "manual field authority")
        if self.authority_digest != canonical_digest(self.content_value()):
            raise AssemblyError("manual field authority digest differs")

    @classmethod
    def create(cls, **values: Any) -> ManualFieldAuthority:
        estimates = tuple(sorted(values["estimates"], key=lambda item: str(item.competitor_id)))
        normalized = {
            "mode": ManualConstructionMode(values["mode"]),
            "field_revision_digest": values["field_revision_digest"],
            "estimates": estimates,
            "actor_id": require_identifier(values["actor_id"], expected_namespace="actor"),
            "reason_code": values["reason_code"],
            "scope": OverrideScope(values["scope"]),
            "created_at": values["created_at"],
        }
        return cls(
            **normalized,
            authority_digest=canonical_digest(_manual_field_content(normalized)),
        )

    def content_value(self) -> dict[str, Any]:
        return _manual_field_content(
            {
                name: getattr(self, name)
                for name in self.__dataclass_fields__
                if name != "authority_digest"
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_value(), "authority_digest": self.authority_digest}


@dataclass(frozen=True, slots=True)
class ManualConstructionSubmission:
    prior_receipt_id: StableIdentifier | None
    prior_receipt_digest: str | None
    upstream_field_revision: int
    field_revision_digest: str
    manual_authority_digest: str
    actor_id: StableIdentifier
    reason_code: str
    scope: OverrideScope
    submitted_at: str
    submission_digest: str

    def __post_init__(self) -> None:
        if (self.prior_receipt_id is None) != (self.prior_receipt_digest is None):
            raise AssemblyError(
                "construction predecessor identity and digest must both be present or absent"
            )
        if self.prior_receipt_id is not None:
            require_identifier(self.prior_receipt_id, expected_namespace="receipt")
            _digest(self.prior_receipt_digest, "construction prior receipt")
        if (
            isinstance(self.upstream_field_revision, bool)
            or not isinstance(self.upstream_field_revision, int)
            or self.upstream_field_revision <= 0
        ):
            raise AssemblyError("construction upstream revision must be positive")
        _digest(self.field_revision_digest, "construction field revision")
        _digest(self.manual_authority_digest, "construction manual authority")
        require_identifier(self.actor_id, expected_namespace="actor")
        if self.reason_code not in {
            "judge_single_survivor_acceptance",
            "judge_complete_expected_time_construction",
        }:
            raise AssemblyError("construction requires the deliberate reason code")
        if self.scope is not OverrideScope.UPCOMING_RACE:
            raise AssemblyError("construction submission must bind the exact race")
        require_utc_milliseconds(self.submitted_at)
        _digest(self.submission_digest, "construction submission")
        if self.submission_digest != canonical_digest(self.content_value()):
            raise AssemblyError("construction submission digest differs")

    @classmethod
    def create(cls, **values: Any) -> ManualConstructionSubmission:
        normalized = {
            "prior_receipt_id": (
                None
                if values.get("prior_receipt_id") is None
                else require_identifier(values["prior_receipt_id"], expected_namespace="receipt")
            ),
            "prior_receipt_digest": values.get("prior_receipt_digest"),
            "upstream_field_revision": values["upstream_field_revision"],
            "field_revision_digest": values["field_revision_digest"],
            "manual_authority_digest": values["manual_authority_digest"],
            "actor_id": require_identifier(values["actor_id"], expected_namespace="actor"),
            "reason_code": values["reason_code"],
            "scope": OverrideScope(values["scope"]),
            "submitted_at": values["submitted_at"],
        }
        provisional = cls(
            **normalized,
            submission_digest=canonical_digest(_manual_construction_submission_content(normalized)),
        )
        return provisional

    def content_value(self) -> dict[str, Any]:
        return _manual_construction_submission_content(
            {
                name: getattr(self, name)
                for name in self.__dataclass_fields__
                if name != "submission_digest"
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_value(), "submission_digest": self.submission_digest}


@dataclass(frozen=True, slots=True)
class OperationalExpectedTimeOverrideAuthority:
    prior_receipt_id: StableIdentifier
    prior_receipt_digest: str
    upstream_field_revision: int
    field_revision_digest: str
    reason_code: str
    override_receipt: ExpectedTimeOverrideReceipt
    after_optimizer_verification_digest: str
    authority_digest: str

    def __post_init__(self) -> None:
        require_identifier(self.prior_receipt_id, expected_namespace="receipt")
        _digest(self.prior_receipt_digest, "override prior receipt")
        if (
            isinstance(self.upstream_field_revision, bool)
            or not isinstance(self.upstream_field_revision, int)
            or self.upstream_field_revision <= 0
        ):
            raise AssemblyError("override upstream revision must be positive")
        _digest(self.field_revision_digest, "override field revision")
        if self.reason_code != "judge_expected_time_override":
            raise AssemblyError("override authority reason code is not supported")
        if not isinstance(self.override_receipt, ExpectedTimeOverrideReceipt):
            raise AssemblyError("override authority requires a typed U13 receipt")
        _digest(
            self.after_optimizer_verification_digest,
            "override optimizer verification",
        )
        _digest(self.authority_digest, "operational override authority")
        if self.authority_digest != canonical_digest(self.content_value()):
            raise AssemblyError("operational override authority digest differs")

    @classmethod
    def create(cls, **values: Any) -> OperationalExpectedTimeOverrideAuthority:
        normalized = {
            "prior_receipt_id": require_identifier(
                values["prior_receipt_id"], expected_namespace="receipt"
            ),
            "prior_receipt_digest": values["prior_receipt_digest"],
            "upstream_field_revision": values["upstream_field_revision"],
            "field_revision_digest": values["field_revision_digest"],
            "reason_code": values.get("reason_code", "judge_expected_time_override"),
            "override_receipt": values["override_receipt"],
            "after_optimizer_verification_digest": values["after_optimizer_verification_digest"],
        }
        return cls(
            **normalized,
            authority_digest=canonical_digest(_operational_override_authority_content(normalized)),
        )

    def content_value(self) -> dict[str, Any]:
        return _operational_override_authority_content(
            {
                name: getattr(self, name)
                for name in self.__dataclass_fields__
                if name != "authority_digest"
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_value(), "authority_digest": self.authority_digest}


@dataclass(frozen=True, slots=True)
class SealedPipelineOutput:
    field_revision_digest: str
    prediction_evidence: tuple[CompetitorPredictionEvidence, ...]
    joint_draws: JointDraws
    optimizer: VerifiedOptimizerReceipt
    disagreement: OperationalDisagreementReceipt | None
    weight_authority: WeightAuthorityBinding
    operational_weight_authority: OperationalWeightAuthority
    dependence_artifact: DependenceArtifact
    manual_authority: ManualFieldAuthority | None
    construction_submission: ManualConstructionSubmission | None
    expected_time_override: OperationalExpectedTimeOverrideAuthority | None
    total_latency_ms: int
    pipeline_digest: str

    def __post_init__(self) -> None:
        _digest(self.field_revision_digest, "field revision")
        if not self.prediction_evidence or not all(
            isinstance(item, CompetitorPredictionEvidence) for item in self.prediction_evidence
        ):
            raise AssemblyError("sealed pipeline requires typed per-competitor prediction evidence")
        if len({item.competitor_id for item in self.prediction_evidence}) != len(
            self.prediction_evidence
        ):
            raise AssemblyError("sealed pipeline prediction roster is invalid")
        if not isinstance(self.joint_draws, JointDraws) or not isinstance(
            self.optimizer, VerifiedOptimizerReceipt
        ):
            raise AssemblyError("sealed pipeline requires typed joint and optimizer receipts")
        if self.disagreement is not None:
            if not isinstance(self.disagreement, OperationalDisagreementReceipt):
                raise AssemblyError("pipeline disagreement must be typed")
            if (
                self.disagreement.field_revision_digest != self.field_revision_digest
                or self.disagreement.pooled_optimizer != self.optimizer
            ):
                raise AssemblyError(
                    "operational disagreement differs from field optimizer authority"
                )
            cards = {item.competitor_id: item.card for item in self.prediction_evidence}
            component_draws = dict(self.disagreement.component_joint_draws)
            for source, verified in self.disagreement.component_optimizers:
                component_commits = [
                    cards[item.competitor_id]
                    .forecasts[_OUTER_ASSESSORS.index(source)]
                    .commit_digest
                    for item in verified.field.competitors
                ]
                expected_pool_digest = canonical_digest(
                    {
                        "schema_version": "strathmark-v3-component-card-set-v1",
                        "source": source.value,
                        "forecast_commit_digests": component_commits,
                    }
                )
                expected_source_digest = canonical_digest(
                    {
                        "schema_version": "strathmark-v3-component-counterfactual-source-v1",
                        "field_revision_digest": self.field_revision_digest,
                        "source": source.value,
                        "card_pool_digest": expected_pool_digest,
                        "dependence_artifact_digest": self.dependence_artifact.artifact_digest,
                        "crn_slots": [
                            [
                                str(draw.competitor_id),
                                draw.draw_slot,
                                draw.crn_index,
                            ]
                            for draw in self.joint_draws.competitors
                        ],
                    }
                )
                exact_forecasts = tuple(
                    FieldCompetitorForecast(
                        draw.competitor_id,
                        draw.draw_slot,
                        cards[draw.competitor_id]
                        .forecasts[_OUTER_ASSESSORS.index(source)]
                        .distribution,
                        draw.crn_index,
                    )
                    for draw in self.joint_draws.competitors
                )
                recorded_draws = component_draws[source]
                component_model = bind_field_dependence(
                    self.dependence_artifact,
                    self.dependence_artifact.target_context,
                    field_id=recorded_draws.inputs.field_id,
                )
                replayed_draws = recorded_draws
                if not has_fresh_joint_generation_proof(recorded_draws):
                    replayed_draws = generate_joint_draws(
                        exact_forecasts,
                        component_model,
                        installed_artifact=self.dependence_artifact,
                        seed=recorded_draws.inputs.seed,
                        draw_count=recorded_draws.inputs.draw_count,
                    )
                expected_optimizer_field = OptimizationField.from_joint_draws(
                    recorded_draws,
                    forecasts=exact_forecasts,
                    source_receipt_digest=expected_source_digest,
                    pool_receipt_digest=expected_pool_digest,
                )
                if (
                    verified.field.pool_receipt_digest != expected_pool_digest
                    or verified.field.source_receipt_digest != expected_source_digest
                    or recorded_draws.inputs.seed != self.joint_draws.inputs.seed
                    or recorded_draws.common_random_map_digest
                    != self.joint_draws.common_random_map_digest
                    or tuple(item.common_uniforms for item in recorded_draws.competitors)
                    != tuple(item.common_uniforms for item in self.joint_draws.competitors)
                    or recorded_draws != replayed_draws
                    or verified.field != expected_optimizer_field
                    or any(
                        row.distribution_digest
                        != cards[row.competitor_id]
                        .forecasts[_OUTER_ASSESSORS.index(source)]
                        .distribution.digest
                        for row in verified.field.competitors
                    )
                ):
                    raise AssemblyError(
                        "component optimizer is not bound to exact signed cards and dependence"
                    )
        if (
            not isinstance(self.weight_authority, WeightAuthorityBinding)
            or not isinstance(self.operational_weight_authority, OperationalWeightAuthority)
            or not isinstance(self.dependence_artifact, DependenceArtifact)
        ):
            raise AssemblyError("pipeline requires typed installed authority candidates")
        if self.operational_weight_authority.binding != self.weight_authority:
            raise AssemblyError("operational weight authority differs from pooled U13 binding")
        if self.construction_submission is not None and self.expected_time_override is not None:
            raise AssemblyError("pipeline cannot mix construction and override authority")
        if self.construction_submission is not None:
            expected_submission_reason = (
                "judge_single_survivor_acceptance"
                if self.manual_authority is not None
                and self.manual_authority.mode is ManualConstructionMode.EXACT_SINGLE_SURVIVOR
                else "judge_complete_expected_time_construction"
            )
            if (
                not isinstance(self.construction_submission, ManualConstructionSubmission)
                or self.manual_authority is None
                or self.construction_submission.field_revision_digest != self.field_revision_digest
                or self.construction_submission.manual_authority_digest
                != self.manual_authority.authority_digest
                or self.construction_submission.actor_id != self.manual_authority.actor_id
                or self.construction_submission.reason_code != expected_submission_reason
            ):
                raise AssemblyError(
                    "construction submission differs from complete manual authority"
                )
        if self.expected_time_override is not None:
            if (
                not isinstance(
                    self.expected_time_override,
                    OperationalExpectedTimeOverrideAuthority,
                )
                or self.expected_time_override.field_revision_digest != self.field_revision_digest
                or self.expected_time_override.after_optimizer_verification_digest
                != self.optimizer.verification_digest
            ):
                raise AssemblyError("expected-time override differs from field optimizer authority")
        identities = tuple(item.competitor_id for item in self.prediction_evidence)
        joint_identities = tuple(item.competitor_id for item in self.joint_draws.competitors)
        optimizer_ids = self.optimizer.receipt.competitor_ids
        if identities != joint_identities or identities != optimizer_ids:
            raise AssemblyError("pool, joint, and optimizer rosters differ")
        if any(
            capability.pool.receipt.weight_authority != self.weight_authority
            for item in self.prediction_evidence
            if (capability := _capability_basis(item.basis)) is not None
        ):
            raise AssemblyError("pipeline pools do not share one frozen weight authority")
        valid_source_sets = tuple(
            tuple(
                forecast.assessor
                for forecast in item.card.forecasts
                if forecast.state.value == "committed"
            )
            for item in self.prediction_evidence
        )
        if self.disagreement is not None:
            if len(set(valid_source_sets)) != 1:
                raise AssemblyError(
                    "operational disagreement cannot cover mixed assessor availability"
                )
            exact_sources = valid_source_sets[0]
            recorded_sources = tuple(
                item.source for item in self.disagreement.decision.component_sheets
            )
            if recorded_sources != exact_sources:
                raise AssemblyError(
                    "operational disagreement omits or adds a valid assessor source"
                )
            audit = self.disagreement.decision.council_audit
            if audit is not None:
                valid_members = sum(
                    member.status is CouncilMemberStatus.VALID for member in audit.members
                )
                if valid_members < 2 and AssessorKind.LLM_COUNCIL in exact_sources:
                    raise AssemblyError("council unavailable with fewer than two valid members")
                card_by_id = {item.competitor_id: item.card for item in self.prediction_evidence}
                council_index = _OUTER_ASSESSORS.index(AssessorKind.LLM_COUNCIL)
                expected_forecast_digest = canonical_digest(
                    [
                        card_by_id[competitor_id].forecasts[council_index].commit_digest
                        for competitor_id in identities
                    ]
                )
                expected_evidence_digest = canonical_digest(
                    [card_by_id[competitor_id].packet_digest for competitor_id in identities]
                )
                epochs = {
                    card_by_id[competitor_id].evidence_packet.tournament_epoch_id
                    for competitor_id in identities
                }
                if (
                    audit.aggregate_forecast_digest != expected_forecast_digest
                    or audit.evidence_digest != expected_evidence_digest
                    or len(epochs) != 1
                    or audit.evidence_epoch_id != next(iter(epochs))
                ):
                    raise AssemblyError("council audit differs from exact signed field cards")
        counts = tuple(len(items) for items in valid_source_sets)
        availability = min(counts)
        mixed_availability = len(set(valid_source_sets)) != 1
        if availability >= 2 and not mixed_availability:
            if self.manual_authority is not None:
                raise AssemblyError("ordinary field cannot carry manual construction authority")
            if any(
                isinstance(item.basis, ManualExpectedTimeBasis) for item in self.prediction_evidence
            ):
                raise AssemblyError("ordinary field cannot carry manual prediction bases")
            basis = {item.competitor_id: item.distribution for item in self.prediction_evidence}
        else:
            survivor_sets = tuple(
                tuple(
                    forecast.assessor
                    for forecast in item.card.forecasts
                    if forecast.state.value == "committed"
                )
                for item in self.prediction_evidence
            )
            exact_single = (
                counts == (1,) * len(counts) and len({items[0] for items in survivor_sets}) == 1
            )
            expected_mode = (
                ManualConstructionMode.EXACT_SINGLE_SURVIVOR
                if exact_single
                else ManualConstructionMode.COMPLETE_EXPECTED_TIME
            )
            if (
                not isinstance(self.manual_authority, ManualFieldAuthority)
                or self.manual_authority.mode is not expected_mode
                or self.manual_authority.field_revision_digest != self.field_revision_digest
                or tuple(item.competitor_id for item in self.manual_authority.estimates)
                != tuple(sorted(identities, key=str))
            ):
                raise AssemblyError(
                    "one-or-zero assessor field lacks complete deliberate manual authority"
                )
            basis = {
                item.competitor_id: item.distribution for item in self.manual_authority.estimates
            }
            if expected_mode is ManualConstructionMode.EXACT_SINGLE_SURVIVOR:
                for item in self.prediction_evidence:
                    available_components = tuple(
                        forecast
                        for forecast in item.card.forecasts
                        if forecast.state.value == "committed"
                    )
                    estimate = next(
                        row
                        for row in self.manual_authority.estimates
                        if row.competitor_id == item.competitor_id
                    )
                    if (
                        len(available_components) != 1
                        or not isinstance(item.basis, CapabilityPoolBasis)
                        or estimate.source_assessor is not available_components[0].assessor
                        or estimate.distribution != item.basis.distribution
                    ):
                        raise AssemblyError(
                            "single-survivor authority differs from exact surviving assessor"
                        )
            else:
                by_id = {item.competitor_id: item for item in self.manual_authority.estimates}
                if any(
                    estimate.source_assessor is not None
                    for estimate in self.manual_authority.estimates
                ) or any(
                    not isinstance(item.basis, ManualExpectedTimeBasis)
                    or item.basis.manual_authority_digest != self.manual_authority.authority_digest
                    or item.basis.distribution != by_id[item.competitor_id].distribution
                    or item.basis.source_assessor is not None
                    for item in self.prediction_evidence
                ):
                    raise AssemblyError("complete manual construction basis differs from authority")
        for draw in self.joint_draws.competitors:
            distribution = basis[draw.competitor_id]
            if distribution is None or draw.distribution_digest != distribution.digest:
                raise AssemblyError("joint draws do not bind exact pooled/manual distributions")
        basis_digests = [_prediction_basis_digest(item.basis) for item in self.prediction_evidence]
        manual_authority_digest = (
            None if self.manual_authority is None else self.manual_authority.authority_digest
        )
        expected_pool_digest = canonical_digest(
            {
                "prediction_basis_digests": basis_digests,
                "manual_authority_digest": manual_authority_digest,
            }
        )
        expected_source_digest = canonical_digest(
            {
                "field": self.field_revision_digest,
                "prediction_basis_digests": basis_digests,
                "manual_authority_digest": manual_authority_digest,
            }
        )
        expected_optimizer_forecasts = tuple(
            FieldCompetitorForecast(
                draw.competitor_id,
                draw.draw_slot,
                basis[draw.competitor_id],
                draw.crn_index,
            )
            for draw in self.joint_draws.competitors
        )
        replayed_joint_draws = self.joint_draws
        if not has_fresh_joint_generation_proof(self.joint_draws):
            pooled_model = bind_field_dependence(
                self.dependence_artifact,
                self.dependence_artifact.target_context,
                field_id=self.joint_draws.inputs.field_id,
            )
            replayed_joint_draws = generate_joint_draws(
                expected_optimizer_forecasts,
                pooled_model,
                installed_artifact=self.dependence_artifact,
                seed=self.joint_draws.inputs.seed,
                draw_count=self.joint_draws.inputs.draw_count,
            )
        if self.joint_draws != replayed_joint_draws:
            raise AssemblyError(
                "pooled optimizer is not bound to exact pooled distributions and dependence"
            )
        expected_optimizer_field = OptimizationField.from_joint_draws(
            self.joint_draws,
            forecasts=expected_optimizer_forecasts,
            source_receipt_digest=expected_source_digest,
            pool_receipt_digest=expected_pool_digest,
        )
        if (
            self.joint_draws.artifact_digest != self.dependence_artifact.artifact_digest
            or self.optimizer.field != expected_optimizer_field
            or self.optimizer.field.joint_samples_digest != self.joint_draws.joint_samples_digest
            or self.optimizer.receipt.joint_samples_digest != self.joint_draws.joint_samples_digest
        ):
            raise AssemblyError("dependence or optimizer authority differs from joint draws")
        if not isinstance(self.zero_history_competitors, tuple) or not set(
            self.zero_history_competitors
        ).issubset(set(identities)):
            raise AssemblyError("zero-history identities must be a subset of the field")
        if (
            isinstance(self.total_latency_ms, bool)
            or not isinstance(self.total_latency_ms, int)
            or self.total_latency_ms < 0
        ):
            raise AssemblyError("pipeline latency must be nonnegative")
        _digest(self.pipeline_digest, "pipeline")
        if self.pipeline_digest != canonical_digest(self.content_value()):
            raise AssemblyError("pipeline digest differs from typed receipt chain")

    @classmethod
    def create(
        cls,
        *,
        field_revision_digest: str,
        prediction_evidence: tuple[CompetitorPredictionEvidence, ...],
        joint_draws: JointDraws,
        optimizer: VerifiedOptimizerReceipt,
        disagreement: OperationalDisagreementReceipt | None,
        weight_authority: WeightAuthorityBinding,
        operational_weight_authority: OperationalWeightAuthority,
        dependence_artifact: DependenceArtifact,
        manual_authority: ManualFieldAuthority | None = None,
        construction_submission: ManualConstructionSubmission | None = None,
        expected_time_override: OperationalExpectedTimeOverrideAuthority | None = None,
        total_latency_ms: int,
    ) -> SealedPipelineOutput:
        values = locals().copy()
        values.pop("cls")
        provisional = {
            "field_revision_digest": field_revision_digest,
            "prediction_evidence": prediction_evidence,
            "joint_draws": joint_draws,
            "optimizer": optimizer,
            "disagreement": disagreement,
            "weight_authority": weight_authority,
            "operational_weight_authority": operational_weight_authority,
            "dependence_artifact": dependence_artifact,
            "manual_authority": manual_authority,
            "construction_submission": construction_submission,
            "expected_time_override": expected_time_override,
            "total_latency_ms": total_latency_ms,
        }
        return cls(
            **provisional,
            pipeline_digest=canonical_digest(_pipeline_content(provisional)),
        )

    @property
    def pools(self) -> tuple[CompetitorPoolEvidence, ...]:
        return tuple(
            CompetitorPoolEvidence(item.card, capability.pool)
            for item in self.prediction_evidence
            if (capability := _capability_basis(item.basis)) is not None
        )

    @property
    def rolling_publications(self) -> tuple[RollingPublicationBinding, ...]:
        return tuple(item.publication for item in self.prediction_evidence)

    @property
    def capability_bindings(self) -> tuple[RollingCapabilityBinding, ...]:
        return tuple(
            capability.capability_binding
            for item in self.prediction_evidence
            if (capability := _capability_basis(item.basis)) is not None
        )

    @property
    def zero_history_competitors(self) -> tuple[StableIdentifier, ...]:
        return tuple(
            item.competitor_id
            for item in self.prediction_evidence
            if _zero_history_basis(item.basis) is not None
        )

    @property
    def availability_count(self) -> int:
        return min(
            sum(forecast.state.value == "committed" for forecast in item.card.forecasts)
            for item in self.prediction_evidence
        )

    @property
    def consequence_color(self) -> ConsequenceColor:
        return ConsequenceColor.RED if self.disagreement is None else self.disagreement.color

    @property
    def council_valid_count(self) -> int:
        audit = None if self.disagreement is None else self.disagreement.decision.council_audit
        if audit is None:
            return 0
        return sum(item.status is CouncilMemberStatus.VALID for item in audit.members)

    def content_value(self) -> dict[str, Any]:
        return _pipeline_content(
            {
                name: getattr(self, name)
                for name in self.__dataclass_fields__
                if name != "pipeline_digest"
            }
        )

    def section_values(self) -> dict[ReceiptSectionKind, dict[str, Any]]:
        return {
            ReceiptSectionKind.COMPONENT_OUTPUTS: {
                "schema_version": "strathmark-v3-receipt-components-v1",
                "competitors": [
                    {
                        "competitor_id": str(item.competitor_id),
                        "packet_digest": item.packet_digest,
                        "rolling_publication": item.publication.to_dict(),
                        "components": [component.to_dict() for component in item.card.forecasts],
                    }
                    for item in self.prediction_evidence
                ],
            },
            ReceiptSectionKind.MEMBER_OUTPUTS: {
                "schema_version": "strathmark-v3-receipt-members-v1",
                "council_audit": (
                    None
                    if self.disagreement is None or self.disagreement.decision.council_audit is None
                    else self.disagreement.decision.council_audit.to_dict()
                ),
            },
            ReceiptSectionKind.VALIDATIONS: {
                "schema_version": "strathmark-v3-receipt-validations-v1",
                "field_revision_digest": self.field_revision_digest,
                "weight_authority_binding_digest": self.weight_authority.binding_digest,
                "operational_weight_authority_digest": self.operational_weight_authority.authority_digest,
                "dependence_artifact_digest": self.dependence_artifact.artifact_digest,
                "optimizer_verification_digest": self.optimizer.verification_digest,
                "manual_authority": (
                    None if self.manual_authority is None else self.manual_authority.to_dict()
                ),
                "construction_submission": (
                    None
                    if self.construction_submission is None
                    else self.construction_submission.to_dict()
                ),
                "expected_time_override": (
                    None
                    if self.expected_time_override is None
                    else self.expected_time_override.to_dict()
                ),
            },
            ReceiptSectionKind.CAPABILITY_ADJUSTMENTS: {
                "schema_version": "strathmark-v3-receipt-prediction-bases-v1",
                "entries": [
                    [
                        str(item.competitor_id),
                        (
                            {
                                "status": "applied",
                                "capability_binding_digest": (
                                    item.basis.capability_binding.binding_digest
                                ),
                                "pool_receipt_digest": (item.basis.pool.receipt.receipt_digest),
                            }
                            if isinstance(item.basis, CapabilityPoolBasis)
                            else {
                                "status": "not_applicable",
                                "basis_kind": (
                                    "zero_history_prior"
                                    if isinstance(item.basis, ZeroHistoryPriorBasis)
                                    else "manual_expected_time"
                                ),
                            }
                        ),
                    ]
                    for item in self.prediction_evidence
                ],
            },
            ReceiptSectionKind.CREDIBILITY: {
                "schema_version": "strathmark-v3-receipt-credibility-v1",
                "weight_authority": self.weight_authority.to_dict(),
                "operational_weight_authority": self.operational_weight_authority.to_dict(),
            },
            ReceiptSectionKind.POOLED_DISTRIBUTION: {
                "schema_version": "strathmark-v3-receipt-prediction-distributions-v1",
                "bases": [
                    [
                        str(item.competitor_id),
                        item.basis.to_dict(),
                    ]
                    for item in self.prediction_evidence
                ],
                "joint_regeneration": {
                    "joint_samples_digest": self.joint_draws.joint_samples_digest,
                    "common_random_map_digest": self.joint_draws.common_random_map_digest,
                    "seed": self.joint_draws.inputs.seed,
                    "draw_count": self.joint_draws.inputs.draw_count,
                    "artifact_digest": self.joint_draws.artifact_digest,
                },
            },
            ReceiptSectionKind.DISAGREEMENT: {
                "schema_version": "strathmark-v3-receipt-disagreement-v1",
                "decision": (
                    None if self.disagreement is None else self.disagreement.decision.to_dict()
                ),
                "operational_receipt": (
                    None if self.disagreement is None else self.disagreement.to_dict()
                ),
            },
            ReceiptSectionKind.OPTIMIZER_FRONTIER: {
                "schema_version": "strathmark-v3-receipt-optimizer-v1",
                "expected_times_ms": [
                    [str(item.competitor_id), item.expected_time_ms]
                    for item in self.optimizer.field.competitors
                ],
                "receipt_digest": self.optimizer.receipt.receipt_digest,
                "input_digest": self.optimizer.field.input_digest,
                "source_receipt_digest": self.optimizer.field.source_receipt_digest,
                "pool_receipt_digest": self.optimizer.field.pool_receipt_digest,
                "frontier_digest": self.optimizer.receipt.frontier_digest,
                "rounded_baseline": list(self.optimizer.receipt.rounded_baseline),
                "selected_marks": list(self.optimizer.receipt.selected_marks),
                "selected_objectives": self.optimizer.receipt.selected_objectives.to_dict(),
                "fallback_reason": (
                    None
                    if self.optimizer.receipt.fallback_reason is None
                    else self.optimizer.receipt.fallback_reason.value
                ),
                "operational_state": (
                    "degraded_review"
                    if self.optimizer.receipt.fallback_reason is OptimizerFallback.OPTIMIZER_FAILURE
                    else "verified"
                ),
                "work_budget": self.optimizer.receipt.work_budget.to_dict(),
                "implementation_artifact_digest": self.optimizer.receipt.implementation_artifact_digest,
                "verification_digest": self.optimizer.verification_digest,
            },
            ReceiptSectionKind.LATENCY_DETAIL: {
                "schema_version": "strathmark-v3-receipt-latency-v1",
                "total_ms": self.total_latency_ms,
            },
        }


@dataclass(frozen=True, slots=True)
class AssemblyResult:
    receipt: FieldReceipt
    canonical_bytes: bytes
    crn_assignments: tuple[tuple[str, str, int], ...]


@dataclass(frozen=True, slots=True)
class JudgeReceiptExplanation:
    """Bounded judge-facing text derived only from verified receipt facts."""

    receipt_id: StableIdentifier
    lines: tuple[str, ...]
    reason_tokens: tuple[str, ...]
    explanation_digest: str

    def __post_init__(self) -> None:
        require_identifier(self.receipt_id, expected_namespace="receipt")
        if not isinstance(self.lines, tuple) or not self.lines:
            raise AssemblyError("judge explanation requires bounded lines")
        if any(
            not isinstance(line, str)
            or not line
            or len(line.encode("utf-8")) > 1024
            or any(ord(character) < 32 or ord(character) > 126 for character in line)
            for line in self.lines
        ):
            raise AssemblyError("judge explanation contains unbounded text")
        if len(self.text.encode("utf-8")) > 4096:
            raise AssemblyError("judge explanation exceeds the bounded detail payload")
        if (
            not isinstance(self.reason_tokens, tuple)
            or self.reason_tokens != tuple(sorted(set(self.reason_tokens)))
            or any(
                not isinstance(token, str)
                or not token
                or len(token) > 64
                or any(
                    character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in token
                )
                for token in self.reason_tokens
            )
        ):
            raise AssemblyError("judge explanation reason tokens are not canonical")
        _digest(self.explanation_digest, "judge explanation")
        if self.explanation_digest != canonical_digest(self.content_value()):
            raise AssemblyError("judge explanation digest differs")

    @property
    def text(self) -> str:
        return " ".join(self.lines)

    def content_value(self) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-judge-receipt-explanation-v1",
            "receipt_id": str(self.receipt_id),
            "lines": list(self.lines),
            "reason_tokens": list(self.reason_tokens),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_value(),
            "text": self.text,
            "explanation_digest": self.explanation_digest,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> JudgeReceiptExplanation:
        expected = {
            "schema_version",
            "receipt_id",
            "lines",
            "reason_tokens",
            "text",
            "explanation_digest",
        }
        if (
            not isinstance(value, dict)
            or set(value) != expected
            or value.get("schema_version") != "strathmark-v3-judge-receipt-explanation-v1"
            or not isinstance(value.get("lines"), list)
            or not isinstance(value.get("reason_tokens"), list)
        ):
            raise AssemblyError("judge explanation schema differs")
        explanation = cls(
            receipt_id=require_identifier(value["receipt_id"], expected_namespace="receipt"),
            lines=tuple(value["lines"]),
            reason_tokens=tuple(value["reason_tokens"]),
            explanation_digest=value["explanation_digest"],
        )
        if value["text"] != explanation.text:
            raise AssemblyError("judge explanation text differs from its lines")
        return explanation


def render_verified_receipt_explanation(
    receipt: FieldReceipt,
) -> JudgeReceiptExplanation:
    """Render deterministic detail without model-authored narrative or provider work."""

    if not isinstance(receipt, FieldReceipt):
        raise AssemblyError("judge explanation requires a verified field receipt")
    sections = {
        section.kind: section.payload.to_value()
        for section in receipt.sections
        if isinstance(section.payload, InlinePayload)
    }
    validations = sections.get(ReceiptSectionKind.VALIDATIONS)
    components = sections.get(ReceiptSectionKind.COMPONENT_OUTPUTS)
    pools = sections.get(ReceiptSectionKind.POOLED_DISTRIBUTION)
    disagreement = sections.get(ReceiptSectionKind.DISAGREEMENT)
    optimizer = sections.get(ReceiptSectionKind.OPTIMIZER_FRONTIER)
    if not all(
        isinstance(value, dict)
        for value in (validations, components, pools, disagreement, optimizer)
    ):
        raise AssemblyError("judge explanation receipt facts are incomplete")

    roster_index = {
        competitor_id: index for index, competitor_id in enumerate(receipt.ordered_competitor_ids)
    }
    start_order = tuple(
        sorted(
            receipt.marks,
            key=lambda item: (-item.mark, roster_index[item.competitor_id]),
        )
    )
    lines = [
        f"Field {receipt.field_id} receipt revision {receipt.receipt_revision} "
        f"uses upstream revision {receipt.upstream_field_revision}.",
        "Start order: "
        + "; ".join(f"{item.competitor_id} Mark {item.mark}" for item in start_order)
        + ".",
        "Marks are rebased so the lowest mark is Mark 3.",
    ]
    tokens = {"rebase_mark_3", "optimizer_replay_verified"}
    manual = validations.get("manual_authority")
    component_rows = components.get("competitors")
    if not isinstance(component_rows, list) or not component_rows:
        raise AssemblyError("judge explanation component facts are malformed")
    try:
        availability = min(
            sum(component.get("state") == "committed" for component in item["components"])
            for item in component_rows
        )
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise AssemblyError("judge explanation availability facts are malformed") from exc
    tokens.add(f"availability_{availability}_of_3")
    if isinstance(manual, dict):
        mode = manual.get("mode")
        if mode not in {item.value for item in ManualConstructionMode}:
            raise AssemblyError("judge explanation manual mode is unknown")
        tokens.add(f"manual_{mode}")
        lines.append(f"Deliberate manual field mode: {mode}.")
    else:
        lines.append(f"Automatic ensemble availability: {availability}/3.")

    operational = disagreement.get("operational_receipt")
    decision = disagreement.get("decision")
    color = (
        operational.get("color")
        if isinstance(operational, dict)
        else decision.get("color")
        if isinstance(decision, dict)
        else "red"
    )
    if color not in {item.value for item in ConsequenceColor}:
        raise AssemblyError("judge explanation consequence color is unknown")
    tokens.add(f"consequence_{color}")
    construction = validations.get("construction_submission")
    override = validations.get("expected_time_override")
    if isinstance(construction, dict):
        tokens.add(str(construction.get("reason_code")))
    if isinstance(override, dict):
        tokens.add(str(override.get("reason_code")))
    lines.append(f"Disagreement consequence: {color}.")
    verification_digest = optimizer.get("verification_digest")
    _digest(verification_digest, "judge explanation optimizer verification")
    lines.append(f"Optimizer replay verification: {verification_digest}.")
    if receipt.warning_codes:
        tokens.update(f"warning_{item}" for item in receipt.warning_codes)
        lines.append("Warnings: " + ", ".join(receipt.warning_codes) + ".")
    else:
        lines.append("Warnings: none.")
    dependence_digest = validations.get("dependence_artifact_digest")
    capacity_digest = validations.get("capacity_authority_digest")
    _digest(dependence_digest, "judge explanation dependence")
    _digest(capacity_digest, "judge explanation capacity")
    lines.append(
        f"Provenance: epoch {receipt.tournament_epoch_id}; bundle "
        f"{receipt.bundles[0].digest}; dependence {dependence_digest}; "
        f"capacity {capacity_digest}."
    )
    content = {
        "schema_version": "strathmark-v3-judge-receipt-explanation-v1",
        "receipt_id": str(receipt.receipt_id),
        "lines": lines,
        "reason_tokens": sorted(tokens),
    }
    return JudgeReceiptExplanation(
        receipt_id=receipt.receipt_id,
        lines=tuple(lines),
        reason_tokens=tuple(sorted(tokens)),
        explanation_digest=canonical_digest(content),
    )


PipelineBuilder = Callable[[FrozenFieldRevision], object]


class FieldAssemblyService:
    """Recovers exact output first, otherwise validates and commits one whole field."""

    def __init__(
        self,
        store: Any,
        *,
        pipeline_builder: PipelineBuilder | None = None,
        manual_action_store: Any | None = None,
    ) -> None:
        required = (
            "lookup_exact",
            "verify_capacity_authority",
            "verify_current_field",
            "verify_weight_authority",
            "verify_card_authority",
            "verify_dependence_authority",
            "verify_disagreement_authority",
            "commit_receipt",
        )
        if any(not callable(getattr(store, name, None)) for name in required):
            raise AssemblyError("field assembly requires the verified SQLite projection port")
        if pipeline_builder is not None and not callable(pipeline_builder):
            raise AssemblyError("configured pipeline builder must be callable")
        if manual_action_store is not None and not callable(
            getattr(manual_action_store, "publish", None)
        ):
            raise AssemblyError("manual-action store must expose publication")
        self._store = store
        self._pipeline_builder = pipeline_builder
        self._manual_action_store = manual_action_store

    def assemble(
        self,
        **values: Any,
    ) -> AssemblyResult:
        return self._assemble(supersession_kind="upstream", **values)

    def submit_construction(self, **values: Any) -> AssemblyResult:
        return self._assemble(supersession_kind="construction", **values)

    def submit_expected_time_override(self, **values: Any) -> AssemblyResult:
        return self._assemble(supersession_kind="expected_time_override", **values)

    def _assemble(
        self,
        *,
        field: FrozenFieldRevision,
        caller_namespace: str,
        request_identity: str,
        actor_id: str,
        occurred_at: str,
        engine_authority: EngineAuthorityBinding | None = None,
        build_pipeline: PipelineBuilder | None = None,
        manual_action_binding: Any | None = None,
        supersession_kind: str,
    ) -> AssemblyResult:
        if not isinstance(field, FrozenFieldRevision):
            raise AssemblyError("field assembly inputs must be typed")
        request = require_idempotency_key(request_identity)
        require_identifier(actor_id, expected_namespace="actor")
        require_utc_milliseconds(occurred_at)
        if engine_authority is not None and not isinstance(
            engine_authority, EngineAuthorityBinding
        ):
            raise AssemblyError("field assembly engine authority must be typed")
        manual_requirement = None
        if manual_action_binding is not None or (
            supersession_kind == "construction" and self._manual_action_store is not None
        ):
            from strathmark.v3.application.manual_actions import ManualActionBinding

            if supersession_kind != "construction":
                raise AssemblyConflict(
                    "manual-action binding is restricted to deliberate construction"
                )
            if self._manual_action_store is None or not isinstance(
                manual_action_binding, ManualActionBinding
            ):
                raise AssemblyConflict(
                    "construction requires an exact current manual-action binding"
                )
            manual_requirement = self._manual_action_store.require_current(manual_action_binding)
        if (
            supersession_kind == "upstream"
            and build_pipeline is None
            and self._manual_action_store is not None
        ):
            sealed_requirement = self._manual_action_store.current(field.field_id)
            if sealed_requirement is not None:
                if sealed_requirement.upstream_field_revision > field.field_revision:
                    raise AssemblyConflict(
                        "manual-action requirement is newer than the supplied field"
                    )
                if sealed_requirement.upstream_field_revision == field.field_revision:
                    if (
                        sealed_requirement.field_revision_digest != field.revision_digest
                        or sealed_requirement.target_context_digest != field.target_context.digest
                        or sealed_requirement.historical_cutoff_key != field.historical_cutoff_key
                        or sealed_requirement.tournament_epoch_id != field.tournament_epoch_id
                        or sealed_requirement.bundle_digest != field.bundle_digest
                        or sealed_requirement.hard_deadline_at != field.deadline_at
                    ):
                        raise AssemblyConflict(
                            "sealed manual-action requirement differs from field"
                        )
                    return sealed_requirement
        retry = self._store.lookup_exact(
            caller_namespace=caller_namespace,
            request_identity=str(request),
            field_revision_digest=field.revision_digest,
        )
        if retry is not None:
            if retry.receipt.engine_authority != engine_authority:
                raise AssemblyConflict(
                    "field receipt engine authority differs from the current request"
                )
            return retry
        configured_builder = build_pipeline is None
        selected_builder = build_pipeline if build_pipeline is not None else self._pipeline_builder
        if not callable(selected_builder):
            raise AssemblyError("pipeline builder is not configured")
        self._store.verify_capacity_authority(
            field.capacity_authority_digest,
            bundle_digest=field.bundle_digest,
            entrant_count=len(field.ordered_assignments),
            declared_max_field_entrants=field.max_field_entrants,
        )
        self._store.verify_current_field(field)
        # The predecessor is authority for every receipt supersession.  Resolve
        # and authenticate it before provider or optimizer work so a corrupted
        # disposable row cannot influence expensive work or become the signed
        # predecessor of a new event.
        prior = self._store.current_receipt(str(field.field_id))
        built_pipeline = selected_builder(field)
        rolling_build = None
        if configured_builder:
            from strathmark.v3.application.manual_actions import (
                ManualActionRequirement,
            )
            from strathmark.v3.application.pipeline_builder import (
                RollingPipelineBuild,
                unwrap_rolling_pipeline_build,
            )

            if isinstance(built_pipeline, ManualActionRequirement):
                if supersession_kind != "upstream":
                    raise AssemblyConflict(
                        "judge submission cannot return a new manual-action requirement"
                    )
                if self._manual_action_store is None:
                    raise AssemblyConflict("manual-action requirement store is not configured")
                return self._manual_action_store.publish(built_pipeline)
            if not isinstance(built_pipeline, RollingPipelineBuild):
                raise AssemblyConflict("configured builder did not return rolling authority")
            rolling_build = built_pipeline
            pipeline = unwrap_rolling_pipeline_build(built_pipeline)
        else:
            pipeline = built_pipeline
        if not isinstance(pipeline, SealedPipelineOutput):
            raise AssemblyConflict("untrusted diagnostic pipeline cannot enter approval")
        if pipeline.field_revision_digest != field.revision_digest:
            raise AssemblyConflict("pipeline was computed for another field revision")
        if manual_requirement is not None:
            from strathmark.v3.application.manual_actions import ManualActionKind

            expected_action = (
                ManualActionKind.ACCEPT_SINGLE_SURVIVOR
                if pipeline.manual_authority is not None
                and pipeline.manual_authority.mode is ManualConstructionMode.EXACT_SINGLE_SURVIVOR
                else ManualActionKind.COMPLETE_EXPECTED_TIME
            )
            if (
                manual_requirement.action is not expected_action
                or manual_requirement.field_revision_digest != field.revision_digest
            ):
                raise AssemblyConflict("manual-action kind differs from judge construction")
        expected_ids = tuple(item.competitor_id for item in field.ordered_assignments)
        if tuple(item.competitor_id for item in pipeline.prediction_evidence) != expected_ids:
            raise AssemblyConflict("pipeline roster differs from frozen upstream assignment")
        expected_authority = {
            "upstream": (
                pipeline.construction_submission is None and pipeline.expected_time_override is None
            ),
            "construction": pipeline.construction_submission is not None
            and pipeline.expected_time_override is None,
            "expected_time_override": pipeline.expected_time_override is not None
            and pipeline.construction_submission is None,
        }.get(supersession_kind, False)
        if not expected_authority:
            raise AssemblyConflict("receipt supersession kind lacks its exact typed authority")
        for assignment, evidence in zip(
            field.ordered_assignments,
            pipeline.prediction_evidence,
            strict=True,
        ):
            card = evidence.card
            publication = evidence.publication
            packet = card.evidence_packet
            basis = evidence.basis
            if (
                publication.competitor_id != assignment.competitor_id
                or publication.target_context_digest != field.target_context.digest
                or publication.historical_cutoff_key != field.historical_cutoff_key
                or publication.tournament_epoch_id != field.tournament_epoch_id
                or publication.bundle_digest != field.bundle_digest
                or publication.evidence_digest != card.packet_digest
                or publication.card_manifest_digest != card.manifest.body_digest
                or packet.competitor_id != evidence.competitor_id
                or packet.target_context != field.target_context
                or str(packet.tournament_epoch_id) != str(field.tournament_epoch_id)
                or packet.tournament_event_sequence != field.tournament_event_sequence
                or packet.historical_cutoff_key != str(field.historical_cutoff_key)
                or card.bundle_digest != field.bundle_digest
            ):
                raise AssemblyConflict(
                    "competitor card differs from frozen field evidence authority"
                )
            if isinstance(basis, CapabilityPoolBasis) and (
                basis.capability_binding.competitor_id != assignment.competitor_id
                or basis.capability_binding.context_digest != field.target_context.digest
                or basis.capability_binding.state_digest
                != basis.pool.receipt.capability_state_digest
            ):
                raise AssemblyConflict("capability prediction basis differs from frozen field")
            if isinstance(basis, ZeroHistoryPriorBasis) and (
                basis.estimate.competitor_id != assignment.competitor_id
                or basis.estimate.target_context_digest != field.target_context.digest
            ):
                raise AssemblyConflict("zero-history prediction basis differs from frozen field")
            self._store.verify_card_authority(card)
        for assignment, draw in zip(
            field.ordered_assignments, pipeline.joint_draws.competitors, strict=True
        ):
            if (
                assignment.competitor_id != draw.competitor_id
                or assignment.crn_index != draw.crn_index
                or str(assignment.stand_id) != draw.draw_slot
            ):
                raise AssemblyConflict("CRN mapping differs from frozen upstream assignment")
        if pipeline.weight_authority.tournament_event_sequence != field.tournament_event_sequence:
            raise AssemblyConflict("weight authority differs from the frozen tournament boundary")
        if (
            pipeline.operational_weight_authority.tournament_id != field.tournament_id
            or pipeline.operational_weight_authority.round_id != field.round_id
            or pipeline.operational_weight_authority.epoch_id != field.tournament_epoch_id
            or pipeline.operational_weight_authority.epoch_digest != field.evidence_digest
        ):
            raise AssemblyConflict("operational weight authority differs from the frozen field")
        if (
            pipeline.manual_authority is not None
            and str(pipeline.manual_authority.actor_id) != actor_id
        ):
            raise AssemblyConflict(
                "manual field authority actor differs from authenticated assembly actor"
            )
        expected_weight_context = ContextNode(
            field.target_context.event_code,
            f"{(field.target_context.size_mm // 50) * 50}_{(field.target_context.size_mm // 50) * 50 + 49}",
            field.target_context.material_code,
        )
        if (
            pipeline.weight_authority.context.history_depth is not None
            or not pipeline.weight_authority.context.contains(expected_weight_context)
            or pipeline.dependence_artifact.target_context.history_depth is not None
            or not pipeline.dependence_artifact.target_context.contains(expected_weight_context)
        ):
            raise AssemblyConflict("weight or dependence context is not a legal field hierarchy")
        # The persistence boundary verifies these installed authorities once
        # before publishing blobs and again inside the atomic writer transaction.
        # Repeating the same store reads here adds no concurrency protection;
        # only the in-transaction recheck can close the stale-authority window.
        same_upstream = prior is not None and prior.upstream_field_revision == field.field_revision
        if same_upstream and supersession_kind == "upstream":
            concurrent_retry = self._store.lookup_exact(
                caller_namespace=caller_namespace,
                request_identity=str(request),
                field_revision_digest=field.revision_digest,
            )
            if concurrent_retry is not None:
                return concurrent_retry
            raise AssemblyConflict("upstream field revision is not monotonic for a new receipt")
        if prior is not None and field.field_revision < prior.upstream_field_revision:
            raise AssemblyConflict("upstream field revision is not monotonic")
        if supersession_kind != "upstream":
            if supersession_kind == "construction":
                submission = pipeline.construction_submission
                if (
                    submission is None
                    or submission.upstream_field_revision != field.field_revision
                    or str(submission.actor_id) != actor_id
                ):
                    raise AssemblyConflict(
                        "construction submission differs from current receipt authority"
                    )
                if prior is None:
                    if (
                        submission.prior_receipt_id is not None
                        or submission.prior_receipt_digest is not None
                    ):
                        raise AssemblyConflict("initial construction cannot claim a predecessor")
                elif (
                    not same_upstream
                    or submission.prior_receipt_id != prior.receipt_id
                    or submission.prior_receipt_digest != prior.content_digest
                ):
                    raise AssemblyConflict(
                        "construction submission differs from current receipt authority"
                    )
            else:
                override = pipeline.expected_time_override
                if (
                    prior is None
                    or not same_upstream
                    or override is None
                    or override.prior_receipt_id != prior.receipt_id
                    or override.prior_receipt_digest != prior.content_digest
                    or override.upstream_field_revision != field.field_revision
                    or override.override_receipt.actor != actor_id
                    or override.override_receipt.target_context_digest
                    != field.target_context.digest
                    or override.override_receipt.evidence_epoch_id != field.tournament_epoch_id
                    or override.override_receipt.evidence_digest != field.evidence_digest
                    or override.override_receipt.assessor_outputs_digest
                    != canonical_digest(
                        pipeline.section_values()[ReceiptSectionKind.COMPONENT_OUTPUTS]
                    )
                    or override.override_receipt.consensus_digest
                    != canonical_digest(
                        pipeline.section_values()[ReceiptSectionKind.POOLED_DISTRIBUTION]
                    )
                    or override.override_receipt.before_sheet != _field_sheet_from_receipt(prior)
                    or override.override_receipt.after_sheet
                    != _field_sheet_from_pipeline(field, pipeline)
                ):
                    raise AssemblyConflict(
                        "expected-time override differs from current whole-field authority"
                    )
        receipt_revision = 1 if prior is None else prior.receipt_revision + 1
        sections = _sections_for_receipt(field, pipeline, expected_ids)
        receipt = FieldReceipt.create(
            caller_namespace=caller_namespace,
            request_identity=request,
            field_id=field.field_id,
            upstream_field_revision=field.field_revision,
            receipt_revision=receipt_revision,
            supersedes_receipt_id=None if prior is None else prior.receipt_id,
            ordered_competitor_ids=expected_ids,
            target_context=field.target_context,
            target_context_digest=field.target_context.digest,
            historical_cutoff_key=str(field.historical_cutoff_key),
            tournament_epoch_id=field.tournament_epoch_id,
            tournament_event_sequence=field.tournament_event_sequence,
            packet_identities=tuple(
                PacketIdentity(
                    item.competitor_id,
                    pool.packet_digest,
                )
                for item, pool in zip(
                    field.ordered_assignments,
                    pipeline.prediction_evidence,
                    strict=True,
                )
            ),
            sections=tuple(
                ReceiptSection(kind, InlinePayload.from_value(sections[kind]))
                for kind in ReceiptSectionKind
            ),
            marks=tuple(
                MarkAssignment(competitor_id, mark)
                for competitor_id, mark in zip(
                    pipeline.optimizer.receipt.competitor_ids,
                    pipeline.optimizer.receipt.selected_marks,
                    strict=True,
                )
            ),
            warning_codes=_warnings(pipeline),
            total_latency_ms=pipeline.total_latency_ms,
            bundles=(BundleIdentity("runtime", "bundle:v3", field.bundle_digest),),
            engine_authority=engine_authority,
        )
        crn = tuple(
            (str(item.competitor_id), str(item.stand_id), item.crn_index)
            for item in field.ordered_assignments
        )
        return self._store.commit_receipt(
            field=field,
            receipt=receipt,
            pipeline=pipeline,
            pipeline_digest=pipeline.pipeline_digest,
            weight_authority=pipeline.operational_weight_authority,
            disagreement_authority=pipeline.disagreement,
            cards=tuple(item.card for item in pipeline.prediction_evidence),
            crn_assignments=crn,
            actor_id=actor_id,
            occurred_at=occurred_at,
            rolling_build=rolling_build,
            manual_action_store=self._manual_action_store,
            manual_action_binding=manual_action_binding,
        )


def _warnings(pipeline: SealedPipelineOutput) -> tuple[str, ...]:
    warnings = []
    if (
        pipeline.manual_authority is not None
        and pipeline.manual_authority.mode is ManualConstructionMode.COMPLETE_EXPECTED_TIME
    ):
        warnings.append("manual_construction_required")
    elif pipeline.availability_count == 2:
        warnings.append("degraded_two_assessors")
    elif pipeline.availability_count == 1:
        warnings.append("manual_single_survivor")
    elif pipeline.availability_count == 0:
        warnings.append("manual_construction_required")
    if pipeline.council_valid_count == 2:
        warnings.append("degraded_llm_council")
    if pipeline.optimizer.receipt.fallback_reason is OptimizerFallback.OPTIMIZER_FAILURE:
        warnings.append("degraded_optimizer_failure")
    if pipeline.zero_history_competitors:
        warnings.append("zero_history")
    if pipeline.consequence_color is ConsequenceColor.RED:
        warnings.append("red_consequence")
    return tuple(sorted(warnings))


def _sections_for_receipt(
    field: FrozenFieldRevision,
    pipeline: SealedPipelineOutput,
    expected_ids: tuple[StableIdentifier, ...],
) -> dict[ReceiptSectionKind, dict[str, Any]]:
    sections = pipeline.section_values()
    flag_reasons: dict[str, set[str]] = {
        str(item): {"zero_history"} for item in pipeline.zero_history_competitors
    }
    for evidence in pipeline.prediction_evidence:
        available_count = sum(
            forecast.state.value == "committed" for forecast in evidence.card.forecasts
        )
        if available_count < 3:
            flag_reasons.setdefault(str(evidence.competitor_id), set()).add(
                f"assessor_availability_{available_count}_of_3"
            )
    field_wide_reasons: set[str] = set()
    if pipeline.manual_authority is not None:
        field_wide_reasons.add("manual_construction")
    if pipeline.consequence_color is not ConsequenceColor.GREEN:
        field_wide_reasons.add(f"consequence_{pipeline.consequence_color.value}")
    if pipeline.council_valid_count == 2:
        field_wide_reasons.add("council_degraded_two_of_three")
    for competitor_id in expected_ids:
        if field_wide_reasons:
            flag_reasons.setdefault(str(competitor_id), set()).update(field_wide_reasons)
    sections[ReceiptSectionKind.VALIDATIONS] = {
        **sections[ReceiptSectionKind.VALIDATIONS],
        "tournament_id": str(field.tournament_id),
        "round_id": str(field.round_id),
        "field_id": str(field.field_id),
        "field_revision": field.field_revision,
        "capacity_authority_digest": field.capacity_authority_digest,
        "max_field_entrants": field.max_field_entrants,
        "call_order": field.call_order,
        "scheduled_at": field.scheduled_at,
        "deadline_at": field.deadline_at,
        "flagged_competitor_ids": sorted(flag_reasons),
        "flag_reason_tokens": [
            [competitor_id, sorted(reasons)]
            for competitor_id, reasons in sorted(flag_reasons.items())
        ],
    }
    return sections


def verify_receipt_matches_pipeline(
    *,
    field: FrozenFieldRevision,
    pipeline: SealedPipelineOutput,
    receipt: FieldReceipt,
    crn_assignments: tuple[tuple[str, str, int], ...],
) -> None:
    """Fail closed unless receipt bytes are a pure projection of sealed authority."""

    if (
        not isinstance(field, FrozenFieldRevision)
        or not isinstance(pipeline, SealedPipelineOutput)
        or not isinstance(receipt, FieldReceipt)
    ):
        raise AssemblyConflict("field commit lacks typed sealed pipeline authority")
    expected_ids = tuple(item.competitor_id for item in field.ordered_assignments)
    expected_crn = tuple(
        (str(item.competitor_id), str(item.stand_id), item.crn_index)
        for item in field.ordered_assignments
    )
    expected_sections = _sections_for_receipt(field, pipeline, expected_ids)
    section_values = tuple(
        ReceiptSection(kind, InlinePayload.from_value(expected_sections[kind]))
        for kind in ReceiptSectionKind
    )
    expected_packets = tuple(
        PacketIdentity(item.competitor_id, evidence.packet_digest)
        for item, evidence in zip(
            field.ordered_assignments, pipeline.prediction_evidence, strict=True
        )
    )
    expected_marks = tuple(
        MarkAssignment(competitor_id, mark)
        for competitor_id, mark in zip(
            pipeline.optimizer.receipt.competitor_ids,
            pipeline.optimizer.receipt.selected_marks,
            strict=True,
        )
    )
    if (
        pipeline.field_revision_digest != field.revision_digest
        or receipt.field_id != field.field_id
        or receipt.upstream_field_revision != field.field_revision
        or receipt.ordered_competitor_ids != expected_ids
        or receipt.target_context != field.target_context
        or receipt.historical_cutoff_key != str(field.historical_cutoff_key)
        or receipt.tournament_epoch_id != field.tournament_epoch_id
        or receipt.tournament_event_sequence != field.tournament_event_sequence
        or receipt.packet_identities != expected_packets
        or receipt.sections != section_values
        or receipt.marks != expected_marks
        or receipt.warning_codes != _warnings(pipeline)
        or receipt.total_latency_ms != pipeline.total_latency_ms
        or receipt.bundles != (BundleIdentity("runtime", "bundle:v3", field.bundle_digest),)
        or crn_assignments != expected_crn
    ):
        raise AssemblyConflict("field receipt differs from sealed pipeline authority")


def _field_sheet_from_pipeline(
    field: FrozenFieldRevision, pipeline: SealedPipelineOutput
) -> FieldSheetSnapshot:
    return FieldSheetSnapshot.create(
        field_id=field.field_id,
        expected_times_ms=tuple(
            (item.competitor_id, item.expected_time_ms)
            for item in pipeline.optimizer.field.competitors
        ),
        marks=tuple(
            zip(
                pipeline.optimizer.receipt.competitor_ids,
                pipeline.optimizer.receipt.selected_marks,
                strict=True,
            )
        ),
        pool_receipt_digest=pipeline.optimizer.field.pool_receipt_digest,
        optimizer_receipt_digest=pipeline.optimizer.receipt.receipt_digest,
        optimizer_verification_status=OptimizerVerificationStatus.PENDING,
    )


def _field_sheet_from_receipt(receipt: FieldReceipt) -> FieldSheetSnapshot:
    sections = {
        item.kind: item.payload.to_value()
        for item in receipt.sections
        if isinstance(item.payload, InlinePayload)
    }
    optimizer = sections.get(ReceiptSectionKind.OPTIMIZER_FRONTIER)
    validation = sections.get(ReceiptSectionKind.VALIDATIONS)
    if not all(isinstance(item, dict) for item in (optimizer, validation)):
        raise AssemblyConflict("prior receipt lacks whole-field optimizer authority")
    rows = optimizer.get("expected_times_ms")
    if not isinstance(rows, list):
        raise AssemblyConflict("prior receipt expected-time authority is malformed")
    try:
        expected_times = tuple(
            (
                require_identifier(item["competitor_id"], expected_namespace="competitor"),
                item["expected_time_ms"],
            )
            if isinstance(item, dict)
            else (
                require_identifier(item[0], expected_namespace="competitor"),
                item[1],
            )
            for item in rows
        )
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise AssemblyConflict("prior receipt expected-time authority is malformed") from exc
    if tuple(item for item, _value in expected_times) != receipt.ordered_competitor_ids or any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for _item, value in expected_times
    ):
        raise AssemblyConflict("prior receipt expected-time authority differs")
    return FieldSheetSnapshot.create(
        field_id=receipt.field_id,
        expected_times_ms=expected_times,
        marks=tuple((item.competitor_id, item.mark) for item in receipt.marks),
        pool_receipt_digest=optimizer["pool_receipt_digest"],
        optimizer_receipt_digest=optimizer["receipt_digest"],
        optimizer_verification_status=OptimizerVerificationStatus.PENDING,
    )


def verify_judge_supersession_authority(
    *,
    field: FrozenFieldRevision,
    pipeline: SealedPipelineOutput,
    prior_receipt: FieldReceipt,
    actor_id: str,
) -> str:
    """Replay the exact construction/override authority against its predecessor."""

    require_identifier(actor_id, expected_namespace="actor")
    if pipeline.construction_submission is not None:
        submission = pipeline.construction_submission
        if (
            pipeline.expected_time_override is not None
            or submission.prior_receipt_id != prior_receipt.receipt_id
            or submission.prior_receipt_digest != prior_receipt.content_digest
            or submission.upstream_field_revision != field.field_revision
            or submission.field_revision_digest != field.revision_digest
            or str(submission.actor_id) != actor_id
            or pipeline.manual_authority is None
            or submission.manual_authority_digest != pipeline.manual_authority.authority_digest
        ):
            raise AssemblyConflict("construction submission differs from current receipt authority")
        return "construction"
    if pipeline.expected_time_override is not None:
        override = pipeline.expected_time_override
        component_outputs = pipeline.section_values()[ReceiptSectionKind.COMPONENT_OUTPUTS]
        pooled_distribution = pipeline.section_values()[ReceiptSectionKind.POOLED_DISTRIBUTION]
        if (
            override.prior_receipt_id != prior_receipt.receipt_id
            or override.prior_receipt_digest != prior_receipt.content_digest
            or override.upstream_field_revision != field.field_revision
            or override.field_revision_digest != field.revision_digest
            or override.override_receipt.actor != actor_id
            or override.override_receipt.target_context_digest != field.target_context.digest
            or override.override_receipt.evidence_epoch_id != field.tournament_epoch_id
            or override.override_receipt.evidence_digest != field.evidence_digest
            or override.override_receipt.assessor_outputs_digest
            != canonical_digest(component_outputs)
            or override.override_receipt.consensus_digest != canonical_digest(pooled_distribution)
            or override.override_receipt.before_sheet != _field_sheet_from_receipt(prior_receipt)
            or override.override_receipt.after_sheet != _field_sheet_from_pipeline(field, pipeline)
            or override.after_optimizer_verification_digest
            != pipeline.optimizer.verification_digest
        ):
            raise AssemblyConflict(
                "expected-time override differs from current whole-field authority"
            )
        return "expected_time_override"
    return "upstream"


def _field_content(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "strathmark-v3-frozen-field-revision-v1",
        "tournament_id": str(values["tournament_id"]),
        "round_id": str(values["round_id"]),
        "field_id": str(values["field_id"]),
        "field_revision": values["field_revision"],
        "assignments": [item.to_dict() for item in values["ordered_assignments"]],
        "target_context": values["target_context"].to_dict(),
        "historical_cutoff_key": str(values["historical_cutoff_key"]),
        "tournament_epoch_id": str(values["tournament_epoch_id"]),
        "tournament_event_sequence": values["tournament_event_sequence"],
        "bundle_digest": values["bundle_digest"],
        "evidence_digest": values["evidence_digest"],
        "capacity_authority_digest": values["capacity_authority_digest"],
        "max_field_entrants": values["max_field_entrants"],
        "call_order": values["call_order"],
        "scheduled_at": values["scheduled_at"],
        "deadline_at": values["deadline_at"],
    }


def _operational_weight_content(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "strathmark-v3-operational-weight-authority-v1",
        "kind": values["kind"].value,
        "binding": values["binding"].to_dict(),
        "tournament_id": str(values["tournament_id"]),
        "round_id": str(values["round_id"]),
        "epoch_id": str(values["epoch_id"]),
        "epoch_digest": values["epoch_digest"],
        "frozen_tournament_sequence": values["frozen_tournament_sequence"],
        "authority_event_sequence": values["authority_event_sequence"],
        "authority_event_digest": values["authority_event_digest"],
        "completed_round_id": (
            None if values["completed_round_id"] is None else str(values["completed_round_id"])
        ),
        "round_close_event_digest": values["round_close_event_digest"],
        "baseline_receipt_digest": values["baseline_receipt_digest"],
        "control_event_sequence": values["control_event_sequence"],
        "control_event_digest": values["control_event_digest"],
    }


def _operational_disagreement_content(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "strathmark-v3-operational-disagreement-receipt-v1",
        "field_revision_digest": values["field_revision_digest"],
        "decision_digest": values["decision"].decision_digest,
        "color": values["decision"].color.value,
        "policy_digest": values["decision"].policy_digest,
        "pooled_optimizer_verification_digest": values["pooled_optimizer"].verification_digest,
        "component_optimizer_verification_digests": [
            [source.value, receipt.verification_digest]
            for source, receipt in values["component_optimizers"]
        ],
        "component_joint_draw_digests": [
            [source.value, draws.joint_samples_digest]
            for source, draws in values["component_joint_draws"]
        ],
        "policy_manifest_digest": values["policy_manifest"].body_digest,
        "council_manifest_digest": (
            None if values["council_manifest"] is None else values["council_manifest"].body_digest
        ),
    }


def _component_common_random_plan_authority(
    component_draws: tuple[tuple[AssessorKind, JointDraws], ...],
) -> dict[str, Any]:
    """Store the one deterministic CRN plan shared by all counterfactuals."""

    if not component_draws:
        raise AssemblyError("component common uniforms require joint draws")
    first = component_draws[0][1]
    roster = tuple((row.draw_slot, row.crn_index, row.common_uniforms) for row in first.competitors)
    for _source, draws in component_draws[1:]:
        if (
            draws.inputs.seed != first.inputs.seed
            or draws.inputs.draw_count != first.inputs.draw_count
            or draws.common_random_map_digest != first.common_random_map_digest
            or tuple(
                (row.draw_slot, row.crn_index, row.common_uniforms) for row in draws.competitors
            )
            != roster
        ):
            raise AssemblyError(
                "component counterfactuals do not share one common-random authority"
            )
    return {
        "schema_version": "strathmark-v3-component-common-random-plan-v1",
        "seed": first.inputs.seed,
        "draw_count": first.inputs.draw_count,
        "rho": first.rho,
        "common_random_map_digest": first.common_random_map_digest,
        "algorithm": first.algorithm,
        "dependency_version": first.dependency_version,
        "rows": [[draw_slot, crn_index] for draw_slot, crn_index, _common_uniforms in roster],
    }


def _compact_component_joint_draw(draws: JointDraws) -> dict[str, Any]:
    """Reference samples already retained by the paired optimizer authority."""

    return {
        "schema_version": "strathmark-v3-compact-component-joint-draws-v2",
        "inputs": draws.inputs.to_dict(),
        "artifact_digest": draws.artifact_digest,
        "rho": draws.rho,
        "effective_rho": draws.effective_rho,
        "competitors": [
            {
                "competitor_id": str(row.competitor_id),
                "draw_slot": row.draw_slot,
                "crn_index": row.crn_index,
                "distribution_digest": row.distribution_digest,
                "samples_digest": row.samples_digest,
            }
            for row in draws.competitors
        ],
        "common_random_map_digest": draws.common_random_map_digest,
        "algorithm": draws.algorithm,
        "dependency_version": draws.dependency_version,
        "time_quantum_ms": draws.time_quantum_ms,
        "joint_samples_digest": draws.joint_samples_digest,
    }


def _inflate_component_joint_draw_authorities(
    shared_value: object,
    draw_rows: list[Any],
    *,
    optimizer_receipts: tuple[tuple[AssessorKind, VerifiedOptimizerReceipt], ...] | None = None,
) -> list[Any]:
    """Restore canonical JointDraws bodies and leave full replay to its decoder."""

    expected_shared = {
        "schema_version",
        "seed",
        "draw_count",
        "rho",
        "common_random_map_digest",
        "algorithm",
        "dependency_version",
        "rows",
    }
    if (
        not isinstance(shared_value, dict)
        or set(shared_value) != expected_shared
        or shared_value.get("schema_version") != "strathmark-v3-component-common-random-plan-v1"
        or isinstance(shared_value.get("seed"), bool)
        or not isinstance(shared_value.get("seed"), int)
        or isinstance(shared_value.get("draw_count"), bool)
        or not isinstance(shared_value.get("draw_count"), int)
        or not 1 <= shared_value["draw_count"] <= 1_000_000
        or not isinstance(shared_value.get("rows"), list)
        or not 1 <= len(shared_value["rows"]) <= 12
    ):
        raise AssemblyConflict("operational disagreement common-random plan differs")
    try:
        _digest(
            shared_value["common_random_map_digest"],
            "component common-random map",
        )
    except (TypeError, ValueError, AssemblyError) as exc:
        raise AssemblyConflict("operational disagreement common-random plan differs") from exc
    plan_rows = shared_value["rows"]
    if any(
        not isinstance(row, list)
        or len(row) != 2
        or not isinstance(row[0], str)
        or not row[0]
        or isinstance(row[1], bool)
        or not isinstance(row[1], int)
        or row[1] < 0
        for row in plan_rows
    ):
        raise AssemblyConflict("operational disagreement common-random plan rows differ")
    slots = tuple((row[0], row[1]) for row in plan_rows)
    if len(set(slots)) != len(slots):
        raise AssemblyConflict("operational disagreement common-random plan rows differ")
    try:
        uniforms = regenerate_joint_uniforms_for_replay(
            slots,
            seed=shared_value["seed"],
            draw_count=shared_value["draw_count"],
            rho=shared_value["rho"],
        )
    except (ContractError, TypeError, ValueError) as exc:
        raise AssemblyConflict("operational disagreement common-random plan differs") from exc
    expected_compact = {
        "schema_version",
        "inputs",
        "artifact_digest",
        "rho",
        "effective_rho",
        "competitors",
        "common_random_map_digest",
        "algorithm",
        "dependency_version",
        "time_quantum_ms",
        "joint_samples_digest",
    }
    expected_competitor = {
        "competitor_id",
        "draw_slot",
        "crn_index",
        "distribution_digest",
        "samples_digest",
    }
    samples_by_source: dict[str, dict[str, list[Any]]] | None = None
    if optimizer_receipts is None:
        expected_competitor.add("samples_ms")
    else:
        samples_by_source = {}
        for source, optimizer in optimizer_receipts:
            source_samples: dict[str, list[Any]] = {}
            for competitor in optimizer.field.competitors:
                competitor_id = str(competitor.competitor_id)
                if competitor_id in source_samples:
                    raise AssemblyConflict(
                        "operational disagreement optimizer sample authority differs"
                    )
                source_samples[competitor_id] = list(competitor.samples_ms)
            samples_by_source[source.value] = source_samples
    inflated = []
    for source, compact in draw_rows:
        compact_schema = (
            "strathmark-v3-compact-component-joint-draws-v1"
            if optimizer_receipts is None
            else "strathmark-v3-compact-component-joint-draws-v2"
        )
        if (
            not isinstance(compact, dict)
            or set(compact) != expected_compact
            or compact.get("schema_version") != compact_schema
            or not isinstance(compact.get("inputs"), dict)
            or compact["inputs"].get("seed") != shared_value["seed"]
            or compact["inputs"].get("draw_count") != shared_value["draw_count"]
            or compact.get("common_random_map_digest") != shared_value["common_random_map_digest"]
            or compact.get("rho") != shared_value["rho"]
            or compact.get("algorithm") != shared_value["algorithm"]
            or compact.get("dependency_version") != shared_value["dependency_version"]
            or not isinstance(compact.get("competitors"), list)
            or len(compact["competitors"]) != len(slots)
        ):
            raise AssemblyConflict("operational disagreement compact joint authority differs")
        competitors = compact["competitors"]
        if any(
            not isinstance(row, dict)
            or set(row) != expected_competitor
            or (row.get("draw_slot"), row.get("crn_index")) not in slots
            for row in competitors
        ):
            raise AssemblyConflict("operational disagreement compact joint rows differ")
        source_samples = None if samples_by_source is None else samples_by_source.get(source)
        if source_samples is not None and (
            len(source_samples) != len(competitors)
            or any(row["competitor_id"] not in source_samples for row in competitors)
        ):
            raise AssemblyConflict("operational disagreement optimizer sample authority differs")
        inflated.append(
            [
                source,
                {
                    **compact,
                    "schema_version": "strathmark-v3-joint-draws-v1",
                    "competitors": [
                        {
                            **row,
                            "common_uniforms": list(uniforms[row["draw_slot"]]),
                            **(
                                {}
                                if source_samples is None
                                else {"samples_ms": source_samples[row["competitor_id"]]}
                            ),
                        }
                        for row in competitors
                    ],
                },
            ]
        )
    return inflated


def _manual_field_content(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "strathmark-v3-manual-field-authority-v1",
        "mode": values["mode"].value,
        "field_revision_digest": values["field_revision_digest"],
        "estimates": [item.to_dict() for item in values["estimates"]],
        "actor_id": str(values["actor_id"]),
        "reason_code": values["reason_code"],
        "scope": values["scope"].value,
        "created_at": values["created_at"],
    }


def _override_basis_content(values: Mapping[str, Any]) -> dict[str, Any]:
    source = values["source_basis"]
    if not isinstance(source, (CapabilityPoolBasis, ZeroHistoryPriorBasis)):
        raise AssemblyError("override starting estimate source is invalid")
    return {
        "basis_kind": "accepted_override_starting_estimate",
        "override_state": values["state"].to_dict(),
        "distribution": values["distribution"].to_dict(),
        "source_basis": source.to_dict(),
        "current_capability_revision": values["current_capability_revision"],
        "later_evidence_applied": values["later_evidence_applied"],
    }


def _capability_basis(basis: PredictionBasis) -> CapabilityPoolBasis | None:
    if isinstance(basis, CapabilityPoolBasis):
        return basis
    if isinstance(basis, OverrideStartingEstimateBasis) and isinstance(
        basis.source_basis, CapabilityPoolBasis
    ):
        return basis.source_basis
    return None


def _zero_history_basis(basis: PredictionBasis) -> ZeroHistoryPriorBasis | None:
    if isinstance(basis, ZeroHistoryPriorBasis):
        return basis
    if isinstance(basis, OverrideStartingEstimateBasis) and isinstance(
        basis.source_basis, ZeroHistoryPriorBasis
    ):
        return basis.source_basis
    return None


def _prediction_basis_digest(basis: PredictionBasis) -> str:
    if isinstance(basis, CapabilityPoolBasis):
        return basis.pool.receipt.receipt_digest
    if isinstance(basis, ZeroHistoryPriorBasis):
        return basis.authority_digest
    return basis.basis_digest


def _manual_construction_submission_content(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "strathmark-v3-manual-construction-submission-v1",
        "prior_receipt_id": (
            None if values["prior_receipt_id"] is None else str(values["prior_receipt_id"])
        ),
        "prior_receipt_digest": values["prior_receipt_digest"],
        "upstream_field_revision": values["upstream_field_revision"],
        "field_revision_digest": values["field_revision_digest"],
        "manual_authority_digest": values["manual_authority_digest"],
        "actor_id": str(values["actor_id"]),
        "reason_code": values["reason_code"],
        "scope": values["scope"].value,
        "submitted_at": values["submitted_at"],
    }


def _operational_override_authority_content(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "strathmark-v3-operational-expected-time-override-v1",
        "prior_receipt_id": str(values["prior_receipt_id"]),
        "prior_receipt_digest": values["prior_receipt_digest"],
        "upstream_field_revision": values["upstream_field_revision"],
        "field_revision_digest": values["field_revision_digest"],
        "reason_code": values["reason_code"],
        "override_receipt": values["override_receipt"].to_dict(),
        "after_optimizer_verification_digest": values["after_optimizer_verification_digest"],
    }


def live_effective_weight_receipt_digest(
    freeze_event_digest: str,
    context: Any,
    weights: tuple[tuple[Any, str], ...],
) -> str:
    """Canonical U13 receipt identity derived from one closed U12 live freeze."""

    _digest(freeze_event_digest, "live freeze event")
    if not hasattr(context, "to_dict"):
        raise AssemblyError("live effective weight context must be typed")
    return canonical_digest(
        {
            "schema_version": "strathmark-v3-live-effective-weight-receipt-v1",
            "freeze_event_digest": freeze_event_digest,
            "context": context.to_dict(),
            "weights": [[item.value, value] for item, value in weights],
        }
    )


def _pipeline_content(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "strathmark-v3-sealed-field-pipeline-v1",
        "field_revision_digest": values["field_revision_digest"],
        "prediction_evidence": [item.to_dict() for item in values["prediction_evidence"]],
        "joint_samples_digest": values["joint_draws"].joint_samples_digest,
        "optimizer_verification_digest": values["optimizer"].verification_digest,
        "disagreement_digest": (
            None if values["disagreement"] is None else values["disagreement"].receipt_digest
        ),
        "weight_authority_digest": values["weight_authority"].binding_digest,
        "operational_weight_authority_digest": values[
            "operational_weight_authority"
        ].authority_digest,
        "dependence_artifact_digest": values["dependence_artifact"].artifact_digest,
        "manual_authority_digest": (
            None
            if values["manual_authority"] is None
            else values["manual_authority"].authority_digest
        ),
        "construction_submission_digest": (
            None
            if values["construction_submission"] is None
            else values["construction_submission"].submission_digest
        ),
        "expected_time_override_authority_digest": (
            None
            if values["expected_time_override"] is None
            else values["expected_time_override"].authority_digest
        ),
        "total_latency_ms": values["total_latency_ms"],
    }


def _rolling_publication_binding_content(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "strathmark-v3-field-rolling-publication-binding-v1",
        "card_key": {
            "schema_version": "strathmark-v3-capability-card-key-v1",
            "competitor_id": str(values["competitor_id"]),
            "target_context_digest": values["target_context_digest"],
            "historical_cutoff_key": str(values["historical_cutoff_key"]),
            "tournament_epoch_id": str(values["tournament_epoch_id"]),
            "bundle_digest": values["bundle_digest"],
            "evidence_digest": values["evidence_digest"],
            "dependency_revision": values["dependency_revision"],
            "card_digest": values["card_digest"],
            "idempotency_key": values["card_idempotency_key"],
        },
        "card_manifest_digest": values["card_manifest_digest"],
        "publication_digest": values["publication_digest"],
        "publication_manifest_digest": values["publication_manifest_digest"],
        "component_refs_digest": values["component_refs_digest"],
        "availability": [list(item) for item in values["availability"]],
        "council_manifest_digest": values["council_manifest_digest"],
        "council_aggregate_manifest_digest": values["council_aggregate_manifest_digest"],
        "hard_deadline_at": values["hard_deadline_at"],
        "sealed_at": values["sealed_at"],
    }


def _rolling_capability_binding_content(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "strathmark-v3-field-rolling-capability-binding-v1",
        "competitor_id": str(values["competitor_id"]),
        "context_digest": values["context_digest"],
        "state_revision": values["state_revision"],
        "state_digest": values["state_digest"],
        "aggregate_id": str(values["aggregate_id"]),
        "aggregate_version": values["aggregate_version"],
        "aggregate_event_digest": values["aggregate_event_digest"],
    }


def _digest(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise AssemblyError(f"{label} must be a lower-case SHA-256 digest")
    return value


__all__ = [
    "AssemblyConflict",
    "AssemblyError",
    "AssemblyResult",
    "CapabilityPoolBasis",
    "CompetitorCardAuthority",
    "CompetitorPoolEvidence",
    "CompetitorPredictionEvidence",
    "FieldAssemblyService",
    "FrozenEntrantAssignment",
    "FrozenFieldRevision",
    "JudgeReceiptExplanation",
    "OperationalWeightAuthority",
    "OperationalWeightKind",
    "OperationalDisagreementReceipt",
    "OverrideStartingEstimateBasis",
    "RollingCapabilityBinding",
    "RollingPublicationBinding",
    "ManualCompetitorEstimate",
    "ManualConstructionMode",
    "ManualExpectedTimeBasis",
    "ManualFieldAuthority",
    "SealedPipelineOutput",
    "ZeroHistoryPriorBasis",
    "counterfactual_sheet_from_optimizer",
    "live_effective_weight_receipt_digest",
    "render_verified_receipt_explanation",
    "seal_competitor_card_authority",
]
