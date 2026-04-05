"""
bot/handlers/callbacks.py
~~~~~~~~~~~~~~~~~~~~~~~~~~
Inline keyboard callback handlers for configuration menus.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from bot.keyboards import (
    ASPECT_RATIOS,
    get_model_keyboard,
    get_aspect_ratio_keyboard,
    get_send_mode_keyboard,
    get_resolution_keyboard,
    get_thinking_level_keyboard,
    get_settings_summary_keyboard,
)
from bot.user_settings import (
    user_settings, get_user_settings, set_last_menu, save_user_settings,
    AVAILABLE_MODELS, SEND_MODES, RESOLUTIONS, THINKING_LEVELS,
)

logger = logging.getLogger(__name__)
router = Router(name="callbacks")


async def _safe_edit(callback: CallbackQuery, text: str, reply_markup=None) -> None:
    try:
        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=reply_markup,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            pass
        else:
            raise
    set_last_menu(
        callback.from_user.id,
        callback.message.chat.id,
        callback.message.message_id,
    )


_SETTINGS_TEXT = "⚙️ <b>Настройки</b>\n<code>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</code>\nВыберите что изменить:"


@router.callback_query(lambda c: c.data == "back_to_settings")
async def back_to_settings(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    await _safe_edit(callback, _SETTINGS_TEXT, reply_markup=get_settings_summary_keyboard(uid))
    await callback.answer()


@router.callback_query(lambda c: c.data == "choose_model")
async def choose_model(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    lines = ["🤖 <b>Выберите модель:</b>\n"]
    for model_id, info in AVAILABLE_MODELS.items():
        lines.append(f"  {info['label']}\n  <i>{info['desc']}</i>\n")
    await _safe_edit(
        callback,
        "\n".join(lines),
        reply_markup=get_model_keyboard(uid),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("model_"))
async def set_model(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    model_id = callback.data.replace("model_", "", 1)
    settings = get_user_settings(uid)

    if model_id not in AVAILABLE_MODELS:
        await callback.answer("Неизвестная модель")
        return

    settings["model"] = model_id
    save_user_settings(uid)
    info = AVAILABLE_MODELS[model_id]
    await callback.answer(f"Модель: {info['label']}")

    await _safe_edit(callback, _SETTINGS_TEXT, reply_markup=get_settings_summary_keyboard(uid))


@router.callback_query(lambda c: c.data == "choose_aspect")
async def choose_aspect_ratio(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    await _safe_edit(
        callback,
        "📐 <b>Выберите соотношение сторон:</b>",
        reply_markup=get_aspect_ratio_keyboard(uid, 0),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("aspect_page_"))
async def aspect_ratio_page(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    try:
        page = int(callback.data.replace("aspect_page_", "", 1))
    except ValueError:
        await callback.answer()
        return
    await _safe_edit(
        callback,
        "📐 <b>Выберите соотношение сторон:</b>",
        reply_markup=get_aspect_ratio_keyboard(uid, page),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("aspect_") and not c.data.startswith("aspect_page_"))
async def set_aspect_ratio(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    ratio_key = callback.data.replace("aspect_", "")
    settings = get_user_settings(uid)

    if ratio_key in ASPECT_RATIOS:
        settings["aspect_ratio"] = ratio_key
        save_user_settings(uid)
        label = ASPECT_RATIOS[ratio_key]
        await callback.answer(f"Установлено: {label}")
    else:
        await callback.answer("Неизвестный формат")
        return

    await _safe_edit(callback, _SETTINGS_TEXT, reply_markup=get_settings_summary_keyboard(uid))


@router.callback_query(lambda c: c.data == "choose_thinking")
async def choose_thinking_level(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    lines = ["🧠 <b>Уровень мышления (Flash):</b>\n"]
    for level_id, info in THINKING_LEVELS.items():
        lines.append(f"  {info['label']}\n  <i>{info['desc']}</i>\n")
    await _safe_edit(
        callback,
        "\n".join(lines),
        reply_markup=get_thinking_level_keyboard(uid),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("thinking_"))
async def set_thinking_level(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    level_id = callback.data.replace("thinking_", "", 1)
    settings = get_user_settings(uid)

    if level_id not in THINKING_LEVELS:
        await callback.answer("Неизвестный уровень")
        return

    settings["thinking_level"] = level_id
    save_user_settings(uid)
    info = THINKING_LEVELS[level_id]
    await callback.answer(f"Мышление: {info['label']}")

    await _safe_edit(callback, _SETTINGS_TEXT, reply_markup=get_settings_summary_keyboard(uid))


@router.callback_query(lambda c: c.data == "choose_send_mode")
async def choose_send_mode(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    lines = ["📤 <b>Формат отправки:</b>\n"]
    for mode_id, info in SEND_MODES.items():
        lines.append(f"  {info['label']}\n  <i>{info['desc']}</i>\n")
    await _safe_edit(
        callback,
        "\n".join(lines),
        reply_markup=get_send_mode_keyboard(uid),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("sendmode_"))
async def set_send_mode(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    mode_id = callback.data.replace("sendmode_", "", 1)
    settings = get_user_settings(uid)

    if mode_id not in SEND_MODES:
        await callback.answer("Неизвестный формат")
        return

    settings["send_mode"] = mode_id
    save_user_settings(uid)
    info = SEND_MODES[mode_id]
    await callback.answer(f"Формат: {info['label']}")

    await _safe_edit(callback, _SETTINGS_TEXT, reply_markup=get_settings_summary_keyboard(uid))


@router.callback_query(lambda c: c.data == "choose_resolution")
async def choose_resolution(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    lines = ["🔍 <b>Выберите качество (разрешение):</b>\n"]
    for res_id, info in RESOLUTIONS.items():
        lines.append(f"  {info['label']}\n  <i>{info['desc']}</i>\n")
    await _safe_edit(
        callback,
        "\n".join(lines),
        reply_markup=get_resolution_keyboard(uid),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("res_"))
async def set_resolution(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    res_id = callback.data.replace("res_", "", 1)
    settings = get_user_settings(uid)

    if res_id not in RESOLUTIONS:
        await callback.answer("Неизвестное разрешение")
        return

    settings["resolution"] = res_id
    save_user_settings(uid)
    info = RESOLUTIONS[res_id]
    await callback.answer(f"Качество: {info['label']}")

    await _safe_edit(callback, _SETTINGS_TEXT, reply_markup=get_settings_summary_keyboard(uid))


@router.callback_query(lambda c: c.data and c.data.startswith("switch_model_"))
async def switch_model_from_error(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    model_id = callback.data.replace("switch_model_", "", 1)
    settings = get_user_settings(uid)

    if model_id not in AVAILABLE_MODELS:
        await callback.answer("Неизвестная модель")
        return

    settings["model"] = model_id
    save_user_settings(uid)
    info = AVAILABLE_MODELS[model_id]
    await callback.answer(f"✅ Модель переключена на {info['label']}")

    try:
        await callback.message.edit_text(
            f"✅ Модель переключена на <b>{info['label']}</b>\n\n"
            "Отправьте запрос ещё раз — теперь будет использоваться новая модель.",
            parse_mode="HTML",
        )
    except TelegramBadRequest:
        pass
