"""Signed evidence-governor composition boundary for the Formula assessor."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from strathmark.v3.assessors.base import (
    FormulaInputPacket,
    FormulaObservationProvenance,
    _project_verified_formula_input,
)
from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.contracts.evidence import (
    EvidencePacket,
    _require_digest,
    require_utc_milliseconds,
)
from strathmark.v3.contracts.identifiers import (
    StableIdentifier,
    deterministic_identifier,
    require_identifier,
)
from strathmark.v3.contracts.statuses import admit_raw_completion
from strathmark.v3.domain.epochs import EvidenceEpoch
from strathmark.v3.domain.evidence import (
    AdmissionReason,
    AdmittedEvidence,
    EvidenceSource,
    IssuedFieldFact,
    admit_observation,
)
from strathmark.v3.infrastructure.integrity import (
    IntegrityError,
    IntegrityTrustStore,
    P256Signer,
    SignedManifest,
    apply_key_rotation,
    sign_manifest,
    verify_manifest,
)

FORMULA_GOVERNOR_MANIFEST_KIND = "formula_governor_batch"


class FormulaGovernorError(ValueError):
    """A Formula governor batch is unsigned, untrusted, or causally inconsistent."""


@dataclass(frozen=True, slots=True)
class FormulaLiveAuthority:
    """Raw issued-field facts from which the governor must derive live admission."""

    evidence_id: StableIdentifier
    issued_field: IssuedFieldFact
    field_revision: int
    claimed_receipt_id: StableIdentifier

    def __post_init__(self) -> None:
        require_identifier(self.evidence_id, expected_namespace="evidence")
        if not isinstance(self.issued_field, IssuedFieldFact):
            raise FormulaGovernorError("live Formula authority requires an IssuedFieldFact")
        if (
            isinstance(self.field_revision, bool)
            or not isinstance(self.field_revision, int)
            or self.field_revision <= 0
        ):
            raise FormulaGovernorError("live Formula field revision must be positive")
        require_identifier(self.claimed_receipt_id, expected_namespace="receipt")


@dataclass(frozen=True, slots=True)
class HistoricalCutoverAuthority:
    """Historical cutover fact that becomes authoritative only inside the signed batch."""

    cutover_id: StableIdentifier
    historical_cutoff_key: str
    authority_digest: str

    def __post_init__(self) -> None:
        require_identifier(self.cutover_id, expected_namespace="historical_cutover")
        require_identifier(self.historical_cutoff_key, expected_namespace="history")
        _require_digest(self.authority_digest, "historical cutover authority digest")


@dataclass(frozen=True, slots=True)
class FormulaHistoricalAuthority:
    """Raw historical membership plus a cutover authority to be governor-sealed."""

    evidence_id: StableIdentifier
    result_key: StableIdentifier
    cutover: HistoricalCutoverAuthority

    def __post_init__(self) -> None:
        require_identifier(self.evidence_id, expected_namespace="evidence")
        require_identifier(self.result_key, expected_namespace="result")
        if not isinstance(self.cutover, HistoricalCutoverAuthority):
            raise FormulaGovernorError("historical Formula authority requires a cutover authority")


@dataclass(frozen=True, slots=True)
class SealedFormulaGovernorBatch:
    """One externally signed Formula evidence-governor batch."""

    manifest: SignedManifest

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, SignedManifest):
            raise FormulaGovernorError("Formula governor seal requires a SignedManifest")
        if self.manifest.kind != FORMULA_GOVERNOR_MANIFEST_KIND:
            raise FormulaGovernorError("Formula governor seal has the wrong manifest kind")

    def to_dict(self) -> dict[str, str]:
        return self.manifest.to_dict()


def seal_formula_governor_batch(
    *,
    evidence: EvidencePacket,
    epoch: EvidenceEpoch,
    cutoff_at_utc: str,
    active_tournament_id: StableIdentifier,
    authoritative_tournament_ids: tuple[StableIdentifier, ...],
    legacy_tournament_ids: tuple[StableIdentifier, ...],
    live_authorities: tuple[FormulaLiveAuthority, ...],
    historical_authorities: tuple[FormulaHistoricalAuthority, ...],
    signer: P256Signer,
    created_at: str,
) -> SealedFormulaGovernorBatch:
    """Derive every admission internally, bind the epoch, and sign the exact batch."""

    _validate_batch_scope(
        evidence,
        epoch,
        cutoff_at_utc,
        active_tournament_id,
        authoritative_tournament_ids,
        legacy_tournament_ids,
    )
    if not isinstance(live_authorities, tuple) or not all(
        isinstance(item, FormulaLiveAuthority) for item in live_authorities
    ):
        raise FormulaGovernorError("live Formula authorities must be an immutable typed tuple")
    if not isinstance(historical_authorities, tuple) or not all(
        isinstance(item, FormulaHistoricalAuthority) for item in historical_authorities
    ):
        raise FormulaGovernorError(
            "historical Formula authorities must be an immutable typed tuple"
        )
    authorities: dict[StableIdentifier, FormulaLiveAuthority | FormulaHistoricalAuthority] = {}
    for authority in (*live_authorities, *historical_authorities):
        if authority.evidence_id in authorities:
            raise FormulaGovernorError("Formula governor authority cannot repeat evidence")
        authorities[authority.evidence_id] = authority
    expected_ids = tuple(item.evidence_id for item in evidence.observations)
    if set(authorities) != set(expected_ids):
        raise FormulaGovernorError("Formula governor authorities must exactly cover evidence")
    members = {item.result_key: item for item in epoch.members}
    admissions = []
    for observation in evidence.observations:
        authority = authorities[observation.evidence_id]
        if isinstance(authority, FormulaLiveAuthority):
            admitted = admit_observation(
                observation,
                issued_field=authority.issued_field,
                field_revision=authority.field_revision,
                claimed_receipt_id=authority.claimed_receipt_id,
            )
            if admitted.reason not in {
                AdmissionReason.ELIGIBLE_COMPLETION,
                AdmissionReason.STATUS_INELIGIBLE,
            }:
                raise FormulaGovernorError(
                    f"live Formula evidence was not authoritatively issued: {admitted.reason.value}"
                )
            result_key = deterministic_identifier(
                "result",
                {
                    "field_id": str(observation.field_id),
                    "field_revision": authority.field_revision,
                    "competitor_id": str(observation.competitor_id),
                },
            )
            authority_reference = _live_authority_value(authority)
        else:
            if authority.cutover.historical_cutoff_key != evidence.historical_cutoff_key:
                raise FormulaGovernorError(
                    "historical Formula cutover does not match the packet cutoff"
                )
            completion = admit_raw_completion(observation.result)
            admitted = AdmittedEvidence(
                observation,
                EvidenceSource.HISTORICAL_IMPORT,
                completion is not None,
                None if completion is None else completion.raw_time_ms,
                AdmissionReason.HISTORICAL_CUTOVER
                if completion is not None
                else AdmissionReason.STATUS_INELIGIBLE,
            )
            result_key = authority.result_key
            authority_reference = _historical_authority_value(authority)
        _require_epoch_membership(observation, admitted, result_key, members)
        admissions.append(_admission_value(observation, admitted, result_key, authority_reference))
    payload = {
        "schema_version": "strathmark-v3-formula-governor-batch-v1",
        "evidence_packet_digest": evidence.content_digest,
        "historical_cutoff_key": evidence.historical_cutoff_key,
        "cutoff_at_utc": cutoff_at_utc,
        "active_tournament_id": str(active_tournament_id),
        "authoritative_tournament_ids": [str(item) for item in authoritative_tournament_ids],
        "legacy_tournament_ids": [str(item) for item in legacy_tournament_ids],
        "epoch": _epoch_value(epoch),
        "admissions": admissions,
    }
    return SealedFormulaGovernorBatch(
        sign_manifest(
            FORMULA_GOVERNOR_MANIFEST_KIND,
            payload,
            signer=signer,
            created_at=created_at,
        )
    )


class FormulaProjectionFactory:
    """Composition-owned verifier pinned to one trust and tournament authority scope."""

    def __init__(
        self,
        *,
        trust_store: IntegrityTrustStore,
        cutoff_at_utc: str,
        active_tournament_id: StableIdentifier,
        authoritative_tournament_ids: tuple[StableIdentifier, ...],
        legacy_tournament_ids: tuple[StableIdentifier, ...],
    ) -> None:
        if not isinstance(trust_store, IntegrityTrustStore):
            raise FormulaGovernorError("Formula projection factory requires a pinned trust store")
        _validate_authority_scope(
            cutoff_at_utc,
            active_tournament_id,
            authoritative_tournament_ids,
            legacy_tournament_ids,
        )
        self._trust_store = trust_store
        self._cutoff_at_utc = cutoff_at_utc
        self._active_tournament_id = active_tournament_id
        self._authoritative_tournament_ids = authoritative_tournament_ids
        self._legacy_tournament_ids = legacy_tournament_ids

    def with_rotation(self, rotation: SignedManifest) -> FormulaProjectionFactory:
        """Return a factory that trusts both the retained and newly rotated key."""

        try:
            trust_store = apply_key_rotation(self._trust_store, rotation)
        except IntegrityError as exc:
            raise FormulaGovernorError("Formula governor key rotation is not trusted") from exc
        return FormulaProjectionFactory(
            trust_store=trust_store,
            cutoff_at_utc=self._cutoff_at_utc,
            active_tournament_id=self._active_tournament_id,
            authoritative_tournament_ids=self._authoritative_tournament_ids,
            legacy_tournament_ids=self._legacy_tournament_ids,
        )

    def project(
        self,
        *,
        evidence: EvidencePacket,
        epoch: EvidenceEpoch,
        sealed_batch: SealedFormulaGovernorBatch,
    ) -> FormulaInputPacket:
        if not isinstance(sealed_batch, SealedFormulaGovernorBatch):
            raise FormulaGovernorError("Formula projection requires a sealed governor batch")
        try:
            payload = verify_manifest(sealed_batch.manifest, self._trust_store)
        except IntegrityError as exc:
            raise FormulaGovernorError(
                "Formula governor signature is invalid or untrusted"
            ) from exc
        _require_fields(
            payload,
            {
                "schema_version",
                "evidence_packet_digest",
                "historical_cutoff_key",
                "cutoff_at_utc",
                "active_tournament_id",
                "authoritative_tournament_ids",
                "legacy_tournament_ids",
                "epoch",
                "admissions",
            },
        )
        if payload["schema_version"] != "strathmark-v3-formula-governor-batch-v1":
            raise FormulaGovernorError("Formula governor payload version is unsupported")
        _validate_batch_scope(
            evidence,
            epoch,
            self._cutoff_at_utc,
            self._active_tournament_id,
            self._authoritative_tournament_ids,
            self._legacy_tournament_ids,
        )
        expected_scope = {
            "evidence_packet_digest": evidence.content_digest,
            "historical_cutoff_key": evidence.historical_cutoff_key,
            "cutoff_at_utc": self._cutoff_at_utc,
            "active_tournament_id": str(self._active_tournament_id),
            "authoritative_tournament_ids": [
                str(item) for item in self._authoritative_tournament_ids
            ],
            "legacy_tournament_ids": [str(item) for item in self._legacy_tournament_ids],
            "epoch": _epoch_value(epoch),
        }
        for key, expected in expected_scope.items():
            if payload[key] != expected:
                raise FormulaGovernorError(f"Formula governor signed {key} binding differs")
        admissions = payload["admissions"]
        if not isinstance(admissions, list) or len(admissions) != len(evidence.observations):
            raise FormulaGovernorError(
                "Formula governor admissions must exactly cover packet observations"
            )
        members = {item.result_key: item for item in epoch.members}
        provenance = []
        for observation, value in zip(evidence.observations, admissions):
            provenance.append(
                _verify_admission_value(
                    observation,
                    value,
                    members,
                    historical_cutoff_key=evidence.historical_cutoff_key,
                )
            )
        return _project_verified_formula_input(
            evidence=evidence,
            tournament_epoch_content_digest=epoch.content_digest,
            cutoff_at_utc=self._cutoff_at_utc,
            active_tournament_id=self._active_tournament_id,
            authoritative_tournament_ids=self._authoritative_tournament_ids,
            legacy_tournament_ids=self._legacy_tournament_ids,
            provenance=tuple(provenance),
            signed_manifest_body_digest=sealed_batch.manifest.body_digest,
            signer_key_id=sealed_batch.manifest.key_id,
        )


def _validate_batch_scope(
    evidence: EvidencePacket,
    epoch: EvidenceEpoch,
    cutoff_at_utc: str,
    active_tournament_id: StableIdentifier,
    authoritative_tournament_ids: tuple[StableIdentifier, ...],
    legacy_tournament_ids: tuple[StableIdentifier, ...],
) -> None:
    if not isinstance(evidence, EvidencePacket) or not isinstance(epoch, EvidenceEpoch):
        raise FormulaGovernorError("Formula governor requires typed evidence and epoch")
    _validate_authority_scope(
        cutoff_at_utc,
        active_tournament_id,
        authoritative_tournament_ids,
        legacy_tournament_ids,
    )
    if evidence.tournament_epoch_id != epoch.epoch_id:
        raise FormulaGovernorError("Formula evidence packet does not bind the exact epoch identity")
    if evidence.historical_cutoff_key != epoch.historical_cutoff_key:
        raise FormulaGovernorError("Formula evidence and epoch cutoff keys differ")
    if evidence.tournament_event_sequence != epoch.maximum_tournament_sequence:
        raise FormulaGovernorError("Formula evidence and epoch maximum sequences differ")


def _validate_authority_scope(
    cutoff_at_utc: str,
    active_tournament_id: StableIdentifier,
    authoritative_tournament_ids: tuple[StableIdentifier, ...],
    legacy_tournament_ids: tuple[StableIdentifier, ...],
) -> None:
    require_utc_milliseconds(cutoff_at_utc)
    require_identifier(active_tournament_id, expected_namespace="tournament")
    for values, label in (
        (authoritative_tournament_ids, "authoritative"),
        (legacy_tournament_ids, "legacy"),
    ):
        if not isinstance(values, tuple):
            raise FormulaGovernorError(f"Formula {label} tournaments must be immutable")
        for value in values:
            require_identifier(value, expected_namespace="tournament")
        if values != tuple(sorted(values)) or len(values) != len(set(values)):
            raise FormulaGovernorError(f"Formula {label} tournaments must be unique and sorted")
    combined = (active_tournament_id, *authoritative_tournament_ids, *legacy_tournament_ids)
    if len(combined) != len(set(combined)):
        raise FormulaGovernorError("Formula tournament authority sets must be disjoint")


def _require_epoch_membership(
    observation: Any,
    admitted: AdmittedEvidence,
    result_key: StableIdentifier,
    members: Mapping[str, Any],
) -> None:
    member = members.get(str(result_key))
    if member is None:
        raise FormulaGovernorError("Formula evidence result is absent from the signed epoch")
    if (
        member.revision != observation.result.revision
        or member.source_sequence != observation.observation_sequence
        or member.numeric_eligible != admitted.numeric_eligible
    ):
        raise FormulaGovernorError("Formula evidence differs from its signed epoch member")


def _issued_field_value(value: IssuedFieldFact) -> dict[str, object]:
    return {
        "field_id": str(value.field_id),
        "upstream_revision": value.upstream_revision,
        "competitor_ids": [str(item) for item in value.competitor_ids],
        "receipt_id": str(value.receipt_id),
        "tournament_id": str(value.tournament_id),
        "round_id": str(value.round_id),
        "context": value.context.to_dict(),
        "issued_marks": [[str(competitor), mark] for competitor, mark in value.issued_marks],
    }


def _live_authority_value(value: FormulaLiveAuthority) -> dict[str, object]:
    content = {
        "kind": "live_issued_field",
        "evidence_id": str(value.evidence_id),
        "field_revision": value.field_revision,
        "claimed_receipt_id": str(value.claimed_receipt_id),
        "issued_field": _issued_field_value(value.issued_field),
    }
    return {**content, "authority_digest": canonical_digest(content)}


def _historical_authority_value(value: FormulaHistoricalAuthority) -> dict[str, object]:
    return {
        "kind": "historical_cutover",
        "evidence_id": str(value.evidence_id),
        "result_key": str(value.result_key),
        "cutover_id": str(value.cutover.cutover_id),
        "historical_cutoff_key": value.cutover.historical_cutoff_key,
        "authority_digest": value.cutover.authority_digest,
    }


def _admission_value(
    observation: Any,
    admitted: AdmittedEvidence,
    result_key: StableIdentifier,
    authority_reference: dict[str, object],
) -> dict[str, object]:
    return {
        "evidence_id": str(observation.evidence_id),
        "result_key": str(result_key),
        "observation_sequence": observation.observation_sequence,
        "result_revision": observation.result.revision,
        "source": admitted.source.value,
        "reason": admitted.reason.value,
        "numeric_eligible": admitted.numeric_eligible,
        "raw_time_ms": admitted.raw_time_ms,
        "source_digest": observation.source_digest,
        "authority_reference": authority_reference,
    }


def _epoch_value(epoch: EvidenceEpoch) -> dict[str, object]:
    return {
        "epoch_id": str(epoch.epoch_id),
        "content_digest": epoch.content_digest,
        "content": epoch.content_value(),
    }


def _verify_admission_value(
    observation: Any,
    value: object,
    members: Mapping[str, Any],
    *,
    historical_cutoff_key: str,
) -> FormulaObservationProvenance:
    _require_fields(
        value,
        {
            "evidence_id",
            "result_key",
            "observation_sequence",
            "result_revision",
            "source",
            "reason",
            "numeric_eligible",
            "raw_time_ms",
            "source_digest",
            "authority_reference",
        },
    )
    if (
        value["evidence_id"] != str(observation.evidence_id)
        or value["observation_sequence"] != observation.observation_sequence
        or value["result_revision"] != observation.result.revision
        or value["source_digest"] != observation.source_digest
    ):
        raise FormulaGovernorError("Formula admission does not bind the exact observation")
    try:
        source = EvidenceSource(value["source"])
        reason = AdmissionReason(value["reason"])
        result_key = require_identifier(value["result_key"], expected_namespace="result")
    except (TypeError, ValueError) as exc:
        raise FormulaGovernorError("Formula admission uses an unknown closed value") from exc
    completion = admit_raw_completion(observation.result)
    numeric_eligible = completion is not None
    raw_time_ms = None if completion is None else completion.raw_time_ms
    expected_reason = (
        AdmissionReason.ELIGIBLE_COMPLETION
        if source is EvidenceSource.LIVE_ISSUED_RACE and numeric_eligible
        else AdmissionReason.HISTORICAL_CUTOVER
        if source is EvidenceSource.HISTORICAL_IMPORT and numeric_eligible
        else AdmissionReason.STATUS_INELIGIBLE
    )
    if (
        value["numeric_eligible"] is not numeric_eligible
        or value["raw_time_ms"] != raw_time_ms
        or reason is not expected_reason
    ):
        raise FormulaGovernorError("Formula admission outcome was forged")
    authority = value["authority_reference"]
    if not isinstance(authority, Mapping):
        raise FormulaGovernorError("Formula admission authority reference is malformed")
    if source is EvidenceSource.LIVE_ISSUED_RACE:
        _verify_live_authority_reference(observation, result_key, authority)
    else:
        _verify_historical_authority_reference(
            result_key, authority, historical_cutoff_key=historical_cutoff_key
        )
    admitted = AdmittedEvidence(observation, source, numeric_eligible, raw_time_ms, reason)
    _require_epoch_membership(observation, admitted, result_key, members)
    return FormulaObservationProvenance(
        observation.evidence_id,
        source,
        reason,
        numeric_eligible,
        observation.source_digest,
    )


def _verify_live_authority_reference(
    observation: Any, result_key: StableIdentifier, authority: Mapping[str, Any]
) -> None:
    _require_fields(
        authority,
        {
            "kind",
            "evidence_id",
            "field_revision",
            "claimed_receipt_id",
            "issued_field",
            "authority_digest",
        },
    )
    content = {key: authority[key] for key in authority if key != "authority_digest"}
    if (
        authority["kind"] != "live_issued_field"
        or authority["evidence_id"] != str(observation.evidence_id)
        or authority["authority_digest"] != canonical_digest(content)
    ):
        raise FormulaGovernorError("Formula live authority reference was forged")
    field_revision = authority["field_revision"]
    expected_key = deterministic_identifier(
        "result",
        {
            "field_id": str(observation.field_id),
            "field_revision": field_revision,
            "competitor_id": str(observation.competitor_id),
        },
    )
    if expected_key != result_key:
        raise FormulaGovernorError("Formula live authority result identity differs")


def _verify_historical_authority_reference(
    result_key: StableIdentifier,
    authority: Mapping[str, Any],
    *,
    historical_cutoff_key: str,
) -> None:
    _require_fields(
        authority,
        {
            "kind",
            "evidence_id",
            "result_key",
            "cutover_id",
            "historical_cutoff_key",
            "authority_digest",
        },
    )
    if authority["kind"] != "historical_cutover" or authority["result_key"] != str(result_key):
        raise FormulaGovernorError("Formula historical authority reference was forged")
    require_identifier(authority["cutover_id"], expected_namespace="historical_cutover")
    require_identifier(authority["historical_cutoff_key"], expected_namespace="history")
    if authority["historical_cutoff_key"] != historical_cutoff_key:
        raise FormulaGovernorError("Formula historical cutoff authority differs")
    _require_digest(authority["authority_digest"], "historical authority digest")


def _require_fields(value: object, expected: set[str]) -> None:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise FormulaGovernorError("Formula governor object has unknown or missing fields")


__all__ = [
    "FORMULA_GOVERNOR_MANIFEST_KIND",
    "FormulaGovernorError",
    "FormulaHistoricalAuthority",
    "FormulaLiveAuthority",
    "FormulaProjectionFactory",
    "HistoricalCutoverAuthority",
    "SealedFormulaGovernorBatch",
    "seal_formula_governor_batch",
]
