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
    get_balance_keyboard,
    get_video_duration_keyboard,
    get_video_resolution_keyboard,
    get_video_aspect_keyboard,
    get_video_panel_text,
    get_video_panel_keyboard,
    get_video_task_keyboard,
)
from bot.user_settings import (
    user_settings, get_user_settings, set_last_menu, save_user_settings,
    AVAILABLE_MODELS, SEND_MODES, RESOLUTIONS, THINKING_LEVELS,
    VIDEO_DURATIONS, VIDEO_RESOLUTIONS, VIDEO_ASPECT_RATIOS, VIDEO_TASKS,
    is_video_model, get_available_tasks_for_model,
)
from bot.services.lava_service import create_payment_url, CREDIT_PACKAGES

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


_SETTINGS_TEXT = "⚙️ <b>Настройки</b>\n\nВыберите параметр который хотите изменить:"


def _video_settings_text(uid: int) -> str:
    from bot.keyboards import get_video_panel_text
    return get_video_panel_text(uid)


def _get_settings_text(uid: int) -> str:
    settings = get_user_settings(uid)
    model_id = settings.get("model", "gemini-3.1-flash-image-preview")
    if is_video_model(model_id):
        return _video_settings_text(uid)
    return _SETTINGS_TEXT


@router.callback_query(lambda c: c.data == "back_to_settings")
async def back_to_settings(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    await _safe_edit(callback, _get_settings_text(uid), reply_markup=get_settings_summary_keyboard(uid))
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

    await _safe_edit(callback, _get_settings_text(uid), reply_markup=get_settings_summary_keyboard(uid))


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


@router.callback_query(lambda c: c.data and c.data.startswith("buy_"))
async def buy_credits(callback: CallbackQuery) -> None:
    pack_key = callback.data.replace("buy_", "", 1)
    pack = CREDIT_PACKAGES.get(pack_key)
    if not pack:
        await callback.answer("Неизвестный пакет")
        return

    result = await create_payment_url(callback.from_user.id, pack_key, source="tg")
    if result["ok"]:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Перейти к оплате", url=result["pay_url"])],
        ])
        await _safe_edit(
            callback,
            f"💳 <b>Оплата: {pack['label']}</b>\n\n"
            "Нажмите кнопку ниже для перехода к оплате.\n"
            "Кредиты будут начислены автоматически после оплаты.",
            reply_markup=kb,
        )
    else:
        await callback.answer(f"Ошибка: {result.get('error', 'неизвестная')}", show_alert=True)
    await callback.answer()


@router.callback_query(lambda c: c.data == "noop")
async def noop_callback(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(lambda c: c.data == "open_video_panel")
async def open_video_panel(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    await _safe_edit(callback, get_video_panel_text(uid), reply_markup=get_video_panel_keyboard(uid))
    await callback.answer()


@router.callback_query(lambda c: c.data == "choose_video_duration")
async def choose_video_duration(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    lines = ["⏱ <b>Длительность видео:</b>\n"]
    for dur, info in VIDEO_DURATIONS.items():
        lines.append(f"  {info['label']}\n  <i>{info['desc']}</i>\n")
    await _safe_edit(callback, "\n".join(lines), reply_markup=get_video_duration_keyboard(uid))
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("vdur_"))
async def set_video_duration(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    try:
        dur = int(callback.data.replace("vdur_", "", 1))
    except ValueError:
        await callback.answer("Неверная длительность")
        return
    if dur not in VIDEO_DURATIONS:
        await callback.answer("Неизвестная длительность")
        return
    settings = get_user_settings(uid)
    settings["video_duration"] = dur
    save_user_settings(uid)
    await callback.answer(f"Длительность: {VIDEO_DURATIONS[dur]['label']}")
    await _safe_edit(callback, _SETTINGS_TEXT, reply_markup=get_settings_summary_keyboard(uid))


@router.callback_query(lambda c: c.data and c.data.startswith("vp_aspect_"))
async def vp_set_aspect(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    key = callback.data.replace("vp_aspect_", "", 1)
    if key not in VIDEO_ASPECT_RATIOS:
        await callback.answer("Неизвестный формат")
        return
    settings = get_user_settings(uid)
    settings["video_aspect_ratio"] = key
    save_user_settings(uid)
    await callback.answer(f"Формат: {VIDEO_ASPECT_RATIOS[key]}")
    await _safe_edit(callback, _video_settings_text(uid), reply_markup=get_settings_summary_keyboard(uid))


@router.callback_query(lambda c: c.data and c.data.startswith("vp_dur_"))
async def vp_set_duration(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    try:
        dur = int(callback.data.replace("vp_dur_", "", 1))
    except ValueError:
        await callback.answer()
        return
    if dur not in VIDEO_DURATIONS:
        await callback.answer("Неизвестная длительность")
        return
    settings = get_user_settings(uid)
    settings["video_duration"] = dur
    save_user_settings(uid)
    await callback.answer(f"Длительность: {dur} сек")
    await _safe_edit(callback, _video_settings_text(uid), reply_markup=get_settings_summary_keyboard(uid))


@router.callback_query(lambda c: c.data and c.data.startswith("vp_res_"))
async def vp_set_resolution(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    res_id = callback.data.replace("vp_res_", "", 1)
    if res_id not in VIDEO_RESOLUTIONS:
        await callback.answer("Неизвестное разрешение")
        return
    settings = get_user_settings(uid)
    from bot.user_settings import get_video_resolutions_for_model
    model_id = settings.get("model", "")
    avail = get_video_resolutions_for_model(model_id)
    if res_id not in avail:
        await callback.answer("Это разрешение недоступно для текущей модели")
        return
    settings["video_resolution"] = res_id
    save_user_settings(uid)
    await callback.answer(f"Разрешение: {res_id}")
    await _safe_edit(callback, _video_settings_text(uid), reply_markup=get_settings_summary_keyboard(uid))


@router.callback_query(lambda c: c.data == "vp_audio")
async def vp_toggle_audio(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    settings = get_user_settings(uid)
    from bot.user_settings import video_supports_audio
    model_id = settings.get("model", "")
    if not video_supports_audio(model_id):
        await callback.answer("Эта модель не поддерживает аудио", show_alert=True)
        return
    current = settings.get("video_audio", True)
    settings["video_audio"] = not current
    save_user_settings(uid)
    state = "Вкл" if not current else "Выкл"
    await callback.answer(f"Аудио: {state}")
    await _safe_edit(callback, _video_settings_text(uid), reply_markup=get_settings_summary_keyboard(uid))


@router.callback_query(lambda c: c.data == "choose_video_task")
async def choose_video_task(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    lines = ["🎯 <b>Тип задачи:</b>\n"]
    settings = get_user_settings(uid)
    model_id = settings.get("model", "")
    avail = get_available_tasks_for_model(model_id)
    for tid, tinfo in avail.items():
        suffix = " (скоро)" if tinfo.get("coming_soon") else ""
        lines.append(f"  {tinfo['label']}{suffix}\n  <i>{tinfo['desc']}</i>\n")
    await _safe_edit(callback, "\n".join(lines), reply_markup=get_video_task_keyboard(uid))
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("vtask_"))
async def set_video_task(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    task_id = callback.data.replace("vtask_", "", 1)
    if task_id not in VIDEO_TASKS:
        await callback.answer("Неизвестная задача")
        return
    task_info = VIDEO_TASKS[task_id]
    if task_info.get("coming_soon"):
        await callback.answer("Эта функция пока недоступна", show_alert=True)
        return
    settings = get_user_settings(uid)
    model_id = settings.get("model", "")
    avail = get_available_tasks_for_model(model_id)
    if task_id not in avail:
        await callback.answer("Задача недоступна для этой модели", show_alert=True)
        return
    settings["video_task"] = task_id
    save_user_settings(uid)
    await callback.answer(f"Задача: {task_info['label']}")
    await _safe_edit(callback, _video_settings_text(uid), reply_markup=get_settings_summary_keyboard(uid))


@router.callback_query(lambda c: c.data == "choose_video_resolution")
async def choose_video_resolution(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    lines = ["📺 <b>Разрешение видео:</b>\n"]
    for res_id, info in VIDEO_RESOLUTIONS.items():
        lines.append(f"  {info['label']}\n  <i>{info['desc']}</i>\n")
    await _safe_edit(callback, "\n".join(lines), reply_markup=get_video_resolution_keyboard(uid))
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("vres_"))
async def set_video_resolution(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    res_id = callback.data.replace("vres_", "", 1)
    if res_id not in VIDEO_RESOLUTIONS:
        await callback.answer("Неизвестное разрешение")
        return
    settings = get_user_settings(uid)
    settings["video_resolution"] = res_id
    save_user_settings(uid)
    await callback.answer(f"Разрешение: {VIDEO_RESOLUTIONS[res_id]['label']}")
    await _safe_edit(callback, _SETTINGS_TEXT, reply_markup=get_settings_summary_keyboard(uid))


@router.callback_query(lambda c: c.data == "choose_video_aspect")
async def choose_video_aspect(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    await _safe_edit(
        callback,
        "📐 <b>Формат видео:</b>\n\nВидео поддерживает только 16:9 и 9:16",
        reply_markup=get_video_aspect_keyboard(uid),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("vaspect_"))
async def set_video_aspect(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    key = callback.data.replace("vaspect_", "", 1)
    if key not in VIDEO_ASPECT_RATIOS:
        await callback.answer("Неизвестный формат")
        return
    settings = get_user_settings(uid)
    settings["video_aspect_ratio"] = key
    save_user_settings(uid)
    await callback.answer(f"Формат: {VIDEO_ASPECT_RATIOS[key]}")
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
