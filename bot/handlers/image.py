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
    is_blocked, has_credits, is_video_model, get_video_credits_cost,
    is_music_model, get_music_credits_cost,
    video_supports_video_extension,
    reserve_credits, release_credits, confirm_credits,
)
from bot.keyboards import BTN_MENU, BTN_STOP, BTN_SETTINGS, BTN_CHAT
from bot.log_channel import log_generation, log_generation_video, log_generation_audio
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


def _prompt_to_audio_filename(prompt: str) -> str:
    return _prompt_to_filename(prompt).replace(".png", ".mp3")


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
_VIDEO_MIME_PREFIXES = ("video/mp4", "video/quicktime", "video/x-msvideo", "video/mpeg", "video/webm", "video/")


def _is_image_document(msg: Message) -> bool:
    if not msg.document:
        return False
    mime = (msg.document.mime_type or "").lower()
    return any(mime.startswith(p) for p in _IMAGE_MIME_PREFIXES)


def _is_video_document(msg: Message) -> bool:
    if not msg.document:
        return False
    mime = (msg.document.mime_type or "").lower()
    return any(mime.startswith(p) for p in _VIDEO_MIME_PREFIXES)


def _has_video(msg: Message) -> bool:
    return bool(msg.video) or _is_video_document(msg)


async def _download_video(bot: Bot, message: Message) -> bytes:
    if message.video:
        file = await bot.get_file(message.video.file_id)
    elif _is_video_document(message):
        file = await bot.get_file(message.document.file_id)
    else:
        raise ValueError("No video in message")
    video_io = await bot.download_file(file.file_path)
    return video_io.read()


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

    if is_blocked(uid):
        await message.reply("⛔ Ваш аккаунт заблокирован. Обратитесь к администратору.")
        return

    settings = get_user_settings(uid)
    user_model = settings.get("model", "gemini-3.1-flash-image-preview")
    if is_music_model(user_model):
        credits_cost = get_music_credits_cost(user_model)
    elif is_video_model(user_model):
        from bot.user_settings import calc_video_credits, video_supports_audio
        credits_cost = calc_video_credits(
            user_model,
            duration_seconds=8,
            audio=settings.get("video_audio", True) and video_supports_audio(user_model),
        )
    else:
        credits_cost = 2 if settings.get("resolution") == "4k" else 1

    if not reserve_credits(uid, credits_cost):
        msg = (
            "💳 <b>Кредиты закончились</b>\n\n"
            "У вас больше нет доступных генераций.\n"
            "Для продолжения работы приобретите пополнение кредитов."
            if credits_cost == 1 else
            "💳 <b>Недостаточно кредитов</b>\n\n"
            f"Генерация выбранной моделью стоит <b>{credits_cost} кредитов</b>.\n"
            "Пополните баланс для продолжения."
        )
        await message.reply(msg, parse_mode="HTML")
        return

    if is_music_model(user_model):
        caption = _collect_caption(photo_messages)
        if not caption:
            await message.reply(
                "📷 Фото получено! Добавьте описание музыки подписью к фото.\n\n"
                "Например: <i>Атмосферный синтвейв по настроению этого изображения</i>",
                parse_mode="HTML",
            )
            return

        bot_obj: Bot = message.bot  # type: ignore[assignment]
        await _dismiss_menu(bot_obj, uid)
        model_info = AVAILABLE_MODELS.get(user_model, {})
        model_label = model_info.get("label", user_model)
        duration_label = model_info.get("duration_label", "аудио")
        base_text = (
            f"🎵 <b>Генерирую музыку по фото…</b>\n"
            f"🤖 {model_label}\n"
            f"⏱ {duration_label} • MP3\n"
            f"<i>{caption[:100]}{'…' if len(caption) > 100 else ''}</i>"
        )
        processing_msg = await message.reply(
            f"{base_text}\n\n◐ <b>Обработка — 0 сек.</b>",
            parse_mode="HTML",
        )
        animator = ProgressAnimator(processing_msg, base_text)
        animator.start()
        _uname = message.from_user.username or message.from_user.first_name or ""

        async def _do_img2music() -> bytes:
            all_photo_bytes = await _download_photos(bot_obj, photo_messages)
            return await vertex_service.generate_music(
                prompt=caption,
                model=user_model,
                user_id=uid,
                username=_uname,
                image=all_photo_bytes[0],
            )

        gen_task = asyncio.create_task(_do_img2music())
        set_active_task(uid, gen_task)
        try:
            audio_bytes = await gen_task
            await animator.stop()
            clear_active_task(uid)
            audio_file = BufferedInputFile(file=audio_bytes, filename=_prompt_to_audio_filename(caption))
            await message.reply_audio(
                audio=audio_file,
                caption=f"✅ Музыка по фото готова!\n<i>{caption[:200]}</i>",
                parse_mode="HTML",
            )
            confirm_credits(uid, credits_cost, message.from_user.first_name or "", platform="tg", prompt=caption, model=user_model, gen_type="music")
            asyncio.create_task(log_generation_audio(
                audio_bytes=audio_bytes, prompt=caption, user_id=uid,
                user_name=message.from_user.first_name or str(uid),
                platform="tg", model=user_model,
            ))
            try:
                await bot_obj.delete_message(chat_id=message.chat.id, message_id=processing_msg.message_id)
            except Exception:
                pass
        except asyncio.CancelledError:
            await animator.stop()
            clear_active_task(uid)
            release_credits(uid, credits_cost)
            try:
                await processing_msg.edit_text("⛔ <b>Генерация отменена.</b>", parse_mode="HTML")
            except Exception:
                pass
        except SafetyFilterError as exc:
            await animator.stop()
            clear_active_task(uid)
            release_credits(uid, credits_cost)
            await processing_msg.edit_text(
                f"🚫 <b>Запрос заблокирован фильтрами безопасности</b>\n\n{exc.user_message}",
                parse_mode="HTML",
            )
        except QuotaExceededError:
            await animator.stop()
            clear_active_task(uid)
            release_credits(uid, credits_cost)
            await processing_msg.edit_text(
                f"Модель <b>{model_label}</b> сейчас перегружена 😔\n\nПопробуйте через пару минут.",
                parse_mode="HTML",
                reply_markup=_suggest_switch_keyboard(user_model),
            )
        except Exception as exc:
            await animator.stop()
            clear_active_task(uid)
            release_credits(uid, credits_cost)
            logger.exception("Error image→music '%s': %s", caption[:60], exc)
            await processing_msg.edit_text(
                "Не удалось сгенерировать музыку по фото 😔\n\nПопробуйте ещё раз.",
                parse_mode="HTML",
                reply_markup=_suggest_switch_keyboard(user_model),
            )
        return

    if is_video_model(user_model):
        from bot.user_settings import video_supports_image, video_supports_audio, calc_video_credits as _vcc
        if not video_supports_image(user_model):
            model_label = AVAILABLE_MODELS.get(user_model, {}).get("label", user_model)
            await message.reply(
                f"🎬 Модель <b>{model_label}</b> принимает только текстовые запросы.\n\n"
                "Отправьте текстовое описание для генерации видео, "
                "или переключите модель на <b>Veo 3.1 / Veo 3.1 Fast</b> для генерации видео по фото.",
                parse_mode="HTML",
            )
            return

        caption = _collect_caption(photo_messages)
        if not caption:
            await message.reply(
                f"📷 Фото получено! Добавьте описание (подпись к фото) — "
                "что должно происходить в видео.\n\n"
                "Например: <i>Камера медленно облетает этот объект</i>",
                parse_mode="HTML",
            )
            return

        video_audio = settings.get("video_audio", True) and video_supports_audio(user_model)
        _vres_photo = settings.get("video_resolution", "720p")
        credits_cost = _vcc(user_model, duration_seconds=8, audio=video_audio, resolution=_vres_photo)
        if not reserve_credits(uid, credits_cost):
            await message.reply(
                "💳 <b>Недостаточно кредитов</b>\n\n"
                f"Генерация видео стоит <b>{credits_cost} кредитов</b>.\n"
                "Пополните баланс для продолжения.",
                parse_mode="HTML",
            )
            return

        bot_obj: Bot = message.bot  # type: ignore[assignment]
        await _dismiss_menu(bot_obj, uid)
        model_label = AVAILABLE_MODELS.get(user_model, {}).get("label", user_model)
        video_aspect = settings.get("video_aspect_ratio", "16:9")
        video_resolution = settings.get("video_resolution", "720p")

        base_text = (
            f"🎬 <b>Генерирую видео по фото…</b>\n"
            f"🤖 {model_label}\n"
            f"📐 {video_aspect} • 8 сек • {video_resolution}\n"
            f"<i>{caption[:100]}{'…' if len(caption) > 100 else ''}</i>"
        )
        processing_msg = await message.reply(
            f"{base_text}\n\n◐ <b>Обработка — 0 сек.</b>",
            parse_mode="HTML",
        )
        animator = ProgressAnimator(processing_msg, base_text)
        animator.start()

        _uname = message.from_user.username or message.from_user.first_name or ""

        async def _do_img2vid() -> bytes:
            all_photo_bytes = await _download_photos(bot_obj, photo_messages)
            return await vertex_service.generate_video(
                prompt=caption,
                model=user_model,
                aspect_ratio=video_aspect,
                duration_seconds=8,
                resolution=video_resolution,
                generate_audio=video_audio,
                user_id=uid,
                username=_uname,
                image=all_photo_bytes[0],
            )

        gen_task = asyncio.create_task(_do_img2vid())
        set_active_task(uid, gen_task)

        try:
            video_bytes = await gen_task
            await animator.stop()
            clear_active_task(uid)

            vid_doc = BufferedInputFile(file=video_bytes, filename="video.mp4")
            await message.reply_video(
                video=vid_doc,
                caption=f"✅ Видео по фото готово!\n<i>{caption[:200]}</i>",
                parse_mode="HTML",
            )
            confirm_credits(uid, credits_cost, message.from_user.first_name or "", platform="tg", prompt=caption, model=user_model, gen_type="video")
            asyncio.create_task(log_generation_video(
                video_bytes=video_bytes, prompt=caption, user_id=uid,
                user_name=message.from_user.first_name or str(uid),
                platform="tg", model=user_model,
            ))
            try:
                await bot_obj.delete_message(chat_id=message.chat.id, message_id=processing_msg.message_id)
            except Exception:
                pass

        except asyncio.CancelledError:
            await animator.stop()
            clear_active_task(uid)
            release_credits(uid, credits_cost)
            try:
                await processing_msg.edit_text("⛔ <b>Генерация отменена.</b>", parse_mode="HTML")
            except Exception:
                pass
        except SafetyFilterError as exc:
            await animator.stop()
            clear_active_task(uid)
            release_credits(uid, credits_cost)
            await processing_msg.edit_text(
                f"🚫 <b>Запрос заблокирован фильтрами безопасности</b>\n\n{exc.user_message}",
                parse_mode="HTML",
            )
        except QuotaExceededError:
            await animator.stop()
            clear_active_task(uid)
            release_credits(uid, credits_cost)
            current_name = AVAILABLE_MODELS.get(user_model, {}).get("label", user_model)
            await processing_msg.edit_text(
                f"Модель <b>{current_name}</b> сейчас перегружена 😔\n\nПопробуйте через пару минут.",
                parse_mode="HTML",
                reply_markup=_suggest_switch_keyboard(user_model),
            )
        except Exception as exc:
            await animator.stop()
            clear_active_task(uid)
            release_credits(uid, credits_cost)
            logger.exception("Error image→video '%s': %s", caption[:60], exc)
            await processing_msg.edit_text(
                "Не удалось сгенерировать видео по фото 😔\n\nПопробуйте ещё раз.",
                parse_mode="HTML",
                reply_markup=_suggest_switch_keyboard(user_model),
            )
        return

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

    _uname = message.from_user.username or message.from_user.first_name or ""

    async def _do_generate() -> bytes:
        all_photo_bytes = await _download_photos(bot, photo_messages)
        raw = await vertex_service.generate_image(
            prompt=caption,
            images=all_photo_bytes,
            model_override=user_model,
            thinking_level=thinking_level,
            user_id=uid,
            username=_uname,
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

        confirm_credits(uid, credits_cost, message.from_user.first_name or "", platform="tg", prompt=caption, model=user_model, gen_type="image")
        asyncio.create_task(log_generation(
            image_bytes=image_bytes,
            prompt=caption,
            user_id=uid,
            user_name=message.from_user.first_name or str(uid),
            platform="tg",
            model=user_model,
        ))

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
        release_credits(uid, credits_cost)
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
        release_credits(uid, credits_cost)
        logger.warning("Safety filter blocked photo edit '%s': %s", caption[:60], exc)
        await processing_msg.edit_text(
            "🚫 <b>Запрос заблокирован фильтрами безопасности</b>\n\n"
            f"{exc.user_message}",
            parse_mode="HTML",
        )
    except QuotaExceededError:
        await animator.stop()
        clear_active_task(uid)
        release_credits(uid, credits_cost)
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
        release_credits(uid, credits_cost)
        logger.error("Bot error for photo edit '%s': %s", caption[:60], exc)
        await processing_msg.edit_text(
            f"{exc.user_message}",
            parse_mode="HTML",
            reply_markup=_suggest_switch_keyboard(user_model),
        )
    except Exception as exc:
        await animator.stop()
        clear_active_task(uid)
        release_credits(uid, credits_cost)
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


@router.message(lambda m: _has_video(m))
async def handle_video_extension(
    message: Message,
    vertex_service: VertexAIService,
) -> None:
    uid = message.from_user.id

    if is_blocked(uid):
        await message.reply("⛔ Ваш аккаунт заблокирован. Обратитесь к администратору.")
        return

    settings = get_user_settings(uid)
    user_model = settings.get("model", "gemini-3.1-flash-image-preview")

    if not is_video_model(user_model):
        await message.reply(
            "🎬 Отправка видео поддерживается только в видео-режиме.\n\n"
            "Переключите модель на <b>Veo 3.1 Lite</b> и выберите задачу "
            "<b>🔄 Video extension</b> в настройках.",
            parse_mode="HTML",
        )
        return

    video_task = settings.get("video_task", "text-to-video")
    if video_task != "video-extension":
        model_label = AVAILABLE_MODELS.get(user_model, {}).get("label", user_model)
        if not video_supports_video_extension(user_model):
            await message.reply(
                f"🎬 Модель <b>{model_label}</b> не поддерживает расширение видео.",
                parse_mode="HTML",
            )
        else:
            await message.reply(
                f"🎬 Видео получено! Для расширения видео переключите задачу на "
                f"<b>🔄 Video extension</b> в настройках видео.\n\n"
                f"Текущая задача: <b>{video_task}</b>",
                parse_mode="HTML",
            )
        return

    if not video_supports_video_extension(user_model):
        model_label = AVAILABLE_MODELS.get(user_model, {}).get("label", user_model)
        await message.reply(
            f"🎬 Модель <b>{model_label}</b> не поддерживает расширение видео.\n\n"
            "Используйте <b>Veo 3.1 Lite</b> для этой задачи.",
            parse_mode="HTML",
        )
        return

    from bot.user_settings import video_supports_audio, calc_video_credits
    video_audio = settings.get("video_audio", True) and video_supports_audio(user_model)
    _vres_ext = settings.get("video_resolution", "720p")
    credits_cost = calc_video_credits(user_model, duration_seconds=8, audio=video_audio, resolution=_vres_ext)
    if not reserve_credits(uid, credits_cost):
        await message.reply(
            "💳 <b>Недостаточно кредитов</b>\n\n"
            f"Расширение видео стоит <b>{credits_cost} кредитов</b>.\n"
            "Пополните баланс для продолжения.",
            parse_mode="HTML",
        )
        return

    caption = (message.caption or "").strip()
    bot_obj: Bot = message.bot  # type: ignore[assignment]
    await _dismiss_menu(bot_obj, uid)

    model_label = AVAILABLE_MODELS.get(user_model, {}).get("label", user_model)
    video_aspect = settings.get("video_aspect_ratio", "16:9")
    video_resolution = settings.get("video_resolution", "720p")

    prompt_display = caption[:100] if caption else "без дополнительного описания"
    base_text = (
        f"🔄 <b>Расширяю видео…</b>\n"
        f"🤖 {model_label}\n"
        f"📐 {video_aspect} • 7 сек • {video_resolution}\n"
        f"<i>{prompt_display}{'…' if len(caption) > 100 else ''}</i>"
    )
    processing_msg = await message.reply(
        f"{base_text}\n\n◐ <b>Обработка — 0 сек.</b>",
        parse_mode="HTML",
    )
    animator = ProgressAnimator(processing_msg, base_text)
    animator.start()

    _uname = message.from_user.username or message.from_user.first_name or ""

    async def _do_video_ext() -> bytes:
        video_bytes = await _download_video(bot_obj, message)
        return await vertex_service.generate_video(
            prompt=caption or "Continue the video naturally",
            model=user_model,
            aspect_ratio=video_aspect,
            duration_seconds=7,
            resolution=video_resolution,
            generate_audio=video_audio,
            user_id=uid,
            username=_uname,
            video=video_bytes,
        )

    gen_task = asyncio.create_task(_do_video_ext())
    set_active_task(uid, gen_task)

    try:
        result_bytes = await gen_task
        await animator.stop()
        clear_active_task(uid)

        vid_doc = BufferedInputFile(file=result_bytes, filename="extended_video.mp4")
        await message.reply_video(
            video=vid_doc,
            caption=f"✅ Видео расширено!\n<i>{caption[:200] if caption else 'Без описания'}</i>",
            parse_mode="HTML",
        )
        confirm_credits(uid, credits_cost, message.from_user.first_name or "", platform="tg", prompt=caption, model=user_model, gen_type="video_ext")
        try:
            await bot_obj.delete_message(chat_id=message.chat.id, message_id=processing_msg.message_id)
        except Exception:
            pass

    except asyncio.CancelledError:
        await animator.stop()
        clear_active_task(uid)
        release_credits(uid, credits_cost)
        try:
            await processing_msg.edit_text("⛔ <b>Генерация отменена.</b>", parse_mode="HTML")
        except Exception:
            pass
    except SafetyFilterError as exc:
        await animator.stop()
        clear_active_task(uid)
        release_credits(uid, credits_cost)
        await processing_msg.edit_text(
            f"🚫 <b>Запрос заблокирован фильтрами безопасности</b>\n\n{exc.user_message}",
            parse_mode="HTML",
        )
    except QuotaExceededError:
        await animator.stop()
        clear_active_task(uid)
        release_credits(uid, credits_cost)
        await processing_msg.edit_text(
            f"Модель <b>{model_label}</b> сейчас перегружена 😔\n\nПопробуйте через пару минут.",
            parse_mode="HTML",
            reply_markup=_suggest_switch_keyboard(user_model),
        )
    except Exception as exc:
        await animator.stop()
        clear_active_task(uid)
        release_credits(uid, credits_cost)
        logger.exception("Error video-extension: %s", exc)
        await processing_msg.edit_text(
            "Не удалось расширить видео 😔\n\nПопробуйте ещё раз.",
            parse_mode="HTML",
            reply_markup=_suggest_switch_keyboard(user_model),
        )


RESERVED_TEXTS = {BTN_MENU, BTN_STOP, BTN_SETTINGS, BTN_CHAT}


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

    if is_blocked(uid):
        await message.reply("⛔ Ваш аккаунт заблокирован. Обратитесь к администратору.")
        return

    settings = get_user_settings(uid)
    user_model = settings.get("model", "gemini-3.1-flash-image-preview")
    _is_video = is_video_model(user_model)
    _is_music = is_music_model(user_model)

    if _is_video:
        from bot.user_settings import calc_video_credits, video_supports_audio
        _vd = settings.get("video_duration", 8)
        _va = settings.get("video_audio", True) and video_supports_audio(user_model)
        _vres = settings.get("video_resolution", "720p")
        credits_cost = calc_video_credits(user_model, duration_seconds=_vd, audio=_va, resolution=_vres)
        video_task = settings.get("video_task", "text-to-video")
        if video_task == "image-to-video":
            from bot.user_settings import video_supports_image
            if video_supports_image(user_model):
                await message.reply(
                    "🖼 <b>Режим Image-to-video</b>\n\n"
                    "Отправьте изображение с подписью (описанием) — что должно происходить в видео.\n\n"
                    "Например: <i>Камера медленно облетает этот объект</i>",
                    parse_mode="HTML",
                )
                return
        elif video_task == "video-extension":
            if video_supports_video_extension(user_model):
                await message.reply(
                    "🔄 <b>Режим Video extension</b>\n\n"
                    "Отправьте видео (можно с подписью — как продолжить видео).\n\n"
                    "Например: <i>Camera slowly zooms out</i>",
                    parse_mode="HTML",
                )
                return
    elif _is_music:
        credits_cost = get_music_credits_cost(user_model)
    else:
        credits_cost = 2 if settings.get("resolution") == "4k" else 1

    if not reserve_credits(uid, credits_cost):
        cost_label = f"{credits_cost} кредитов" if credits_cost > 1 else "1 кредит"
        msg = (
            f"💳 <b>Недостаточно кредитов</b>\n\n"
            f"Генерация {'видео' if _is_video else 'музыки' if _is_music else 'изображения'} стоит <b>{cost_label}</b>.\n"
            "Пополните баланс для продолжения."
        )
        await message.reply(msg, parse_mode="HTML")
        return

    model_label = AVAILABLE_MODELS.get(user_model, {}).get("label", user_model)

    bot: Bot = message.bot  # type: ignore[assignment]
    await _dismiss_menu(bot, uid)

    queue_msg_id: int | None = None
    if vertex_service.is_at_capacity:
        queue_note = await message.reply(
            "⏳ <b>Ваш запрос поставлен в очередь.</b>\n"
            "Система сейчас обрабатывает максимальное количество одновременных запросов. "
            f"{'Видео' if _is_video else 'Музыка' if _is_music else 'Изображение'} будет сгенерировано в ближайшее время.",
            parse_mode="HTML",
        )
        queue_msg_id = queue_note.message_id

    gen_type = "видео" if _is_video else "музыку" if _is_music else "изображение"
    base_text = (
        f"🎨 <b>Генерирую {gen_type}…</b>\n"
        f"🤖 {model_label}\n"
        f"<i>Промпт: {prompt[:100]}{'…' if len(prompt) > 100 else ''}</i>"
    )
    if _is_video:
        dur = settings.get("video_duration", 8)
        vres = settings.get("video_resolution", "720p")
        base_text += f"\n⏱ {dur} сек • 📺 {vres}"
    elif _is_music:
        duration_label = AVAILABLE_MODELS.get(user_model, {}).get("duration_label", "MP3")
        base_text += f"\n⏱ {duration_label} • MP3"

    processing_msg = await message.reply(
        f"{base_text}\n\n◐ <b>Обработка — 0 сек.</b>",
        parse_mode="HTML",
    )

    animator = ProgressAnimator(processing_msg, base_text)
    animator.start()

    _uname_t = message.from_user.username or message.from_user.first_name or ""

    if _is_video:
        from bot.user_settings import video_supports_audio
        video_aspect = settings.get("video_aspect_ratio", "16:9")
        video_duration = settings.get("video_duration", 8)
        video_resolution = settings.get("video_resolution", "720p")
        video_audio = settings.get("video_audio", True) and video_supports_audio(user_model)

        async def _do_video_generate() -> bytes:
            return await vertex_service.generate_video(
                prompt=prompt,
                model=user_model,
                aspect_ratio=video_aspect,
                duration_seconds=video_duration,
                resolution=video_resolution,
                generate_audio=video_audio,
                user_id=uid,
                username=_uname_t,
            )

        gen_task = asyncio.create_task(_do_video_generate())
    elif _is_music:
        async def _do_music_generate() -> bytes:
            return await vertex_service.generate_music(
                prompt=prompt,
                model=user_model,
                user_id=uid,
                username=_uname_t,
            )

        gen_task = asyncio.create_task(_do_music_generate())
    else:
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
                user_id=uid,
                username=_uname_t,
            )
            if max_side > 0:
                loop = asyncio.get_running_loop()
                raw = await loop.run_in_executor(None, _upscale_image, raw, max_side)
            return raw

        gen_task = asyncio.create_task(_do_text_generate())

    set_active_task(uid, gen_task)

    try:
        result_bytes = await gen_task

        await animator.stop()
        clear_active_task(uid)

        if _is_video:
            fname = _prompt_to_filename(prompt).replace(".png", ".mp4")
            result_caption = f"✅ Ваше видео готово!\n<i>{prompt[:200]}</i>"
            video_file = BufferedInputFile(file=result_bytes, filename=fname)
            await message.reply_video(
                video=video_file,
                caption=result_caption,
                parse_mode="HTML",
            )
        elif _is_music:
            fname = _prompt_to_audio_filename(prompt)
            result_caption = f"✅ Ваша музыка готова!\n<i>{prompt[:200]}</i>"
            audio_file = BufferedInputFile(file=result_bytes, filename=fname)
            await message.reply_audio(
                audio=audio_file,
                caption=result_caption,
                parse_mode="HTML",
            )
        else:
            send_mode = settings.get("send_mode", "photo")
            fname = _prompt_to_filename(prompt)
            result_caption = f"✅ Ваше изображение готово!\n<i>{prompt[:200]}</i>"

            if send_mode == "document":
                doc = BufferedInputFile(file=result_bytes, filename=fname)
                await message.reply_document(
                    document=doc,
                    caption=result_caption,
                    parse_mode="HTML",
                )
            else:
                photo = BufferedInputFile(file=result_bytes, filename=fname)
                await message.reply_photo(
                    photo=photo,
                    caption=result_caption,
                    parse_mode="HTML",
                )

        _log_gen_type = "video" if _is_video else "music" if _is_music else "image"
        confirm_credits(uid, credits_cost, message.from_user.first_name or "", platform="tg", prompt=prompt, model=user_model, gen_type=_log_gen_type)
        _uname_log = message.from_user.first_name or str(uid)
        if _is_video:
            asyncio.create_task(log_generation_video(
                video_bytes=result_bytes, prompt=prompt, user_id=uid,
                user_name=_uname_log, platform="tg", model=user_model,
            ))
        elif _is_music:
            asyncio.create_task(log_generation_audio(
                audio_bytes=result_bytes, prompt=prompt, user_id=uid,
                user_name=_uname_log, platform="tg", model=user_model,
            ))
        else:
            asyncio.create_task(log_generation(
                image_bytes=result_bytes, prompt=prompt, user_id=uid,
                user_name=_uname_log, platform="tg", model=user_model,
            ))

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
        release_credits(uid, credits_cost)
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
        release_credits(uid, credits_cost)
        logger.warning("Safety filter blocked prompt '%s': %s", prompt[:60], exc)
        await processing_msg.edit_text(
            "🚫 <b>Запрос заблокирован фильтрами безопасности</b>\n\n"
            f"{exc.user_message}",
            parse_mode="HTML",
        )
    except QuotaExceededError:
        await animator.stop()
        clear_active_task(uid)
        release_credits(uid, credits_cost)
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
        release_credits(uid, credits_cost)
        logger.error("Bot error for prompt '%s': %s", prompt[:60], exc)
        await processing_msg.edit_text(
            f"{exc.user_message}",
            parse_mode="HTML",
            reply_markup=_suggest_switch_keyboard(user_model),
        )
    except Exception as exc:
        await animator.stop()
        clear_active_task(uid)
        release_credits(uid, credits_cost)
        logger.exception("Unexpected error for prompt '%s': %s", prompt[:60], exc)
        await processing_msg.edit_text(
            f"Не удалось сгенерировать {gen_type} 😔\n\n"
            "Попробуйте ещё раз или переключитесь на другую модель.",
            parse_mode="HTML",
            reply_markup=_suggest_switch_keyboard(user_model),
        )
