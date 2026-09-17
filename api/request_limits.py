"""Bound request bodies before JSON/multipart parsing or endpoint execution."""

from __future__ import annotations

import asyncio
import tempfile

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from fastapi.concurrency import run_in_threadpool

MAX_REQUEST_BYTES = 150 * 1024 * 1024
MAX_BODY_REQUESTS = 8


class RequestLimitsMiddleware:
    def __init__(self, app: ASGIApp, *, max_bytes: int = MAX_REQUEST_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self._body_requests = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in {
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        }:
            await self.app(scope, receive, send)
            return

        async def reject(status: int, message: str) -> None:
            await JSONResponse(
                {"error": {"message": message, "type": "invalid_request_error"}},
                status_code=status,
            )(scope, receive, send)

        for name, value in scope.get("headers", []):
            if name.lower() == b"content-length":
                try:
                    size = int(value)
                    if size < 0:
                        raise ValueError
                except ValueError:
                    await reject(400, "Invalid Content-Length")
                    return
                if size > self.max_bytes:
                    await reject(413, "Request body exceeds 150 MiB limit")
                    return
        if self._body_requests >= MAX_BODY_REQUESTS:
            await reject(429, "Too many active requests; retry later")
            return
        self._body_requests += 1
        spool = tempfile.SpooledTemporaryFile(max_size=1024 * 1024)
        try:
            total = 0
            try:
                async with asyncio.timeout(60):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        body = message.get("body", b"")
                        total += len(body)
                        if total > self.max_bytes:
                            await reject(413, "Request body exceeds 150 MiB limit")
                            return
                        await run_in_threadpool(spool.write, body)
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                await reject(408, "Request body upload timed out")
                return
            spool.seek(0)
            remaining = total
            delivered = False

            async def replay():
                nonlocal remaining, delivered
                if delivered:
                    return await receive()
                if remaining == 0:
                    delivered = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                chunk = await run_in_threadpool(spool.read, min(64 * 1024, remaining))
                remaining -= len(chunk)
                delivered = remaining == 0
                return {
                    "type": "http.request",
                    "body": chunk,
                    "more_body": remaining > 0,
                }

            await self.app(scope, replay, send)
        finally:
            spool.close()
            self._body_requests -= 1
