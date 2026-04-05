"""
bot/handlers/start.py
~~~~~~~~~~~~~~~~~~~~~
Handler for the /start, /menu, /settings commands and persistent reply-keyboard buttons.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import CommandStart, Command
from aiogram.types import Message

from bot.keyboards import (
    BTN_MENU,
    BTN_STOP,
    BTN_SETTINGS,
    get_persistent_keyboard,
    get_settings_summary_keyboard,
)
from bot.user_settings import (
    get_user_settings,
    set_last_menu,
    save_user_settings,
    cancel_active_task,
    FREE_CREDITS,
)
from bot.handlers.creative import _sessions as creative_sessions, _final_prompts as creative_prompts

router = Router(name="start")


def _build_menu_text(first_name: str, generations: int, credits: int, blocked: bool) -> str:
    greeting = f"👋 <b>Привет, {first_name}!</b>\n\n" if first_name else "👋 <b>Главное меню</b>\n\n"
    used = max(0, FREE_CREDITS - credits)
    if blocked:
        credit_line = "🚫 <b>Доступ закрыт.</b> Обратитесь к администратору.\n\n"
    else:
        credit_line = (
            f"💳 Бесплатных кредитов выдано: <b>{FREE_CREDITS}</b>\n"
            f"🎨 Использовано: <b>{used}</b>\n"
            f"🔋 Осталось: <b>{credits}</b>\n\n"
        )
    return f"{greeting}{credit_line}Отправьте текст или фото с описанием:"


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    uid = message.from_user.id
    first_name = message.from_user.first_name or ""
    settings = get_user_settings(uid)
    settings["first_name"] = first_name
    save_user_settings(uid)
    generations = settings.get("generations_count", 0)
    credits = settings.get("credits", FREE_CREDITS)
    blocked = settings.get("blocked", False)

    await message.answer(
        "⌨️ Клавиатура активирована",
        reply_markup=get_persistent_keyboard(),
    )
    await message.answer(
        _build_menu_text(first_name, generations, credits, blocked),
        parse_mode="HTML",
    )


@router.message(Command("menu"))
@router.message(lambda m: m.text == BTN_MENU)
async def cmd_menu(message: Message) -> None:
    uid = message.from_user.id
    first_name = message.from_user.first_name or ""
    settings = get_user_settings(uid)
    settings["first_name"] = first_name
    credits = settings.get("credits", FREE_CREDITS)
    blocked = settings.get("blocked", False)
    generations = settings.get("generations_count", 0)

    await message.answer(
        _build_menu_text(first_name, generations, credits, blocked),
        parse_mode="HTML",
    )


@router.message(Command("settings"))
@router.message(lambda m: m.text == BTN_SETTINGS)
async def cmd_settings(message: Message) -> None:
    uid = message.from_user.id
    sent = await message.answer(
        "⚙️ <b>Настройки</b>\n\nВыберите параметр который хотите изменить:",
        parse_mode="HTML",
        reply_markup=get_settings_summary_keyboard(uid),
    )
    set_last_menu(uid, sent.chat.id, sent.message_id)


@router.message(Command("cancel"))
@router.message(lambda m: m.text == BTN_STOP)
async def cmd_stop(message: Message) -> None:
    uid = message.from_user.id
    cancelled = cancel_active_task(uid)
    was_creative = uid in creative_sessions
    creative_sessions.pop(uid, None)
    creative_prompts.pop(uid, None)

    if cancelled or was_creative:
        text = "⛔ <b>Отменено.</b>\n\nОтправьте новый промпт или откройте меню."
        if was_creative:
            text = "⛔ <b>Режим «Идеи» завершён.</b>\n\nОтправьте промпт или начните заново."
        await message.answer(text, parse_mode="HTML")
    else:
        await message.answer(
            "ℹ️ Нет активной генерации для отмены.",
            parse_mode="HTML",
        )
