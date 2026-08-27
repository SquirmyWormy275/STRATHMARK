"""Closed Pydantic transport schemas for the frozen V3 consumer boundary."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from strathmark.v3.contracts.canonical import canonical_bytes

_ID = r"^[a-z][a-z0-9_.-]{0,31}:[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,94}$"
_TOURNAMENT_ID = r"^tournament:[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,94}$"
_ROUND_ID = r"^round:[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,94}$"
_FIELD_ID = r"^field:[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,94}$"
_COMPETITOR_ID = r"^competitor:[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,94}$"
_RECEIPT_ID = r"^receipt:[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,94}$"
_DIGEST = r"^[0-9a-f]{64}$"
_UTC_MS = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$"


class StrictV3Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class ErrorResponse(StrictV3Model):
    schema_version: Literal["strathmark-v3-error-v1"] = "strathmark-v3-error-v1"
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    message: str = Field(min_length=1, max_length=256)


class HealthResponse(StrictV3Model):
    schema_version: Literal["strathmark-v3-health-v1"] = "strathmark-v3-health-v1"
    status: Literal["ok"] = "ok"


class CompetitionEngineSelectionInput(StrictV3Model):
    schema_version: Literal["strathmark-v3-competition-engine-selection-v1"]
    scope_id: str = Field(pattern=_TOURNAMENT_ID)
    engine: Literal["v3"]
    mode: Literal["rehearsal", "production"]
    selected_by_actor_id: str = Field(pattern=r"^actor:[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,94}$")
    selected_at_utc: str = Field(pattern=_UTC_MS)
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    consumer_contract_digest: str = Field(pattern=_DIGEST)
    source_commit: str = Field(pattern=r"^[0-9a-f]{7,40}$")


class ScopeOpenRequest(StrictV3Model):
    schema_version: Literal["strathmark-v3-scope-open-request-v1"]
    scope_id: str = Field(pattern=_TOURNAMENT_ID)
    bundle_id: str = Field(pattern=r"^bundle:[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,94}$")
    historical_cutoff_key: str = Field(pattern=r"^history:[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,94}$")
    root_round_ids: list[str] = Field(min_length=1, max_length=128)
    engine_selection: CompetitionEngineSelectionInput
    opened_at_utc: str = Field(pattern=_UTC_MS)
    deadline_ms: int = Field(ge=25, le=10_000)

    @model_validator(mode="after")
    def _scope_identity(self) -> ScopeOpenRequest:
        if self.engine_selection.scope_id != self.scope_id:
            raise ValueError("engine selection scope must match opened scope")
        if self.engine_selection.selected_at_utc > self.opened_at_utc:
            raise ValueError("engine selection cannot follow scope open")
        if len(set(self.root_round_ids)) != len(self.root_round_ids) or any(
            __import__("re").fullmatch(_ROUND_ID, value) is None for value in self.root_round_ids
        ):
            raise ValueError("root round IDs must be unique round identifiers")
        return self


class ScopeOpenResponse(StrictV3Model):
    schema_version: Literal["strathmark-v3-scope-open-response-v1"] = (
        "strathmark-v3-scope-open-response-v1"
    )
    scope_id: str = Field(pattern=_TOURNAMENT_ID)
    selection_digest: str = Field(pattern=_DIGEST)
    authority_sequence: int = Field(ge=1)
    status: Literal["opened", "recovered"]


class SnapshotSyncRequest(StrictV3Model):
    schema_version: Literal["strathmark-v3-snapshot-sync-request-v1"]
    entity_kind: Literal["tournament", "round", "field"]
    entity_id: str = Field(pattern=_ID)
    upstream_revision: int = Field(ge=1)
    tournament_id: str = Field(pattern=_TOURNAMENT_ID)
    round_id: str | None = Field(pattern=_ROUND_ID)
    snapshot: dict[str, Any]
    engine_selection: CompetitionEngineSelectionInput
    synchronized_at_utc: str = Field(pattern=_UTC_MS)
    deadline_ms: int = Field(ge=25, le=10_000)

    @model_validator(mode="after")
    def _closed_snapshot(self) -> SnapshotSyncRequest:
        from strathmark.v3.application.lifecycle import SnapshotKind, UpstreamSnapshot
        from strathmark.v3.contracts.events import CompetitionEngineSelection
        from strathmark.v3.contracts.identifiers import StableIdentifier

        if self.engine_selection.selected_at_utc > self.synchronized_at_utc:
            raise ValueError("engine selection cannot follow snapshot synchronization")
        try:
            UpstreamSnapshot(
                SnapshotKind(self.entity_kind),
                StableIdentifier(self.entity_id),
                self.upstream_revision,
                StableIdentifier(self.tournament_id),
                None if self.round_id is None else StableIdentifier(self.round_id),
                self.snapshot,
                CompetitionEngineSelection.from_dict(self.engine_selection.model_dump(mode="json")),
            )
        except Exception as exc:
            raise ValueError("snapshot is not a closed data-minimized V3 snapshot") from exc
        return self


class SnapshotSyncResponse(StrictV3Model):
    schema_version: Literal["strathmark-v3-snapshot-sync-response-v1"] = (
        "strathmark-v3-snapshot-sync-response-v1"
    )
    entity_id: str = Field(pattern=_ID)
    upstream_revision: int = Field(ge=1)
    snapshot_digest: str = Field(pattern=_DIGEST)
    authority_sequence: int = Field(ge=1)
    status: Literal["synchronized", "recovered"]


class RoundFreezeRequest(StrictV3Model):
    schema_version: Literal["strathmark-v3-round-freeze-request-v1"]
    round_id: str = Field(pattern=_ROUND_ID)
    epoch_revision: int = Field(ge=1)
    historical_cutoff_key: str = Field(pattern=r"^history:[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,94}$")
    closure_ids: list[str] = Field(max_length=128)
    frozen_at_utc: str = Field(pattern=_UTC_MS)
    deadline_ms: int = Field(ge=25, le=10_000)

    @model_validator(mode="after")
    def _closed_closures(self) -> RoundFreezeRequest:
        pattern = r"^round_closure:[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,94}$"
        if len(set(self.closure_ids)) != len(self.closure_ids) or any(
            __import__("re").fullmatch(pattern, value) is None for value in self.closure_ids
        ):
            raise ValueError("closure IDs must be unique round-closure identifiers")
        return self


class RoundFreezeResponse(StrictV3Model):
    schema_version: Literal["strathmark-v3-round-freeze-response-v1"] = (
        "strathmark-v3-round-freeze-response-v1"
    )
    round_id: str = Field(pattern=_ROUND_ID)
    epoch_id: str = Field(pattern=r"^epoch:[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,94}$")
    epoch_revision: int = Field(ge=1)
    authority_sequence: int = Field(ge=1)
    status: Literal["frozen", "recovered"]


class PreFieldForecastRequest(StrictV3Model):
    schema_version: Literal["strathmark-v3-pre-field-forecast-request-v1"]
    tournament_id: str = Field(pattern=_TOURNAMENT_ID)
    round_id: str = Field(pattern=_ROUND_ID)
    forecast_set_revision: int = Field(ge=1)
    ordered_competitor_ids: list[str] = Field(min_length=1, max_length=128)
    target_context: dict[str, Any]
    hard_deadline_at: str = Field(pattern=_UTC_MS)
    requested_at_utc: str = Field(pattern=_UTC_MS)
    deadline_ms: int = Field(ge=25, le=10_000)

    @model_validator(mode="after")
    def _closed_pre_field_request(self) -> PreFieldForecastRequest:
        from strathmark.v3.contracts.evidence import TargetContext

        if (
            len(self.ordered_competitor_ids) != len(set(self.ordered_competitor_ids))
            or any(
                __import__("re").fullmatch(_COMPETITOR_ID, item) is None
                for item in self.ordered_competitor_ids
            )
            or self.requested_at_utc > self.hard_deadline_at
        ):
            raise ValueError("pre-field forecast roster or deadline is invalid")
        try:
            TargetContext.from_dict(self.target_context)
        except Exception as exc:
            raise ValueError("pre-field target context is invalid") from exc
        return self


class PreFieldForecastResponse(StrictV3Model):
    schema_version: Literal["strathmark-v3-pre-field-forecast-response-v1"] = (
        "strathmark-v3-pre-field-forecast-response-v1"
    )
    forecast_set_id: str = Field(pattern=r"^forecast_set:[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,94}$")
    receipt_digest: str = Field(pattern=_DIGEST)
    disposition: Literal["forecasted", "recovered"]
    purpose: Literal["pre_field_seeding_only"]
    issued_mark: Literal[False]
    canonical_receipt_json: str = Field(min_length=2, max_length=1_048_576)
    authority_sequence: int = Field(ge=1)


class ApprovalPageResponse(StrictV3Model):
    schema_version: Literal["strathmark-v3-approval-page-response-v1"] = (
        "strathmark-v3-approval-page-response-v1"
    )
    tournament_id: str = Field(pattern=_TOURNAMENT_ID)
    snapshot_id: str = Field(pattern=r"^approval_snapshot:[0-9a-f]{64}$")
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    total: int = Field(ge=0)
    lifecycle_state: str = Field(min_length=1, max_length=64)
    rows: list[dict[str, Any]] = Field(max_length=100)
    authority_sequence: int = Field(ge=0)


class ApprovalDetailResponse(StrictV3Model):
    schema_version: Literal["strathmark-v3-approval-detail-response-v1"] = (
        "strathmark-v3-approval-detail-response-v1"
    )
    tournament_id: str = Field(pattern=_TOURNAMENT_ID)
    snapshot_id: str = Field(pattern=r"^approval_snapshot:[0-9a-f]{64}$")
    receipt_id: str = Field(pattern=_RECEIPT_ID)
    detail: dict[str, Any]
    authority_sequence: int = Field(ge=1)


class RoundCloseRequest(StrictV3Model):
    schema_version: Literal["strathmark-v3-round-close-request-v1"]
    round_id: str = Field(pattern=_ROUND_ID)
    closed_at_utc: str = Field(pattern=_UTC_MS)
    deadline_ms: int = Field(ge=25, le=10_000)


class RoundCloseResponse(StrictV3Model):
    schema_version: Literal["strathmark-v3-round-close-response-v1"] = (
        "strathmark-v3-round-close-response-v1"
    )
    round_id: str = Field(pattern=_ROUND_ID)
    closure_id: str = Field(pattern=r"^round_closure:[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,94}$")
    authority_sequence: int = Field(ge=1)
    status: Literal["closed", "recovered"]


class ScopeCloseRequest(StrictV3Model):
    schema_version: Literal["strathmark-v3-scope-close-request-v1"]
    scope_id: str = Field(pattern=_TOURNAMENT_ID)
    closed_at_utc: str = Field(pattern=_UTC_MS)
    deadline_ms: int = Field(ge=25, le=10_000)


class ScopeCloseResponse(StrictV3Model):
    schema_version: Literal["strathmark-v3-scope-close-response-v1"] = (
        "strathmark-v3-scope-close-response-v1"
    )
    scope_id: str = Field(pattern=_TOURNAMENT_ID)
    authority_sequence: int = Field(ge=1)
    status: Literal["closed", "recovered"]


class PrepareCardRequest(StrictV3Model):
    schema_version: Literal["strathmark-v3-card-preparation-request-v1"]
    tournament_id: str = Field(pattern=_ID)
    round_id: str = Field(pattern=_ID)
    field_id: str = Field(pattern=_ID)
    competitor_id: str = Field(pattern=_ID)
    source_revision: int = Field(ge=1)
    target_context_digest: str = Field(pattern=_DIGEST)
    deadline_ms: int = Field(ge=25, le=60_000)


class PrepareCardResponse(StrictV3Model):
    schema_version: Literal["strathmark-v3-card-preparation-response-v1"] = (
        "strathmark-v3-card-preparation-response-v1"
    )
    job_id: str = Field(pattern=_ID)
    status: Literal["queued", "already_queued", "ready"]
    authority_sequence: int = Field(ge=0)


class AssembleFieldRequest(StrictV3Model):
    schema_version: Literal["strathmark-v3-field-assembly-request-v1"]
    field_id: str = Field(pattern=_ID)
    upstream_field_revision: int = Field(ge=1)
    ordered_competitor_ids: list[str] = Field(min_length=2, max_length=64)
    deadline_ms: int = Field(ge=25, le=10_000)

    @model_validator(mode="after")
    def _closed_roster(self) -> AssembleFieldRequest:
        if len(set(self.ordered_competitor_ids)) != len(self.ordered_competitor_ids):
            raise ValueError("ordered competitor IDs must be unique")
        if any(
            __import__("re").fullmatch(_ID, value) is None for value in self.ordered_competitor_ids
        ):
            raise ValueError("ordered competitor IDs must be namespaced identifiers")
        return self


class AssembleFieldResponse(StrictV3Model):
    schema_version: Literal["strathmark-v3-field-assembly-response-v1"] = (
        "strathmark-v3-field-assembly-response-v1"
    )
    receipt_id: str = Field(pattern=_ID)
    receipt_digest: str = Field(pattern=_DIGEST)
    disposition: Literal["prepared", "recovered"]
    canonical_receipt_json: str = Field(min_length=2, max_length=1_048_576)
    authority_sequence: int = Field(ge=1)


class ReceiptLookupRequest(StrictV3Model):
    schema_version: Literal["strathmark-v3-receipt-lookup-request-v1"]
    request_identity: str = Field(pattern=_ID)
    receipt_id: str | None = Field(pattern=_ID)
    deadline_ms: int = Field(ge=25, le=5_000)


class ReceiptLookupResponse(StrictV3Model):
    schema_version: Literal["strathmark-v3-receipt-lookup-response-v1"] = (
        "strathmark-v3-receipt-lookup-response-v1"
    )
    found: bool
    receipt_id: str | None = Field(default=None, pattern=_ID)
    receipt_digest: str | None = Field(default=None, pattern=_DIGEST)
    canonical_receipt_json: str | None = Field(default=None, max_length=1_048_576)
    authority_sequence: int = Field(ge=0)

    @model_validator(mode="after")
    def _complete_result(self) -> ReceiptLookupResponse:
        present = (self.receipt_id, self.receipt_digest, self.canonical_receipt_json)
        if self.found != all(value is not None for value in present):
            raise ValueError("receipt lookup result must be complete exactly when found")
        return self


class ReceiptBinding(StrictV3Model):
    receipt_id: str = Field(pattern=_ID)
    receipt_digest: str = Field(pattern=_DIGEST)


class ApprovalReceiptBinding(StrictV3Model):
    field_id: str = Field(pattern=_FIELD_ID)
    receipt_id: str = Field(pattern=_RECEIPT_ID)
    receipt_digest: str = Field(pattern=_DIGEST)
    receipt_revision: int = Field(ge=1)
    upstream_field_revision: int = Field(ge=1)
    row_digest: str = Field(pattern=_DIGEST)
    call_order: int = Field(ge=0)


class ApprovalDecisionRequest(StrictV3Model):
    schema_version: Literal["strathmark-v3-approval-decision-request-v1"]
    tournament_id: str = Field(pattern=_TOURNAMENT_ID)
    snapshot_id: str = Field(pattern=r"^approval_snapshot:[0-9a-f]{64}$")
    action: Literal[
        "ordinary_batch_accept",
        "degraded_batch_accept",
        "individual_accept",
        "override_submitted",
        "exclude",
        "defer",
    ]
    selected: list[ApprovalReceiptBinding] = Field(max_length=100)
    excluded: list[ApprovalReceiptBinding] = Field(max_length=100)
    actor_metadata: dict[str, Any]
    reason_code: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
    superseded_receipt_id: str | None = Field(pattern=_RECEIPT_ID)
    decided_at_utc: str = Field(pattern=_UTC_MS)
    deadline_ms: int = Field(ge=25, le=10_000)

    @model_validator(mode="after")
    def _closed_decision(self) -> ApprovalDecisionRequest:
        try:
            canonical_bytes(self.actor_metadata, max_bytes=2_048)
        except Exception as exc:
            raise ValueError("approval actor metadata must be a bounded JSON object") from exc
        if not self.selected and not self.excluded:
            raise ValueError("approval decision requires receipt bindings")
        if len(self.selected) + len(self.excluded) > 100:
            raise ValueError("approval decision has too many receipt bindings")

        def order(item: ApprovalReceiptBinding) -> tuple[int, str, str]:
            return item.call_order, item.field_id, item.receipt_id

        if self.selected != sorted(self.selected, key=order) or self.excluded != sorted(
            self.excluded, key=order
        ):
            raise ValueError("approval receipt bindings must be canonically sorted")
        selected = tuple((item.field_id, item.receipt_id) for item in self.selected)
        excluded = tuple((item.field_id, item.receipt_id) for item in self.excluded)
        if (
            len(set(selected)) != len(selected)
            or len(set(excluded)) != len(excluded)
            or set(selected) & set(excluded)
        ):
            raise ValueError("approval receipt bindings must be unique and disjoint")
        if self.action in {"ordinary_batch_accept", "degraded_batch_accept"}:
            if not self.selected:
                raise ValueError("batch approval requires a selected receipt")
        elif len(self.selected) != 1 or self.excluded:
            raise ValueError("individual approval requires exactly one selected receipt")
        if (self.action == "override_submitted") != (self.superseded_receipt_id is not None):
            raise ValueError("only an override binds a superseded receipt")
        if (
            self.superseded_receipt_id is not None
            and self.superseded_receipt_id == self.selected[0].receipt_id
        ):
            raise ValueError("override predecessor must differ from the selected receipt")
        return self


class ApprovalDecisionResult(StrictV3Model):
    receipt_id: str = Field(pattern=_RECEIPT_ID)
    decision_state: Literal["accepted", "override-submitted", "excluded", "deferred"]


class ApprovalDecisionResponse(StrictV3Model):
    schema_version: Literal["strathmark-v3-approval-decision-response-v1"] = (
        "strathmark-v3-approval-decision-response-v1"
    )
    command_id: str = Field(pattern=_ID)
    caller_namespace: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    tournament_id: str = Field(pattern=_TOURNAMENT_ID)
    snapshot_id: str = Field(pattern=r"^approval_snapshot:[0-9a-f]{64}$")
    action: Literal[
        "ordinary_batch_accept",
        "degraded_batch_accept",
        "individual_accept",
        "override_submitted",
        "exclude",
        "defer",
    ]
    decisions: tuple[ApprovalDecisionResult, ...] = Field(min_length=1, max_length=100)
    decided_at_utc: str = Field(pattern=_UTC_MS)
    actor_metadata_digest: str = Field(pattern=_DIGEST)
    receipt_bindings_digest: str = Field(pattern=_DIGEST)
    command_digest: str = Field(pattern=_DIGEST)
    decision_digest: str = Field(pattern=_DIGEST)
    authority_sequence: int = Field(ge=1)


class IssueAcknowledgmentRequest(StrictV3Model):
    schema_version: Literal["strathmark-v3-issue-acknowledgment-request-v1"]
    upstream_issue_id: str = Field(pattern=_ID)
    receipt_bindings: list[ReceiptBinding] = Field(min_length=1, max_length=64)
    issued_at_utc: str = Field(pattern=_UTC_MS)
    deadline_ms: int = Field(ge=25, le=10_000)

    @model_validator(mode="after")
    def _unique_receipts(self) -> IssueAcknowledgmentRequest:
        identities = tuple(item.receipt_id for item in self.receipt_bindings)
        if len(set(identities)) != len(identities):
            raise ValueError("issue acknowledgment cannot repeat a receipt")
        return self


class IssueAcknowledgmentResponse(StrictV3Model):
    schema_version: Literal["strathmark-v3-issue-acknowledgment-response-v1"] = (
        "strathmark-v3-issue-acknowledgment-response-v1"
    )
    issue_batch_id: str = Field(pattern=_ID)
    receipt_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    authority_sequence: int = Field(ge=1)
    recovery_marker_digest: str = Field(pattern=_DIGEST)


class ResultRow(StrictV3Model):
    competitor_id: str = Field(pattern=_ID)
    status: Literal["completion", "dnf", "dq", "dns", "void", "penalty"]
    raw_time_ms: int | None = Field(ge=1, le=600_000)
    penalty_ms: int | None = Field(ge=1, le=600_000)
    source_revision: int = Field(ge=1)

    @model_validator(mode="after")
    def _status_fields(self) -> ResultRow:
        if self.status == "completion" and (
            self.raw_time_ms is None or self.penalty_ms is not None
        ):
            raise ValueError("completion requires raw time and no penalty")
        if self.status == "penalty" and (self.raw_time_ms is None or self.penalty_ms is None):
            raise ValueError("penalty requires raw time and penalty")
        if self.status not in {"completion", "penalty"} and (
            self.raw_time_ms is not None or self.penalty_ms is not None
        ):
            raise ValueError("nonfinish/void result cannot carry numeric time")
        return self


class SettlementRequest(StrictV3Model):
    schema_version: Literal["strathmark-v3-settlement-request-v1"]
    issue_batch_id: str = Field(pattern=_ID)
    receipt_id: str = Field(pattern=_ID)
    results: list[ResultRow] = Field(min_length=1, max_length=64)
    observed_at_utc: str = Field(pattern=_UTC_MS)
    deadline_ms: int = Field(ge=25, le=10_000)

    @model_validator(mode="after")
    def _unique_results(self) -> SettlementRequest:
        identities = tuple(item.competitor_id for item in self.results)
        if len(set(identities)) != len(identities):
            raise ValueError("settlement cannot repeat a competitor")
        return self


class SettlementResponse(StrictV3Model):
    schema_version: Literal["strathmark-v3-settlement-response-v1"] = (
        "strathmark-v3-settlement-response-v1"
    )
    settlement_id: str = Field(pattern=_ID)
    receipt_id: str = Field(pattern=_ID)
    authority_sequence: int = Field(ge=1)
    status: Literal["recorded", "recovered"]


class StatusResponse(StrictV3Model):
    schema_version: Literal["strathmark-v3-status-response-v1"] = "strathmark-v3-status-response-v1"
    service: Literal["ready", "degraded", "stopped"] = Field(
        description="Health of this V3 service process; not a production-authority claim."
    )
    authority_sequence: int = Field(ge=0)
    engine_authority: Literal["v2", "v3", "traditional_manual"] = Field(
        description="Deprecated compatibility alias for production_authority."
    )
    v3_readiness: Literal["candidate", "production"] = Field(
        description="V3 release posture, independent of local process health."
    )
    production_authority: Literal["v2", "v3", "traditional_manual"] = Field(
        description="Current externally verified production authority."
    )
    cutover_receipt_digest: str | None = Field(pattern=_DIGEST)
    cutover_verified_at_utc: str | None = Field(pattern=_UTC_MS)
    deep_verification_state: Literal["verified", "unavailable"]
    event_last_deep_verified_at_utc: str = Field(pattern=_UTC_MS)
    event_checkpoint_digest: str = Field(pattern=_DIGEST)
    field_last_deep_verified_at_utc: str = Field(pattern=_UTC_MS)
    field_checkpoint_digest: str = Field(pattern=_DIGEST)
    job_last_deep_verified_at_utc: str = Field(pattern=_UTC_MS)
    job_checkpoint_digest: str = Field(pattern=_DIGEST)
    open_tournament_count: int = Field(ge=0)
    v3_option_state: Literal["rehearsal_ready", "production_ready", "ineligible"]
    rehearsal_eligible: bool
    production_eligible: bool
    eligibility_reason_codes: tuple[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")], ...]
    consumer_contract_version: str = Field(min_length=1, max_length=128)
    consumer_contract_digest: str = Field(pattern=_DIGEST)
    source_commit: str | None = Field(pattern=r"^[0-9a-f]{7,40}$")

    @model_validator(mode="after")
    def _consistent_authority(self) -> StatusResponse:
        if self.engine_authority != self.production_authority:
            raise ValueError("legacy engine authority must mirror production authority")
        if (self.v3_readiness == "production") != (self.production_authority == "v3"):
            raise ValueError("V3 production readiness requires verified V3 authority")
        cutover_evidence_present = (
            self.cutover_receipt_digest is not None and self.cutover_verified_at_utc is not None
        )
        if (self.cutover_receipt_digest is None) != (self.cutover_verified_at_utc is None):
            raise ValueError("cutover evidence must be complete")
        if (self.production_authority == "v3") != cutover_evidence_present:
            raise ValueError("V3 production authority requires cutover evidence")
        expected_flags = {
            "rehearsal_ready": (True, False),
            "production_ready": (True, True),
            "ineligible": (False, False),
        }[self.v3_option_state]
        if (self.rehearsal_eligible, self.production_eligible) != expected_flags:
            raise ValueError("V3 readiness evidence is internally inconsistent")
        if self.production_eligible != (self.production_authority == "v3"):
            raise ValueError("V3 readiness evidence differs from production authority")
        if self.eligibility_reason_codes != tuple(sorted(set(self.eligibility_reason_codes))):
            raise ValueError("V3 readiness evidence reason codes must be canonical")
        if self.v3_option_state == "rehearsal_ready" and (
            "production_cutover_not_verified" not in self.eligibility_reason_codes
        ):
            raise ValueError("V3 readiness evidence must explain rehearsal-only eligibility")
        if (self.source_commit is not None) != self.rehearsal_eligible:
            raise ValueError("V3 readiness evidence requires exact service source identity")
        return self


class CredentialRotationRequest(StrictV3Model):
    schema_version: Literal["strathmark-v3-credential-rotation-request-v1"]
    overlap_seconds: int = Field(ge=1, le=900)


class CredentialRotationResponse(StrictV3Model):
    schema_version: Literal["strathmark-v3-credential-rotation-response-v1"] = (
        "strathmark-v3-credential-rotation-response-v1"
    )
    credential: str = Field(min_length=30, max_length=264, repr=False)
    key_id_digest: str = Field(pattern=_DIGEST)
    principal_id: str = Field(pattern=_ID)
    overlap_seconds: int = Field(ge=1, le=900)


class CredentialRevocationRequest(StrictV3Model):
    schema_version: Literal["strathmark-v3-credential-revocation-request-v1"]
    key_id_digest: str = Field(pattern=_DIGEST)


class CredentialRevocationResponse(StrictV3Model):
    schema_version: Literal["strathmark-v3-credential-revocation-response-v1"] = (
        "strathmark-v3-credential-revocation-response-v1"
    )
    key_id_digest: str = Field(pattern=_DIGEST)
    status: Literal["revoked"] = "revoked"


__all__ = [name for name in globals() if name.endswith(("Request", "Response"))]
