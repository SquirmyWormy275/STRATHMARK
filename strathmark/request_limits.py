"""Small pre-parse byte, receive-time and admission limits for public V2 HTTP."""

import asyncio

from starlette.responses import JSONResponse


class BoundedRequestMiddleware:
    def __init__(self, app, *, max_bytes=1_048_576, max_inflight=16):
        self.app = app
        self.max_bytes = max_bytes
        self.max_inflight = max_inflight
        self.active = 0

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or str(scope.get("path", "")).startswith("/v1/shadow/"):
            # The authenticated shadow gate owns its bounded body and reserved work pools.
            await self.app(scope, receive, send)
            return

        async def reject(status, detail):
            await JSONResponse({"detail": detail}, status_code=status)(scope, receive, send)

        if self.active >= self.max_inflight:
            await reject(503, "Request capacity is exhausted.")
            return
        headers = {}
        for key, value in scope.get("headers", []):
            key = key.lower()
            if key in headers and key in (b"content-length", b"content-encoding"):
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
        self.active += 1
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
            self.active -= 1
