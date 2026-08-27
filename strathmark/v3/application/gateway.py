"""Concrete transport-to-application adapter for the frozen V3 REST surface.

The gateway owns no prediction logic.  It resolves persisted authority and delegates
to the rolling-job, field-assembly, issue, and settlement application services that
were explicitly composed for this process.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from strathmark.v3.api.router import RequestContext
from strathmark.v3.api.schemas import (
    ApprovalDecisionResponse,
    ApprovalDetailResponse,
    ApprovalPageResponse,
    AssembleFieldResponse,
    IssueAcknowledgmentResponse,
    PreFieldForecastResponse,
    PrepareCardResponse,
    ReceiptLookupResponse,
    RoundCloseResponse,
    RoundFreezeResponse,
    ScopeCloseResponse,
    ScopeOpenResponse,
    SettlementResponse,
    SnapshotSyncResponse,
    StatusResponse,
)
from strathmark.v3.application.approval import (
    ApprovalConflict,
    ApprovalDecisionAction,
    ApprovalDecisionCommand,
    ApprovalDecisionSelection,
)
from strathmark.v3.application.field_assembly import (
    FieldAssemblyService,
    FrozenEntrantAssignment,
    FrozenFieldRevision,
)
from strathmark.v3.application.issuance import (
    IssuanceService,
    IssueBatchCommand,
    IssueFieldSelection,
)
from strathmark.v3.application.job_ports import DurableJobError
from strathmark.v3.application.lifecycle import (
    LifecycleService,
    SnapshotKind,
    UpstreamSnapshot,
)
from strathmark.v3.application.pre_field_forecasts import (
    PreFieldForecastError,
    PreFieldForecastService,
)
from strathmark.v3.application.settlement import SettlementCommand, SettlementService
from strathmark.v3.consumer_contract import (
    V3_CONSUMER_CONTRACT_VERSION,
    v3_consumer_contract_digest,
)
from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.commands import CommandKind
from strathmark.v3.contracts.events import (
    AggregateKind,
    CompetitionEngineSelection,
    EventEnvelope,
    EventKind,
)
from strathmark.v3.contracts.evidence import TargetContext, require_utc_milliseconds
from strathmark.v3.contracts.identifiers import (
    IdempotencyKey,
    StableIdentifier,
    deterministic_identifier,
    require_identifier,
)
from strathmark.v3.contracts.pre_field_forecasts import ForecastSetSnapshot
from strathmark.v3.contracts.receipts import EngineAuthorityBinding, FieldReceipt
from strathmark.v3.contracts.statuses import OfficialResult, ResultStatus
from strathmark.v3.domain.evidence import LiveResultSubmission
from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection
from strathmark.v3.infrastructure.sqlite.event_store import EventStoreConflict, SQLiteEventStore
from strathmark.v3.infrastructure.sqlite.jobs import DurableJobRepository
from strathmark.v3.infrastructure.sqlite.projections import SQLiteFieldProjectionStore


@dataclass(frozen=True, slots=True)
class GatewayServices:
    """The concrete services sharing one V3 event authority."""

    events: SQLiteEventStore
    lifecycle: LifecycleService
    fields: SQLiteFieldProjectionStore
    assembly: FieldAssemblyService
    issuance: IssuanceService
    settlement: SettlementService
    settlement_reactions: Any
    jobs: DurableJobRepository
    pre_field_forecasts: PreFieldForecastService

    def __post_init__(self) -> None:
        if not all(
            (
                isinstance(self.events, SQLiteEventStore),
                isinstance(self.lifecycle, LifecycleService),
                isinstance(self.fields, SQLiteFieldProjectionStore),
                isinstance(self.assembly, FieldAssemblyService),
                isinstance(self.issuance, IssuanceService),
                isinstance(self.settlement, SettlementService),
                isinstance(self.jobs, DurableJobRepository),
                isinstance(self.pre_field_forecasts, PreFieldForecastService),
                callable(getattr(self.settlement_reactions, "react", None)),
            )
        ):
            raise TypeError("gateway requires concrete V3 application services")
        database = self.events.database_path
        if self.lifecycle.projections.database_path != database:
            raise ValueError("gateway lifecycle authority differs from its event store")
        for service, attribute in (
            (self.fields, "database_path"),
            (self.jobs, "database_path"),
        ):
            if getattr(service, attribute, None) != database:
                raise ValueError("gateway services must share one exact V3 database")
        if getattr(self.settlement_reactions, "database_path", None) != database:
            raise ValueError("gateway settlement reactions must share its exact V3 database")
        if self.pre_field_forecasts.database_path != database:
            raise ValueError("gateway pre-field forecasts must share its exact V3 database")


@dataclass(frozen=True, slots=True)
class VerifiedV3CutoverState:
    """Result supplied by a separately verified production-cutover authority."""

    receipt_digest: str
    verified_at_utc: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.receipt_digest, str)
            or len(self.receipt_digest) != 64
            or any(character not in "0123456789abcdef" for character in self.receipt_digest)
        ):
            raise ValueError("verified cutover receipt digest is invalid")
        require_utc_milliseconds(self.verified_at_utc)


@dataclass(frozen=True, slots=True)
class V3ServiceIdentity:
    """Installation/composition-owned identity for one executable V3 service."""

    source_commit: str
    consumer_contract_version: str
    consumer_contract_digest: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"[0-9a-f]{7,40}", self.source_commit) is None:
            raise ValueError("V3 service source commit is invalid")
        if self.consumer_contract_version != V3_CONSUMER_CONTRACT_VERSION:
            raise ValueError("V3 service consumer contract version is unsupported")
        if (
            re.fullmatch(r"[0-9a-f]{64}", self.consumer_contract_digest) is None
            or self.consumer_contract_digest != v3_consumer_contract_digest()
        ):
            raise ValueError("V3 service consumer contract digest is not installed evidence")

    @classmethod
    def from_installed_contract(cls, *, source_commit: str) -> V3ServiceIdentity:
        return cls(
            source_commit=source_commit,
            consumer_contract_version=V3_CONSUMER_CONTRACT_VERSION,
            consumer_contract_digest=v3_consumer_contract_digest(),
        )


class V3ApplicationGateway:
    """Packaged implementation of :class:`V3ApplicationPort`."""

    def __init__(
        self,
        services: GatewayServices,
        *,
        clock: Callable[[], str],
        caller_namespace: str = "api",
        verified_cutover: Callable[[], VerifiedV3CutoverState | None] | None = None,
        service_identity: V3ServiceIdentity | None = None,
    ) -> None:
        if not isinstance(services, GatewayServices) or not callable(clock):
            raise TypeError("gateway requires typed services and an injected clock")
        if (
            not isinstance(caller_namespace, str)
            or not caller_namespace
            or not caller_namespace.replace("_", "a").replace("-", "a").isalnum()
            or not caller_namespace[0].islower()
        ):
            raise ValueError("gateway caller namespace is invalid")
        self._services = services
        self._clock = clock
        self._caller_namespace = caller_namespace
        if verified_cutover is not None and not callable(verified_cutover):
            raise TypeError("verified cutover authority must be callable")
        self._verified_cutover = verified_cutover or (lambda: None)
        if service_identity is not None and not isinstance(service_identity, V3ServiceIdentity):
            raise TypeError("V3 service identity must be verified composition evidence")
        self._service_identity = service_identity

    def verify_startup(self) -> None:
        """Run the explicit deep audit once before the application begins serving."""

        self._services.events.verify()
        self._services.fields.verify()
        self._services.jobs.verify()

    def open_scope(self, payload: dict[str, Any], context: RequestContext) -> ScopeOpenResponse:
        selection = CompetitionEngineSelection.from_dict(payload["engine_selection"])
        self._require_selection_compatible(selection)
        self._verify_scope_snapshot_selection(payload, selection)
        configure_id = IdempotencyKey(
            str(
                deterministic_identifier(
                    "command",
                    {"public_command_id": str(context.command_id), "phase": "configure_scope"},
                )
            )
        )
        self._services.lifecycle._execute(
            CommandKind.CONFIGURE_TOURNAMENT,
            EventKind.TOURNAMENT_CONFIGURED,
            AggregateKind.TOURNAMENT,
            StableIdentifier(payload["scope_id"]),
            {"configured": True},
            configure_id,
            context.principal.principal_id,
            payload["opened_at_utc"],
            0,
        )
        stored = self._services.lifecycle.open_tournament(
            StableIdentifier(payload["scope_id"]),
            bundle_id=StableIdentifier(payload["bundle_id"]),
            historical_cutoff_key=payload["historical_cutoff_key"],
            root_round_ids=tuple(StableIdentifier(item) for item in payload["root_round_ids"]),
            command_id=context.command_id,
            actor_id=context.principal.principal_id,
            occurred_at_utc=payload["opened_at_utc"],
            monotonic_elapsed_ms=0,
            engine_selection=selection,
        )
        return ScopeOpenResponse(
            scope_id=payload["scope_id"],
            selection_digest=selection.selection_digest,
            authority_sequence=stored.last_global_sequence,
            status="opened",
        )

    def synchronize_snapshot(
        self, payload: dict[str, Any], context: RequestContext
    ) -> SnapshotSyncResponse:
        snapshot = UpstreamSnapshot(
            SnapshotKind(payload["entity_kind"]),
            StableIdentifier(payload["entity_id"]),
            payload["upstream_revision"],
            StableIdentifier(payload["tournament_id"]),
            None if payload["round_id"] is None else StableIdentifier(payload["round_id"]),
            payload["snapshot"],
            CompetitionEngineSelection.from_dict(payload["engine_selection"]),
        )
        self._require_selection_compatible(snapshot.engine_selection)
        stored = self._services.lifecycle.ingest_snapshot(
            snapshot,
            command_id=context.command_id,
            actor_id=context.principal.principal_id,
            occurred_at_utc=payload["synchronized_at_utc"],
            monotonic_elapsed_ms=0,
        )
        return SnapshotSyncResponse(
            entity_id=payload["entity_id"],
            upstream_revision=payload["upstream_revision"],
            snapshot_digest=canonical_digest(payload["snapshot"]),
            authority_sequence=stored.last_global_sequence,
            status="synchronized",
        )

    def freeze_round(self, payload: dict[str, Any], context: RequestContext) -> RoundFreezeResponse:
        self._require_scope_compatible(self._round_parent(payload["round_id"]))
        if self._round_state(payload["round_id"]) is None:
            configure_id = IdempotencyKey(
                str(
                    deterministic_identifier(
                        "command",
                        {
                            "public_command_id": str(context.command_id),
                            "phase": "configure_round",
                        },
                    )
                )
            )
            self._services.lifecycle._execute(
                CommandKind.CONFIGURE_ROUND,
                EventKind.ROUND_CONFIGURED,
                AggregateKind.ROUND,
                StableIdentifier(payload["round_id"]),
                {"configured": True},
                configure_id,
                context.principal.principal_id,
                payload["frozen_at_utc"],
                0,
            )
        epoch, stored = self._services.lifecycle.freeze_round_epoch(
            StableIdentifier(payload["round_id"]),
            epoch_revision=payload["epoch_revision"],
            historical_cutoff_key=payload["historical_cutoff_key"],
            closure_ids=tuple(StableIdentifier(item) for item in payload["closure_ids"]),
            command_id=context.command_id,
            actor_id=context.principal.principal_id,
            occurred_at_utc=payload["frozen_at_utc"],
            monotonic_elapsed_ms=0,
        )
        return RoundFreezeResponse(
            round_id=payload["round_id"],
            epoch_id=str(epoch.epoch_id),
            epoch_revision=payload["epoch_revision"],
            authority_sequence=stored.last_global_sequence,
            status="frozen",
        )

    def approval_page(
        self, payload: dict[str, Any], _context: RequestContext
    ) -> ApprovalPageResponse:
        self._require_scope_compatible(payload["tournament_id"])
        with open_v3_connection(self._services.events.database_path, read_only=True) as connection:
            meta = connection.execute(
                "SELECT 1 FROM v3_approval_projection_meta WHERE tournament_id=?",
                (payload["tournament_id"],),
            ).fetchone()
            opened = connection.execute(
                "SELECT MAX(global_sequence) FROM v3_events "
                "WHERE aggregate_id=? AND event_kind='tournament_opened'",
                (payload["tournament_id"],),
            ).fetchone()
            field_count = int(
                connection.execute(
                    "SELECT COUNT(DISTINCT entity_id) FROM v3_ingress_snapshots "
                    "WHERE entity_kind='field' AND tournament_id=?",
                    (payload["tournament_id"],),
                ).fetchone()[0]
            )
        if meta is None:
            if opened is None or opened[0] is None:
                self._not_found("approval_scope_not_found")
            if field_count:
                raise RuntimeError("approval projection missing for configured fields")
            sequence = int(opened[0])
            snapshot_id = f"approval_snapshot:{canonical_digest({'tournament_id': payload['tournament_id'], 'source_global_sequence': sequence, 'fields': []})}"
            return ApprovalPageResponse(
                tournament_id=payload["tournament_id"],
                snapshot_id=snapshot_id,
                offset=payload["offset"],
                limit=payload["limit"],
                total=0,
                lifecycle_state="no_scheduled_fields",
                rows=[],
                authority_sequence=sequence,
            )
        page = self._services.fields.approval_page(**payload)
        return ApprovalPageResponse(
            tournament_id=page.tournament_id,
            snapshot_id=page.snapshot_id,
            offset=page.offset,
            limit=page.limit,
            total=page.total,
            lifecycle_state=page.lifecycle_state,
            rows=[item.to_dict() for item in page.rows],
            authority_sequence=max(page.source_global_sequence, page.decision_global_sequence),
        )

    def forecast_pre_field(
        self, payload: dict[str, Any], context: RequestContext
    ) -> PreFieldForecastResponse:
        tournament_id = self._round_parent(payload["round_id"])
        if tournament_id != payload["tournament_id"]:
            self._conflict("pre_field_round_scope_differs")
        self._require_scope_compatible(tournament_id)
        if self._round_state(payload["round_id"]) != "round_frozen":
            self._conflict("pre_field_round_is_not_frozen")
        with open_v3_connection(self._services.events.database_path, read_only=True) as connection:
            epoch = connection.execute(
                "SELECT epoch_id,epoch_revision,maximum_tournament_sequence,"
                "historical_cutoff_key,epoch_digest FROM v3_evidence_epochs "
                "WHERE round_id=? ORDER BY epoch_revision DESC LIMIT 1",
                (payload["round_id"],),
            ).fetchone()
        if epoch is None:
            self._conflict("pre_field_epoch_not_found")
        if int(epoch[1]) != payload["forecast_set_revision"]:
            self._conflict("pre_field_revision_differs_from_frozen_epoch")
        snapshot = ForecastSetSnapshot.create(
            tournament_id=tournament_id,
            round_id=payload["round_id"],
            forecast_set_revision=payload["forecast_set_revision"],
            ordered_competitor_ids=tuple(payload["ordered_competitor_ids"]),
            target_context=TargetContext.from_dict(payload["target_context"]),
            historical_cutoff_key=str(epoch[3]),
            tournament_epoch_id=str(epoch[0]),
            epoch_digest=str(epoch[4]),
            maximum_tournament_sequence=int(epoch[2]),
            bundle_digest=self._resolve_forecast_bundle(
                competitor_ids=tuple(payload["ordered_competitor_ids"]),
                target_context_digest=TargetContext.from_dict(payload["target_context"]).digest,
                historical_cutoff_key=str(epoch[3]),
                tournament_epoch_id=str(epoch[0]),
            ),
            hard_deadline_at=payload["hard_deadline_at"],
            engine_authority=self._scope_engine_authority(tournament_id),
        )
        try:
            receipt, recovered = self._services.pre_field_forecasts.forecast(
                snapshot,
                caller_namespace=self._caller_namespace,
                request_identity=str(context.command_id),
                created_at=payload["requested_at_utc"],
            )
        except (PreFieldForecastError, DurableJobError):
            self._conflict("pre_field_forecast_not_ready")
        return PreFieldForecastResponse(
            forecast_set_id=str(receipt.snapshot.forecast_set_id),
            receipt_digest=receipt.receipt_digest,
            disposition="recovered" if recovered else "forecasted",
            purpose=receipt.purpose,
            issued_mark=receipt.issued_mark,
            canonical_receipt_json=canonical_bytes(receipt.to_dict()).decode("utf-8"),
            authority_sequence=self._services.events.current_anchor().global_sequence,
        )

    def _resolve_forecast_bundle(
        self,
        *,
        competitor_ids: tuple[str, ...],
        target_context_digest: str,
        historical_cutoff_key: str,
        tournament_epoch_id: str,
    ) -> str:
        placeholders = ",".join("?" for _ in competitor_ids)
        query = (
            "SELECT json_extract(payload_json, '$.card_key.competitor_id'), "
            "json_extract(payload_json, '$.card_key.bundle_digest') FROM v3_jobs "
            "WHERE json_extract(payload_json, '$.schema_version')=? "
            "AND json_extract(payload_json, '$.card_key.target_context_digest')=? "
            "AND json_extract(payload_json, '$.card_key.historical_cutoff_key')=? "
            "AND json_extract(payload_json, '$.card_key.tournament_epoch_id')=? "
            f"AND json_extract(payload_json, '$.card_key.competitor_id') IN ({placeholders})"
        )
        with open_v3_connection(self._services.events.database_path, read_only=True) as connection:
            rows = connection.execute(
                query,
                (
                    "strathmark-v3-rolling-component-job-v1",
                    target_context_digest,
                    historical_cutoff_key,
                    tournament_epoch_id,
                    *competitor_ids,
                ),
            ).fetchall()
            council_rows = connection.execute(
                "SELECT bundle_digest FROM v3_rolling_council_authorities"
            ).fetchall()
        observed_ids = {str(row[0]) for row in rows}
        bundles = {str(row[1]) for row in rows}
        if not rows and len(council_rows) == 1:
            return str(council_rows[0][0])
        if observed_ids != set(competitor_ids) or len(bundles) != 1:
            self._conflict("pre_field_bundle_authority_is_not_unique")
        return next(iter(bundles))

    def approval_detail(
        self, payload: dict[str, Any], _context: RequestContext
    ) -> ApprovalDetailResponse:
        self._require_scope_compatible(payload["tournament_id"])
        detail = self._services.fields.approval_detail(**payload)
        with open_v3_connection(self._services.events.database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT source_global_sequence FROM v3_approval_details "
                "WHERE tournament_id=? AND receipt_id=?",
                (payload["tournament_id"], payload["receipt_id"]),
            ).fetchone()
        if row is None:
            self._not_found("approval_detail_not_found")
        source = int(row[0])
        return ApprovalDetailResponse(
            tournament_id=payload["tournament_id"],
            snapshot_id=payload["snapshot_id"],
            receipt_id=payload["receipt_id"],
            detail=detail,
            authority_sequence=source,
        )

    def close_round(self, payload: dict[str, Any], context: RequestContext) -> RoundCloseResponse:
        self._require_scope_compatible(self._round_parent(payload["round_id"]))
        if self._round_state(payload["round_id"]) == "round_frozen":
            begin_id = IdempotencyKey(
                str(
                    deterministic_identifier(
                        "command",
                        {
                            "public_command_id": str(context.command_id),
                            "phase": "begin_round_closing",
                        },
                    )
                )
            )
            self._services.lifecycle._execute(
                CommandKind.BEGIN_ROUND_CLOSING,
                EventKind.ROUND_CLOSING_STARTED,
                AggregateKind.ROUND,
                StableIdentifier(payload["round_id"]),
                {"closing": True},
                begin_id,
                context.principal.principal_id,
                payload["closed_at_utc"],
                0,
            )
        closure_id, stored = self._services.lifecycle.close_evidence_round(
            StableIdentifier(payload["round_id"]),
            command_id=context.command_id,
            actor_id=context.principal.principal_id,
            occurred_at_utc=payload["closed_at_utc"],
            monotonic_elapsed_ms=0,
        )
        return RoundCloseResponse(
            round_id=payload["round_id"],
            closure_id=str(closure_id),
            authority_sequence=stored.last_global_sequence,
            status="closed",
        )

    def close_scope(self, payload: dict[str, Any], context: RequestContext) -> ScopeCloseResponse:
        self._require_scope_compatible(payload["scope_id"])
        stored = self._services.lifecycle.close_tournament(
            StableIdentifier(payload["scope_id"]),
            command_id=context.command_id,
            actor_id=context.principal.principal_id,
            occurred_at_utc=payload["closed_at_utc"],
            monotonic_elapsed_ms=0,
        )
        return ScopeCloseResponse(
            scope_id=payload["scope_id"],
            authority_sequence=stored.last_global_sequence,
            status="closed",
        )

    def prepare_card(
        self, payload: dict[str, Any], _context: RequestContext
    ) -> PrepareCardResponse:
        self._require_scope_compatible(payload["tournament_id"])
        field = self._resolve_field(payload["field_id"])
        if (
            str(field.tournament_id) != payload["tournament_id"]
            or str(field.round_id) != payload["round_id"]
            or field.field_revision != payload["source_revision"]
            or field.target_context.digest != payload["target_context_digest"]
            or payload["competitor_id"]
            not in {str(item.competitor_id) for item in field.ordered_assignments}
        ):
            self._conflict("card_request_differs_from_current_field")
        key = self._services.jobs.rolling_card_key_for_field(
            competitor_id=payload["competitor_id"],
            target_context_digest=payload["target_context_digest"],
            historical_cutoff_key=str(field.historical_cutoff_key),
            tournament_epoch_id=str(field.tournament_epoch_id),
            bundle_digest=field.bundle_digest,
        )
        if key is None:
            self._conflict("card_work_not_scheduled")
        records = self._services.jobs.records_for_card(key["card_digest"])
        if len(records) != 5:
            self._conflict("card_component_set_incomplete")
        publication = self._services.jobs.rolling_publication_row(card_digest=key["card_digest"])
        ready = publication is not None
        return PrepareCardResponse(
            job_id=min(record.job_id for record in records),
            status="ready" if ready else "already_queued",
            authority_sequence=self._services.events.current_anchor().global_sequence,
        )

    def assemble_field(
        self, payload: dict[str, Any], context: RequestContext
    ) -> AssembleFieldResponse:
        field = self._resolve_field(payload["field_id"])
        if field.field_revision != payload["upstream_field_revision"] or tuple(
            payload["ordered_competitor_ids"]
        ) != tuple(str(item.competitor_id) for item in field.ordered_assignments):
            self._conflict("field_request_differs_from_current_authority")
        prior = self._services.fields.lookup_exact(
            caller_namespace=self._caller_namespace,
            request_identity=str(context.command_id),
            field_revision_digest=field.revision_digest,
        )
        result = self._services.assembly.assemble(
            field=field,
            caller_namespace=self._caller_namespace,
            request_identity=str(context.command_id),
            actor_id=str(context.principal.principal_id),
            occurred_at=self._clock(),
            engine_authority=self._scope_engine_authority(str(field.tournament_id)),
        )
        authority_sequence = self._receipt_sequence(str(result.receipt.receipt_id))
        return AssembleFieldResponse(
            receipt_id=str(result.receipt.receipt_id),
            receipt_digest=result.receipt.content_digest,
            disposition="recovered" if prior is not None else "prepared",
            canonical_receipt_json=result.canonical_bytes.decode("utf-8"),
            authority_sequence=authority_sequence,
        )

    def lookup_receipt(
        self, payload: dict[str, Any], _context: RequestContext
    ) -> ReceiptLookupResponse:
        receipt_id = payload["receipt_id"]
        with open_v3_connection(self._services.events.database_path, read_only=True) as connection:
            if receipt_id is None:
                row = connection.execute(
                    "SELECT receipt_id FROM v3_field_receipts "
                    "WHERE caller_namespace=? AND request_identity=?",
                    (self._caller_namespace, payload["request_identity"]),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT receipt_id FROM v3_field_receipts "
                    "WHERE caller_namespace=? AND receipt_id=?",
                    (self._caller_namespace, receipt_id),
                ).fetchone()
        if row is None:
            return ReceiptLookupResponse(
                found=False,
                authority_sequence=self._services.events.current_anchor().global_sequence,
            )
        receipt_id = str(row[0])
        try:
            receipt = self._services.fields.verified_receipt(receipt_id)
        except KeyError:
            return ReceiptLookupResponse(
                found=False,
                authority_sequence=self._services.events.current_anchor().global_sequence,
            )
        return ReceiptLookupResponse(
            found=True,
            receipt_id=str(receipt.receipt_id),
            receipt_digest=receipt.content_digest,
            canonical_receipt_json=receipt.canonical_payload.decode("utf-8"),
            authority_sequence=self._receipt_sequence(str(receipt.receipt_id)),
        )

    def acknowledge_issue(
        self, payload: dict[str, Any], context: RequestContext
    ) -> IssueAcknowledgmentResponse:
        receipts = []
        tournament_ids: set[str] = set()
        for binding in payload["receipt_bindings"]:
            receipt = self._services.fields.verified_receipt(binding["receipt_id"])
            if receipt.content_digest != binding["receipt_digest"]:
                self._conflict("issue_receipt_digest_differs")
            tournament_id, round_id = self._field_parent(str(receipt.field_id))
            tournament_ids.add(tournament_id)
            receipts.append((receipt, tournament_id, round_id))
        if len(tournament_ids) != 1:
            self._conflict("issue_receipts_span_tournaments")
        tournament_id = next(iter(tournament_ids))
        self._require_scope_compatible(tournament_id)
        snapshot = self._services.fields.approval_page(
            tournament_id=tournament_id, offset=0, limit=1
        )
        selections = []
        for receipt, receipt_tournament_id, round_id in receipts:
            if receipt_tournament_id != tournament_id:
                self._conflict("issue_receipts_span_tournaments")
            try:
                detail = self._services.fields.approval_detail(
                    tournament_id=tournament_id,
                    snapshot_id=snapshot.snapshot_id,
                    receipt_id=str(receipt.receipt_id),
                )
            except KeyError:
                self._conflict("issue_requires_one_approved_current_receipt")
            row = detail.get("row")
            if not isinstance(row, dict) or row.get("decision_state") not in {
                "accepted",
                "override-submitted",
            }:
                self._conflict("issue_requires_one_approved_current_receipt")
            if row.get("receipt_id") != str(receipt.receipt_id):
                self._conflict("issue_approval_detail_differs_from_receipt")
            head = self._services.events.aggregate_head(str(receipt.field_id))
            if head is None:
                self._conflict("issue_field_has_no_authority_head")
            selections.append(
                IssueFieldSelection(
                    row["field_id"],
                    row["receipt_id"],
                    row["receipt_revision"],
                    row["upstream_field_revision"],
                    head[0],
                    row["row_digest"],
                    row["call_order"],
                    round_id,
                    str(receipt.tournament_epoch_id),
                    tuple(str(item) for item in receipt.ordered_competitor_ids),
                    tuple((str(item.competitor_id), item.mark) for item in receipt.marks),
                )
            )
        command = IssueBatchCommand.create(
            caller_namespace=self._caller_namespace,
            request_identity=str(context.command_id),
            tournament_id=tournament_id,
            approval_snapshot_id=snapshot.snapshot_id,
            selections=tuple(sorted(selections, key=lambda item: item.field_id)),
            actor_id=str(context.principal.principal_id),
            actor_metadata={
                "upstream_issue_id": payload["upstream_issue_id"],
                "upstream_actor_id": context.upstream_actor_id,
                "upstream_action": context.upstream_action,
                "upstream_trace_id": context.upstream_trace_id,
            },
            reason_code="upstream_issue_acknowledged",
            submitted_at=payload["issued_at_utc"],
            monotonic_elapsed_ms=0,
        )
        acknowledgment = self._services.issuance.acknowledge(command)
        return IssueAcknowledgmentResponse(
            issue_batch_id=acknowledgment.issue_batch_id,
            receipt_ids=acknowledgment.receipt_ids,
            authority_sequence=acknowledgment.last_global_sequence,
            recovery_marker_digest=acknowledgment.result_digest,
        )

    def record_approval_decision(
        self, payload: dict[str, Any], context: RequestContext
    ) -> ApprovalDecisionResponse:
        self._require_scope_compatible(payload["tournament_id"])
        bindings = (*payload["selected"], *payload["excluded"])
        for binding in bindings:
            try:
                receipt = self._services.fields.verified_receipt(binding["receipt_id"])
            except KeyError:
                self._conflict("approval_receipt_not_found")
            if (
                receipt.content_digest != binding["receipt_digest"]
                or str(receipt.field_id) != binding["field_id"]
            ):
                self._conflict("approval_receipt_digest_differs")
        receipt_bindings_digest = canonical_digest(
            {"selected": payload["selected"], "excluded": payload["excluded"]}
        )

        def selection(value: dict[str, Any]) -> ApprovalDecisionSelection:
            return ApprovalDecisionSelection(
                value["field_id"],
                value["receipt_id"],
                value["receipt_revision"],
                value["upstream_field_revision"],
                value["row_digest"],
                value["call_order"],
            )

        actor_metadata = {
            "submitted": payload["actor_metadata"],
            "transport": {
                "upstream_actor_id": context.upstream_actor_id,
                "upstream_action": context.upstream_action,
                "upstream_trace_id": context.upstream_trace_id,
            },
            "receipt_bindings_digest": receipt_bindings_digest,
        }
        command = ApprovalDecisionCommand.create(
            caller_namespace=self._caller_namespace,
            request_identity=str(context.command_id),
            tournament_id=payload["tournament_id"],
            snapshot_id=payload["snapshot_id"],
            action=ApprovalDecisionAction(payload["action"]),
            selected=tuple(selection(item) for item in payload["selected"]),
            excluded=tuple(selection(item) for item in payload["excluded"]),
            actor_id=str(context.principal.principal_id),
            actor_metadata=actor_metadata,
            reason_code=payload["reason_code"],
            superseded_receipt_id=payload["superseded_receipt_id"],
            submitted_at=payload["decided_at_utc"],
        )
        try:
            decision = self._services.fields.record_approval_decision(command)
        except (ApprovalConflict, EventStoreConflict):
            self._conflict("approval_decision_conflicts")
        with open_v3_connection(self._services.events.database_path, read_only=True) as connection:
            authority = connection.execute(
                "SELECT source_global_sequence,command_digest,decision_digest "
                "FROM v3_approval_command_projection WHERE tournament_id=? "
                "AND caller_namespace=? AND request_identity=?",
                (
                    command.tournament_id,
                    command.caller_namespace,
                    command.request_identity,
                ),
            ).fetchone()
        if authority is None or (
            str(authority[1]) != command.command_digest
            or str(authority[2]) != decision.decision_digest
        ):
            raise RuntimeError("approval acknowledgment differs from projection authority")
        return ApprovalDecisionResponse(
            command_id=str(context.command_id),
            caller_namespace=decision.caller_namespace,
            tournament_id=decision.tournament_id,
            snapshot_id=decision.snapshot_id,
            action=decision.action.value,
            decisions=tuple(
                {"receipt_id": receipt_id, "decision_state": state.value}
                for receipt_id, state in decision.decisions
            ),
            decided_at_utc=decision.decided_at,
            actor_metadata_digest=command.actor_metadata_digest,
            receipt_bindings_digest=receipt_bindings_digest,
            command_digest=decision.command_digest,
            decision_digest=decision.decision_digest,
            authority_sequence=int(authority[0]),
        )

    def settle_result(self, payload: dict[str, Any], context: RequestContext) -> SettlementResponse:
        receipt = self._services.fields.verified_receipt(payload["receipt_id"])
        self._verify_issue_binding(payload["issue_batch_id"], payload["receipt_id"])
        tournament_id, round_id = self._field_parent(str(receipt.field_id))
        self._require_scope_compatible(tournament_id)
        rows = {item["competitor_id"]: item for item in payload["results"]}
        roster = tuple(str(item) for item in receipt.ordered_competitor_ids)
        if set(rows) != set(roster):
            self._conflict("settlement_requires_complete_issued_roster")
        marks = {str(item.competitor_id): item.mark for item in receipt.marks}
        completion_clocks: dict[str, int] = {}
        for competitor_id, row in rows.items():
            if row["status"] in {"completion", "penalty"}:
                completion_clocks[competitor_id] = (
                    marks[competitor_id] * 1_000 + row["raw_time_ms"] + (row["penalty_ms"] or 0)
                )
        ordered = tuple(sorted(completion_clocks, key=lambda item: (completion_clocks[item], item)))
        placing = {competitor_id: index for index, competitor_id in enumerate(ordered, start=1)}
        winning_clock = None if not ordered else completion_clocks[ordered[0]]
        submissions = tuple(
            self._submission(
                row=rows[competitor_id],
                competitor_id=competitor_id,
                tournament_id=tournament_id,
                round_id=round_id,
                receipt=receipt,
                issued_mark=marks[competitor_id],
                completion_clock=completion_clocks.get(competitor_id),
                placing=placing.get(competitor_id),
                gap=(
                    None
                    if competitor_id not in completion_clocks or winning_clock is None
                    else completion_clocks[competitor_id] - winning_clock
                ),
                issue_batch_id=payload["issue_batch_id"],
                observed_at=payload["observed_at_utc"],
            )
            for competitor_id in roster
        )
        recovered = self._stored_settlement_retry(
            context=context,
            receipt=receipt,
            submissions=submissions,
        )
        if recovered is not None:
            settlement_id, stored = recovered
            self._services.settlement_reactions.react(stored)
            return SettlementResponse(
                settlement_id=settlement_id,
                receipt_id=str(receipt.receipt_id),
                authority_sequence=stored.last_global_sequence,
                status="recovered",
            )
        acknowledgment = self._services.settlement.record_and_settle(
            SettlementCommand.create(
                field_id=str(receipt.field_id),
                field_revision=receipt.upstream_field_revision,
                receipt_id=str(receipt.receipt_id),
                command_id=str(context.command_id),
                actor_id=str(context.principal.principal_id),
                occurred_at=payload["observed_at_utc"],
                monotonic_elapsed_ms=0,
            ),
            submissions,
        )
        settlement_id = self._settlement_id(acknowledgment.last_global_sequence)
        return SettlementResponse(
            settlement_id=settlement_id,
            receipt_id=acknowledgment.receipt_id,
            authority_sequence=acknowledgment.last_global_sequence,
            status="recorded",
        )

    def status(self, _context: RequestContext) -> StatusResponse:
        event_integrity = self._services.events.integrity_checkpoint_status()
        field_integrity = self._services.fields.integrity_checkpoint_status()
        job_integrity = self._services.jobs.integrity_checkpoint_status()
        with open_v3_connection(self._services.events.database_path, read_only=True) as connection:
            open_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM v3_aggregate_heads head "
                    "JOIN v3_events event ON event.aggregate_id=head.aggregate_id "
                    "AND event.aggregate_version=head.aggregate_version "
                    "WHERE head.aggregate_kind='tournament' "
                    "AND event.event_kind='tournament_opened'"
                ).fetchone()[0]
            )
        cutover = self._verified_cutover()
        if cutover is not None and not isinstance(cutover, VerifiedV3CutoverState):
            raise TypeError("verified cutover authority returned an invalid state")
        identity = getattr(self, "_service_identity", None)
        effective_cutover = cutover if identity is not None else None
        production_authority = "v3" if effective_cutover is not None else "v2"
        v3_readiness = "production" if effective_cutover is not None else "candidate"
        v3_option_state = (
            "ineligible"
            if identity is None
            else "production_ready"
            if effective_cutover is not None
            else "rehearsal_ready"
        )
        field_checkpoint_unavailable = (
            int(field_integrity["authority_sequence"]) == 0
            and str(field_integrity["checkpoint_digest"]) == "0" * 64
            and str(field_integrity["last_deep_verified_at"]) == "1970-01-01T00:00:00.000Z"
        )
        return StatusResponse(
            service="ready",
            authority_sequence=int(event_integrity["authority_sequence"]),
            engine_authority=production_authority,
            v3_readiness=v3_readiness,
            production_authority=production_authority,
            cutover_receipt_digest=(
                None if effective_cutover is None else effective_cutover.receipt_digest
            ),
            cutover_verified_at_utc=(
                None if effective_cutover is None else effective_cutover.verified_at_utc
            ),
            deep_verification_state=("unavailable" if field_checkpoint_unavailable else "verified"),
            event_last_deep_verified_at_utc=str(event_integrity["last_deep_verified_at"]),
            event_checkpoint_digest=str(event_integrity["checkpoint_digest"]),
            field_last_deep_verified_at_utc=str(field_integrity["last_deep_verified_at"]),
            field_checkpoint_digest=str(field_integrity["checkpoint_digest"]),
            job_last_deep_verified_at_utc=str(job_integrity["last_deep_verified_at"]),
            job_checkpoint_digest=str(job_integrity["checkpoint_digest"]),
            open_tournament_count=open_count,
            v3_option_state=v3_option_state,
            rehearsal_eligible=identity is not None,
            production_eligible=effective_cutover is not None,
            eligibility_reason_codes=(
                ("service_identity_unavailable",)
                if identity is None
                else ()
                if effective_cutover is not None
                else ("production_cutover_not_verified",)
            ),
            consumer_contract_version=V3_CONSUMER_CONTRACT_VERSION,
            consumer_contract_digest=v3_consumer_contract_digest(),
            source_commit=None if identity is None else identity.source_commit,
        )

    def _resolve_field(self, field_id: str) -> FrozenFieldRevision:
        require_identifier(field_id, expected_namespace="field")
        with open_v3_connection(self._services.events.database_path, read_only=True) as connection:
            ingress = connection.execute(
                "SELECT upstream_revision,tournament_id,round_id,snapshot_json "
                "FROM v3_ingress_snapshots WHERE entity_kind='field' AND entity_id=? "
                "ORDER BY upstream_revision DESC LIMIT 1",
                (field_id,),
            ).fetchone()
            if ingress is None:
                self._not_found("field_authority_not_found")
            snapshot = json.loads(str(ingress[3]))
            epoch = connection.execute(
                "SELECT epoch_id,epoch_digest,maximum_tournament_sequence,"
                "historical_cutoff_key FROM v3_evidence_epochs WHERE round_id=? "
                "ORDER BY epoch_revision DESC LIMIT 1",
                (str(ingress[2]),),
            ).fetchone()
            capacity = connection.execute(
                "SELECT bundle_digest FROM v3_field_capacity_authorities WHERE authority_digest=?",
                (snapshot["capacity_authority_digest"],),
            ).fetchone()
        if epoch is None or capacity is None:
            self._conflict("field_causal_authority_incomplete")
        assignments = tuple(
            FrozenEntrantAssignment.create(competitor, stand, index)
            for index, (competitor, stand) in enumerate(
                zip(snapshot["competitor_ids"], snapshot["stand_ids"], strict=True)
            )
        )
        from strathmark.v3.contracts.evidence import TargetContext

        return FrozenFieldRevision.create(
            tournament_id=str(ingress[1]),
            round_id=str(ingress[2]),
            field_id=field_id,
            field_revision=int(ingress[0]),
            assignments=assignments,
            target_context=TargetContext.from_dict(snapshot["target_context"]),
            historical_cutoff_key=str(epoch[3]),
            tournament_epoch_id=str(epoch[0]),
            tournament_event_sequence=int(epoch[2]),
            bundle_digest=str(capacity[0]),
            evidence_digest=str(epoch[1]),
            capacity_authority_digest=snapshot["capacity_authority_digest"],
            max_field_entrants=snapshot["max_field_entrants"],
            call_order=snapshot["call_order"],
            scheduled_at=snapshot["scheduled_at"],
            deadline_at=snapshot["deadline_at"],
        )

    def _round_state(self, round_id: str) -> str | None:
        require_identifier(round_id, expected_namespace="round")
        with open_v3_connection(self._services.events.database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT lifecycle_status FROM v3_aggregate_heads "
                "WHERE aggregate_kind='round' AND aggregate_id=?",
                (round_id,),
            ).fetchone()
        return None if row is None else str(row[0])

    def _round_parent(self, round_id: str) -> str:
        require_identifier(round_id, expected_namespace="round")
        with open_v3_connection(self._services.events.database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT tournament_id FROM v3_ingress_snapshots "
                "WHERE entity_kind='round' AND entity_id=? "
                "ORDER BY upstream_revision DESC LIMIT 1",
                (round_id,),
            ).fetchone()
        if row is None:
            self._not_found("round_authority_not_found")
        return str(row[0])

    def _verify_scope_snapshot_selection(
        self, payload: Mapping[str, Any], selection: CompetitionEngineSelection
    ) -> None:
        roots = tuple(payload["root_round_ids"])
        with open_v3_connection(self._services.events.database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT entity_kind,entity_id,source_global_sequence "
                "FROM v3_ingress_snapshots WHERE tournament_id=? "
                "ORDER BY entity_kind,entity_id,upstream_revision DESC",
                (payload["scope_id"],),
            ).fetchall()
        latest: dict[tuple[str, str], int] = {}
        for row in rows:
            latest.setdefault((str(row[0]), str(row[1])), int(row[2]))
        required = {("tournament", payload["scope_id"]), *(("round", item) for item in roots)}
        if not required.issubset(latest):
            self._conflict("scope_requires_bound_initial_snapshots")
        expected = selection.to_dict()
        if any(
            self._services.events.event_at(sequence)
            .command.payload.to_value()
            .get("engine_selection")
            != expected
            for sequence in latest.values()
        ):
            self._conflict("scope_snapshot_selection_differs")

    def _require_selection_compatible(self, selection: CompetitionEngineSelection | None) -> None:
        identity = self._service_identity
        if identity is None:
            self._conflict("service_identity_unavailable")
        if selection is None or (
            selection.consumer_contract_digest != identity.consumer_contract_digest
            or selection.source_commit != identity.source_commit
        ):
            self._conflict("scope_service_identity_mismatch")

    def _require_scope_compatible(self, tournament_id: str) -> None:
        require_identifier(tournament_id, expected_namespace="tournament")
        with open_v3_connection(self._services.events.database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT global_sequence FROM v3_events WHERE aggregate_id=? "
                "AND event_kind='tournament_opened' ORDER BY aggregate_version DESC LIMIT 1",
                (tournament_id,),
            ).fetchone()
        if row is None:
            self._conflict("field_scope_has_no_engine_authority")
        opened = self._services.events.event_at(int(row[0]))
        selection_value = opened.command.payload.to_value().get("engine_selection")
        if selection_value is None:
            self._conflict("field_scope_has_no_engine_authority")
        if not isinstance(selection_value, Mapping):
            self._conflict("field_scope_engine_authority_is_malformed")
        self._require_selection_compatible(CompetitionEngineSelection.from_dict(selection_value))

    def _scope_engine_authority(self, tournament_id: str) -> EngineAuthorityBinding:
        require_identifier(tournament_id, expected_namespace="tournament")
        with open_v3_connection(self._services.events.database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT global_sequence FROM v3_events WHERE aggregate_id=? "
                "AND event_kind='tournament_opened' ORDER BY aggregate_version DESC LIMIT 1",
                (tournament_id,),
            ).fetchone()
        if row is None:
            self._conflict("field_scope_has_no_engine_authority")
        opened = self._services.events.event_at(int(row[0]))
        selection_value = opened.command.payload.to_value().get("engine_selection")
        if selection_value is None:
            self._conflict("field_scope_has_no_engine_authority")
        if not isinstance(selection_value, Mapping):
            self._conflict("field_scope_engine_authority_is_malformed")
        selection = CompetitionEngineSelection.from_dict(selection_value)
        if str(selection.scope_id) != tournament_id:
            self._conflict("field_scope_engine_authority_differs")
        self._require_selection_compatible(selection)
        return EngineAuthorityBinding(
            scope_id=selection.scope_id,
            engine=selection.engine,
            mode=selection.mode,
            selection_digest=selection.selection_digest,
            consumer_contract_digest=selection.consumer_contract_digest,
            source_commit=selection.source_commit,
        )

    def _submission(
        self,
        *,
        row: Mapping[str, Any],
        competitor_id: str,
        tournament_id: str,
        round_id: str,
        receipt: FieldReceipt,
        issued_mark: int,
        completion_clock: int | None,
        placing: int | None,
        gap: int | None,
        issue_batch_id: str,
        observed_at: str,
    ) -> LiveResultSubmission:
        revision = row["source_revision"]
        source = {
            "schema_version": "strathmark-v3-api-result-source-v1",
            "issue_batch_id": issue_batch_id,
            "receipt_id": str(receipt.receipt_id),
            "competitor_id": competitor_id,
            "row": dict(row),
            "observed_at_utc": observed_at,
        }
        return LiveResultSubmission(
            deterministic_identifier(
                "evidence",
                {
                    "field_id": str(receipt.field_id),
                    "receipt_id": str(receipt.receipt_id),
                    "competitor_id": competitor_id,
                    "source_revision": revision,
                },
            ),
            StableIdentifier(competitor_id),
            StableIdentifier(tournament_id),
            StableIdentifier(round_id),
            receipt.field_id,
            receipt.target_context,
            observed_at,
            issued_mark,
            completion_clock,
            placing,
            gap,
            OfficialResult(
                ResultStatus(row["status"]),
                row["raw_time_ms"],
                row["penalty_ms"],
                revision,
                None if revision == 1 else revision - 1,
            ),
            canonical_digest(source),
        )

    def _stored_settlement_retry(
        self,
        *,
        context: RequestContext,
        receipt: FieldReceipt,
        submissions: tuple[LiveResultSubmission, ...],
    ) -> tuple[str, Any] | None:
        with open_v3_connection(self._services.events.database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT event.envelope_json FROM v3_idempotency_records record "
                "JOIN v3_events event ON event.global_sequence=record.first_global_sequence "
                "WHERE record.principal_id=? AND record.idempotency_key=?",
                (str(context.principal.principal_id), str(context.command_id)),
            ).fetchone()
            if row is None:
                return None
        envelope = EventEnvelope.from_dict(json.loads(str(row[0])))
        command = envelope.command
        stored = self._services.events.lookup_exact_retry(
            principal_id=str(context.principal.principal_id),
            idempotency_key=str(context.command_id),
            command_kind=CommandKind.SETTLE_LIVE_RACE,
            target_aggregate=str(command.target_aggregate),
            payload_digest=command.payload_digest,
        )
        if stored is None:
            raise RuntimeError("settlement idempotency authority disappeared")
        value = command.payload.to_value()
        original = value.get("result_submissions")
        settlement = value.get("settlement")
        expected_submissions = [
            item.to_dict() for item in sorted(submissions, key=lambda item: str(item.competitor_id))
        ]
        original_submissions = (
            [item.get("submission") for item in original if isinstance(item, dict)]
            if isinstance(original, list)
            else None
        )
        if (
            value.get("schema_version") != "strathmark-v3-record-and-settle-live-race-v1"
            or value.get("field_id") != str(receipt.field_id)
            or value.get("field_revision") != receipt.upstream_field_revision
            or value.get("receipt_id") != str(receipt.receipt_id)
            or not isinstance(settlement, dict)
            or settlement.get("field_id") != str(receipt.field_id)
            or settlement.get("receipt_id") != str(receipt.receipt_id)
            or original_submissions != expected_submissions
        ):
            self._conflict("idempotency_key_already_binds_different_settlement")
        return str(command.target_aggregate), stored

    def _verify_issue_binding(self, issue_batch_id: str, receipt_id: str) -> None:
        with open_v3_connection(self._services.events.database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT envelope_json FROM v3_events WHERE aggregate_id=? "
                "AND event_kind='issue_batch_issued' ORDER BY global_sequence DESC LIMIT 1",
                (issue_batch_id,),
            ).fetchone()
        if row is None:
            self._conflict("settlement_issue_batch_not_found")
        event = EventEnvelope.from_dict(json.loads(str(row[0])))
        value = event.command.payload.to_value()
        if receipt_id not in {item["receipt_id"] for item in value.get("fields", ())}:
            self._conflict("settlement_receipt_not_in_issue_batch")

    def _field_parent(self, field_id: str) -> tuple[str, str]:
        with open_v3_connection(self._services.events.database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT tournament_id,round_id FROM v3_ingress_snapshots "
                "WHERE entity_kind='field' AND entity_id=? "
                "ORDER BY upstream_revision DESC LIMIT 1",
                (field_id,),
            ).fetchone()
        if row is None:
            self._not_found("field_parent_not_found")
        return str(row[0]), str(row[1])

    def _receipt_sequence(self, receipt_id: str) -> int:
        with open_v3_connection(self._services.events.database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT source_global_sequence FROM v3_field_receipts WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()
        if row is None:
            self._not_found("receipt_not_found")
        return int(row[0])

    def _settlement_id(self, global_sequence: int) -> str:
        event = self._services.events.event_at(global_sequence)
        if event.kind is not EventKind.LIVE_RACE_SETTLED:
            event = self._services.events.event_at(global_sequence - 1)
        if event.kind is not EventKind.LIVE_RACE_SETTLED:
            raise RuntimeError("settlement acknowledgment lacks its authority event")
        return str(event.aggregate_id)

    @staticmethod
    def _conflict(code: str) -> None:
        from strathmark.v3.api.app import TransportError

        raise TransportError(409, code, "V3 request conflicts with current authority.")

    @staticmethod
    def _not_found(code: str) -> None:
        from strathmark.v3.api.app import TransportError

        raise TransportError(404, code, "Requested V3 authority was not found.")


__all__ = ["GatewayServices", "V3ApplicationGateway"]
