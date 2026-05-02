"""
bot/log_channel.py
~~~~~~~~~~~~~~~~~~
Forward generated images / videos / audio to a private Telegram log channel.
Nothing is stored in our database other than the resulting `file_id` — the
files themselves live only in Telegram's infrastructure and are re-streamed
to the web chat on demand.

Two send paths:
  - log_generation*()      — uses the aiogram Bot instance (TG handler + web ctx)
  - log_generation_*_vk()  — uses raw aiohttp (VK handler context, separate loop)
"""
from __future__ import annotations

import logging
import os

import aiohttp

logger = logging.getLogger(__name__)


def _resolve_log_channel_id() -> int | None:
    raw = os.getenv("LOG_CHANNEL_ID", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("log_channel: LOG_CHANNEL_ID=%r is not an integer", raw)
        return None


LOG_CHANNEL_ID: int | None = _resolve_log_channel_id()
_TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")


def _caption(prompt: str, user_id: int, user_name: str,
             platform: str, model: str) -> str:
    if platform == "vk":
        plat_icon = "ВКонтакте"
    elif platform == "web":
        plat_icon = "Веб-чат"
    else:
        plat_icon = "Telegram"
    text = (
        f"{plat_icon} | <b>{user_name}</b> (<code>{user_id}</code>)\n"
        f"<i>{prompt[:300]}</i>"
    )
    if model:
        text += f"\n<code>{model}</code>"
    return text


def _channel_ok() -> bool:
    if LOG_CHANNEL_ID is None:
        logger.warning("log_channel: LOG_CHANNEL_ID is not set — skipping upload")
        return False
    return True


async def log_generation(
    image_bytes: bytes,
    prompt: str,
    user_id: int,
    user_name: str,
    platform: str = "tg",
    model: str = "",
) -> tuple[str, str] | None:
    """Send generated image to the log channel via aiogram Bot.

    Returns (file_id, file_unique_id) on success.
    """
    if not _channel_ok():
        return None
    from bot.notify import _tg_bot
    if _tg_bot is None:
        logger.warning("log_channel: _tg_bot is None — канал не инициализирован")
        return None
    try:
        from aiogram.types import BufferedInputFile
        from bot import db
        msg = await _tg_bot.send_photo(
            chat_id=LOG_CHANNEL_ID,
            photo=BufferedInputFile(file=image_bytes, filename="gen.jpg"),
            caption=_caption(prompt, user_id, user_name, platform, model),
            parse_mode="HTML",
        )
        logger.info("log_channel: фото отправлено в канал %s", LOG_CHANNEL_ID)
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
            return largest.file_id, largest.file_unique_id
    except Exception as exc:
        logger.warning("log_channel (tg path) failed [channel=%s]: %s", LOG_CHANNEL_ID, exc)
    return None


async def log_generation_video(
    video_bytes: bytes,
    prompt: str,
    user_id: int,
    user_name: str,
    platform: str = "tg",
    model: str = "",
) -> tuple[str, str] | None:
    if not _channel_ok():
        return None
    from bot.notify import _tg_bot
    if _tg_bot is None:
        return None
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
            return msg.video.file_id, msg.video.file_unique_id
    except Exception as exc:
        logger.warning("log_channel video failed [channel=%s]: %s", LOG_CHANNEL_ID, exc)
    return None


async def log_generation_audio(
    audio_bytes: bytes,
    prompt: str,
    user_id: int,
    user_name: str,
    platform: str = "tg",
    model: str = "",
) -> tuple[str, str] | None:
    if not _channel_ok():
        return None
    from bot.notify import _tg_bot
    if _tg_bot is None:
        return None
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
            return msg.audio.file_id, msg.audio.file_unique_id
    except Exception as exc:
        logger.warning("log_channel audio failed [channel=%s]: %s", LOG_CHANNEL_ID, exc)
    return None


async def log_generation_document(
    file_bytes: bytes,
    filename: str,
    prompt: str,
    user_id: int,
    user_name: str,
    platform: str = "tg",
    model: str = "",
) -> tuple[str, str] | None:
    """Send any binary as a document (no compression). For files > 50 MB."""
    if not _channel_ok():
        return None
    from bot.notify import _tg_bot
    if _tg_bot is None:
        return None
    try:
        from aiogram.types import BufferedInputFile
        caption = _caption(prompt, user_id, user_name, platform, model)
        msg = await _tg_bot.send_document(
            chat_id=LOG_CHANNEL_ID,
            document=BufferedInputFile(file=file_bytes, filename=filename),
            caption=caption,
            parse_mode="HTML",
        )
        logger.info("log_channel: документ отправлен в канал %s", LOG_CHANNEL_ID)
        if msg.document:
            from bot import db
            db.save_image_log(
                user_id=user_id,
                user_name=user_name,
                platform=platform,
                prompt=prompt,
                model=model,
                file_id=msg.document.file_id,
                file_unique_id=msg.document.file_unique_id,
            )
            return msg.document.file_id, msg.document.file_unique_id
    except Exception as exc:
        logger.warning("log_channel document failed [channel=%s]: %s", LOG_CHANNEL_ID, exc)
    return None


async def log_generation_vk(
    image_bytes: bytes,
    prompt: str,
    user_id: int,
    user_name: str,
    model: str = "",
) -> None:
    """Raw HTTP variant for the VK bot's separate event loop."""
    if not _channel_ok() or not _TG_TOKEN:
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
                                    timeout=aiohttp.ClientTimeout(total=30)) as resp:
                body = await resp.json(content_type=None)
                if resp.status != 200 or not body.get("ok"):
                    logger.warning("log_channel (vk path) HTTP %s: %s", resp.status, str(body)[:120])
                else:
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


async def log_generation_video_vk(
    video_bytes: bytes,
    prompt: str,
    user_id: int,
    user_name: str,
    model: str = "",
) -> None:
    """Raw HTTP video upload from the VK bot context."""
    if not _channel_ok() or not _TG_TOKEN:
        return
    try:
        url = f"https://api.telegram.org/bot{_TG_TOKEN}/sendVideo"
        caption = _caption(prompt, user_id, user_name, "vk", model)
        data = aiohttp.FormData()
        data.add_field("chat_id", str(LOG_CHANNEL_ID))
        data.add_field("caption", caption)
        data.add_field("parse_mode", "HTML")
        data.add_field(
            "video", video_bytes,
            filename="gen.mp4", content_type="video/mp4",
        )
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data,
                                    timeout=aiohttp.ClientTimeout(total=180)) as resp:
                body = await resp.json(content_type=None)
                if resp.status != 200 or not body.get("ok"):
                    logger.warning("log_channel video (vk path) HTTP %s: %s", resp.status, str(body)[:200])
                    return
                try:
                    from bot import db
                    video = body.get("result", {}).get("video") or {}
                    if video.get("file_id"):
                        db.save_image_log(
                            user_id=user_id,
                            user_name=user_name,
                            platform="vk",
                            prompt=prompt,
                            model=model,
                            file_id=video["file_id"],
                            file_unique_id=video.get("file_unique_id", ""),
                        )
                except Exception as db_exc:
                    logger.debug("log_channel video (vk path) db save failed: %s", db_exc)
    except Exception as exc:
        logger.warning("log_channel video (vk path) failed: %s", exc)


async def log_generation_audio_vk(
    audio_bytes: bytes,
    prompt: str,
    user_id: int,
    user_name: str,
    model: str = "",
) -> None:
    """Raw HTTP audio upload from the VK bot context."""
    if not _channel_ok() or not _TG_TOKEN:
        return
    try:
        url = f"https://api.telegram.org/bot{_TG_TOKEN}/sendAudio"
        caption = _caption(prompt, user_id, user_name, "vk", model)
        data = aiohttp.FormData()
        data.add_field("chat_id", str(LOG_CHANNEL_ID))
        data.add_field("caption", caption)
        data.add_field("parse_mode", "HTML")
        data.add_field(
            "audio", audio_bytes,
            filename="gen.mp3", content_type="audio/mpeg",
        )
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data,
                                    timeout=aiohttp.ClientTimeout(total=120)) as resp:
                body = await resp.json(content_type=None)
                if resp.status != 200 or not body.get("ok"):
                    logger.warning("log_channel audio (vk path) HTTP %s: %s", resp.status, str(body)[:200])
                    return
                try:
                    from bot import db
                    audio = body.get("result", {}).get("audio") or {}
                    if audio.get("file_id"):
                        db.save_image_log(
                            user_id=user_id,
                            user_name=user_name,
                            platform="vk",
                            prompt=prompt,
                            model=model,
                            file_id=audio["file_id"],
                            file_unique_id=audio.get("file_unique_id", ""),
                        )
                except Exception as db_exc:
                    logger.debug("log_channel audio (vk path) db save failed: %s", db_exc)
    except Exception as exc:
        logger.warning("log_channel audio (vk path) failed: %s", exc)
