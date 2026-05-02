"""
bot/main.py
~~~~~~~~~~~
Entry point for the Telegram image-generation bot.

Responsibilities:
  - Load configuration via pydantic-settings.
  - Configure structured logging.
  - Build the aiogram Bot + Dispatcher.
  - Register middlewares, handlers, and dependency injections.
  - Start long-polling.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

# ── Block Google Application Default Credentials (ADC) ───────────────────────
# Replit runs on GCP infrastructure and exposes ambient Google credentials via
# the metadata server. Without this block, the genai SDK silently falls back to
# those credentials when our API key is disabled/revoked, bypassing all access
# controls. We replace google.auth.default with one that returns anonymous
# (empty) credentials so the SDK can only use the API key we explicitly provide.
def _block_adc() -> None:
    try:
        import google.auth
        from google.auth.credentials import AnonymousCredentials

        def _no_adc(scopes=None, request=None, quota_project_id=None, **kw):
            return AnonymousCredentials(), None

        google.auth.default = _no_adc
        logging.getLogger(__name__).debug("Google ADC blocked — strict API key mode active")
    except Exception:
        pass

_block_adc()

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.config import get_settings
from bot.handlers import admin as admin_handler
from bot.handlers import callbacks as callbacks_handler
from bot.handlers import creative as creative_handler
from bot.handlers import image as image_handler
from bot.handlers import start as start_handler
from bot.middlewares.album_middleware import AlbumMiddleware
from bot.middlewares.identity_middleware import IdentityMiddleware
from bot.middlewares.logging_middleware import LoggingMiddleware
from bot.services.vertex_ai_service import VertexAIService
from bot.user_settings import (
    list_user_ids_missing_username,
    load_settings,
    set_tg_identity,
)


class _MskFormatter(logging.Formatter):
    """Logging formatter that shows Moscow time (UTC+3)."""
    import datetime as _dt
    _MSK = _dt.timezone(_dt.timedelta(hours=3))

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        import datetime as _dt
        dt = _dt.datetime.fromtimestamp(record.created, tz=self._MSK)
        return dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S")


def _configure_logging() -> None:
    """Set up a sensible default logging configuration."""
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
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("google_genai").setLevel(logging.WARNING)


async def main() -> None:
    _configure_logging()
    logger = logging.getLogger(__name__)

    settings = get_settings()
    load_settings()
    logger.info("Starting Telegram bot (model=%s)", settings.vertex_ai_model)

    # ── Vertex AI service ────────────────────────────────────────────────────
    vertex_service = VertexAIService(settings)

    # ── Aiogram bot & dispatcher ─────────────────────────────────────────────
    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    # ── Middlewares ───────────────────────────────────────────────────────────
    dp.update.outer_middleware(LoggingMiddleware())
    # Capture sender first_name + @username on every message/callback so the
    # web-chat login flow can resolve `@username → user_id` locally (the
    # Bot API has no public way to do that lookup).
    dp.message.outer_middleware(IdentityMiddleware())
    dp.callback_query.outer_middleware(IdentityMiddleware())
    dp.message.middleware(AlbumMiddleware())

    # ── Dependency injection ──────────────────────────────────────────────────
    # Inject vertex_service into every handler that declares the parameter.
    dp["vertex_service"] = vertex_service

    # ── Routers ───────────────────────────────────────────────────────────────
    dp.include_router(start_handler.router)
    dp.include_router(admin_handler.router)
    dp.include_router(creative_handler.router)
    dp.include_router(callbacks_handler.router)
    dp.include_router(image_handler.router)

    # ── Username backfill ────────────────────────────────────────────────────
    # Existing users registered before the `username` column existed have an
    # empty handle in storage, which breaks login-by-@username from the web
    # chat. Run a throttled background sweep that asks Telegram for each
    # missing user's current @handle and saves it. New users are captured
    # automatically by IdentityMiddleware on their next message/callback.
    asyncio.create_task(_backfill_usernames(bot))

    # ── Start polling ─────────────────────────────────────────────────────────
    logger.info("Bot is running. Press Ctrl+C to stop.")
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        await bot.session.close()
        logger.info("Bot stopped.")


async def _backfill_usernames(bot: "Bot") -> None:
    """Backfill empty `username` fields for known TG users by calling
    `getChat(uid)` on each. Throttled to ~1 RPS so we never trip
    Telegram's per-bot rate limit (~30 RPS) even with thousands of
    users. Errors are logged and ignored — a missing username is not
    fatal, the user can still log in by numeric ID.
    """
    logger = logging.getLogger(__name__)
    try:
        # Tiny initial delay so the bot is fully ready before we start
        # hitting the API.
        await asyncio.sleep(2)
        ids = list_user_ids_missing_username(platform="tg")
        if not ids:
            return
        logger.info("username backfill: scanning %d user(s)", len(ids))
        filled = 0
        for uid in ids:
            try:
                chat = await bot.get_chat(uid)
                uname = (getattr(chat, "username", "") or "").strip()
                fname = (getattr(chat, "first_name", "") or "").strip()
                if uname or fname:
                    set_tg_identity(uid, first_name=fname, username=uname,
                                    platform="tg")
                    if uname:
                        filled += 1
            except Exception as exc:
                logger.debug("username backfill: get_chat(%s) failed: %s",
                             uid, exc)
            # ~1 RPS — well under TG's per-bot limits.
            await asyncio.sleep(1.0)
        logger.info("username backfill: done, filled %d/%d", filled, len(ids))
    except Exception:
        logger.exception("username backfill: aborted")


if __name__ == "__main__":
    asyncio.run(main())
