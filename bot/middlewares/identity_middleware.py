"""
bot/middlewares/identity_middleware.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Captures the sender's Telegram first_name + @username on every message and
callback so the web-chat login flow can later resolve `@username → user_id`
from local state. The Bot API has no public method to resolve a user's
@username into a numeric ID, so we maintain this mapping ourselves on
every interaction with the bot.

Cheap: a write happens only when the stored identity actually differs.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery

from bot.user_settings import set_tg_identity

logger = logging.getLogger(__name__)


class IdentityMiddleware(BaseMiddleware):
    """Saves first_name + @username for the sender on every event."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            user = None
            if isinstance(event, Message):
                user = event.from_user
            elif isinstance(event, CallbackQuery):
                user = event.from_user
            if user is not None and user.id:
                set_tg_identity(
                    int(user.id),
                    first_name=user.first_name or "",
                    username=user.username or "",
                    platform="tg",
                )
        except Exception:
            # Identity capture must never break message handling.
            logger.debug("identity capture failed", exc_info=True)
        return await handler(event, data)
