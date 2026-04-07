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
from pathlib import Path
from typing import Any

import bot.db as _db

logger = logging.getLogger(__name__)

SETTINGS_FILE = Path(os.getenv("SETTINGS_FILE", "telegram-bot/data/user_settings.json"))

FREE_CREDITS = 20

_PERSISTENT_KEYS = {
    "model", "send_mode", "resolution", "aspect_ratio", "thinking_level",
    "first_name", "generations_count", "platform",
    "credits", "blocked",
}

user_settings: dict[int, dict[str, Any]] = {}

active_tasks: dict[int, asyncio.Task] = {}


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
    },
    "gemini-3-pro-image-preview": {
        "label": "🎯 Gemini 3 Pro Image",
        "desc": "Лучшее качество, сложные задачи",
    },
}

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
}


def _save_to_disk() -> None:
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
    _save_to_disk()


def increment_generations(
    user_id: int,
    first_name: str = "",
    platform: str = "",
    credits_cost: int = 1,
) -> int:
    s = get_user_settings(user_id)
    s["generations_count"] = s.get("generations_count", 0) + 1
    current_credits = s.get("credits", FREE_CREDITS)
    s["credits"] = max(0, current_credits - credits_cost)
    if first_name:
        s["first_name"] = first_name
    if platform and not s.get("platform"):
        s["platform"] = platform
    _save_to_disk()
    return s["generations_count"]


def is_blocked(user_id: int) -> bool:
    return bool(get_user_settings(user_id).get("blocked", False))


def has_credits(user_id: int, required: int = 1) -> bool:
    return get_user_settings(user_id).get("credits", FREE_CREDITS) >= required


def add_credits(user_id: int, amount: int) -> int:
    s = get_user_settings(user_id)
    s["credits"] = s.get("credits", 0) + amount
    _save_to_disk()
    return s["credits"]


def set_blocked(user_id: int, blocked: bool) -> None:
    s = get_user_settings(user_id)
    s["blocked"] = blocked
    _save_to_disk()


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
