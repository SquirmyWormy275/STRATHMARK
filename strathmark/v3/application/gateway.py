"""Concrete transport-to-application adapter for the frozen V3 REST surface.

The gateway owns no prediction logic.  It resolves persisted authority and delegates
to the rolling-job, field-assembly, issue, and settlement application services that
were explicitly composed for this process.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from strathmark.v3.api.router import RequestContext
from strathmark.v3.api.schemas import (
    AssembleFieldResponse,
    ExecuteCommandResponse,
    IssueAcknowledgmentResponse,
    PrepareCardResponse,
    ReceiptLookupResponse,
    SettlementResponse,
    StatusResponse,
)
from strathmark.v3.application.commands import (
    _COMMAND_EVENT,
    CommandRequest,
    EventIntent,
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
from strathmark.v3.application.lifecycle import LifecycleService
from strathmark.v3.application.settlement import SettlementCommand, SettlementService
from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.contracts.commands import CommandEnvelope, CommandKind, InlinePayload
from strathmark.v3.contracts.events import EventEnvelope, EventKind
from strathmark.v3.contracts.identifiers import (
    StableIdentifier,
    deterministic_identifier,
    require_identifier,
)
from strathmark.v3.contracts.receipts import FieldReceipt
from strathmark.v3.contracts.statuses import OfficialResult, ResultStatus
from strathmark.v3.domain.evidence import LiveResultSubmission
from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection
from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore
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
            raise ValueError(
                "gateway settlement reactions must share its exact V3 database"
            )


class V3ApplicationGateway:
    """Packaged implementation of :class:`V3ApplicationPort`."""

    def __init__(
        self,
        services: GatewayServices,
        *,
        clock: Callable[[], str],
        caller_namespace: str = "api",
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

    def execute_command(
        self, payload: dict[str, Any], context: RequestContext
    ) -> ExecuteCommandResponse:
        kind = CommandKind(payload["command_kind"])
        try:
            aggregate_kind, event_kind = _COMMAND_EVENT[kind]
        except KeyError as exc:
            self._conflict("command_requires_specialized_service")
            raise AssertionError from exc
        target = require_identifier(
            payload["target_aggregate"], expected_namespace=aggregate_kind.value
        )
        inline = InlinePayload(
            payload["canonical_payload_json"],
            payload["payload_digest"],
        )
        inline_value = inline.to_value()
        if (
            not isinstance(inline_value, dict)
            or inline_value.get("schema_version") != payload["payload_schema_version"]
        ):
            self._conflict("command_payload_schema_version_differs")
        command = CommandEnvelope(
            kind,
            context.command_id,
            target,
            tuple(
                (item["aggregate_id"], item["version"])
                for item in payload["expected_versions"]
            ),
            context.principal.principal_id,
            inline,
        )
        result_value = {
            "schema_version": "strathmark-v3-command-result-v1",
            "accepted": True,
            "command_kind": kind.value,
            "target_aggregate": str(target),
            "payload_digest": inline.digest,
        }
        prior = self._services.events.lookup_exact_retry(
            principal_id=str(context.principal.principal_id),
            idempotency_key=str(context.command_id),
            command_kind=kind,
            target_aggregate=str(target),
            payload_digest=inline.digest,
        )
        stored = prior
        if stored is None:
            stored = self._services.events.execute(
                CommandRequest(
                    context.principal.principal_id,
                    command,
                    (EventIntent(aggregate_kind, target, event_kind),),
                    "strathmark-v3-command-result-v1",
                    result_value,
                    self._clock(),
                    0,
                ),
                projection_hook=self._services.lifecycle.projections.apply_events,
            )
        canonical_result = stored.result_bytes.decode("utf-8")
        return ExecuteCommandResponse(
            command_id=str(context.command_id),
            disposition="recovered" if prior is not None else "committed",
            result_schema_version=stored.result_schema_version,
            result_digest=stored.result_digest,
            canonical_result_json=canonical_result,
            first_global_sequence=stored.first_global_sequence,
            last_global_sequence=stored.last_global_sequence,
            event_set_digest=stored.event_set_digest,
        )

    def prepare_card(
        self, payload: dict[str, Any], _context: RequestContext
    ) -> PrepareCardResponse:
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
        publication = self._services.jobs.rolling_publication_row(
            card_digest=key["card_digest"]
        )
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
        if receipt_id is None:
            with open_v3_connection(
                self._services.events.database_path, read_only=True
            ) as connection:
                row = connection.execute(
                    "SELECT receipt_id FROM v3_field_receipts WHERE request_identity=?",
                    (payload["request_identity"],),
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
                    tuple(
                        (str(item.competitor_id), item.mark) for item in receipt.marks
                    ),
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

    def settle_result(
        self, payload: dict[str, Any], context: RequestContext
    ) -> SettlementResponse:
        receipt = self._services.fields.verified_receipt(payload["receipt_id"])
        self._verify_issue_binding(payload["issue_batch_id"], payload["receipt_id"])
        tournament_id, round_id = self._field_parent(str(receipt.field_id))
        rows = {item["competitor_id"]: item for item in payload["results"]}
        roster = tuple(str(item) for item in receipt.ordered_competitor_ids)
        if set(rows) != set(roster):
            self._conflict("settlement_requires_complete_issued_roster")
        marks = {str(item.competitor_id): item.mark for item in receipt.marks}
        completion_clocks: dict[str, int] = {}
        for competitor_id, row in rows.items():
            if row["status"] in {"completion", "penalty"}:
                completion_clocks[competitor_id] = (
                    marks[competitor_id] * 1_000
                    + row["raw_time_ms"]
                    + (row["penalty_ms"] or 0)
                )
        ordered = tuple(
            sorted(completion_clocks, key=lambda item: (completion_clocks[item], item))
        )
        placing = {
            competitor_id: index for index, competitor_id in enumerate(ordered, start=1)
        }
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
        self._services.events.verify()
        self._services.fields.verify()
        self._services.jobs.verify()
        with open_v3_connection(
            self._services.events.database_path, read_only=True
        ) as connection:
            open_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM v3_aggregate_heads head "
                    "JOIN v3_events event ON event.aggregate_id=head.aggregate_id "
                    "AND event.aggregate_version=head.aggregate_version "
                    "WHERE head.aggregate_kind='tournament' "
                    "AND event.event_kind='tournament_opened'"
                ).fetchone()[0]
            )
        return StatusResponse(
            service="ready",
            authority_sequence=self._services.events.current_anchor().global_sequence,
            engine_authority="v3",
            open_tournament_count=open_count,
        )

    def _resolve_field(self, field_id: str) -> FrozenFieldRevision:
        require_identifier(field_id, expected_namespace="field")
        with open_v3_connection(
            self._services.events.database_path, read_only=True
        ) as connection:
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
        with open_v3_connection(
            self._services.events.database_path, read_only=True
        ) as connection:
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
            item.to_dict()
            for item in sorted(submissions, key=lambda item: str(item.competitor_id))
        ]
        original_submissions = (
            [item.get("submission") for item in original if isinstance(item, dict)]
            if isinstance(original, list)
            else None
        )
        if (
            value.get("schema_version")
            != "strathmark-v3-record-and-settle-live-race-v1"
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
        with open_v3_connection(
            self._services.events.database_path, read_only=True
        ) as connection:
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
        with open_v3_connection(
            self._services.events.database_path, read_only=True
        ) as connection:
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
        with open_v3_connection(
            self._services.events.database_path, read_only=True
        ) as connection:
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
