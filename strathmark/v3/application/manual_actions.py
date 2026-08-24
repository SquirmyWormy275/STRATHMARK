"""Typed, signed manual-action requirements for sealed degraded fields.

These records are not handicap sheets and contain no invented expected times.  They make
the hard-deadline 1/3 and 0/3 paths durable and actionable until a judge deliberately
accepts the exact survivor or supplies a complete expected-time construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.contracts.evidence import require_utc_milliseconds
from strathmark.v3.contracts.forecasts import AssessorKind
from strathmark.v3.contracts.identifiers import (
    StableIdentifier,
    deterministic_identifier,
    require_identifier,
)
from strathmark.v3.infrastructure.integrity import (
    IntegrityError,
    IntegrityTrustStore,
    P256Signer,
    SignedManifest,
    sign_manifest,
    verify_manifest,
)

_OUTER = (AssessorKind.FORMULA, AssessorKind.ML, AssessorKind.LLM_COUNCIL)
_REQUIREMENT_SCHEMA = "strathmark-v3-manual-action-requirement-v1"
_AUTHORITY_SCHEMA = "strathmark-v3-manual-action-authority-v1"
_BINDING_SCHEMA = "strathmark-v3-manual-action-binding-v1"
_RESOLUTION_SCHEMA = "strathmark-v3-manual-action-resolution-v1"
_REQUIREMENT_MANIFEST_KIND = "manual_action_requirement"
_RESOLUTION_MANIFEST_KIND = "manual_action_resolution"


class ManualActionError(RuntimeError):
    """Base error for a manual-action requirement."""


class ManualActionConflict(ManualActionError):
    """The supplied material differs from the exact current requirement."""


class ManualActionKind(str, Enum):
    ACCEPT_SINGLE_SURVIVOR = "accept_single_survivor"
    COMPLETE_EXPECTED_TIME = "complete_expected_time"


def _digest(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ManualActionConflict(f"{label} must be a lower-case SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True)
class ManualActionEntrant:
    competitor_id: StableIdentifier
    available_assessors: tuple[AssessorKind, ...]
    publication_binding_digest: str
    candidate_basis_digest: str | None

    def __post_init__(self) -> None:
        require_identifier(self.competitor_id, expected_namespace="competitor")
        if (
            not isinstance(self.available_assessors, tuple)
            or any(item not in _OUTER for item in self.available_assessors)
            or self.available_assessors
            != tuple(item for item in _OUTER if item in self.available_assessors)
            or len(set(self.available_assessors)) != len(self.available_assessors)
        ):
            raise ManualActionConflict(
                "manual-action availability must be unique outer assessors in canonical order"
            )
        _digest(self.publication_binding_digest, "manual-action publication binding")
        if self.candidate_basis_digest is not None:
            _digest(self.candidate_basis_digest, "manual-action candidate basis")

    def to_dict(self) -> dict[str, Any]:
        return {
            "competitor_id": str(self.competitor_id),
            "available_assessors": [item.value for item in self.available_assessors],
            "publication_binding_digest": self.publication_binding_digest,
            "candidate_basis_digest": self.candidate_basis_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ManualActionEntrant:
        expected = {
            "competitor_id",
            "available_assessors",
            "publication_binding_digest",
            "candidate_basis_digest",
        }
        if not isinstance(value, Mapping) or set(value) != expected or not isinstance(
            value.get("available_assessors"), list
        ):
            raise ManualActionConflict("manual-action entrant fields differ")
        try:
            assessors = tuple(AssessorKind(item) for item in value["available_assessors"])
        except (TypeError, ValueError) as exc:
            raise ManualActionConflict("manual-action entrant assessor is unknown") from exc
        return cls(
            require_identifier(value["competitor_id"], expected_namespace="competitor"),
            assessors,
            value["publication_binding_digest"],
            value["candidate_basis_digest"],
        )


@dataclass(frozen=True, slots=True)
class ManualActionBinding:
    requirement_id: StableIdentifier
    requirement_digest: str
    requirement_manifest_digest: str
    field_id: StableIdentifier
    upstream_field_revision: int
    field_revision_digest: str
    action: ManualActionKind
    binding_digest: str

    def __post_init__(self) -> None:
        require_identifier(self.requirement_id, expected_namespace="manual_action")
        require_identifier(self.field_id, expected_namespace="field")
        if (
            isinstance(self.upstream_field_revision, bool)
            or not isinstance(self.upstream_field_revision, int)
            or self.upstream_field_revision <= 0
        ):
            raise ManualActionConflict("manual-action field revision must be positive")
        if not isinstance(self.action, ManualActionKind):
            raise ManualActionConflict("manual-action binding kind is invalid")
        for value, label in (
            (self.requirement_digest, "manual-action requirement"),
            (self.requirement_manifest_digest, "manual-action manifest"),
            (self.field_revision_digest, "manual-action field revision"),
            (self.binding_digest, "manual-action binding"),
        ):
            _digest(value, label)
        if self.binding_digest != canonical_digest(self.content_value()):
            raise ManualActionConflict("manual-action binding digest differs")

    @classmethod
    def create(cls, requirement: ManualActionRequirement) -> ManualActionBinding:
        values = {
            "requirement_id": requirement.requirement_id,
            "requirement_digest": requirement.requirement_digest,
            "requirement_manifest_digest": requirement.manifest.body_digest,
            "field_id": requirement.field_id,
            "upstream_field_revision": requirement.upstream_field_revision,
            "field_revision_digest": requirement.field_revision_digest,
            "action": requirement.action,
        }
        return cls(**values, binding_digest=canonical_digest(_binding_content(values)))

    def content_value(self) -> dict[str, Any]:
        return _binding_content(
            {
                name: getattr(self, name)
                for name in self.__dataclass_fields__
                if name != "binding_digest"
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_value(), "binding_digest": self.binding_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ManualActionBinding:
        expected = {
            "schema_version",
            "requirement_id",
            "requirement_digest",
            "requirement_manifest_digest",
            "field_id",
            "upstream_field_revision",
            "field_revision_digest",
            "action",
            "binding_digest",
        }
        if (
            not isinstance(value, Mapping)
            or set(value) != expected
            or value.get("schema_version") != _BINDING_SCHEMA
        ):
            raise ManualActionConflict("manual-action binding fields differ")
        try:
            action = ManualActionKind(value["action"])
        except (TypeError, ValueError) as exc:
            raise ManualActionConflict("manual-action binding kind is unknown") from exc
        return cls(
            require_identifier(value["requirement_id"], expected_namespace="manual_action"),
            value["requirement_digest"],
            value["requirement_manifest_digest"],
            require_identifier(value["field_id"], expected_namespace="field"),
            value["upstream_field_revision"],
            value["field_revision_digest"],
            action,
            value["binding_digest"],
        )


@dataclass(frozen=True, slots=True)
class ManualActionRequirement:
    requirement_id: StableIdentifier
    field_id: StableIdentifier
    upstream_field_revision: int
    field_revision_digest: str
    target_context_digest: str
    historical_cutoff_key: StableIdentifier
    tournament_epoch_id: StableIdentifier
    bundle_digest: str
    hard_deadline_at: str
    entrants: tuple[ManualActionEntrant, ...]
    action: ManualActionKind
    created_at: str
    requirement_digest: str
    manifest: SignedManifest

    def __post_init__(self) -> None:
        require_identifier(self.requirement_id, expected_namespace="manual_action")
        require_identifier(self.field_id, expected_namespace="field")
        require_identifier(self.historical_cutoff_key, expected_namespace="history")
        require_identifier(self.tournament_epoch_id, expected_namespace="epoch")
        if (
            isinstance(self.upstream_field_revision, bool)
            or not isinstance(self.upstream_field_revision, int)
            or self.upstream_field_revision <= 0
        ):
            raise ManualActionConflict("manual-action field revision must be positive")
        for value, label in (
            (self.field_revision_digest, "manual-action field revision"),
            (self.target_context_digest, "manual-action target context"),
            (self.bundle_digest, "manual-action bundle"),
            (self.requirement_digest, "manual-action requirement"),
        ):
            _digest(value, label)
        hard_deadline = require_utc_milliseconds(self.hard_deadline_at)
        created = require_utc_milliseconds(self.created_at)
        if created < hard_deadline:
            raise ManualActionConflict(
                "manual action cannot seal before the hard deadline"
            )
        if (
            not isinstance(self.entrants, tuple)
            or not self.entrants
            or not all(isinstance(item, ManualActionEntrant) for item in self.entrants)
            or len({item.competitor_id for item in self.entrants}) != len(self.entrants)
        ):
            raise ManualActionConflict(
                "manual-action entrants must be a nonempty unique ordered roster"
            )
        derived = _derive_action(self.entrants)
        if self.action is not derived:
            raise ManualActionConflict("manual-action kind differs from availability")
        if self.requirement_digest != canonical_digest(self.content_value()):
            raise ManualActionConflict("manual-action requirement digest differs")
        expected_id = deterministic_identifier(
            "manual_action", {"requirement_digest": self.requirement_digest}
        )
        if self.requirement_id != expected_id:
            raise ManualActionConflict("manual-action requirement identity differs")
        if not isinstance(self.manifest, SignedManifest):
            raise ManualActionConflict("manual-action requirement manifest is untyped")
        expected_authority = _authority_payload(self)
        if (
            self.manifest.kind != _REQUIREMENT_MANIFEST_KIND
            or self.manifest.body().get("created_at") != self.created_at
            or self.manifest.body().get("payload") != expected_authority
        ):
            raise ManualActionConflict("manual-action requirement manifest differs")

    @property
    def binding(self) -> ManualActionBinding:
        return ManualActionBinding.create(self)

    def creation_arguments(self) -> dict[str, Any]:
        return {
            "field_id": self.field_id,
            "upstream_field_revision": self.upstream_field_revision,
            "field_revision_digest": self.field_revision_digest,
            "target_context_digest": self.target_context_digest,
            "historical_cutoff_key": self.historical_cutoff_key,
            "tournament_epoch_id": self.tournament_epoch_id,
            "bundle_digest": self.bundle_digest,
            "hard_deadline_at": self.hard_deadline_at,
            "entrants": self.entrants,
        }

    def content_value(self) -> dict[str, Any]:
        return {
            "schema_version": _REQUIREMENT_SCHEMA,
            "field_id": str(self.field_id),
            "upstream_field_revision": self.upstream_field_revision,
            "field_revision_digest": self.field_revision_digest,
            "target_context_digest": self.target_context_digest,
            "historical_cutoff_key": str(self.historical_cutoff_key),
            "tournament_epoch_id": str(self.tournament_epoch_id),
            "bundle_digest": self.bundle_digest,
            "hard_deadline_at": self.hard_deadline_at,
            "entrants": [item.to_dict() for item in self.entrants],
            "action": self.action.value,
            "created_at": self.created_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_value(),
            "requirement_id": str(self.requirement_id),
            "requirement_digest": self.requirement_digest,
            "manifest": self.manifest.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ManualActionRequirement:
        expected = {
            "schema_version",
            "requirement_id",
            "field_id",
            "upstream_field_revision",
            "field_revision_digest",
            "target_context_digest",
            "historical_cutoff_key",
            "tournament_epoch_id",
            "bundle_digest",
            "hard_deadline_at",
            "entrants",
            "action",
            "created_at",
            "requirement_digest",
            "manifest",
        }
        if (
            not isinstance(value, Mapping)
            or set(value) != expected
            or value.get("schema_version") != _REQUIREMENT_SCHEMA
            or not isinstance(value.get("entrants"), list)
            or not isinstance(value.get("manifest"), Mapping)
        ):
            raise ManualActionConflict("manual-action requirement fields differ")
        try:
            action = ManualActionKind(value["action"])
        except (TypeError, ValueError) as exc:
            raise ManualActionConflict("manual-action kind is unknown") from exc
        return cls(
            require_identifier(value["requirement_id"], expected_namespace="manual_action"),
            require_identifier(value["field_id"], expected_namespace="field"),
            value["upstream_field_revision"],
            value["field_revision_digest"],
            value["target_context_digest"],
            require_identifier(value["historical_cutoff_key"], expected_namespace="history"),
            require_identifier(value["tournament_epoch_id"], expected_namespace="epoch"),
            value["bundle_digest"],
            value["hard_deadline_at"],
            tuple(ManualActionEntrant.from_dict(item) for item in value["entrants"]),
            action,
            value["created_at"],
            value["requirement_digest"],
            SignedManifest.from_dict(value["manifest"]),
        )

    def verify(self, trust_store: IntegrityTrustStore) -> dict[str, Any]:
        try:
            payload = verify_manifest(self.manifest, trust_store)
        except IntegrityError as exc:
            raise ManualActionConflict(
                "manual-action manifest signer is not trusted or material is invalid"
            ) from exc
        if payload != _authority_payload(self):
            raise ManualActionConflict("manual-action verified manifest differs")
        return self.content_value()


def create_manual_action_requirement(
    *,
    field_id: str | StableIdentifier,
    upstream_field_revision: int,
    field_revision_digest: str,
    target_context_digest: str,
    historical_cutoff_key: str | StableIdentifier,
    tournament_epoch_id: str | StableIdentifier,
    bundle_digest: str,
    hard_deadline_at: str,
    entrants: tuple[ManualActionEntrant, ...],
    signer: P256Signer,
    created_at: str,
) -> ManualActionRequirement:
    action = _derive_action(entrants)
    content = {
        "schema_version": _REQUIREMENT_SCHEMA,
        "field_id": str(require_identifier(field_id, expected_namespace="field")),
        "upstream_field_revision": upstream_field_revision,
        "field_revision_digest": field_revision_digest,
        "target_context_digest": target_context_digest,
        "historical_cutoff_key": str(
            require_identifier(historical_cutoff_key, expected_namespace="history")
        ),
        "tournament_epoch_id": str(
            require_identifier(tournament_epoch_id, expected_namespace="epoch")
        ),
        "bundle_digest": bundle_digest,
        "hard_deadline_at": hard_deadline_at,
        "entrants": [item.to_dict() for item in entrants],
        "action": action.value,
        "created_at": created_at,
    }
    requirement_digest = canonical_digest(content)
    requirement_id = deterministic_identifier(
        "manual_action", {"requirement_digest": requirement_digest}
    )
    authority = {
        "schema_version": _AUTHORITY_SCHEMA,
        "requirement_id": str(requirement_id),
        "requirement_digest": requirement_digest,
        "requirement": content,
    }
    manifest = sign_manifest(
        _REQUIREMENT_MANIFEST_KIND,
        authority,
        signer=signer,
        created_at=created_at,
    )
    return ManualActionRequirement(
        requirement_id,
        require_identifier(field_id, expected_namespace="field"),
        upstream_field_revision,
        field_revision_digest,
        target_context_digest,
        require_identifier(historical_cutoff_key, expected_namespace="history"),
        require_identifier(tournament_epoch_id, expected_namespace="epoch"),
        bundle_digest,
        hard_deadline_at,
        entrants,
        action,
        created_at,
        requirement_digest,
        manifest,
    )


@dataclass(frozen=True, slots=True)
class ManualActionResolution:
    requirement_id: StableIdentifier
    requirement_digest: str
    field_id: StableIdentifier
    receipt_id: StableIdentifier
    receipt_digest: str
    actor_id: StableIdentifier
    resolved_at: str
    resolution_digest: str
    manifest: SignedManifest

    def __post_init__(self) -> None:
        require_identifier(self.requirement_id, expected_namespace="manual_action")
        require_identifier(self.field_id, expected_namespace="field")
        require_identifier(self.receipt_id, expected_namespace="receipt")
        require_identifier(self.actor_id, expected_namespace="actor")
        require_utc_milliseconds(self.resolved_at)
        for value, label in (
            (self.requirement_digest, "manual-action resolution requirement"),
            (self.receipt_digest, "manual-action resolution receipt"),
            (self.resolution_digest, "manual-action resolution"),
        ):
            _digest(value, label)
        if self.resolution_digest != canonical_digest(self.content_value()):
            raise ManualActionConflict("manual-action resolution digest differs")
        if (
            not isinstance(self.manifest, SignedManifest)
            or self.manifest.kind != _RESOLUTION_MANIFEST_KIND
            or self.manifest.body().get("created_at") != self.resolved_at
            or self.manifest.body().get("payload") != _resolution_authority(self)
        ):
            raise ManualActionConflict("manual-action resolution manifest differs")

    def content_value(self) -> dict[str, Any]:
        return {
            "schema_version": _RESOLUTION_SCHEMA,
            "requirement_id": str(self.requirement_id),
            "requirement_digest": self.requirement_digest,
            "field_id": str(self.field_id),
            "receipt_id": str(self.receipt_id),
            "receipt_digest": self.receipt_digest,
            "actor_id": str(self.actor_id),
            "resolved_at": self.resolved_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_value(),
            "resolution_digest": self.resolution_digest,
            "manifest": self.manifest.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ManualActionResolution:
        expected = {
            "schema_version",
            "requirement_id",
            "requirement_digest",
            "field_id",
            "receipt_id",
            "receipt_digest",
            "actor_id",
            "resolved_at",
            "resolution_digest",
            "manifest",
        }
        if (
            not isinstance(value, Mapping)
            or set(value) != expected
            or value.get("schema_version") != _RESOLUTION_SCHEMA
            or not isinstance(value.get("manifest"), Mapping)
        ):
            raise ManualActionConflict("manual-action resolution fields differ")
        return cls(
            require_identifier(value["requirement_id"], expected_namespace="manual_action"),
            value["requirement_digest"],
            require_identifier(value["field_id"], expected_namespace="field"),
            require_identifier(value["receipt_id"], expected_namespace="receipt"),
            value["receipt_digest"],
            require_identifier(value["actor_id"], expected_namespace="actor"),
            value["resolved_at"],
            value["resolution_digest"],
            SignedManifest.from_dict(value["manifest"]),
        )

    def verify(self, trust_store: IntegrityTrustStore) -> dict[str, Any]:
        try:
            payload = verify_manifest(self.manifest, trust_store)
        except IntegrityError as exc:
            raise ManualActionConflict(
                "manual-action resolution signer is not trusted or material is invalid"
            ) from exc
        if payload != _resolution_authority(self):
            raise ManualActionConflict("manual-action verified resolution differs")
        return self.content_value()


def create_manual_action_resolution(
    requirement: ManualActionRequirement,
    *,
    receipt_id: str | StableIdentifier,
    receipt_digest: str,
    actor_id: str | StableIdentifier,
    resolved_at: str,
    signer: P256Signer,
) -> ManualActionResolution:
    if not isinstance(requirement, ManualActionRequirement):
        raise ManualActionConflict("manual-action resolution requires typed authority")
    content = {
        "schema_version": _RESOLUTION_SCHEMA,
        "requirement_id": str(requirement.requirement_id),
        "requirement_digest": requirement.requirement_digest,
        "field_id": str(requirement.field_id),
        "receipt_id": str(require_identifier(receipt_id, expected_namespace="receipt")),
        "receipt_digest": receipt_digest,
        "actor_id": str(require_identifier(actor_id, expected_namespace="actor")),
        "resolved_at": resolved_at,
    }
    digest = canonical_digest(content)
    authority = {
        "schema_version": "strathmark-v3-manual-action-resolution-authority-v1",
        "resolution_digest": digest,
        "resolution": content,
    }
    manifest = sign_manifest(
        _RESOLUTION_MANIFEST_KIND,
        authority,
        signer=signer,
        created_at=resolved_at,
    )
    return ManualActionResolution(
        requirement.requirement_id,
        requirement.requirement_digest,
        requirement.field_id,
        require_identifier(receipt_id, expected_namespace="receipt"),
        receipt_digest,
        require_identifier(actor_id, expected_namespace="actor"),
        resolved_at,
        digest,
        manifest,
    )


def _derive_action(entrants: tuple[ManualActionEntrant, ...]) -> ManualActionKind:
    if (
        not isinstance(entrants, tuple)
        or not entrants
        or not all(isinstance(item, ManualActionEntrant) for item in entrants)
    ):
        raise ManualActionConflict("manual action requires typed entrant availability")
    source_sets = tuple(item.available_assessors for item in entrants)
    exact_single = all(len(items) == 1 for items in source_sets) and len(
        {items[0] for items in source_sets}
    ) == 1
    ordinary = all(len(items) >= 2 for items in source_sets) and len(set(source_sets)) == 1
    if ordinary:
        raise ManualActionConflict(
            "ordinary uniform two-or-three assessor fields do not require manual action"
        )
    if exact_single:
        if any(item.candidate_basis_digest is None for item in entrants):
            raise ManualActionConflict(
                "single-survivor manual action requires every exact candidate basis"
            )
        return ManualActionKind.ACCEPT_SINGLE_SURVIVOR
    if any(item.candidate_basis_digest is not None for item in entrants):
        raise ManualActionConflict(
            "complete expected-time action cannot carry a hidden candidate basis"
        )
    return ManualActionKind.COMPLETE_EXPECTED_TIME


def _authority_payload(requirement: ManualActionRequirement) -> dict[str, Any]:
    return {
        "schema_version": _AUTHORITY_SCHEMA,
        "requirement_id": str(requirement.requirement_id),
        "requirement_digest": requirement.requirement_digest,
        "requirement": requirement.content_value(),
    }


def _resolution_authority(resolution: ManualActionResolution) -> dict[str, Any]:
    return {
        "schema_version": "strathmark-v3-manual-action-resolution-authority-v1",
        "resolution_digest": resolution.resolution_digest,
        "resolution": resolution.content_value(),
    }


def _binding_content(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": _BINDING_SCHEMA,
        "requirement_id": str(values["requirement_id"]),
        "requirement_digest": values["requirement_digest"],
        "requirement_manifest_digest": values["requirement_manifest_digest"],
        "field_id": str(values["field_id"]),
        "upstream_field_revision": values["upstream_field_revision"],
        "field_revision_digest": values["field_revision_digest"],
        "action": values["action"].value,
    }


__all__ = [
    "ManualActionBinding",
    "ManualActionConflict",
    "ManualActionEntrant",
    "ManualActionError",
    "ManualActionKind",
    "ManualActionRequirement",
    "ManualActionResolution",
    "create_manual_action_requirement",
    "create_manual_action_resolution",
]
