from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse
from utils.diagnostics import sanitize_diagnostic_text


class TextGenerationError(RuntimeError):
    """Text-call failure shared by HTTP, SSE and call-record projections."""

    def __init__(self, message: str, code: str = "upstream_error", status_code: int = 502) -> None:
        self.code = code
        self.status_code = status_code
        self.public_error = sanitize_diagnostic_text(message, limit=1000)
        super().__init__(self.public_error)

    def to_openai_error(self) -> dict[str, Any]:
        return openai_error_payload(self.public_error, self.status_code, code=self.code)

    @classmethod
    def from_detail(cls, detail: object, status_code: int = 502) -> TextGenerationError:
        if isinstance(detail, dict):
            for key in ("error", "detail"):
                if isinstance(detail.get(key), (dict, str)):
                    return cls.from_detail(detail[key], status_code)
            code = str(detail.get("code") or "upstream_error")
            if code == "work_subscription_required":
                status_code = 403
            return cls(str(detail.get("message") or "Upstream text generation failed."), code, status_code)
        return cls(detail if isinstance(detail, str) and detail else "Upstream text generation failed.",
                   status_code=status_code)


def _message_from_value(value: object) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    message = value.get("message")
    if isinstance(message, str) and message:
        return message
    return _message_from_value(value.get("error"))


def error_message_from_detail(detail: object) -> str:
    if isinstance(detail, list):
        messages = []
        for item in detail:
            if not isinstance(item, dict):
                continue
            location = ".".join(str(part) for part in item.get("loc", []) if part != "body")
            message = str(item.get("msg") or "").strip()
            if location and message:
                messages.append(f"{location}: {message}")
            elif message:
                messages.append(message)
        return "; ".join(messages)
    if isinstance(detail, dict):
        message = _message_from_value(detail.get("error")) or _message_from_value(detail)
        if message:
            return message
    return str(detail or "").strip()


def _default_error_type(status_code: int) -> str:
    if status_code == 401:
        return "authentication_error"
    if status_code == 403:
        return "permission_error"
    if status_code == 429:
        return "rate_limit_error"
    if 400 <= status_code < 500:
        return "invalid_request_error"
    return "server_error"


def _default_error_code(status_code: int) -> str:
    if status_code == 401:
        return "invalid_api_key"
    if status_code == 403:
        return "permission_denied"
    if status_code == 429:
        return "rate_limit_exceeded"
    if 400 <= status_code < 500:
        return "bad_request"
    return "upstream_error"


def openai_error_payload(
    detail: object,
    status_code: int,
    *,
    error_type: str | None = None,
    code: object | None = None,
    param: object | None = None,
) -> dict[str, Any]:
    error_detail = detail.get("error") if isinstance(detail, dict) else None
    if isinstance(error_detail, dict):
        return {
            "error": {
                "message": error_message_from_detail(error_detail) or "request failed",
                "type": str(error_detail.get("type") or error_type or _default_error_type(status_code)),
                "param": error_detail.get("param", param),
                "code": error_detail.get("code", code if code is not None else _default_error_code(status_code)),
            }
        }
    return {
        "error": {
            "message": error_message_from_detail(detail) or "request failed",
            "type": error_type or _default_error_type(status_code),
            "param": param,
            "code": code if code is not None else _default_error_code(status_code),
        }
    }


def openai_error_response(
    detail: object,
    status_code: int,
    *,
    headers: dict[str, str] | None = None,
    error_type: str | None = None,
    code: object | None = None,
    param: object | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=openai_error_payload(detail, status_code, error_type=error_type, code=code, param=param),
        headers=headers,
    )


def anthropic_error_response(
    detail: object,
    status_code: int,
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    error_type = "api_error" if status_code >= 500 else _default_error_type(status_code)
    return JSONResponse(
        status_code=status_code,
        content={
            "type": "error",
            "error": {
                "type": error_type,
                "message": error_message_from_detail(detail) or "request failed",
            },
        },
        headers=headers,
    )
