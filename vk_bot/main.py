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


def _configure_logging() -> None:
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("vkbottle").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


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
