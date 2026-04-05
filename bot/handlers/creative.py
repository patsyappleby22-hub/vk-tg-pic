"""
bot/handlers/creative.py
~~~~~~~~~~~~~~~~~~~~~~~~~
Creative assistant — helps users brainstorm and formulate image prompts
via an interactive chat powered by gemini-3.1-pro-preview.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup

from bot.keyboards import BTN_IDEAS, get_persistent_keyboard
from bot.services.vertex_ai_service import VertexAIService
from bot.user_settings import get_user_settings

logger = logging.getLogger(__name__)
router = Router(name="creative")

_sessions: dict[int, list[dict[str, Any]]] = {}

_final_prompts: dict[int, str] = {}

_msg_counts: dict[int, int] = {}

SYSTEM_PROMPT = (
    "Ты — креативный ассистент по созданию изображений. Твоя задача — помочь "
    "пользователю придумать идеальный промпт для генерации изображения с помощью ИИ.\n\n"
    "Правила:\n"
    "1. Общайся на русском языке, дружелюбно и вдохновляюще.\n"
    "2. Задавай вопросы по одному, чтобы уточнить идею: тема, стиль, настроение, "
    "цветовая палитра, композиция, детали.\n"
    "3. Когда у тебя достаточно информации (обычно после 2-4 вопросов), предложи "
    "итоговый промпт.\n"
    "4. Итоговый промпт оформи СТРОГО в таком формате:\n"
    "---PROMPT---\n"
    "тут детальный промпт на английском языке для генерации изображения\n"
    "---END---\n"
    "5. После промпта на русском объясни что он содержит и спроси подтверждение.\n"
    "6. Промпт должен быть на английском, детальный и включать все обсуждённые элементы.\n"
    "7. Отвечай кратко — не более 3-4 предложений за раз (кроме финального промпта)."
)

PROMPT_MARKER_START = "---PROMPT---"
PROMPT_MARKER_END = "---END---"


def _is_in_session(user_id: int) -> bool:
    return user_id in _sessions


def _extract_prompt(text: str) -> str | None:
    if PROMPT_MARKER_START not in text:
        return None
    start = text.index(PROMPT_MARKER_START) + len(PROMPT_MARKER_START)
    end = text.index(PROMPT_MARKER_END) if PROMPT_MARKER_END in text else len(text)
    prompt = text[start:end].strip()
    return prompt if prompt else None


def _build_contents(history: list[dict[str, Any]]) -> list[Any]:
    from google.genai import types as genai_types
    contents = []
    for msg in history:
        contents.append(
            genai_types.Content(
                role=msg["role"],
                parts=[genai_types.Part.from_text(text=msg["text"])],
            )
        )
    return contents


def _clean_for_display(text: str) -> str:
    result = text
    if PROMPT_MARKER_START in result:
        start = result.index(PROMPT_MARKER_START)
        end_marker = PROMPT_MARKER_END
        if end_marker in result:
            end = result.index(end_marker) + len(end_marker)
        else:
            end = len(result)
        prompt_block = result[start:end]
        result = result.replace(prompt_block, "").strip()
    return result


@router.message(lambda m: m.text == BTN_IDEAS)
async def start_creative(message: Message) -> None:
    uid = message.from_user.id
    _sessions[uid] = [
        {"role": "user", "text": SYSTEM_PROMPT + "\n\nПривет! Помоги мне придумать изображение."},
    ]
    _final_prompts.pop(uid, None)
    _msg_counts[uid] = 0

    await message.answer(
        "💡 <b>Режим «Идеи»</b>\n\n"
        "Я помогу придумать идеальное изображение! "
        "Расскажите, что вы хотите создать — я буду задавать вопросы.\n\n"
        "<i>Для выхода нажмите ⛔ Стоп</i>",
        parse_mode="HTML",
    )


@router.message(lambda m: m.text and _is_in_session(m.from_user.id) and m.text.strip().startswith("/") is False)
async def creative_chat(message: Message, vertex_service: VertexAIService) -> None:
    uid = message.from_user.id
    user_text = message.text.strip()

    if uid not in _sessions:
        return

    history = _sessions[uid]
    history.append({"role": "user", "text": user_text})
    _msg_counts[uid] = _msg_counts.get(uid, 0) + 1

    thinking_msg = await message.answer("💭 <b>Думаю...</b>", parse_mode="HTML")

    try:
        contents = _build_contents(history)
        response = await vertex_service.chat_text(contents)

        if not response:
            await thinking_msg.edit_text("Не удалось получить ответ, попробуйте ещё раз.")
            return

        history.append({"role": "model", "text": response})

        extracted = _extract_prompt(response)

        if extracted:
            _final_prompts[uid] = extracted
            display_text = _clean_for_display(response)

            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🎨 Генерируй!", callback_data="creative_generate")],
                [InlineKeyboardButton(text="✏️ Изменить", callback_data="creative_edit")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="creative_cancel")],
            ])

            prompt_preview = f"\n\n<b>Промпт:</b>\n<code>{extracted[:500]}</code>"

            await thinking_msg.edit_text(
                f"{display_text}{prompt_preview}",
                parse_mode="HTML",
                reply_markup=kb,
            )
        else:
            if _msg_counts.get(uid, 0) >= 2:
                auto_kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🪄 Дополни сам и генерируй", callback_data="creative_auto")],
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="creative_cancel")],
                ])
                await thinking_msg.edit_text(response, reply_markup=auto_kb)
            else:
                await thinking_msg.edit_text(response)

    except Exception as exc:
        logger.exception("Creative chat error: %s", exc)
        err_text = str(exc).lower()
        if "429" in err_text or "quota" in err_text or "resource exhausted" in err_text:
            msg = "⏳ Все API ключи сейчас перегружены. Подождите пару минут и попробуйте снова."
        else:
            msg = "Произошла ошибка, попробуйте ещё раз."
        try:
            await thinking_msg.edit_text(msg)
        except TelegramBadRequest:
            pass


@router.callback_query(lambda c: c.data == "creative_generate")
async def creative_generate(callback: CallbackQuery, vertex_service: VertexAIService) -> None:
    uid = callback.from_user.id
    prompt = _final_prompts.pop(uid, None)

    if not prompt:
        await callback.answer("Промпт не найден, начните заново.")
        return

    _sessions.pop(uid, None)
    _msg_counts.pop(uid, None)

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await callback.answer()

    await _run_generation(callback.message, callback.from_user, prompt, vertex_service)


async def _run_generation(
    message: Message,
    user: Any,
    prompt: str,
    vertex_service: VertexAIService,
) -> None:
    import asyncio
    from aiogram.types import BufferedInputFile
    from bot.handlers.image import (
        ProgressAnimator, _prompt_to_filename, _upscale_image,
        _suggest_switch_keyboard, _other_model_label,
    )
    from bot.user_settings import (
        increment_generations, set_active_task, clear_active_task,
        AVAILABLE_MODELS, RESOLUTIONS,
    )
    from core.exceptions import QuotaExceededError, SafetyFilterError, BotError

    uid = user.id
    settings = get_user_settings(uid)
    user_model = settings.get("model", "gemini-3.1-flash-image-preview")
    model_label = AVAILABLE_MODELS.get(user_model, {}).get("label", user_model)
    aspect_ratio = settings.get("aspect_ratio", "1:1")
    resolution = settings.get("resolution", "original")
    max_side = RESOLUTIONS.get(resolution, {}).get("max_side", 0)

    bot: Bot = message.bot

    base_text = (
        f"🎨 <b>Генерирую изображение…</b>\n"
        f"🤖 {model_label}\n"
        f"<i>Промпт: {prompt[:100]}{'…' if len(prompt) > 100 else ''}</i>"
    )
    processing_msg = await message.answer(
        f"{base_text}\n\n◐ <b>Обработка — 0 сек.</b>",
        parse_mode="HTML",
    )

    animator = ProgressAnimator(processing_msg, base_text)
    animator.start()

    async def _do_generate() -> bytes:
        raw = await vertex_service.generate_image(
            prompt=prompt, model_override=user_model, aspect_ratio=aspect_ratio,
            thinking_level=settings.get("thinking_level", "low"),
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
        fname = _prompt_to_filename(prompt)
        result_caption = f"✅ Ваше изображение готово!\n<i>{prompt[:200]}</i>"

        if send_mode == "document":
            doc = BufferedInputFile(file=image_bytes, filename=fname)
            await message.answer_document(document=doc, caption=result_caption, parse_mode="HTML")
        else:
            photo = BufferedInputFile(file=image_bytes, filename=fname)
            await message.answer_photo(photo=photo, caption=result_caption, parse_mode="HTML")

        increment_generations(uid, user.first_name or "")

        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=processing_msg.message_id)
        except Exception:
            pass

    except asyncio.CancelledError:
        await animator.stop()
        clear_active_task(uid)
        try:
            await processing_msg.edit_text("⛔ <b>Генерация отменена.</b>", parse_mode="HTML")
        except Exception:
            pass
    except QuotaExceededError:
        await animator.stop()
        clear_active_task(uid)
        current_name = AVAILABLE_MODELS.get(user_model, {}).get("label", user_model)
        other_name = _other_model_label(user_model)
        await processing_msg.edit_text(
            f"Модель <b>{current_name}</b> сейчас перегружена 😔\n\n"
            f"Попробуйте через пару минут или переключитесь на <b>{other_name}</b>.",
            parse_mode="HTML",
            reply_markup=_suggest_switch_keyboard(user_model),
        )
    except (SafetyFilterError, BotError) as exc:
        await animator.stop()
        clear_active_task(uid)
        await processing_msg.edit_text(f"{exc.user_message}", parse_mode="HTML")
    except Exception:
        await animator.stop()
        clear_active_task(uid)
        await processing_msg.edit_text(
            "Не удалось сгенерировать изображение 😔\nПопробуйте ещё раз.",
            parse_mode="HTML",
        )


@router.callback_query(lambda c: c.data == "creative_auto")
async def creative_auto_complete(callback: CallbackQuery, vertex_service: VertexAIService) -> None:
    uid = callback.from_user.id
    if uid not in _sessions:
        await callback.answer("Сессия не найдена, начните заново.")
        return

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await callback.answer()

    history = _sessions[uid]
    history.append({
        "role": "user",
        "text": "Достаточно вопросов! На основе того что мы обсудили, "
                "додумай остальные детали сам и сразу выдай итоговый промпт "
                "в формате ---PROMPT--- ... ---END---",
    })

    thinking_msg = await callback.message.answer("🪄 <b>Дополняю и создаю промпт...</b>", parse_mode="HTML")

    try:
        contents = _build_contents(history)
        response = await vertex_service.chat_text(contents)

        if not response:
            await thinking_msg.edit_text("Не удалось получить ответ, попробуйте ещё раз.")
            return

        history.append({"role": "model", "text": response})
        extracted = _extract_prompt(response)

        if extracted:
            _final_prompts[uid] = extracted
            display_text = _clean_for_display(response)
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🎨 Генерируй!", callback_data="creative_generate")],
                [InlineKeyboardButton(text="✏️ Изменить", callback_data="creative_edit")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="creative_cancel")],
            ])
            prompt_preview = f"\n\n<b>Промпт:</b>\n<code>{extracted[:500]}</code>"
            await thinking_msg.edit_text(
                f"{display_text}{prompt_preview}",
                parse_mode="HTML",
                reply_markup=kb,
            )
        else:
            await thinking_msg.edit_text(response)

    except Exception as exc:
        logger.exception("Creative auto-complete error: %s", exc)
        try:
            await thinking_msg.edit_text("Произошла ошибка, попробуйте ещё раз.")
        except TelegramBadRequest:
            pass


@router.callback_query(lambda c: c.data == "creative_edit")
async def creative_edit(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    _final_prompts.pop(uid, None)

    if uid in _sessions and _sessions[uid]:
        _sessions[uid].append({"role": "user", "text": "Давай изменим промпт. Что ты предлагаешь улучшить?"})

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await callback.answer()

    await callback.message.answer(
        "✏️ Хорошо! Расскажите, что хотите изменить.",
    )


@router.callback_query(lambda c: c.data == "creative_cancel")
async def creative_cancel(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    _sessions.pop(uid, None)
    _final_prompts.pop(uid, None)

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await callback.answer("Сессия завершена")

    await callback.message.answer(
        "❌ Режим «Идеи» завершён.\n\nМожете отправить промпт напрямую или начать заново.",
        reply_markup=get_persistent_keyboard(),
    )
