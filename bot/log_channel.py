"""
bot/log_channel.py
~~~~~~~~~~~~~~~~~~
Forward generated images to a private Telegram log channel.
Nothing is stored in our database — images reside only in Telegram's infrastructure.

Two send paths:
  - log_generation()     — uses the aiogram Bot instance (for TG handler context)
  - log_generation_vk()  — uses raw aiohttp (for VK handler context, separate event loop)
"""
from __future__ import annotations

import logging
import os

import aiohttp

logger = logging.getLogger(__name__)

LOG_CHANNEL_ID = -1003911574431
_TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")


def _caption(prompt: str, user_id: int, user_name: str,
             platform: str, model: str) -> str:
    plat_icon = "📱 Telegram" if platform == "tg" else "💙 ВКонтакте"
    text = (
        f"{plat_icon} | <b>{user_name}</b> (<code>{user_id}</code>)\n"
        f"🎨 <i>{prompt[:300]}</i>"
    )
    if model:
        text += f"\n🤖 <code>{model}</code>"
    return text


async def log_generation(
    image_bytes: bytes,
    prompt: str,
    user_id: int,
    user_name: str,
    platform: str = "tg",
    model: str = "",
) -> None:
    """Send generated image to the log channel via aiogram Bot (TG handler context)."""
    from bot.notify import _tg_bot
    if _tg_bot is None:
        logger.warning("log_channel: _tg_bot is None — канал не инициализирован")
        return
    try:
        from aiogram.types import BufferedInputFile
        from bot import db
        logger.debug("log_channel: отправляю фото в канал %s", LOG_CHANNEL_ID)
        msg = await _tg_bot.send_photo(
            chat_id=LOG_CHANNEL_ID,
            photo=BufferedInputFile(file=image_bytes, filename="gen.jpg"),
            caption=_caption(prompt, user_id, user_name, platform, model),
            parse_mode="HTML",
        )
        logger.info("log_channel: фото успешно отправлено в канал %s", LOG_CHANNEL_ID)
        # Save to DB for admin panel
        if msg.photo:
            largest = msg.photo[-1]
            db.save_image_log(
                user_id=user_id,
                user_name=user_name,
                platform=platform,
                prompt=prompt,
                model=model,
                file_id=largest.file_id,
                file_unique_id=largest.file_unique_id,
            )
    except Exception as exc:
        logger.warning("log_channel (tg path) failed [channel=%s]: %s", LOG_CHANNEL_ID, exc)


async def log_generation_video(
    video_bytes: bytes,
    prompt: str,
    user_id: int,
    user_name: str,
    platform: str = "tg",
    model: str = "",
) -> None:
    """Send generated video to the log channel."""
    from bot.notify import _tg_bot
    if _tg_bot is None:
        return
    try:
        from aiogram.types import BufferedInputFile
        caption = _caption(prompt, user_id, user_name, platform, model)
        msg = await _tg_bot.send_video(
            chat_id=LOG_CHANNEL_ID,
            video=BufferedInputFile(file=video_bytes, filename="gen.mp4"),
            caption=caption,
            parse_mode="HTML",
        )
        logger.info("log_channel: видео отправлено в канал %s", LOG_CHANNEL_ID)
        if msg.video:
            from bot import db
            db.save_image_log(
                user_id=user_id,
                user_name=user_name,
                platform=platform,
                prompt=prompt,
                model=model,
                file_id=msg.video.file_id,
                file_unique_id=msg.video.file_unique_id,
            )
    except Exception as exc:
        logger.warning("log_channel video failed [channel=%s]: %s", LOG_CHANNEL_ID, exc)


async def log_generation_audio(
    audio_bytes: bytes,
    prompt: str,
    user_id: int,
    user_name: str,
    platform: str = "tg",
    model: str = "",
) -> None:
    """Send generated audio/music to the log channel."""
    from bot.notify import _tg_bot
    if _tg_bot is None:
        return
    try:
        from aiogram.types import BufferedInputFile
        caption = _caption(prompt, user_id, user_name, platform, model)
        msg = await _tg_bot.send_audio(
            chat_id=LOG_CHANNEL_ID,
            audio=BufferedInputFile(file=audio_bytes, filename="gen.mp3"),
            caption=caption,
            parse_mode="HTML",
        )
        logger.info("log_channel: аудио отправлено в канал %s", LOG_CHANNEL_ID)
        if msg.audio:
            from bot import db
            db.save_image_log(
                user_id=user_id,
                user_name=user_name,
                platform=platform,
                prompt=prompt,
                model=model,
                file_id=msg.audio.file_id,
                file_unique_id=msg.audio.file_unique_id,
            )
    except Exception as exc:
        logger.warning("log_channel audio failed [channel=%s]: %s", LOG_CHANNEL_ID, exc)


async def log_generation_vk(
    image_bytes: bytes,
    prompt: str,
    user_id: int,
    user_name: str,
    model: str = "",
) -> None:
    """Send generated image to the log channel via raw HTTP (VK handler context)."""
    if not _TG_TOKEN:
        return
    try:
        url = f"https://api.telegram.org/bot{_TG_TOKEN}/sendPhoto"
        caption = _caption(prompt, user_id, user_name, "vk", model)
        data = aiohttp.FormData()
        data.add_field("chat_id", str(LOG_CHANNEL_ID))
        data.add_field("caption", caption)
        data.add_field("parse_mode", "HTML")
        data.add_field(
            "photo", image_bytes,
            filename="gen.jpg", content_type="image/jpeg",
        )
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data,
                                    timeout=aiohttp.ClientTimeout(total=20)) as resp:
                body = await resp.json(content_type=None)
                if resp.status != 200 or not body.get("ok"):
                    logger.warning("log_channel (vk path) HTTP %s: %s", resp.status, str(body)[:120])
                else:
                    # Save file_id to DB
                    try:
                        from bot import db
                        photos = body.get("result", {}).get("photo", [])
                        if photos:
                            largest = max(photos, key=lambda p: p.get("file_size", 0))
                            db.save_image_log(
                                user_id=user_id,
                                user_name=user_name,
                                platform="vk",
                                prompt=prompt,
                                model=model,
                                file_id=largest["file_id"],
                                file_unique_id=largest["file_unique_id"],
                            )
                    except Exception as db_exc:
                        logger.debug("log_channel (vk path) db save failed: %s", db_exc)
    except Exception as exc:
        logger.warning("log_channel (vk path) failed: %s", exc)
