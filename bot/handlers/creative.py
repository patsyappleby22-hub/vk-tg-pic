"""
bot/handlers/creative.py
~~~~~~~~~~~~~~~~~~~~~~~~~
AI Chat mode — full multimodal conversation powered by gemini-3.1-pro-preview.
Accepts text, images, voice, audio, video, video notes, documents (PDF/text), stickers.
"""

from __future__ import annotations

import asyncio
import io
import logging
from typing import Any

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from bot.keyboards import BTN_CHAT, get_persistent_keyboard
from bot.services.vertex_ai_service import VertexAIService

logger = logging.getLogger(__name__)
router = Router(name="creative")

_sessions: dict[int, list[dict[str, Any]]] = {}

_SUPPORTED_IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"}
_SUPPORTED_AUDIO_MIMES = {
    "audio/x-aac", "audio/flac", "audio/mp3", "audio/m4a", "audio/mpeg",
    "audio/mpga", "audio/mp4", "audio/ogg", "audio/pcm", "audio/wav", "audio/webm",
}
_SUPPORTED_VIDEO_MIMES = {
    "video/x-flv", "video/quicktime", "video/mpeg", "video/mpegs", "video/mpg",
    "video/mp4", "video/webm", "video/wmv", "video/3gpp",
}
_SUPPORTED_DOC_MIMES = {"application/pdf", "text/plain"}
_ALL_SUPPORTED_MIMES = (
    _SUPPORTED_IMAGE_MIMES | _SUPPORTED_AUDIO_MIMES | _SUPPORTED_VIDEO_MIMES | _SUPPORTED_DOC_MIMES
)

_MIME_ALIASES: dict[str, str] = {
    "audio/x-opus+ogg": "audio/ogg",
    "audio/opus": "audio/ogg",
    "image/jpg": "image/jpeg",
    "audio/x-m4a": "audio/m4a",
    "video/x-mp4": "video/mp4",
}


def _is_in_session(user_id: int) -> bool:
    return user_id in _sessions


def _normalize_mime(mime: str | None) -> str | None:
    if not mime:
        return None
    mime = _MIME_ALIASES.get(mime, mime)
    if mime in _ALL_SUPPORTED_MIMES:
        return mime
    return None


async def _download(bot: Bot, file_id: str) -> bytes:
    buf = io.BytesIO()
    await bot.download(file_id, destination=buf)
    buf.seek(0)
    return buf.read()


async def _extract_parts(message: Message) -> list[dict]:
    """Download attachments and return list of part dicts."""
    bot = message.bot
    parts: list[dict] = []

    text = message.text or message.caption or ""
    if text:
        parts.append({"type": "text", "text": text})

    if message.photo:
        try:
            data = await _download(bot, message.photo[-1].file_id)
            parts.append({"type": "media", "data": data, "mime_type": "image/jpeg"})
        except Exception as e:
            logger.warning("Photo download failed: %s", e)
            parts.append({"type": "text", "text": "[изображение — не удалось загрузить]"})

    elif message.voice:
        try:
            mime = _normalize_mime(message.voice.mime_type) or "audio/ogg"
            data = await _download(bot, message.voice.file_id)
            parts.append({"type": "media", "data": data, "mime_type": mime})
        except Exception as e:
            logger.warning("Voice download failed: %s", e)
            parts.append({"type": "text", "text": "[голосовое сообщение — не удалось загрузить]"})

    elif message.audio:
        try:
            mime = _normalize_mime(message.audio.mime_type) or "audio/mpeg"
            data = await _download(bot, message.audio.file_id)
            title = message.audio.title or message.audio.file_name or "аудио"
            parts.append({"type": "media", "data": data, "mime_type": mime})
            if not text:
                parts.insert(0, {"type": "text", "text": f"[аудиофайл: {title}]"})
        except Exception as e:
            logger.warning("Audio download failed: %s", e)
            parts.append({"type": "text", "text": "[аудиофайл — не удалось загрузить]"})

    elif message.video:
        try:
            mime = _normalize_mime(message.video.mime_type) or "video/mp4"
            data = await _download(bot, message.video.file_id)
            parts.append({"type": "media", "data": data, "mime_type": mime})
        except Exception as e:
            logger.warning("Video download failed: %s", e)
            parts.append({"type": "text", "text": "[видео — не удалось загрузить]"})

    elif message.video_note:
        try:
            data = await _download(bot, message.video_note.file_id)
            parts.append({"type": "media", "data": data, "mime_type": "video/mp4"})
        except Exception as e:
            logger.warning("Video note download failed: %s", e)
            parts.append({"type": "text", "text": "[видео-кружок — не удалось загрузить]"})

    elif message.document:
        doc = message.document
        mime = _normalize_mime(doc.mime_type)
        if mime:
            try:
                data = await _download(bot, doc.file_id)
                parts.append({"type": "media", "data": data, "mime_type": mime})
                if not text:
                    parts.insert(0, {"type": "text", "text": f"[документ: {doc.file_name or 'файл'}]"})
            except Exception as e:
                logger.warning("Document download failed: %s", e)
                parts.append({"type": "text", "text": f"[документ {doc.file_name or ''} — не удалось загрузить]"})
        else:
            fname = doc.file_name or "неизвестный файл"
            parts.append({"type": "text", "text": f"[прикреплён файл: {fname} — формат не поддерживается моделью]"})

    elif message.sticker:
        sticker = message.sticker
        if not sticker.is_animated and not sticker.is_video:
            try:
                mime = _normalize_mime(sticker.mime_type) or "image/webp"
                data = await _download(bot, sticker.file_id)
                parts.append({"type": "media", "data": data, "mime_type": mime})
            except Exception as e:
                logger.warning("Sticker download failed: %s", e)
                parts.append({"type": "text", "text": "[стикер]"})
        else:
            parts.append({"type": "text", "text": "[анимированный/видео-стикер]"})

    return parts


def _build_api_contents(history: list[dict[str, Any]]) -> list[Any]:
    from google.genai import types as genai_types
    contents = []
    for msg in history:
        api_parts = []
        for part in msg["parts"]:
            if part["type"] == "text":
                api_parts.append(genai_types.Part.from_text(text=part["text"]))
            elif part["type"] == "media":
                api_parts.append(
                    genai_types.Part.from_bytes(data=part["data"], mime_type=part["mime_type"])
                )
        if api_parts:
            contents.append(genai_types.Content(role=msg["role"], parts=api_parts))
    return contents


_THINKING_FRAMES = ["💭 Думаю.", "💭 Думаю..", "💭 Думаю..."]


async def _animate_thinking(msg: Any, stop: asyncio.Event) -> None:
    i = 0
    while not stop.is_set():
        await asyncio.sleep(0.8)
        if stop.is_set():
            break
        try:
            await msg.edit_text(_THINKING_FRAMES[i % 3])
        except Exception:
            break
        i += 1


@router.message(lambda m: m.text == BTN_CHAT)
async def start_chat(message: Message) -> None:
    uid = message.from_user.id
    _sessions[uid] = []
    await message.answer(
        "💬 <b>Чат с Gemini 3.1 Pro</b>\n\n"
        "🧠 Анализирую текст, код, фото, видео, аудио и документы\n"
        "🌍 Отвечаю на любом языке\n"
        "📎 Разбираю PDF и файлы\n"
        "🎯 Решаю задачи, объясняю, генерирую идеи\n\n"
        "<i>Для выхода — ⛔ Стоп</i>",
        parse_mode="HTML",
    )


@router.message(
    lambda m: _is_in_session(m.from_user.id)
    and not (m.text and m.text.strip().startswith("/"))
)
async def chat_message(message: Message, vertex_service: VertexAIService) -> None:
    uid = message.from_user.id
    if uid not in _sessions:
        return

    thinking_msg = await message.answer("💭 Думаю.")
    stop_event = asyncio.Event()
    anim_task = asyncio.create_task(_animate_thinking(thinking_msg, stop_event))

    try:
        parts = await _extract_parts(message)

        if not parts:
            stop_event.set()
            anim_task.cancel()
            await thinking_msg.edit_text("Не удалось разобрать содержимое. Попробуйте ещё раз.")
            return

        _sessions[uid].append({"role": "user", "parts": parts})

        contents = _build_api_contents(_sessions[uid])
        response = await vertex_service.chat_text(contents)

        stop_event.set()
        anim_task.cancel()

        if not response:
            _sessions[uid].pop()
            await thinking_msg.edit_text("Не удалось получить ответ. Попробуйте ещё раз.")
            return

        _sessions[uid].append({
            "role": "model",
            "parts": [{"type": "text", "text": response}],
        })

        if len(_sessions[uid]) > 42:
            _sessions[uid] = _sessions[uid][:2] + _sessions[uid][-40:]

        if len(response) <= 4096:
            try:
                await thinking_msg.edit_text(response)
            except TelegramBadRequest:
                await thinking_msg.edit_text(response, parse_mode=None)
        else:
            try:
                await thinking_msg.edit_text(response[:4096])
            except TelegramBadRequest:
                await thinking_msg.edit_text(response[:4096], parse_mode=None)
            for i in range(4096, len(response), 4096):
                chunk = response[i:i + 4096]
                try:
                    await message.answer(chunk)
                except TelegramBadRequest:
                    await message.answer(chunk, parse_mode=None)

    except Exception as exc:
        stop_event.set()
        anim_task.cancel()
        logger.exception("Chat error: %s", exc)
        err_text = str(exc).lower()
        if "429" in err_text or "quota" in err_text or "resource exhausted" in err_text:
            msg = "⏳ API перегружен. Подождите пару минут и попробуйте снова."
        else:
            msg = "Произошла ошибка. Попробуйте ещё раз."
        try:
            await thinking_msg.edit_text(msg)
        except TelegramBadRequest:
            pass
