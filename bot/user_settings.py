"""
bot/user_settings.py
~~~~~~~~~~~~~~~~~~~~~
Per-user settings storage.

If DATABASE_URL is set — stores in PostgreSQL (via bot.db).
Otherwise — falls back to a local JSON file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Any

import bot.db as _db

logger = logging.getLogger(__name__)

SETTINGS_FILE = Path(os.getenv("SETTINGS_FILE", "telegram-bot/data/user_settings.json"))

FREE_CREDITS = 5

_PERSISTENT_KEYS = {
    "model", "send_mode", "resolution", "aspect_ratio", "thinking_level",
    "first_name", "generations_count", "platform",
    "credits", "blocked",
    "video_duration", "video_resolution", "video_aspect_ratio",
    "video_audio", "video_task",
    "chat_model",
}

user_settings: dict[int, dict[str, Any]] = {}

active_tasks: dict[int, asyncio.Task] = {}

# ── Credit reservation (freeze) ───────────────────────────────────────────────
# Maps user_id → number of credits currently frozen (reserved but not yet spent).
# Purpose: prevent parallel requests from passing has_credits() before the first
# one actually deducts. On success → confirm_credits() deducts + unfreezes.
# On error/cancel → release_credits() unfreezes without deducting.
_reserved_credits: dict[int, int] = {}


def reserve_credits(user_id: int, amount: int) -> bool:
    """Freeze `amount` credits for user_id.

    Returns True and freezes the credits if
    (current_balance - already_frozen) >= amount.
    Returns False if the user cannot afford this reservation.
    """
    s = get_user_settings(user_id)
    balance = s.get("credits", FREE_CREDITS)
    already_frozen = _reserved_credits.get(user_id, 0)
    available = balance - already_frozen
    if available < amount:
        return False
    _reserved_credits[user_id] = already_frozen + amount
    return True


def release_credits(user_id: int, amount: int) -> None:
    """Unfreeze `amount` credits without spending them (called on error/cancel)."""
    frozen = _reserved_credits.get(user_id, 0)
    new_frozen = max(0, frozen - amount)
    if new_frozen == 0:
        _reserved_credits.pop(user_id, None)
    else:
        _reserved_credits[user_id] = new_frozen


def confirm_credits(
    user_id: int,
    amount: int,
    first_name: str = "",
    platform: str = "tg",
    prompt: str = "",
    model: str = "",
    gen_type: str = "image",
) -> None:
    """Unfreeze `amount` and actually deduct them (called on successful generation)."""
    release_credits(user_id, amount)
    increment_generations(
        user_id, first_name,
        platform=platform,
        credits_cost=amount,
        prompt=prompt,
        model=model,
        gen_type=gen_type,
    )


def set_active_task(user_id: int, task: asyncio.Task) -> None:
    active_tasks[user_id] = task


def cancel_active_task(user_id: int) -> bool:
    task = active_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()
        return True
    return False


def clear_active_task(user_id: int) -> None:
    active_tasks.pop(user_id, None)

AVAILABLE_MODELS: dict[str, dict[str, Any]] = {
    "gemini-3.1-flash-image-preview": {
        "label": "⚡ Gemini 3.1 Flash Image",
        "desc": "Быстрая генерация, баланс цены и качества",
        "type": "image",
    },
    "gemini-3-pro-image-preview": {
        "label": "🎯 Gemini 3 Pro Image",
        "desc": "Лучшее качество, сложные задачи",
        "type": "image",
    },
    "veo-3.1-generate-001": {
        "label": "🎬 Veo 3.1 (Видео)",
        "desc": "Макс. качество, текст/фото→видео, продление, 4K",
        "type": "video",
        "credits": 5,
        "supports_audio": True,
        "supports_image": True,
        "supports_video_extension": True,
        "supports_4k": True,
    },
    "veo-3.1-fast-generate-001": {
        "label": "⚡ Veo 3.1 Fast (Видео)",
        "desc": "Быстрая, текст/фото→видео, продление, 4K",
        "type": "video",
        "credits": 3,
        "supports_audio": True,
        "supports_image": True,
        "supports_video_extension": True,
        "supports_4k": True,
    },
    "veo-3.1-lite-generate-001": {
        "label": "💡 Veo 3.1 Lite (Видео)",
        "desc": "Экономичная, аудио, фото→видео, продление",
        "type": "video",
        "credits": 2,
        "supports_audio": True,
        "supports_image": True,
        "supports_video_extension": True,
        "supports_4k": False,
    },
    "lyria-3-pro-preview": {
        "label": "🎼 Lyria 3 Pro (Музыка)",
        "desc": "Полная песня до 3 минут, текст или фото",
        "type": "music",
        "credits": 4,
        "google_price_usd": 0.08,
        "duration_label": "до 3 минут",
        "supports_image": True,
    },
    "lyria-3-clip-preview": {
        "label": "🎵 Lyria 3 (Музыка)",
        "desc": "30-секундный музыкальный клип, текст или фото",
        "type": "music",
        "credits": 2,
        "google_price_usd": 0.04,
        "duration_label": "30 секунд",
        "supports_image": True,
    },
}

VIDEO_DURATIONS: dict[int, dict[str, str]] = {
    4: {"label": "⏱ 4 секунды", "desc": "Короткий клип"},
    6: {"label": "⏱ 6 секунд", "desc": "Средний клип"},
    8: {"label": "⏱ 8 секунд", "desc": "Максимальный клип"},
}

VIDEO_RESOLUTIONS: dict[str, dict[str, str]] = {
    "720p": {"label": "📺 720p (HD)", "desc": "Стандартное качество"},
    "1080p": {"label": "🖥 1080p (Full HD)", "desc": "Высокое качество"},
    "4k": {"label": "📽 4K (Ultra HD)", "desc": "Максимальное качество (Preview)"},
}

VIDEO_ASPECT_RATIOS: dict[str, str] = {
    "16:9": "16:9 (Горизонтальный)",
    "9:16": "9:16 (Вертикальный)",
}

VIDEO_TASKS: dict[str, dict[str, Any]] = {
    "text-to-video": {
        "label": "📝 Text-to-video",
        "desc": "Генерация видео по текстовому описанию",
        "input": "text",
    },
    "image-to-video": {
        "label": "🖼 Image-to-video",
        "desc": "Генерация видео по изображению + текст",
        "input": "image",
        "requires_image_support": True,
    },
    "video-extension": {
        "label": "🔄 Video extension",
        "desc": "Продление существующего видео",
        "input": "video",
        "requires_video_extension_support": True,
    },
    "ref-subject": {
        "label": "👤 Reference-to-video (Subject)",
        "desc": "Видео с сохранением субъекта из фото",
        "input": "image",
        "coming_soon": True,
    },
    "ref-style": {
        "label": "🎨 Reference-to-video (Style)",
        "desc": "Видео в стиле референсного изображения",
        "input": "image",
        "coming_soon": True,
    },
    "inpaint-insert": {
        "label": "✏️ Video inpaint (Insert)",
        "desc": "Вставка объекта в видео",
        "input": "video",
        "coming_soon": True,
    },
    "inpaint-remove": {
        "label": "🗑 Video inpaint (Remove)",
        "desc": "Удаление объекта из видео",
        "input": "video",
        "coming_soon": True,
    },
}


def video_supports_video_extension(model_id: str) -> bool:
    info = AVAILABLE_MODELS.get(model_id, {})
    return bool(info.get("supports_video_extension", False))


def get_available_tasks_for_model(model_id: str) -> dict[str, dict[str, Any]]:
    has_image = video_supports_image(model_id)
    has_ext = video_supports_video_extension(model_id)
    result = {}
    for tid, tinfo in VIDEO_TASKS.items():
        if tinfo.get("requires_image_support") and not has_image:
            continue
        if tinfo.get("requires_video_extension_support") and not has_ext:
            continue
        result[tid] = tinfo
    return result


def is_video_model(model_id: str) -> bool:
    info = AVAILABLE_MODELS.get(model_id, {})
    return info.get("type") == "video"


def is_music_model(model_id: str) -> bool:
    info = AVAILABLE_MODELS.get(model_id, {})
    return info.get("type") == "music"


# Google Vertex/Gemini API pricing for Veo 3.1 (USD per second)
# audio_premium is added on top of video when generate_audio=True
VIDEO_PRICE_PER_SEC: dict[str, dict[str, float]] = {
    "veo-3.1-generate-001":      {"video": 0.20, "audio_premium": 0.20},
    "veo-3.1-fast-generate-001": {"video": 0.10, "audio_premium": 0.05},
    "veo-3.1-lite-generate-001": {"video": 0.05, "audio_premium": 0.03},
}

# Our credit price: 30 credits = $1.40 → 1 credit ≈ $0.04667
# Our user-facing price target: 3× cheaper than Google
CREDIT_USD = 1.40 / 30
PRICE_MARKDOWN = 3.0


def calc_video_credits(model_id: str, duration_seconds: int = 8, audio: bool = False) -> int:
    """Calculate credits to charge for one video generation call.

    Cost depends on model, duration (seconds), and whether audio is generated.
    Mirrors Google's per-second billing scaled by PRICE_MARKDOWN and converted to credits.
    """
    import math
    pricing = VIDEO_PRICE_PER_SEC.get(model_id)
    if pricing is None:
        info = AVAILABLE_MODELS.get(model_id, {})
        return int(info.get("credits", 3))
    if duration_seconds not in (4, 6, 8):
        duration_seconds = 8
    has_audio = bool(audio) and video_supports_audio(model_id)
    google_usd = (pricing["video"] + (pricing["audio_premium"] if has_audio else 0)) * duration_seconds
    user_usd = google_usd / PRICE_MARKDOWN
    credits = math.ceil(user_usd / CREDIT_USD)
    return max(1, credits)


def get_video_credits_cost(model_id: str, duration_seconds: int = 8, audio: bool = True) -> int:
    """Backward-compatible wrapper. Defaults to worst-case (8s with audio)."""
    return calc_video_credits(model_id, duration_seconds=duration_seconds, audio=audio)


def get_music_credits_cost(model_id: str) -> int:
    info = AVAILABLE_MODELS.get(model_id, {})
    return info.get("credits", 2)


def music_supports_image(model_id: str) -> bool:
    info = AVAILABLE_MODELS.get(model_id, {})
    return bool(info.get("supports_image", True))


def video_supports_audio(model_id: str) -> bool:
    info = AVAILABLE_MODELS.get(model_id, {})
    return bool(info.get("supports_audio", False))


def video_supports_image(model_id: str) -> bool:
    info = AVAILABLE_MODELS.get(model_id, {})
    return bool(info.get("supports_image", False))


def video_supports_4k(model_id: str) -> bool:
    info = AVAILABLE_MODELS.get(model_id, {})
    return bool(info.get("supports_4k", False))


def get_video_resolutions_for_model(model_id: str) -> dict[str, dict[str, str]]:
    if video_supports_4k(model_id):
        return VIDEO_RESOLUTIONS
    return {k: v for k, v in VIDEO_RESOLUTIONS.items() if k != "4k"}

RESOLUTIONS: dict[str, dict[str, Any]] = {
    "original": {
        "label": "📷 Оригинал",
        "desc": "Без изменений, как выдаёт модель",
        "max_side": 0,
    },
    "1080p": {
        "label": "🖥 1080p (Full HD)",
        "desc": "Макс. сторона 1920 пикселей",
        "max_side": 1920,
    },
    "2k": {
        "label": "🖥 2K (QHD)",
        "desc": "Макс. сторона 2560 пикселей",
        "max_side": 2560,
    },
    "4k": {
        "label": "🖥 4K (Ultra HD)",
        "desc": "Макс. сторона 3840 пикселей",
        "max_side": 3840,
    },
}

SEND_MODES: dict[str, dict[str, str]] = {
    "photo": {
        "label": "🖼 Фото (сжатое)",
        "desc": "Быстрый просмотр, Telegram сжимает изображение",
    },
    "document": {
        "label": "📄 Файл (оригинал)",
        "desc": "Без сжатия, полное качество PNG",
    },
}

THINKING_LEVELS: dict[str, dict[str, str]] = {
    "none": {
        "label": "⚡ Без размышлений",
        "desc": "Самый быстрый — модель отвечает сразу",
    },
    "low": {
        "label": "💭 Лёгкий",
        "desc": "Быстрая генерация с минимальным анализом",
    },
    "medium": {
        "label": "🧠 Средний",
        "desc": "Баланс скорости и качества",
    },
    "high": {
        "label": "🔬 Глубокий",
        "desc": "Максимальное качество, больше времени",
    },
}

CHAT_MODELS: dict[str, dict[str, Any]] = {
    "gemini-3.1-pro": {
        "label": "💎 Gemini 3.1 Pro",
        "short": "Gemini 3.1 Pro",
        "desc": "Мультимодальный: текст, фото, видео, аудио, PDF",
        "backend": "gemini",
        "model_id": "gemini-3.1-pro-preview",
    },
    "grok-4.20-reasoning": {
        "label": "🧠 Grok 4.20 (Reasoning)",
        "short": "Grok 4.20",
        "desc": "С поиском в интернете, рассуждения, текст и фото",
        "backend": "grok",
        "model_id": "xai/grok-4.20-reasoning",
    },
}

DEFAULT_CHAT_MODEL = "gemini-3.1-pro"


def get_chat_model(user_id: int) -> str:
    cm = get_user_settings(user_id).get("chat_model", DEFAULT_CHAT_MODEL)
    if cm not in CHAT_MODELS:
        cm = DEFAULT_CHAT_MODEL
    return cm


def set_chat_model(user_id: int, chat_model: str) -> bool:
    if chat_model not in CHAT_MODELS:
        return False
    s = get_user_settings(user_id)
    s["chat_model"] = chat_model
    _save_user(user_id)
    return True


DEFAULT_SETTINGS: dict[str, Any] = {
    "model": "gemini-3.1-flash-image-preview",
    "send_mode": "photo",
    "resolution": "original",
    "aspect_ratio": "1:1",
    "thinking_level": "low",
    "first_name": "",
    "generations_count": 0,
    "platform": "",
    "last_menu_message_id": None,
    "last_menu_chat_id": None,
    "credits": FREE_CREDITS,
    "blocked": False,
    "video_duration": 8,
    "video_resolution": "720p",
    "video_aspect_ratio": "16:9",
    "video_audio": True,
    "video_task": "text-to-video",
    "chat_model": DEFAULT_CHAT_MODEL,
}


def _save_to_disk() -> None:
    """Save all users — used only at startup migration. Prefer _save_user() for single updates."""
    snapshot: dict[int, dict[str, Any]] = {
        uid: {k: v for k, v in s.items() if k in _PERSISTENT_KEYS}
        for uid, s in user_settings.items()
    }
    if _db.is_available():
        _db.save_all_users(snapshot)
        return
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        str_snapshot = {str(uid): data for uid, data in snapshot.items()}
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(str_snapshot, ensure_ascii=False, indent=2))
        tmp.replace(SETTINGS_FILE)
        logger.info("Saved %d users to %s (size=%d bytes)",
                    len(snapshot), SETTINGS_FILE, SETTINGS_FILE.stat().st_size)
    except Exception:
        logger.exception("Failed to save user settings to %s", SETTINGS_FILE)


def _save_user(user_id: int) -> None:
    """Save a single user — thread-safe, much faster than _save_to_disk()."""
    s = user_settings.get(user_id)
    if s is None:
        return
    data = {k: v for k, v in s.items() if k in _PERSISTENT_KEYS}
    if _db.is_available():
        _db.save_one_user(user_id, data)
        return
    # Fallback: full file save for non-DB mode
    _save_to_disk()


def _check_storage() -> None:
    """Log diagnostic info about storage at startup."""
    env_val = os.getenv("SETTINGS_FILE", "(not set)")
    logger.info("SETTINGS_FILE env var: %s", env_val)
    logger.info("Resolved SETTINGS_FILE path: %s", SETTINGS_FILE.resolve())
    parent = SETTINGS_FILE.parent
    parent.mkdir(parents=True, exist_ok=True)
    # List all files in the storage directory
    try:
        files = list(parent.iterdir())
        logger.info("Contents of %s: %s", parent, [f.name for f in files])
    except Exception as e:
        logger.error("Cannot list %s: %s", parent, e)
    if SETTINGS_FILE.exists():
        size = SETTINGS_FILE.stat().st_size
        logger.info("Settings file EXISTS, size=%d bytes", size)
    else:
        logger.info("Settings file does NOT exist yet (will be created on first save)")
    test_file = parent / ".write_test"
    try:
        test_file.write_text("ok")
        test_file.unlink()
        logger.info("Storage directory %s is WRITABLE", parent)
    except Exception as e:
        logger.error("Storage directory %s is NOT writable: %s", parent, e)


def _merge_saved(saved: dict[str, Any]) -> dict[str, Any]:
    merged = {**DEFAULT_SETTINGS}
    for k in _PERSISTENT_KEYS:
        if k in saved:
            merged[k] = saved[k]
    return merged


def load_settings() -> None:
    if _db.is_available():
        _db.init_tables()
        raw = _db.load_all_users()
        migrated = 0
        for uid, saved in raw.items():
            merged = _merge_saved(saved)
            if "credits" not in saved:
                gens = merged.get("generations_count", 0)
                merged["credits"] = max(0, FREE_CREDITS - gens)
                migrated += 1
            user_settings[uid] = merged
        logger.info("Loaded %d users from PostgreSQL", len(raw))
        if migrated:
            logger.info("Migrated credits for %d existing users", migrated)
            _save_to_disk()
        return

    _check_storage()
    if not SETTINGS_FILE.exists():
        logger.info("No saved settings file found at %s — starting fresh", SETTINGS_FILE)
        return
    try:
        raw_file = json.loads(SETTINGS_FILE.read_text())
        count = 0
        migrated = 0
        for uid_str, saved in raw_file.items():
            uid = int(uid_str)
            merged = _merge_saved(saved)
            if "credits" not in saved:
                gens = merged.get("generations_count", 0)
                merged["credits"] = max(0, FREE_CREDITS - gens)
                migrated += 1
            user_settings[uid] = merged
            count += 1
        logger.info("Loaded settings for %d users from %s", count, SETTINGS_FILE)
        if migrated:
            logger.info("Migrated credits for %d existing users", migrated)
            _save_to_disk()
    except Exception:
        logger.exception("Failed to load user settings from %s", SETTINGS_FILE)


def get_user_settings(user_id: int) -> dict[str, Any]:
    if user_id not in user_settings:
        user_settings[user_id] = {**DEFAULT_SETTINGS}
    return user_settings[user_id]


def save_user_settings(user_id: int) -> None:
    _save_user(user_id)


def increment_generations(
    user_id: int,
    first_name: str = "",
    platform: str = "",
    credits_cost: int = 1,
    prompt: str = "",
    model: str = "",
    gen_type: str = "",
) -> int:
    s = get_user_settings(user_id)
    s["generations_count"] = s.get("generations_count", 0) + 1
    current_credits = s.get("credits", FREE_CREDITS)
    new_credits = max(0, current_credits - credits_cost)
    s["credits"] = new_credits
    if first_name:
        s["first_name"] = first_name
    if platform and not s.get("platform"):
        s["platform"] = platform
    _save_user(user_id)
    try:
        import bot.db as _db
        _db.save_credit_log(
            user_id=user_id,
            change_type="spend",
            credits_change=-credits_cost,
            balance_after=new_credits,
            model=model,
            gen_type=gen_type,
            prompt=prompt,
            platform=platform or s.get("platform", ""),
        )
    except Exception:
        pass
    return s["generations_count"]


def is_blocked(user_id: int) -> bool:
    return bool(get_user_settings(user_id).get("blocked", False))


def has_credits(user_id: int, required: int = 1) -> bool:
    return get_user_settings(user_id).get("credits", FREE_CREDITS) >= required


def add_credits(user_id: int, amount: int, note: str = "admin") -> int:
    s = get_user_settings(user_id)
    old = s.get("credits", 0)
    s["credits"] = old + amount
    _save_user(user_id)
    try:
        import bot.db as _db
        _db.save_credit_log(
            user_id=user_id,
            change_type="topup",
            credits_change=amount,
            balance_after=s["credits"],
            note=note,
        )
    except Exception:
        pass
    return s["credits"]


def set_credits(user_id: int, amount: int, note: str = "admin") -> int:
    s = get_user_settings(user_id)
    old = s.get("credits", 0)
    s["credits"] = max(0, int(amount))
    _save_user(user_id)
    try:
        import bot.db as _db
        _db.save_credit_log(
            user_id=user_id,
            change_type="set",
            credits_change=s["credits"] - old,
            balance_after=s["credits"],
            note=note,
        )
    except Exception:
        pass
    return s["credits"]


def reset_generations(user_id: int) -> None:
    s = get_user_settings(user_id)
    s["generations_count"] = 0
    _save_user(user_id)


def delete_user(user_id: int) -> bool:
    existed = user_id in user_settings
    user_settings.pop(user_id, None)
    # Remove from DB directly — faster and correct with multiple replicas
    _db.delete_one_user(user_id)
    return existed


def set_blocked(user_id: int, blocked: bool) -> None:
    s = get_user_settings(user_id)
    s["blocked"] = blocked
    _save_user(user_id)


def set_last_menu(user_id: int, chat_id: int, message_id: int) -> None:
    s = get_user_settings(user_id)
    s["last_menu_chat_id"] = chat_id
    s["last_menu_message_id"] = message_id


def pop_last_menu(user_id: int) -> tuple[int, int] | None:
    s = get_user_settings(user_id)
    chat_id = s.get("last_menu_chat_id")
    msg_id = s.get("last_menu_message_id")
    if chat_id and msg_id:
        s["last_menu_chat_id"] = None
        s["last_menu_message_id"] = None
        return (chat_id, msg_id)
    return None


# ── Chat daily request limits ─────────────────────────────────────────────────
# uid → (count_today, date_of_count)
_chat_daily: dict[int, tuple[int, date]] = {}

CHAT_MAX_PER_DAY = 500


def get_chat_daily_limit(user_id: int) -> int:
    """Daily chat request limit = min(user credits, CHAT_MAX_PER_DAY)."""
    credits = get_user_settings(user_id).get("credits", FREE_CREDITS)
    return min(credits, CHAT_MAX_PER_DAY)


def get_chat_daily_count(user_id: int) -> int:
    """Return how many chat requests the user has made today."""
    entry = _chat_daily.get(user_id)
    if entry is None or entry[1] != date.today():
        return 0
    return entry[0]


def has_chat_quota(user_id: int) -> bool:
    """True if the user can still send a chat message today."""
    return get_chat_daily_count(user_id) < get_chat_daily_limit(user_id)


def increment_chat_count(user_id: int) -> int:
    """Record one chat request for today and return the new count."""
    today = date.today()
    entry = _chat_daily.get(user_id)
    if entry is None or entry[1] != today:
        count = 1
    else:
        count = entry[0] + 1
    _chat_daily[user_id] = (count, today)
    return count
