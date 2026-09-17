import base64
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from utils import helper


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1/x", "http://169.254.169.254/x", "http://[::1]/x"]
)
def test_chat_rejects_private_images_before_network(monkeypatch, url):
    calls = []

    def get(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(
            status_code=200, headers={"content-type": "image/png"}, content=b"image"
        )

    monkeypatch.setattr(helper.requests, "get", get)
    with pytest.raises(HTTPException):
        helper.extract_image_from_message_content(
            [{"type": "image_url", "image_url": {"url": url}}]
        )
    assert not calls


def test_chat_rejects_oversized_data_before_decode(monkeypatch):
    monkeypatch.setattr(helper, "MAX_JSON_IMAGE_BYTES", 3)
    with pytest.raises(HTTPException):
        helper.extract_image_from_message_content(
            [
                {
                    "type": "image_url",
                    "image_url": "data:image/png;base64,"
                    + base64.b64encode(b"1234").decode(),
                }
            ]
        )


def test_chat_rejects_image_count(monkeypatch):
    monkeypatch.setattr(helper, "MAX_JSON_EDIT_IMAGES", 1)
    image = {"type": "image_url", "image_url": "data:image/png;base64,eA=="}
    with pytest.raises(HTTPException):
        helper.extract_image_from_message_content([image, image])


from utils import remote_images
from curl_cffi import CurlOpt


class Response:
    def __init__(self, status=200, headers=None, chunks=(b"png",)):
        self.status_code = status
        self.headers = headers or {"content-type": "image/png"}
        self.chunks = chunks
        self.closed = False

    def iter_content(self, **kwargs):
        yield from self.chunks

    def close(self):
        self.closed = True


def test_public_image_pins_dns_and_streams(monkeypatch):
    monkeypatch.setattr(
        remote_images.socket,
        "getaddrinfo",
        lambda *a, **kw: [(None, None, None, None, ("93.184.216.34", 443))],
    )
    response = Response()
    captured = {}

    def get(url, **kwargs):
        captured.update(kwargs)
        kwargs["content_callback"](b"png")
        return response

    monkeypatch.setattr(remote_images.requests, "get", get)
    assert remote_images.download_image_url(
        "https://images.example/a.png", max_bytes=10
    ) == (b"png", "image/png")
    assert captured["curl_options"][CurlOpt.RESOLVE] == [
        "images.example:443:93.184.216.34"
    ]
    assert captured["curl_options"][CurlOpt.NOPROXY] == "*"
    assert (
        "stream" not in captured
        and captured["verify"]
        and not captured["allow_redirects"]
    )
    assert captured["timeout"] == 60
    assert response.closed


def test_redirect_to_private_is_rejected(monkeypatch):
    response = Response(302, {"location": "http://127.0.0.1/private"})
    calls = []

    def get(url, **kw):
        calls.append(url)
        return response

    monkeypatch.setattr(remote_images.requests, "get", get)
    with pytest.raises(HTTPException):
        remote_images.download_image_url("https://93.184.216.34/a", max_bytes=10)
    assert len(calls) == 1 and response.closed


def test_chunked_image_limit_closes_response(monkeypatch):
    response = Response(chunks=(b"123", b"456"))

    def get(*a, **kw):
        kw["content_callback"](b"123")
        kw["content_callback"](b"456")
        return response

    monkeypatch.setattr(remote_images.requests, "get", get)
    with pytest.raises(HTTPException):
        remote_images.download_image_url("https://93.184.216.34/a", max_bytes=5)
    assert response.closed


def test_whitespace_types_do_not_bypass_image_count(monkeypatch):
    monkeypatch.setattr(helper, "MAX_JSON_EDIT_IMAGES", 1)
    image = {"type": " image_url ", "image_url": "data:image/png;base64,eA=="}
    with pytest.raises(HTTPException):
        helper.extract_image_from_message_content([image, image])


def test_images_are_limited_across_messages(monkeypatch):
    from services.protocol.conversation import normalize_messages

    image = {"type": "image_url", "image_url": "data:image/png;base64,eA=="}
    with pytest.raises(HTTPException):
        normalize_messages([{"role": "user", "content": [image]} for _ in range(11)])


@pytest.mark.parametrize("url", ["http://224.0.0.1/a", "http://[ff02::1]/a"])
def test_multicast_is_not_a_public_image_destination(monkeypatch, url):
    def get(*a, **kw):
        raise AssertionError("multicast reached transport")

    monkeypatch.setattr(remote_images.requests, "get", get)
    with pytest.raises(HTTPException) as error:
        remote_images.download_image_url(url, max_bytes=5)
    assert error.value.detail["error"] == "image_url host is not publicly reachable"


def test_idn_connection_uses_same_ascii_host_as_dns_pin(monkeypatch):
    monkeypatch.setattr(
        remote_images.socket,
        "getaddrinfo",
        lambda *a, **kw: [(None, None, None, None, ("93.184.216.34", 443))],
    )
    calls = []

    def get(url, **kw):
        calls.append((url, kw))
        kw["content_callback"](b"png")
        return Response()

    monkeypatch.setattr(remote_images.requests, "get", get)
    remote_images.download_image_url("https://bücher.example/a", max_bytes=5)
    assert calls[0][0] == "https://xn--bcher-kva.example/a"
    assert calls[0][1]["curl_options"][CurlOpt.RESOLVE] == [
        "xn--bcher-kva.example:443:93.184.216.34"
    ]


@pytest.mark.parametrize(
    "path,handler",
    [
        ("/images/test.png", "get_image_response"),
        ("/image-thumbnails/test.png", "get_thumbnail_response"),
    ],
)
def test_public_image_preparation_does_not_block_other_requests(
    monkeypatch, path, handler
):
    import asyncio
    import threading
    import httpx
    from fastapi.responses import Response
    from api import system
    from api.app import create_app

    started = threading.Event()
    release = threading.Event()

    def slow_image(_):
        started.set()
        release.wait(2)
        return Response(b"image", media_type="image/png")

    monkeypatch.setattr(system, handler, slow_image)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
        ) as client:
            task = asyncio.create_task(client.get(path))
            try:
                assert await asyncio.to_thread(started.wait, 3)
                assert not task.done(), (
                    "image preparation blocked the event loop until completion"
                )
                response = await asyncio.wait_for(client.get("/version"), 0.5)
                assert response.status_code == 200
            finally:
                release.set()
                response = await task
            assert response.status_code == 200
            assert response.content == b"image"

    asyncio.run(scenario())


def test_public_image_capacity_survives_request_cancellation(monkeypatch):
    import asyncio
    import threading
    import httpx
    from fastapi.responses import Response
    from api import system
    from api.app import create_app

    release = threading.Event()

    async def scenario():
        loop = asyncio.get_running_loop()
        started = asyncio.Queue()

        def slow_image(_):
            loop.call_soon_threadsafe(started.put_nowait, True)
            release.wait(5)
            return Response(b"image")

        monkeypatch.setattr(system, "get_image_response", slow_image)
        monkeypatch.setattr(system, "get_thumbnail_response", slow_image)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
        ) as client:
            tasks = [
                asyncio.create_task(client.get("/images/test.png")) for _ in range(4)
            ]
            try:
                for _ in tasks:
                    await asyncio.wait_for(started.get(), 2)
                response = await client.get("/image-thumbnails/test.png")
                assert response.status_code == 429
                assert response.headers["retry-after"] == "1"
                assert (await client.get("/version")).status_code == 200
                tasks[0].cancel()
                await asyncio.gather(tasks[0], return_exceptions=True)
                assert (await client.get("/images/test.png")).status_code == 429
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)
            # The cancelled request's worker may finish after the others; no
            # worker remains blocked, so a normal request can now be admitted.
            response = await client.get("/image-thumbnails/test.png")
            assert response.status_code == 200

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "path", ["/images/missing.png", "/image-thumbnails/missing.png"]
)
def test_public_image_errors_release_capacity(monkeypatch, path):
    import asyncio
    import httpx
    from api import system
    from api.app import create_app

    def missing(_):
        raise HTTPException(status_code=404, detail="image not found")

    monkeypatch.setattr(system, "get_image_response", missing)
    monkeypatch.setattr(system, "get_thumbnail_response", missing)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
        ) as client:
            for _ in range(8):
                assert (await client.get(path)).status_code == 404

    asyncio.run(scenario())


def test_public_images_serve_local_assets_and_thumbnails(tmp_path, monkeypatch):
    import asyncio
    import io
    import httpx
    from PIL import Image
    from api.app import create_app
    from services import image_service
    from services.image_storage_service import ImageStorageService

    class ImageConfig:
        images_dir = tmp_path / "images"
        image_thumbnails_dir = tmp_path / "thumbnails"
        base_url = "http://test"

        def get_image_storage_settings(self):
            return {"mode": "local"}

    import importlib

    storage_module = importlib.import_module("services.image_storage_service")
    monkeypatch.setattr(storage_module, "config", ImageConfig())
    monkeypatch.setattr(image_service, "config", ImageConfig())
    storage = ImageStorageService(tmp_path / "index.json")
    monkeypatch.setattr(image_service, "image_storage_service", storage)
    buf = io.BytesIO()
    Image.new("RGB", (640, 480), "red").save(buf, format="PNG")
    payload = buf.getvalue()
    asset = storage.save(payload)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
        ) as client:
            response = await client.get("/images/" + asset.rel)
            assert response.status_code == 200
            assert response.content == payload
            assert response.headers["content-type"] == "image/png"
            for _ in range(2):
                thumb = await client.get("/image-thumbnails/" + asset.rel)
                assert thumb.status_code == 200
                with Image.open(io.BytesIO(thumb.content)) as image:
                    assert image.size == (320, 240)
            assert (await client.get("/images/missing.png")).status_code == 404
            assert (await client.get("/images/%2e%2e/config.json")).status_code == 404

    asyncio.run(scenario())


def test_concurrent_thumbnail_reads_do_not_generate_same_file_twice(
    monkeypatch, tmp_path
):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor
    from services import image_service

    guard = threading.Lock()
    active = 0
    peak = 0

    def prepare(_):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03)
        with guard:
            active -= 1
        return tmp_path / "thumbnail.png"

    monkeypatch.setattr(image_service, "ensure_thumbnail", prepare)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(image_service.get_thumbnail_response, ["same.png"] * 4))
    assert peak == 1
