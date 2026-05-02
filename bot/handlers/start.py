"""
bot/handlers/start.py
~~~~~~~~~~~~~~~~~~~~~
Handler for the /start, /menu, /settings commands and persistent reply-keyboard buttons.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup

from bot.keyboards import (
    BTN_MENU,
    BTN_STOP,
    BTN_SETTINGS,
    BTN_WEB_CHAT,
    get_persistent_keyboard,
    get_settings_summary_keyboard,
    get_balance_keyboard,
    get_video_panel_text,
)
from bot.user_settings import is_video_model as _is_video_model
from bot.user_settings import (
    get_user_settings,
    set_last_menu,
    save_user_settings,
    cancel_active_task,
    FREE_CREDITS,
    get_chat_daily_count,
    get_chat_daily_limit,
)
from bot.handlers.creative import _sessions as chat_sessions

router = Router(name="start")

BTN_BALANCE = "💰 Баланс"


def _build_menu_text(first_name: str, generations: int, credits: int, blocked: bool) -> str:
    greeting = f"👋 <b>Привет, {first_name}!</b>\n\n" if first_name else "👋 <b>Главное меню</b>\n\n"
    if blocked:
        credit_line = "🚫 <b>Доступ закрыт.</b> Обратитесь к администратору.\n\n"
    else:
        purchased = max(0, credits - FREE_CREDITS) if credits > FREE_CREDITS else 0
        free_left = min(credits, FREE_CREDITS)
        credit_line = (
            f"┌─────────────────────\n"
            f"│ 🔋 <b>Баланс: {credits} кредитов</b>\n"
        )
        if purchased > 0:
            credit_line += f"│ 💎 Купленные: <b>{purchased}</b>\n"
            credit_line += f"│ 🎁 Бесплатные: <b>{free_left}</b>\n"
        else:
            credit_line += f"│ 🎁 Бесплатные: <b>{free_left} из {FREE_CREDITS}</b>\n"
        credit_line += (
            f"│ 🎨 Сгенерировано: <b>{generations}</b>\n"
            f"└─────────────────────\n\n"
        )
    return f"{greeting}{credit_line}Отправьте текст или фото с описанием:"


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    uid = message.from_user.id
    first_name = message.from_user.first_name or ""
    settings = get_user_settings(uid)
    settings["first_name"] = first_name
    if not settings.get("platform"):
        settings["platform"] = "tg"
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
    settings = get_user_settings(uid)
    model_id = settings.get("model", "gemini-3.1-flash-image-preview")
    if _is_video_model(model_id):
        text = get_video_panel_text(uid)
    else:
        text = "⚙️ <b>Настройки</b>\n\nВыберите параметр который хотите изменить:"
    sent = await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=get_settings_summary_keyboard(uid),
    )
    set_last_menu(uid, sent.chat.id, sent.message_id)


@router.message(lambda m: m.text == BTN_BALANCE)
async def cmd_balance(message: Message) -> None:
    uid = message.from_user.id
    settings = get_user_settings(uid)
    credits = settings.get("credits", FREE_CREDITS)
    generations = settings.get("generations_count", 0)
    chat_used = get_chat_daily_count(uid)
    chat_limit = get_chat_daily_limit(uid)

    purchased = max(0, credits - FREE_CREDITS) if credits > FREE_CREDITS else 0
    free_left = min(credits, FREE_CREDITS)

    lines = ["💰 <b>Ваш баланс</b>", ""]
    lines.append("┌─────────────────────")
    lines.append(f"│ 🔋 <b>Кредитов: {credits}</b>")
    if purchased > 0:
        lines.append(f"│ 💎 Купленные: {purchased}")
        lines.append(f"│ 🎁 Бесплатные: {free_left}")
    else:
        lines.append(f"│ 🎁 Бесплатные: {free_left} из {FREE_CREDITS}")
    lines.append(f"│ 🎨 Сгенерировано: {generations}")
    lines.append("└─────────────────────")
    lines.append("")
    lines.append("📋 <b>Стоимость генерации:</b>")
    lines.append("▫️ Фото 2К, Full HD и ниже — <b>1 кредит</b>")
    lines.append("▫️ Фото 4K — <b>2 кредита</b>")
    lines.append("▫️ Lyria 3 Pro (полная песня) — <b>4 кредита</b>")
    lines.append("▫️ Lyria 3 (30 сек.) — <b>2 кредита</b>")
    lines.append("")
    lines.append("💬 <b>Чат с ИИ (в день):</b>")
    lines.append(f"▫️ Использовано: <b>{chat_used}</b> из <b>{chat_limit}</b>")
    lines.append(f"▫️ Дневной лимит: <b>{chat_limit}</b> запросов")
    lines.append("")
    lines.append("💳 <b>Выберите пакет для пополнения:</b>")

    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=get_balance_keyboard(),
    )


@router.message(Command("info"))
async def cmd_info(message: Message) -> None:
    BASE = "https://www.vk-tg-picgenai.ru"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📁 ПУБЛИЧНАЯ ОФЕРТА", url=f"{BASE}/offer")],
        [InlineKeyboardButton(text="📁 Политика обработки данных", url=f"{BASE}/privacy")],
        [InlineKeyboardButton(text="✅ Согласие на обработку", url=f"{BASE}/consent")],
        [InlineKeyboardButton(text="💰 Условия возврата", url=f"{BASE}/refund")],
        [InlineKeyboardButton(text="📁 Назад", callback_data="back_to_settings")],
    ])
    await message.answer(
        "📁 <b>Правовые документы и условия использования:</b>\n\n"
        "Вы можете ознакомиться с нашими документами по ссылкам ниже:",
        parse_mode="HTML",
        reply_markup=kb,
    )


_WEB_CHAT_BASE = "https://www.vk-tg-picgenai.ru"


@router.message(Command("webchat"))
@router.message(lambda m: m.text == BTN_WEB_CHAT)
async def cmd_web_chat(message: Message) -> None:
    """Issue a fresh login code and DM the user a prefilled web-chat link."""
    from bot.web_chat import issue_login_code

    uid = message.from_user.id
    code, err = await issue_login_code(uid, "tg")
    if not code:
        await message.answer(err or "Не удалось создать код. Попробуйте позже.")
        return

    link = f"{_WEB_CHAT_BASE}/chat?platform=tg&uid={uid}"
    await message.answer(
        "🌐 <b>Веб-версия чата PicGenAI</b>\n\n"
        f"1. Откройте по ссылке: {link}\n"
        "2. Введите шестизначный код ниже\n\n"
        "Код действует 5 минут. Кредиты и история — общие с этим ботом.",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await message.answer(f"<code>{code}</code>", parse_mode="HTML")


@router.message(Command("cancel"))
@router.message(lambda m: m.text == BTN_STOP)
async def cmd_stop(message: Message) -> None:
    uid = message.from_user.id
    cancelled = cancel_active_task(uid)
    was_chat = uid in chat_sessions
    chat_sessions.pop(uid, None)

    if cancelled or was_chat:
        text = "⛔ <b>Отменено.</b>\n\nОтправьте новый промпт или откройте меню."
        if was_chat:
            text = "⛔ <b>Чат завершён.</b>\n\nОтправьте промпт для генерации или начните чат заново."
        await message.answer(text, parse_mode="HTML")
    else:
        await message.answer(
            "ℹ️ Нет активной генерации для отмены.",
            parse_mode="HTML",
        )
