"""Small pre-parse byte, receive-time and admission limits for public V2 HTTP."""

import asyncio

from starlette.responses import JSONResponse

from strathmark.admission import WorkAdmission


class BoundedRequestMiddleware:
    def __init__(self, app, *, max_bytes=1_048_576, max_inflight=16, trusted_request=None):
        self.app = app
        self.max_bytes = max_bytes
        self.max_inflight = max_inflight
        self.active = 0
        self.trusted_active = 0
        self._sources = {}
        self._trusted_request = trusted_request
        self.health_admission = WorkAdmission(2, 1)
        self._health_tasks = set()

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or str(scope.get("path", "")).startswith("/v1/shadow/"):
            # The authenticated shadow gate owns its bounded body and reserved work pools.
            await self.app(scope, receive, send)
            return

        async def reject(status, detail):
            await JSONResponse({"detail": detail}, status_code=status)(scope, receive, send)

        headers = {}
        for key, value in scope.get("headers", []):
            key = key.lower()
            if key in headers and key in (b"content-length", b"content-encoding", b"authorization"):
                await reject(400, "Duplicate request length or encoding.")
                return
            headers[key] = value
        try:
            length = int(headers[b"content-length"]) if b"content-length" in headers else None
        except ValueError:
            await reject(400, "Content-Length is invalid.")
            return
        if length is not None and (length < 0 or length > self.max_bytes):
            await reject(413, "Request body is too large.")
            return
        if headers.get(b"content-encoding", b"identity") != b"identity":
            await reject(415, "Encoded request bodies are rejected.")
            return
        if scope.get("path") == "/health" and scope.get("method", "GET") in {"GET", "HEAD"}:
            if length not in (None, 0) or b"transfer-encoding" in headers:
                await reject(400, "Health accepts no request body.")
                return

            async def empty_receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            source = str((scope.get("client") or ("unknown",))[0])
            if not self.health_admission.acquire(source):
                await reject(503, "Health capacity is busy.")
                return

            async def complete_health():
                try:
                    await self.app(scope, empty_receive, send)
                finally:
                    self.health_admission.release(source)

            # A disconnected caller must not free capacity while its synchronous
            # dependency or handler is still running in the framework thread pool.
            task = asyncio.create_task(complete_health())
            self._health_tasks.add(task)
            task.add_done_callback(self._health_completed)
            await asyncio.shield(task)
            return
        authorization = headers.get(b"authorization", b"")
        trusted = False
        if self._trusted_request is not None and len(authorization) <= 8192:
            try:
                trusted = self._trusted_request(authorization.decode("ascii")) is True
            except UnicodeError:
                pass
        source = str((scope.get("client") or ("unknown",))[0])
        if trusted:
            if self.trusted_active >= 4:
                await reject(503, "Authenticated request capacity is exhausted.")
                return
            self.trusted_active += 1
        else:
            if self.active >= self.max_inflight or self._sources.get(source, 0) >= 4:
                await reject(503, "Public request capacity is exhausted.")
                return
            self.active += 1
            self._sources[source] = self._sources.get(source, 0) + 1
        try:
            body = bytearray()
            end = asyncio.get_running_loop().time() + 10
            while True:
                try:
                    message = await asyncio.wait_for(
                        receive(), min(2, end - asyncio.get_running_loop().time())
                    )
                except TimeoutError:
                    await reject(408, "Request body deadline expired.")
                    return
                if message.get("type") == "http.disconnect":
                    return
                if message.get("type") != "http.request":
                    await reject(400, "Invalid request stream.")
                    return
                chunk = message.get("body", b"")
                if len(body) + len(chunk) > self.max_bytes:
                    await reject(413, "Request body is too large.")
                    return
                body.extend(chunk)
                if not message.get("more_body", False):
                    break
            if length is not None and length != len(body):
                await reject(400, "Content-Length differs from the body.")
                return

            async def replay():
                return {"type": "http.request", "body": bytes(body), "more_body": False}

            await self.app(scope, replay, send)
        finally:
            if trusted:
                self.trusted_active -= 1
            else:
                self.active -= 1
                count = self._sources[source] - 1
                if count:
                    self._sources[source] = count
                else:
                    del self._sources[source]

    def _health_completed(self, task):
        self._health_tasks.discard(task)
        if not task.cancelled():
            # Retrieve detached errors when the client has already disconnected.
            task.exception()
