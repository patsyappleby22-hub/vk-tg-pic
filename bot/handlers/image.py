"""
bot/handlers/image.py
~~~~~~~~~~~~~~~~~~~~~~
Handlers for text prompts and photo messages.

Supports two modes:
  - text: plain text prompt → image generation
  - photo: user sends a photo + caption → image editing/transformation
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.services.vertex_ai_service import VertexAIService
from bot.user_settings import (
    get_user_settings, pop_last_menu,
    set_active_task, clear_active_task, increment_generations,
    AVAILABLE_MODELS, SEND_MODES, RESOLUTIONS,
)
from bot.keyboards import BTN_MENU, BTN_STOP, BTN_SETTINGS, BTN_IDEAS
from core.exceptions import (
    BotError,
    QuotaExceededError,
    SafetyFilterError,
)

logger = logging.getLogger(__name__)
router = Router(name="image")


def _other_model_label(current_model: str) -> str:
    for model_id, info in AVAILABLE_MODELS.items():
        if model_id != current_model:
            return info["label"]
    return "другую модель"


def _suggest_switch_keyboard(current_model: str) -> InlineKeyboardMarkup | None:
    other_models = {k: v for k, v in AVAILABLE_MODELS.items() if k != current_model}
    if not other_models:
        return None
    buttons: list[list[InlineKeyboardButton]] = []
    for model_id, info in other_models.items():
        buttons.append([
            InlineKeyboardButton(
                text=f"🔄 Переключиться на {info['label']}",
                callback_data=f"switch_model_{model_id}",
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

SPINNER = ["◐", "◓", "◑", "◒"]
ANIMATION_INTERVAL = 2.5

_TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
})


def _prompt_to_filename(prompt: str, max_words: int = 6) -> str:
    text = prompt.lower().translate(_TRANSLIT)
    text = unicodedata.normalize("NFKD", text)
    text = re.sub(r"[^a-z0-9\s]", "", text)
    words = text.split()[:max_words]
    slug = "_".join(words) if words else "image"
    slug = slug[:60]
    return f"{slug}.png"


def _upscale_image(image_bytes: bytes, max_side: int) -> bytes:
    if max_side <= 0:
        return image_bytes

    import io
    from PIL import Image

    img = Image.open(io.BytesIO(image_bytes))
    w, h = img.size

    if max(w, h) >= max_side:
        return image_bytes

    scale = max_side / max(w, h)
    new_w = int(w * scale)
    new_h = int(h * scale)

    img = img.resize((new_w, new_h), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class ProgressAnimator:

    def __init__(self, msg: Message, base_text: str) -> None:
        self._msg = msg
        self._base_text = base_text
        self._task: asyncio.Task | None = None
        self._stopped = False
        self._start_time = 0.0

    def start(self) -> None:
        import time
        self._start_time = time.monotonic()
        self._task = asyncio.create_task(self._animate())

    async def stop(self) -> None:
        self._stopped = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _animate(self) -> None:
        import time
        tick = 0
        await asyncio.sleep(ANIMATION_INTERVAL)

        while not self._stopped:
            elapsed = time.monotonic() - self._start_time
            secs = int(elapsed)
            spin = SPINNER[tick % len(SPINNER)]

            text = f"{self._base_text}\n\n{spin} <b>Обработка — {secs} сек.</b>"
            try:
                await self._msg.edit_text(text, parse_mode="HTML")
            except TelegramBadRequest:
                pass
            except Exception:
                break

            tick += 1
            await asyncio.sleep(ANIMATION_INTERVAL)


async def _dismiss_menu(bot: Bot, user_id: int) -> None:
    menu = pop_last_menu(user_id)
    if menu:
        chat_id, msg_id = menu
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass


_IMAGE_MIME_PREFIXES = ("image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp")


def _is_image_document(msg: Message) -> bool:
    if not msg.document:
        return False
    mime = (msg.document.mime_type or "").lower()
    return any(mime.startswith(p) for p in _IMAGE_MIME_PREFIXES)


def _has_image(msg: Message) -> bool:
    return bool(msg.photo) or _is_image_document(msg)


async def _download_photos(bot: Bot, messages: list[Message]) -> list[bytes]:
    photos: list[bytes] = []
    for msg in messages:
        if msg.photo:
            photo_obj = msg.photo[-1]
            file = await bot.get_file(photo_obj.file_id)
            photo_io = await bot.download_file(file.file_path)
            photos.append(photo_io.read())
        elif _is_image_document(msg):
            file = await bot.get_file(msg.document.file_id)
            photo_io = await bot.download_file(file.file_path)
            photos.append(photo_io.read())
    return photos


def _collect_caption(messages: list[Message]) -> str:
    for msg in messages:
        if msg.caption and msg.caption.strip():
            return msg.caption.strip()
    return ""


@router.message(lambda m: m.photo is not None)
async def handle_photo_prompt(
    message: Message,
    vertex_service: VertexAIService,
    album: list[Message] | None = None,
) -> None:
    photo_messages = album if album else [message]
    uid = message.from_user.id
    settings = get_user_settings(uid)

    caption = _collect_caption(photo_messages)
    if not caption:
        await message.reply(
            f"📷 Фото получено ({len(photo_messages)} шт.)! Пожалуйста, добавьте описание "
            "(подпись к фото) — что именно нужно сделать с изображением.\n\n"
            "Например: <i>Сделай фон более ярким</i> или <i>Добавь закат на задний план</i>",
            parse_mode="HTML",
        )
        return

    bot: Bot = message.bot  # type: ignore[assignment]
    await _dismiss_menu(bot, uid)
    user_model = settings.get("model", "gemini-3.1-flash-image-preview")
    model_label = AVAILABLE_MODELS.get(user_model, {}).get("label", user_model)
    photo_count = len(photo_messages)

    queue_msg_id: int | None = None
    if vertex_service.is_at_capacity:
        queue_note = await message.reply(
            "⏳ <b>Ваш запрос поставлен в очередь.</b>\n"
            "Система сейчас обрабатывает максимальное количество одновременных запросов. "
            "Ваше изображение будет сгенерировано в ближайшее время — пожалуйста, подождите.",
            parse_mode="HTML",
        )
        queue_msg_id = queue_note.message_id

    base_text = (
        f"🎨 <b>Редактирую изображение…</b>\n"
        f"🤖 {model_label}\n"
        f"📷 Фото: {photo_count} шт.\n"
        f"<i>Описание: {caption[:100]}{'…' if len(caption) > 100 else ''}</i>"
    )
    processing_msg = await message.reply(
        f"{base_text}\n\n◐ <b>Обработка — 0 сек.</b>",
        parse_mode="HTML",
    )

    animator = ProgressAnimator(processing_msg, base_text)
    animator.start()

    resolution = settings.get("resolution", "original")
    max_side = RESOLUTIONS.get(resolution, {}).get("max_side", 0)

    thinking_level = settings.get("thinking_level", "low")

    async def _do_generate() -> bytes:
        all_photo_bytes = await _download_photos(bot, photo_messages)
        raw = await vertex_service.generate_image(
            prompt=caption,
            images=all_photo_bytes,
            model_override=user_model,
            thinking_level=thinking_level,
        )
        if max_side > 0:
            loop = asyncio.get_running_loop()
            raw = await loop.run_in_executor(None, _upscale_image, raw, max_side)
        return raw

    gen_task = asyncio.create_task(_do_generate())
    set_active_task(uid, gen_task)

    try:
        image_bytes = await gen_task

        await animator.stop()
        clear_active_task(uid)

        send_mode = settings.get("send_mode", "photo")
        fname = _prompt_to_filename(caption)
        result_caption = f"✅ Изображение готово!\n<i>{caption[:200]}</i>"

        if send_mode == "document":
            doc = BufferedInputFile(file=image_bytes, filename=fname)
            await message.reply_document(
                document=doc,
                caption=result_caption,
                parse_mode="HTML",
            )
        else:
            photo = BufferedInputFile(file=image_bytes, filename=fname)
            await message.reply_photo(
                photo=photo,
                caption=result_caption,
                parse_mode="HTML",
            )

        increment_generations(uid, message.from_user.first_name or "")

        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=processing_msg.message_id)
        except Exception:
            pass
        if queue_msg_id is not None:
            try:
                await bot.delete_message(chat_id=message.chat.id, message_id=queue_msg_id)
            except Exception:
                pass

    except asyncio.CancelledError:
        await animator.stop()
        clear_active_task(uid)
        try:
            await processing_msg.edit_text(
                "⛔ <b>Генерация отменена.</b>",
                parse_mode="HTML",
            )
        except Exception:
            pass

    except SafetyFilterError as exc:
        await animator.stop()
        clear_active_task(uid)
        logger.warning("Safety filter blocked photo edit '%s': %s", caption[:60], exc)
        await processing_msg.edit_text(
            "🚫 <b>Запрос заблокирован фильтрами безопасности</b>\n\n"
            f"{exc.user_message}",
            parse_mode="HTML",
        )
    except QuotaExceededError:
        await animator.stop()
        clear_active_task(uid)
        logger.error("Quota exhausted for photo edit '%s'", caption[:60])
        current_name = AVAILABLE_MODELS.get(user_model, {}).get("label", user_model)
        other_name = _other_model_label(user_model)
        await processing_msg.edit_text(
            f"Модель <b>{current_name}</b> сейчас перегружена 😔\n\n"
            f"Попробуйте через пару минут или переключитесь на "
            f"<b>{other_name}</b> — у неё может быть свободная квота.",
            parse_mode="HTML",
            reply_markup=_suggest_switch_keyboard(user_model),
        )
    except BotError as exc:
        await animator.stop()
        clear_active_task(uid)
        logger.error("Bot error for photo edit '%s': %s", caption[:60], exc)
        await processing_msg.edit_text(
            f"{exc.user_message}",
            parse_mode="HTML",
            reply_markup=_suggest_switch_keyboard(user_model),
        )
    except Exception as exc:
        await animator.stop()
        clear_active_task(uid)
        logger.exception("Unexpected error for photo edit '%s': %s", caption[:60], exc)
        await processing_msg.edit_text(
            "Не удалось обработать изображение 😔\n\n"
            "Попробуйте ещё раз или переключитесь на другую модель.",
            parse_mode="HTML",
            reply_markup=_suggest_switch_keyboard(user_model),
        )


@router.message(lambda m: _is_image_document(m))
async def handle_document_photo(
    message: Message,
    vertex_service: VertexAIService,
    album: list[Message] | None = None,
) -> None:
    await handle_photo_prompt(message, vertex_service, album)


RESERVED_TEXTS = {BTN_MENU, BTN_STOP, BTN_SETTINGS, BTN_IDEAS}


def _in_creative_session(m: Message) -> bool:
    from bot.handlers.creative import _is_in_session
    return _is_in_session(m.from_user.id)


@router.message(
    ~Command(commands=["start", "help", "cancel", "menu", "settings", "adminmrxgyt"]),
    lambda m: m.text not in RESERVED_TEXTS if m.text else True,
    lambda m: not _in_creative_session(m),
)
async def handle_text_prompt(message: Message, vertex_service: VertexAIService) -> None:
    prompt = message.text or ""
    if not prompt.strip():
        await message.reply("Пожалуйста, отправьте текстовое описание изображения, которое хотите сгенерировать.")
        return

    uid = message.from_user.id
    settings = get_user_settings(uid)
    user_model = settings.get("model", "gemini-3.1-flash-image-preview")
    model_label = AVAILABLE_MODELS.get(user_model, {}).get("label", user_model)

    bot: Bot = message.bot  # type: ignore[assignment]
    await _dismiss_menu(bot, uid)

    queue_msg_id: int | None = None
    if vertex_service.is_at_capacity:
        queue_note = await message.reply(
            "⏳ <b>Ваш запрос поставлен в очередь.</b>\n"
            "Система сейчас обрабатывает максимальное количество одновременных запросов. "
            "Ваше изображение будет сгенерировано в ближайшее время — пожалуйста, подождите.",
            parse_mode="HTML",
        )
        queue_msg_id = queue_note.message_id

    base_text = (
        f"🎨 <b>Генерирую изображение…</b>\n"
        f"🤖 {model_label}\n"
        f"<i>Промпт: {prompt[:100]}{'…' if len(prompt) > 100 else ''}</i>"
    )
    processing_msg = await message.reply(
        f"{base_text}\n\n◐ <b>Обработка — 0 сек.</b>",
        parse_mode="HTML",
    )

    animator = ProgressAnimator(processing_msg, base_text)
    animator.start()

    resolution = settings.get("resolution", "original")
    max_side = RESOLUTIONS.get(resolution, {}).get("max_side", 0)

    aspect_ratio = settings.get("aspect_ratio", "1:1")
    thinking_level = settings.get("thinking_level", "low")

    async def _do_text_generate() -> bytes:
        raw = await vertex_service.generate_image(
            prompt=prompt,
            model_override=user_model,
            aspect_ratio=aspect_ratio,
            thinking_level=thinking_level,
        )
        if max_side > 0:
            loop = asyncio.get_running_loop()
            raw = await loop.run_in_executor(None, _upscale_image, raw, max_side)
        return raw

    gen_task = asyncio.create_task(_do_text_generate())
    set_active_task(uid, gen_task)

    try:
        image_bytes = await gen_task

        await animator.stop()
        clear_active_task(uid)

        send_mode = settings.get("send_mode", "photo")
        fname = _prompt_to_filename(prompt)
        result_caption = f"✅ Ваше изображение готово!\n<i>{prompt[:200]}</i>"

        if send_mode == "document":
            doc = BufferedInputFile(file=image_bytes, filename=fname)
            await message.reply_document(
                document=doc,
                caption=result_caption,
                parse_mode="HTML",
            )
        else:
            photo = BufferedInputFile(file=image_bytes, filename=fname)
            await message.reply_photo(
                photo=photo,
                caption=result_caption,
                parse_mode="HTML",
            )

        increment_generations(uid, message.from_user.first_name or "")

        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=processing_msg.message_id)
        except Exception:
            pass
        if queue_msg_id is not None:
            try:
                await bot.delete_message(chat_id=message.chat.id, message_id=queue_msg_id)
            except Exception:
                pass

    except asyncio.CancelledError:
        await animator.stop()
        clear_active_task(uid)
        try:
            await processing_msg.edit_text(
                "⛔ <b>Генерация отменена.</b>",
                parse_mode="HTML",
            )
        except Exception:
            pass

    except SafetyFilterError as exc:
        await animator.stop()
        clear_active_task(uid)
        logger.warning("Safety filter blocked prompt '%s': %s", prompt[:60], exc)
        await processing_msg.edit_text(
            "🚫 <b>Запрос заблокирован фильтрами безопасности</b>\n\n"
            f"{exc.user_message}",
            parse_mode="HTML",
        )
    except QuotaExceededError:
        await animator.stop()
        clear_active_task(uid)
        logger.error("Quota exhausted for prompt '%s'", prompt[:60])
        current_name = AVAILABLE_MODELS.get(user_model, {}).get("label", user_model)
        other_name = _other_model_label(user_model)
        await processing_msg.edit_text(
            f"Модель <b>{current_name}</b> сейчас перегружена 😔\n\n"
            f"Попробуйте через пару минут или переключитесь на "
            f"<b>{other_name}</b> — у неё может быть свободная квота.",
            parse_mode="HTML",
            reply_markup=_suggest_switch_keyboard(user_model),
        )
    except BotError as exc:
        await animator.stop()
        clear_active_task(uid)
        logger.error("Bot error for prompt '%s': %s", prompt[:60], exc)
        await processing_msg.edit_text(
            f"{exc.user_message}",
            parse_mode="HTML",
            reply_markup=_suggest_switch_keyboard(user_model),
        )
    except Exception as exc:
        await animator.stop()
        clear_active_task(uid)
        logger.exception("Unexpected error for prompt '%s': %s", prompt[:60], exc)
        await processing_msg.edit_text(
            "Не удалось сгенерировать изображение 😔\n\n"
            "Попробуйте ещё раз или переключитесь на другую модель.",
            parse_mode="HTML",
            reply_markup=_suggest_switch_keyboard(user_model),
        )
