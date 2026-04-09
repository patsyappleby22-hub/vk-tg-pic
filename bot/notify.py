"""
bot/notify.py
~~~~~~~~~~~~~
Shared reference to the Telegram bot instance so the web server can
send payment notifications without circular imports.
"""

from __future__ import annotations
import logging
from typing import Any

logger = logging.getLogger(__name__)

_tg_bot: Any = None


def set_bot(bot: Any) -> None:
    global _tg_bot
    _tg_bot = bot


async def notify_payment(user_id: int, credits: int, amount: float, pack_label: str) -> None:
    if _tg_bot is None:
        return
    try:
        await _tg_bot.send_message(
            chat_id=user_id,
            text=(
                f"✅ <b>Оплата прошла успешно!</b>\n\n"
                f"💎 Пакет: {pack_label}\n"
                f"💰 Сумма: {amount:.0f}₽\n"
                f"🔋 Начислено: <b>+{credits} кредитов</b>\n\n"
                "Приятного использования! 🎨"
            ),
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.warning("Failed to send payment notification to user %s: %s", user_id, exc)
