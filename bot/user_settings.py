"""
bot/user_settings.py
~~~~~~~~~~~~~~~~~~~~~
Per-user settings storage with JSON file persistence.

Settings are kept in memory for fast access and saved to a JSON file
on every change so they survive bot restarts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SETTINGS_FILE = Path(os.getenv("SETTINGS_FILE", "telegram-bot/data/user_settings.json"))

_PERSISTENT_KEYS = {"model", "send_mode", "resolution", "aspect_ratio", "thinking_level", "first_name", "generations_count"}

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
    "last_menu_message_id": None,
    "last_menu_chat_id": None,
}


def _save_to_disk() -> None:
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        snapshot: dict[str, dict[str, Any]] = {}
        for uid, s in user_settings.items():
            snapshot[str(uid)] = {k: v for k, v in s.items() if k in _PERSISTENT_KEYS}
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
        tmp.replace(SETTINGS_FILE)
    except Exception:
        logger.exception("Failed to save user settings to %s", SETTINGS_FILE)


def load_settings() -> None:
    if not SETTINGS_FILE.exists():
        logger.info("No saved settings file found at %s — starting fresh", SETTINGS_FILE)
        return
    try:
        raw = json.loads(SETTINGS_FILE.read_text())
        count = 0
        for uid_str, saved in raw.items():
            uid = int(uid_str)
            merged = {**DEFAULT_SETTINGS}
            for k in _PERSISTENT_KEYS:
                if k in saved:
                    merged[k] = saved[k]
            user_settings[uid] = merged
            count += 1
        logger.info("Loaded settings for %d users from %s", count, SETTINGS_FILE)
    except Exception:
        logger.exception("Failed to load user settings from %s", SETTINGS_FILE)


def get_user_settings(user_id: int) -> dict[str, Any]:
    if user_id not in user_settings:
        user_settings[user_id] = {**DEFAULT_SETTINGS}
    return user_settings[user_id]


def save_user_settings(user_id: int) -> None:
    _save_to_disk()


def increment_generations(user_id: int, first_name: str = "") -> int:
    s = get_user_settings(user_id)
    s["generations_count"] = s.get("generations_count", 0) + 1
    if first_name:
        s["first_name"] = first_name
    _save_to_disk()
    return s["generations_count"]


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
