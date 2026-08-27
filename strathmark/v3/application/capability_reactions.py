"""Signed, append-only capability reactions over the V3 event authority."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Protocol

from strathmark.v3.application.commands import CommandRequest, EventIntent
from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.contracts.commands import CommandEnvelope, CommandKind, InlinePayload
from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.events import AggregateKind, EventEnvelope, EventKind
from strathmark.v3.contracts.evidence import require_utc_milliseconds
from strathmark.v3.contracts.forecasts import PositiveTimeDistribution
from strathmark.v3.contracts.identifiers import (
    IdempotencyKey,
    StableIdentifier,
    deterministic_identifier,
    require_identifier,
)
from strathmark.v3.domain.capability import (
    CapabilityCapacityEnvelope,
    CapabilityEvidence,
    CapabilityPrior,
    CapabilityState,
    HistoricalImportBinding,
    RebaseCapacityDecision,
    evaluate_rebase_capacity,
    replay_capability,
)
from strathmark.v3.domain.epochs import MandatoryReaction
from strathmark.v3.domain.evidence import AdmittedEvidence, EvidenceSource
from strathmark.v3.infrastructure.integrity import (
    IntegrityError,
    IntegrityTrustStore,
    P256Signer,
    SignedManifest,
    sign_manifest,
    verify_manifest,
)
from strathmark.v3.infrastructure.sqlite.connection import immediate_transaction, open_v3_connection
from strathmark.v3.infrastructure.sqlite.event_store import (
    EventStoreConflict,
    SQLiteEventStore,
    StoredCommandResult,
)

CAPABILITY_ADMISSION_MANIFEST_KIND = "capability_admission"
CAPABILITY_CAPACITY_MANIFEST_KIND = "capability_capacity"
HISTORICAL_CUTOVER_MANIFEST_KIND = "historical_cutover"
CAPABILITY_REACTION_SCHEMA_VERSION = "strathmark-v3-capability-reaction-v1"


class CapabilityReactionError(ValueError):
    """A capability reaction is unsigned, unsupported, or causally inconsistent."""


@dataclass(frozen=True, slots=True)
class SealedCapabilityAdmission:
    manifest: SignedManifest

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, SignedManifest):
            raise CapabilityReactionError("capability admission requires a signed manifest")
        if self.manifest.kind != CAPABILITY_ADMISSION_MANIFEST_KIND:
            raise CapabilityReactionError("capability admission manifest kind differs")

    def to_dict(self) -> dict[str, str]:
        return self.manifest.to_dict()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SealedCapabilityAdmission:
        try:
            return cls(SignedManifest.from_dict(value))
        except (IntegrityError, ContractError) as exc:
            raise CapabilityReactionError("capability admission manifest is invalid") from exc


@dataclass(frozen=True, slots=True)
class SealedCapabilityCapacity:
    """P-256 signed correction/reaction capacity authority."""

    manifest: SignedManifest

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, SignedManifest):
            raise CapabilityReactionError("capability capacity requires a signed manifest")
        if self.manifest.kind != CAPABILITY_CAPACITY_MANIFEST_KIND:
            raise CapabilityReactionError("capability capacity manifest kind differs")

    def to_dict(self) -> dict[str, str]:
        return self.manifest.to_dict()


def seal_capability_capacity(
    envelope: CapabilityCapacityEnvelope, *, signer: P256Signer, created_at: str
) -> SealedCapabilityCapacity:
    if not isinstance(envelope, CapabilityCapacityEnvelope):
        raise CapabilityReactionError("capacity sealing requires a typed envelope")
    return SealedCapabilityCapacity(
        sign_manifest(
            CAPABILITY_CAPACITY_MANIFEST_KIND,
            {
                "schema_version": "strathmark-v3-capability-capacity-manifest-v1",
                "envelope": envelope.to_dict(),
            },
            signer=signer,
            created_at=created_at,
        )
    )


class CapabilityCapacityVerifier:
    def __init__(self, trust_store: IntegrityTrustStore) -> None:
        if not isinstance(trust_store, IntegrityTrustStore):
            raise CapabilityReactionError("capacity verifier requires pinned integrity trust")
        self._trust_store = trust_store

    def verify(self, sealed: SealedCapabilityCapacity) -> CapabilityCapacityEnvelope:
        if not isinstance(sealed, SealedCapabilityCapacity):
            raise CapabilityReactionError("capacity verification requires a sealed manifest")
        try:
            payload = verify_manifest(sealed.manifest, self._trust_store)
        except IntegrityError as exc:
            raise CapabilityReactionError("capacity signature is invalid or untrusted") from exc
        if (
            set(payload) != {"schema_version", "envelope"}
            or payload.get("schema_version") != "strathmark-v3-capability-capacity-manifest-v1"
            or not isinstance(payload["envelope"], Mapping)
        ):
            raise CapabilityReactionError("capacity manifest payload is not closed")
        try:
            return CapabilityCapacityEnvelope.from_dict(payload["envelope"])
        except ContractError as exc:
            raise CapabilityReactionError("capacity envelope is invalid") from exc


def _signed_manifest_digest(sealed: SealedCapabilityAdmission | SealedCapabilityCapacity) -> str:
    return canonical_digest(sealed.to_dict())


def seal_historical_import_cutover(
    database_path: Path | str,
    import_id: str,
    *,
    signer: P256Signer,
    created_at: str,
) -> SignedManifest:
    """Sign the exact immutable import and complete row membership for cutover."""

    with open_v3_connection(database_path, read_only=True) as connection:
        imported = connection.execute(
            "SELECT source_cutoff, source_catalog_digest, source_tip_digest, imported_row_count, "
            "eligible, cutover_manifest_digest FROM v3_historical_imports WHERE import_id=?",
            (import_id,),
        ).fetchone()
        rows = connection.execute(
            "SELECT row_digest, eligible FROM v3_historical_import_rows "
            "WHERE import_id=? ORDER BY row_digest",
            (import_id,),
        ).fetchall()
    if (
        imported is None
        or int(imported[4]) != 0
        or imported[5] is not None
        or int(imported[3]) != len(rows)
        or any(int(row[1]) != 0 for row in rows)
    ):
        raise CapabilityReactionError("historical import is not an exact uncutover authority")
    return sign_manifest(
        HISTORICAL_CUTOVER_MANIFEST_KIND,
        {
            "schema_version": "strathmark-v3-historical-cutover-v1",
            "import_id": import_id,
            "source_cutoff": str(imported[0]),
            "source_catalog_digest": str(imported[1]),
            "source_tip_digest": str(imported[2]),
            "row_digests": [str(row[0]) for row in rows],
        },
        signer=signer,
        created_at=created_at,
    )


def activate_historical_import_cutover(
    database_path: Path | str,
    manifest: SignedManifest,
    *,
    trust_store: IntegrityTrustStore,
    activated_at: str,
) -> str:
    """Verify and atomically activate one signed, exact historical import manifest."""

    if (
        not isinstance(manifest, SignedManifest)
        or manifest.kind != HISTORICAL_CUTOVER_MANIFEST_KIND
    ):
        raise CapabilityReactionError("historical cutover requires its signed manifest kind")
    try:
        payload = verify_manifest(manifest, trust_store)
        require_utc_milliseconds(activated_at)
    except (IntegrityError, ContractError) as exc:
        raise CapabilityReactionError(
            "historical cutover signature or activation time is invalid"
        ) from exc
    if (
        set(payload)
        != {
            "schema_version",
            "import_id",
            "source_cutoff",
            "source_catalog_digest",
            "source_tip_digest",
            "row_digests",
        }
        or payload.get("schema_version") != "strathmark-v3-historical-cutover-v1"
    ):
        raise CapabilityReactionError("historical cutover payload is not closed")
    digest = canonical_digest(manifest.to_dict())
    with open_v3_connection(database_path) as connection:
        with immediate_transaction(connection):
            imported = connection.execute(
                "SELECT source_cutoff, source_catalog_digest, source_tip_digest, "
                "imported_row_count, eligible, cutover_manifest_digest "
                "FROM v3_historical_imports WHERE import_id=?",
                (payload["import_id"],),
            ).fetchone()
            rows = connection.execute(
                "SELECT row_digest, eligible FROM v3_historical_import_rows "
                "WHERE import_id=? ORDER BY row_digest",
                (payload["import_id"],),
            ).fetchall()
            expected = (
                None
                if imported is None
                else {
                    "source_cutoff": str(imported[0]),
                    "source_catalog_digest": str(imported[1]),
                    "source_tip_digest": str(imported[2]),
                    "row_digests": [str(row[0]) for row in rows],
                }
            )
            observed = None if expected is None else {key: payload[key] for key in expected}
            if (
                imported is None
                or expected != observed
                or int(imported[3]) != len(rows)
                or any(int(row[1]) != 0 for row in rows)
                or int(imported[4]) != 0
                or imported[5] is not None
            ):
                raise CapabilityReactionError(
                    "historical cutover does not bind the exact ineligible import and rows"
                )
            connection.execute(
                "INSERT INTO v3_historical_cutovers VALUES (?, ?, ?, ?)",
                (
                    payload["import_id"],
                    json.dumps(manifest.to_dict(), sort_keys=True, separators=(",", ":")),
                    digest,
                    activated_at,
                ),
            )
            connection.execute("DROP TRIGGER v3_historical_imports_no_update")
            connection.execute("DROP TRIGGER v3_historical_import_rows_no_update")
            connection.execute(
                "UPDATE v3_historical_imports SET eligible=1, cutover_manifest_digest=? "
                "WHERE import_id=?",
                (digest, payload["import_id"]),
            )
            connection.execute(
                "UPDATE v3_historical_import_rows SET eligible=1 WHERE import_id=?",
                (payload["import_id"],),
            )
            connection.execute(
                "CREATE TRIGGER v3_historical_imports_no_update BEFORE UPDATE ON "
                "v3_historical_imports BEGIN SELECT RAISE(ABORT, 'append-only authority'); END"
            )
            connection.execute(
                "CREATE TRIGGER v3_historical_import_rows_no_update BEFORE UPDATE ON "
                "v3_historical_import_rows "
                "BEGIN SELECT RAISE(ABORT, 'append-only authority'); END"
            )
    return digest


def seal_capability_admission(
    *,
    admitted: AdmittedEvidence,
    result_key: StableIdentifier,
    source_global_sequence: int,
    authority_digest: str,
    prior: CapabilityPrior,
    evidence_log_variance: str,
    conversion_log_variance: str,
    effective_weight: str,
    historical_binding: HistoricalImportBinding | None,
    signer: P256Signer,
    created_at: str,
) -> SealedCapabilityAdmission:
    """Seal the evidence governor's exact admission decision and provenance."""

    if not isinstance(admitted, AdmittedEvidence):
        raise CapabilityReactionError("capability sealing requires typed admitted evidence")
    require_identifier(result_key, expected_namespace="result")
    observation = admitted.observation
    evidence = CapabilityEvidence(
        result_key=result_key,
        result_revision=observation.result.revision,
        supersedes_revision=observation.result.supersedes_revision,
        competitor_id=observation.competitor_id,
        context_digest=canonical_digest(observation.context.to_dict()),
        source_global_sequence=source_global_sequence,
        observed_at_utc=observation.occurred_at_utc,
        raw_time_ms=admitted.raw_time_ms,
        source=admitted.source,
        numeric_eligible=admitted.numeric_eligible,
        admission_reason=admitted.reason,
        observation_digest=canonical_digest(observation.to_dict()),
        authority_digest=authority_digest,
        prior=prior,
        evidence_log_variance=evidence_log_variance,
        conversion_log_variance=conversion_log_variance,
        effective_weight=effective_weight,
        historical_binding=historical_binding,
    )
    return SealedCapabilityAdmission(
        sign_manifest(
            CAPABILITY_ADMISSION_MANIFEST_KIND,
            {
                "schema_version": "strathmark-v3-capability-admission-v1",
                "evidence": evidence.to_dict(),
            },
            signer=signer,
            created_at=created_at,
        )
    )


class CapabilityAdmissionVerifier:
    """Composition-owned verifier pinned to the evidence governor trust set."""

    def __init__(self, trust_store: IntegrityTrustStore) -> None:
        if not isinstance(trust_store, IntegrityTrustStore):
            raise CapabilityReactionError("capability verifier requires pinned integrity trust")
        self._trust_store = trust_store

    def verify(self, sealed: SealedCapabilityAdmission) -> CapabilityEvidence:
        if not isinstance(sealed, SealedCapabilityAdmission):
            raise CapabilityReactionError("capability verification requires a sealed admission")
        try:
            payload = verify_manifest(sealed.manifest, self._trust_store)
        except IntegrityError as exc:
            raise CapabilityReactionError(
                "capability admission signature is invalid or untrusted"
            ) from exc
        if (
            set(payload) != {"schema_version", "evidence"}
            or payload.get("schema_version") != "strathmark-v3-capability-admission-v1"
        ):
            raise CapabilityReactionError("capability admission payload is not closed")
        evidence = payload["evidence"]
        if not isinstance(evidence, Mapping):
            raise CapabilityReactionError("capability admission evidence is not an object")
        try:
            return CapabilityEvidence.from_dict(evidence)
        except ContractError as exc:
            raise CapabilityReactionError("capability admission evidence is invalid") from exc


class CapabilityAuthorityPort(Protocol):
    """Narrow source-authority boundary; callers cannot self-classify evidence."""

    def verify_source(self, evidence: CapabilityEvidence) -> None: ...

    def invalidated_unissued_work(
        self, evidence: CapabilityEvidence
    ) -> tuple[StableIdentifier, ...]: ...

    def mandatory_reaction_count(
        self,
        evidence: CapabilityEvidence,
        lineage_sources: tuple[int, ...],
        invalidated_work: tuple[StableIdentifier, ...],
    ) -> int: ...

    def verify_source_at_commit(
        self, connection: sqlite3.Connection, evidence: CapabilityEvidence
    ) -> None: ...


class SQLiteCapabilityAuthority:
    """Verify signed admission against the authoritative result/import event."""

    def __init__(self, database_path: Path | str) -> None:
        self._database_path = Path(database_path).expanduser().resolve(strict=False)

    def verify_source(self, evidence: CapabilityEvidence) -> None:
        with open_v3_connection(self._database_path, read_only=True) as connection:
            self._verify(connection, evidence)

    def verify_source_at_commit(
        self, connection: sqlite3.Connection, evidence: CapabilityEvidence
    ) -> None:
        self._verify(connection, evidence)

    @staticmethod
    def _verify(connection: sqlite3.Connection, evidence: CapabilityEvidence) -> None:
        if evidence.source is EvidenceSource.LIVE_ISSUED_RACE:
            row = connection.execute(
                "SELECT result.result_key, result.revision, result.competitor_id, "
                "result.observation_digest, result.numeric_eligible, "
                "result.settled_global_sequence, event.event_digest "
                "FROM v3_result_revisions result JOIN v3_events event "
                "ON event.global_sequence=result.source_global_sequence "
                "WHERE result.source_global_sequence=? AND result.revision=("
                "SELECT MAX(latest.revision) FROM v3_result_revisions latest "
                "WHERE latest.result_key=result.result_key)",
                (evidence.source_global_sequence,),
            ).fetchone()
            expected = (
                str(evidence.result_key),
                evidence.result_revision,
                str(evidence.competitor_id),
                evidence.observation_digest,
                int(evidence.numeric_eligible),
                evidence.authority_digest,
            )
            observed = (
                None
                if row is None
                else (str(row[0]), int(row[1]), str(row[2]), str(row[3]), int(row[4]), str(row[6]))
            )
            if row is None or row[5] is None or observed != expected:
                raise CapabilityReactionError(
                    "capability live evidence is not the latest settled issued result revision"
                )
            return
        binding = evidence.historical_binding
        if binding is None:
            raise CapabilityReactionError("historical capability evidence has no row binding")
        row = connection.execute(
            "SELECT event.event_digest, event.source_import_id, imported.source_cutoff, "
            "imported.source_catalog_digest, imported.source_tip_digest, historical.canonical_json, "
            "imported.eligible, historical.eligible, imported.cutover_manifest_digest, "
            "cutover.signed_manifest_digest "
            "FROM v3_events event JOIN v3_historical_imports imported "
            "ON imported.import_id=event.source_import_id JOIN v3_historical_import_rows historical "
            "ON historical.import_id=imported.import_id "
            "JOIN v3_historical_cutovers cutover ON cutover.import_id=imported.import_id "
            "WHERE event.global_sequence=? AND event.event_kind=? AND imported.import_id=? "
            "AND historical.row_digest=?",
            (
                evidence.source_global_sequence,
                EventKind.HISTORY_IMPORTED.value,
                binding.import_id,
                binding.row_digest,
            ),
        ).fetchone()
        if (
            row is None
            or str(row[0]) != evidence.authority_digest
            or str(row[2]) != binding.source_cutoff
        ):
            raise CapabilityReactionError(
                "historical capability evidence is not exact imported-row authority"
            )
        cutover_digest = row[8]
        if (
            int(row[6]) != 1
            or int(row[7]) != 1
            or not isinstance(cutover_digest, str)
            or len(cutover_digest) != 64
            or any(character not in "0123456789abcdef" for character in cutover_digest)
            or cutover_digest != binding.cutover_manifest_digest
            or str(row[9]) != cutover_digest
        ):
            raise CapabilityReactionError(
                "historical capability evidence lacks its exact verified signed cutover"
            )
        canonical_json = str(row[5])
        try:
            canonical_row = json.loads(canonical_json)
        except json.JSONDecodeError as exc:
            raise CapabilityReactionError("historical imported row is not canonical JSON") from exc
        if (
            not isinstance(canonical_row, Mapping)
            or json.dumps(canonical_row, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            != canonical_json
            or hashlib.sha256(canonical_json.encode("utf-8")).hexdigest() != binding.row_digest
        ):
            raise CapabilityReactionError("historical imported row digest or encoding differs")
        provenance = canonical_digest(
            {
                "schema_version": "strathmark-v3-historical-capability-provenance-v1",
                "import_id": str(row[1]),
                "row_digest": binding.row_digest,
                "canonical_row": canonical_row,
                "source_cutoff": str(row[2]),
                "source_catalog_digest": str(row[3]),
                "source_tip_digest": str(row[4]),
                "result_key": str(evidence.result_key),
                "competitor_id": str(evidence.competitor_id),
                "raw_time_ms": evidence.raw_time_ms,
                "context_digest": evidence.context_digest,
                "observed_at_utc": evidence.observed_at_utc,
            }
        )
        if provenance != binding.provenance_digest:
            raise CapabilityReactionError(
                "historical row membership or normalized provenance differs"
            )

    def invalidated_unissued_work(
        self, evidence: CapabilityEvidence
    ) -> tuple[StableIdentifier, ...]:
        if evidence.supersedes_revision is None:
            return ()
        with open_v3_connection(self._database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT DISTINCT dependency.field_id "
                "FROM v3_prepared_field_dependencies dependency "
                "JOIN v3_evidence_epoch_members member ON member.epoch_id=dependency.epoch_id "
                "JOIN v3_events invalidation ON invalidation.global_sequence=dependency.invalidated_by_sequence "
                "JOIN v3_events correction ON correction.command_id=invalidation.command_id "
                "AND correction.event_kind=? LEFT JOIN v3_round_issue_seals seal "
                "ON seal.round_id=dependency.round_id WHERE member.result_key=? "
                "AND correction.global_sequence=? AND invalidation.event_kind=? "
                "AND seal.round_id IS NULL ORDER BY dependency.field_id",
                (
                    EventKind.RESULT_SUPERSEDED.value,
                    str(evidence.result_key),
                    evidence.source_global_sequence,
                    EventKind.FIELD_SUPERSEDED.value,
                ),
            ).fetchall()
        return tuple(require_identifier(str(row[0]), expected_namespace="field") for row in rows)

    def mandatory_reaction_count(
        self,
        evidence: CapabilityEvidence,
        lineage_sources: tuple[int, ...],
        invalidated_work: tuple[StableIdentifier, ...],
    ) -> int:
        sources = set(lineage_sources)
        with open_v3_connection(self._database_path, read_only=True) as connection:
            if invalidated_work:
                placeholders = ",".join("?" for _item in invalidated_work)
                rows = connection.execute(
                    "SELECT DISTINCT member.source_global_sequence "
                    "FROM v3_prepared_field_dependencies dependency "
                    "JOIN v3_evidence_epoch_members member ON member.epoch_id=dependency.epoch_id "
                    "JOIN v3_events invalidation "
                    "ON invalidation.global_sequence=dependency.invalidated_by_sequence "
                    "JOIN v3_events correction ON correction.command_id=invalidation.command_id "
                    f"WHERE correction.global_sequence=? AND invalidation.event_kind=? "
                    f"AND dependency.field_id IN ({placeholders})",
                    (
                        evidence.source_global_sequence,
                        EventKind.FIELD_SUPERSEDED.value,
                        *(str(item) for item in invalidated_work),
                    ),
                ).fetchall()
                sources.update(int(row[0]) for row in rows)
            if not sources:
                return 0
            placeholders = ",".join("?" for _item in sources)
            row = connection.execute(
                "SELECT COUNT(*) FROM v3_derivation_reactions pending "
                f"WHERE pending.source_global_sequence IN ({placeholders}) "
                "AND pending.state='pending' AND NOT EXISTS ("
                "SELECT 1 FROM v3_derivation_reactions completed "
                "WHERE completed.source_global_sequence=pending.source_global_sequence "
                "AND completed.reaction_type=pending.reaction_type "
                "AND completed.state='completed')",
                tuple(sorted(sources)),
            ).fetchone()
        return int(row[0])


@dataclass(frozen=True, slots=True)
class CapabilityStateSnapshot:
    state_digest: str
    current_form: PositiveTimeDistribution
    demonstrated_capability: PositiveTimeDistribution
    observation_count: int

    @classmethod
    def from_state(cls, state: CapabilityState) -> CapabilityStateSnapshot:
        return cls(
            state.state_digest,
            state.current_form,
            state.demonstrated_capability,
            state.observation_count,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "state_digest": self.state_digest,
            "current_form": self.current_form.to_dict(),
            "demonstrated_capability": self.demonstrated_capability.to_dict(),
            "observation_count": self.observation_count,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CapabilityStateSnapshot:
        if (
            set(value)
            != {"state_digest", "current_form", "demonstrated_capability", "observation_count"}
            or not isinstance(value["current_form"], Mapping)
            or not isinstance(value["demonstrated_capability"], Mapping)
        ):
            raise CapabilityReactionError("capability state snapshot is invalid")
        return cls(
            value["state_digest"],
            PositiveTimeDistribution.from_dict(value["current_form"]),
            PositiveTimeDistribution.from_dict(value["demonstrated_capability"]),
            value["observation_count"],
        )


@dataclass(frozen=True, slots=True)
class CapabilityReactionReceipt:
    competitor_id: StableIdentifier
    context_digest: str
    source_global_sequence: int
    superseded_source_global_sequence: int | None
    event_kind: str
    governor_receipt_digest: str
    capacity_manifest_digest: str
    before_state: CapabilityStateSnapshot | None
    after_state: CapabilityState | None
    invalidated_unissued_work: tuple[StableIdentifier, ...]
    capacity: RebaseCapacityDecision
    receipt_digest: str

    def content_value(self) -> dict[str, object]:
        return {
            "schema_version": CAPABILITY_REACTION_SCHEMA_VERSION,
            "competitor_id": str(self.competitor_id),
            "context_digest": self.context_digest,
            "source_global_sequence": self.source_global_sequence,
            "superseded_source_global_sequence": self.superseded_source_global_sequence,
            "event_kind": self.event_kind,
            "governor_receipt_digest": self.governor_receipt_digest,
            "capacity_manifest_digest": self.capacity_manifest_digest,
            "before_state": None if self.before_state is None else self.before_state.to_dict(),
            "after_state": None if self.after_state is None else self.after_state.to_dict(),
            "invalidated_unissued_work": [str(item) for item in self.invalidated_unissued_work],
            "capacity": {
                "admitted": self.capacity.admitted,
                "evidence_preserved": self.capacity.evidence_preserved,
                "next_round_barrier_open": self.capacity.next_round_barrier_open,
                "reason": self.capacity.reason,
                "lineage_rows": self.capacity.lineage_rows,
                "invalidated_work": self.capacity.invalidated_work,
                "mandatory_reactions": self.capacity.mandatory_reactions,
                "envelope_digest": self.capacity.envelope_digest,
            },
        }

    def to_dict(self) -> dict[str, object]:
        return {**self.content_value(), "receipt_digest": self.receipt_digest}

    @classmethod
    def create(
        cls,
        *,
        evidence: CapabilityEvidence,
        superseded_source_global_sequence: int | None,
        event_kind: EventKind,
        governor_receipt_digest: str,
        capacity_manifest_digest: str,
        before_state: CapabilityState | None,
        after_state: CapabilityState | None,
        invalidated_unissued_work: tuple[StableIdentifier, ...],
        capacity: RebaseCapacityDecision,
    ) -> CapabilityReactionReceipt:
        provisional = cls(
            evidence.competitor_id,
            evidence.context_digest,
            evidence.source_global_sequence,
            superseded_source_global_sequence,
            event_kind.value,
            governor_receipt_digest,
            capacity_manifest_digest,
            None if before_state is None else CapabilityStateSnapshot.from_state(before_state),
            after_state,
            invalidated_unissued_work,
            capacity,
            "0" * 64,
        )
        return replace(provisional, receipt_digest=canonical_digest(provisional.content_value()))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CapabilityReactionReceipt:
        expected = {
            "schema_version",
            "competitor_id",
            "context_digest",
            "source_global_sequence",
            "superseded_source_global_sequence",
            "event_kind",
            "governor_receipt_digest",
            "capacity_manifest_digest",
            "before_state",
            "after_state",
            "invalidated_unissued_work",
            "capacity",
            "receipt_digest",
        }
        if (
            set(value) != expected
            or value.get("schema_version") != CAPABILITY_REACTION_SCHEMA_VERSION
        ):
            raise CapabilityReactionError("capability reaction receipt fields are not closed")
        before = value["before_state"]
        after = value["after_state"]
        capacity = value["capacity"]
        work = value["invalidated_unissued_work"]
        if (
            (before is not None and not isinstance(before, Mapping))
            or (after is not None and not isinstance(after, Mapping))
            or not isinstance(capacity, Mapping)
            or not isinstance(work, list)
        ):
            raise CapabilityReactionError("capability reaction receipt nested values are invalid")
        result = cls(
            competitor_id=require_identifier(
                value["competitor_id"], expected_namespace="competitor"
            ),
            context_digest=value["context_digest"],
            source_global_sequence=value["source_global_sequence"],
            superseded_source_global_sequence=value["superseded_source_global_sequence"],
            event_kind=value["event_kind"],
            governor_receipt_digest=value["governor_receipt_digest"],
            capacity_manifest_digest=value["capacity_manifest_digest"],
            before_state=None if before is None else CapabilityStateSnapshot.from_dict(before),
            after_state=None if after is None else CapabilityState.from_dict(after),
            invalidated_unissued_work=tuple(require_identifier(item) for item in work),
            capacity=RebaseCapacityDecision(**capacity),
            receipt_digest=value["receipt_digest"],
        )
        if result.receipt_digest != canonical_digest(result.content_value()):
            raise CapabilityReactionError("capability reaction receipt digest mismatch")
        return result


class CapabilityReactionService:
    """Append one state transition/rebase and then cross the U5 capability barrier."""

    def __init__(
        self,
        database_path: Path | str,
        *,
        verifier: CapabilityAdmissionVerifier,
        capacity: SealedCapabilityCapacity,
        capacity_verifier: CapabilityCapacityVerifier,
        authority: CapabilityAuthorityPort | None = None,
    ) -> None:
        if not isinstance(verifier, CapabilityAdmissionVerifier):
            raise CapabilityReactionError("capability service requires a pinned verifier")
        if not isinstance(capacity_verifier, CapabilityCapacityVerifier):
            raise CapabilityReactionError("capability service requires a pinned capacity verifier")
        self._events = SQLiteEventStore(database_path)
        self._verifier = verifier
        self._authority = authority or SQLiteCapabilityAuthority(database_path)
        self._capacity = capacity_verifier.verify(capacity)
        self._capacity_manifest_digest = _signed_manifest_digest(capacity)

    @property
    def database_path(self) -> Path:
        """The exact event authority used by this reaction service."""

        return self._events.database_path

    def react(
        self,
        sealed: SealedCapabilityAdmission,
        *,
        command_id: IdempotencyKey,
        actor_id: StableIdentifier,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
        complete_derivation_barrier: bool = True,
    ) -> CapabilityReactionReceipt:
        if not isinstance(command_id, IdempotencyKey):
            raise CapabilityReactionError("capability command requires an IdempotencyKey")
        require_identifier(actor_id, expected_namespace="actor")
        if not isinstance(complete_derivation_barrier, bool):
            raise CapabilityReactionError("barrier completion choice must be explicit")
        evidence = self._verifier.verify(sealed)
        retry = self._exact_retry(command_id, actor_id, sealed)
        if retry is not None:
            if complete_derivation_barrier:
                self._complete_barrier(retry, actor_id, occurred_at_utc, monotonic_elapsed_ms)
            return retry
        self._authority.verify_source(evidence)
        aggregate_id = self._aggregate_id(evidence.competitor_id, evidence.context_digest)
        history = self._history(aggregate_id)
        active_history = history
        same_result = sorted(
            (item for item in history if item.result_key == evidence.result_key),
            key=lambda item: item.result_revision,
        )
        superseded_sequence = None
        if evidence.result_revision == 1:
            if same_result:
                raise CapabilityReactionError("capability result already has a first revision")
        else:
            if not same_result or same_result[-1].result_revision != evidence.supersedes_revision:
                raise CapabilityReactionError(
                    "capability correction does not supersede the active revision"
                )
            superseded_sequence = same_result[-1].source_global_sequence
        before = replay_capability(tuple(active_history))
        invalidated = self._authority.invalidated_unissued_work(evidence)
        if tuple(sorted(invalidated, key=str)) != invalidated or len(set(invalidated)) != len(
            invalidated
        ):
            raise CapabilityReactionError(
                "authoritative invalidation work must be sorted and unique"
            )
        lineage_sources = tuple(
            sorted({item.source_global_sequence for item in (*history, evidence)})
        )
        mandatory_reactions = self._authority.mandatory_reaction_count(
            evidence, lineage_sources, invalidated
        )
        capacity = evaluate_rebase_capacity(
            self._capacity,
            lineage_rows=len(history) + 1,
            invalidated_work=len(invalidated),
            mandatory_reactions=mandatory_reactions,
        )
        after = replay_capability((*active_history, evidence)) if capacity.admitted else before
        event_kind = (
            EventKind.CAPABILITY_UPDATED
            if evidence.supersedes_revision is None
            else EventKind.CAPABILITY_STATE_REBASED
        )
        receipt = CapabilityReactionReceipt.create(
            evidence=evidence,
            superseded_source_global_sequence=superseded_sequence,
            event_kind=event_kind,
            governor_receipt_digest=_signed_manifest_digest(sealed),
            capacity_manifest_digest=self._capacity_manifest_digest,
            before_state=before,
            after_state=after,
            invalidated_unissued_work=invalidated,
            capacity=capacity,
        )
        if not capacity.admitted:
            return receipt
        payload = InlinePayload.from_value(
            {
                "schema_version": CAPABILITY_REACTION_SCHEMA_VERSION,
                "admission_manifest": sealed.to_dict(),
                "evidence": evidence.to_dict(),
                "receipt": receipt.to_dict(),
            }
        )
        head = self._events.aggregate_head(str(aggregate_id))
        version = 0 if head is None else head[0]
        command_kind = (
            CommandKind.RECORD_CAPABILITY_UPDATE
            if event_kind is EventKind.CAPABILITY_UPDATED
            else CommandKind.REBASE_CAPABILITY_STATE
        )
        command = CommandEnvelope(
            command_kind,
            command_id,
            aggregate_id,
            ((str(aggregate_id), version),),
            actor_id,
            payload,
        )
        request = CommandRequest(
            actor_id,
            command,
            (EventIntent(AggregateKind.COMPETITOR, aggregate_id, event_kind),),
            CAPABILITY_REACTION_SCHEMA_VERSION,
            receipt.to_dict(),
            occurred_at_utc,
            monotonic_elapsed_ms,
        )

        def commit_guard(
            connection: sqlite3.Connection, _events: tuple[EventEnvelope, ...]
        ) -> None:
            self._authority.verify_source_at_commit(connection, evidence)

        stored = self._events.execute(request, projection_hook=commit_guard)
        persisted = CapabilityReactionReceipt.from_dict(stored.value())
        if complete_derivation_barrier and capacity.admitted:
            self._complete_barrier(persisted, actor_id, occurred_at_utc, monotonic_elapsed_ms)
        return persisted

    def replay_active_state(
        self, competitor_id: StableIdentifier, context_digest: str
    ) -> CapabilityState | None:
        aggregate_id = self._aggregate_id(competitor_id, context_digest)
        return replay_capability(tuple(self._history(aggregate_id)))

    @staticmethod
    def _aggregate_id(competitor_id: StableIdentifier, context_digest: str) -> StableIdentifier:
        require_identifier(competitor_id, expected_namespace="competitor")
        if not isinstance(context_digest, str) or len(context_digest) != 64:
            raise CapabilityReactionError("capability context requires a digest")
        return deterministic_identifier(
            "competitor",
            {"competitor_id": str(competitor_id), "context_digest": context_digest},
        )

    def _history(self, aggregate_id: StableIdentifier) -> list[CapabilityEvidence]:
        with open_v3_connection(self._events.database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT envelope_json FROM v3_events WHERE aggregate_id=? AND event_kind IN (?, ?) "
                "ORDER BY aggregate_version",
                (
                    str(aggregate_id),
                    EventKind.CAPABILITY_UPDATED.value,
                    EventKind.CAPABILITY_STATE_REBASED.value,
                ),
            ).fetchall()
        history = []
        for row in rows:
            event = EventEnvelope.from_dict(json.loads(str(row[0])))
            if not isinstance(event.command.payload, InlinePayload):
                raise CapabilityReactionError("capability authority event must remain inline")
            value = event.command.payload.to_value()
            evidence = value.get("evidence")
            receipt = value.get("receipt")
            if (
                value.get("schema_version") != CAPABILITY_REACTION_SCHEMA_VERSION
                or not isinstance(evidence, Mapping)
                or not isinstance(receipt, Mapping)
            ):
                raise CapabilityReactionError("capability authority payload is malformed")
            CapabilityReactionReceipt.from_dict(receipt)
            history.append(CapabilityEvidence.from_dict(evidence))
        return history

    def _exact_retry(
        self,
        command_id: IdempotencyKey,
        actor_id: StableIdentifier,
        sealed: SealedCapabilityAdmission,
    ) -> CapabilityReactionReceipt | None:
        with open_v3_connection(self._events.database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT first_global_sequence FROM v3_idempotency_records WHERE idempotency_key=?",
                (str(command_id),),
            ).fetchone()
            if row is None:
                return None
            envelope_row = connection.execute(
                "SELECT envelope_json FROM v3_events WHERE global_sequence=?", (int(row[0]),)
            ).fetchone()
        event = EventEnvelope.from_dict(json.loads(str(envelope_row[0])))
        stored = self._events.lookup_exact_retry(
            principal_id=str(actor_id),
            idempotency_key=str(command_id),
            command_kind=event.command.kind,
            target_aggregate=str(event.command.target_aggregate),
            payload_digest=event.command.payload_digest,
        )
        assert isinstance(stored, StoredCommandResult)
        receipt = CapabilityReactionReceipt.from_dict(stored.value())
        if not isinstance(event.command.payload, InlinePayload):
            raise EventStoreConflict("capability retry authority payload is not inline")
        original = event.command.payload.to_value().get("admission_manifest")
        if not isinstance(original, Mapping) or canonical_digest(original) != canonical_digest(
            sealed.to_dict()
        ):
            raise EventStoreConflict("capability idempotency key binds different signed admission")
        if receipt.governor_receipt_digest != _signed_manifest_digest(sealed):
            raise EventStoreConflict("capability idempotency key binds different admission")
        return receipt

    def _complete_barrier(
        self,
        receipt: CapabilityReactionReceipt,
        actor_id: StableIdentifier,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
    ) -> None:
        if not receipt.capacity.admitted:
            return
        from strathmark.v3.application.lifecycle import LifecycleService

        service = LifecycleService(self._events.database_path)
        service.complete_derivation_reaction(
            receipt.source_global_sequence,
            MandatoryReaction.CAPABILITY,
            receipt.receipt_digest,
            command_id=IdempotencyKey(
                f"command:{canonical_digest({'source': receipt.source_global_sequence, 'capability': receipt.receipt_digest})}"
            ),
            actor_id=actor_id,
            occurred_at_utc=occurred_at_utc,
            monotonic_elapsed_ms=monotonic_elapsed_ms,
        )


__all__ = [
    "CAPABILITY_ADMISSION_MANIFEST_KIND",
    "HISTORICAL_CUTOVER_MANIFEST_KIND",
    "CapabilityAdmissionVerifier",
    "CapabilityAuthorityPort",
    "CapabilityCapacityVerifier",
    "CapabilityReactionError",
    "CapabilityReactionReceipt",
    "CapabilityReactionService",
    "CapabilityStateSnapshot",
    "SQLiteCapabilityAuthority",
    "SealedCapabilityAdmission",
    "SealedCapabilityCapacity",
    "activate_historical_import_cutover",
    "seal_capability_admission",
    "seal_capability_capacity",
    "seal_historical_import_cutover",
]
