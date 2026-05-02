"""
bot/notify.py
~~~~~~~~~~~~~
Shared notification helpers for Telegram and VK bots.
The web server calls these after a successful payment webhook.
"""

from __future__ import annotations
import logging
import os
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

_tg_bot: Any = None
VK_BOT_TOKEN = os.getenv("VK_BOT_TOKEN", "")

_PAYMENT_TEXT = (
    "✅ Оплата прошла успешно!\n\n"
    "💎 Пакет: {label}\n"
    "💰 Сумма: {amount:.0f}₽\n"
    "🔋 Начислено: +{credits} кредитов\n\n"
    "Приятного использования! 🎨"
)


def set_bot(bot: Any) -> None:
    global _tg_bot
    _tg_bot = bot


def get_tg_bot() -> Any:
    """Return the registered aiogram Bot instance (or None if TG disabled)."""
    return _tg_bot


async def notify_payment(user_id: int, credits: int, amount: float,
                         pack_label: str, source: str = "tg") -> None:
    text = _PAYMENT_TEXT.format(label=pack_label, amount=amount, credits=credits)
    if source == "vk":
        await _notify_vk(user_id, text)
    else:
        await _notify_tg(user_id, text)


async def _notify_tg(user_id: int, text: str) -> None:
    if _tg_bot is None:
        return
    try:
        await _tg_bot.send_message(
            chat_id=user_id,
            text=text.replace("+", "<b>+").replace(" кредитов", " кредитов</b>", 1),
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.warning("TG payment notification failed for user %s: %s", user_id, exc)


async def _notify_vk(user_id: int, text: str) -> None:
    token = VK_BOT_TOKEN or os.getenv("VK_BOT_TOKEN", "")
    if not token:
        logger.warning("VK_BOT_TOKEN not set — cannot send VK payment notification")
        return
    try:
        import random
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.vk.com/method/messages.send",
                data={
                    "user_id": user_id,
                    "message": text,
                    "random_id": random.randint(0, 2**31),
                    "access_token": token,
                    "v": "5.131",
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                result = await resp.json(content_type=None)
                if "error" in result:
                    logger.warning("VK notify error for user %s: %s", user_id, result["error"])
    except Exception as exc:
        logger.warning("VK payment notification failed for user %s: %s", user_id, exc)
