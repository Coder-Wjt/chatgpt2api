import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from api.request_limits import RequestLimitsMiddleware
from api.app import create_app
from services.config import config


def test_oversized_body_is_rejected_before_authentication():
    client = TestClient(create_app())
    response = client.post(
        "/auth/login", headers={"content-length": str(151 * 1024 * 1024)}, content=b""
    )
    assert response.status_code == 413


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
            {
                "type": "http",
                "method": "POST",
                "headers": headers
                + [(b"authorization", f"Bearer {config.auth_key}".encode())],
            },
            receive,
            send,
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
            {
                "type": "http",
                "method": "POST",
                "headers": [(b"authorization", f"Bearer {config.auth_key}".encode())],
            },
            receive,
            send,
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


@pytest.mark.parametrize(
    "path",
    [
        "/auth/login",
        "/v1/chat/completions",
        "/v1/messages",
        "/api/settings",
        "/unknown",
    ],
)
@pytest.mark.parametrize("authorization", [None, "Bearer invalid-key"])
def test_unauthorized_writes_never_read_body_or_take_capacity(path, authorization):
    async def scenario():
        app = create_app()
        middleware = app.build_middleware_stack()
        app.middleware_stack = middleware
        headers = (
            []
            if authorization is None
            else [(b"authorization", authorization.encode())]
        )
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": headers,
            "server": ("test", 80),
            "client": ("127.0.0.1", 1234),
        }

        async def one_request():
            responses = []

            async def receive():
                raise AssertionError("unauthorized request body was consumed")

            async def send(message):
                responses.append(message)

            await middleware(dict(scope), receive, send)
            assert responses[0]["status"] == 401

        await asyncio.gather(*(one_request() for _ in range(8)))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/auth/login", headers={"Authorization": f"Bearer {config.auth_key}"}
            )
            assert response.status_code == 200

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_anonymous_rejected_even_when_capacity_full(method):
    async def app(scope, receive, send):
        raise AssertionError("unauthorized request reached endpoint")

    middleware = RequestLimitsMiddleware(app)
    middleware._body_requests = 8
    responses = []

    async def receive():
        raise AssertionError("unauthorized body was read")

    async def send(message):
        responses.append(message)

    asyncio.run(
        middleware(
            {"type": "http", "method": method, "path": "/api/settings", "headers": []},
            receive,
            send,
        )
    )
    assert responses[0]["status"] == 401
    assert middleware._body_requests == 8


def test_anthropic_api_key_is_accepted_before_body_parsing():
    path = "/v1/messages"
    # Invalid messages yield validation, with no upstream call.
    client = TestClient(create_app())
    response = client.post(
        path, headers={"x-api-key": config.auth_key}, json={"messages": "invalid"}
    )
    assert response.status_code == 422
    assert response.json()["type"] == "error"
    response = client.post(
        path,
        headers={"x-api-key": config.auth_key, "Authorization": "Bearer invalid"},
        json={},
    )
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "authentication_error"


def test_api_key_header_is_not_accepted_for_other_endpoints():
    response = TestClient(create_app()).post(
        "/auth/login", headers={"x-api-key": config.auth_key}
    )
    assert response.status_code == 401


def test_user_key_still_cannot_create_other_keys():
    from services.auth_service import auth_service

    item, key = auth_service.create_key(role="user")
    try:
        client = TestClient(create_app())
        assert (
            client.post(
                "/auth/login", headers={"Authorization": f"Bearer {key}"}
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/auth/users", headers={"Authorization": f"Bearer {key}"}, json={}
            ).status_code
            == 403
        )
    finally:
        auth_service.delete_key(str(item["id"]))


def test_authenticated_capacity_and_cancellation_cleanup():
    async def scenario():
        entered = asyncio.Queue()

        async def app(scope, receive, send):
            raise AssertionError("incomplete upload reached endpoint")

        middleware = RequestLimitsMiddleware(app)
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [(b"authorization", f"Bearer {config.auth_key}".encode())],
        }

        async def slow_receive():
            await entered.put(True)
            await asyncio.Event().wait()

        async def discard(message):
            pass

        tasks = [
            asyncio.create_task(middleware(dict(scope), slow_receive, discard))
            for _ in range(8)
        ]
        try:
            for _ in tasks:
                await asyncio.wait_for(entered.get(), timeout=5)
            responses = []

            async def send(message):
                responses.append(message)

            await middleware(dict(scope), slow_receive, send)
            assert responses[0]["status"] == 429
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        assert middleware._body_requests == 0

    asyncio.run(scenario())
