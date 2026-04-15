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
import datetime
import json
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Any

from bot.config import Settings
from core.exceptions import (
    AmbiguousPromptError,
    GenerationError,
    QuotaExceededError,
    SafetyFilterError,
)

logger = logging.getLogger(__name__)

_alert_task_refs: set[asyncio.Task] = set()

def _fire_alert(coro):
    task = asyncio.ensure_future(coro)
    _alert_task_refs.add(task)
    task.add_done_callback(_alert_task_refs.discard)

# When a 429 is received the slot is locked out for the full quota-reset window.
COOLDOWN_SECONDS = 60

# Proactive sliding-window rate limiting — per key, per model (independent quotas).
# Each model family has its own QPM bucket on the same API key.
RATE_WINDOW_SECONDS = 60

# QPM limits per model name substring (longest match wins, "default" is the fallback).
# Image-generation models are expensive — keep their QPM low.
# Text/chat models (gemini-3.1-pro-preview, etc.) have much higher quotas.
MODEL_QPM: dict[str, int] = {
    "flash-image": 5,   # gemini-x.x-flash-image-preview
    "pro-image":   3,   # gemini-x-pro-image-preview
    "default":    60,   # text/chat models — no artificial low limit
}


def _qpm_for_model(model: str) -> int:
    """Return the requests-per-minute limit for a given model name."""
    model_lower = model.lower()
    for key, qpm in MODEL_QPM.items():
        if key != "default" and key in model_lower:
            return qpm
    return MODEL_QPM["default"]

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
    """401/403 — the key is disabled, revoked, or lacks permissions.
    Note: pydantic errors contain 'extra_forbidden' — must NOT be treated as auth errors.
    """
    msg = str(exc).lower()
    # Pydantic ValidationError contains "extra_forbidden" — skip it
    if "extra_forbidden" in msg or "validation error" in msg:
        return False
    if "401" in msg or "unauthenticated" in msg or "access_token_type_unsupported" in msg:
        return True
    if "403" in msg or "permission_denied" in msg:
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

    MAX_HISTORY = 200

    def __init__(self, index: int) -> None:
        self.index = index
        self.client: Any = None
        self.cooldown_until: float = 0.0
        self.auth_error: bool = False
        self.auth_error_msg: str = ""
        self.active_requests: int = 0
        self.last_used_at: float = 0.0
        self.last_model: str = ""
        self.total_ok: int = 0
        self.total_err: int = 0
        self._model_request_times: dict[str, list[float]] = {}
        self.history: deque[dict] = deque(maxlen=self.MAX_HISTORY)

    def record_history(
        self,
        *,
        user_id: int | None,
        username: str,
        prompt: str,
        model: str,
        status: str,
        error: str = "",
        duration_ms: int = 0,
    ) -> None:
        self.history.appendleft({
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "user_id": user_id,
            "username": username,
            "prompt": prompt[:120],
            "model": model,
            "status": status,
            "error": error[:200] if error else "",
            "duration_ms": duration_ms,
        })

    @property
    def label(self) -> str:
        raise NotImplementedError

    # ── cooldown (post-429, model-agnostic) ───────────────────────────────
    @property
    def is_available(self) -> bool:
        return time.monotonic() >= self.cooldown_until

    def mark_rate_limited(self) -> None:
        self.cooldown_until = time.monotonic() + COOLDOWN_SECONDS
        logger.warning("Account '%s' rate-limited, cooldown +%ds", self.label, COOLDOWN_SECONDS)

    # ── proactive sliding-window rate limit (per model) ───────────────────
    def _trim_model_window(self, model: str) -> list[float]:
        cutoff = time.monotonic() - RATE_WINDOW_SECONDS
        times = [t for t in self._model_request_times.get(model, []) if t >= cutoff]
        self._model_request_times[model] = times
        return times

    def requests_in_window(self, model: str) -> int:
        return len(self._trim_model_window(model))

    def has_capacity(self, model: str) -> bool:
        """True if this slot can accept one more request for the given model."""
        return self.requests_in_window(model) < _qpm_for_model(model)

    def next_capacity_at(self, model: str) -> float:
        """Monotonic timestamp when this slot will next have capacity for model (0 = now)."""
        times = self._trim_model_window(model)
        qpm = _qpm_for_model(model)
        if len(times) < qpm:
            return 0.0
        return min(times) + RATE_WINDOW_SECONDS

    def record_request(self, model: str) -> None:
        """Call immediately before dispatching an API request for model."""
        self._model_request_times.setdefault(model, []).append(time.monotonic())

    def requests_in_window_family(self, family: str) -> int:
        """Sum requests in window for all models whose name contains `family`."""
        now = time.monotonic()
        cutoff = now - RATE_WINDOW_SECONDS
        total = 0
        for key in list(self._model_request_times.keys()):
            if family in key.lower():
                times = [t for t in self._model_request_times[key] if t >= cutoff]
                self._model_request_times[key] = times
                total += len(times)
        return total

    # ── combined availability (per model) ─────────────────────────────────
    def ready_at(self, model: str) -> float:
        """Earliest time this slot can serve a new request for model."""
        return max(self.cooldown_until, self.next_capacity_at(model))

    def is_ready(self, model: str) -> bool:
        return time.monotonic() >= self.ready_at(model)

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
        if self._lock.locked():
            self._slots = slots
            self._current_index = 0
        else:
            try:
                loop = asyncio.get_running_loop()
                async def _swap():
                    async with self._lock:
                        self._slots = slots
                        self._current_index = 0
                loop.create_task(_swap())
            except RuntimeError:
                self._slots = slots
                self._current_index = 0
        if slots:
            logger.info("Reloaded %d credential slot(s)", len(slots))
        else:
            logger.warning("reload_keys: all credentials removed — bot will reject requests")

    @property
    def is_at_capacity(self) -> bool:
        return self._semaphore.locked()

    @property
    def key_count(self) -> int:
        return len(self._slots)

    def get_slots_status(self) -> list[dict]:
        """Return status info for each credential slot (for admin panel display)."""
        from bot import api_keys_store
        now = time.monotonic()
        result = []
        for slot in self._slots:
            remaining = max(0.0, slot.cooldown_until - now)
            if slot.auth_error:
                status = "auth_error"
            elif slot.active_requests > 0:
                status = "active"
            elif remaining > 0:
                status = "cooldown"
            else:
                status = "ok"
            last_used_ago = int(now - slot.last_used_at) if slot.last_used_at > 0 else None
            key_masked = api_keys_store.mask_key(slot._api_key) if isinstance(slot, _ApiKeySlot) else None
            sa_name = slot.sa_path.stem if isinstance(slot, _CredSlot) else None
            result.append({
                "label": slot.label,
                "key_masked": key_masked,
                "sa_name": sa_name,
                "type": "api_key" if isinstance(slot, _ApiKeySlot) else "service_account",
                "status": status,
                "cooldown_remaining": int(remaining),
                "auth_error_msg": slot.auth_error_msg,
                "active_requests": slot.active_requests,
                "last_used_ago": last_used_ago,
                "last_model": slot.last_model,
                "total_ok": slot.total_ok,
                "total_err": slot.total_err,
                "req_flash": slot.requests_in_window_family("flash-image"),
                "req_pro": slot.requests_in_window_family("pro-image"),
                "qpm_flash": _qpm_for_model("flash-image"),
                "qpm_pro": _qpm_for_model("pro-image"),
            })
        return result

    def get_slot_history(self, slot_index: int) -> list[dict]:
        if 0 <= slot_index < len(self._slots):
            return list(self._slots[slot_index].history)
        return []

    def _get_next_available_slot(self, model: str) -> _BaseSlot | None:
        """Return the next ready slot using round-robin rotation.

        After each use the pointer advances so every key gets equal traffic.
        'Ready' means: past cooldown_until AND has_capacity for this specific model
        AND no permanent auth error.
        """
        usable = [s for s in self._slots if not s.auth_error]
        if not usable:
            return None
        n = len(usable)
        for i in range(n):
            idx = (self._current_index + i) % n
            slot = usable[idx]
            if slot.is_ready(model):
                self._current_index = (idx + 1) % n
                return slot
        return None

    def _earliest_ready_at(self, model: str) -> float:
        """Monotonic timestamp when any slot will next be ready for model."""
        # Only consider slots without permanent auth errors
        usable = [s for s in self._slots if not s.auth_error]
        if not usable:
            return float("inf")
        return min(s.ready_at(model) for s in usable)

    def _check_and_alert_auth_errors(self) -> None:
        from bot import admin_alerts
        auth_err_slots = [s for s in self._slots if s.auth_error]
        total = len(self._slots)
        if not total:
            return
        err_details = [f"{s.label}: {s.auth_error_msg}" for s in auth_err_slots]
        if len(auth_err_slots) == total:
            _fire_alert(admin_alerts.alert_all_keys_auth_error(total, err_details))
        elif len(auth_err_slots) >= 1:
            _fire_alert(admin_alerts.alert_keys_degraded(total, len(auth_err_slots), err_details))

    def _alert_quota_exhausted(self, model: str) -> None:
        from bot import admin_alerts
        auth_err_count = sum(1 for s in self._slots if s.auth_error)
        total = len(self._slots)
        if auth_err_count == total:
            err_details = [f"{s.label}: {s.auth_error_msg}" for s in self._slots if s.auth_error]
            _fire_alert(admin_alerts.alert_all_keys_auth_error(total, err_details))
        else:
            _fire_alert(admin_alerts.alert_all_keys_quota(total, model))

    async def generate_image(
        self,
        prompt: str,
        images: list[bytes] | None = None,
        model_override: str | None = None,
        aspect_ratio: str = "1:1",
        thinking_level: str = "low",
        user_id: int | None = None,
        username: str = "",
    ) -> bytes:
        model = model_override or self._settings.vertex_ai_model
        return await self._try_all_keys(prompt, images, model, aspect_ratio, thinking_level, user_id=user_id, username=username)

    async def _try_all_keys(
        self,
        prompt: str,
        images: list[bytes] | None,
        model: str,
        aspect_ratio: str,
        thinking_level: str = "low",
        user_id: int | None = None,
        username: str = "",
    ) -> bytes:
        """Dispatch the request to the best available key, queuing if all are busy.

        Strategy
        --------
        * Proactive: each slot tracks its own sliding-window usage (5 req / 60 s).
          We never send a request to a slot that is already at capacity.
        * Reactive safety net: if a 429 slips through anyway, the slot is locked
          for the full 60-second window.
        * Waiting: when all slots are at capacity we sleep until the soonest slot
          becomes ready again, then retry.  Maximum total wait: 5 minutes.
        * Ambiguous prompt: if the model returns text instead of an image we retry
          once with an explicit image instruction before giving up.
        """
        text_retry_done = False
        current_prompt = prompt
        deadline = time.monotonic() + 120  # 2-minute absolute deadline

        while time.monotonic() < deadline:
            async with self._lock:
                slot = self._get_next_available_slot(model)

            if slot is not None:
                slot.record_request(model)
                slot.active_requests += 1
                slot.last_used_at = time.monotonic()
                slot.last_model = model
                _exc_to_raise: Exception | None = None
                _t0 = time.monotonic()
                try:
                    logger.info(
                        "Trying '%s' [%d/%d used for %s], prompt='%s'",
                        slot.label,
                        slot.requests_in_window(model),
                        _qpm_for_model(model),
                        model,
                        current_prompt[:60],
                    )
                    result = await asyncio.wait_for(
                        self._call_api(slot, current_prompt, images, model, aspect_ratio, thinking_level),
                        timeout=90,
                    )
                    slot.total_ok += 1
                    slot.record_history(
                        user_id=user_id, username=username, prompt=prompt,
                        model=model, status="ok",
                        duration_ms=int((time.monotonic() - _t0) * 1000),
                    )
                    return result
                except asyncio.TimeoutError:
                    slot.total_err += 1
                    slot.mark_rate_limited()
                    slot.record_history(
                        user_id=user_id, username=username, prompt=prompt,
                        model=model, status="timeout", error="90s timeout",
                        duration_ms=int((time.monotonic() - _t0) * 1000),
                    )
                    logger.warning(
                        "Slot '%s' timed out (90s) for '%s', rotating to next key...",
                        slot.label, current_prompt[:60],
                    )
                except Exception as exc:
                    slot.total_err += 1
                    _dur = int((time.monotonic() - _t0) * 1000)
                    logger.error(
                        "Slot '%s' error for '%s': %s",
                        slot.label, current_prompt[:60], repr(exc),
                    )
                    if _is_safety_error(exc):
                        slot.record_history(
                            user_id=user_id, username=username, prompt=prompt,
                            model=model, status="safety", error=str(exc)[:200],
                            duration_ms=_dur,
                        )
                        _exc_to_raise = SafetyFilterError(str(exc))
                    elif _is_retryable(exc):
                        slot.record_history(
                            user_id=user_id, username=username, prompt=prompt,
                            model=model, status="rate_limit", error="429",
                            duration_ms=_dur,
                        )
                        slot.mark_rate_limited()
                        logger.warning(
                            "Slot '%s' returned 429 — 60s cooldown applied, rotating...",
                            slot.label,
                        )
                    elif _is_auth_error(exc):
                        slot.record_history(
                            user_id=user_id, username=username, prompt=prompt,
                            model=model, status="auth_error", error=str(exc)[:200],
                            duration_ms=_dur,
                        )
                        slot.reset_client()
                        slot.auth_error = True
                        slot.auth_error_msg = str(exc)[:120]
                        logger.warning(
                            "Slot '%s' auth error, key invalid — skipping: %s",
                            slot.label, exc,
                        )
                        self._check_and_alert_auth_errors()
                    elif _is_model_error(exc):
                        slot.record_history(
                            user_id=user_id, username=username, prompt=prompt,
                            model=model, status="error", error=str(exc)[:200],
                            duration_ms=_dur,
                        )
                        slot.cooldown_until = time.monotonic() + 300
                        logger.warning(
                            "Slot '%s' returned 400 INVALID_ARGUMENT — 5min cooldown applied, rotating...",
                            slot.label,
                        )
                    elif isinstance(exc, GenerationError) and "вернула текст" in str(exc):
                        if not text_retry_done:
                            text_retry_done = True
                            slot.record_history(
                                user_id=user_id, username=username, prompt=prompt,
                                model=model, status="text_retry", error="модель вернула текст",
                                duration_ms=_dur,
                            )
                            current_prompt = (
                                f"Generate a high-quality image of: {prompt}. "
                                "Important: output must be an IMAGE, not text."
                            )
                            logger.info(
                                "Model returned text for '%s', retrying with enhanced prompt...",
                                prompt[:40],
                            )
                        else:
                            slot.record_history(
                                user_id=user_id, username=username, prompt=prompt,
                                model=model, status="error", error=str(exc)[:200],
                                duration_ms=_dur,
                            )
                            _exc_to_raise = AmbiguousPromptError(str(exc))
                    else:
                        slot.record_history(
                            user_id=user_id, username=username, prompt=prompt,
                            model=model, status="error", error=str(exc)[:200],
                            duration_ms=_dur,
                        )
                        _exc_to_raise = GenerationError(str(exc))
                finally:
                    slot.active_requests = max(0, slot.active_requests - 1)
                if _exc_to_raise is not None:
                    raise _exc_to_raise
            else:
                # All slots are at capacity or in cooldown — wait precisely.
                earliest = self._earliest_ready_at(model)
                now = time.monotonic()
                wait = max(0.1, earliest - now)
                if now + wait > deadline:
                    break
                logger.info(
                    "All %d slot(s) at capacity for %s; waiting %.1fs for next available...",
                    len(self._slots), model, wait,
                )
                await asyncio.sleep(wait + 0.1)

        logger.error("Deadline reached — all credential slots busy for model %s", model)
        self._alert_quota_exhausted(model)
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

    CHAT_MODEL = "gemini-3.1-pro-preview"
    SEARCH_MODEL = "gemini-3.1-flash-lite-preview"

    async def chat_text(
        self,
        contents: list[Any],
        model_override: str | None = None,
        on_thought: Any = None,
        use_search: bool = False,
    ) -> str:
        """Send a chat request with the same key-rotation and wait logic as image generation.

        on_thought:  optional callable(str) — called for each thinking chunk.
        use_search:  enable Google Search grounding (only when actually needed for trends).
        """
        model = model_override or self.CHAT_MODEL
        deadline = time.monotonic() + 300  # 5-minute absolute deadline

        while time.monotonic() < deadline:
            async with self._lock:
                slot = self._get_next_available_slot(model)

            if slot is not None:
                slot.record_request(model)
                slot.active_requests += 1
                slot.last_used_at = time.monotonic()
                slot.last_model = model
                _chat_exc: Exception | None = None
                try:
                    logger.info(
                        "Chat: trying '%s' [%d/%d used for %s] search=%s",
                        slot.label,
                        slot.requests_in_window(model),
                        _qpm_for_model(model),
                        model,
                        use_search,
                    )
                    loop = asyncio.get_running_loop()
                    import functools
                    result = await loop.run_in_executor(
                        None, functools.partial(self._sync_chat, slot, contents, model, on_thought, use_search)
                    )
                    slot.total_ok += 1
                    return result
                except Exception as exc:
                    slot.total_err += 1
                    logger.error("Chat: slot '%s' error: %s", slot.label, repr(exc))
                    if _is_retryable(exc):
                        slot.mark_rate_limited()
                        logger.warning(
                            "Chat: slot '%s' returned 429 — 60s cooldown, rotating...",
                            slot.label,
                        )
                    elif _is_auth_error(exc):
                        slot.reset_client()
                        slot.auth_error = True
                        slot.auth_error_msg = str(exc)[:120]
                        logger.warning(
                            "Chat: slot '%s' auth error, key invalid — skipping: %s",
                            slot.label, exc,
                        )
                        self._check_and_alert_auth_errors()
                    elif _is_model_error(exc):
                        slot.cooldown_until = time.monotonic() + 300
                        logger.warning(
                            "Chat: slot '%s' returned 400 INVALID_ARGUMENT — 5min cooldown applied, rotating...",
                            slot.label,
                        )
                    else:
                        _chat_exc = GenerationError(str(exc))
                finally:
                    slot.active_requests = max(0, slot.active_requests - 1)
                if _chat_exc is not None:
                    raise _chat_exc
            else:
                earliest = self._earliest_ready_at(model)
                now = time.monotonic()
                wait = max(0.1, earliest - now)
                if now + wait > deadline:
                    break
                logger.info(
                    "Chat: all %d slot(s) at capacity for %s; waiting %.1fs...",
                    len(self._slots), model, wait,
                )
                await asyncio.sleep(wait + 0.1)

        logger.error("Chat: deadline reached — all slots busy for %s", model)
        self._alert_quota_exhausted(model)
        raise QuotaExceededError()

    def _sync_chat(
        self,
        slot: _BaseSlot,
        contents: list[Any],
        model: str | None = None,
        on_thought: Any = None,
        use_search: bool = False,
    ) -> str:
        from google.genai import types as genai_types

        client = slot.get_client()
        use_model = model or self.CHAT_MODEL
        m_lower = use_model.lower()

        config_kwargs: dict[str, Any] = {
            "temperature": 1,
            "top_p": 0.95,
            "safety_settings": _get_safety_settings(),
        }

        # Thinking: only for full Flash or Pro models — NOT for Lite variants
        # (Lite models don't support thinking_config; sending it causes 400 INVALID_ARGUMENT)
        supports_thinking = ("flash" in m_lower and "lite" not in m_lower) or "pro" in m_lower
        if supports_thinking:
            config_kwargs["thinking_config"] = genai_types.ThinkingConfig(thinking_budget=8192)

        # Google Search: only when caller explicitly requests it (trend search)
        # Enabling it unconditionally wastes quota and may cause 400 on unsupported models
        if use_search:
            config_kwargs["tools"] = [genai_types.Tool(google_search=genai_types.GoogleSearch())]

        config = genai_types.GenerateContentConfig(**config_kwargs)

        text_parts: list[str] = []
        for chunk in client.models.generate_content_stream(
            model=use_model,
            contents=contents,
            config=config,
        ):
            if not chunk.candidates:
                continue
            content = chunk.candidates[0].content
            if content is None:
                continue
            for part in (content.parts or []):
                txt = getattr(part, "text", None)
                if not txt:
                    continue
                if getattr(part, "thought", False):
                    if on_thought is not None:
                        try:
                            on_thought(txt)
                        except Exception:
                            pass
                else:
                    text_parts.append(txt)

        return "".join(text_parts) if text_parts else ""
