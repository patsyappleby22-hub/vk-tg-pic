import asyncio
import datetime
import errno
import logging
import os
import sys


class _MskFormatter(logging.Formatter):
    """Logging formatter that shows Moscow time (UTC+3) instead of UTC."""
    _MSK = datetime.timezone(datetime.timedelta(hours=3))

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.datetime.fromtimestamp(record.created, tz=self._MSK)
        return dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S")


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
logging.getLogger("vkbottle").setLevel(logging.WARNING)
logging.getLogger("google_genai").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

logger = logging.getLogger("start_all")


async def run_telegram(vertex_service):
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not tg_token:
        logger.info("TELEGRAM_BOT_TOKEN not set — Telegram bot disabled")
        return

    from aiogram import Bot, Dispatcher
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from bot.handlers import admin as admin_handler
    from bot.handlers import callbacks as callbacks_handler
    from bot.handlers import creative as creative_handler
    from bot.handlers import image as image_handler
    from bot.handlers import start as start_handler
    from bot.middlewares.album_middleware import AlbumMiddleware
    from bot.middlewares.identity_middleware import IdentityMiddleware
    from bot.middlewares.logging_middleware import LoggingMiddleware

    bot = Bot(
        token=tg_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    from bot.notify import set_bot
    set_bot(bot)

    dp = Dispatcher()
    dp.update.outer_middleware(LoggingMiddleware())
    # Capture sender first_name + @username on every message/callback so the
    # web-chat login flow can resolve `@username → user_id` locally (the
    # Bot API has no public way to do that lookup for plain users).
    dp.message.outer_middleware(IdentityMiddleware())
    dp.callback_query.outer_middleware(IdentityMiddleware())
    dp.message.middleware(AlbumMiddleware())
    dp["vertex_service"] = vertex_service
    dp.include_router(start_handler.router)
    dp.include_router(admin_handler.router)
    dp.include_router(creative_handler.router)
    dp.include_router(callbacks_handler.router)
    dp.include_router(image_handler.router)

    # One-shot background backfill: existing users registered before the
    # `username` column existed have an empty handle in storage, which
    # breaks login-by-@username from the web chat. Throttled to ~1 RPS.
    asyncio.create_task(_backfill_usernames(bot))

    logger.info("Telegram bot starting...")
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        await bot.session.close()
        logger.info("Telegram bot stopped.")


async def _backfill_usernames(bot) -> None:
    """Backfill empty `username` fields for known TG users by calling
    `getChat(uid)` on each. Throttled to ~1 RPS to stay well under
    Telegram's per-bot rate limit. Errors are logged and ignored —
    a missing username is not fatal, the user can still log in by
    numeric ID."""
    from bot.user_settings import (
        list_user_ids_missing_username, set_tg_identity,
    )
    try:
        await asyncio.sleep(2)  # let the bot finish booting
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
            await asyncio.sleep(1.0)
        logger.info("username backfill: done, filled %d/%d",
                    filled, len(ids))
    except Exception:
        logger.exception("username backfill: aborted")


async def run_vk(vertex_service):
    vk_token = os.getenv("VK_BOT_TOKEN", "")
    if not vk_token:
        logger.info("VK_BOT_TOKEN not set — VK bot disabled")
        return

    from vkbottle.bot import Bot as VKBot
    from vk_bot.handlers import register_handlers
    import threading

    logger.info("VK bot starting...")

    def _run_vk_in_thread():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        bot = VKBot(token=vk_token)
        register_handlers(bot, vertex_service)
        try:
            bot.run_forever()
        except Exception:
            logger.exception("VK bot error")
        finally:
            loop.close()
            # Close this thread's own DB connection cleanly
            from bot.db import _close_conn as _db_close
            _db_close()
            logger.info("VK bot stopped.")

    thread = threading.Thread(target=_run_vk_in_thread, daemon=True, name="vk-bot")
    thread.start()
    # Keep this coroutine alive as long as the thread runs
    while thread.is_alive():
        await asyncio.sleep(1)


async def web_server():
    from aiohttp import web
    from bot.web_server import create_web_app

    port = int(os.environ.get("PORT", 8080))
    logger.info("Web server starting on port %d (landing + payment webhooks)", port)
    for attempt in range(10):
        app = create_web_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', port)
        try:
            await site.start()
            break
        except OSError as exc:
            await runner.cleanup()
            if exc.errno != errno.EADDRINUSE:
                raise
            if attempt == 9:
                logger.error(
                    "Port %d is already in use after retries; keeping bots running without starting another web server",
                    port,
                )
                while True:
                    await asyncio.sleep(3600)
            logger.warning("Port %d is already in use; retrying web server start in 3 seconds", port)
            await asyncio.sleep(3)
    while True:
        await asyncio.sleep(3600)


async def main():
    from bot.config import get_settings
    from bot.services.vertex_ai_service import VertexAIService
    from bot.user_settings import load_settings

    settings = get_settings()
    load_settings()

    vertex_service = VertexAIService(settings)

    # Give the admin panel access to live slot statuses
    from bot.web_admin import set_vertex_service
    set_vertex_service(vertex_service)
    # Same for the user-facing web chat
    from bot.web_chat import set_vertex_service as _wc_set
    _wc_set(vertex_service)

    tasks = []

    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    vk_token = os.getenv("VK_BOT_TOKEN", "")

    if tg_token:
        tasks.append(asyncio.create_task(run_telegram(vertex_service)))
    if vk_token:
        tasks.append(asyncio.create_task(run_vk(vertex_service)))

    tasks.append(asyncio.create_task(web_server()))

    # Autopub scheduler
    try:
        from bot.autopub.scheduler import autopub_loop
        tasks.append(asyncio.create_task(autopub_loop(vertex_service)))
    except ImportError as _e:
        logger.warning("autopub module not available, skipping scheduler: %s", _e)

    # Broadcasts scheduler (mass mailing)
    try:
        from bot.broadcasts.scheduler import broadcast_loop
        tasks.append(asyncio.create_task(broadcast_loop()))
    except Exception as _e:
        logger.warning("broadcasts module not available, skipping scheduler: %s", _e)

    if not tasks:
        logger.error("No bot tokens set — set TELEGRAM_BOT_TOKEN and/or VK_BOT_TOKEN")
        return

    enabled = []
    if tg_token:
        enabled.append("Telegram")
    if vk_token:
        enabled.append("VK")
    logger.info("Running bots: %s", ", ".join(enabled))

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
