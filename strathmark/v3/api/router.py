"""V3 HTTP routing mapped only to dependency-injected application ports."""

from __future__ import annotations

import asyncio
import functools
import inspect
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Protocol

from fastapi import APIRouter, Request, Response

from strathmark.v3.api.auth import CredentialError, ServiceCredentialRegistry, ServicePrincipal
from strathmark.v3.api.schemas import (
    AssembleFieldRequest,
    AssembleFieldResponse,
    CredentialRevocationRequest,
    CredentialRevocationResponse,
    CredentialRotationRequest,
    CredentialRotationResponse,
    ExecuteCommandRequest,
    ExecuteCommandResponse,
    HealthResponse,
    IssueAcknowledgmentRequest,
    IssueAcknowledgmentResponse,
    PrepareCardRequest,
    PrepareCardResponse,
    ReceiptLookupRequest,
    ReceiptLookupResponse,
    SettlementRequest,
    SettlementResponse,
    StatusResponse,
)
from strathmark.v3.contracts.identifiers import IdempotencyKey


class BlockingOperationTimeout(TimeoutError):
    """Bounded blocking work did not finish within the caller's deadline."""


class BoundedBlockingExecutor:
    """Run blocking SQLite/OS credential work off-loop with bounded concurrency."""

    def __init__(self, *, max_concurrency: int) -> None:
        if (
            isinstance(max_concurrency, bool)
            or not isinstance(max_concurrency, int)
            or not 1 <= max_concurrency <= 64
        ):
            raise ValueError("blocking concurrency must be an integer from 1 to 64")
        self._slots = asyncio.Semaphore(max_concurrency)
        self._active_count = 0

    @property
    def active_count(self) -> int:
        return self._active_count

    async def run(self, function, /, *args, timeout_ms: int, **kwargs):
        if not callable(function):
            raise TypeError("blocking operation must be callable")
        if (
            isinstance(timeout_ms, bool)
            or not isinstance(timeout_ms, int)
            or not 1 <= timeout_ms <= 60_000
        ):
            raise ValueError("blocking operation timeout must be from 1 to 60000 milliseconds")
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        try:
            await asyncio.wait_for(self._slots.acquire(), timeout=timeout_ms / 1000)
        except TimeoutError as exc:
            raise BlockingOperationTimeout("blocking operation capacity deadline expired") from exc
        self._active_count += 1
        try:
            task = asyncio.create_task(
                asyncio.to_thread(functools.partial(function, *args, **kwargs))
            )
        except BaseException:
            self._active_count -= 1
            self._slots.release()
            raise

        def finished(completed: asyncio.Task) -> None:
            self._active_count -= 1
            self._slots.release()
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(finished)
        remaining = timeout_ms / 1000 - (loop.time() - started_at)
        if remaining <= 0:
            raise BlockingOperationTimeout("blocking operation deadline expired")
        completed, _pending = await asyncio.wait((task,), timeout=remaining)
        if not completed:
            raise BlockingOperationTimeout("blocking operation deadline expired")
        return task.result()


@dataclass(frozen=True, slots=True)
class RequestContext:
    principal: ServicePrincipal
    command_id: IdempotencyKey
    external_idempotency_key: str
    upstream_actor_id: str | None = None
    upstream_action: str | None = None
    upstream_trace_id: str | None = None


class V3ApplicationPort(Protocol):
    def execute_command(
        self, payload: dict[str, Any], context: RequestContext
    ) -> ExecuteCommandResponse | Awaitable[ExecuteCommandResponse]: ...

    def prepare_card(
        self, payload: dict[str, Any], context: RequestContext
    ) -> PrepareCardResponse | Awaitable[PrepareCardResponse]: ...

    def assemble_field(
        self, payload: dict[str, Any], context: RequestContext
    ) -> AssembleFieldResponse | Awaitable[AssembleFieldResponse]: ...

    def lookup_receipt(
        self, payload: dict[str, Any], context: RequestContext
    ) -> ReceiptLookupResponse | Awaitable[ReceiptLookupResponse]: ...

    def acknowledge_issue(
        self, payload: dict[str, Any], context: RequestContext
    ) -> IssueAcknowledgmentResponse | Awaitable[IssueAcknowledgmentResponse]: ...

    def settle_result(
        self, payload: dict[str, Any], context: RequestContext
    ) -> SettlementResponse | Awaitable[SettlementResponse]: ...

    def status(self, context: RequestContext) -> StatusResponse | Awaitable[StatusResponse]: ...


async def _invoke(method, *arguments, deadline_ms: int):
    from strathmark.v3.api.app import TransportError

    try:
        if inspect.iscoroutinefunction(method):
            pending = method(*arguments)
        else:
            pending = asyncio.to_thread(method, *arguments)
        return await asyncio.wait_for(pending, timeout=deadline_ms / 1000)
    except TimeoutError as exc:
        raise TransportError(
            504, "operation_deadline_exceeded", "V3 operation deadline expired."
        ) from exc


def _context(request: Request, *, require_idempotency: bool = True) -> RequestContext:
    principal = getattr(request.state, "service_principal", None)
    if not isinstance(principal, ServicePrincipal):
        raise RuntimeError("authenticated service principal was not installed")
    external = request.headers.get("idempotency-key")
    if require_idempotency and (not isinstance(external, str) or not 1 <= len(external) <= 128):
        from strathmark.v3.api.app import TransportError

        raise TransportError(400, "idempotency_key_required", "Idempotency-Key is required.")
    external = external or "status-read"
    try:
        command_id = principal.idempotency_key(external)
    except CredentialError as exc:
        from strathmark.v3.api.app import TransportError

        raise TransportError(400, "idempotency_key_invalid", "Idempotency-Key is invalid.") from exc
    upstream_actor = request.headers.get("x-strathmark-upstream-actor")
    upstream_action = request.headers.get("x-strathmark-upstream-action")
    upstream_trace = request.headers.get("x-strathmark-upstream-trace")
    if (
        upstream_actor is not None
        and re.fullmatch(
            r"[a-z][a-z0-9_.-]{0,31}:[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,94}", upstream_actor
        )
        is None
    ):
        from strathmark.v3.api.app import TransportError

        raise TransportError(400, "upstream_audit_invalid", "Upstream audit metadata is invalid.")
    if (
        upstream_action is not None
        and re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", upstream_action) is None
    ):
        from strathmark.v3.api.app import TransportError

        raise TransportError(400, "upstream_audit_invalid", "Upstream audit metadata is invalid.")
    if (
        upstream_trace is not None
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", upstream_trace) is None
    ):
        from strathmark.v3.api.app import TransportError

        raise TransportError(400, "upstream_audit_invalid", "Upstream audit metadata is invalid.")
    return RequestContext(
        principal,
        command_id,
        external,
        upstream_actor_id=upstream_actor,
        upstream_action=upstream_action,
        upstream_trace_id=upstream_trace,
    )


def create_router(
    *,
    gateway: V3ApplicationPort,
    credentials: ServiceCredentialRegistry,
    blocking_executor: BoundedBlockingExecutor | None = None,
    credential_operation_timeout_ms: int = 5_000,
) -> APIRouter:
    router = APIRouter(prefix="/v3")
    blocking = blocking_executor or BoundedBlockingExecutor(max_concurrency=4)

    async def credential_call(function, /, *args, **kwargs):
        from strathmark.v3.api.app import TransportError

        try:
            return await blocking.run(
                function,
                *args,
                timeout_ms=credential_operation_timeout_ms,
                **kwargs,
            )
        except BlockingOperationTimeout as exc:
            raise TransportError(
                504,
                "operation_deadline_exceeded",
                "V3 operation deadline expired.",
            ) from exc

    @router.get("/health", response_model=HealthResponse, operation_id="v3_health")
    async def health() -> HealthResponse:
        return HealthResponse()

    @router.post(
        "/commands/execute",
        response_model=ExecuteCommandResponse,
        operation_id="v3_execute_command",
    )
    async def execute_command(
        payload: ExecuteCommandRequest, request: Request
    ) -> ExecuteCommandResponse:
        context = _context(request)
        response = await _invoke(
            gateway.execute_command,
            payload.model_dump(mode="json"),
            context,
            deadline_ms=payload.deadline_ms,
        )
        if not isinstance(response, ExecuteCommandResponse) or response.command_id != str(
            context.command_id
        ):
            raise RuntimeError("application command result does not bind transport identity")
        return response

    @router.post(
        "/cards/prepare",
        response_model=PrepareCardResponse,
        status_code=202,
        operation_id="v3_prepare_card",
    )
    async def prepare_card(payload: PrepareCardRequest, request: Request) -> PrepareCardResponse:
        return await _invoke(
            gateway.prepare_card,
            payload.model_dump(mode="json"),
            _context(request),
            deadline_ms=payload.deadline_ms,
        )

    @router.post(
        "/fields/assemble",
        response_model=AssembleFieldResponse,
        operation_id="v3_assemble_field",
    )
    async def assemble_field(
        payload: AssembleFieldRequest, request: Request
    ) -> AssembleFieldResponse:
        return await _invoke(
            gateway.assemble_field,
            payload.model_dump(mode="json"),
            _context(request),
            deadline_ms=payload.deadline_ms,
        )

    @router.post(
        "/receipts/lookup",
        response_model=ReceiptLookupResponse,
        operation_id="v3_lookup_receipt",
    )
    async def lookup_receipt(
        payload: ReceiptLookupRequest, request: Request
    ) -> ReceiptLookupResponse:
        return await _invoke(
            gateway.lookup_receipt,
            payload.model_dump(mode="json"),
            _context(request),
            deadline_ms=payload.deadline_ms,
        )

    @router.post(
        "/issues/acknowledge",
        response_model=IssueAcknowledgmentResponse,
        operation_id="v3_acknowledge_issue",
    )
    async def acknowledge_issue(
        payload: IssueAcknowledgmentRequest, request: Request
    ) -> IssueAcknowledgmentResponse:
        return await _invoke(
            gateway.acknowledge_issue,
            payload.model_dump(mode="json"),
            _context(request),
            deadline_ms=payload.deadline_ms,
        )

    @router.post(
        "/results/settle",
        response_model=SettlementResponse,
        operation_id="v3_settle_result",
    )
    async def settle_result(payload: SettlementRequest, request: Request) -> SettlementResponse:
        return await _invoke(
            gateway.settle_result,
            payload.model_dump(mode="json"),
            _context(request),
            deadline_ms=payload.deadline_ms,
        )

    @router.get("/status", response_model=StatusResponse, operation_id="v3_status")
    async def status(request: Request) -> StatusResponse:
        return await _invoke(
            gateway.status, _context(request, require_idempotency=False), deadline_ms=5_000
        )

    @router.post(
        "/credentials/rotate",
        response_model=CredentialRotationResponse,
        operation_id="v3_rotate_credential",
    )
    async def rotate_credential(
        payload: CredentialRotationRequest, request: Request, response: Response
    ) -> CredentialRotationResponse:
        context = _context(request)
        issued = await credential_call(
            credentials.rotate,
            context.principal,
            overlap_seconds=payload.overlap_seconds,
            command_id=context.command_id,
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return CredentialRotationResponse(
            credential=issued.credential,
            key_id_digest=issued.key_id_digest,
            principal_id=str(issued.principal_id),
            overlap_seconds=payload.overlap_seconds,
        )

    @router.post(
        "/credentials/revoke",
        response_model=CredentialRevocationResponse,
        operation_id="v3_revoke_credential",
    )
    async def revoke_credential(
        payload: CredentialRevocationRequest, request: Request
    ) -> CredentialRevocationResponse:
        context = _context(request)
        await credential_call(
            credentials.revoke,
            context.principal,
            payload.key_id_digest,
            command_id=context.command_id,
        )
        return CredentialRevocationResponse(key_id_digest=payload.key_id_digest)

    return router


__all__ = [
    "BlockingOperationTimeout",
    "BoundedBlockingExecutor",
    "RequestContext",
    "V3ApplicationPort",
    "create_router",
]
