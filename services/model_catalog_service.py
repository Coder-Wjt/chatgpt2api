from __future__ import annotations

from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Event, Lock, Thread
from time import monotonic
import hashlib
import json
from typing import Any

from contracts.models import (
    ModelCatalogCapabilities,
    ModelCatalogDefaults,
    ModelCatalogSource,
    ModelCatalogView,
)
from services.account_service import account_service
from services.config import config
from services.account_processing import account_processing_slot
from services.openai_backend_api import OpenAIBackendAPI
from utils.helper import CODEX_IMAGE_MODEL, WEB_IMAGE_MODELS
from utils.log import logger


FALLBACK_CHAT_MODELS = [
    "gpt-5.6",
    "auto",
]

FALLBACK_IMAGE_MODELS = list(WEB_IMAGE_MODELS)


def _normalize_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    values: list[str] = []
    seen: set[str] = set()
    for item in raw:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        values.append(value)
    return values


def _settings_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _configured_chat_models(settings: dict[str, Any]) -> list[str]:
    catalog = _settings_dict(settings.get("model_catalog"))
    explicit = _normalize_list(catalog.get("chat_models"))
    if explicit:
        return explicit

    combined: list[str] = []
    for key in (
        "base_chat_models",
        "specialized_chat_models",
        "image_capable_chat_models",
    ):
        for model in _normalize_list(catalog.get(key)):
            if model not in combined:
                combined.append(model)
    return combined


def _configured_image_models(settings: dict[str, Any]) -> list[str]:
    image_generation = _settings_dict(settings.get("image_generation"))
    catalog = _settings_dict(settings.get("model_catalog"))
    for source in (
        image_generation.get("model_options"),
        catalog.get("image_api_models"),
        image_generation.get("supported_models"),
    ):
        models = _normalize_list(source)
        if models:
            return models
    return []


def _image_models_from_accounts(accounts: list[dict[str, Any]]) -> list[str]:
    available_accounts = [
        account
        for account in accounts
        if isinstance(account, dict)
        and account_service._is_image_account_available(account)
    ]
    if not available_accounts:
        return []

    models = list(WEB_IMAGE_MODELS)
    codex_types = {
        normalized
        for account in available_accounts
        if account_service._normalize_source_type(account.get("source_type")) == "codex"
        and (normalized := account_service._normalize_account_type(account.get("type")))
    }

    if codex_types & {"Plus", "Team", "Pro"}:
        models.append(CODEX_IMAGE_MODEL)
    for plan_type in ("Plus", "Team", "Pro"):
        if plan_type in codex_types:
            models.append(f"{plan_type.lower()}-{CODEX_IMAGE_MODEL}")
    return models


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _generated_at() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _revision(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


MODEL_CACHE_TTL = 300.0
MODEL_CACHE_MAX_AGE = 3600.0
MODEL_RETRY_DELAY = 60.0
MODEL_COLD_WAIT = 1.0


@dataclass
class _AccountModels:
    models: tuple[str, ...] = ()
    succeeded_at: float | None = None
    retry_at: float = 0.0


class ModelCatalogService:
    """Build the model facts shared by management and OpenAI-compatible APIs."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._cache: dict[str, _AccountModels] = {}
        self._refresh_thread: Thread | None = None
        self._refresh_done = Event()
        self._refresh_done.set()
        self._closed = False

    def start(self) -> None:
        with self._lock:
            self._closed = False

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            thread = self._refresh_thread
        if thread:
            thread.join()

    @staticmethod
    def _account_key(account: dict[str, Any]) -> str:
        # Include routing and plan changes without retaining raw credentials in keys.
        value = {
            key: account.get(key)
            for key in (
                "access_token",
                "type",
                "source_type",
                "proxy",
                "group_id",
            )
        }
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, default=str).encode()
        ).hexdigest()

    def _fetch(self, key: str, account: dict[str, Any]) -> None:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None or self._closed:
                return
        models: tuple[str, ...] = ()
        try:
            with account_processing_slot():
                token = account_service.ensure_access_token(
                    str(account["access_token"]), event="model_catalog"
                )
                if not token:
                    raise ValueError("account credentials unavailable")
                with OpenAIBackendAPI(access_token=token) as backend:
                    payload = backend.list_models()
            data = payload.get("data")
            if isinstance(data, list):
                models = tuple(
                    dict.fromkeys(
                        item["id"].strip()
                        for item in data
                        if isinstance(item, dict)
                        and isinstance(item.get("id"), str)
                        and item["id"].strip()
                    )
                )
        except Exception as exc:
            # Log only a stage/type, never exception strings, tokens or response bodies.
            logger.warning({
                "event": "model_catalog_refresh_failed",
                "error_type": type(exc).__name__,
                "stage": getattr(exc, "upstream_stage", "model_discovery"),
            })
        with self._lock:
            if self._cache.get(key) is not entry:
                return
            now = monotonic()
            entry.retry_at = now + (MODEL_CACHE_TTL if models else MODEL_RETRY_DELAY)
            if models:
                entry.models = models
                entry.succeeded_at = now

    def _refresh(self, due: list[tuple[str, dict[str, Any]]]) -> None:
        try:
            with ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="model-discovery"
            ) as pool:
                # Submit at most two jobs at a time; no unbounded executor backlog.
                for offset in range(0, len(due), 2):
                    with self._lock:
                        if self._closed:
                            break
                    futures = [
                        pool.submit(self._fetch, key, account)
                        for key, account in due[offset : offset + 2]
                    ]
                    for future in futures:
                        future.result()
        finally:
            self._refresh_done.set()

    def _upstream_models(self, accounts: list[dict[str, Any]]) -> list[str]:
        eligible = {
            self._account_key(account): account
            for account in accounts
            if account.get("access_token")
            and account_service._is_account_selectable(
                account, allow_limited=True, allow_image_pending=True
            )
        }
        now = monotonic()
        with self._lock:
            self._cache = {
                key: self._cache.get(key, _AccountModels()) for key in eligible
            }
            due = [
                (key, account)
                for key, account in eligible.items()
                if self._cache[key].retry_at <= now
            ]
            has_cache = any(
                entry.models
                and entry.succeeded_at is not None
                and now - entry.succeeded_at <= MODEL_CACHE_MAX_AGE
                for entry in self._cache.values()
            )
            if due and self._refresh_done.is_set() and not self._closed:
                self._refresh_done.clear()
                self._refresh_thread = Thread(
                    target=self._refresh, args=(due,), name="model-catalog", daemon=True
                )
                self._refresh_thread.start()
        if not has_cache:
            self._refresh_done.wait(MODEL_COLD_WAIT)
        with self._lock:
            now = monotonic()
            return _unique(
                [
                    model
                    for key in eligible
                    if (entry := self._cache.get(key)) is not None
                    and entry.succeeded_at is not None
                    and now - entry.succeeded_at <= MODEL_CACHE_MAX_AGE
                    for model in entry.models
                ]
            )

    def view(self) -> ModelCatalogView:
        settings = config.get()
        configured_chat_models = _configured_chat_models(settings)
        configured_image_models = _configured_image_models(settings)

        accounts = account_service.list_accounts()
        upstream_models = (
            [] if configured_chat_models else self._upstream_models(accounts)
        )
        chat_source = (
            "config"
            if configured_chat_models
            else ("accounts" if upstream_models else "fallback")
        )
        chat_models = _unique(
            configured_chat_models
            or (
                ["auto", *upstream_models]
                if upstream_models
                else list(FALLBACK_CHAT_MODELS)
            )
        )

        if configured_image_models:
            image_source = "config"
            image_models = _unique(configured_image_models)
        else:
            account_models = _image_models_from_accounts(accounts)
            image_source = "accounts" if account_models else "fallback"
            image_models = _unique(account_models or list(FALLBACK_IMAGE_MODELS))

        all_models = _unique([*chat_models, *image_models])
        high_resolution_models = [
            model
            for model in image_models
            if model == CODEX_IMAGE_MODEL or model.endswith(f"-{CODEX_IMAGE_MODEL}")
        ]
        defaults = {
            "chat_model": "auto" if "auto" in chat_models else chat_models[0],
            "image_model": "gpt-image-2"
            if "gpt-image-2" in image_models
            else image_models[0],
        }
        capabilities = {
            "image_upscale": bool(settings.get("image_upscale_enabled")),
            "high_resolution_image_models": high_resolution_models,
        }
        source = {"chat": chat_source, "image": image_source}
        revision_payload = {
            "chat_models": chat_models,
            "image_models": image_models,
            "defaults": defaults,
            "capabilities": capabilities,
            "source": source,
        }

        return ModelCatalogView(
            generated_at=_generated_at(),
            revision=_revision(revision_payload),
            chat_models=tuple(chat_models),
            image_models=tuple(image_models),
            all_models=tuple(all_models),
            defaults=ModelCatalogDefaults(**defaults),
            capabilities=ModelCatalogCapabilities(
                image_upscale=capabilities["image_upscale"],
                high_resolution_image_models=tuple(high_resolution_models),
            ),
            source=ModelCatalogSource(**source),
        )


model_catalog_service = ModelCatalogService()


def get_model_catalog() -> ModelCatalogView:
    return model_catalog_service.view()
