from __future__ import annotations

import ipaddress
import mimetypes
import socket
import threading
from urllib.parse import ParseResult, urljoin, urlparse

from curl_cffi import CurlOpt, requests
from curl_cffi.curl import CURL_WRITEFUNC_ERROR
from fastapi import HTTPException

MAX_IMAGE_REDIRECTS = 3
_IMAGE_FETCH_SLOTS = threading.BoundedSemaphore(8)


def _clean(value: object) -> str:
    return str(value or "").strip()


def _response_mime_type(response: requests.Response, parsed_path: str) -> str:
    """识别下载图片类型：优先响应头，必要时按 URL 后缀推断。"""
    header_type = (
        str(response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    )
    guessed_type = mimetypes.guess_type(parsed_path)[0] or ""
    if header_type.startswith("image/"):
        return header_type
    if header_type and header_type not in {
        "application/octet-stream",
        "binary/octet-stream",
    }:
        raise HTTPException(
            status_code=400, detail={"error": "image_url must point to an image"}
        )
    if guessed_type.startswith("image/"):
        return guessed_type
    if not header_type or header_type in {
        "application/octet-stream",
        "binary/octet-stream",
    }:
        return "image/png"
    raise HTTPException(
        status_code=400, detail={"error": "image_url must point to an image"}
    )


def _validate_public_image_url(source: str) -> tuple[ParseResult, tuple[str, ...]]:
    """Validate a remote image URL before every network hop.

    Public image URLs are the supported contract. Resolving the hostname here
    blocks loopback, private, link-local, multicast, and other non-routable
    destinations that would otherwise make this endpoint an SSRF primitive.
    Redirects are validated independently by the caller.
    """
    if "\\" in source or any(ord(char) < 32 for char in source):
        raise HTTPException(status_code=400, detail={"error": "invalid image_url"})
    try:
        parsed = urlparse(source)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail={"error": "invalid image_url"}
        ) from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(
            status_code=400, detail={"error": "image_url must be an http or https URL"}
        )
    if parsed.username or parsed.password:
        raise HTTPException(
            status_code=400, detail={"error": "image_url must not include credentials"}
        )
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail={"error": "invalid image_url host"}
        ) from exc
    if not hostname:
        raise HTTPException(status_code=400, detail={"error": "invalid image_url host"})
    try:
        normalized_host = hostname.rstrip(".").lower().encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise HTTPException(
            status_code=400, detail={"error": "invalid image_url host"}
        ) from exc
    if "%" in normalized_host:
        raise HTTPException(status_code=400, detail={"error": "invalid image_url host"})
    authority = f"[{normalized_host}]" if ":" in normalized_host else normalized_host
    parsed = parsed._replace(
        netloc=f"{authority}:{port}" if port is not None else authority
    )
    if normalized_host in {
        "localhost",
        "localhost.localdomain",
    } or normalized_host.endswith(".local"):
        raise HTTPException(
            status_code=400,
            detail={"error": "image_url host is not publicly reachable"},
        )
    try:
        literal = ipaddress.ip_address(normalized_host)
    except ValueError:
        literal = None
    addresses: list[str]
    if literal is not None:
        addresses = [str(literal)]
    else:
        try:
            addresses = [
                str(item[4][0])
                for item in socket.getaddrinfo(
                    normalized_host, port, type=socket.SOCK_STREAM
                )
            ]
        except OSError as exc:
            raise HTTPException(
                status_code=400,
                detail={"error": "image_url host could not be resolved"},
            ) from exc
    if not addresses or any(
        not ipaddress.ip_address(address).is_global
        or ipaddress.ip_address(address).is_multicast
        for address in addresses
    ):
        raise HTTPException(
            status_code=400,
            detail={"error": "image_url host is not publicly reachable"},
        )
    return parsed, tuple(dict.fromkeys(addresses))


def _image_fetch_curl_options(
    parsed: ParseResult, addresses: tuple[str, ...]
) -> dict[CurlOpt, object]:
    """Pin the connection to the public addresses validated for this hop."""
    hostname = parsed.hostname or ""
    try:
        ipaddress.ip_address(hostname.rstrip("."))
    except ValueError:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        resolved = [
            f"{hostname}:{port}:{f'[{address}]' if ':' in address else address}"
            for address in addresses
        ]
    else:
        resolved = []
    options: dict[CurlOpt, object] = {CurlOpt.NOPROXY: "*"}
    if resolved:
        options[CurlOpt.RESOLVE] = resolved
    return options


def download_image_url(url: str, *, max_bytes: int) -> tuple[bytes, str]:
    """下载远程图片：把 http/https 图片链接转成标准图片输入元组。"""
    source = _clean(url)
    current = source
    for redirect_count in range(MAX_IMAGE_REDIRECTS + 1):
        acquired = False
        data = bytearray()
        oversized = False

        def receive_chunk(chunk: bytes) -> int:
            nonlocal oversized
            if len(data) + len(chunk) > max_bytes:
                oversized = True
                return CURL_WRITEFUNC_ERROR
            data.extend(chunk)
            return len(chunk)

        try:
            acquired = _IMAGE_FETCH_SLOTS.acquire(timeout=60)
            if not acquired:
                raise TimeoutError("image fetch concurrency limit reached")
            parsed, addresses = _validate_public_image_url(current)
            current = parsed.geturl()
            response = requests.get(
                current,
                headers={
                    "Accept": "image/*,*/*;q=0.8",
                    "User-Agent": "chatgpt2api image fetcher",
                },
                timeout=60,
                allow_redirects=False,
                content_callback=receive_chunk,
                verify=True,
                curl_options=_image_fetch_curl_options(parsed, addresses),
            )
        except HTTPException:
            if acquired:
                _IMAGE_FETCH_SLOTS.release()
            raise
        except Exception as exc:
            if acquired:
                _IMAGE_FETCH_SLOTS.release()
            message = (
                "image URL exceeds size limit"
                if oversized
                else "image_url fetch failed"
            )
            raise HTTPException(status_code=400, detail={"error": message}) from exc
        try:
            if 300 <= response.status_code < 400:
                if redirect_count >= MAX_IMAGE_REDIRECTS:
                    raise HTTPException(
                        status_code=400,
                        detail={"error": "image_url has too many redirects"},
                    )
                location = _clean(response.headers.get("location"))
                if not location:
                    raise HTTPException(
                        status_code=400,
                        detail={"error": "image_url redirect is missing a location"},
                    )
                current = urljoin(current, location)
                continue
            if not 200 <= response.status_code < 300:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": f"image_url fetch failed: HTTP {response.status_code}"
                    },
                )
            content_length = _clean(response.headers.get("content-length"))
            if (
                content_length
                and content_length.isdigit()
                and int(content_length) > max_bytes
            ):
                raise HTTPException(
                    status_code=400, detail={"error": "image URL exceeds size limit"}
                )
            if oversized:
                raise HTTPException(
                    status_code=400, detail={"error": "image URL exceeds size limit"}
                )
            if not data:
                raise HTTPException(
                    status_code=400,
                    detail={"error": "image_url returned empty content"},
                )
            mime_type = _response_mime_type(response, parsed.path)
            return bytes(data), mime_type
        finally:
            try:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            finally:
                _IMAGE_FETCH_SLOTS.release()
    raise HTTPException(
        status_code=400, detail={"error": "image_url has too many redirects"}
    )
