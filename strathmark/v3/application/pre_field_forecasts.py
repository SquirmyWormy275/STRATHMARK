"""Field-independent marginal forecasts for deterministic tournament seeding."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from strathmark.v3.application.field_assembly import (
    OperationalWeightAuthority,
    RollingCapabilityBinding,
)
from strathmark.v3.application.job_ports import DurableJobError
from strathmark.v3.application.pipeline_builder import (
    RollingCapabilityAuthority,
    RollingCurrentCard,
)
from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.forecasts import AssessorKind, ForecastState, SamplingSpec
from strathmark.v3.contracts.pre_field_forecasts import (
    ForecastSetSnapshot,
    PreFieldCompetitorForecast,
    PreFieldForecastReceipt,
)
from strathmark.v3.domain.credibility import WeightReceipt
from strathmark.v3.domain.pooling import WeightAuthorityBinding, pool_forecasts
from strathmark.v3.infrastructure.integrity import (
    IntegrityKeyIdentity,
    IntegrityTrustStore,
    P256Signer,
)
from strathmark.v3.infrastructure.sqlite.connection import (
    immediate_transaction,
    open_v3_connection,
)


class PreFieldForecastError(RuntimeError):
    """Pre-field forecast authority is unavailable, stale, or conflicting."""


@dataclass(frozen=True, slots=True)
class PreFieldForecastInputs:
    cards: tuple[RollingCurrentCard, ...]
    capabilities: tuple[RollingCapabilityAuthority | None, ...]
    weight_receipt: WeightReceipt
    weight_authority: WeightAuthorityBinding

    def __post_init__(self) -> None:
        if (
            not isinstance(self.cards, tuple)
            or not isinstance(self.capabilities, tuple)
            or len(self.cards) != len(self.capabilities)
            or not isinstance(self.weight_receipt, WeightReceipt)
            or not isinstance(self.weight_authority, WeightAuthorityBinding)
            or self.weight_authority.weight_receipt_digest != self.weight_receipt.receipt_digest
        ):
            raise PreFieldForecastError("pre-field forecast inputs are invalid")


class PreFieldForecastInputPort(Protocol):
    def prepare(self, snapshot: ForecastSetSnapshot, *, observed_at: str) -> None: ...

    def load_current(self, snapshot: ForecastSetSnapshot) -> PreFieldForecastInputs: ...

    def verify_current(
        self,
        snapshot: ForecastSetSnapshot,
        publication_digests: tuple[str, ...],
        capability_bindings: tuple[RollingCapabilityBinding, ...],
    ) -> None: ...


class UnavailablePreFieldForecastInputSource:
    """Fail-closed source for deliberately non-predictive test compositions."""

    def prepare(self, _snapshot: ForecastSetSnapshot, *, observed_at: str) -> None:
        raise PreFieldForecastError("pre-field forecast source is unavailable")

    def load_current(self, _snapshot: ForecastSetSnapshot) -> PreFieldForecastInputs:
        raise PreFieldForecastError("pre-field forecast source is unavailable")

    def verify_current(
        self,
        _snapshot: ForecastSetSnapshot,
        _publication_digests: tuple[str, ...],
        _capability_bindings: tuple[RollingCapabilityBinding, ...],
    ) -> None:
        raise PreFieldForecastError("pre-field forecast source is unavailable")


class CoordinatorPreFieldForecastInputSource:
    """Resolve exact epoch/bundle rolling publications without inventing a field."""

    def __init__(
        self,
        coordinator: object,
        *,
        capability_resolver: object,
        authority_verifier: object,
        weight_receipt: WeightReceipt,
        weight_authority: OperationalWeightAuthority,
    ) -> None:
        if (
            not callable(getattr(coordinator, "publications_for_forecast", None))
            or not callable(getattr(coordinator, "schedule_forecast", None))
            or not callable(getattr(capability_resolver, "resolve_current", None))
            or not callable(getattr(capability_resolver, "verify_current", None))
            or not callable(getattr(authority_verifier, "verify_card_authority", None))
            or not callable(getattr(authority_verifier, "verify_weight_authority", None))
        ):
            raise PreFieldForecastError("pre-field source dependencies are invalid")
        self._coordinator = coordinator
        self._capability_resolver = capability_resolver
        self._authority_verifier = authority_verifier
        self._weight_receipt = weight_receipt
        self._operational_weight_authority = weight_authority

    def prepare(self, snapshot: ForecastSetSnapshot, *, observed_at: str) -> None:
        try:
            self._coordinator.publications_for_forecast(snapshot)
        except DurableJobError as exc:
            if str(exc) not in {
                "rolling forecast publication is missing",
                "rolling forecast card is not published",
            }:
                raise
            self._coordinator.schedule_forecast(snapshot, observed_at=observed_at)

    def load_current(self, snapshot: ForecastSetSnapshot) -> PreFieldForecastInputs:
        self._authority_verifier.verify_weight_authority(self._operational_weight_authority)
        publications = self._coordinator.publications_for_forecast(snapshot)
        cards = tuple(RollingCurrentCard.from_publication(item) for item in publications)
        for card in cards:
            self._authority_verifier.verify_card_authority(card.card)
        capabilities = tuple(
            self._capability_resolver.resolve_current(competitor_id, snapshot.target_context.digest)
            for competitor_id in snapshot.ordered_competitor_ids
        )
        return PreFieldForecastInputs(
            cards,
            capabilities,
            self._weight_receipt,
            self._operational_weight_authority.binding,
        )

    def verify_current(
        self,
        snapshot: ForecastSetSnapshot,
        publication_digests: tuple[str, ...],
        capability_bindings: tuple[RollingCapabilityBinding, ...],
    ) -> None:
        current = self._coordinator.publications_for_forecast(snapshot)
        if tuple(item.publication_digest for item in current) != publication_digests:
            raise PreFieldForecastError("pre-field rolling publication changed")
        self._capability_resolver.verify_current(capability_bindings)


class PreFieldForecastService:
    """Create or recover one immutable signed marginal-forecast receipt."""

    def __init__(
        self,
        database_path: Path | str,
        *,
        source: PreFieldForecastInputPort,
        signer: P256Signer,
        trust_store: IntegrityTrustStore,
    ) -> None:
        if isinstance(database_path, bool) or not isinstance(database_path, (Path, str)):
            raise PreFieldForecastError("pre-field database path is invalid")
        if (
            not callable(getattr(source, "prepare", None))
            or not callable(getattr(source, "load_current", None))
            or not callable(getattr(source, "verify_current", None))
            or not callable(getattr(signer, "sign", None))
            or not isinstance(trust_store, IntegrityTrustStore)
        ):
            raise PreFieldForecastError("pre-field service dependencies are invalid")
        trust_store.identity(signer.identity.key_id)
        self.database_path = Path(database_path).expanduser().resolve(strict=False)
        self._source = source
        self._signer = signer
        self._trust_store = trust_store

    @property
    def signer_identity(self) -> IntegrityKeyIdentity:
        """Return public verification material for consumer trust bootstrap."""

        return self._signer.identity

    def forecast(
        self,
        snapshot: ForecastSetSnapshot,
        *,
        caller_namespace: str,
        request_identity: str,
        created_at: str,
    ) -> tuple[PreFieldForecastReceipt, bool]:
        request_digest = canonical_digest(snapshot.to_dict())
        recovered = self._recover(
            caller_namespace=caller_namespace,
            request_identity=request_identity,
            request_digest=request_digest,
        )
        if recovered is not None:
            return recovered, True
        self._source.prepare(snapshot, observed_at=created_at)
        inputs = self._source.load_current(snapshot)
        if (
            tuple(item.card.competitor_id for item in inputs.cards)
            != snapshot.ordered_competitor_ids
        ):
            raise PreFieldForecastError("pre-field cards differ from ordered snapshot roster")
        forecasts = []
        capability_bindings = []
        for ordinal, (current, capability) in enumerate(
            zip(inputs.cards, inputs.capabilities, strict=True)
        ):
            if (
                current.publication.tournament_epoch_id != snapshot.tournament_epoch_id
                or current.publication.bundle_digest != snapshot.bundle_digest
                or current.publication.historical_cutoff_key != snapshot.historical_cutoff_key
                or current.publication.target_context_digest != snapshot.target_context.digest
                or current.card.evidence_packet.tournament_event_sequence
                > snapshot.maximum_tournament_sequence
            ):
                raise PreFieldForecastError("pre-field card is outside frozen causal authority")
            committed = tuple(
                item for item in current.card.forecasts if item.state is ForecastState.COMMITTED
            )
            if capability is None:
                formula = next(
                    (item for item in committed if item.assessor is AssessorKind.FORMULA),
                    None,
                )
                if (
                    formula is None
                    or formula.distribution is None
                    or formula.support.eligible_count != 0
                    or formula.support.exact_context_count != 0
                ):
                    raise PreFieldForecastError(
                        "zero-history pre-field forecast lacks exact Formula prior"
                    )
                distribution = formula.distribution
                basis_kind = "zero_history_formula_prior"
                capability_digest = None
                pool_digest = None
                component_digests = (formula.commit_digest,)
            else:
                pool = pool_forecasts(
                    current.card.forecasts,
                    inputs.weight_receipt,
                    capability.state,
                    SamplingSpec(
                        seed=int(
                            canonical_digest(
                                {
                                    "forecast_set_id": str(snapshot.forecast_set_id),
                                    "competitor_id": str(current.card.competitor_id),
                                    "ordinal": ordinal,
                                }
                            )[:16],
                            16,
                        )
                        & ((1 << 63) - 1),
                        draw_count=4096,
                    ),
                    weight_authority=inputs.weight_authority,
                )
                if pool.receipt.pooled_summary is None:
                    raise PreFieldForecastError(
                        "pre-field forecast requires at least two valid assessors"
                    )
                distribution = pool.receipt.pooled_summary
                basis_kind = "capability_pool"
                capability_digest = capability.binding.binding_digest
                pool_digest = pool.receipt.receipt_digest
                component_digests = tuple(item.commit_digest for item in committed)
                capability_bindings.append(capability.binding)
            forecasts.append(
                PreFieldCompetitorForecast.create(
                    competitor_id=current.card.competitor_id,
                    distribution=distribution,
                    basis_kind=basis_kind,
                    evidence_digest=current.card.packet_digest,
                    publication_digest=current.publication.publication_digest,
                    card_digest=current.publication.card_digest,
                    capability_binding_digest=capability_digest,
                    pool_receipt_digest=pool_digest,
                    component_forecast_digests=component_digests,
                )
            )
        publications = tuple(item.publication.publication_digest for item in inputs.cards)
        self._source.verify_current(snapshot, publications, tuple(capability_bindings))
        receipt = PreFieldForecastReceipt.create(
            snapshot=snapshot,
            forecasts=tuple(forecasts),
            signer=self._signer,
            created_at=created_at,
        )
        self._source.verify_current(snapshot, publications, tuple(capability_bindings))
        return self._persist(
            receipt,
            caller_namespace=caller_namespace,
            request_identity=request_identity,
            request_digest=request_digest,
        )

    def _recover(
        self, *, caller_namespace: str, request_identity: str, request_digest: str
    ) -> PreFieldForecastReceipt | None:
        with open_v3_connection(self.database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT request_digest,receipt_json FROM v3_pre_field_forecast_receipts "
                "WHERE request_namespace=? AND request_identity=?",
                (caller_namespace, request_identity),
            ).fetchone()
        if row is None:
            return None
        if str(row[0]) != request_digest:
            raise PreFieldForecastError(
                "pre-field request identity already binds different causal authority"
            )
        return PreFieldForecastReceipt.from_dict(
            json.loads(str(row[1])), trust_store=self._trust_store
        )

    def _persist(
        self,
        receipt: PreFieldForecastReceipt,
        *,
        caller_namespace: str,
        request_identity: str,
        request_digest: str,
    ) -> tuple[PreFieldForecastReceipt, bool]:
        snapshot = receipt.snapshot
        serialized = canonical_bytes(receipt.to_dict()).decode("utf-8")
        with open_v3_connection(self.database_path) as connection:
            with immediate_transaction(connection):
                existing = connection.execute(
                    "SELECT request_digest,receipt_digest,receipt_json FROM "
                    "v3_pre_field_forecast_receipts WHERE request_namespace=? "
                    "AND request_identity=?",
                    (caller_namespace, request_identity),
                ).fetchone()
                if existing is not None:
                    if str(existing[0]) != request_digest:
                        raise PreFieldForecastError(
                            "pre-field request identity already binds different causal authority"
                        )
                    authoritative = PreFieldForecastReceipt.from_dict(
                        json.loads(str(existing[2])), trust_store=self._trust_store
                    )
                    if authoritative.receipt_digest != str(existing[1]):
                        raise PreFieldForecastError(
                            "stored pre-field receipt digest differs from signed authority"
                        )
                    return authoritative, True
                connection.execute(
                    "INSERT INTO v3_pre_field_forecast_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(snapshot.forecast_set_id),
                        str(snapshot.tournament_id),
                        str(snapshot.round_id),
                        snapshot.forecast_set_revision,
                        caller_namespace,
                        request_identity,
                        request_digest,
                        snapshot.snapshot_digest,
                        receipt.receipt_digest,
                        serialized,
                        receipt.created_at,
                    ),
                )
        return receipt, False


__all__ = [
    "CoordinatorPreFieldForecastInputSource",
    "PreFieldForecastError",
    "PreFieldForecastInputPort",
    "PreFieldForecastInputs",
    "PreFieldForecastService",
    "UnavailablePreFieldForecastInputSource",
]
