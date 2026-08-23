"""Bounded loopback-only Ollama adapter over an injected transport port."""

from __future__ import annotations

import base64
import hashlib
import http.client
import ipaddress
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

from strathmark.v3.application.coordinator import ProviderResponse
from strathmark.v3.assessors.llm_council import (
    PersistedLeaseAuthority,
    ProviderCallError,
    ProviderKind,
    RawOutputSink,
    TransportPort,
    TransportPreflight,
    TransportResponse,
    TransportSecurity,
    execute_response_loop,
    seal_claimed_llm_job,
)
from strathmark.v3.contracts.commands import (
    MAX_INLINE_PAYLOAD_BYTES,
    BlobReferenceV2,
    BlobRetentionClass,
    InlinePayload,
)
from strathmark.v3.infrastructure.blobs import BlobMetadata, ContentAddressedBlobStore


@dataclass(frozen=True, slots=True)
class RawOutputStorageReference:
    raw_digest: str
    byte_count: int
    inline_parts: tuple[InlinePayload, ...]
    blob_reference: BlobReferenceV2 | None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.raw_digest, str)
            or len(self.raw_digest) != 64
            or any(character not in "0123456789abcdef" for character in self.raw_digest)
            or isinstance(self.byte_count, bool)
            or not isinstance(self.byte_count, int)
            or self.byte_count < 0
            or not isinstance(self.inline_parts, tuple)
            or any(not isinstance(item, InlinePayload) for item in self.inline_parts)
            or (
                self.blob_reference is not None
                and not isinstance(self.blob_reference, BlobReferenceV2)
            )
        ):
            raise ValueError("raw output storage reference is invalid")
        if (self.blob_reference is None) == (not self.inline_parts):
            raise ValueError("raw output storage reference must use exactly one storage form")
        if self.blob_reference is not None and (
            self.blob_reference.digest != self.raw_digest
            or self.blob_reference.byte_count != self.byte_count
        ):
            raise ValueError("raw output blob identity differs")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-llm-raw-output-reference-v1",
            "raw_digest": self.raw_digest,
            "byte_count": self.byte_count,
            "inline_parts": [item.to_dict() for item in self.inline_parts],
            "blob_reference": (
                None if self.blob_reference is None else self.blob_reference.to_dict()
            ),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RawOutputStorageReference:
        if not isinstance(value, Mapping) or set(value) != {
            "schema_version",
            "raw_digest",
            "byte_count",
            "inline_parts",
            "blob_reference",
        }:
            raise ValueError("raw output storage reference fields differ")
        if value["schema_version"] != "strathmark-v3-llm-raw-output-reference-v1":
            raise ValueError("raw output storage reference schema differs")
        inline = value["inline_parts"]
        blob = value["blob_reference"]
        if not isinstance(inline, list):
            raise ValueError("raw output inline reference list is invalid")
        return cls(
            value["raw_digest"],
            value["byte_count"],
            tuple(InlinePayload.from_dict(item) for item in inline),
            None if blob is None else BlobReferenceV2.from_dict(blob),
        )


@dataclass(slots=True)
class ContentAddressedRawOutputSink:
    """Durably retain every provider attempt before validation or correction."""

    store: ContentAddressedBlobStore
    references: list[RawOutputStorageReference]

    def __init__(self, store: ContentAddressedBlobStore) -> None:
        if not isinstance(store, ContentAddressedBlobStore):
            raise ValueError("raw output sink requires content-addressed blob storage")
        self.store = store
        self.references = []

    def publish(self, payload: bytes) -> str:
        if not isinstance(payload, bytes):
            raise ValueError("raw provider output must be immutable bytes")
        raw_digest = hashlib.sha256(payload).hexdigest()
        blob_reference: BlobReferenceV2 | None = None
        parts: tuple[InlinePayload, ...] = ()
        if len(payload) > MAX_INLINE_PAYLOAD_BYTES:
            blob_reference = self.store.publish(
                payload,
                metadata=BlobMetadata(
                    "application/octet-stream",
                    "strathmark-v3-llm-raw-output-v1",
                    BlobRetentionClass.REQUIRED,
                ),
            )
        else:
            chunks = tuple(
                payload[index : index + 24_000] for index in range(0, max(1, len(payload)), 24_000)
            )
            parts = tuple(
                InlinePayload.from_value(
                    {
                        "schema_version": "strathmark-v3-llm-raw-output-chunk-v1",
                        "raw_digest": raw_digest,
                        "part_index": index,
                        "part_count": len(chunks),
                        "raw_base64": base64.b64encode(chunk).decode("ascii"),
                    }
                )
                for index, chunk in enumerate(chunks)
            )
        reference = RawOutputStorageReference(raw_digest, len(payload), parts, blob_reference)
        self.references.append(reference)
        return raw_digest

    def read_raw(self, reference: RawOutputStorageReference) -> bytes:
        if not isinstance(reference, RawOutputStorageReference):
            raise ValueError("raw output storage reference is invalid")
        if reference.blob_reference is not None:
            raw = self.store.read(reference.blob_reference)
        else:
            values = tuple(part.to_value() for part in reference.inline_parts)
            if any(
                value.get("schema_version") != "strathmark-v3-llm-raw-output-chunk-v1"
                or value.get("raw_digest") != reference.raw_digest
                or value.get("part_index") != index
                or value.get("part_count") != len(values)
                for index, value in enumerate(values)
            ):
                raise ValueError("raw output inline chunks differ")
            try:
                raw = b"".join(
                    base64.b64decode(value["raw_base64"], validate=True) for value in values
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("raw output inline chunk is invalid") from exc
        if (
            len(raw) != reference.byte_count
            or hashlib.sha256(raw).hexdigest() != reference.raw_digest
        ):
            raise ValueError("raw output storage digest differs")
        return raw


@dataclass(frozen=True, slots=True)
class OllamaConfig:
    origin: str
    hostname: str


class PinnedLoopbackHTTPTransport:
    """Concrete loopback socket transport with no proxy or redirect authority."""

    def __init__(self) -> None:
        self._connections: dict[str, tuple[http.client.HTTPConnection, TransportPreflight]] = {}

    def preflight(self, request: TransportPreflight) -> TransportSecurity:
        if request.use_ambient_proxy or len(request.allowed_addresses) != 1:
            raise ProviderCallError("local_direct_connection_required")
        address = request.allowed_addresses[0]
        if not ipaddress.ip_address(address).is_loopback or address != request.hostname:
            raise ProviderCallError("local_loopback_binding_required")
        port = urlsplit(request.origin).port or 80
        connection = http.client.HTTPConnection(
            address, port=port, timeout=request.deadlines.connect_ms / 1_000
        )
        connection.connect()
        if connection.sock is None or connection.sock.getpeername()[0] != address:
            connection.close()
            raise ProviderCallError("local_socket_substitution_rejected")
        connection_id = f"connection:{uuid.uuid4().hex}"
        self._connections[connection_id] = (connection, request)
        return TransportSecurity(address, request.hostname, True, connection_id)

    def send(self, request) -> TransportResponse:
        bound = self._connections.pop(request.connection_id, None)
        if bound is None:
            raise ProviderCallError("local_connection_not_preflighted")
        connection, preflight = bound
        if (
            request.origin != preflight.origin
            or request.allow_redirects
            or request.use_ambient_proxy
        ):
            connection.close()
            raise ProviderCallError("local_connection_policy_mismatch")
        started = time.monotonic()
        try:
            connection.request(
                "POST", "/api/generate", body=request.body, headers=dict(request.headers)
            )
            response = connection.getresponse()
            body = response.read(1_048_577)
            if len(body) > 1_048_576:
                raise ProviderCallError("local_response_too_large")
            headers = {key.lower(): value for key, value in response.getheaders()}
            return TransportResponse(
                response.status,
                body,
                preflight.origin,
                headers,
                int((time.monotonic() - started) * 1_000),
                headers.get("x-model-version"),
                headers.get("x-model-digest"),
                headers.get("x-api-revision"),
                headers.get("x-canary-digest"),
            )
        finally:
            connection.close()


class OllamaAdapter:
    """Execute one persisted local-member job without ambient proxy or redirect use."""

    def __init__(
        self,
        config: OllamaConfig,
        lease_authority: PersistedLeaseAuthority,
        member,
        transport: TransportPort,
        sink: RawOutputSink,
    ) -> None:
        if not isinstance(config, OllamaConfig):
            raise ValueError("Ollama adapter requires typed configuration")
        self._config = config
        if not isinstance(lease_authority, PersistedLeaseAuthority):
            raise ValueError("Ollama adapter requires repository-backed lease authority")
        self._lease_authority = lease_authority
        if getattr(member, "provider_kind", None) is not ProviderKind.LOCAL:
            raise ValueError("Ollama adapter requires one pinned local member")
        self._member = member
        if type(transport) is not PinnedLoopbackHTTPTransport:
            raise ValueError("Ollama adapter requires concrete pinned loopback transport")
        self._transport = transport
        self._sink = sink

    @property
    def member(self):
        return self._member

    @property
    def lease_authority(self) -> PersistedLeaseAuthority:
        return self._lease_authority

    def execute(self, job) -> ProviderResponse:
        sealed = seal_claimed_llm_job(job, self._member, self._lease_authority)
        try:
            lifecycle_started_at = time.monotonic() - sealed.queue_elapsed_ms / 1_000
            addresses = _validate_loopback(self._config)
            from strathmark.v3.assessors.llm_council import _bounded_call

            remaining_ms = int(
                (lifecycle_started_at + sealed.deadlines.overall_ms / 1_000 - time.monotonic())
                * 1_000
            )
            if remaining_ms <= 0:
                raise ProviderCallError("overall_timeout")
            security = _bounded_call(
                lambda: _preflight(
                    self._transport,
                    TransportPreflight(
                        self._config.origin,
                        self._config.hostname,
                        addresses,
                        sealed.deadlines,
                    ),
                ),
                min(sealed.deadlines.connect_ms, remaining_ms),
                "connect_timeout",
            )
            _verify_security(security, self._config.hostname, addresses)
            executed = execute_response_loop(
                sealed,
                origin=self._config.origin,
                headers=(("content-type", "application/json"),),
                transport=self._transport,
                sink=self._sink,
                connection_id=security.connection_id,
                lifecycle_started_at=lifecycle_started_at,
            )
        except ProviderCallError as exc:
            raise exc.bind_execution(self._member)
        return ProviderResponse(
            executed.audit.raw_response_digest,
            sealed.evidence_digest,
            sealed.bundle_digest,
            executed,
            executed.execution_audit,
        )


def _validate_loopback(config: OllamaConfig) -> tuple[str, ...]:
    if not isinstance(config.origin, str) or not isinstance(config.hostname, str):
        raise ProviderCallError("invalid_local_configuration")
    parsed = urlsplit(config.origin)
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.hostname != config.hostname
    ):
        raise ProviderCallError("unallowlisted_local_origin")
    try:
        address = ipaddress.ip_address(config.hostname)
    except ValueError as exc:
        raise ProviderCallError("local_hostname_must_be_loopback_ip") from exc
    if not address.is_loopback:
        raise ProviderCallError("local_origin_must_be_loopback")
    return (config.hostname,)


def _preflight(transport: TransportPort, request: TransportPreflight) -> TransportSecurity:
    try:
        result = transport.preflight(request)
    except TimeoutError as exc:
        raise ProviderCallError("connect_timeout") from exc
    except ProviderCallError:
        raise
    except Exception as exc:
        raise ProviderCallError("transport_preflight_failure") from exc
    if not isinstance(result, TransportSecurity):
        raise ProviderCallError("invalid_transport_security")
    return result


def _verify_security(
    security: TransportSecurity, hostname: str, allowed_addresses: tuple[str, ...]
) -> None:
    if security.resolved_address not in allowed_addresses:
        raise ProviderCallError("dns_substitution_rejected")
    if security.peer_hostname != hostname:
        raise ProviderCallError("hostname_mismatch")
    if not isinstance(security.connection_id, str) or not security.connection_id:
        raise ProviderCallError("connection_binding_missing")


__all__ = [
    "ContentAddressedRawOutputSink",
    "OllamaAdapter",
    "OllamaConfig",
    "PinnedLoopbackHTTPTransport",
    "RawOutputStorageReference",
]
