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
