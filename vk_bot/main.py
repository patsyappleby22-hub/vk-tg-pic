from __future__ import annotations

import asyncio
import logging
import os
import sys


def _block_adc() -> None:
    try:
        import google.auth
        from google.auth.credentials import AnonymousCredentials

        def _no_adc(scopes=None, request=None, quota_project_id=None, **kw):
            return AnonymousCredentials(), None

        google.auth.default = _no_adc
    except Exception:
        pass


_block_adc()

from vkbottle.bot import Bot

from bot.config import get_settings
from bot.services.vertex_ai_service import VertexAIService
from bot.user_settings import load_settings
from vk_bot.handlers import register_handlers


class _MskFormatter(logging.Formatter):
    """Logging formatter that shows Moscow time (UTC+3)."""
    import datetime as _dt
    _MSK = _dt.timezone(_dt.timedelta(hours=3))

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        import datetime as _dt
        dt = _dt.datetime.fromtimestamp(record.created, tz=self._MSK)
        return dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S")


def _configure_logging() -> None:
    if logging.getLogger().handlers:
        return  # already configured by start_all.py
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(_MskFormatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _root = logging.getLogger()
    _root.setLevel(getattr(logging, log_level, logging.INFO))
    _root.addHandler(_handler)
    logging.getLogger("vkbottle").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("google_genai").setLevel(logging.WARNING)


def main() -> None:
    _configure_logging()
    logger = logging.getLogger(__name__)

    vk_token = os.getenv("VK_BOT_TOKEN", "")
    if not vk_token:
        logger.error("VK_BOT_TOKEN not set — cannot start VK bot")
        sys.exit(1)

    settings = get_settings()
    load_settings()

    logger.info("Starting VK bot")

    vertex_service = VertexAIService(settings)

    bot = Bot(token=vk_token)

    register_handlers(bot, vertex_service)

    logger.info("VK bot is running. Press Ctrl+C to stop.")
    bot.run_forever()


if __name__ == "__main__":
    main()
