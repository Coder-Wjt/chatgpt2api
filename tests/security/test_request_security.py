from fastapi.testclient import TestClient
from api.app import create_app


def test_oversized_body_is_rejected_before_authentication():
    client = TestClient(create_app())
    response = client.post(
        "/auth/login", headers={"content-length": str(151 * 1024 * 1024)}, content=b""
    )
    assert response.status_code == 413


import asyncio
import pytest
from api.request_limits import RequestLimitsMiddleware


@pytest.mark.parametrize("headers", [[], [(b"content-length", b"1")]])
def test_chunked_or_forged_length_cannot_bypass_limit(headers):
    called = []

    async def app(scope, receive, send):
        called.append(True)

    messages = iter(
        [
            {"type": "http.request", "body": b"123", "more_body": True},
            {"type": "http.request", "body": b"456", "more_body": False},
        ]
    )
    responses = []

    async def receive():
        return next(messages)

    async def send(message):
        responses.append(message)

    asyncio.run(
        RequestLimitsMiddleware(app, max_bytes=5)(
            {"type": "http", "method": "POST", "headers": headers}, receive, send
        )
    )
    assert not called
    assert responses[0]["status"] == 413


def test_body_replay_then_real_disconnect():
    received = []

    async def app(scope, receive, send):
        received.append(await receive())
        received.append(await receive())

    messages = iter(
        [
            {"type": "http.request", "body": b"ok", "more_body": False},
            {"type": "http.disconnect"},
        ]
    )

    async def receive():
        return next(messages)

    async def send(message):
        pass

    asyncio.run(
        RequestLimitsMiddleware(app)(
            {"type": "http", "method": "POST", "headers": []}, receive, send
        )
    )
    assert received[-1]["type"] == "http.disconnect"


def test_get_remains_available_when_write_capacity_is_full():
    called = []

    async def app(scope, receive, send):
        called.append(True)

    middleware = RequestLimitsMiddleware(app)
    middleware._body_requests = 8

    async def receive():
        raise AssertionError("GET body should not be consumed")

    async def send(message):
        pass

    asyncio.run(
        middleware({"type": "http", "method": "GET", "headers": []}, receive, send)
    )
    assert called
