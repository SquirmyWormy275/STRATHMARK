"""Independent capacity remains finite before optional HTTP work begins."""

import asyncio
import json

from strathmark.api import TrustedShadowRequestGate
from strathmark.request_limits import BoundedRequestMiddleware


def test_health_has_its_own_no_wait_source_and_global_work_limits():
    async def scenario():
        release = asyncio.Event()
        entered = []
        replies = []

        async def downstream(scope, receive, send):
            entered.append(scope["client"][0])
            await release.wait()

        middleware = BoundedRequestMiddleware(downstream)

        async def never_read():
            raise AssertionError("health must not wait on incoming bytes")

        async def send(message):
            replies.append(message)

        async def call(source):
            await middleware(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/health",
                    "headers": [],
                    "client": (source, 1),
                },
                never_read,
                send,
            )

        first = asyncio.create_task(call("one"))
        await asyncio.sleep(0)
        await asyncio.gather(*(call("one") for _ in range(32)))
        second = asyncio.create_task(call("two"))
        while len(entered) < 2:
            await asyncio.sleep(0)
        await call("three")
        assert entered == ["one", "two"]
        assert [m["status"] for m in replies if m["type"] == "http.response.start"] == [503] * 33
        assert middleware.active == middleware.trusted_active == 0
        release.set()
        await asyncio.gather(first, second)
        assert middleware.health_admission.active == 0
        assert middleware.health_admission._callers == {}

    asyncio.run(scenario())


def test_shadow_bodies_are_admitted_before_reading_and_recovery_is_reserved(monkeypatch):
    credentials = {f"test:consumer:{i}": f"synthetic-bearer-token-{i}" for i in range(5)}
    monkeypatch.setenv("STRATHMARK_SHADOW_SERVICE_CREDENTIALS", json.dumps(credentials))
    monkeypatch.setenv(
        "STRATHMARK_SHADOW_ATTESTATION_KEYS",
        json.dumps({key: f"separate-actor-signing-key-{i}" for i, key in enumerate(credentials)}),
    )

    async def scenario():
        release = asyncio.Event()
        entered = []
        replies = []

        async def downstream(scope, receive, send):
            raise AssertionError("incomplete body must not reach the handler")

        gate = TrustedShadowRequestGate(downstream)
        middleware = BoundedRequestMiddleware(gate)

        async def call(caller, path="/v1/shadow/calculate"):
            async def receive():
                entered.append((caller, path))
                await release.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                replies.append(message)

            await middleware(
                {
                    "type": "http",
                    "method": "POST",
                    "path": path,
                    "headers": [
                        (b"authorization", f"Bearer synthetic-bearer-token-{caller}".encode()),
                        (b"content-length", b"262144"),
                    ],
                },
                receive,
                send,
            )

        tasks = [asyncio.create_task(call(i)) for i in range(4) for _ in range(2)]
        try:
            while len(entered) < 8:
                await asyncio.sleep(0)
            await asyncio.gather(*(call(0) for _ in range(32)), call(4))
            assert len(entered) == 8
            assert [m["status"] for m in replies if m["type"] == "http.response.start"] == [
                429
            ] * 33
            recovery = asyncio.create_task(call(0, "/v1/shadow/receipts/lookup"))
            tasks.append(recovery)
            while len(entered) < 9:
                await asyncio.sleep(0)
            assert gate.body_admission.active == 8
            assert gate.recovery_admission.active == 1
            tasks[0].cancel()
            await asyncio.gather(tasks[0], return_exceptions=True)
            assert gate.body_admission.active == 7
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
        assert gate.body_admission.active == gate.recovery_admission.active == 0
        assert not gate.body_admission._callers and not gate.recovery_admission._callers

    asyncio.run(scenario())


def test_login_attempt_memory_and_account_source_budgets_are_finite():
    from strathmark.admission import LoginAttempts

    attempts = LoginAttempts(total=10, per_account=2, per_source=3)
    assert attempts.admit("unknown", "peer")
    assert attempts.admit("unknown", "other-peer")
    assert not attempts.admit("unknown", "third-peer")
    assert attempts.admit("different", "peer")
    assert attempts.admit("another", "peer")
    assert not attempts.admit("new", "peer")
    for i in range(100):
        attempts.admit(f"rotated-{i}", f"address-{i}")
    assert attempts._attempts == 10
    assert len(attempts._accounts) <= 10
    assert len(attempts._sources) <= 10
    attempts._started -= attempts.window
    assert attempts.admit("unknown", "peer")
    assert attempts._accounts == {"unknown": 1}
    assert attempts._sources == {"peer": 1}


def test_cancelled_actual_health_request_retains_capacity_until_worker_exit(monkeypatch):
    import threading

    import httpx

    from strathmark import api

    release = threading.Event()
    started = threading.Event()
    finished = threading.Event()

    class Provider:
        def snapshot(self, cutoff):
            started.set()
            try:
                assert release.wait(3)
                raise RuntimeError("synthetic health work completed")
            finally:
                finished.set()

    async def scenario():
        api.app.dependency_overrides[api.get_store] = lambda: None
        api.app.dependency_overrides[api.get_ledger] = lambda: None
        api.app.dependency_overrides[api.get_prediction_provider] = lambda: Provider()
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=api.app), base_url="http://test"
            ) as client:
                first = asyncio.create_task(client.get("/health"))
                while not started.is_set():
                    await asyncio.sleep(0.001)
                middleware = api.app.middleware_stack
                while not isinstance(middleware, BoundedRequestMiddleware):
                    middleware = middleware.app
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)
                assert not finished.is_set()
                assert middleware.health_admission.active == 1
                assert (await client.get("/health")).status_code == 503
                release.set()
                for _ in range(1000):
                    # Capacity is released inside the coroutine; its registered
                    # completion callback clears the retained task next.
                    if middleware.health_admission.active == 0 and not middleware._health_tasks:
                        break
                    await asyncio.sleep(0.001)
                assert finished.is_set()
                assert middleware.health_admission.active == 0
                assert not middleware._health_tasks
        finally:
            release.set()
            api.app.dependency_overrides.clear()

    asyncio.run(scenario())
