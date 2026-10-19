"""Unauthenticated slow input cannot consume reserved operational capacity."""

import asyncio
import threading

from strathmark.request_limits import BoundedRequestMiddleware
from strathmark.v3.api.app import AuthenticatedBoundedMiddleware, ListenerSecurityPolicy
from strathmark.v3.api.router import BoundedBlockingExecutor


def test_v2_public_slow_bodies_leave_health_and_verified_token_capacity():
    async def scenario():
        release = asyncio.Event()
        entered = []
        reached = []

        async def downstream(scope, receive, send):
            reached.append(scope["path"])
            await receive()

        middleware = BoundedRequestMiddleware(
            downstream, trusted_request=lambda value: value == "Bearer valid"
        )

        async def hold(source):
            async def receive():
                entered.append(source)
                await release.wait()
                return {"type": "http.request", "body": b"{}"}

            await middleware(
                {"type": "http", "path": "/calculate", "client": (source, 1), "headers": []},
                receive,
                lambda value: asyncio.sleep(0),
            )

        tasks = [asyncio.create_task(hold(f"source-{i}")) for i in range(16)]
        try:
            while len(entered) < 16:
                await asyncio.sleep(0)
            assert middleware.active == 16
            output = []

            async def send(message):
                output.append(message)

            async def never_read():
                raise AssertionError("overflow or health read peer body")

            await middleware({"type": "http", "path": "/health", "headers": []}, never_read, send)
            await middleware(
                {
                    "type": "http",
                    "path": "/ledger/calculate",
                    "headers": [(b"authorization", b"Bearer wrong")],
                },
                never_read,
                send,
            )
            assert output[0]["status"] == 503

            async def body():
                return {"type": "http.request", "body": b"{}"}

            await middleware(
                {
                    "type": "http",
                    "path": "/ledger/calculate",
                    "headers": [(b"authorization", b"Bearer valid")],
                },
                body,
                send,
            )
            assert reached == ["/health", "/ledger/calculate"]
        finally:
            release.set()
            await asyncio.gather(*tasks)
        assert middleware.active == middleware.trusted_active == 0
        assert middleware._sources == {}

    asyncio.run(scenario())


def test_v3_authentication_overflow_has_no_wait_queue_and_health_still_runs():
    released = threading.Event()
    started = threading.Event()

    class Credentials:
        def authenticate(self, *args, **kwargs):
            started.set()
            released.wait(5)
            raise ValueError("synthetic completion")

    async def scenario():
        executor = BoundedBlockingExecutor(max_concurrency=1)
        reached = []

        async def downstream(scope, receive, send):
            reached.append(scope["path"])

        middleware = AuthenticatedBoundedMiddleware(
            downstream,
            credentials=Credentials(),
            listener=ListenerSecurityPolicy(),
            max_body_bytes=100,
            max_inflight=2,
            blocking_executor=executor,
        )

        async def call(path):
            output = []

            async def receive():
                raise AssertionError("unverified request read its body")

            async def send(message):
                output.append(message)

            await middleware(
                {"type": "http", "method": "GET", "path": path, "headers": []}, receive, send
            )
            return output

        first = asyncio.create_task(call("/v3/status"))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            flooded = [asyncio.create_task(call("/v3/status")) for _ in range(100)]
            finished, pending = await asyncio.wait(flooded, timeout=0.5)
            assert not pending
            assert all(task.result()[0]["status"] == 503 for task in finished)
            assert executor.active_count == 1
            await call("/v3/health")
            assert reached == ["/v3/health"]
        finally:
            released.set()
            await asyncio.gather(first, return_exceptions=True)
        assert executor.active_count == 0

    asyncio.run(scenario())


def test_one_public_source_cannot_reserve_every_body_slot():
    async def scenario():
        release = asyncio.Event()
        entered = []
        reached = []

        async def downstream(scope, receive, send):
            reached.append(scope["client"][0])

        middleware = BoundedRequestMiddleware(downstream)

        async def slow():
            entered.append(True)
            await release.wait()
            return {"type": "http.request", "body": b"{}"}

        async def send(_message):
            pass

        scope = {"type": "http", "path": "/calculate", "client": ("peer", 1), "headers": []}
        tasks = [asyncio.create_task(middleware(dict(scope), slow, send)) for _ in range(4)]
        try:
            while len(entered) < 4:
                await asyncio.sleep(0)
            output = []

            async def capture(message):
                output.append(message)

            async def body():
                return {"type": "http.request", "body": b"{}"}

            await middleware(dict(scope), body, capture)
            assert output[0]["status"] == 503
            await middleware({**scope, "client": ("other-peer", 1)}, body, capture)
            assert reached == ["other-peer"]
        finally:
            release.set()
            await asyncio.gather(*tasks)
        assert middleware.active == 0

    asyncio.run(scenario())
