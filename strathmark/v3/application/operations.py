"""Dependency-specific operational truth and redacted support export for V3.

This module deliberately has no global probes or mutable health cache.  Composition supplies
one callable per dependency and every snapshot invokes them independently, so a living HTTP
process can never masquerade as a ready prediction authority.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any

from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.evidence import require_utc_milliseconds
from strathmark.v3.contracts.identifiers import require_identifier
from strathmark.v3.infrastructure.integrity import (
    IntegrityTrustStore,
    P256Signer,
    SignedManifest,
    sign_manifest,
    verify_manifest,
)

_REASON = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ARTIFACT_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,95}\.json$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_KEY = re.compile(
    r"(?:secret|token|password|credential|authorization|cookie|private|path|dsn|key_material)",
    re.IGNORECASE,
)
_SENSITIVE_VALUE = re.compile(
    r"(?:bearer\s+\S+|-----BEGIN [A-Z ]+PRIVATE KEY-----|[A-Za-z]:[/\\])",
    re.IGNORECASE,
)
_MAX_OBSERVED_MODELS = 128


class DependencyState(str, Enum):
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    CORRUPT = "corrupt"
    STALE = "stale"
    SATURATED = "saturated"


class ReadinessPath(str, Enum):
    FIELD = "field"
    ISSUE = "issue"
    LOOKUP = "lookup"
    RESULT = "result"
    RECOVERY = "recovery"
    SUPPORT = "support"


class RoundStage(str, Enum):
    HEAT = "heat"
    QUARTER_FINAL = "quarter_final"
    SEMI_FINAL = "semi_final"
    DIVISIONAL_FINAL = "divisional_final"
    GRAND_FINAL = "grand_final"


class FieldDisposition(str, Enum):
    PREDICTIVE = "predictive"
    TRADITIONAL_MANUAL = "traditional_manual"
    PARTIAL = "partial"


class ModelResidencyError(ValueError):
    """A local model-residency transition could not be verified."""


@dataclass(frozen=True, slots=True)
class ModelResidencyPolicy:
    enabled: bool
    max_models: int

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ModelResidencyError("model residency enabled flag must be explicit")
        if (
            isinstance(self.max_models, bool)
            or not isinstance(self.max_models, int)
            or not 1 <= self.max_models <= 16
        ):
            raise ModelResidencyError("model residency maximum must be between one and sixteen")


@dataclass(frozen=True, slots=True)
class ModelResidencyReceipt:
    event_window_id: str
    enabled: bool
    state: str
    required_model_ids: tuple[str, ...]
    resident_model_ids: tuple[str, ...]
    warmed_model_ids: tuple[str, ...]
    released_model_ids: tuple[str, ...]
    observed_at: str

    def __post_init__(self) -> None:
        require_identifier(self.event_window_id, expected_namespace="event_window")
        if not isinstance(self.enabled, bool) or self.state not in {
            "active",
            "closed",
            "disabled",
        }:
            raise ModelResidencyError("model residency receipt state is invalid")
        require_utc_milliseconds(self.observed_at)
        for values in (
            self.required_model_ids,
            self.resident_model_ids,
            self.warmed_model_ids,
            self.released_model_ids,
        ):
            if values != tuple(sorted(set(values))):
                raise ModelResidencyError("model residency receipt identifiers are not canonical")
            for model_id in values:
                require_identifier(model_id, expected_namespace="model")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-model-residency-receipt-v1",
            "event_window_id": self.event_window_id,
            "enabled": self.enabled,
            "state": self.state,
            "required_model_ids": list(self.required_model_ids),
            "resident_model_ids": list(self.resident_model_ids),
            "warmed_model_ids": list(self.warmed_model_ids),
            "released_model_ids": list(self.released_model_ids),
            "observed_at": self.observed_at,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict())


class EventWindowModelResidency:
    """Reconcile caller-driven event windows against local installed model state.

    The injected ports are deliberately local process/host operations. This component neither
    downloads models nor grants prediction authority; artifact preflight remains authoritative.
    """

    def __init__(
        self,
        *,
        policy: ModelResidencyPolicy,
        installed_model_ids: Callable[[], Sequence[str]],
        resident_model_ids: Callable[[], Sequence[str]],
        warm_local_model: Callable[[str], None],
        release_local_model: Callable[[str], None],
    ) -> None:
        if not isinstance(policy, ModelResidencyPolicy):
            raise ModelResidencyError("model residency requires a typed policy")
        ports = (
            installed_model_ids,
            resident_model_ids,
            warm_local_model,
            release_local_model,
        )
        if any(not callable(port) for port in ports):
            raise ModelResidencyError("model residency requires explicit local ports")
        self._policy = policy
        self._installed_model_ids = installed_model_ids
        self._resident_model_ids = resident_model_ids
        self._warm_local_model = warm_local_model
        self._release_local_model = release_local_model

    def reconcile(
        self,
        *,
        event_window_id: str,
        active: bool,
        required_model_ids: Sequence[str],
        observed_at: str,
    ) -> ModelResidencyReceipt:
        require_identifier(event_window_id, expected_namespace="event_window")
        if not isinstance(active, bool):
            raise ModelResidencyError("event-window active flag must be explicit")
        timestamp = require_utc_milliseconds(observed_at)
        required = self._canonical_models(
            required_model_ids,
            label="required",
            maximum=self._policy.max_models,
        )
        if not self._policy.enabled:
            return ModelResidencyReceipt(
                event_window_id,
                False,
                "disabled",
                required,
                (),
                (),
                (),
                timestamp,
            )
        installed = set(self._probe(self._installed_model_ids, "installed"))
        missing = tuple(model_id for model_id in required if model_id not in installed)
        if missing:
            raise ModelResidencyError("required event-window models are not installed locally")
        before = set(self._probe(self._resident_model_ids, "resident"))
        changed: list[str] = []
        try:
            if active:
                changed = [model_id for model_id in required if model_id not in before]
                for model_id in changed:
                    self._warm_local_model(model_id)
            else:
                changed = [model_id for model_id in required if model_id in before]
                for model_id in changed:
                    self._release_local_model(model_id)
        except Exception as exc:
            raise ModelResidencyError("local model residency transition failed closed") from exc
        after = set(self._probe(self._resident_model_ids, "resident"))
        if active and not set(required).issubset(after):
            raise ModelResidencyError("required event-window models remain cold")
        if not active and set(required) & after:
            raise ModelResidencyError("closed event-window models remain resident")
        return ModelResidencyReceipt(
            event_window_id,
            True,
            "active" if active else "closed",
            required,
            tuple(sorted(after & set(required))),
            tuple(changed) if active else (),
            tuple(changed) if not active else (),
            timestamp,
        )

    def _probe(self, probe: Callable[[], Sequence[str]], label: str) -> tuple[str, ...]:
        try:
            return self._canonical_models(
                probe(),
                label=label,
                maximum=_MAX_OBSERVED_MODELS,
            )
        except ModelResidencyError:
            raise
        except Exception as exc:
            raise ModelResidencyError(f"local {label} model probe failed closed") from exc

    @staticmethod
    def _canonical_models(values: Sequence[str], *, label: str, maximum: int) -> tuple[str, ...]:
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise ModelResidencyError(f"{label} model identifiers must be a sequence")
        if len(values) > maximum:
            raise ModelResidencyError(f"{label} model count exceeds the configured maximum")
        canonical = tuple(sorted(set(values)))
        if len(canonical) != len(values):
            raise ModelResidencyError(f"{label} model identifiers must be unique")
        try:
            for model_id in canonical:
                require_identifier(model_id, expected_namespace="model")
        except (TypeError, ValueError) as exc:
            raise ModelResidencyError(f"{label} model identifier is invalid") from exc
        return canonical


_DEPENDENCIES = (
    "event_integrity",
    "projection_currency",
    "blob_integrity",
    "pinned_bundle",
    "formula",
    "ml",
    "llm_local_primary",
    "llm_local_secondary",
    "llm_cloud",
    "pool_degradation_mode",
    "writer_latency",
    "queue_deadline_risk",
    "disk_reserve",
    "backup_age",
    "issue_recovery_path",
    "cloud_consent",
)

_PATH_DEPENDENCIES: dict[ReadinessPath, tuple[str, ...]] = {
    ReadinessPath.FIELD: (
        "event_integrity",
        "projection_currency",
        "blob_integrity",
        "pinned_bundle",
        "formula",
        "ml",
        "pool_degradation_mode",
        "writer_latency",
        "queue_deadline_risk",
        "disk_reserve",
        "issue_recovery_path",
    ),
    ReadinessPath.ISSUE: (
        "event_integrity",
        "projection_currency",
        "blob_integrity",
        "writer_latency",
        "disk_reserve",
        "issue_recovery_path",
    ),
    ReadinessPath.LOOKUP: ("event_integrity", "blob_integrity", "issue_recovery_path"),
    ReadinessPath.RESULT: (
        "event_integrity",
        "projection_currency",
        "writer_latency",
        "disk_reserve",
        "issue_recovery_path",
    ),
    ReadinessPath.RECOVERY: (
        "event_integrity",
        "blob_integrity",
        "disk_reserve",
        "backup_age",
        "issue_recovery_path",
    ),
    ReadinessPath.SUPPORT: ("event_integrity", "disk_reserve"),
}

REQUIRED_OPERATIONAL_METRICS = (
    "stage_latency_ms",
    "deadline_misses",
    "invalid_outputs",
    "abstentions",
    "degraded_sheets",
    "disagreement_green",
    "disagreement_amber",
    "disagreement_red",
    "weight_movement_ppm",
    "calibration_loss_ppm",
    "score_drift_ppm",
    "projection_lag_events",
    "outbox_lag_items",
    "recovery_successes",
    "recovery_failures",
)

_QUEUE_LANES = ("hot_field", "inference", "lookup_recovery", "maintenance")
_ASSESSORS = (
    "formula",
    "ml",
    "llm_local_primary",
    "llm_local_secondary",
    "llm_cloud",
)
_WARM_MODELS = ("llm_local_primary", "llm_local_secondary")


@dataclass(frozen=True, slots=True)
class DependencyObservation:
    name: str
    state: DependencyState
    reason_code: str
    observed_at: str
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.name not in _DEPENDENCIES:
            raise ValueError("dependency name is not in the closed operational graph")
        if not isinstance(self.state, DependencyState):
            raise ValueError("dependency state must use the closed vocabulary")
        if not isinstance(self.reason_code, str) or _REASON.fullmatch(self.reason_code) is None:
            raise ValueError("dependency reason code is invalid")
        require_utc_milliseconds(self.observed_at)
        if self.detail is not None and (
            not isinstance(self.detail, str)
            or not self.detail
            or len(self.detail.encode("utf-8")) > 256
        ):
            raise ValueError("dependency detail must be bounded operator text")

    @property
    def ready(self) -> bool:
        return self.state in {DependencyState.READY, DependencyState.DEGRADED}

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "name": self.name,
            "state": self.state.value,
            "reason_code": self.reason_code,
            "observed_at": self.observed_at,
        }
        if self.detail is not None:
            value["detail"] = self.detail
        return value


Probe = Callable[[], DependencyObservation]


@dataclass(frozen=True, slots=True)
class OperationalProbeSet:
    event_integrity: Probe
    projection_currency: Probe
    blob_integrity: Probe
    pinned_bundle: Probe
    formula: Probe
    ml: Probe
    llm_local_primary: Probe
    llm_local_secondary: Probe
    llm_cloud: Probe
    pool_degradation_mode: Probe
    writer_latency: Probe
    queue_deadline_risk: Probe
    disk_reserve: Probe
    backup_age: Probe
    issue_recovery_path: Probe
    cloud_consent: Probe

    def __post_init__(self) -> None:
        if any(not callable(getattr(self, name)) for name in _DEPENDENCIES):
            raise ValueError("every operational dependency requires an explicit probe")

    @staticmethod
    def required_dependency_names() -> tuple[str, ...]:
        return _DEPENDENCIES


@dataclass(frozen=True, slots=True)
class PathReadiness:
    path: ReadinessPath
    required_dependencies: tuple[str, ...]
    blocking_dependencies: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.blocking_dependencies

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path.value,
            "required_dependencies": list(self.required_dependencies),
            "blocking_dependencies": list(self.blocking_dependencies),
            "ready": self.ready,
        }


@dataclass(frozen=True, slots=True)
class OperationalFacts:
    telemetry_available: bool
    active_bundle_digest: str | None
    frozen_epoch_id: str | None
    frozen_weights_digest: str | None
    queue_depths: tuple[tuple[str, int], ...]
    oldest_job_age_ms: int | None
    assessor_availability: tuple[tuple[str, bool], ...]
    model_warmth: tuple[tuple[str, bool], ...]
    last_event_digest: str | None
    projection_healthy: bool
    backup_healthy: bool
    readiness_sla_risk: bool

    def __post_init__(self) -> None:
        for name in (
            "telemetry_available",
            "projection_healthy",
            "backup_healthy",
            "readiness_sla_risk",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError("operational fact flags must be explicit")
        for name in ("active_bundle_digest", "frozen_weights_digest", "last_event_digest"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str) or _DIGEST.fullmatch(value) is None
            ):
                raise ValueError("operational fact digest is invalid")
        if self.frozen_epoch_id is not None and (
            not isinstance(self.frozen_epoch_id, str)
            or not self.frozen_epoch_id.startswith("epoch:")
        ):
            raise ValueError("frozen epoch identity is invalid")
        if tuple(name for name, _depth in self.queue_depths) != _QUEUE_LANES:
            raise ValueError("operational queue lanes must be complete and canonical")
        if any(
            isinstance(depth, bool) or not isinstance(depth, int) or depth < 0
            for _name, depth in self.queue_depths
        ):
            raise ValueError("operational queue depths must be non-negative")
        if self.oldest_job_age_ms is not None and (
            isinstance(self.oldest_job_age_ms, bool)
            or not isinstance(self.oldest_job_age_ms, int)
            or self.oldest_job_age_ms < 0
        ):
            raise ValueError("oldest job age must be non-negative")
        for collection, label, expected in (
            (self.assessor_availability, "assessor availability", _ASSESSORS),
            (self.model_warmth, "model warmth", _WARM_MODELS),
        ):
            names = tuple(name for name, _ready in collection)
            if (
                not collection
                or len(names) != len(set(names))
                or (self.telemetry_available and names != expected)
                or any(
                    not isinstance(name, str) or not name or not isinstance(ready, bool)
                    for name, ready in collection
                )
            ):
                raise ValueError(f"{label} must be complete, unique, and explicit")

    @classmethod
    def unavailable(cls) -> OperationalFacts:
        return cls(
            False,
            None,
            None,
            None,
            tuple((name, 0) for name in _QUEUE_LANES),
            None,
            (("unavailable", False),),
            (("unavailable", False),),
            None,
            False,
            False,
            True,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "telemetry_available": self.telemetry_available,
            "active_bundle_digest": self.active_bundle_digest,
            "frozen_epoch_id": self.frozen_epoch_id,
            "frozen_weights_digest": self.frozen_weights_digest,
            "queue_depths": dict(self.queue_depths),
            "oldest_job_age_ms": self.oldest_job_age_ms,
            "assessor_availability": dict(self.assessor_availability),
            "model_warmth": dict(self.model_warmth),
            "last_event_digest": self.last_event_digest,
            "projection_healthy": self.projection_healthy,
            "backup_healthy": self.backup_healthy,
            "readiness_sla_risk": self.readiness_sla_risk,
        }


@dataclass(frozen=True, slots=True)
class OperationalMetrics:
    available: bool
    values: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.available, bool):
            raise ValueError("operational metric availability must be explicit")
        names = tuple(name for name, _value in self.values)
        if self.available and names != REQUIRED_OPERATIONAL_METRICS:
            raise ValueError("operational metrics must be complete and canonical")
        if not self.available and self.values:
            raise ValueError("unavailable operational metrics cannot contain values")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for _name, value in self.values
        ):
            raise ValueError("operational metrics must be non-negative integers")

    @classmethod
    def from_mapping(cls, values: Mapping[str, int]) -> OperationalMetrics:
        if not isinstance(values, Mapping) or tuple(values) != REQUIRED_OPERATIONAL_METRICS:
            raise ValueError("operational metrics must be complete and canonical")
        return cls(True, tuple((name, values[name]) for name in REQUIRED_OPERATIONAL_METRICS))

    @classmethod
    def unavailable(cls) -> OperationalMetrics:
        return cls(False, ())

    def to_dict(self) -> dict[str, Any]:
        return {"available": self.available, "values": dict(self.values)}


@dataclass(frozen=True, slots=True)
class OperationalStatus:
    observed_at: str
    dependencies: tuple[DependencyObservation, ...]
    paths: Mapping[ReadinessPath, PathReadiness]
    facts: OperationalFacts
    metrics: OperationalMetrics

    def __post_init__(self) -> None:
        require_utc_milliseconds(self.observed_at)
        if tuple(item.name for item in self.dependencies) != _DEPENDENCIES:
            raise ValueError("operational dependencies must be complete and canonical")
        if set(self.paths) != set(ReadinessPath):
            raise ValueError("operational readiness paths must be complete")
        if not isinstance(self.facts, OperationalFacts) or not isinstance(
            self.metrics, OperationalMetrics
        ):
            raise ValueError("operational status requires typed facts and metrics")
        object.__setattr__(self, "paths", MappingProxyType(dict(self.paths)))

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    def dependency(self, name: str) -> DependencyObservation:
        for item in self.dependencies:
            if item.name == name:
                return item
        raise ValueError("dependency name is not in the snapshot")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-operational-status-v1",
            "observed_at": self.observed_at,
            "dependencies": [item.to_dict() for item in self.dependencies],
            "paths": {
                path.value: self.paths[path].to_dict()
                for path in sorted(ReadinessPath, key=lambda item: item.value)
            },
            "facts": self.facts.to_dict(),
            "metrics": self.metrics.to_dict(),
        }


class OperationalStatusService:
    def __init__(self, probes: OperationalProbeSet) -> None:
        if not isinstance(probes, OperationalProbeSet):
            raise ValueError("operational status requires a typed probe set")
        self._probes = probes

    def snapshot(
        self,
        *,
        observed_at: str,
        facts: OperationalFacts | None = None,
        metrics: OperationalMetrics | None = None,
    ) -> OperationalStatus:
        timestamp = require_utc_milliseconds(observed_at)
        observations: list[DependencyObservation] = []
        for name in _DEPENDENCIES:
            try:
                observation = getattr(self._probes, name)()
                if not isinstance(observation, DependencyObservation):
                    raise TypeError("probe result is not an observation")
                if observation.name != name:
                    observation = DependencyObservation(
                        name, DependencyState.UNAVAILABLE, "probe_identity_mismatch", timestamp
                    )
                elif observation.observed_at != timestamp:
                    observation = DependencyObservation(
                        name, DependencyState.STALE, "probe_timestamp_mismatch", timestamp
                    )
            except Exception:
                observation = DependencyObservation(
                    name, DependencyState.UNAVAILABLE, "probe_failed_closed", timestamp
                )
            observations.append(observation)
        by_name = {item.name: item for item in observations}
        paths = {
            path: PathReadiness(
                path,
                required,
                tuple(name for name in required if not by_name[name].ready),
            )
            for path, required in _PATH_DEPENDENCIES.items()
        }
        if facts is not None and not isinstance(facts, OperationalFacts):
            raise ValueError("operational facts must be typed")
        if metrics is not None and not isinstance(metrics, OperationalMetrics):
            raise ValueError("operational metrics must be typed")
        return OperationalStatus(
            timestamp,
            tuple(observations),
            paths,
            OperationalFacts.unavailable() if facts is None else facts,
            OperationalMetrics.unavailable() if metrics is None else metrics,
        )


@dataclass(frozen=True, slots=True)
class RaceDayField:
    field_id: str
    stage: RoundStage
    epoch: int
    ordered_competitors: tuple[str, ...]
    marks_seconds: tuple[tuple[str, int], ...]
    official_placing: tuple[str, ...]
    winner: str
    result_to_ready_ms: int
    field_assembly_ms: int
    disposition: FieldDisposition
    receipt_digest: str
    scheduled_start_offset_ms: int = 0
    called_after_prior_result_ms: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.field_id, str) or not self.field_id.startswith("field:"):
            raise ValueError("race-day field id is invalid")
        if not isinstance(self.stage, RoundStage):
            raise ValueError("race-day stage must use the closed progression")
        if isinstance(self.epoch, bool) or not isinstance(self.epoch, int) or self.epoch <= 0:
            raise ValueError("race-day epoch must be positive")
        if (
            not isinstance(self.ordered_competitors, tuple)
            or len(self.ordered_competitors) < 2
            or len(set(self.ordered_competitors)) != len(self.ordered_competitors)
            or any(not isinstance(item, str) or not item for item in self.ordered_competitors)
        ):
            raise ValueError("race-day field competitors must be unique and immutable")
        if tuple(name for name, _mark in self.marks_seconds) != self.ordered_competitors:
            raise ValueError("marks must bind the exact ordered field")
        if any(
            isinstance(mark, bool) or not isinstance(mark, int) or mark < 3
            for _, mark in self.marks_seconds
        ):
            raise ValueError("race-day marks must be whole seconds rebased to Mark-3")
        if min(mark for _name, mark in self.marks_seconds) != 3:
            raise ValueError("every field must use Mark-3 rebasing")
        if self.official_placing != tuple(dict.fromkeys(self.official_placing)) or set(
            self.official_placing
        ) != set(self.ordered_competitors):
            raise ValueError("official placing must be an exact field permutation")
        if not self.official_placing or self.winner != self.official_placing[0]:
            raise ValueError("winner must equal the immutable official placing")
        for name in (
            "result_to_ready_ms",
            "field_assembly_ms",
            "scheduled_start_offset_ms",
            "called_after_prior_result_ms",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("race-day latency must be a non-negative integer")
        if self.result_to_ready_ms > 120_000:
            raise ValueError("result-to-ready exceeds the two-minute service level")
        if self.field_assembly_ms >= 2_000:
            raise ValueError("field assembly must remain under two seconds")
        if not isinstance(self.disposition, FieldDisposition):
            raise ValueError("field disposition is invalid")
        if self.disposition is FieldDisposition.PARTIAL:
            raise ValueError("partial field state is prohibited")
        if self.disposition not in {
            FieldDisposition.PREDICTIVE,
            FieldDisposition.TRADITIONAL_MANUAL,
        }:
            raise ValueError("field disposition is invalid")
        if (
            not isinstance(self.receipt_digest, str)
            or _DIGEST.fullmatch(self.receipt_digest) is None
        ):
            raise ValueError("race-day receipt digest is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_id": self.field_id,
            "stage": self.stage.value,
            "epoch": self.epoch,
            "ordered_competitors": list(self.ordered_competitors),
            "marks_seconds": [list(item) for item in self.marks_seconds],
            "official_placing": list(self.official_placing),
            "winner": self.winner,
            "result_to_ready_ms": self.result_to_ready_ms,
            "field_assembly_ms": self.field_assembly_ms,
            "disposition": self.disposition.value,
            "receipt_digest": self.receipt_digest,
            "scheduled_start_offset_ms": self.scheduled_start_offset_ms,
            "called_after_prior_result_ms": self.called_after_prior_result_ms,
        }


@dataclass(frozen=True, slots=True)
class RaceDayReplayReport:
    field_count: int
    stage_count: int
    maximum_result_to_ready_ms: int
    maximum_field_assembly_ms: int
    manual_traditional_fields: tuple[str, ...]
    maximum_heat_interval_ms: int
    grand_final_turnaround_ms: int
    same_round_epochs_verified: bool = True
    between_round_updates_verified: bool = True
    mark_three_rebasing_verified: bool = True
    immutable_winners_verified: bool = True

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-race-day-replay-v1",
            "field_count": self.field_count,
            "stage_count": self.stage_count,
            "maximum_result_to_ready_ms": self.maximum_result_to_ready_ms,
            "maximum_field_assembly_ms": self.maximum_field_assembly_ms,
            "manual_traditional_fields": list(self.manual_traditional_fields),
            "maximum_heat_interval_ms": self.maximum_heat_interval_ms,
            "grand_final_turnaround_ms": self.grand_final_turnaround_ms,
            "same_round_epochs_verified": self.same_round_epochs_verified,
            "between_round_updates_verified": self.between_round_updates_verified,
            "mark_three_rebasing_verified": self.mark_three_rebasing_verified,
            "immutable_winners_verified": self.immutable_winners_verified,
        }


def verify_race_day_replay(fields: tuple[RaceDayField, ...]) -> RaceDayReplayReport:
    if (
        not isinstance(fields, tuple)
        or not fields
        or any(not isinstance(item, RaceDayField) for item in fields)
    ):
        raise ValueError("race-day replay requires a nonempty immutable transcript")
    if len({item.field_id for item in fields}) != len(fields):
        raise ValueError("race-day field identities must be unique")
    stage_order = tuple(RoundStage)
    grouped: dict[RoundStage, list[RaceDayField]] = {}
    observed_indices: list[int] = []
    for field in fields:
        grouped.setdefault(field.stage, []).append(field)
        observed_indices.append(stage_order.index(field.stage))
    if observed_indices != sorted(observed_indices):
        raise ValueError("race-day stages must be canonically ordered")
    if set(grouped) != set(RoundStage):
        raise ValueError("race-day replay must cover the complete tournament progression")
    heats = grouped[RoundStage.HEAT]
    heat_offsets = tuple(item.scheduled_start_offset_ms for item in heats)
    if heat_offsets != tuple(sorted(heat_offsets)) or len(set(heat_offsets)) != len(heat_offsets):
        raise ValueError("heat schedule offsets must be unique and increasing")
    heat_intervals = tuple(
        current - previous for previous, current in zip(heat_offsets, heat_offsets[1:])
    )
    if any(interval > 600_000 for interval in heat_intervals):
        raise ValueError("heat schedule exceeds the ten-minute cadence")
    grand_final = grouped[RoundStage.GRAND_FINAL]
    if len(grand_final) != 1 or grand_final[0].called_after_prior_result_ms > 300_000:
        raise ValueError("grand final exceeds the five-minute finalist turnaround")
    prior_epoch = 0
    prior_winners: set[str] = set()
    for stage in stage_order:
        stage_fields = grouped[stage]
        epochs = {item.epoch for item in stage_fields}
        if len(epochs) != 1:
            raise ValueError("same-round epoch differs across fields")
        epoch = next(iter(epochs))
        if epoch <= prior_epoch:
            raise ValueError("between-round epoch must strictly increase")
        entrants = {entrant for item in stage_fields for entrant in item.ordered_competitors}
        if prior_winners and not prior_winners <= entrants:
            raise ValueError("a prior immutable winner is absent from the next round")
        prior_winners = {item.winner for item in stage_fields}
        prior_epoch = epoch
    return RaceDayReplayReport(
        field_count=len(fields),
        stage_count=len(grouped),
        maximum_result_to_ready_ms=max(item.result_to_ready_ms for item in fields),
        maximum_field_assembly_ms=max(item.field_assembly_ms for item in fields),
        manual_traditional_fields=tuple(
            item.field_id
            for item in fields
            if item.disposition is FieldDisposition.TRADITIONAL_MANUAL
        ),
        maximum_heat_interval_ms=max(heat_intervals, default=0),
        grand_final_turnaround_ms=grand_final[0].called_after_prior_result_ms,
    )


_RECOVERY_FAILURES = (
    "process_restart",
    "machine_restart",
    "worker_crash",
    "ollama_restart",
    "cloud_timeout",
    "power_loss",
    "wal_recovery",
    "blob_corruption",
    "disk_reserve",
    "queue_saturation",
)


@dataclass(frozen=True, slots=True)
class RecoveryTrial:
    failure: str
    recovered: bool
    duplicate_forecasts: int
    receipt_before_digest: str
    receipt_after_digest: str
    authority_after: str
    recovery_ms: int

    def __post_init__(self) -> None:
        if self.failure not in _RECOVERY_FAILURES:
            raise ValueError("recovery failure kind is not recognized")
        if not isinstance(self.recovered, bool):
            raise ValueError("recovery result must be explicit")
        if (
            isinstance(self.duplicate_forecasts, bool)
            or not isinstance(self.duplicate_forecasts, int)
            or self.duplicate_forecasts < 0
        ):
            raise ValueError("duplicate forecast count must be non-negative")
        if (
            not isinstance(self.recovery_ms, int)
            or isinstance(self.recovery_ms, bool)
            or self.recovery_ms < 0
            or self.recovery_ms > 300_000
        ):
            raise ValueError("recovery time exceeds the bounded rehearsal window")
        for value in (self.receipt_before_digest, self.receipt_after_digest):
            if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
                raise ValueError("recovery receipt digest is invalid")
        if self.authority_after not in {"v3", "traditional_manual"}:
            raise ValueError("recovery must declare one supported authority")


@dataclass(frozen=True, slots=True)
class RecoveryMatrixReport:
    failures: tuple[str, ...]
    maximum_recovery_ms: int
    zero_duplicate_forecasts: bool
    immutable_receipts: bool

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "schema_version": "strathmark-v3-recovery-matrix-v1",
                "failures": list(self.failures),
                "maximum_recovery_ms": self.maximum_recovery_ms,
                "zero_duplicate_forecasts": self.zero_duplicate_forecasts,
                "immutable_receipts": self.immutable_receipts,
            }
        )


def verify_recovery_matrix(trials: tuple[RecoveryTrial, ...]) -> RecoveryMatrixReport:
    if (
        not isinstance(trials, tuple)
        or tuple(item.failure for item in trials) != _RECOVERY_FAILURES
    ):
        raise ValueError("recovery matrix must be complete, unique, and canonically ordered")
    if any(not isinstance(item, RecoveryTrial) for item in trials):
        raise ValueError("recovery matrix must contain typed trials")
    if any(not item.recovered for item in trials):
        raise ValueError("recovery matrix contains an unrecovered failure")
    if any(item.duplicate_forecasts for item in trials):
        raise ValueError("recovery matrix detected a duplicate forecast")
    if any(item.receipt_before_digest != item.receipt_after_digest for item in trials):
        raise ValueError("recovery matrix detected a changed immutable receipt")
    return RecoveryMatrixReport(
        _RECOVERY_FAILURES,
        max(item.recovery_ms for item in trials),
        True,
        True,
    )


class SupportBundleExporter:
    """Build a deterministic, signed ZIP from already-authorized support material."""

    def __init__(self, *, signer: P256Signer, max_bytes: int = 8_388_608) -> None:
        if not hasattr(signer, "identity") or not callable(getattr(signer, "sign", None)):
            raise ValueError("support export requires a signer")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 16_384:
            raise ValueError("support bundle maximum is invalid")
        self._signer = signer
        self._max_bytes = max_bytes

    def export(
        self,
        *,
        status: OperationalStatus,
        created_at: str,
        artifacts: Mapping[str, Any],
    ) -> bytes:
        if not isinstance(status, OperationalStatus) or not isinstance(artifacts, Mapping):
            raise ValueError("support export requires typed status and artifact mapping")
        timestamp = require_utc_milliseconds(created_at)
        if "status.json" in artifacts or "support-manifest.json" in artifacts:
            raise ValueError("support artifact name is reserved")
        entries: dict[str, bytes] = {"status.json": canonical_bytes(status.to_dict())}
        for name, value in artifacts.items():
            if not isinstance(name, str) or _ARTIFACT_NAME.fullmatch(name) is None:
                raise ValueError("support artifact name is unsafe")
            entries[name] = canonical_bytes(_redact(value), max_bytes=self._max_bytes)
        if sum(len(value) for value in entries.values()) > self._max_bytes:
            raise ValueError("support material exceeds maximum bytes")
        payload = {
            "schema_version": "strathmark-v3-support-bundle-v1",
            "status_digest": status.digest,
            "entry_count": len(entries),
            "entries": [
                {
                    "name": name,
                    "byte_count": len(entries[name]),
                    "sha256": hashlib.sha256(entries[name]).hexdigest(),
                }
                for name in sorted(entries)
            ],
        }
        manifest = sign_manifest(
            "support_bundle", payload, signer=self._signer, created_at=timestamp
        )
        entries["support-manifest.json"] = canonical_bytes(manifest.to_dict())
        encoded = _encode_zip(entries)
        if len(encoded) > self._max_bytes:
            raise ValueError("support bundle exceeds maximum bytes")
        return encoded


def verify_support_bundle(bundle: bytes, *, trust_store: IntegrityTrustStore) -> dict[str, Any]:
    if not isinstance(bundle, bytes) or not bundle or len(bundle) > 8_388_608:
        raise ValueError("support bundle bytes are invalid")
    try:
        with zipfile.ZipFile(io.BytesIO(bundle), "r") as archive:
            names = archive.namelist()
            if (
                names != sorted(names)
                or len(names) != len(set(names))
                or "support-manifest.json" not in names
                or any(_ARTIFACT_NAME.fullmatch(name) is None for name in names)
            ):
                raise ValueError("support bundle entries are unsafe or noncanonical")
            material = {name: archive.read(name) for name in names}
            if archive.testzip() is not None:
                raise ValueError("support bundle digest verification failed")
        if bundle != _encode_zip(material):
            raise ValueError("support bundle digest verification failed")
        manifest_value = json.loads(material.pop("support-manifest.json"))
        manifest = SignedManifest.from_dict(manifest_value)
        if manifest.kind != "support_bundle":
            raise ValueError("support bundle manifest kind differs")
        payload = verify_manifest(manifest, trust_store)
        expected = payload.get("entries")
        observed = [
            {
                "name": name,
                "byte_count": len(value),
                "sha256": hashlib.sha256(value).hexdigest(),
            }
            for name, value in sorted(material.items())
        ]
        if (
            payload.get("schema_version") != "strathmark-v3-support-bundle-v1"
            or payload.get("entry_count") != len(material)
            or expected != observed
        ):
            raise ValueError("support bundle digest verification failed")
        status = json.loads(material["status.json"])
        if payload.get("status_digest") != canonical_digest(status):
            raise ValueError("support bundle status digest differs")
        return payload
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("support bundle digest verification failed") from exc


def _encode_zip(entries: Mapping[str, bytes]) -> bytes:
    target = io.BytesIO()
    with zipfile.ZipFile(
        target, "w", compression=zipfile.ZIP_STORED, strict_timestamps=True
    ) as archive:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o600 << 16
            archive.writestr(info, entries[name])
    return target.getvalue()


def _redact(value: Any, *, key: str = "") -> Any:
    if _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(name): _redact(item, key=str(name)) for name, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_redact(item) for item in value]
    if isinstance(value, str) and _SENSITIVE_VALUE.search(value):
        return "[REDACTED]"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return "[REDACTED]"


__all__ = [
    "DependencyObservation",
    "DependencyState",
    "EventWindowModelResidency",
    "FieldDisposition",
    "ModelResidencyError",
    "ModelResidencyPolicy",
    "ModelResidencyReceipt",
    "OperationalProbeSet",
    "OperationalFacts",
    "OperationalMetrics",
    "OperationalStatus",
    "OperationalStatusService",
    "PathReadiness",
    "ReadinessPath",
    "REQUIRED_OPERATIONAL_METRICS",
    "RaceDayField",
    "RaceDayReplayReport",
    "RecoveryMatrixReport",
    "RecoveryTrial",
    "RoundStage",
    "SupportBundleExporter",
    "verify_race_day_replay",
    "verify_recovery_matrix",
    "verify_support_bundle",
]
