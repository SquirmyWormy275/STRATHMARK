"""Restart-safe composition of every mandatory post-settlement reaction.

The U5 projection already records an append-only obligation for every result and
mandatory derivation.  This module deliberately uses that authority instead of
creating a parallel queue: a result becomes eligible for learning only after its
issued field is durably settled, and a restart resumes every derivation that does not
yet have a completed reaction row.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Protocol, cast

from strathmark.v3.application.capability_reactions import (
    CapabilityReactionReceipt,
    CapabilityReactionService,
    SealedCapabilityAdmission,
    seal_capability_admission,
)
from strathmark.v3.application.credibility_reactions import (
    SQLiteCredibilityReactionService,
)
from strathmark.v3.application.factory import FactoryService
from strathmark.v3.application.job_ports import RollingDerivationPending
from strathmark.v3.application.lifecycle import LifecycleService
from strathmark.v3.contracts.canonical import canonical_decimal_string, canonical_digest
from strathmark.v3.contracts.commands import InlinePayload
from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.events import EventEnvelope, EventKind
from strathmark.v3.contracts.evidence import (
    ResultObservation,
    TargetContext,
    require_utc_milliseconds,
)
from strathmark.v3.contracts.identifiers import (
    IdempotencyKey,
    StableIdentifier,
    require_identifier,
)
from strathmark.v3.domain.capability import CapabilityPrior
from strathmark.v3.domain.epochs import MandatoryReaction
from strathmark.v3.domain.evidence import IssuedFieldFact, admit_observation
from strathmark.v3.infrastructure.integrity import P256Signer, verify_manifest
from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection
from strathmark.v3.infrastructure.sqlite.event_store import (
    EVENT_SET_SCHEMA_VERSION,
    StoredCommandResult,
)


class SettlementReactionError(RuntimeError):
    """Settlement learning authority is missing, inconsistent, or incomplete."""


class RollingReactionPort(Protocol):
    """The existing durable rolling reaction service boundary."""

    def react(self, result: StoredCommandResult) -> None: ...

    def recover_pending(self) -> int: ...

    def derivation_authority(self, source_global_sequence: int) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class SettlementCapabilityPolicy:
    """Closed capability-admission parameters bound into a promoted bundle."""

    prior_median_seconds: str = "40"
    calibrated_beta: str = "0.12"
    evidence_log_variance: str = "0.0025"
    conversion_log_variance: str = "0"
    effective_weight: str = "1"

    def __post_init__(self) -> None:
        try:
            median = canonical_decimal_string(self.prior_median_seconds)
            beta = canonical_decimal_string(self.calibrated_beta)
            evidence_value = canonical_decimal_string(self.evidence_log_variance)
            conversion_value = canonical_decimal_string(self.conversion_log_variance)
            weight_value = canonical_decimal_string(self.effective_weight)
            prior = CapabilityPrior.from_median_seconds(median, calibrated_beta=beta)
            evidence = Decimal(evidence_value)
            conversion = Decimal(conversion_value)
            weight = Decimal(weight_value)
        except (ContractError, InvalidOperation, TypeError, ValueError) as exc:
            raise SettlementReactionError("capability settlement policy is invalid") from exc
        if (
            not evidence.is_finite()
            or not conversion.is_finite()
            or not weight.is_finite()
            or not Decimal("0") <= evidence <= Decimal("100")
            or not Decimal("0") <= conversion <= Decimal("100")
            or not Decimal("0.000001") <= weight <= Decimal("1")
        ):
            raise SettlementReactionError("capability settlement policy is outside its bounds")
        object.__setattr__(self, "prior_median_seconds", median)
        object.__setattr__(self, "calibrated_beta", prior.calibrated_beta)
        object.__setattr__(self, "evidence_log_variance", evidence_value)
        object.__setattr__(self, "conversion_log_variance", conversion_value)
        object.__setattr__(self, "effective_weight", weight_value)

    @property
    def prior(self) -> CapabilityPrior:
        return CapabilityPrior.from_median_seconds(
            self.prior_median_seconds, calibrated_beta=self.calibrated_beta
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "schema_version": "strathmark-v3-settlement-capability-policy-v1",
            "prior_median_seconds": self.prior_median_seconds,
            "calibrated_beta": self.calibrated_beta,
            "evidence_log_variance": self.evidence_log_variance,
            "conversion_log_variance": self.conversion_log_variance,
            "effective_weight": self.effective_weight,
        }

    @property
    def component_digest(self) -> str:
        return canonical_digest(self.to_dict())


class SettlementReactionDispatcher:
    """Compose durable post-commit reactions for every newly settled result.

    Capability and credibility effects are exactly-once because their own authority
    events and the U5 completed-derivation rows are idempotent.  If a process exits
    after an inner authority commit but before returning, the next recovery observes
    the completed row and resumes only the remaining work.
    """

    def __init__(
        self,
        database_path: Path | str,
        *,
        rolling: RollingReactionPort,
        capability: CapabilityReactionService,
        credibility: SQLiteCredibilityReactionService,
        factory: FactoryService,
        capability_policy: SettlementCapabilityPolicy,
        admission_signer: P256Signer,
        actor_id: StableIdentifier,
        clock: Callable[[], str],
        monotonic_clock: Callable[[], int],
    ) -> None:
        path = Path(database_path).expanduser().resolve(strict=False)
        if (
            not callable(getattr(rolling, "react", None))
            or not callable(getattr(rolling, "recover_pending", None))
            or not callable(getattr(rolling, "derivation_authority", None))
            or not isinstance(capability, CapabilityReactionService)
            or not isinstance(credibility, SQLiteCredibilityReactionService)
            or not isinstance(factory, FactoryService)
            or not isinstance(capability_policy, SettlementCapabilityPolicy)
            or not callable(getattr(admission_signer, "sign", None))
            or not hasattr(admission_signer, "identity")
            or not callable(clock)
            or not callable(monotonic_clock)
        ):
            raise SettlementReactionError("settlement dispatcher requires typed authorities")
        require_identifier(actor_id, expected_namespace="actor")
        if (
            capability.database_path != path
            or credibility.database_path != path
            or factory.event_store.database_path != path
        ):
            raise SettlementReactionError("settlement reaction authorities use different ledgers")
        self._database_path = path
        self._rolling = rolling
        self._capability = capability
        self._credibility = credibility
        self._factory = factory
        self._policy = capability_policy
        self._signer = admission_signer
        self._actor_id = actor_id
        self._clock = clock
        self._monotonic_clock = monotonic_clock
        self._validate_runtime_clock()
        self.recover_pending()

    @property
    def database_path(self) -> Path:
        """Return the exact ledger authority used by every reaction."""

        return self._database_path

    def react(self, result: StoredCommandResult) -> None:
        if not isinstance(result, StoredCommandResult):
            raise SettlementReactionError("settlement dispatcher requires a stored command result")
        contains_settlement = self._contains_settlement(result)
        self._rolling.react(result)
        if contains_settlement:
            self.recover_pending()

    def recover_pending(self) -> int:
        """Drain all settled capability/credibility obligations in source order."""

        self._rolling.recover_pending()
        completed = 0
        for source in self._pending_sources():
            before = self._pending_learning_reactions(source)
            if not before:
                continue
            self._react_source(source, before)
            after = self._pending_learning_reactions(source)
            rolling_pending = {
                MandatoryReaction.INVALIDATION,
                MandatoryReaction.READINESS,
            }
            if any(item not in rolling_pending for item in after):
                raise SettlementReactionError(
                    "settlement learning reaction returned without completing its durable marker"
                )
            completed += int(after != before)
        self._rolling.recover_pending()
        return completed

    def _react_source(
        self, source: int, pending: tuple[MandatoryReaction, ...]
    ) -> CapabilityReactionReceipt | None:
        row, observation, issued, authority_digest = self._settled_authority(source)
        bundle_digest = self._verify_bundle_policy(observation.tournament_id)
        now = require_utc_milliseconds(self._clock())
        monotonic = self._monotonic_clock()
        if isinstance(monotonic, bool) or not isinstance(monotonic, int) or monotonic < 0:
            raise SettlementReactionError("settlement reaction monotonic clock is invalid")
        capability_receipt = None
        if MandatoryReaction.CAPABILITY in pending:
            admitted = admit_observation(
                observation,
                issued_field=issued,
                field_revision=int(row[7]),
                claimed_receipt_id=StableIdentifier(str(row[9])),
            )
            if (
                admitted.numeric_eligible is not bool(row[5])
                or admitted.reason.value != str(row[6])
                or canonical_digest(observation.to_dict()) != str(row[4])
            ):
                raise SettlementReactionError(
                    "settled result admission differs from authoritative U5 classification"
                )
            command_id = self._capability_command_id(source, bundle_digest)
            sealed = self._existing_capability_admission(command_id)
            if sealed is None:
                sealed = seal_capability_admission(
                    admitted=admitted,
                    result_key=StableIdentifier(str(row[0])),
                    source_global_sequence=source,
                    authority_digest=authority_digest,
                    prior=self._policy.prior,
                    evidence_log_variance=self._policy.evidence_log_variance,
                    conversion_log_variance=self._policy.conversion_log_variance,
                    effective_weight=self._policy.effective_weight,
                    historical_binding=None,
                    signer=self._signer,
                    created_at=now,
                )
            else:
                body = sealed.manifest.body()
                payload = body.get("payload")
                evidence = payload.get("evidence") if isinstance(payload, dict) else None
                if (
                    not isinstance(evidence, dict)
                    or evidence.get("source_global_sequence") != source
                    or evidence.get("authority_digest") != authority_digest
                ):
                    raise SettlementReactionError(
                        "persisted capability retry admission differs from settlement authority"
                    )
            capability_receipt = self._capability.react(
                sealed,
                command_id=command_id,
                actor_id=self._actor_id,
                occurred_at_utc=now,
                monotonic_elapsed_ms=monotonic,
                complete_derivation_barrier=True,
            )
            if not capability_receipt.capacity.admitted:
                raise SettlementReactionError("capability capacity rejected settled evidence")
        if any(
            reaction in pending
            for reaction in (
                MandatoryReaction.SCORING,
                MandatoryReaction.COVERAGE,
                MandatoryReaction.WEIGHTS,
                MandatoryReaction.CREDIBILITY,
            )
        ):
            ledger, weights = self._credibility.react_result(
                StableIdentifier(str(row[0])),
                actor_id=self._actor_id,
                occurred_at_utc=now,
                monotonic_elapsed_ms=monotonic,
            )
            self._complete_credibility_reactions(
                source,
                pending,
                ledger=ledger,
                weight_receipt=weights,
                occurred_at_utc=now,
                monotonic_elapsed_ms=monotonic,
            )
        try:
            self._complete_rolling_reactions(source, pending, now, monotonic)
        except RollingDerivationPending:
            pass
        return capability_receipt

    def _complete_rolling_reactions(
        self,
        source: int,
        pending: tuple[MandatoryReaction, ...],
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
    ) -> None:
        reactions = tuple(
            item
            for item in (MandatoryReaction.INVALIDATION, MandatoryReaction.READINESS)
            if item in pending
        )
        if not reactions:
            return
        authority = self._rolling.derivation_authority(source)
        expected = {
            "schema_version",
            "source_global_sequence",
            "reaction_id",
            "event_set_digest",
            "plan_digest",
            "completion_digest",
            "card_publications",
        }
        if (
            not isinstance(authority, dict)
            or set(authority) != expected
            or authority.get("schema_version") != "strathmark-v3-rolling-derivation-authority-v1"
            or authority.get("source_global_sequence") != source
            or not isinstance(authority.get("card_publications"), list)
            or any(
                not isinstance(item, dict) or set(item) != {"card_digest", "publication_digest"}
                for item in authority.get("card_publications", [])
            )
        ):
            raise SettlementReactionError("rolling derivation authority differs")
        for reaction in reactions:
            output_digest = canonical_digest(
                {
                    "schema_version": "strathmark-v3-rolling-result-reaction-v1",
                    "reaction": reaction.value,
                    "authority": authority,
                }
            )
            self._complete_reaction(
                source,
                reaction,
                output_digest,
                occurred_at_utc=occurred_at_utc,
                monotonic_elapsed_ms=monotonic_elapsed_ms,
            )

    def _complete_credibility_reactions(
        self,
        source: int,
        pending: tuple[MandatoryReaction, ...],
        *,
        ledger: object,
        weight_receipt: object,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
    ) -> None:
        scoring = [
            item.to_dict()
            for item in getattr(ledger, "scores", ())
            if item.source_sequence == source
        ] + [
            item.to_dict()
            for item in getattr(ledger, "reversals", ())
            if item.source_sequence == source and item.target_kind == "score"
        ]
        coverage = [
            item.to_dict()
            for item in getattr(ledger, "opportunities", ())
            if item.source_sequence == source
        ] + [
            item.to_dict()
            for item in getattr(ledger, "reversals", ())
            if item.source_sequence == source and item.target_kind == "opportunity"
        ]
        outputs = {
            MandatoryReaction.SCORING: canonical_digest(
                {
                    "schema_version": "strathmark-v3-scoring-reaction-output-v1",
                    "source_global_sequence": source,
                    "records": scoring,
                }
            ),
            MandatoryReaction.COVERAGE: canonical_digest(
                {
                    "schema_version": "strathmark-v3-coverage-reaction-output-v1",
                    "source_global_sequence": source,
                    "records": coverage,
                }
            ),
            MandatoryReaction.WEIGHTS: canonical_digest(
                {
                    "schema_version": "strathmark-v3-weights-reaction-output-v1",
                    "source_global_sequence": source,
                    "weight_receipt_digest": getattr(weight_receipt, "receipt_digest", None),
                }
            ),
        }
        for reaction, output_digest in outputs.items():
            if reaction in pending:
                self._complete_reaction(
                    source,
                    reaction,
                    output_digest,
                    occurred_at_utc=occurred_at_utc,
                    monotonic_elapsed_ms=monotonic_elapsed_ms,
                )

    def _complete_reaction(
        self,
        source: int,
        reaction: MandatoryReaction,
        output_digest: str,
        *,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
    ) -> None:
        LifecycleService(self._database_path).complete_derivation_reaction(
            source,
            reaction,
            output_digest,
            command_id=IdempotencyKey(
                f"command:{canonical_digest({'source': source, 'reaction': reaction.value, 'output': output_digest})}"
            ),
            actor_id=self._actor_id,
            occurred_at_utc=occurred_at_utc,
            monotonic_elapsed_ms=monotonic_elapsed_ms,
        )

    @staticmethod
    def _capability_command_id(source: int, bundle_digest: str) -> IdempotencyKey:
        return IdempotencyKey(
            f"command:{canonical_digest({'source': source, 'reaction': 'capability', 'bundle_digest': bundle_digest})}"
        )

    def _existing_capability_admission(
        self, command_id: IdempotencyKey
    ) -> SealedCapabilityAdmission | None:
        """Recover the exact signed input when an event outran its barrier marker.

        P-256 signatures are intentionally non-deterministic. Re-signing the same
        semantic admission after a crash would therefore conflict with the already
        durable idempotency record. The append-only capability event is the authority
        for the original sealed input on this recovery path.
        """

        with open_v3_connection(self._database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT event.envelope_json FROM v3_idempotency_records record "
                "JOIN v3_events event ON event.global_sequence=record.first_global_sequence "
                "WHERE record.principal_id=? AND record.idempotency_key=?",
                (str(self._actor_id), str(command_id)),
            ).fetchone()
        if row is None:
            return None
        event = EventEnvelope.from_dict(json.loads(str(row[0])))
        if event.kind not in (
            EventKind.CAPABILITY_UPDATED,
            EventKind.CAPABILITY_STATE_REBASED,
        ) or not isinstance(event.command.payload, InlinePayload):
            raise SettlementReactionError(
                "capability retry idempotency record is not a capability authority event"
            )
        manifest = event.command.payload.to_value().get("admission_manifest")
        if not isinstance(manifest, dict):
            raise SettlementReactionError("capability retry admission manifest is malformed")
        try:
            return SealedCapabilityAdmission.from_dict(manifest)
        except Exception as exc:
            raise SettlementReactionError(
                "capability retry admission manifest failed closed validation"
            ) from exc

    def _verify_bundle_policy(self, tournament_id: StableIdentifier) -> str:
        try:
            installed = self._factory.bundle_for_tournament(tournament_id)
            payload = verify_manifest(
                installed.manifest,
                self._factory.repository.trust_policy.bundle_trust_store,
            )
        except Exception as exc:
            raise SettlementReactionError(
                "settlement learning requires a verified active or tournament-pinned bundle"
            ) from exc
        components = payload.get("component_digests")
        if (
            not isinstance(components, dict)
            or components.get("capability") != self._policy.component_digest
            or components.get("credibility") != self._credibility.component_digest
            or payload.get("bundle_digest") != installed.bundle_digest
        ):
            raise SettlementReactionError(
                "settlement learning policy differs from bundle authority"
            )
        return installed.bundle_digest

    def _settled_authority(
        self, source: int
    ) -> tuple[object, ResultObservation, IssuedFieldFact, str]:
        with open_v3_connection(self._database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT result_key,revision,source_global_sequence,observation_json,"
                "observation_digest,numeric_eligible,admission_reason,field_revision,field_id,"
                "claimed_receipt_id,settled_global_sequence FROM v3_result_revisions "
                "WHERE source_global_sequence=? AND revision=(SELECT MAX(latest.revision) "
                "FROM v3_result_revisions latest WHERE latest.result_key="
                "v3_result_revisions.result_key)",
                (source,),
            ).fetchone()
            event_row = connection.execute(
                "SELECT event_digest FROM v3_events WHERE global_sequence=?", (source,)
            ).fetchone()
        if row is None or row[10] is None or event_row is None:
            raise SettlementReactionError("learning source is not the active settled result")
        observation = ResultObservation.from_dict(json.loads(str(row[3])))
        issued = self._issued_field(StableIdentifier(str(row[8])), before_sequence=int(row[10]))
        return row, observation, issued, str(event_row[0])

    def _issued_field(self, field_id: StableIdentifier, *, before_sequence: int) -> IssuedFieldFact:
        with open_v3_connection(self._database_path, read_only=True) as connection:
            issue_row = connection.execute(
                "SELECT global_sequence,envelope_json FROM v3_events WHERE aggregate_id=? "
                "AND event_kind=? AND global_sequence<? ORDER BY global_sequence DESC LIMIT 1",
                (str(field_id), EventKind.FIELD_ISSUED.value, before_sequence),
            ).fetchone()
            if issue_row is None:
                raise SettlementReactionError("settled result has no prior issued-field authority")
            event = EventEnvelope.from_dict(json.loads(str(issue_row[1])))
            value = cast(InlinePayload, event.command.payload).to_value()
            if value.get("schema_version") == "strathmark-v3-batch-issue-authority-v1":
                matches = [
                    item
                    for item in value.get("fields", [])
                    if isinstance(item, dict) and item.get("field_id") == str(field_id)
                ]
                if len(matches) != 1:
                    raise SettlementReactionError("batch issue field authority is ambiguous")
                value = matches[0]
            ingress = connection.execute(
                "SELECT tournament_id,round_id,snapshot_json FROM v3_ingress_snapshots "
                "WHERE entity_kind='field' AND entity_id=? AND source_global_sequence<? "
                "ORDER BY upstream_revision DESC LIMIT 1",
                (str(field_id), int(issue_row[0])),
            ).fetchone()
        if ingress is None:
            raise SettlementReactionError("issued field has no authoritative ingress snapshot")
        snapshot = json.loads(str(ingress[2]))
        roster = tuple(
            require_identifier(item, expected_namespace="competitor")
            for item in value["competitor_ids"]
        )
        marks = value["issued_marks"]
        if not isinstance(marks, dict):
            raise SettlementReactionError("issued marks authority is malformed")
        return IssuedFieldFact(
            field_id,
            int(value["field_revision"]),
            roster,
            require_identifier(value["receipt_id"], expected_namespace="receipt"),
            require_identifier(str(ingress[0]), expected_namespace="tournament"),
            require_identifier(str(ingress[1]), expected_namespace="round"),
            TargetContext.from_dict(snapshot["target_context"]),
            tuple((competitor_id, int(marks[str(competitor_id)])) for competitor_id in roster),
        )

    def _pending_sources(self) -> tuple[int, ...]:
        with open_v3_connection(self._database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT DISTINCT result.source_global_sequence FROM v3_result_revisions result "
                "JOIN v3_derivation_reactions reaction ON reaction.source_global_sequence="
                "result.source_global_sequence WHERE result.settled_global_sequence IS NOT NULL "
                "AND reaction.state='pending' AND NOT EXISTS ("
                "SELECT 1 FROM v3_derivation_reactions completed WHERE "
                "completed.source_global_sequence=reaction.source_global_sequence AND "
                "completed.reaction_type=reaction.reaction_type AND completed.state='completed') "
                "ORDER BY result.source_global_sequence",
            ).fetchall()
        return tuple(int(row[0]) for row in rows)

    def _pending_learning_reactions(self, source: int) -> tuple[MandatoryReaction, ...]:
        with open_v3_connection(self._database_path, read_only=True) as connection:
            completed = {
                str(row[0])
                for row in connection.execute(
                    "SELECT reaction_type FROM v3_derivation_reactions "
                    "WHERE source_global_sequence=? AND state='completed'",
                    (source,),
                )
            }
        return tuple(reaction for reaction in MandatoryReaction if reaction.value not in completed)

    def _contains_settlement(self, result: StoredCommandResult) -> bool:
        with open_v3_connection(self._database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT global_sequence,event_id,event_digest,event_kind FROM v3_events "
                "WHERE global_sequence BETWEEN ? AND ? "
                "ORDER BY global_sequence",
                (result.first_global_sequence, result.last_global_sequence),
            ).fetchall()
        authority_digest = canonical_digest(
            {
                "schema_version": EVENT_SET_SCHEMA_VERSION,
                "events": [
                    {
                        "global_sequence": int(row[0]),
                        "event_id": str(row[1]),
                        "event_digest": str(row[2]),
                    }
                    for row in rows
                ],
            }
        )
        if (
            tuple(str(row[1]) for row in rows) != result.event_ids
            or authority_digest != result.event_set_digest
        ):
            raise SettlementReactionError("post-commit result event set differs from authority")
        return any(str(row[3]) == EventKind.LIVE_RACE_SETTLED.value for row in rows)

    def _validate_runtime_clock(self) -> None:
        require_utc_milliseconds(self._clock())
        value = self._monotonic_clock()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SettlementReactionError("settlement dispatcher clock is invalid")


__all__ = [
    "RollingReactionPort",
    "SettlementCapabilityPolicy",
    "SettlementReactionDispatcher",
    "SettlementReactionError",
]
