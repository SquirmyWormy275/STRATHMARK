"""Injected FastAPI application factory and pre-body security boundary for V3."""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, MutableMapping

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from strathmark.v3.api.auth import CredentialError, ServiceCredentialRegistry
from strathmark.v3.api.router import (
    BlockingOperationTimeout,
    BoundedBlockingExecutor,
    V3ApplicationPort,
    create_router,
)
from strathmark.v3.api.schemas import ErrorResponse

MAX_V3_REQUEST_BODY_BYTES = 1_048_576
MAX_V3_INFLIGHT_REQUESTS = 64
MAX_V3_BLOCKING_OPERATIONS = 16
_PUBLIC_PATHS = frozenset({"/v3/health"})
_FORWARDED_HEADERS = frozenset(
    {b"forwarded", b"x-forwarded-for", b"x-forwarded-host", b"x-forwarded-proto"}
)
_SINGLETON_HEADERS = frozenset(
    {
        b"authorization",
        b"content-length",
        b"content-type",
        b"content-encoding",
        b"host",
        b"idempotency-key",
        b"x-strathmark-upstream-action",
        b"x-strathmark-upstream-actor",
        b"x-strathmark-upstream-trace",
        *_FORWARDED_HEADERS,
    }
)


class TransportError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ListenerSecurityPolicy:
    """Startup-time listener contract; the ASGI server supplies verified cert identity."""

    host: str = "127.0.0.1"
    port: int = 8787
    server_hostname: str | None = None
    tls_certificate: Path | None = None
    tls_private_key: Path | None = None
    pinned_client_ca: Path | None = None
    trust_proxy_headers: bool = False
    follow_redirects: bool = False
    ambient_proxy: bool = False
    _client_ca_digest: str = field(init=False, repr=False, default="")

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not self.host:
            raise ValueError("listener host must be explicit")
        if (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not 1 <= self.port <= 65535
        ):
            raise ValueError("listener port must be an integer from 1 to 65535")
        if self.trust_proxy_headers or self.ambient_proxy:
            raise ValueError("ambient and forwarded proxy identity is forbidden")
        if self.follow_redirects:
            raise ValueError("credential-bearing transport cannot follow redirects")
        if self.is_loopback:
            return
        if not isinstance(self.server_hostname, str) or not self.server_hostname:
            raise ValueError("non-loopback mutual TLS requires an expected server hostname")
        paths = (self.tls_certificate, self.tls_private_key, self.pinned_client_ca)
        if any(
            not isinstance(path, Path) or not path.is_file() or path.stat().st_size == 0
            for path in paths
        ):
            raise ValueError(
                "non-loopback mutual TLS requires server certificate, private key, and pinned client CA"
            )
        digest = _validate_mtls_material(
            self.tls_certificate,
            self.tls_private_key,
            self.pinned_client_ca,
            self.server_hostname,
        )
        object.__setattr__(self, "_client_ca_digest", digest)

    @property
    def is_loopback(self) -> bool:
        if self.host.casefold() == "localhost":
            return True
        try:
            return ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            return False

    def certificate_principal(self, scope: Mapping[str, Any]) -> str | None:
        if self.is_loopback:
            return None
        extensions = scope.get("extensions")
        if not isinstance(extensions, Mapping):
            raise CredentialError("verified client certificate is required")
        certificate = extensions.get("strathmark.verified_client_certificate")
        if not isinstance(certificate, Mapping) or certificate.get("verified") is not True:
            raise CredentialError("verified client certificate is required")
        if certificate.get("server_hostname") != self.server_hostname:
            raise CredentialError("mutual TLS hostname validation failed")
        if not hmac.compare_digest(
            str(certificate.get("client_ca_digest", "")), self._client_ca_digest
        ):
            raise CredentialError("mutual TLS client CA binding failed")
        principal = certificate.get("principal_id")
        if not isinstance(principal, str):
            raise CredentialError("verified client certificate principal is required")
        return principal

    @property
    def uvicorn_ssl_kwargs(self) -> dict[str, object]:
        """Exact server arguments required to make a non-loopback policy enforceable."""

        if self.is_loopback:
            return {}
        assert self.tls_certificate is not None
        assert self.tls_private_key is not None
        assert self.pinned_client_ca is not None
        return {
            "ssl_certfile": str(self.tls_certificate),
            "ssl_keyfile": str(self.tls_private_key),
            "ssl_ca_certs": str(self.pinned_client_ca),
            "ssl_cert_reqs": ssl.CERT_REQUIRED,
        }

    @property
    def pinned_client_ca_digest(self) -> str:
        if self.is_loopback:
            raise ValueError("loopback policy does not have a pinned client CA")
        return self._client_ca_digest


class _InflightGate:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._active = 0
        self._lock = asyncio.Lock()

    async def enter(self) -> bool:
        async with self._lock:
            if self._active >= self._limit:
                return False
            self._active += 1
            return True

    async def leave(self) -> None:
        async with self._lock:
            self._active -= 1


class AuthenticatedBoundedMiddleware:
    """Authenticate and reserve bounded capacity before receiving request-body bytes."""

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        credentials: ServiceCredentialRegistry,
        listener: ListenerSecurityPolicy,
        max_body_bytes: int,
        max_inflight: int,
        blocking_executor: BoundedBlockingExecutor | None = None,
        authentication_timeout_ms: int = 5_000,
    ) -> None:
        self.app = app
        self._credentials = credentials
        self._listener = listener
        self._max_body_bytes = max_body_bytes
        self._gate = _InflightGate(max_inflight)
        self._blocking = blocking_executor or BoundedBlockingExecutor(
            max_concurrency=min(max_inflight, MAX_V3_BLOCKING_OPERATIONS)
        )
        self._authentication_timeout_ms = authentication_timeout_ms

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        try:
            headers = _header_map(scope)
        except ValueError:
            await _send_error(
                send, 400, "ambiguous_request_headers", "Duplicate singleton headers are rejected."
            )
            return
        is_public = path in _PUBLIC_PATHS
        if not self._listener.is_loopback and _FORWARDED_HEADERS.intersection(headers):
            await _send_error(
                send, 400, "proxy_identity_rejected", "Proxy identity is not accepted."
            )
            return
        if not self._listener.is_loopback:
            raw_host = headers.get(b"host", b"")
            try:
                requested_host = raw_host.decode("ascii").rsplit(":", 1)[0].rstrip(".").casefold()
            except UnicodeError:
                requested_host = ""
            if requested_host != str(self._listener.server_hostname).rstrip(".").casefold():
                await _send_error(
                    send, 400, "host_identity_rejected", "Request hostname is not trusted."
                )
                return
        if not is_public:
            try:
                authorization = headers.get(b"authorization")
                principal = await self._blocking.run(
                    self._credentials.authenticate,
                    None if authorization is None else authorization.decode("ascii", "strict"),
                    timeout_ms=self._authentication_timeout_ms,
                    require_certificate=not self._listener.is_loopback,
                    certificate_principal=self._listener.certificate_principal(scope),
                )
            except BlockingOperationTimeout:
                await _send_error(
                    send,
                    503,
                    "authentication_timeout",
                    "Service credential verification did not complete before its deadline.",
                    extra_headers=[(b"retry-after", b"1")],
                )
                return
            except (CredentialError, UnicodeError):
                await _send_error(
                    send,
                    401,
                    "authentication_failed",
                    "A valid V3 service credential is required.",
                    extra_headers=[(b"www-authenticate", b"Bearer")],
                )
                return
            state = scope.setdefault("state", {})
            if not isinstance(state, MutableMapping):
                await _send_error(
                    send, 500, "transport_state_invalid", "Transport state is invalid."
                )
                return
            state["service_principal"] = principal
        method = str(scope.get("method", "")).upper()
        if method in {"POST", "PUT", "PATCH"}:
            content_type = headers.get(b"content-type", b"").split(b";", 1)[0].strip().lower()
            if content_type != b"application/json":
                await _send_error(
                    send, 415, "media_type_rejected", "V3 request bodies must use application/json."
                )
                return
            if headers.get(b"content-encoding", b"identity").lower() != b"identity":
                await _send_error(
                    send, 415, "content_encoding_rejected", "Encoded request bodies are rejected."
                )
                return
        if not await self._gate.enter():
            await _send_error(
                send,
                503,
                "request_capacity_exhausted",
                "V3 request capacity is temporarily exhausted.",
                extra_headers=[(b"retry-after", b"1")],
            )
            return
        try:
            content_length = headers.get(b"content-length")
            if content_length is not None:
                try:
                    declared = int(content_length)
                except ValueError:
                    await _send_error(
                        send, 400, "content_length_invalid", "Content-Length is invalid."
                    )
                    return
                if declared < 0 or declared > self._max_body_bytes:
                    await _send_error(
                        send, 413, "request_body_too_large", "Request body is too large."
                    )
                    return
            body = bytearray()
            more = True
            while more:
                message = await receive()
                if message.get("type") == "http.disconnect":
                    return
                if message.get("type") != "http.request":
                    await _send_error(
                        send, 400, "request_stream_invalid", "Request stream is invalid."
                    )
                    return
                body.extend(message.get("body", b""))
                if len(body) > self._max_body_bytes:
                    await _send_error(
                        send, 413, "request_body_too_large", "Request body is too large."
                    )
                    return
                more = bool(message.get("more_body", False))
            replayed = False

            async def replay_receive():
                nonlocal replayed
                if replayed:
                    return {"type": "http.request", "body": b"", "more_body": False}
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}

            async def protected_send(message):
                if message.get("type") == "http.response.start" and not is_public:
                    response_headers = list(message.get("headers", ()))
                    names = {bytes(name).lower() for name, _value in response_headers}
                    if b"cache-control" not in names:
                        response_headers.append((b"cache-control", b"no-store"))
                    if b"pragma" not in names:
                        response_headers.append((b"pragma", b"no-cache"))
                    if b"x-content-type-options" not in names:
                        response_headers.append((b"x-content-type-options", b"nosniff"))
                    message = {**message, "headers": response_headers}
                await send(message)

            await self.app(scope, replay_receive, protected_send)
        finally:
            await self._gate.leave()


def create_v3_app(
    *,
    gateway: V3ApplicationPort,
    credentials: ServiceCredentialRegistry,
    listener: ListenerSecurityPolicy | None = None,
    max_body_bytes: int = MAX_V3_REQUEST_BODY_BYTES,
    max_inflight: int = MAX_V3_INFLIGHT_REQUESTS,
    blocking_max_concurrency: int = 4,
    authentication_timeout_ms: int = 5_000,
    credential_operation_timeout_ms: int = 5_000,
) -> FastAPI:
    """Create one independent app; no store, executor, or semaphore is module-global."""

    if not isinstance(credentials, ServiceCredentialRegistry):
        raise TypeError("credentials must be a ServiceCredentialRegistry")
    if (
        isinstance(max_body_bytes, bool)
        or not isinstance(max_body_bytes, int)
        or not 1_024 <= max_body_bytes <= MAX_V3_REQUEST_BODY_BYTES
    ):
        raise ValueError("max_body_bytes must be between 1024 and 1048576")
    if (
        isinstance(max_inflight, bool)
        or not isinstance(max_inflight, int)
        or not 1 <= max_inflight <= MAX_V3_INFLIGHT_REQUESTS
    ):
        raise ValueError("max_inflight must be between 1 and 64")
    if (
        isinstance(blocking_max_concurrency, bool)
        or not isinstance(blocking_max_concurrency, int)
        or not 1 <= blocking_max_concurrency <= MAX_V3_BLOCKING_OPERATIONS
    ):
        raise ValueError("blocking_max_concurrency must be between 1 and 16")
    for name, value in (
        ("authentication_timeout_ms", authentication_timeout_ms),
        ("credential_operation_timeout_ms", credential_operation_timeout_ms),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 60_000:
            raise ValueError(f"{name} must be between 1 and 60000")
    policy = listener or ListenerSecurityPolicy()
    blocking = BoundedBlockingExecutor(max_concurrency=blocking_max_concurrency)
    startup_verify = getattr(gateway, "verify_startup", None)
    if startup_verify is not None:
        if not callable(startup_verify):
            raise TypeError("gateway startup verification must be callable")
        startup_verify()
    app = FastAPI(
        title="STRATHMARK V3 Service",
        version="3.0.0",
        openapi_version="3.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
    )
    app.include_router(
        create_router(
            gateway=gateway,
            credentials=credentials,
            blocking_executor=blocking,
            credential_operation_timeout_ms=credential_operation_timeout_ms,
        )
    )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(_request: Request, _exc: RequestValidationError):
        return _error_response(
            422,
            "request_validation_failed",
            "Request does not match the frozen V3 schema.",
        )

    @app.exception_handler(TransportError)
    async def transport_error_handler(_request: Request, exc: TransportError):
        return _error_response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(CredentialError)
    async def credential_error_handler(_request: Request, _exc: CredentialError):
        return _error_response(
            409,
            "credential_lifecycle_conflict",
            "Credential lifecycle command conflicts with current authority.",
        )

    @app.exception_handler(ResponseValidationError)
    async def response_validation_handler(_request: Request, _exc: ResponseValidationError):
        return _error_response(
            500,
            "application_contract_violation",
            "Application port returned an invalid V3 response.",
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(_request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            return _error_response(404, "route_not_found", "V3 route was not found.")
        if exc.status_code == 405:
            return _error_response(405, "method_not_allowed", "HTTP method is not allowed.")
        return _error_response(
            exc.status_code, "http_request_rejected", "HTTP request was rejected."
        )

    @app.exception_handler(Exception)
    async def closed_internal_error_handler(_request: Request, _exc: Exception):
        return _error_response(
            500,
            "internal_service_error",
            "V3 operation failed without a trusted result.",
        )

    app.add_middleware(
        AuthenticatedBoundedMiddleware,
        credentials=credentials,
        listener=policy,
        max_body_bytes=max_body_bytes,
        max_inflight=max_inflight,
        blocking_executor=blocking,
        authentication_timeout_ms=authentication_timeout_ms,
    )
    app.state.listener_security_policy = policy
    app.state.blocking_executor = blocking
    # Interactive consumers and generated clients see the reviewed installed bytes,
    # never a silently drifted framework approximation.
    from strathmark.v3.consumer_contract import load_v3_consumer_contract

    app.openapi = load_v3_consumer_contract  # type: ignore[method-assign]
    return app


def _header_map(scope: Mapping[str, Any]) -> dict[bytes, bytes]:
    result: dict[bytes, bytes] = {}
    for raw_name, raw_value in scope.get("headers", ()):
        name = bytes(raw_name).lower()
        if name in result and name in _SINGLETON_HEADERS:
            raise ValueError("duplicate singleton header")
        if name not in result:
            result[name] = bytes(raw_value)
    return result


def _validate_mtls_material(
    certificate_path: Path,
    private_key_path: Path,
    client_ca_path: Path,
    server_hostname: str,
) -> str:
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization

        certificate = x509.load_pem_x509_certificate(certificate_path.read_bytes())
        private_key = serialization.load_pem_private_key(
            private_key_path.read_bytes(), password=None
        )
        client_ca = x509.load_pem_x509_certificate(client_ca_path.read_bytes())
        now = datetime.now(timezone.utc)
        for item in (certificate, client_ca):
            not_before = item.not_valid_before_utc
            not_after = item.not_valid_after_utc
            if not_before > now or not_after < now:
                raise ValueError("mutual TLS certificate is not currently valid")
        certificate_key = certificate.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        private_public_key = private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        if not hmac.compare_digest(certificate_key, private_public_key):
            raise ValueError("server TLS certificate and private key do not match")
        constraints = client_ca.extensions.get_extension_for_class(x509.BasicConstraints).value
        if constraints.ca is not True:
            raise ValueError("pinned client CA certificate is not a CA")
        san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        try:
            expected_ip = ipaddress.ip_address(server_hostname)
        except ValueError:
            names = san.get_values_for_type(x509.DNSName)
            if not any(_dns_hostname_matches(name, server_hostname) for name in names):
                raise ValueError("server TLS certificate hostname does not match")
        else:
            if expected_ip not in san.get_values_for_type(x509.IPAddress):
                raise ValueError("server TLS certificate hostname does not match")
        return client_ca.fingerprint(hashes.SHA256()).hex()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("non-loopback mutual TLS material is malformed") from exc


def _dns_hostname_matches(pattern: str, hostname: str) -> bool:
    pattern = pattern.rstrip(".").casefold()
    hostname = hostname.rstrip(".").casefold()
    if "*" not in pattern:
        return pattern == hostname
    if not pattern.startswith("*.") or pattern.count("*") != 1:
        return False
    suffix = pattern[2:]
    return hostname.endswith("." + suffix) and hostname.count(".") == suffix.count(".") + 1


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    value = ErrorResponse(code=code, message=message).model_dump(mode="json")
    return JSONResponse(value, status_code=status_code)


async def _send_error(
    send,
    status_code: int,
    code: str,
    message: str,
    *,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    body = json.dumps(
        ErrorResponse(code=code, message=message).model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    headers.extend(extra_headers or ())
    await send({"type": "http.response.start", "status": status_code, "headers": headers})
    await send({"type": "http.response.body", "body": body})


__all__ = [
    "AuthenticatedBoundedMiddleware",
    "ListenerSecurityPolicy",
    "MAX_V3_INFLIGHT_REQUESTS",
    "MAX_V3_REQUEST_BODY_BYTES",
    "TransportError",
    "create_v3_app",
]
