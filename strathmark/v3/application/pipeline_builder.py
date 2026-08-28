"""Production field pipeline construction from current rolling authorities."""

from __future__ import annotations

import json
from dataclasses import InitVar, dataclass
from pathlib import Path
from time import perf_counter_ns
from typing import Callable, Protocol

from strathmark.v3.application.capability_reactions import (
    CAPABILITY_REACTION_SCHEMA_VERSION,
    CapabilityAdmissionVerifier,
    CapabilityReactionReceipt,
    CapabilityStateSnapshot,
    SealedCapabilityAdmission,
)
from strathmark.v3.application.field_assembly import (
    AssemblyConflict,
    CapabilityPoolBasis,
    CompetitorCardAuthority,
    CompetitorPoolEvidence,
    CompetitorPredictionEvidence,
    FrozenFieldRevision,
    OperationalDisagreementReceipt,
    OperationalWeightAuthority,
    OverrideStartingEstimateBasis,
    RollingCapabilityBinding,
    RollingPublicationBinding,
    SealedPipelineOutput,
    ZeroHistoryPriorBasis,
    counterfactual_sheet_from_optimizer,
    seal_council_field_audit_authority,
    seal_disagreement_policy_authority,
)
from strathmark.v3.application.manual_actions import (
    ManualActionEntrant,
    ManualActionRequirement,
    create_manual_action_requirement,
)
from strathmark.v3.assessors.formula import (
    FormulaManifest,
    resolve_zero_history_prior,
)
from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.commands import InlinePayload
from strathmark.v3.contracts.events import AggregateKind, EventEnvelope, EventKind
from strathmark.v3.contracts.evidence import require_utc_milliseconds
from strathmark.v3.contracts.forecasts import AssessorKind, ForecastState
from strathmark.v3.contracts.identifiers import (
    StableIdentifier,
    deterministic_identifier,
)
from strathmark.v3.domain.capability import (
    CapabilityEvidence,
    CapabilityState,
    replay_capability,
)
from strathmark.v3.domain.credibility import WeightReceipt
from strathmark.v3.domain.disagreement import (
    AcceptedExpectedTimeOverrideState,
    CouncilAudit,
    CouncilMemberAudit,
    CouncilMemberStatus,
    DisagreementPolicy,
    ZeroHistoryPolicy,
    classify_disagreement,
    create_zero_history_estimate,
)
from strathmark.v3.domain.joint_dependence import (
    DependenceArtifact,
    FieldCompetitorForecast,
    bind_field_dependence,
    generate_aligned_component_joint_draws,
    generate_joint_draws,
    generate_joint_draws_from_pool_results,
    generate_joint_uniforms,
)
from strathmark.v3.domain.optimizer import OptimizationField, optimize_and_verify_field
from strathmark.v3.domain.pooling import pool_forecasts
from strathmark.v3.infrastructure.integrity import (
    IntegrityTrustStore,
    P256Signer,
    SignedManifest,
    verify_manifest,
)
from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection
from strathmark.v3.infrastructure.sqlite.projections import (
    ProjectionError,
    verified_expected_time_override_chain,
)

_OUTER = (
    AssessorKind.FORMULA,
    AssessorKind.ML,
    AssessorKind.LLM_COUNCIL,
)


@dataclass(frozen=True, slots=True)
class RollingCurrentCard:
    card: CompetitorCardAuthority
    publication: RollingPublicationBinding
    publication_manifest: SignedManifest
    council_aggregate_manifest: SignedManifest
    components: tuple[object, ...] = ()
    availability: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.card, CompetitorCardAuthority)
            or not isinstance(self.publication, RollingPublicationBinding)
            or not isinstance(self.publication_manifest, SignedManifest)
            or not isinstance(self.council_aggregate_manifest, SignedManifest)
            or self.card.competitor_id != self.publication.competitor_id
            or self.card.packet_digest != self.publication.evidence_digest
            or self.card.manifest.body_digest != self.publication.card_manifest_digest
            or self.publication_manifest.body_digest != self.publication.publication_manifest_digest
        ):
            raise AssemblyConflict("rolling current card authority differs")
        if self.components and (
            canonical_digest([item.to_dict() for item in self.components])
            != self.publication.component_refs_digest
            or self.availability != self.publication.availability
        ):
            raise AssemblyConflict("rolling current component authority differs")

    @classmethod
    def from_publication(cls, publication: object) -> RollingCurrentCard:
        from strathmark.v3.application.coordinator import RollingCardPublication

        if not isinstance(publication, RollingCardPublication):
            raise AssemblyConflict("rolling current card must be a typed publication")
        return cls(
            publication.authority,
            RollingPublicationBinding.create(
                card_key=publication.key.to_dict(),
                card_manifest_digest=publication.authority.manifest.body_digest,
                publication_digest=publication.publication_digest,
                publication_manifest_digest=publication.manifest.body_digest,
                component_refs_digest=canonical_digest(
                    [item.to_dict() for item in publication.components]
                ),
                availability=publication.availability,
                council_manifest_digest=publication.council_manifest_digest,
                council_aggregate_manifest_digest=(
                    publication.council_aggregate_manifest.body_digest
                ),
                hard_deadline_at=publication.hard_deadline_at,
                sealed_at=publication.sealed_at,
            ),
            publication.manifest,
            publication.council_aggregate_manifest,
            publication.components,
            publication.availability,
        )


@dataclass(frozen=True, slots=True)
class RollingCapabilityAuthority:
    state: CapabilityState
    binding: RollingCapabilityBinding

    def __post_init__(self) -> None:
        if (
            not isinstance(self.state, CapabilityState)
            or not isinstance(self.binding, RollingCapabilityBinding)
            or self.state.competitor_id != self.binding.competitor_id
            or self.state.context_digest != self.binding.context_digest
            or self.state.state_revision != self.binding.state_revision
            or self.state.state_digest != self.binding.state_digest
        ):
            raise AssemblyConflict("rolling capability state authority differs")


@dataclass(frozen=True, slots=True)
class RollingFieldBuildInputs:
    cards: tuple[RollingCurrentCard, ...]
    weight_receipt: WeightReceipt
    operational_weight_authority: OperationalWeightAuthority
    dependence_artifact: DependenceArtifact
    capability_authorities: tuple[RollingCapabilityAuthority, ...]
    disagreement_policy: DisagreementPolicy
    formula_manifest: FormulaManifest | None = None
    zero_history_policy: ZeroHistoryPolicy | None = None
    override_states: tuple[AcceptedExpectedTimeOverrideState, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.cards, tuple)
            or not self.cards
            or not all(isinstance(item, RollingCurrentCard) for item in self.cards)
            or not isinstance(self.weight_receipt, WeightReceipt)
            or not isinstance(self.operational_weight_authority, OperationalWeightAuthority)
            or not isinstance(self.dependence_artifact, DependenceArtifact)
            or not isinstance(self.capability_authorities, tuple)
            or not all(
                isinstance(item, RollingCapabilityAuthority) for item in self.capability_authorities
            )
            or not isinstance(self.disagreement_policy, DisagreementPolicy)
            or not isinstance(self.override_states, tuple)
            or not all(
                isinstance(item, AcceptedExpectedTimeOverrideState) for item in self.override_states
            )
        ):
            raise AssemblyConflict("rolling field build inputs must be typed")
        card_ids = tuple(item.card.competitor_id for item in self.cards)
        capability_ids = tuple(item.state.competitor_id for item in self.capability_authorities)
        override_ids = tuple(item.competitor_id for item in self.override_states)
        if (
            len(card_ids) != len(set(card_ids))
            or len(capability_ids) != len(set(capability_ids))
            or not set(capability_ids).issubset(set(card_ids))
            or len(override_ids) != len(set(override_ids))
            or not set(override_ids).issubset(set(card_ids))
        ):
            raise AssemblyConflict("rolling field capability roster differs")
        if set(card_ids) != set(capability_ids) and (
            not isinstance(self.formula_manifest, FormulaManifest)
            or not isinstance(self.zero_history_policy, ZeroHistoryPolicy)
        ):
            raise AssemblyConflict(
                "zero-history entrants require pinned Formula and policy authority"
            )


class RollingFieldInputPort(Protocol):
    def load_current(self, field: FrozenFieldRevision) -> RollingFieldBuildInputs: ...

    def verify_current(
        self,
        field: FrozenFieldRevision,
        publications: tuple[RollingPublicationBinding, ...],
        capabilities: tuple[RollingCapabilityBinding, ...],
        overrides: tuple[AcceptedExpectedTimeOverrideState, ...],
    ) -> None: ...


class CapabilityStateResolverPort(Protocol):
    def resolve_current(
        self, competitor_id: StableIdentifier, context_digest: str
    ) -> RollingCapabilityAuthority | None: ...

    def verify_current(self, capabilities: tuple[RollingCapabilityBinding, ...]) -> None: ...

    def resolve_override_current(
        self, field: FrozenFieldRevision, competitor_id: StableIdentifier
    ) -> AcceptedExpectedTimeOverrideState | None: ...

    def verify_override_current(
        self, overrides: tuple[AcceptedExpectedTimeOverrideState, ...]
    ) -> None: ...


class SQLiteCapabilityStateResolver:
    """Replay one bounded capability stream and return its exact atomic head."""

    _MAX_LINEAGE_ROWS = 256

    def __init__(
        self,
        database_path: Path | str,
        *,
        trust_store: IntegrityTrustStore,
    ) -> None:
        if isinstance(database_path, bool) or not isinstance(database_path, (Path, str)):
            raise AssemblyConflict("capability resolver path is invalid")
        if not isinstance(trust_store, IntegrityTrustStore):
            raise AssemblyConflict("capability resolver trust authority is invalid")
        self._database_path = Path(database_path).expanduser().resolve(strict=False)
        self._admission_verifier = CapabilityAdmissionVerifier(trust_store)

    def resolve_current(
        self, competitor_id: StableIdentifier, context_digest: str
    ) -> RollingCapabilityAuthority | None:
        competitor = StableIdentifier(str(competitor_id))
        aggregate_id = deterministic_identifier(
            "competitor",
            {
                "competitor_id": str(competitor),
                "context_digest": context_digest,
            },
        )
        with open_v3_connection(self._database_path, read_only=True) as connection:
            connection.execute("BEGIN")
            head = connection.execute(
                "SELECT aggregate_version,event_digest FROM v3_aggregate_heads "
                "WHERE aggregate_kind='competitor' AND aggregate_id=?",
                (str(aggregate_id),),
            ).fetchone()
            rows = connection.execute(
                "SELECT global_sequence,event_id,aggregate_kind,aggregate_id,"
                "aggregate_version,event_kind,envelope_json,event_digest,"
                "prior_global_digest,prior_aggregate_digest,occurred_at_utc,command_id "
                "FROM v3_events WHERE aggregate_kind='competitor' AND aggregate_id=? "
                "ORDER BY aggregate_version LIMIT ?",
                (str(aggregate_id), self._MAX_LINEAGE_ROWS + 1),
            ).fetchall()
            if not rows:
                if head is not None:
                    raise AssemblyConflict("capability head exists without its event authority")
                return None
            if len(rows) > self._MAX_LINEAGE_ROWS:
                raise AssemblyConflict("capability lineage exceeds admitted capacity")
            evidence_rows = []
            previous_digest = "0" * 64
            final_receipt = None
            for expected_version, row in enumerate(rows, start=1):
                event = self._decode_capability_event(connection, row)
                if (
                    event.aggregate_version != expected_version
                    or event.prior_aggregate_digest != previous_digest
                ):
                    raise AssemblyConflict("capability aggregate chain differs")
                payload = event.command.payload
                if not isinstance(payload, InlinePayload):
                    raise AssemblyConflict("capability authority must remain inline")
                value = payload.to_value()
                if (
                    value.get("schema_version") != CAPABILITY_REACTION_SCHEMA_VERSION
                    or not isinstance(value.get("admission_manifest"), dict)
                    or not isinstance(value.get("evidence"), dict)
                    or not isinstance(value.get("receipt"), dict)
                ):
                    raise AssemblyConflict("capability authority payload is malformed")
                evidence = CapabilityEvidence.from_dict(value["evidence"])
                receipt = CapabilityReactionReceipt.from_dict(value["receipt"])
                sealed = SealedCapabilityAdmission.from_dict(value["admission_manifest"])
                verified_evidence = self._admission_verifier.verify(sealed)
                before = replay_capability(tuple(evidence_rows))
                after = replay_capability((*evidence_rows, evidence))
                if (
                    evidence.competitor_id != competitor
                    or evidence != verified_evidence
                    or evidence.context_digest != context_digest
                    or receipt.competitor_id != competitor
                    or receipt.context_digest != context_digest
                    or receipt.source_global_sequence != evidence.source_global_sequence
                    or receipt.governor_receipt_digest != canonical_digest(sealed.to_dict())
                    or receipt.event_kind != event.kind.value
                    or receipt.before_state
                    != (None if before is None else CapabilityStateSnapshot.from_state(before))
                    or receipt.after_state != after
                    or not receipt.capacity.admitted
                ):
                    raise AssemblyConflict("capability receipt replay differs")
                evidence_rows.append(evidence)
                final_receipt = receipt
                previous_digest = event.event_digest
            if (
                head is None
                or int(head[0]) != len(rows)
                or str(head[1]) != previous_digest
                or final_receipt is None
                or final_receipt.after_state is None
            ):
                raise AssemblyConflict("capability aggregate head differs")
            state = final_receipt.after_state
            return RollingCapabilityAuthority(
                state,
                RollingCapabilityBinding.create(
                    competitor_id=state.competitor_id,
                    context_digest=state.context_digest,
                    state_revision=state.state_revision,
                    state_digest=state.state_digest,
                    aggregate_version=int(head[0]),
                    aggregate_event_digest=str(head[1]),
                ),
            )

    def verify_current(self, capabilities: tuple[RollingCapabilityBinding, ...]) -> None:
        if not isinstance(capabilities, tuple) or not all(
            isinstance(item, RollingCapabilityBinding) for item in capabilities
        ):
            raise AssemblyConflict("capability currentness bindings are invalid")
        with open_v3_connection(self._database_path, read_only=True) as connection:
            connection.execute("BEGIN")
            for binding in capabilities:
                head = connection.execute(
                    "SELECT aggregate_version,event_digest FROM v3_aggregate_heads "
                    "WHERE aggregate_kind='competitor' AND aggregate_id=?",
                    (str(binding.aggregate_id),),
                ).fetchone()
                if (
                    head is None
                    or int(head[0]) != binding.aggregate_version
                    or str(head[1]) != binding.aggregate_event_digest
                ):
                    raise AssemblyConflict("capability authority is no longer current")

    def resolve_override_current(
        self, field: FrozenFieldRevision, competitor_id: StableIdentifier
    ) -> AcceptedExpectedTimeOverrideState | None:
        if not isinstance(field, FrozenFieldRevision):
            raise AssemblyConflict("override resolver requires a frozen field")
        competitor = StableIdentifier(str(competitor_id))
        try:
            with open_v3_connection(self._database_path, read_only=True) as connection:
                states = verified_expected_time_override_chain(
                    connection,
                    competitor_id=competitor,
                    tournament_id=field.tournament_id,
                )
        except ProjectionError as exc:
            raise AssemblyConflict("accepted override differs from authority") from exc
        if not states:
            return None
        state = states[-1]
        return (
            state
            if state.applies_to(
                tournament_id=field.tournament_id,
                field_id=field.field_id,
                target_context_digest=field.target_context.digest,
                call_order=field.call_order,
            )
            else None
        )

    def verify_override_current(
        self, overrides: tuple[AcceptedExpectedTimeOverrideState, ...]
    ) -> None:
        if not isinstance(overrides, tuple) or not all(
            isinstance(item, AcceptedExpectedTimeOverrideState) for item in overrides
        ):
            raise AssemblyConflict("accepted override bindings are invalid")
        with open_v3_connection(self._database_path, read_only=True) as connection:
            for state in overrides:
                try:
                    current = verified_expected_time_override_chain(
                        connection,
                        competitor_id=state.competitor_id,
                        tournament_id=state.tournament_id,
                    )
                except ProjectionError as exc:
                    raise AssemblyConflict("accepted override differs from authority") from exc
                if not current or current[-1] != state:
                    raise AssemblyConflict("accepted override authority is no longer current")

    @staticmethod
    def _decode_capability_event(connection: object, row: object) -> EventEnvelope:
        try:
            raw = str(row[6])
            value = json.loads(raw)
            event = EventEnvelope.from_dict(value)
            expected_id = deterministic_identifier(
                "event",
                {
                    "command_digest": canonical_digest(event.command.to_dict()),
                    "aggregate_id": str(event.aggregate_id),
                    "aggregate_version": event.aggregate_version,
                    "event_kind": event.kind.value,
                },
            )
            persisted = (
                event.global_sequence,
                str(event.event_id),
                event.aggregate_kind.value,
                str(event.aggregate_id),
                event.aggregate_version,
                event.kind.value,
                canonical_bytes(event.to_dict()).decode("utf-8"),
                event.event_digest,
                event.prior_global_digest,
                event.prior_aggregate_digest,
                event.occurred_at_utc,
                str(event.command.command_id),
            )
            if (
                raw != canonical_bytes(value).decode("utf-8")
                or tuple(row) != persisted
                or event.event_id != expected_id
                or event.aggregate_kind is not AggregateKind.COMPETITOR
                or event.kind
                not in {
                    EventKind.CAPABILITY_UPDATED,
                    EventKind.CAPABILITY_STATE_REBASED,
                }
            ):
                raise ValueError("event material differs")
            prior = connection.execute(
                "SELECT event_digest FROM v3_events WHERE global_sequence=?",
                (event.global_sequence - 1,),
            ).fetchone()
            expected_prior = "0" * 64 if prior is None else str(prior[0])
            if event.prior_global_digest != expected_prior:
                raise ValueError("global predecessor differs")
            idem = connection.execute(
                "SELECT principal_id,command_digest,result_json,result_digest,"
                "first_global_sequence,last_global_sequence,event_set_digest "
                "FROM v3_idempotency_records WHERE idempotency_key=?",
                (str(event.command.command_id),),
            ).fetchone()
            result = None if idem is None else json.loads(str(idem[2]))
            expected_event_set = canonical_digest(
                {
                    "schema_version": "strathmark-v3-event-set-v1",
                    "events": [
                        {
                            "global_sequence": event.global_sequence,
                            "event_id": str(event.event_id),
                            "event_digest": event.event_digest,
                        }
                    ],
                }
            )
            if (
                idem is None
                or str(idem[0]) != str(event.command.actor_id)
                or str(idem[1]) != canonical_digest(event.command.to_dict())
                or not isinstance(result, dict)
                or canonical_bytes(result).decode("utf-8") != str(idem[2])
                or canonical_digest(result) != str(idem[3])
                or int(idem[4]) != event.global_sequence
                or int(idem[5]) != event.global_sequence
                or str(idem[6]) != expected_event_set
            ):
                raise ValueError("idempotency authority differs")
            return event
        except Exception as exc:
            raise AssemblyConflict("capability event authority differs") from exc


class RollingFieldAuthorityVerifierPort(Protocol):
    def verify_current_field(self, field: FrozenFieldRevision) -> None: ...

    def verify_card_authority(self, card: CompetitorCardAuthority) -> None: ...

    def verify_weight_authority(self, authority: OperationalWeightAuthority) -> None: ...

    def verify_dependence_authority(self, artifact: DependenceArtifact) -> None: ...


@dataclass(frozen=True, slots=True)
class _RollingPipelineProof:
    token: object
    pipeline: SealedPipelineOutput
    publications: tuple[RollingPublicationBinding, ...]
    capabilities: tuple[RollingCapabilityBinding, ...]


@dataclass(frozen=True, slots=True)
class RollingPipelineBuild:
    pipeline: SealedPipelineOutput
    publications: tuple[RollingPublicationBinding, ...]
    capabilities: tuple[RollingCapabilityBinding, ...]
    _proof: InitVar[_RollingPipelineProof | None] = None

    def __post_init__(self, _proof: _RollingPipelineProof | None) -> None:
        if not _accepts_rolling_pipeline_build(
            _proof, self.pipeline, self.publications, self.capabilities
        ):
            raise AssemblyConflict("rolling pipeline build lacks same-call verified authority")
        if (
            self.pipeline.rolling_publications != self.publications
            or self.pipeline.capability_bindings != self.capabilities
        ):
            raise AssemblyConflict("rolling pipeline build bindings differ")


def _install_rolling_pipeline_build_capability():
    token = object()

    def construct(pipeline: SealedPipelineOutput) -> RollingPipelineBuild:
        publications = pipeline.rolling_publications
        capabilities = pipeline.capability_bindings
        return RollingPipelineBuild(
            pipeline,
            publications,
            capabilities,
            _RollingPipelineProof(token, pipeline, publications, capabilities),
        )

    def accepts(
        proof: _RollingPipelineProof | None,
        pipeline: SealedPipelineOutput,
        publications: tuple[RollingPublicationBinding, ...],
        capabilities: tuple[RollingCapabilityBinding, ...],
    ) -> bool:
        return (
            isinstance(proof, _RollingPipelineProof)
            and proof.token is token
            and proof.pipeline is pipeline
            and proof.publications is publications
            and proof.capabilities is capabilities
        )

    return construct, accepts


(
    _construct_rolling_pipeline_build,
    _accepts_rolling_pipeline_build,
) = _install_rolling_pipeline_build_capability()
del _install_rolling_pipeline_build_capability


def unwrap_rolling_pipeline_build(value: object) -> SealedPipelineOutput:
    if not isinstance(value, RollingPipelineBuild):
        raise AssemblyConflict("configured builder did not return rolling authority")
    # Construction already proved same-call identity; recheck immutable bindings here.
    if (
        value.pipeline.rolling_publications != value.publications
        or value.pipeline.capability_bindings != value.capabilities
    ):
        raise AssemblyConflict("configured rolling pipeline authority differs")
    return value.pipeline


class CoordinatorRollingFieldInputSource:
    """Production adapter over the current rolling coordinator and U12 authorities."""

    def __init__(
        self,
        coordinator: object,
        *,
        authority_verifier: RollingFieldAuthorityVerifierPort,
        capability_resolver: CapabilityStateResolverPort,
        weight_receipt: WeightReceipt,
        operational_weight_authority: OperationalWeightAuthority,
        dependence_artifact: DependenceArtifact,
        disagreement_policy: DisagreementPolicy,
        formula_manifest: FormulaManifest | None = None,
        zero_history_policy: ZeroHistoryPolicy | None = None,
    ) -> None:
        if (
            not callable(getattr(coordinator, "current_publications_for_field", None))
            or not callable(getattr(authority_verifier, "verify_current_field", None))
            or not callable(getattr(authority_verifier, "verify_card_authority", None))
            or not callable(getattr(authority_verifier, "verify_weight_authority", None))
            or not callable(getattr(authority_verifier, "verify_dependence_authority", None))
            or not callable(getattr(capability_resolver, "resolve_current", None))
            or not callable(getattr(capability_resolver, "verify_current", None))
            or not callable(getattr(capability_resolver, "resolve_override_current", None))
            or not callable(getattr(capability_resolver, "verify_override_current", None))
            or not isinstance(weight_receipt, WeightReceipt)
            or not isinstance(operational_weight_authority, OperationalWeightAuthority)
            or not isinstance(dependence_artifact, DependenceArtifact)
            or not isinstance(disagreement_policy, DisagreementPolicy)
        ):
            raise AssemblyConflict("rolling input source dependencies are invalid")
        self._coordinator = coordinator
        self._authority_verifier = authority_verifier
        self._capability_resolver = capability_resolver
        self._weight_receipt = weight_receipt
        self._operational_weight_authority = operational_weight_authority
        self._dependence_artifact = dependence_artifact
        self._disagreement_policy = disagreement_policy
        self._formula_manifest = formula_manifest
        self._zero_history_policy = zero_history_policy

    def load_current(self, field: FrozenFieldRevision) -> RollingFieldBuildInputs:
        self._verify_installed(field)
        publications = self._coordinator.current_publications_for_field(field)
        cards = tuple(RollingCurrentCard.from_publication(item) for item in publications)
        for item in cards:
            self._authority_verifier.verify_card_authority(item.card)
        resolved = tuple(
            self._capability_resolver.resolve_current(
                assignment.competitor_id, field.target_context.digest
            )
            for assignment in field.ordered_assignments
        )
        capabilities = tuple(item for item in resolved if item is not None)
        overrides = tuple(
            item
            for assignment in field.ordered_assignments
            if (
                item := self._capability_resolver.resolve_override_current(
                    field, assignment.competitor_id
                )
            )
            is not None
        )
        return RollingFieldBuildInputs(
            cards,
            self._weight_receipt,
            self._operational_weight_authority,
            self._dependence_artifact,
            capabilities,
            self._disagreement_policy,
            self._formula_manifest,
            self._zero_history_policy,
            overrides,
        )

    def verify_current(
        self,
        field: FrozenFieldRevision,
        publications: tuple[RollingPublicationBinding, ...],
        capabilities: tuple[RollingCapabilityBinding, ...],
        overrides: tuple[AcceptedExpectedTimeOverrideState, ...],
    ) -> None:
        self._verify_installed(field)
        current = tuple(
            RollingCurrentCard.from_publication(item).publication
            for item in self._coordinator.current_publications_for_field(field)
        )
        if current != publications:
            raise AssemblyConflict("rolling publication changed during field build")
        self._capability_resolver.verify_current(capabilities)
        self._capability_resolver.verify_override_current(overrides)

    def _verify_installed(self, field: FrozenFieldRevision) -> None:
        self._authority_verifier.verify_current_field(field)
        self._authority_verifier.verify_weight_authority(self._operational_weight_authority)
        self._authority_verifier.verify_dependence_authority(self._dependence_artifact)

    def pre_field_source(self):
        """Expose only the authorities required for field-independent forecasts."""

        from strathmark.v3.application.pre_field_forecasts import (
            CoordinatorPreFieldForecastInputSource,
        )

        return CoordinatorPreFieldForecastInputSource(
            self._coordinator,
            capability_resolver=self._capability_resolver,
            authority_verifier=self._authority_verifier,
            weight_receipt=self._weight_receipt,
            weight_authority=self._operational_weight_authority,
        )


class RollingFieldPipelineBuilder:
    """Build one ordinary field from exact current rolling card authority."""

    def __init__(
        self,
        source: RollingFieldInputPort,
        *,
        signer: P256Signer,
        trust_store: IntegrityTrustStore,
        clock: Callable[[], str],
        monotonic_ns: Callable[[], int] = perf_counter_ns,
    ) -> None:
        if (
            not callable(getattr(source, "load_current", None))
            or not callable(getattr(source, "verify_current", None))
            or not callable(getattr(signer, "sign", None))
            or not isinstance(trust_store, IntegrityTrustStore)
            or not callable(clock)
            or not callable(monotonic_ns)
        ):
            raise AssemblyConflict("rolling field builder dependencies are invalid")
        trust_store.identity(signer.identity.key_id)
        self._source = source
        self._signer = signer
        self._trust_store = trust_store
        self._clock = clock
        self._monotonic_ns = monotonic_ns

    def pre_field_source(self):
        provider = getattr(self._source, "pre_field_source", None)
        if not callable(provider):
            raise AssemblyConflict("rolling builder has no pre-field forecast source")
        return provider()

    def __call__(
        self, field: FrozenFieldRevision
    ) -> RollingPipelineBuild | ManualActionRequirement:
        started_ns = self._monotonic_ns()
        if isinstance(started_ns, bool) or not isinstance(started_ns, int):
            raise AssemblyConflict("rolling field monotonic clock is invalid")
        if not isinstance(field, FrozenFieldRevision):
            raise AssemblyConflict("rolling field builder requires a frozen field")
        inputs = self._source.load_current(field)
        if not isinstance(inputs, RollingFieldBuildInputs):
            raise AssemblyConflict("rolling field input source returned untyped authority")
        cards = self._verify_inputs(field, inputs)
        publications = tuple(item.publication for item in cards)
        capabilities = tuple(item.binding for item in inputs.capability_authorities)
        self._source.verify_current(field, publications, capabilities, inputs.override_states)
        requirement = self._manual_action_requirement(field, inputs, cards)
        if requirement is not None:
            self._source.verify_current(field, publications, capabilities, inputs.override_states)
            return requirement
        pipeline = self._build(field, inputs, cards, started_ns=started_ns)
        self._source.verify_current(field, publications, capabilities, inputs.override_states)
        return _construct_rolling_pipeline_build(pipeline)

    def _verify_inputs(
        self, field: FrozenFieldRevision, inputs: RollingFieldBuildInputs
    ) -> tuple[RollingCurrentCard, ...]:
        by_id = {item.card.competitor_id: item for item in inputs.cards}
        expected = tuple(item.competitor_id for item in field.ordered_assignments)
        if set(by_id) != set(expected) or len(by_id) != len(expected):
            raise AssemblyConflict("rolling current publication roster differs")
        ordered = tuple(by_id[item] for item in expected)
        for assignment, current in zip(field.ordered_assignments, ordered, strict=True):
            card = current.card
            packet = card.evidence_packet
            binding = current.publication
            if (
                binding.competitor_id != assignment.competitor_id
                or binding.target_context_digest != field.target_context.digest
                or binding.historical_cutoff_key != field.historical_cutoff_key
                or binding.tournament_epoch_id != field.tournament_epoch_id
                or binding.bundle_digest != field.bundle_digest
                or binding.hard_deadline_at != field.deadline_at
                or packet.target_context != field.target_context
                or str(packet.historical_cutoff_key) != str(field.historical_cutoff_key)
                or str(packet.tournament_epoch_id) != str(field.tournament_epoch_id)
                or packet.tournament_event_sequence != field.tournament_event_sequence
                or card.bundle_digest != field.bundle_digest
            ):
                raise AssemblyConflict("rolling current publication differs from field")
            if verify_manifest(card.manifest, self._trust_store) != card.content_value():
                raise AssemblyConflict("rolling card manifest is untrusted")
            publication_payload = verify_manifest(current.publication_manifest, self._trust_store)
            if (
                publication_payload.get("card_key") != binding.card_key_value()
                or publication_payload.get("publication_digest") != binding.publication_digest
                or publication_payload.get("card_authority_digest")
                != canonical_digest(card.to_dict())
                or publication_payload.get("component_refs_digest") != binding.component_refs_digest
                or publication_payload.get("availability")
                != [list(item) for item in binding.availability]
                or publication_payload.get("council_manifest_digest")
                != binding.council_manifest_digest
                or publication_payload.get("council_aggregate_manifest_digest")
                != binding.council_aggregate_manifest_digest
                or publication_payload.get("hard_deadline_at") != binding.hard_deadline_at
                or publication_payload.get("sealed_at") != binding.sealed_at
                or current.council_aggregate_manifest.body_digest
                != binding.council_aggregate_manifest_digest
            ):
                raise AssemblyConflict("rolling publication manifest differs")
            aggregate = verify_manifest(current.council_aggregate_manifest, self._trust_store)
            if (
                aggregate.get("card_digest") != binding.card_digest
                or aggregate.get("council_manifest_digest") != binding.council_manifest_digest
                or aggregate.get("aggregate_forecast_commit_digest")
                != card.forecasts[2].commit_digest
            ):
                raise AssemblyConflict("rolling council aggregate differs from card")
        authority = inputs.operational_weight_authority
        binding = authority.binding
        receipt = inputs.weight_receipt
        if (
            binding.weights != receipt.weights
            or binding.context != receipt.context
            or binding.weight_receipt_digest != receipt.receipt_digest
            or binding.calibration_cutoff_at_utc != receipt.calibration_cutoff_at_utc
            or binding.policy_digest != receipt.policy_digest
            or authority.tournament_id != field.tournament_id
            or authority.round_id != field.round_id
            or authority.epoch_id != field.tournament_epoch_id
            or authority.epoch_digest != field.evidence_digest
            or authority.frozen_tournament_sequence != field.tournament_event_sequence
            or inputs.dependence_artifact.target_context != receipt.context
        ):
            raise AssemblyConflict("rolling weight or dependence authority differs")
        capabilities = {item.state.competitor_id: item for item in inputs.capability_authorities}
        overrides = {item.competitor_id: item for item in inputs.override_states}
        if any(
            item.state.context_digest != field.target_context.digest
            for item in capabilities.values()
        ):
            raise AssemblyConflict("rolling capability context differs from field")
        return ordered

    def _manual_action_requirement(
        self,
        field: FrozenFieldRevision,
        inputs: RollingFieldBuildInputs,
        cards: tuple[RollingCurrentCard, ...],
    ) -> ManualActionRequirement | None:
        available = tuple(
            tuple(
                forecast.assessor
                for forecast in current.card.forecasts
                if forecast.state is ForecastState.COMMITTED
            )
            for current in cards
        )
        if available and len(set(available)) == 1 and len(available[0]) >= 2:
            return None
        created_at = require_utc_milliseconds(self._clock())
        if created_at < require_utc_milliseconds(field.deadline_at):
            raise AssemblyConflict("degraded rolling field awaits its hard-deadline manual action")
        capabilities = {item.state.competitor_id: item for item in inputs.capability_authorities}
        exact_single = (
            bool(available)
            and all(len(items) == 1 for items in available)
            and len({items[0] for items in available}) == 1
        )
        entrants = []
        for current, sources in zip(cards, available, strict=True):
            candidate_digest = None
            if exact_single:
                forecast = next(
                    item for item in current.card.forecasts if item.assessor is sources[0]
                )
                capability = capabilities.get(current.card.competitor_id)
                if capability is None:
                    candidate_digest = self._zero_history_basis(
                        field, inputs, current, created_at=created_at
                    ).authority_digest
                else:
                    candidate_digest = canonical_digest(
                        {
                            "schema_version": ("strathmark-v3-single-survivor-candidate-basis-v1"),
                            "competitor_id": str(current.card.competitor_id),
                            "source_assessor": sources[0].value,
                            "forecast_commit_digest": forecast.commit_digest,
                            "publication_binding_digest": (current.publication.binding_digest),
                            "capability_binding_digest": (capability.binding.binding_digest),
                            "weight_authority_digest": (
                                inputs.operational_weight_authority.authority_digest
                            ),
                        }
                    )
            entrants.append(
                ManualActionEntrant(
                    current.card.competitor_id,
                    sources,
                    current.publication.binding_digest,
                    candidate_digest,
                )
            )
        return create_manual_action_requirement(
            field_id=field.field_id,
            upstream_field_revision=field.field_revision,
            field_revision_digest=field.revision_digest,
            target_context_digest=field.target_context.digest,
            historical_cutoff_key=field.historical_cutoff_key,
            tournament_epoch_id=field.tournament_epoch_id,
            bundle_digest=field.bundle_digest,
            hard_deadline_at=field.deadline_at,
            entrants=tuple(entrants),
            signer=self._signer,
            created_at=created_at,
        )

    def _build(
        self,
        field: FrozenFieldRevision,
        inputs: RollingFieldBuildInputs,
        current_cards: tuple[RollingCurrentCard, ...],
        *,
        started_ns: int,
    ) -> SealedPipelineOutput:
        created_at = require_utc_milliseconds(self._clock())
        authority = inputs.operational_weight_authority.binding
        model = bind_field_dependence(
            inputs.dependence_artifact,
            inputs.weight_receipt.context,
            field_id=field.field_id,
        )
        crn_source_digest = canonical_digest(
            {
                "schema_version": "strathmark-v3-field-crn-source-v1",
                "field_revision_digest": field.revision_digest,
                "dependence_artifact_digest": inputs.dependence_artifact.artifact_digest,
                "weight_authority_digest": authority.binding_digest,
            }
        )
        seed = int(crn_source_digest[:16], 16) & ((1 << 63) - 1)
        cards = {item.card.competitor_id: item.card for item in current_cards}
        capabilities = {item.state.competitor_id: item for item in inputs.capability_authorities}
        overrides = {item.competitor_id: item for item in inputs.override_states}
        slot_basis = tuple(
            FieldCompetitorForecast(
                assignment.competitor_id,
                str(assignment.stand_id),
                next(
                    forecast.distribution
                    for forecast in cards[assignment.competitor_id].forecasts
                    if forecast.state is ForecastState.COMMITTED
                    and forecast.distribution is not None
                ),
                assignment.crn_index,
            )
            for assignment in field.ordered_assignments
        )
        uniform_plan = generate_joint_uniforms(
            slot_basis,
            model,
            installed_artifact=inputs.dependence_artifact,
            seed=seed,
            draw_count=4096,
        )
        prediction_evidence = []
        for assignment, current in zip(field.ordered_assignments, current_cards, strict=True):
            capability = capabilities.get(current.card.competitor_id)
            if capability is None:
                basis = self._zero_history_basis(field, inputs, current, created_at=created_at)
            else:
                pool = pool_forecasts(
                    current.card.forecasts,
                    inputs.weight_receipt,
                    capability.state,
                    uniform_plan.sampling_spec(str(assignment.stand_id)),
                    weight_authority=authority,
                )
                basis = CapabilityPoolBasis(pool, capability.binding)
            override = overrides.get(current.card.competitor_id)
            if override is not None:
                if not override.applies_to(
                    tournament_id=field.tournament_id,
                    field_id=field.field_id,
                    target_context_digest=field.target_context.digest,
                    call_order=field.call_order,
                ):
                    raise AssemblyConflict("accepted override scope differs from field")
                basis = OverrideStartingEstimateBasis.create(override, basis)
            prediction_evidence.append(
                CompetitorPredictionEvidence(
                    current.card,
                    current.publication,
                    basis,
                )
            )
        prediction_evidence = tuple(prediction_evidence)
        pools = tuple(
            CompetitorPoolEvidence(
                item.card,
                (
                    item.basis.source_basis.pool
                    if isinstance(item.basis, OverrideStartingEstimateBasis)
                    and isinstance(item.basis.source_basis, CapabilityPoolBasis)
                    else item.basis.pool
                ),
            )
            for item in prediction_evidence
            if isinstance(item.basis, CapabilityPoolBasis)
            or (
                isinstance(item.basis, OverrideStartingEstimateBasis)
                and isinstance(item.basis.source_basis, CapabilityPoolBasis)
            )
        )
        available_sets = tuple(
            tuple(
                component.assessor
                for component in item.pool.receipt.components
                if component.availability.value == "valid"
            )
            for item in pools
        )
        all_capability = len(pools) == len(prediction_evidence)
        if all_capability and (
            not available_sets or len(set(available_sets)) != 1 or len(available_sets[0]) < 2
        ):
            raise AssemblyConflict(
                "rolling ordinary builder requires one consistent two-or-three assessor set"
            )
        available = available_sets[0] if all_capability else ()
        forecasts = tuple(
            FieldCompetitorForecast(
                assignment.competitor_id,
                str(assignment.stand_id),
                evidence.distribution,
                assignment.crn_index,
            )
            for assignment, evidence in zip(
                field.ordered_assignments, prediction_evidence, strict=True
            )
        )
        if all_capability and not inputs.override_states:
            joint = generate_joint_draws_from_pool_results(
                forecasts,
                tuple(item.pool for item in pools),
                model,
                installed_artifact=inputs.dependence_artifact,
                seed=seed,
                draw_count=4096,
                uniform_plan=uniform_plan,
            )
        else:
            joint = generate_joint_draws(
                forecasts,
                model,
                installed_artifact=inputs.dependence_artifact,
                seed=seed,
                draw_count=4096,
                uniform_plan=uniform_plan,
            )
        basis_digests = [
            (
                item.basis.pool.receipt.receipt_digest
                if isinstance(item.basis, CapabilityPoolBasis)
                else (
                    item.basis.authority_digest
                    if isinstance(item.basis, ZeroHistoryPriorBasis)
                    else item.basis.basis_digest
                )
            )
            for item in prediction_evidence
        ]
        pool_digest = canonical_digest(
            {
                "prediction_basis_digests": basis_digests,
                "manual_authority_digest": None,
            }
        )
        source_digest = canonical_digest(
            {
                "field": field.revision_digest,
                "prediction_basis_digests": basis_digests,
                "manual_authority_digest": None,
            }
        )
        optimizer = optimize_and_verify_field(
            OptimizationField.from_joint_draws(
                joint,
                forecasts=forecasts,
                source_receipt_digest=source_digest,
                pool_receipt_digest=pool_digest,
            ),
            ceiling=183,
        )
        if not all_capability:
            completed_ns = self._monotonic_ns()
            if (
                isinstance(completed_ns, bool)
                or not isinstance(completed_ns, int)
                or completed_ns < started_ns
            ):
                raise AssemblyConflict("rolling field monotonic clock moved backwards")
            return SealedPipelineOutput.create(
                field_revision_digest=field.revision_digest,
                prediction_evidence=prediction_evidence,
                joint_draws=joint,
                optimizer=optimizer,
                disagreement=None,
                weight_authority=authority,
                operational_weight_authority=inputs.operational_weight_authority,
                dependence_artifact=inputs.dependence_artifact,
                total_latency_ms=(completed_ns - started_ns + 999_999) // 1_000_000,
            )
        slots = [
            [str(row.competitor_id), row.draw_slot, row.crn_index] for row in joint.competitors
        ]
        component_inputs = []
        for source in available:
            index = _OUTER.index(source)
            component_forecasts = tuple(
                FieldCompetitorForecast(
                    assignment.competitor_id,
                    str(assignment.stand_id),
                    cards[assignment.competitor_id].forecasts[index].distribution,
                    assignment.crn_index,
                )
                for assignment in field.ordered_assignments
            )
            commits = [
                cards[item.competitor_id].forecasts[index].commit_digest
                for item in field.ordered_assignments
            ]
            component_pool_digest = canonical_digest(
                {
                    "schema_version": "strathmark-v3-component-card-set-v1",
                    "source": source.value,
                    "forecast_commit_digests": commits,
                }
            )
            component_source_digest = canonical_digest(
                {
                    "schema_version": "strathmark-v3-component-counterfactual-source-v1",
                    "field_revision_digest": field.revision_digest,
                    "source": source.value,
                    "card_pool_digest": component_pool_digest,
                    "dependence_artifact_digest": inputs.dependence_artifact.artifact_digest,
                    "crn_slots": slots,
                }
            )
            component_inputs.append(
                (
                    source,
                    component_forecasts,
                    component_source_digest,
                    component_pool_digest,
                )
            )
        component_draws = generate_aligned_component_joint_draws(
            tuple(item[1] for item in component_inputs),
            model,
            installed_artifact=inputs.dependence_artifact,
            seed=seed,
            draw_count=4096,
            uniform_plan=uniform_plan,
        )
        component_optimizers = tuple(
            (
                source,
                optimize_and_verify_field(
                    OptimizationField.from_joint_draws(
                        draws,
                        forecasts=component_forecasts,
                        source_receipt_digest=component_source_digest,
                        pool_receipt_digest=component_pool_digest,
                    ),
                    ceiling=183,
                ),
            )
            for (
                source,
                component_forecasts,
                component_source_digest,
                component_pool_digest,
            ), draws in zip(component_inputs, component_draws, strict=True)
        )
        component_sheets = tuple(
            counterfactual_sheet_from_optimizer(source, receipt)
            for source, receipt in component_optimizers
        )
        council_audit = self._council_audit(field, current_cards, available, component_sheets)
        decision = classify_disagreement(
            counterfactual_sheet_from_optimizer("pooled", optimizer),
            component_sheets,
            council_audit,
            inputs.disagreement_policy,
            available_assessors=available,
        )
        policy_manifest = seal_disagreement_policy_authority(
            inputs.disagreement_policy,
            bundle_digest=field.bundle_digest,
            signer=self._signer,
            created_at=created_at,
        )
        council_manifest = (
            None
            if council_audit is None
            else seal_council_field_audit_authority(
                council_audit,
                field_revision_digest=field.revision_digest,
                card_manifest_digests=tuple(
                    item.card.manifest.body_digest for item in current_cards
                ),
                signer=self._signer,
                created_at=created_at,
            )
        )
        disagreement = OperationalDisagreementReceipt.create(
            field_revision_digest=field.revision_digest,
            decision=decision,
            pooled_optimizer=optimizer,
            component_optimizers=component_optimizers,
            component_joint_draws=tuple(zip(available, component_draws, strict=True)),
            policy_manifest=policy_manifest,
            council_manifest=council_manifest,
        )
        completed_ns = self._monotonic_ns()
        if (
            isinstance(completed_ns, bool)
            or not isinstance(completed_ns, int)
            or completed_ns < started_ns
        ):
            raise AssemblyConflict("rolling field monotonic clock moved backwards")
        latency_ms = (completed_ns - started_ns + 999_999) // 1_000_000
        return SealedPipelineOutput.create(
            field_revision_digest=field.revision_digest,
            prediction_evidence=prediction_evidence,
            joint_draws=joint,
            optimizer=optimizer,
            disagreement=disagreement,
            weight_authority=authority,
            operational_weight_authority=inputs.operational_weight_authority,
            dependence_artifact=inputs.dependence_artifact,
            total_latency_ms=latency_ms,
        )

    def _zero_history_basis(
        self,
        field: FrozenFieldRevision,
        inputs: RollingFieldBuildInputs,
        current: RollingCurrentCard,
        *,
        created_at: str,
    ) -> ZeroHistoryPriorBasis:
        from strathmark.v3.application.capacity import JobKind
        from strathmark.v3.application.coordinator import RollingComponentOutcome

        manifest = inputs.formula_manifest
        policy = inputs.zero_history_policy
        if not isinstance(manifest, FormulaManifest) or not isinstance(policy, ZeroHistoryPolicy):
            raise AssemblyConflict("zero-history entrant lacks pinned Formula and policy authority")
        formula = next(
            (item for item in current.card.forecasts if item.assessor is AssessorKind.FORMULA),
            None,
        )
        if (
            formula is None
            or formula.state is not ForecastState.COMMITTED
            or formula.distribution is None
            or formula.support.eligible_count != 0
            or formula.support.exact_context_count != 0
            or formula.evidence_digest != current.card.packet_digest
        ):
            raise AssemblyConflict(
                "zero-history entrant lacks an exact no-history Formula forecast"
            )
        formula_artifacts = tuple(
            item for item in formula.artifacts if item.role == "formula_manifest"
        )
        resolved = resolve_zero_history_prior(field.target_context, manifest)
        if (
            len(formula_artifacts) != 1
            or formula_artifacts[0].digest != manifest.digest
            or resolved.manifest_digest != manifest.digest
            or formula.distribution != resolved.distribution
        ):
            raise AssemblyConflict("zero-history Formula prior authority differs")
        component = next(
            (
                item
                for item in current.components
                if getattr(item, "component_id", None) == "formula"
            ),
            None,
        )
        if (
            component is None
            or component.job_kind is not JobKind.FORMULA_CARD
            or component.outcome is not RollingComponentOutcome.SUCCEEDED
            or component.result_digest != formula.commit_digest
        ):
            raise AssemblyConflict("zero-history Formula component authority differs")
        estimate = create_zero_history_estimate(
            current.card.competitor_id,
            field.target_context.digest,
            resolved.distribution,
            resolved.prior_lineage_digest,
            policy,
        )
        basis = ZeroHistoryPriorBasis.create(
            estimate=estimate,
            publication_binding_digest=current.publication.binding_digest,
            formula_forecast_digest=formula.commit_digest,
            formula_component_result_digest=component.result_digest,
            formula_component_payload_digest=component.payload_digest,
            formula_manifest_digest=manifest.digest,
            prior_lineage_digest=resolved.prior_lineage_digest,
            zero_history_policy_digest=policy.digest,
            signer=self._signer,
            created_at=created_at,
        )
        if verify_manifest(basis.manifest, self._trust_store) != basis.content_value():
            raise AssemblyConflict("zero-history Formula authority is untrusted")
        return basis

    def _council_audit(
        self,
        field: FrozenFieldRevision,
        cards: tuple[RollingCurrentCard, ...],
        available: tuple[AssessorKind, ...],
        component_sheets: tuple[object, ...],
    ) -> CouncilAudit | None:
        if AssessorKind.LLM_COUNCIL not in available:
            return None
        sheet = component_sheets[available.index(AssessorKind.LLM_COUNCIL)]
        member_rows: dict[str, list[dict[str, object]]] = {}
        for card in cards:
            payload = verify_manifest(card.council_aggregate_manifest, self._trust_store)
            rows = payload.get("member_receipts")
            if not isinstance(rows, list) or len(rows) != 3:
                raise AssemblyConflict("rolling council member receipts differ")
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("member_id"), str):
                    raise AssemblyConflict("rolling council member receipt is invalid")
                member_rows.setdefault(row["member_id"], []).append(row)
        if len(member_rows) != 3 or any(len(rows) != len(cards) for rows in member_rows.values()):
            raise AssemblyConflict("rolling council roster is mixed across cards")
        audits = []
        for member_id, rows in sorted(member_rows.items()):
            outcomes = tuple(str(item.get("outcome")) for item in rows)
            status = (
                CouncilMemberStatus.VALID
                if set(outcomes) == {"succeeded"}
                else (
                    CouncilMemberStatus.INVALID
                    if "invalid" in outcomes
                    else CouncilMemberStatus.FAILED
                )
            )
            audits.append(
                CouncilMemberAudit(
                    StableIdentifier(f"llm_member:{member_id}"),
                    status,
                    canonical_digest(
                        [[item.get("outcome"), item.get("result_digest")] for item in rows]
                    ),
                    canonical_digest(rows),
                )
            )
        council_commits = [item.card.forecasts[2].commit_digest for item in cards]
        return CouncilAudit.create(
            aggregate_sheet=sheet,
            aggregate_forecast_digest=canonical_digest(council_commits),
            evidence_digest=canonical_digest([item.card.packet_digest for item in cards]),
            evidence_epoch_id=field.tournament_epoch_id,
            members=tuple(audits),
        )


__all__ = [
    "RollingCurrentCard",
    "RollingCapabilityAuthority",
    "CapabilityStateResolverPort",
    "SQLiteCapabilityStateResolver",
    "CoordinatorRollingFieldInputSource",
    "RollingFieldBuildInputs",
    "RollingFieldInputPort",
    "RollingFieldPipelineBuilder",
    "RollingPipelineBuild",
    "unwrap_rolling_pipeline_build",
]
