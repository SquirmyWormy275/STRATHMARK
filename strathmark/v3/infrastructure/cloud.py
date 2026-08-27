"""Explicit-consent, exact-origin cloud LLM adapter over an injected transport."""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from strathmark.v3.application.coordinator import ProviderResponse
from strathmark.v3.assessors.llm_council import (
    PersistedLeaseAuthority,
    ProviderCallError,
    ProviderKind,
    RawOutputSink,
    SealedLLMJob,
    TransportPort,
    TransportPreflight,
    TransportRequest,
    TransportResponse,
    TransportSecurity,
    execute_response_loop,
    seal_claimed_llm_job,
)
from strathmark.v3.infrastructure.integrity import SignedManifest


@dataclass(frozen=True, slots=True)
class CloudConfig:
    deployment_manifest: SignedManifest
    credential: str = field(repr=False)
    consent: bool


class PolicyEnforcingCloudTransport:
    """Connection-binding guard that withholds payloads until pinned preflight succeeds.

    The injected port performs the platform-specific socket operation.  This boundary
    strips ambient proxy/redirect authority, remembers the verified connection and
    refuses every send that is not bound to that exact preflight.
    """

    def __init__(self, port: TransportPort) -> None:
        if not callable(getattr(port, "preflight", None)) or not callable(
            getattr(port, "send", None)
        ):
            raise ValueError("cloud transport requires a bounded connection port")
        self._port = port
        self._bindings: dict[str, TransportPreflight] = {}

    def preflight(self, request: TransportPreflight) -> TransportSecurity:
        if request.use_ambient_proxy:
            raise ProviderCallError("cloud_proxy_forbidden")
        security = self._port.preflight(request)
        if isinstance(security, TransportSecurity) and security.connection_id:
            self._bindings[security.connection_id] = request
        return security

    def send(self, request: TransportRequest) -> TransportResponse:
        binding = self._bindings.get(request.connection_id)
        if binding is None:
            raise ProviderCallError("cloud_connection_not_preflighted")
        if request.origin != binding.origin or request.allow_redirects or request.use_ambient_proxy:
            raise ProviderCallError("cloud_connection_policy_mismatch")
        response = self._port.send(request)
        if isinstance(response, TransportResponse) and response.returned_origin not in {
            "",
            binding.origin,
        }:
            raise ProviderCallError("redirect_rejected")
        return response


class _DirectHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, address: str, hostname: str, port: int, timeout: float) -> None:
        super().__init__(hostname, port=port, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self) -> None:
        raw = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


class PinnedHTTPSJSONTransport:
    """Concrete direct TLS transport: exact IP, system TLS, no proxy, no redirects."""

    def __init__(self) -> None:
        self._connections: dict[str, tuple[_DirectHTTPSConnection, TransportPreflight]] = {}

    def preflight(self, request: TransportPreflight) -> TransportSecurity:
        if request.use_ambient_proxy or not request.allowed_addresses:
            raise ProviderCallError("cloud_direct_connection_required")
        parsed = urlsplit(request.origin)
        port = parsed.port or 443
        address = request.allowed_addresses[0]
        connection = _DirectHTTPSConnection(
            address, request.hostname, port, request.deadlines.connect_ms / 1_000
        )
        connection.connect()
        if connection.sock is None:
            raise ProviderCallError("cloud_tls_socket_missing")
        peer_address = connection.sock.getpeername()[0]
        if peer_address != address:
            connection.close()
            raise ProviderCallError("cloud_dns_substitution_rejected")
        certificate = connection.sock.getpeercert()
        if not certificate:
            connection.close()
            raise ProviderCallError("cloud_certificate_invalid")
        connection_id = f"connection:{uuid.uuid4().hex}"
        self._connections[connection_id] = (connection, request)
        return TransportSecurity(address, request.hostname, True, connection_id)

    def send(self, request: TransportRequest) -> TransportResponse:
        bound = self._connections.pop(request.connection_id, None)
        if bound is None:
            raise ProviderCallError("cloud_connection_not_preflighted")
        connection, preflight = bound
        if (
            request.origin != preflight.origin
            or request.allow_redirects
            or request.use_ambient_proxy
        ):
            connection.close()
            raise ProviderCallError("cloud_connection_policy_mismatch")
        started = time.monotonic()
        try:
            connection.request("POST", "/", body=request.body, headers=dict(request.headers))
            response = connection.getresponse()
            body = response.read(1_048_577)
            if len(body) > 1_048_576:
                raise ProviderCallError("cloud_response_too_large")
            headers = {key.lower(): value for key, value in response.getheaders()}
            returned_origin = (
                headers.get("location", preflight.origin)
                if 300 <= response.status < 400
                else preflight.origin
            )
            return TransportResponse(
                response.status,
                body,
                returned_origin,
                headers,
                int((time.monotonic() - started) * 1_000),
                headers.get("x-model-version"),
                headers.get("x-model-digest"),
                headers.get("x-api-revision"),
                headers.get("x-canary-digest"),
            )
        finally:
            connection.close()


class CloudAdapter:
    """Fail closed before payload egress when any cloud trust fact is absent."""

    def __init__(
        self,
        config: CloudConfig,
        lease_authority: PersistedLeaseAuthority,
        member,
        transport: TransportPort,
        sink: RawOutputSink,
    ) -> None:
        if not isinstance(config, CloudConfig):
            raise ValueError("cloud adapter requires typed configuration")
        self._config = config
        if not isinstance(lease_authority, PersistedLeaseAuthority):
            raise ValueError("cloud adapter requires repository-backed lease authority")
        self._lease_authority = lease_authority
        if getattr(member, "provider_kind", None) is not ProviderKind.CLOUD:
            raise ValueError("cloud adapter requires one pinned cloud member")
        self._member = member
        if type(transport) is not PinnedHTTPSJSONTransport:
            raise ValueError("cloud adapter requires concrete pinned HTTPS transport")
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
            config = self._validated_config(job)
            if config.api_revision != sealed.member.runtime_version:
                raise ProviderCallError("cloud_api_revision_pin_mismatch")
            security = self._preflight(sealed, config, lifecycle_started_at=lifecycle_started_at)
            self._verify_security(security, config)
            executed = execute_response_loop(
                sealed,
                origin=config.origin,
                headers=(
                    ("authorization", f"Bearer {config.credential}"),
                    ("content-type", "application/json"),
                    ("x-api-revision", config.api_revision),
                ),
                transport=self._transport,
                sink=self._sink,
                connection_id=security.connection_id,
                lifecycle_started_at=lifecycle_started_at,
                forbidden_output_tokens=(config.credential.encode("utf-8"),),
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

    def _validated_config(self, job: object) -> _VerifiedCloudConfig:
        config = self._config
        if not isinstance(config.consent, bool) or not config.consent:
            raise ProviderCallError("cloud_consent_required")
        if not isinstance(config.credential, str) or not config.credential:
            raise ProviderCallError("cloud_credential_required")
        try:
            payload = self._lease_authority.verify_manifest(config.deployment_manifest, job)
        except Exception as exc:
            raise ProviderCallError("cloud_deployment_manifest_invalid") from exc
        if config.deployment_manifest.kind != "cloud_llm_deployment":
            raise ProviderCallError("cloud_deployment_manifest_kind_invalid")
        if set(payload) != {"origin", "hostname", "allowed_addresses", "api_revision"}:
            raise ProviderCallError("cloud_deployment_manifest_schema_invalid")
        verified = _VerifiedCloudConfig(
            payload["origin"],
            payload["hostname"],
            (
                tuple(payload["allowed_addresses"])
                if isinstance(payload["allowed_addresses"], list)
                else ()
            ),
            payload["api_revision"],
            config.credential,
        )
        if not isinstance(verified.api_revision, str) or not verified.api_revision:
            raise ProviderCallError("cloud_api_revision_required")
        if not verified.allowed_addresses:
            raise ProviderCallError("cloud_dns_allowlist_required")
        for address in verified.allowed_addresses:
            try:
                ipaddress.ip_address(address)
            except ValueError as exc:
                raise ProviderCallError("invalid_cloud_dns_allowlist") from exc
        if not isinstance(verified.origin, str) or not isinstance(verified.hostname, str):
            raise ProviderCallError("invalid_cloud_configuration")
        parsed = urlsplit(verified.origin)
        if parsed.scheme != "https":
            raise ProviderCallError("cloud_https_required")
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.hostname != verified.hostname
        ):
            raise ProviderCallError("unallowlisted_cloud_origin")
        return verified

    def _preflight(
        self,
        job: SealedLLMJob,
        config: _VerifiedCloudConfig,
        *,
        lifecycle_started_at: float,
    ) -> TransportSecurity:
        request = TransportPreflight(
            config.origin,
            config.hostname,
            config.allowed_addresses,
            job.deadlines,
        )
        try:
            from strathmark.v3.assessors.llm_council import _bounded_call

            remaining_ms = int(
                (lifecycle_started_at + job.deadlines.overall_ms / 1_000 - time.monotonic()) * 1_000
            )
            if remaining_ms <= 0:
                raise ProviderCallError("overall_timeout")
            security = _bounded_call(
                lambda: self._transport.preflight(request),
                min(job.deadlines.connect_ms, remaining_ms),
                "connect_timeout",
            )
        except ProviderCallError:
            raise
        except Exception as exc:
            raise ProviderCallError("transport_preflight_failure") from exc
        if not isinstance(security, TransportSecurity):
            raise ProviderCallError("invalid_transport_security")
        return security

    @staticmethod
    def _verify_security(security: TransportSecurity, config: _VerifiedCloudConfig) -> None:
        if security.resolved_address not in config.allowed_addresses:
            raise ProviderCallError("cloud_dns_substitution_rejected")
        if security.peer_hostname != config.hostname:
            raise ProviderCallError("cloud_hostname_mismatch")
        if not security.certificate_valid:
            raise ProviderCallError("cloud_certificate_invalid")
        if not isinstance(security.connection_id, str) or not security.connection_id:
            raise ProviderCallError("cloud_connection_binding_missing")


@dataclass(frozen=True, slots=True)
class _VerifiedCloudConfig:
    origin: str
    hostname: str
    allowed_addresses: tuple[str, ...]
    api_revision: str
    credential: str = field(repr=False)


__all__ = [
    "CloudAdapter",
    "CloudConfig",
    "PinnedHTTPSJSONTransport",
    "PolicyEnforcingCloudTransport",
]
