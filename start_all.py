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

log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logging.getLogger("aiogram").setLevel(logging.WARNING)
logging.getLogger("aiohttp").setLevel(logging.WARNING)
logging.getLogger("vkbottle").setLevel(logging.WARNING)

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
    port = int(os.environ.get("PORT", 5000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    logger.info("Web server starting on port %d (landing + payment webhooks)", port)
    await site.start()
    while True:
        await asyncio.sleep(3600)


async def main():
    from bot.config import get_settings
    from bot.services.vertex_ai_service import VertexAIService
    from bot.user_settings import load_settings

    settings = get_settings()
    load_settings()

    vertex_service = VertexAIService(settings)

    tasks = []

    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    vk_token = os.getenv("VK_BOT_TOKEN", "")

    if tg_token:
        tasks.append(asyncio.create_task(run_telegram(vertex_service)))
    if vk_token:
        tasks.append(asyncio.create_task(run_vk(vertex_service)))

    tasks.append(asyncio.create_task(web_server()))

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
