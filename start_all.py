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
    from bot.middlewares.logging_middleware import LoggingMiddleware

    bot = Bot(
        token=tg_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    from bot.notify import set_bot
    set_bot(bot)

    dp = Dispatcher()
    dp.update.outer_middleware(LoggingMiddleware())
    dp.message.middleware(AlbumMiddleware())
    dp["vertex_service"] = vertex_service
    dp.include_router(start_handler.router)
    dp.include_router(admin_handler.router)
    dp.include_router(creative_handler.router)
    dp.include_router(callbacks_handler.router)
    dp.include_router(image_handler.router)

    logger.info("Telegram bot starting...")
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        await bot.session.close()
        logger.info("Telegram bot stopped.")


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

    app = create_web_app()
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    logger.info("Web server starting on port %d (landing + payment webhooks)", port)
    for attempt in range(10):
        try:
            await site.start()
            break
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                await runner.cleanup()
                raise
            if attempt == 9:
                logger.error(
                    "Port %d is already in use after retries; keeping bots running without starting another web server",
                    port,
                )
                await runner.cleanup()
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
