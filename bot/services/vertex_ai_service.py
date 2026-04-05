"""
bot/services/vertex_ai_service.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Async wrapper around the Google Gen AI SDK for image generation.

Supports two authentication modes (in priority order):
1. API key(s) from environment variables (GOOGLE_CLOUD_API_KEY, _1, _2, _3)
2. Service-account JSON files from data/service_accounts/ directory

Multiple keys/accounts rotate automatically on 429 errors.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from bot.config import Settings
from core.exceptions import (
    GenerationError,
    QuotaExceededError,
    SafetyFilterError,
)

logger = logging.getLogger(__name__)

COOLDOWN_SECONDS = 10

SA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "service_accounts"


def _is_retryable(exc: BaseException) -> bool:
    msg = str(exc).lower()
    retryable_keywords = (
        "429", "quota", "resource exhausted", "rate limit",
        "too many requests", "server error", "503", "500",
        "internal", "temporarily unavailable",
    )
    return any(kw in msg for kw in retryable_keywords)


def _is_model_error(exc: BaseException) -> bool:
    """400 INVALID_ARGUMENT — the model doesn't support this key/config, skip slot."""
    msg = str(exc).lower()
    return "400" in msg and "invalid_argument" in msg


def _is_auth_error(exc: BaseException) -> bool:
    """401/403 — the key is disabled, revoked, or lacks permissions."""
    msg = str(exc).lower()
    if "401" in msg or "unauthenticated" in msg or "access_token_type_unsupported" in msg:
        return True
    if "403" in msg or "permission_denied" in msg or "forbidden" in msg:
        return True
    return False


def _is_safety_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    safety_keywords = (
        "safety", "blocked", "harm", "policy", "prohibited",
        "content_filter", "safetyfiltererror", "finish_reason: safety",
    )
    return any(kw in msg for kw in safety_keywords)


def _get_safety_settings() -> list[Any]:
    from google.genai import types as genai_types
    return [
        genai_types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="OFF"),
        genai_types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="OFF"),
        genai_types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="OFF"),
        genai_types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="OFF"),
    ]


def _build_config_for_model(
    model: str,
    aspect_ratio: str = "1:1",
    has_images: bool = False,
    thinking_level: str = "low",
) -> Any:
    from google.genai import types as genai_types

    safety = _get_safety_settings()

    image_cfg_kwargs: dict[str, Any] = {
        "output_mime_type": "image/png",
    }
    if not has_images:
        image_cfg_kwargs["aspect_ratio"] = aspect_ratio

    config_kwargs: dict[str, Any] = {
        "temperature": 1,
        "top_p": 0.95,
        "max_output_tokens": 32768,
        "response_modalities": ["TEXT", "IMAGE"],
        "safety_settings": safety,
        "image_config": genai_types.ImageConfig(**image_cfg_kwargs),
    }

    if "flash" in model.lower() and "lite" not in model.lower():
        level = thinking_level.upper() if thinking_level != "none" else "NONE"
        config_kwargs["thinking_config"] = genai_types.ThinkingConfig(
            thinking_level=level,
        )

    return genai_types.GenerateContentConfig(**config_kwargs)


def _load_sa_files() -> list[Path]:
    if not SA_DIR.exists():
        SA_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(SA_DIR.glob("*.json"))
    return [f for f in files if f.stat().st_size > 10]


class _BaseSlot:
    """Abstract base for credential slots."""

    def __init__(self, index: int) -> None:
        self.index = index
        self.client: Any = None
        self.cooldown_until: float = 0.0

    @property
    def label(self) -> str:
        raise NotImplementedError

    @property
    def is_available(self) -> bool:
        return time.monotonic() >= self.cooldown_until

    def mark_rate_limited(self) -> None:
        self.cooldown_until = time.monotonic() + COOLDOWN_SECONDS
        logger.warning("Account '%s' rate-limited, cooldown +%ds", self.label, COOLDOWN_SECONDS)

    def get_client(self) -> Any:
        raise NotImplementedError

    def reset_client(self) -> None:
        self.client = None
        self.cooldown_until = 0.0


class _ApiKeySlot(_BaseSlot):
    """Slot that authenticates via a Google API key with Vertex AI backend."""

    def __init__(self, api_key: str, index: int) -> None:
        super().__init__(index)
        self._api_key = api_key

    @property
    def label(self) -> str:
        return f"api_key_{self.index + 1}"

    def get_client(self) -> Any:
        if self.client is None:
            import google.genai as genai
            self.client = genai.Client(
                vertexai=True,
                api_key=self._api_key,
            )
            logger.info("Initialised genai client for '%s' (Vertex AI + API key mode)", self.label)
        return self.client


class _CredSlot(_BaseSlot):
    """Slot that authenticates via a service-account JSON file (Vertex AI)."""

    def __init__(self, sa_path: Path, index: int) -> None:
        super().__init__(index)
        self.sa_path = sa_path
        self._project_id: str | None = None
        self._load_project_id()

    def _load_project_id(self) -> None:
        try:
            with open(self.sa_path, "r") as f:
                data = json.load(f)
            self._project_id = data.get("project_id")
        except Exception:
            self._project_id = None

    @property
    def label(self) -> str:
        return self.sa_path.stem

    def get_client(self) -> Any:
        if self.client is None:
            import google.genai as genai
            self.client = genai.Client(
                vertexai=True,
                project=self._project_id,
                location="global",
                credentials=self._get_credentials(),
            )
            logger.info("Initialised genai client for account '%s' (project=%s)", self.label, self._project_id)
        return self.client

    def _get_credentials(self) -> Any:
        from google.oauth2 import service_account as sa
        return sa.Credentials.from_service_account_file(
            str(self.sa_path),
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )


class VertexAIService:
    """
    Service that generates images via Google Gen AI API.
    Uses API keys (priority) or service-account JSON files for authentication.
    Rotates between multiple keys/accounts on 429 errors.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_requests)

        slots: list[_BaseSlot] = []

        # --- Priority 1: API keys (migrate env vars into store, then load all) ---
        from bot.api_keys_store import get_all_keys, migrate_env_keys
        migrate_env_keys()
        api_keys = get_all_keys()
        for i, key in enumerate(api_keys):
            slots.append(_ApiKeySlot(api_key=key, index=i))
        if api_keys:
            logger.info("Loaded %d API key(s) for authentication", len(api_keys))

        # --- Priority 2: Service account JSON files (fallback) ---
        if not slots:
            sa_files = _load_sa_files()
            for i, f in enumerate(sa_files):
                slots.append(_CredSlot(sa_path=f, index=i))
            if sa_files:
                logger.info("Loaded %d service account file(s) for authentication", len(sa_files))

        if not slots:
            logger.warning(
                "No credentials found — bot will start but reject AI requests. "
                "Add API keys via the admin panel."
            )

        self._slots = slots
        self._current_index = 0
        self._lock = asyncio.Lock()

        logger.info("VertexAIService initialised with %d credential slot(s)", len(self._slots))

    def reload_keys(self, settings: Settings | None = None) -> None:
        from bot.api_keys_store import get_all_keys
        slots: list[_BaseSlot] = []
        api_keys = get_all_keys()
        for i, key in enumerate(api_keys):
            slots.append(_ApiKeySlot(api_key=key, index=i))
        if not slots:
            sa_files = _load_sa_files()
            for i, f in enumerate(sa_files):
                slots.append(_CredSlot(sa_path=f, index=i))
        self._slots = slots
        self._current_index = 0
        if slots:
            logger.info("Reloaded %d credential slot(s)", len(self._slots))
        else:
            logger.warning("reload_keys: all credentials removed — bot will reject requests")

    @property
    def is_at_capacity(self) -> bool:
        return self._semaphore.locked()

    @property
    def key_count(self) -> int:
        return len(self._slots)

    def _get_next_available_slot(self) -> _BaseSlot | None:
        n = len(self._slots)
        for i in range(n):
            idx = (self._current_index + i) % n
            if self._slots[idx].is_available:
                self._current_index = (idx + 1) % n
                return self._slots[idx]
        return None

    async def generate_image(
        self,
        prompt: str,
        images: list[bytes] | None = None,
        model_override: str | None = None,
        aspect_ratio: str = "1:1",
        thinking_level: str = "low",
    ) -> bytes:
        if self._semaphore.locked():
            logger.info("Semaphore at capacity – request queued for '%s'", prompt[:60])

        async with self._semaphore:
            model = model_override or self._settings.vertex_ai_model
            return await self._try_all_keys(prompt, images, model, aspect_ratio, thinking_level)

    async def _try_all_keys(
        self,
        prompt: str,
        images: list[bytes] | None,
        model: str,
        aspect_ratio: str,
        thinking_level: str = "low",
    ) -> bytes:
        tried_keys: set[int] = set()
        n = len(self._slots)

        while len(tried_keys) < n:
            async with self._lock:
                slot = self._get_next_available_slot()

            if slot is None:
                break

            if slot.index in tried_keys:
                break

            tried_keys.add(slot.index)

            try:
                logger.info(
                    "Trying '%s' for model %s, prompt='%s'",
                    slot.label, model, prompt[:60],
                )
                result = await self._call_api(slot, prompt, images, model, aspect_ratio, thinking_level)
                return result
            except Exception as exc:
                logger.error(
                    "Slot '%s' error for '%s': %s",
                    slot.label, prompt[:60], repr(exc),
                )
                if _is_safety_error(exc):
                    raise SafetyFilterError(str(exc)) from exc
                if _is_retryable(exc):
                    slot.mark_rate_limited()
                    logger.warning(
                        "Slot '%s' returned 429 for '%s', rotating...",
                        slot.label, prompt[:60],
                    )
                    continue
                if _is_auth_error(exc):
                    slot.reset_client()
                    logger.warning(
                        "Slot '%s' auth error for '%s', key invalid — skipping: %s",
                        slot.label, prompt[:60], exc,
                    )
                    continue
                if _is_model_error(exc):
                    logger.warning(
                        "Slot '%s' returned 400 for '%s', model issue — skipping...",
                        slot.label, prompt[:60],
                    )
                    continue
                raise GenerationError(str(exc)) from exc

        logger.error("All %d credential slots exhausted for model %s", n, model)
        raise QuotaExceededError()

    async def _call_api(
        self, slot: _BaseSlot,
        prompt: str, images: list[bytes] | None,
        model: str, aspect_ratio: str, thinking_level: str = "low",
    ) -> bytes:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._sync_generate, slot, prompt, images, model, aspect_ratio, thinking_level
        )

    def _sync_generate(
        self, slot: _BaseSlot,
        prompt: str, images: list[bytes] | None,
        model: str, aspect_ratio: str, thinking_level: str = "low",
    ) -> bytes:
        from google.genai import types as genai_types

        client = slot.get_client()

        parts: list[Any] = []

        if images:
            for img_data in images:
                parts.append(
                    genai_types.Part.from_bytes(
                        data=img_data,
                        mime_type="image/jpeg",
                    )
                )

        parts.append(genai_types.Part.from_text(text=prompt))

        contents = [
            genai_types.Content(
                role="user",
                parts=parts,
            )
        ]

        config = _build_config_for_model(model, aspect_ratio, has_images=bool(images), thinking_level=thinking_level)

        image_bytes: bytes | None = None
        text_parts: list[str] = []

        for chunk in client.models.generate_content_stream(
            model=model,
            contents=contents,
            config=config,
        ):
            if not chunk.candidates:
                continue
            for part in chunk.candidates[0].content.parts:
                if getattr(part, "inline_data", None) is not None:
                    image_bytes = part.inline_data.data
                elif getattr(part, "text", None):
                    text_parts.append(part.text)

        if image_bytes:
            return image_bytes

        if text_parts:
            refusal_text = "".join(text_parts)
            safety_keywords = (
                "не могу", "cannot", "sorry", "извините", "unable",
                "запрещ", "нельзя", "безопасност", "safety", "policy",
            )
            if any(kw in refusal_text.lower() for kw in safety_keywords):
                raise SafetyFilterError(refusal_text)
            raise GenerationError(f"Модель вернула текст вместо изображения: {refusal_text[:300]}")

        raise GenerationError("The model did not return an image part.")

    CHAT_MODEL = "gemini-3.1-flash-lite-preview"

    async def chat_text(self, contents: list[Any]) -> str:
        n = len(self._slots)
        tried_keys: set[int] = set()

        while len(tried_keys) < n:
            async with self._lock:
                slot = self._get_next_available_slot()

            if slot is None:
                break
            if slot.index in tried_keys:
                break
            tried_keys.add(slot.index)

            try:
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(
                    None, self._sync_chat, slot, contents
                )
            except Exception as exc:
                if _is_retryable(exc):
                    slot.mark_rate_limited()
                    logger.warning("Chat: slot '%s' returned 429, rotating...", slot.label)
                    continue
                if _is_auth_error(exc):
                    slot.reset_client()
                    logger.warning("Chat: slot '%s' auth error, key invalid — skipping: %s", slot.label, exc)
                    continue
                if _is_model_error(exc):
                    logger.warning("Chat: slot '%s' returned 400, model issue — skipping", slot.label)
                    continue
                raise GenerationError(str(exc)) from exc

        raise QuotaExceededError()

    def _sync_chat(self, slot: _BaseSlot, contents: list[Any]) -> str:
        from google.genai import types as genai_types

        client = slot.get_client()

        config = genai_types.GenerateContentConfig(
            temperature=1,
            top_p=0.95,
            seed=0,
            max_output_tokens=65535,
            safety_settings=_get_safety_settings(),
            thinking_config=genai_types.ThinkingConfig(thinking_level="LOW"),
        )

        text_parts: list[str] = []
        for chunk in client.models.generate_content_stream(
            model=self.CHAT_MODEL,
            contents=contents,
            config=config,
        ):
            if not chunk.candidates:
                continue
            for part in chunk.candidates[0].content.parts:
                if getattr(part, "text", None):
                    text_parts.append(part.text)

        return "".join(text_parts) if text_parts else ""
