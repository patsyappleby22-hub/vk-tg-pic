"""
bot/middlewares/logging_middleware.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Aiogram middleware that logs every incoming update with timing information.

Registered as an outer middleware on the message router so it wraps the full
handler call, including downstream middlewares and the handler itself.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

logger = logging.getLogger(__name__)


class LoggingMiddleware(BaseMiddleware):
    """
    Outer middleware that logs every incoming Telegram update.

    Logs:
      - Update type and user info on entry.
      - Elapsed time on exit (success or exception).
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        start = time.monotonic()

        # Extract human-readable context if this is a full Update object
        user_info = "unknown"
        update_type = type(event).__name__

        if isinstance(event, Update):
            update_type = event.event_type
            msg = event.message or event.edited_message or event.callback_query
            if msg:
                user = getattr(msg, "from_user", None) or getattr(msg, "from_", None)
                if user:
                    user_info = f"user_id={user.id} username=@{user.username or 'N/A'}"

        logger.info("→ [%s] %s", update_type, user_info)

        try:
            result = await handler(event, data)
            elapsed = (time.monotonic() - start) * 1000
            logger.info("← [%s] %s — %.1f ms", update_type, user_info, elapsed)
            return result
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            logger.warning(
                "✗ [%s] %s — %.1f ms — %s: %s",
                update_type,
                user_info,
                elapsed,
                type(exc).__name__,
                exc,
            )
            raise
