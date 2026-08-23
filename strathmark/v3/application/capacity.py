"""Pure, provider-independent capacity admission for V3 durable work."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any, Mapping

from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest

CAPACITY_SCHEMA_VERSION = "strathmark-v3-job-capacity-v1"


class CapacityError(ValueError):
    """A capacity manifest or admission request is unsafe."""


class JobLane(str, Enum):
    """Physically separate work budgets; no lane borrows another's lease slots."""

    HOT_FIELD = "hot_field"
    INFERENCE = "inference"
    LOOKUP_RECOVERY = "lookup_recovery"
    MAINTENANCE = "maintenance"


class JobResourceClass(str, Enum):
    """Execution resources whose concurrency must be independently fenced."""

    LOCAL_CPU = "local_cpu"
    LOCAL_GPU = "local_gpu"
    CLOUD = "cloud"
    STORAGE_IO = "storage_io"


class JobKind(str, Enum):
    """Closed work catalog binding every kind to its only legal lane and resource."""

    HOT_FIELD_ASSEMBLY = "hot_field_assembly"
    FORMULA_CARD = "formula_card"
    ML_CARD = "ml_card"
    LOCAL_LLM_CARD = "local_llm_card"
    CLOUD_LLM_CARD = "cloud_llm_card"
    RECEIPT_LOOKUP = "receipt_lookup"
    PROJECTION_REBUILD = "projection_rebuild"
    MODEL_FACTORY = "model_factory"
    MAINTENANCE = "maintenance"

    @property
    def lane(self) -> JobLane:
        return {
            JobKind.HOT_FIELD_ASSEMBLY: JobLane.HOT_FIELD,
            JobKind.FORMULA_CARD: JobLane.INFERENCE,
            JobKind.ML_CARD: JobLane.INFERENCE,
            JobKind.LOCAL_LLM_CARD: JobLane.INFERENCE,
            JobKind.CLOUD_LLM_CARD: JobLane.INFERENCE,
            JobKind.RECEIPT_LOOKUP: JobLane.LOOKUP_RECOVERY,
            JobKind.PROJECTION_REBUILD: JobLane.LOOKUP_RECOVERY,
            JobKind.MODEL_FACTORY: JobLane.MAINTENANCE,
            JobKind.MAINTENANCE: JobLane.MAINTENANCE,
        }[self]

    @property
    def resource_class(self) -> JobResourceClass:
        return {
            JobKind.HOT_FIELD_ASSEMBLY: JobResourceClass.LOCAL_CPU,
            JobKind.FORMULA_CARD: JobResourceClass.LOCAL_CPU,
            JobKind.ML_CARD: JobResourceClass.LOCAL_CPU,
            JobKind.LOCAL_LLM_CARD: JobResourceClass.LOCAL_GPU,
            JobKind.CLOUD_LLM_CARD: JobResourceClass.CLOUD,
            JobKind.RECEIPT_LOOKUP: JobResourceClass.STORAGE_IO,
            JobKind.PROJECTION_REBUILD: JobResourceClass.STORAGE_IO,
            JobKind.MODEL_FACTORY: JobResourceClass.LOCAL_GPU,
            JobKind.MAINTENANCE: JobResourceClass.LOCAL_CPU,
        }[self]


class JobPriority(IntEnum):
    """Declared race-day order; aging is applied only inside a lane."""

    MAINTENANCE = 100
    SCHEDULED_ENTRANT = 200
    PLAUSIBLE_QUALIFIER = 300
    IMMINENT_FIELD = 400
    RECOVERY = 500


@dataclass(frozen=True, slots=True)
class LaneCapacity:
    lane: JobLane
    max_queued: int
    max_leased: int

    def __post_init__(self) -> None:
        if not isinstance(self.lane, JobLane):
            raise CapacityError("lane must be a JobLane")
        _positive(self.max_queued, "max_queued")
        _positive(self.max_leased, "max_leased")
        if self.max_leased > self.max_queued:
            raise CapacityError("max_leased cannot exceed max_queued")

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane.value,
            "max_queued": self.max_queued,
            "max_leased": self.max_leased,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> LaneCapacity:
        _fields(value, {"lane", "max_queued", "max_leased"})
        try:
            lane = JobLane(value["lane"])
        except (TypeError, ValueError) as exc:
            raise CapacityError("unknown job lane") from exc
        return cls(lane, value["max_queued"], value["max_leased"])


@dataclass(frozen=True, slots=True)
class CapacityManifest:
    """Frozen limits whose digest is suitable for a bundle manifest."""

    schema_version: str
    max_open_tournaments: int
    max_round_entrants: int
    max_field_entrants: int
    max_plausible_qualifiers: int
    max_context_cards: int
    max_queued_jobs: int
    max_receipt_bytes: int
    max_blob_bytes: int
    max_api_page_size: int
    reserved_imminent_jobs: int
    reserved_recovery_jobs: int
    aging_interval_ms: int
    aging_increment: int
    lanes: tuple[LaneCapacity, ...]

    def __post_init__(self) -> None:
        if self.schema_version != CAPACITY_SCHEMA_VERSION:
            raise CapacityError("unsupported capacity manifest schema")
        for label in (
            "max_open_tournaments",
            "max_round_entrants",
            "max_field_entrants",
            "max_plausible_qualifiers",
            "max_context_cards",
            "max_queued_jobs",
            "max_receipt_bytes",
            "max_blob_bytes",
            "max_api_page_size",
            "aging_interval_ms",
            "aging_increment",
        ):
            _positive(getattr(self, label), label)
        _positive(self.reserved_imminent_jobs, "reserved_imminent_jobs")
        _positive(self.reserved_recovery_jobs, "reserved_recovery_jobs")
        if self.reserved_imminent_jobs + self.reserved_recovery_jobs >= self.max_queued_jobs:
            raise CapacityError("reserved jobs must leave ordinary queue capacity")
        if self.max_field_entrants > self.max_round_entrants:
            raise CapacityError("field entrants cannot exceed round entrants")
        if self.max_plausible_qualifiers > self.max_round_entrants:
            raise CapacityError("plausible qualifiers cannot exceed round entrants")
        if self.max_context_cards < self.max_plausible_qualifiers:
            raise CapacityError("context-card capacity cannot be below plausible qualifiers")
        if not isinstance(self.lanes, tuple) or any(
            not isinstance(item, LaneCapacity) for item in self.lanes
        ):
            raise CapacityError("lanes must be an immutable LaneCapacity tuple")
        observed = tuple(item.lane for item in self.lanes)
        if len(set(observed)) != len(observed) or set(observed) != set(JobLane):
            raise CapacityError("capacity manifest must define every lane exactly once")
        if sum(item.max_queued for item in self.lanes) < self.max_queued_jobs:
            raise CapacityError("lane queue limits cannot be below the global queue limit")

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    def lane(self, lane: JobLane) -> LaneCapacity:
        if not isinstance(lane, JobLane):
            raise CapacityError("lane lookup requires a JobLane")
        return next(item for item in self.lanes if item.lane is lane)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "max_open_tournaments": self.max_open_tournaments,
            "max_round_entrants": self.max_round_entrants,
            "max_field_entrants": self.max_field_entrants,
            "max_plausible_qualifiers": self.max_plausible_qualifiers,
            "max_context_cards": self.max_context_cards,
            "max_queued_jobs": self.max_queued_jobs,
            "max_receipt_bytes": self.max_receipt_bytes,
            "max_blob_bytes": self.max_blob_bytes,
            "max_api_page_size": self.max_api_page_size,
            "reserved_imminent_jobs": self.reserved_imminent_jobs,
            "reserved_recovery_jobs": self.reserved_recovery_jobs,
            "aging_interval_ms": self.aging_interval_ms,
            "aging_increment": self.aging_increment,
            "lanes": [item.to_dict() for item in self.lanes],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CapacityManifest:
        expected = {
            "schema_version",
            "max_open_tournaments",
            "max_round_entrants",
            "max_field_entrants",
            "max_plausible_qualifiers",
            "max_context_cards",
            "max_queued_jobs",
            "max_receipt_bytes",
            "max_blob_bytes",
            "max_api_page_size",
            "reserved_imminent_jobs",
            "reserved_recovery_jobs",
            "aging_interval_ms",
            "aging_increment",
            "lanes",
        }
        _fields(value, expected)
        lanes = value["lanes"]
        if not isinstance(lanes, list):
            raise CapacityError("manifest lanes must be a JSON array")
        return cls(
            schema_version=value["schema_version"],
            max_open_tournaments=value["max_open_tournaments"],
            max_round_entrants=value["max_round_entrants"],
            max_field_entrants=value["max_field_entrants"],
            max_plausible_qualifiers=value["max_plausible_qualifiers"],
            max_context_cards=value["max_context_cards"],
            max_queued_jobs=value["max_queued_jobs"],
            max_receipt_bytes=value["max_receipt_bytes"],
            max_blob_bytes=value["max_blob_bytes"],
            max_api_page_size=value["max_api_page_size"],
            reserved_imminent_jobs=value["reserved_imminent_jobs"],
            reserved_recovery_jobs=value["reserved_recovery_jobs"],
            aging_interval_ms=value["aging_interval_ms"],
            aging_increment=value["aging_increment"],
            lanes=tuple(LaneCapacity.from_dict(item) for item in lanes),
        )

    @classmethod
    def load(cls, path: Path | str) -> CapacityManifest:
        if isinstance(path, bool) or not isinstance(path, (Path, str)):
            raise CapacityError("capacity manifest path must be a filesystem path")
        try:
            raw = Path(path).read_bytes()
            value = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CapacityError("capacity manifest cannot be read as JSON") from exc
        if not isinstance(value, dict):
            raise CapacityError("capacity manifest must be a JSON object")
        manifest = cls.from_dict(value)
        if raw not in {
            canonical_bytes(manifest.to_dict()),
            canonical_bytes(manifest.to_dict()) + b"\n",
        }:
            raise CapacityError("capacity manifest must use exact canonical JSON bytes")
        return manifest


@dataclass(frozen=True, slots=True)
class QueueLoad:
    total_active: int
    lane_active: int
    lane_leased: int

    def __post_init__(self) -> None:
        _nonnegative(self.total_active, "total_active")
        _nonnegative(self.lane_active, "lane_active")
        _nonnegative(self.lane_leased, "lane_leased")
        if self.lane_leased > self.lane_active:
            raise CapacityError("leased work cannot exceed active work in its lane")


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    admitted: bool
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.admitted, bool):
            raise CapacityError("admitted must be an explicit boolean")
        if not isinstance(self.reason, str) or not self.reason:
            raise CapacityError("admission reason must be nonempty")


@dataclass(frozen=True, slots=True)
class CapacityUse:
    """Complete proposed operational footprint checked before job admission."""

    open_tournaments: int
    round_entrants: int
    field_entrants: int
    plausible_qualifiers: int
    context_cards: int
    receipt_bytes: int
    blob_bytes: int
    api_page_size: int

    def __post_init__(self) -> None:
        for label in self.__dataclass_fields__:
            _nonnegative(getattr(self, label), label)

    def to_dict(self) -> dict[str, int]:
        return {label: getattr(self, label) for label in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CapacityUse:
        expected = set(cls.__dataclass_fields__)
        _fields(value, expected)
        return cls(**{label: value[label] for label in expected})


def validate_capacity_use(manifest: CapacityManifest, proposed: CapacityUse) -> AdmissionDecision:
    """Reject every R23.8 dimension before any durable work is inserted."""

    if not isinstance(manifest, CapacityManifest) or not isinstance(proposed, CapacityUse):
        raise CapacityError("operational admission requires typed manifest and use")
    limits = (
        ("open_tournaments", "max_open_tournaments"),
        ("round_entrants", "max_round_entrants"),
        ("field_entrants", "max_field_entrants"),
        ("plausible_qualifiers", "max_plausible_qualifiers"),
        ("context_cards", "max_context_cards"),
        ("receipt_bytes", "max_receipt_bytes"),
        ("blob_bytes", "max_blob_bytes"),
        ("api_page_size", "max_api_page_size"),
    )
    for field, limit in limits:
        if getattr(proposed, field) > getattr(manifest, limit):
            return AdmissionDecision(False, f"{field}_capacity_exceeded")
    return AdmissionDecision(True, "operational_capacity_available")


def decide_admission(
    manifest: CapacityManifest,
    lane: JobLane,
    priority: JobPriority,
    load: QueueLoad,
    *,
    for_claim: bool = False,
    maintenance_suspended: bool = False,
) -> AdmissionDecision:
    """Apply queue/lease limits without allowing cross-lane capacity theft."""

    if not isinstance(manifest, CapacityManifest):
        raise CapacityError("admission requires a CapacityManifest")
    if not isinstance(lane, JobLane) or not isinstance(priority, JobPriority):
        raise CapacityError("admission requires typed lane and priority")
    if not isinstance(load, QueueLoad):
        raise CapacityError("admission requires a QueueLoad")
    if not isinstance(for_claim, bool) or not isinstance(maintenance_suspended, bool):
        raise CapacityError("admission flags must be explicit booleans")
    limit = manifest.lane(lane)
    if maintenance_suspended and lane is JobLane.MAINTENANCE:
        return AdmissionDecision(False, "maintenance_suspended")
    if for_claim and load.lane_leased >= limit.max_leased:
        return AdmissionDecision(False, "lane_lease_full")
    if not for_claim and load.lane_active >= limit.max_queued:
        return AdmissionDecision(False, "lane_queue_full")
    if not for_claim and load.total_active >= manifest.max_queued_jobs:
        return AdmissionDecision(False, "global_queue_full")
    if (
        not for_claim
        and lane is not JobLane.LOOKUP_RECOVERY
        and load.total_active >= manifest.max_queued_jobs - manifest.reserved_recovery_jobs
    ):
        return AdmissionDecision(False, "recovery_capacity_reserved")
    if (
        not for_claim
        and lane is not JobLane.LOOKUP_RECOVERY
        and priority is not JobPriority.IMMINENT_FIELD
        and priority is not JobPriority.RECOVERY
        and load.total_active
        >= manifest.max_queued_jobs
        - manifest.reserved_recovery_jobs
        - manifest.reserved_imminent_jobs
    ):
        return AdmissionDecision(False, "imminent_capacity_reserved")
    return AdmissionDecision(True, "capacity_available")


def _fields(value: Mapping[str, Any], expected: set[str]) -> None:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise CapacityError("capacity fields are missing, unknown, or extra")


def _positive(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CapacityError(f"{label} must be a positive integer")
    return value


def _nonnegative(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CapacityError(f"{label} must be a non-negative integer")
    return value


__all__ = [
    "CAPACITY_SCHEMA_VERSION",
    "AdmissionDecision",
    "CapacityUse",
    "CapacityError",
    "CapacityManifest",
    "JobKind",
    "JobLane",
    "JobPriority",
    "JobResourceClass",
    "LaneCapacity",
    "QueueLoad",
    "decide_admission",
    "validate_capacity_use",
]
