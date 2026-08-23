"""Provider-independent result and audit contracts shared by V3 assessors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol

from strathmark.v3.contracts.canonical import canonical_decimal_string, canonical_digest
from strathmark.v3.contracts.evidence import (
    EvidencePacket,
    _require_digest,
    require_utc_milliseconds,
)
from strathmark.v3.contracts.forecasts import AssessorForecast
from strathmark.v3.contracts.identifiers import StableIdentifier
from strathmark.v3.contracts.statuses import admit_raw_completion
from strathmark.v3.domain.evidence import AdmissionReason, EvidenceSource

_VERIFIED_GOVERNOR_PROJECTION = object()


class ReviewClassification(str, Enum):
    """Deterministic exception classification; authority remains outside STRATHMARK."""

    GREEN = "green"
    AMBER = "amber"
    RED = "red"


class EvidenceQuality(str, Enum):
    """Frozen provenance class used by every assessor-specific packet projection."""

    ISSUED_OFFICIAL = "issued_official"
    VERIFIED_HISTORICAL = "verified_historical"


class TournamentRelevance(str, Enum):
    """Frozen tournament relationship; never inferred from opaque identifiers."""

    ACTIVE = "active"
    OTHER_AUTHORITATIVE = "other_authoritative"
    LEGACY = "legacy"


class EvidenceOrigin(str, Enum):
    """Governor-verifiable source class; assessor quality is derived from this origin."""

    ISSUED_RESULT_RECEIPT = "issued_result_receipt"
    VERIFIED_HISTORICAL_IMPORT = "verified_historical_import"


@dataclass(frozen=True, slots=True, order=True)
class FormulaObservationProvenance:
    """One evidence-governor authority binding, never an assessor caller label."""

    evidence_id: StableIdentifier
    source: EvidenceSource
    admission_reason: AdmissionReason
    numeric_eligible: bool
    authority_digest: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.evidence_id, StableIdentifier)
            or self.evidence_id.namespace != "evidence"
        ):
            raise ValueError("formula provenance requires an evidence identifier")
        if not isinstance(self.source, EvidenceSource):
            raise ValueError("formula provenance source must be an EvidenceSource")
        if not isinstance(self.admission_reason, AdmissionReason):
            raise ValueError("formula provenance reason must be an AdmissionReason")
        if not isinstance(self.numeric_eligible, bool):
            raise ValueError("formula provenance numeric eligibility must be explicit")
        _require_digest(self.authority_digest, "formula provenance authority_digest")

    def to_dict(self) -> dict[str, object]:
        return {
            "evidence_id": str(self.evidence_id),
            "source": self.source.value,
            "admission_reason": self.admission_reason.value,
            "numeric_eligible": self.numeric_eligible,
            "authority_digest": self.authority_digest,
        }


@dataclass(frozen=True, slots=True)
class FormulaGovernorReceipt:
    """Audit receipt created only after external signature and epoch verification."""

    evidence_digest: str
    historical_cutoff_key: str
    tournament_epoch_id: StableIdentifier
    tournament_epoch_content_digest: str
    cutoff_at_utc: str
    active_tournament_id: StableIdentifier
    authoritative_tournament_ids: tuple[StableIdentifier, ...]
    legacy_tournament_ids: tuple[StableIdentifier, ...]
    provenance: tuple[FormulaObservationProvenance, ...]
    signed_manifest_body_digest: str
    signer_key_id: str
    receipt_digest: str

    def __post_init__(self) -> None:
        _require_digest(self.evidence_digest, "formula governor evidence_digest")
        if not isinstance(
            self.historical_cutoff_key, str
        ) or not self.historical_cutoff_key.startswith("history:"):
            raise ValueError("formula governor requires a historical cutoff key")
        if (
            not isinstance(self.tournament_epoch_id, StableIdentifier)
            or self.tournament_epoch_id.namespace != "epoch"
        ):
            raise ValueError("formula governor requires an epoch identifier")
        _require_digest(
            self.tournament_epoch_content_digest,
            "formula governor epoch content digest",
        )
        require_utc_milliseconds(self.cutoff_at_utc)
        if (
            not isinstance(self.active_tournament_id, StableIdentifier)
            or self.active_tournament_id.namespace != "tournament"
        ):
            raise ValueError("formula governor requires an active tournament identifier")
        for values, label in (
            (self.authoritative_tournament_ids, "authoritative tournaments"),
            (self.legacy_tournament_ids, "legacy tournaments"),
        ):
            if not isinstance(values, tuple) or not all(
                isinstance(item, StableIdentifier) and item.namespace == "tournament"
                for item in values
            ):
                raise ValueError(f"formula governor {label} must be an immutable typed tuple")
            if values != tuple(sorted(values)) or len(values) != len(set(values)):
                raise ValueError(f"formula governor {label} must be unique and sorted")
        all_tournaments = (
            (self.active_tournament_id,)
            + self.authoritative_tournament_ids
            + self.legacy_tournament_ids
        )
        if len(all_tournaments) != len(set(all_tournaments)):
            raise ValueError("formula governor tournament authority sets must be disjoint")
        if not isinstance(self.provenance, tuple) or not all(
            isinstance(item, FormulaObservationProvenance) for item in self.provenance
        ):
            raise ValueError("formula governor provenance must be an immutable typed tuple")
        provenance_ids = tuple(item.evidence_id for item in self.provenance)
        if len(provenance_ids) != len(set(provenance_ids)):
            raise ValueError("formula governor provenance evidence identifiers must be unique")
        _require_digest(
            self.signed_manifest_body_digest,
            "formula governor signed manifest body digest",
        )
        if not isinstance(self.signer_key_id, str) or not self.signer_key_id.startswith(
            "integrity-key:"
        ):
            raise ValueError("formula governor requires a signed integrity key identifier")
        _require_digest(self.receipt_digest, "formula governor receipt_digest")
        if self.receipt_digest != canonical_digest(self._content_value()):
            raise ValueError("formula governor receipt digest mismatch")

    @classmethod
    def _from_verified_projection(
        cls,
        *,
        evidence: EvidencePacket,
        tournament_epoch_content_digest: str,
        cutoff_at_utc: str,
        active_tournament_id: StableIdentifier,
        authoritative_tournament_ids: tuple[StableIdentifier, ...],
        legacy_tournament_ids: tuple[StableIdentifier, ...],
        provenance: tuple[FormulaObservationProvenance, ...],
        signed_manifest_body_digest: str,
        signer_key_id: str,
        _verification: object,
    ) -> FormulaGovernorReceipt:
        if _verification is not _VERIFIED_GOVERNOR_PROJECTION:
            raise ValueError("formula governor receipt requires verified signed authority")
        arguments = {
            "evidence_digest": evidence.content_digest,
            "historical_cutoff_key": evidence.historical_cutoff_key,
            "tournament_epoch_id": evidence.tournament_epoch_id,
            "tournament_epoch_content_digest": tournament_epoch_content_digest,
            "cutoff_at_utc": cutoff_at_utc,
            "active_tournament_id": active_tournament_id,
            "authoritative_tournament_ids": authoritative_tournament_ids,
            "legacy_tournament_ids": legacy_tournament_ids,
            "provenance": provenance,
            "signed_manifest_body_digest": signed_manifest_body_digest,
            "signer_key_id": signer_key_id,
        }
        receipt = cls(
            **arguments,
            receipt_digest=canonical_digest(cls._content_value_from_arguments(**arguments)),
        )
        _derive_formula_facts(evidence, receipt)
        return receipt

    @staticmethod
    def _content_value_from_arguments(**arguments: Any) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-formula-governor-receipt-v1",
            "evidence_digest": arguments["evidence_digest"],
            "historical_cutoff_key": arguments["historical_cutoff_key"],
            "tournament_epoch_id": str(arguments["tournament_epoch_id"]),
            "tournament_epoch_content_digest": arguments["tournament_epoch_content_digest"],
            "cutoff_at_utc": arguments["cutoff_at_utc"],
            "active_tournament_id": str(arguments["active_tournament_id"]),
            "authoritative_tournament_ids": [
                str(item) for item in arguments["authoritative_tournament_ids"]
            ],
            "legacy_tournament_ids": [str(item) for item in arguments["legacy_tournament_ids"]],
            "provenance": [item.to_dict() for item in arguments["provenance"]],
            "signed_manifest_body_digest": arguments["signed_manifest_body_digest"],
            "signer_key_id": arguments["signer_key_id"],
        }

    def _content_value(self) -> dict[str, Any]:
        return self._content_value_from_arguments(
            evidence_digest=self.evidence_digest,
            historical_cutoff_key=self.historical_cutoff_key,
            tournament_epoch_id=self.tournament_epoch_id,
            tournament_epoch_content_digest=self.tournament_epoch_content_digest,
            cutoff_at_utc=self.cutoff_at_utc,
            active_tournament_id=self.active_tournament_id,
            authoritative_tournament_ids=self.authoritative_tournament_ids,
            legacy_tournament_ids=self.legacy_tournament_ids,
            provenance=self.provenance,
            signed_manifest_body_digest=self.signed_manifest_body_digest,
            signer_key_id=self.signer_key_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {**self._content_value(), "receipt_digest": self.receipt_digest}


@dataclass(frozen=True, slots=True, order=True)
class FormulaObservationFacts:
    """Non-numeric provenance facts required by the published Formula bootstrap."""

    evidence_id: StableIdentifier
    age_days: str
    quality: EvidenceQuality
    tournament: TournamentRelevance
    governor_receipt_digest: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.evidence_id, StableIdentifier)
            or self.evidence_id.namespace != "evidence"
        ):
            raise ValueError("formula observation facts require an evidence identifier")
        if canonical_decimal_string(self.age_days) != self.age_days or self.age_days.startswith(
            "-"
        ):
            raise ValueError("formula age_days must be a nonnegative canonical decimal")
        if not isinstance(self.quality, EvidenceQuality):
            raise ValueError("formula quality must be an EvidenceQuality")
        if not isinstance(self.tournament, TournamentRelevance):
            raise ValueError("formula tournament must be a TournamentRelevance")
        _require_digest(self.governor_receipt_digest, "formula fact governor_receipt_digest")

    def to_dict(self) -> dict[str, str]:
        return {
            "evidence_id": str(self.evidence_id),
            "age_days": self.age_days,
            "quality": self.quality.value,
            "tournament": self.tournament.value,
            "governor_receipt_digest": self.governor_receipt_digest,
        }


@dataclass(frozen=True, slots=True, init=False)
class FormulaInputPacket:
    """One frozen assessor projection: canonical evidence plus explicit provenance."""

    evidence: EvidencePacket
    governor_receipt: FormulaGovernorReceipt
    observation_facts: tuple[FormulaObservationFacts, ...]

    def __init__(self, *_arguments: object, **_keywords: object) -> None:
        raise ValueError("FormulaInputPacket is created only by a verified governor projection")

    @classmethod
    def _from_verified_projection(
        cls,
        evidence: EvidencePacket,
        governor_receipt: FormulaGovernorReceipt,
        observation_facts: tuple[FormulaObservationFacts, ...],
        *,
        _verification: object,
    ) -> FormulaInputPacket:
        if _verification is not _VERIFIED_GOVERNOR_PROJECTION:
            raise ValueError("FormulaInputPacket requires verified signed authority")
        packet = object.__new__(cls)
        object.__setattr__(packet, "evidence", evidence)
        object.__setattr__(packet, "governor_receipt", governor_receipt)
        object.__setattr__(packet, "observation_facts", observation_facts)
        packet.__post_init__()
        return packet

    def __post_init__(self) -> None:
        if not isinstance(self.evidence, EvidencePacket):
            raise ValueError("formula input requires an EvidencePacket")
        if not isinstance(self.governor_receipt, FormulaGovernorReceipt):
            raise ValueError("formula input requires a FormulaGovernorReceipt")
        if not isinstance(self.observation_facts, tuple) or not all(
            isinstance(item, FormulaObservationFacts) for item in self.observation_facts
        ):
            raise ValueError("formula observation facts must be an immutable typed tuple")
        expected = tuple(item.evidence_id for item in self.evidence.observations)
        actual = tuple(item.evidence_id for item in self.observation_facts)
        if actual != expected:
            raise ValueError(
                "formula observation facts must exactly cover packet observations in order"
            )
        derived = _derive_formula_facts(self.evidence, self.governor_receipt)
        if self.observation_facts != derived:
            raise ValueError("formula observation facts must be governor-derived and receipt-bound")

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-formula-input-v1",
            "evidence": self.evidence.to_dict(),
            "governor_receipt": self.governor_receipt.to_dict(),
            "observation_facts": [item.to_dict() for item in self.observation_facts],
        }


def _project_verified_formula_input(
    *,
    evidence: EvidencePacket,
    tournament_epoch_content_digest: str,
    cutoff_at_utc: str,
    active_tournament_id: StableIdentifier,
    authoritative_tournament_ids: tuple[StableIdentifier, ...],
    legacy_tournament_ids: tuple[StableIdentifier, ...],
    provenance: tuple[FormulaObservationProvenance, ...],
    signed_manifest_body_digest: str,
    signer_key_id: str,
) -> FormulaInputPacket:
    """Internal handoff used only by the signature-verifying application factory."""

    governor_receipt = FormulaGovernorReceipt._from_verified_projection(
        evidence=evidence,
        tournament_epoch_content_digest=tournament_epoch_content_digest,
        cutoff_at_utc=cutoff_at_utc,
        active_tournament_id=active_tournament_id,
        authoritative_tournament_ids=authoritative_tournament_ids,
        legacy_tournament_ids=legacy_tournament_ids,
        provenance=provenance,
        signed_manifest_body_digest=signed_manifest_body_digest,
        signer_key_id=signer_key_id,
        _verification=_VERIFIED_GOVERNOR_PROJECTION,
    )
    facts = _derive_formula_facts(evidence, governor_receipt)
    return FormulaInputPacket._from_verified_projection(
        evidence,
        governor_receipt,
        facts,
        _verification=_VERIFIED_GOVERNOR_PROJECTION,
    )


def _derive_formula_facts(
    evidence: EvidencePacket, receipt: FormulaGovernorReceipt
) -> tuple[FormulaObservationFacts, ...]:
    if receipt.evidence_digest != evidence.content_digest:
        raise ValueError("formula governor receipt does not bind the evidence packet")
    if receipt.historical_cutoff_key != evidence.historical_cutoff_key:
        raise ValueError("formula governor receipt does not bind the historical cutoff")
    if receipt.tournament_epoch_id != evidence.tournament_epoch_id:
        raise ValueError("formula governor receipt does not bind the tournament epoch")
    expected_ids = tuple(item.evidence_id for item in evidence.observations)
    provenance_ids = tuple(item.evidence_id for item in receipt.provenance)
    if provenance_ids != expected_ids:
        raise ValueError("formula governor provenance must exactly cover evidence in order")
    cutoff = _parse_utc(receipt.cutoff_at_utc)
    facts = []
    for observation, provenance in zip(evidence.observations, receipt.provenance):
        if provenance.authority_digest != observation.source_digest:
            raise ValueError("formula provenance does not bind the canonical observation source")
        numeric_eligible = admit_raw_completion(observation.result) is not None
        expected_reason = (
            AdmissionReason.ELIGIBLE_COMPLETION
            if provenance.source is EvidenceSource.LIVE_ISSUED_RACE and numeric_eligible
            else AdmissionReason.HISTORICAL_CUTOVER
            if provenance.source is EvidenceSource.HISTORICAL_IMPORT and numeric_eligible
            else AdmissionReason.STATUS_INELIGIBLE
        )
        if (
            provenance.numeric_eligible != numeric_eligible
            or provenance.admission_reason is not expected_reason
        ):
            raise ValueError("formula provenance does not match canonical admission classification")
        occurred = _parse_utc(observation.occurred_at_utc)
        if occurred > cutoff:
            raise ValueError("formula evidence occurred after the sealed cutoff")
        elapsed = cutoff - occurred
        total_seconds = Decimal(elapsed.days * 86400 + elapsed.seconds) + Decimal(
            elapsed.microseconds
        ) / Decimal(1_000_000)
        quality = (
            EvidenceQuality.ISSUED_OFFICIAL
            if provenance.source is EvidenceSource.LIVE_ISSUED_RACE
            else EvidenceQuality.VERIFIED_HISTORICAL
        )
        if observation.tournament_id == receipt.active_tournament_id:
            tournament = TournamentRelevance.ACTIVE
        elif observation.tournament_id in receipt.authoritative_tournament_ids:
            tournament = TournamentRelevance.OTHER_AUTHORITATIVE
        elif observation.tournament_id in receipt.legacy_tournament_ids:
            tournament = TournamentRelevance.LEGACY
        else:
            raise ValueError("formula evidence tournament has no governor authority classification")
        facts.append(
            FormulaObservationFacts(
                observation.evidence_id,
                canonical_decimal_string(total_seconds / Decimal(86400)),
                quality,
                tournament,
                receipt.receipt_digest,
            )
        )
    return tuple(facts)


def _parse_utc(value: str) -> datetime:
    require_utc_milliseconds(value)
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class ArithmeticTraceRow:
    """One printed, canonical arithmetic step in an assessor's worked trace."""

    stage: str
    label: str
    value: str
    unit: str
    terms: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        for item, label in ((self.stage, "stage"), (self.label, "label"), (self.unit, "unit")):
            if not isinstance(item, str) or not item:
                raise ValueError(f"trace {label} must be a nonempty string")
        if not isinstance(self.value, str) or canonical_decimal_string(self.value) != self.value:
            raise ValueError("trace value must be a canonical decimal string")
        if not isinstance(self.terms, tuple) or any(
            not isinstance(term, tuple)
            or len(term) != 2
            or not all(isinstance(value, str) and value for value in term)
            for term in self.terms
        ):
            raise ValueError("trace terms must be immutable nonempty string pairs")
        keys = tuple(key for key, _ in self.terms)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("trace term keys must be unique and sorted")

    @property
    def details(self) -> dict[str, str]:
        return dict(self.terms)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "label": self.label,
            "value": self.value,
            "unit": self.unit,
            "details": self.details,
        }


@dataclass(frozen=True, slots=True)
class AssessmentResult:
    """Committed assessor forecast plus its complete deterministic explanation."""

    forecast: AssessorForecast
    review: ReviewClassification
    center_ms: int
    uncertainty_ms: int
    log_center: str
    log_scale: str
    effective_sample_size: str
    personal_weight: str
    manifest_digest: str
    trace: tuple[ArithmeticTraceRow, ...]
    assessment_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.forecast, AssessorForecast):
            raise ValueError("assessment forecast must be an AssessorForecast")
        if not isinstance(self.review, ReviewClassification):
            raise ValueError("assessment review must be a ReviewClassification")
        if (
            isinstance(self.center_ms, bool)
            or not isinstance(self.center_ms, int)
            or self.center_ms <= 0
        ):
            raise ValueError("assessment center_ms must be a positive integer")
        if (
            isinstance(self.uncertainty_ms, bool)
            or not isinstance(self.uncertainty_ms, int)
            or self.uncertainty_ms <= 0
        ):
            raise ValueError("assessment uncertainty_ms must be a positive integer")
        for value, label in (
            (self.log_center, "log_center"),
            (self.log_scale, "log_scale"),
            (self.effective_sample_size, "effective_sample_size"),
        ):
            if canonical_decimal_string(value) != value:
                raise ValueError(f"assessment {label} must be canonical")
        if self.log_scale.startswith("-") or self.effective_sample_size.startswith("-"):
            raise ValueError("assessment scale and effective sample size must be nonnegative")
        if canonical_decimal_string(self.personal_weight) != self.personal_weight:
            raise ValueError("assessment personal_weight must be canonical")
        _require_digest(self.manifest_digest, "manifest_digest")
        if (
            not isinstance(self.trace, tuple)
            or not self.trace
            or not all(isinstance(row, ArithmeticTraceRow) for row in self.trace)
        ):
            raise ValueError("assessment trace must be a nonempty immutable trace")
        _require_digest(self.assessment_digest, "assessment_digest")
        if self.recompute_digest() != self.assessment_digest:
            raise ValueError("assessment digest mismatch")

    @classmethod
    def create(cls, **arguments: Any) -> AssessmentResult:
        content = cls._content_value(**arguments)
        return cls(**arguments, assessment_digest=canonical_digest(content))

    @staticmethod
    def _content_value(**arguments: Any) -> dict[str, Any]:
        forecast = arguments["forecast"]
        review = arguments["review"]
        trace = arguments["trace"]
        return {
            "schema_version": "strathmark-v3-assessment-result-v1",
            "forecast": forecast.to_dict(),
            "review": review.value,
            "center_ms": arguments["center_ms"],
            "uncertainty_ms": arguments["uncertainty_ms"],
            "log_center": arguments["log_center"],
            "log_scale": arguments["log_scale"],
            "effective_sample_size": arguments["effective_sample_size"],
            "personal_weight": arguments["personal_weight"],
            "manifest_digest": arguments["manifest_digest"],
            "trace": [row.to_dict() for row in trace],
        }

    def recompute_digest(self) -> str:
        return canonical_digest(
            self._content_value(
                forecast=self.forecast,
                review=self.review,
                center_ms=self.center_ms,
                uncertainty_ms=self.uncertainty_ms,
                log_center=self.log_center,
                log_scale=self.log_scale,
                effective_sample_size=self.effective_sample_size,
                personal_weight=self.personal_weight,
                manifest_digest=self.manifest_digest,
                trace=self.trace,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._content_value(
                forecast=self.forecast,
                review=self.review,
                center_ms=self.center_ms,
                uncertainty_ms=self.uncertainty_ms,
                log_center=self.log_center,
                log_scale=self.log_scale,
                effective_sample_size=self.effective_sample_size,
                personal_weight=self.personal_weight,
                manifest_digest=self.manifest_digest,
                trace=self.trace,
            ),
            "assessment_digest": self.assessment_digest,
        }


class Assessor(Protocol):
    """Narrow blind assessor port: one sealed packet in, one committed result out."""

    def assess(self, packet: FormulaInputPacket) -> AssessmentResult: ...


__all__ = [
    "ArithmeticTraceRow",
    "AssessmentResult",
    "Assessor",
    "EvidenceOrigin",
    "EvidenceQuality",
    "FormulaGovernorReceipt",
    "FormulaInputPacket",
    "FormulaObservationFacts",
    "FormulaObservationProvenance",
    "ReviewClassification",
    "TournamentRelevance",
]
