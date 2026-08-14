"""Trusted whole-field shadow calculation and immutable receipt contracts.

The public calculator remains stateless unless a ledger is explicitly supplied.
This module adds the recovery-first orchestration needed by tournament software:
caller/request lookup always happens before a model snapshot is opened, while the
receipt core is written in the same local transaction as its predictions.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from datetime import date
from typing import TYPE_CHECKING, Any, Mapping, Optional, Sequence

from strathmark.features import normalize_prediction_as_of
from strathmark.ledger import LedgerConflictError, PredictionLedger, canonical_hash
from strathmark.predictor import (
    CompetitorRecord,
    PredictionContext,
    PredictionEngineProvider,
    WoodProfile,
    get_prediction_provider,
)

if TYPE_CHECKING:
    from strathmark.store import ResultStore

RECEIPT_CORE_SCHEMA_VERSION = "strathmark.shadow-receipt-core.v1"
ACTIVE_INPUT_SCHEMA_VERSION = "strathmark.shadow-active-input.v1"
IDENTITY_SCHEMA_VERSION = "strathmark.namespaced-identity.v1"
OBSERVATION_SCHEMA_VERSION = "strathmark.shadow-observation-fingerprint.v1"
SHADOW_TARGET_SINGLE_ELAPSED = "single-elapsed-seconds.v1"
REQUEST_PROJECTION_SCHEMA_VERSION = "strathmark.shadow-request-projection.v1"

_NAMESPACED_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,31}:[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,94}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ShadowFieldRequest:
    """Versioned trusted request metadata supplied by one server-side consumer."""

    consumer_id: str
    tournament_id: str
    event_occurrence_id: str
    field_run_id: str
    operator_id: str
    request_id: str
    run_revision: str
    event_code: str
    target_contract: str
    prediction_as_of: date | str
    schedule_fingerprint: str
    observation_schema_version: str
    observation_fingerprint: str
    seed: int = 20260811


@dataclass(frozen=True)
class ShadowLiveStatus:
    """Derived, mutable-at-read-time facts kept outside the immutable core."""

    trust: str
    mirror: str
    freshness: str
    ready_for_review: bool


@dataclass(frozen=True)
class ShadowReceipt:
    """Exact persisted core JSON plus a separately derived live projection."""

    core_json: str
    core: Mapping[str, Any]
    status: ShadowLiveStatus


@dataclass(frozen=True)
class ShadowCalculationResult:
    """Trusted recovered receipt or explicitly untrusted numeric draft."""

    receipt: Optional[ShadowReceipt]
    status: ShadowLiveStatus
    draft_predictions: tuple[Mapping[str, Any], ...] = ()

    @property
    def trusted(self) -> bool:
        return self.receipt is not None and self.status.trust == "recorded"


class ShadowReceiptCorruptionError(RuntimeError):
    """A persisted receipt failed its immutable integrity contract."""


class ShadowPredictionService:
    """Recovery-first facade around the existing V2 calculator and local ledger."""

    def __init__(
        self,
        ledger: PredictionLedger,
        *,
        result_store: ResultStore,
        prediction_provider: Optional[PredictionEngineProvider] = None,
        wood_df: Any = None,
        event_ceiling: Optional[int] = None,
    ) -> None:
        self._ledger = ledger
        if result_store is None:
            raise ValueError("trusted shadow calculation requires a durable ResultStore")
        self._prediction_provider = prediction_provider
        self._result_store = result_store
        self._wood_df = wood_df
        self._event_ceiling = event_ceiling

    def calculate(
        self,
        request: ShadowFieldRequest,
        competitors: Sequence[CompetitorRecord],
        wood: WoodProfile,
    ) -> ShadowCalculationResult:
        """Recover an exact receipt or calculate and atomically record a new one."""

        validated = _validate_request(request, competitors)
        cutoff = validated["cutoff"]
        request_projection = _request_projection(request, competitors, wood, cutoff)
        existing = self._ledger.get_shadow_receipt(
            request.consumer_id,
            request.request_id,
            expected_run_revision=request.run_revision,
        )
        if existing is not None:
            recorded_projection = existing.core.get("request_projection")
            if (
                not isinstance(recorded_projection, Mapping)
                or recorded_projection.get("fingerprint") != request_projection["fingerprint"]
            ):
                raise LedgerConflictError(
                    "request_id was already used by this caller for a different request"
                )
            existing = derive_current_receipt_status(existing, self._result_store)
            return ShadowCalculationResult(receipt=existing, status=existing.status)

        selection = self._result_store.load_evidence_for_competitors(
            [str(competitor.competitor_id) for competitor in competitors]
        )
        if selection is None:
            raise ValueError(
                "a verified local evidence snapshot is required before trusted shadow calculation"
            )
        evidence_status = selection.status
        if not evidence_status.ready_for_offline:
            raise ValueError(
                "active evidence snapshot is stale or not ready for trusted calculation"
            )
        if evidence_status.cutoff != cutoff:
            raise ValueError(
                "active evidence snapshot cutoff does not match the field cutoff; "
                "perform an explicit operator refresh"
            )
        effective_competitors = [
            replace(
                competitor,
                name=str(competitor.competitor_id),
                history=list(selection.histories[str(competitor.competitor_id)]),
            )
            for competitor in competitors
        ]
        from strathmark.calculator import (
            HandicapCalculator,
            canonical_active_v2_request,
            effective_mark_ceiling,
        )

        caller_input = canonical_active_v2_request(
            effective_competitors,
            wood,
            request.event_code,
            PredictionContext(
                prediction_as_of=cutoff,
                request_id=request.request_id,
                seed=request.seed,
                engine="v2",
            ),
            wood_df=self._wood_df,
            effective_ceiling=effective_mark_ceiling(self._event_ceiling),
        )
        active_input = {
            "schema_version": ACTIVE_INPUT_SCHEMA_VERSION,
            "tournament_id": request.tournament_id,
            "event_occurrence_id": request.event_occurrence_id,
            "field_run_id": request.field_run_id,
            "run_revision": request.run_revision,
            "target_contract": request.target_contract,
            "schedule_fingerprint": request.schedule_fingerprint,
            "caller_input": caller_input,
        }
        active_input["evidence_snapshot"] = evidence_status.input_projection()
        active_fingerprint = canonical_hash(active_input)

        provider = self._prediction_provider or get_prediction_provider()
        prediction_bundle = provider.snapshot(cutoff)
        calculation_input = canonical_active_v2_request(
            effective_competitors,
            wood,
            request.event_code,
            PredictionContext(
                prediction_as_of=cutoff,
                request_id=request.request_id,
                seed=request.seed,
                engine="v2",
            ),
            wood_df=self._wood_df,
            effective_ceiling=effective_mark_ceiling(self._event_ceiling),
            prediction_bundle=prediction_bundle,
        )

        calculator = HandicapCalculator(
            event_ceiling=self._event_ceiling,
            wood_df=self._wood_df,
            prediction_provider=provider,
            ledger_sink=self._ledger,
            ledger_caller_id=request.consumer_id,
        )
        receipt_metadata = {
            "schema_version": RECEIPT_CORE_SCHEMA_VERSION,
            "identity_schema_version": IDENTITY_SCHEMA_VERSION,
            "consumer_id": request.consumer_id,
            "tournament_id": request.tournament_id,
            "event_occurrence_id": request.event_occurrence_id,
            "field_run_id": request.field_run_id,
            "operator_id": request.operator_id,
            "request_id": request.request_id,
            "run_revision": request.run_revision,
            "event_code": request.event_code.strip().upper(),
            "target_contract": request.target_contract,
            "prediction_as_of": cutoff.isoformat(),
            "cutoff_semantics": "exclusive-utc-date",
            "request_projection": request_projection,
            "active_input": {
                **active_input,
                "fingerprint": active_fingerprint,
            },
            "calculation_input": calculation_input,
            "observation": {
                "schema_version": request.observation_schema_version,
                "fingerprint": request.observation_fingerprint,
            },
        }
        receipt_metadata["evidence_snapshot"] = evidence_status.receipt_projection()
        results = calculator.calculate(
            effective_competitors,
            wood,
            request.event_code,
            context=PredictionContext(
                prediction_as_of=cutoff,
                request_id=request.request_id,
                seed=request.seed,
                engine="v2",
            ),
            receipt_metadata=receipt_metadata,
            prediction_bundle=prediction_bundle,
        )

        receipt = self._ledger.get_shadow_receipt(
            request.consumer_id,
            request.request_id,
            current_active_fingerprint=active_fingerprint,
            expected_run_revision=request.run_revision,
        )
        if receipt is not None:
            return ShadowCalculationResult(receipt=receipt, status=receipt.status)

        trust = (
            "conflict"
            if any(row.ledger_status == "idempotency_conflict" for row in results)
            else "write-failed"
        )
        status = ShadowLiveStatus(
            trust=trust,
            mirror="not-configured",
            freshness="current",
            ready_for_review=False,
        )
        drafts = tuple(
            {
                "competitor_id": row.competitor_id,
                "median_seconds": float(row.predicted_time),
                "assigned_mark": int(row.mark),
                "prediction_id": row.prediction_id,
                "ledger_status": row.ledger_status,
            }
            for row in results
        )
        return ShadowCalculationResult(
            receipt=None,
            status=status,
            draft_predictions=drafts,
        )


def _validate_request(
    request: ShadowFieldRequest,
    competitors: Sequence[CompetitorRecord],
) -> Mapping[str, Any]:
    for field_name in (
        "consumer_id",
        "tournament_id",
        "event_occurrence_id",
        "field_run_id",
        "operator_id",
        "request_id",
        "run_revision",
    ):
        validate_namespaced_identity(getattr(request, field_name), field_name)
    if isinstance(competitors, (str, bytes, bytearray)) or not isinstance(competitors, Sequence):
        raise ValueError("competitors must be a bounded ordered sequence")
    if not competitors or len(competitors) > 64:
        raise ValueError("competitors must contain between 1 and 64 entrants")
    competitor_ids = []
    for competitor in competitors:
        validate_namespaced_identity(competitor.competitor_id, "competitor_id")
        competitor_ids.append(str(competitor.competitor_id).strip())
        if competitor.manual_time_override is not None or competitor.tournament_time is not None:
            raise ValueError(
                "manual comparison input is not permitted in a trusted shadow calculation"
            )
    if len(set(competitor_ids)) != len(competitor_ids):
        raise ValueError("competitor_id values must be unique within a field")
    if request.target_contract != SHADOW_TARGET_SINGLE_ELAPSED:
        raise ValueError(
            "unsupported shadow target; configure the approved single elapsed-time target"
        )
    event_code = str(request.event_code or "").strip().upper()
    if event_code not in {"SB", "UH"}:
        raise ValueError("event_code must be 'SB' or 'UH'")
    if request.prediction_as_of is None or not str(request.prediction_as_of).strip():
        raise ValueError("prediction_as_of must be an explicit exclusive UTC cutoff")
    cutoff = normalize_prediction_as_of(request.prediction_as_of)
    _validate_digest(request.schedule_fingerprint, "schedule_fingerprint")
    if request.observation_schema_version != OBSERVATION_SCHEMA_VERSION:
        raise ValueError("unsupported observation_schema_version")
    _validate_digest(request.observation_fingerprint, "observation_fingerprint")
    if not isinstance(request.seed, int) or isinstance(request.seed, bool):
        raise ValueError("seed must be an integer")
    return {"cutoff": cutoff}


def _request_projection(
    request: ShadowFieldRequest,
    competitors: Sequence[CompetitorRecord],
    wood: WoodProfile,
    cutoff: date,
) -> dict[str, Any]:
    """Canonical caller-controlled identity used before any current evidence read."""

    species = str(wood.species or "").strip().upper()
    if not species or len(species) > 100:
        raise ValueError("wood species must contain between 1 and 100 characters")
    try:
        diameter_mm = float(wood.diameter_mm)
        quality = int(wood.quality)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("wood diameter and quality must be numeric") from exc
    if not math.isfinite(diameter_mm) or not 0 < diameter_mm <= 10_000:
        raise ValueError("wood diameter must be finite and positive")
    if isinstance(wood.quality, bool) or not 1 <= quality <= 10:
        raise ValueError("wood quality must be between 1 and 10")
    entrants = []
    for competitor in competitors:
        gender = str(competitor.gender or "").strip().upper()
        if gender not in {"", "M", "F"}:
            raise ValueError("competitor gender must be M, F, or omitted")
        entrants.append(
            {
                "competitor_id": str(competitor.competitor_id).strip(),
                "gender": gender or "UNKNOWN",
            }
        )
    core = {
        "schema_version": REQUEST_PROJECTION_SCHEMA_VERSION,
        "consumer_id": request.consumer_id,
        "tournament_id": request.tournament_id,
        "event_occurrence_id": request.event_occurrence_id,
        "field_run_id": request.field_run_id,
        "operator_id": request.operator_id,
        "request_id": request.request_id,
        "run_revision": request.run_revision,
        "event_code": str(request.event_code).strip().upper(),
        "target_contract": request.target_contract,
        "prediction_as_of": cutoff.isoformat(),
        "cutoff_semantics": "exclusive-utc-date",
        "schedule_fingerprint": str(request.schedule_fingerprint).strip().lower(),
        "observation_schema_version": request.observation_schema_version,
        "observation_fingerprint": str(request.observation_fingerprint).strip().lower(),
        "seed": int(request.seed),
        "competitors": entrants,
        "wood": {
            "species": species,
            "diameter_mm": diameter_mm,
            "quality": quality,
        },
    }
    return {**core, "fingerprint": canonical_hash(core)}


def derive_current_receipt_status(
    receipt: ShadowReceipt,
    result_store: ResultStore,
) -> ShadowReceipt:
    """Attach a local, read-time evidence view without changing immutable core bytes."""

    freshness = "invalid"
    try:
        current = result_store.get_evidence_snapshot_status()
        active_input = receipt.core.get("active_input")
        recorded_evidence = (
            active_input.get("evidence_snapshot") if isinstance(active_input, Mapping) else None
        )
        if current is not None and isinstance(recorded_evidence, Mapping):
            current_fingerprint = canonical_hash(current.input_projection())
            recorded_fingerprint = canonical_hash(dict(recorded_evidence))
            same_cutoff = current.cutoff.isoformat() == receipt.core.get("prediction_as_of")
            if (
                current.ready_for_offline
                and same_cutoff
                and current_fingerprint == recorded_fingerprint
            ):
                freshness = "current"
            else:
                freshness = "stale"
    except Exception:
        # Immutable receipt recovery must survive unavailable or corrupt current
        # evidence.  The failure is represented in the mutable live projection.
        freshness = "invalid"
    live_status = ShadowLiveStatus(
        trust=receipt.status.trust,
        mirror=receipt.status.mirror,
        freshness=freshness,
        ready_for_review=receipt.status.trust == "recorded" and freshness == "current",
    )
    return replace(receipt, status=live_status)


def validate_namespaced_identity(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if len(text) > 128:
        raise ValueError(f"{label} must be at most 128 characters")
    if not _NAMESPACED_ID.fullmatch(text):
        raise ValueError(f"{label} must be namespaced as 'namespace:value'")
    return text


def _validate_digest(value: Any, label: str) -> str:
    text = str(value or "").strip().lower()
    if not _SHA256.fullmatch(text):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return text


__all__ = [
    "ACTIVE_INPUT_SCHEMA_VERSION",
    "IDENTITY_SCHEMA_VERSION",
    "OBSERVATION_SCHEMA_VERSION",
    "REQUEST_PROJECTION_SCHEMA_VERSION",
    "RECEIPT_CORE_SCHEMA_VERSION",
    "SHADOW_TARGET_SINGLE_ELAPSED",
    "ShadowCalculationResult",
    "ShadowFieldRequest",
    "ShadowLiveStatus",
    "ShadowPredictionService",
    "ShadowReceipt",
    "ShadowReceiptCorruptionError",
    "derive_current_receipt_status",
    "validate_namespaced_identity",
]
