"""Bound request bodies before JSON/multipart parsing or endpoint execution."""

from __future__ import annotations

import asyncio
import tempfile

from fastapi import HTTPException
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from fastapi.concurrency import run_in_threadpool

from api.support import require_identity
from services.protocol.error_response import (
    anthropic_error_response,
    openai_error_response,
)

MAX_REQUEST_BYTES = 150 * 1024 * 1024
MAX_BODY_REQUESTS = 8
SMALL_BODY_BYTES = 1024 * 1024
ACTIVE_REQUEST_LIMITS = {"chat": 250, "image": 8, "write": 8}
CHAT_PATHS = frozenset({
    "/v1/chat/completions", "/v1/responses", "/v1/messages", "/v1/search",
})


class RequestLimitsMiddleware:
    def __init__(self, app: ASGIApp, *, max_bytes: int = MAX_REQUEST_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self._body_requests = 0
        self._active_requests = dict.fromkeys(ACTIVE_REQUEST_LIMITS, 0)

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
        # No write endpoint accepts anonymous bodies. Authenticate before reserving
        # capacity or touching receive(); endpoint role/ownership checks still apply.
        headers = Headers(scope=scope)
        path = scope.get("path", "")
        authorization = headers.get("authorization")
        if path == "/v1/messages" and not authorization:
            api_key = headers.get("x-api-key")
            authorization = f"Bearer {api_key}" if api_key else None
        try:
            if authorization:
                await run_in_threadpool(require_identity, authorization)
            else:
                require_identity(None)
        except HTTPException as exc:
            if path == "/v1/messages":
                response = anthropic_error_response(exc.detail, exc.status_code)
            elif path == "/v1" or path.startswith("/v1/"):
                response = openai_error_response(exc.detail, exc.status_code)
            else:
                response = JSONResponse(
                    {"detail": exc.detail}, status_code=exc.status_code
                )
            await response(scope, receive, send)
            return
        pool = "write"
        if scope["method"] == "POST":
            if path in CHAT_PATHS:
                pool = "chat"
            elif path in {"/v1/images/generations", "/v1/images/edits"} or path.startswith("/api/image-tasks/"):
                pool = "image"
        if self._active_requests[pool] >= ACTIVE_REQUEST_LIMITS[pool]:
            await reject(429, "Too many active requests; retry later")
            return
        if self._body_requests >= MAX_BODY_REQUESTS:
            await reject(429, "Too many active uploads; retry later")
            return
        self._active_requests[pool] += 1
        self._body_requests += 1
        uploading = True
        spool = None
        try:
            spool = tempfile.SpooledTemporaryFile(max_size=SMALL_BODY_BYTES)
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
            # Small uploads release capacity before execution. Large bodies keep
            # it through response completion to bound concurrent parsing/memory.
            if total <= SMALL_BODY_BYTES:
                self._body_requests -= 1
                uploading = False
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
            try:
                if spool is not None:
                    spool.close()
            finally:
                if uploading:
                    self._body_requests -= 1
                self._active_requests[pool] -= 1
