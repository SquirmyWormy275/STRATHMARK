"""Resource and authenticated-evidence regressions from the cloud scan."""

import asyncio
import io
import threading
import zipfile
from decimal import Decimal

import pytest

from strathmark.v3.api.router import BoundedBlockingExecutor, _invoke
from strathmark.v3.contracts.canonical import canonical_decimal_string
from strathmark.v3.contracts.errors import CanonicalBoundsError, CanonicalNumberError


@pytest.mark.parametrize("value", ["1e100000000", "1e-100000000", Decimal("1e100000000")])
def test_short_exponents_fail_before_fixed_point_expansion(value):
    with pytest.raises((CanonicalBoundsError, CanonicalNumberError)):
        canonical_decimal_string(value)


@pytest.mark.parametrize("value", ["1.2300", "-0.0010", "12e-3", "1e3", "-0"])
def test_bounded_decimal_conversion_preserves_normal_values(value):
    assert Decimal(canonical_decimal_string(value)) == Decimal(value)


def test_gateway_deadlines_retain_actual_blocking_capacity():
    from strathmark.v3.api.app import TransportError

    release = threading.Event()
    calls = []

    def blocked():
        calls.append(True)
        release.wait(5)

    async def scenario():
        executor = BoundedBlockingExecutor(max_concurrency=1)
        try:
            for _ in range(3):
                with pytest.raises(TransportError):
                    await _invoke(blocked, deadline_ms=10, blocking=executor)
                assert executor.active_count == 1
            assert len(calls) == 1
        finally:
            release.set()
            for _ in range(100):
                if executor.active_count == 0:
                    break
                await asyncio.sleep(0.005)
        assert executor.active_count == 0

    asyncio.run(scenario())


def test_support_zip_compression_is_rejected_before_member_read(monkeypatch):
    from strathmark.v3.application.operations import verify_support_bundle

    source = io.BytesIO()
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("support-manifest.json", b"x" * 100_000)

    def forbidden(*args, **kwargs):
        pytest.fail("member was decompressed before preflight")

    monkeypatch.setattr(zipfile.ZipFile, "read", forbidden)
    with pytest.raises(ValueError, match="budget"):
        verify_support_bundle(source.getvalue(), trust_store=None)


def test_oversized_import_fails_before_source_scan_or_destination_creation(tmp_path):
    from strathmark.v3.infrastructure.v2_import import V2ImportError, import_v2_snapshot

    source = tmp_path / "huge.sqlite3"
    with source.open("wb") as stream:
        stream.truncate(67_108_865)
    destination = tmp_path / "never-created.sqlite3"
    with pytest.raises(V2ImportError, match="budget"):
        import_v2_snapshot(source, destination, cutoff="2026-10-19T00:00:00.000Z")
    assert not destination.exists()


def test_public_health_never_reads_or_reserves_authority_capacity():
    from strathmark.v3.api.app import AuthenticatedBoundedMiddleware, ListenerSecurityPolicy

    calls = []
    output = []

    async def downstream(scope, receive, send):
        calls.append(scope["path"])

    async def receive():
        pytest.fail("public health read an attacker-controlled body")

    async def send(message):
        output.append(message)

    middleware = AuthenticatedBoundedMiddleware(
        downstream,
        credentials=None,
        listener=ListenerSecurityPolicy(),
        max_body_bytes=1024,
        max_inflight=1,
    )

    async def scenario():
        assert await middleware._gate.enter()
        await middleware(
            {"type": "http", "method": "GET", "path": "/v3/health", "headers": []}, receive, send
        )
        await middleware(
            {
                "type": "http",
                "method": "GET",
                "path": "/v3/health",
                "headers": [(b"content-length", b"1")],
            },
            receive,
            send,
        )
        await middleware._gate.leave()

    asyncio.run(scenario())
    assert calls == ["/v3/health"]
    assert output[0]["status"] == 400


def test_llm_probability_exponent_is_rejected_before_numeric_conversion(monkeypatch):
    from strathmark.v3.assessors.output_validation import LLMOutputError, _distribution
    from strathmark.v3.contracts.forecasts import QuantilePoint

    def forbidden(*args):
        pytest.fail("untrusted probability reached numeric conversion")

    monkeypatch.setattr(QuantilePoint, "from_dict", forbidden)
    with pytest.raises(LLMOutputError, match="invalid_quantiles"):
        _distribution([{"probability": "1e-100000000", "time_ms": 1000}] * 7)
