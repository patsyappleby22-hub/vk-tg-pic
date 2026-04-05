"""
bot/handlers/admin.py
~~~~~~~~~~~~~~~~~~~~~~
Admin panel accessible via /adminmrxgyt command with password protection.
Supports managing Google API keys for Vertex AI authentication.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Router, Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup

from bot.user_settings import (
    user_settings, AVAILABLE_MODELS,
    add_credits, set_blocked, get_user_settings, FREE_CREDITS,
)
from bot import api_keys_store

logger = logging.getLogger(__name__)
router = Router(name="admin")

ADMIN_PASSWORD = "mrxgyt02"

_admin_sessions: set[int] = set()
_pending_key_input: set[int] = set()


def _is_admin(user_id: int) -> bool:
    return user_id in _admin_sessions


def _get_admin_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="adm_users")],
        [InlineKeyboardButton(text="🔑 API ключи", callback_data="adm_keys")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats")],
        [InlineKeyboardButton(text="🚪 Выйти", callback_data="adm_logout")],
    ])


def _keys_status_text() -> str:
    all_keys = api_keys_store.get_all_keys()

    if not all_keys:
        return (
            "🔑 <b>API ключи</b>\n\n"
            "❌ Нет активных ключей.\n\n"
            "Добавьте Google Cloud API ключ через кнопку ниже."
        )

    lines = [f"🔑 <b>API ключи</b> (всего: {len(all_keys)})\n"]
    for i, key in enumerate(all_keys):
        lines.append(f"<b>{i + 1}.</b> <code>{api_keys_store.mask_key(key)}</code>")

    return "\n".join(lines)


def _get_keys_keyboard() -> InlineKeyboardMarkup:
    all_keys = api_keys_store.get_all_keys()

    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="➕ Добавить ключ", callback_data="adm_add_key")],
    ]
    for i, key in enumerate(all_keys):
        rows.append([
            InlineKeyboardButton(
                text=f"🗑 {api_keys_store.mask_key(key)}",
                callback_data=f"adm_del_key_{i}",
            )
        ])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


_PLATFORM_ICON = {"tg": "✈️ TG", "vk": "🔵 VK"}


def _users_text(page: int = 0, per_page: int = 10) -> tuple[str, InlineKeyboardMarkup]:
    all_users = list(user_settings.items())
    total = len(all_users)
    total_gens = sum(s.get("generations_count", 0) for _, s in all_users)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    chunk = all_users[start:start + per_page]

    lines = [f"👥 <b>Пользователи</b> ({total} чел. · {total_gens} генераций)\n"]
    user_buttons: list[list[InlineKeyboardButton]] = []

    for uid, s in chunk:
        name = s.get("first_name", "") or "—"
        platform = s.get("platform", "")
        platform_label = _PLATFORM_ICON.get(platform, "❓")
        gens = s.get("generations_count", 0)
        credits = s.get("credits", FREE_CREDITS)
        blocked = s.get("blocked", False)
        status_icon = "🚫" if blocked else "✅"
        lines.append(
            f"{platform_label} · <b>{name}</b> · <code>{uid}</code>\n"
            f"  🎨 {gens} ген. · 💳 {credits} кр. · {status_icon}\n"
        )
        btn_label = f"{'🚫 ' if blocked else ''}{name} ({credits} кр.)"
        user_buttons.append([
            InlineKeyboardButton(text=btn_label, callback_data=f"adm_user_{uid}")
        ])

    lines.append(f"\nСтраница {page + 1}/{total_pages}")

    nav_buttons: list[InlineKeyboardButton] = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀️", callback_data=f"adm_users_p_{page - 1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text="▶️", callback_data=f"adm_users_p_{page + 1}"))

    rows: list[list[InlineKeyboardButton]] = user_buttons[:]
    if nav_buttons:
        rows.append(nav_buttons)
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")])

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _user_card_text(uid: int) -> str:
    s = get_user_settings(uid)
    name = s.get("first_name", "") or "—"
    platform = s.get("platform", "")
    platform_label = _PLATFORM_ICON.get(platform, "❓")
    gens = s.get("generations_count", 0)
    credits = s.get("credits", FREE_CREDITS)
    blocked = s.get("blocked", False)
    model = s.get("model", "")
    model_label = AVAILABLE_MODELS.get(model, {}).get("label", model)
    status = "🚫 Заблокирован" if blocked else "✅ Активен"

    return (
        f"👤 <b>Карточка пользователя</b>\n\n"
        f"Имя: <b>{name}</b>\n"
        f"ID: <code>{uid}</code>\n"
        f"Платформа: {platform_label}\n"
        f"Модель: {model_label}\n"
        f"Генераций: <b>{gens}</b>\n"
        f"Кредиты: <b>{credits}</b>\n"
        f"Статус: {status}"
    )


def _get_user_card_keyboard(uid: int, page: int = 0) -> InlineKeyboardMarkup:
    s = get_user_settings(uid)
    blocked = s.get("blocked", False)
    block_text = "✅ Разблокировать" if blocked else "🚫 Заблокировать"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ +30 кредитов", callback_data=f"adm_addcr_{uid}")],
        [InlineKeyboardButton(text=block_text, callback_data=f"adm_blk_{uid}")],
        [InlineKeyboardButton(text="◀️ К списку", callback_data=f"adm_users_p_{page}")],
    ])


def _stats_text() -> str:
    total_users = len(user_settings)
    total_gens = sum(s.get("generations_count", 0) for s in user_settings.values())

    tg_users = sum(1 for s in user_settings.values() if s.get("platform") == "tg")
    vk_users = sum(1 for s in user_settings.values() if s.get("platform") == "vk")
    unknown_users = total_users - tg_users - vk_users

    tg_gens = sum(s.get("generations_count", 0) for s in user_settings.values() if s.get("platform") == "tg")
    vk_gens = sum(s.get("generations_count", 0) for s in user_settings.values() if s.get("platform") == "vk")

    model_counts: dict[str, int] = {}
    for s in user_settings.values():
        m = s.get("model", "unknown")
        model_counts[m] = model_counts.get(m, 0) + 1

    lines = [
        "📊 <b>Статистика</b>\n",
        f"👥 Пользователей: <b>{total_users}</b>",
        f"  ✈️ Telegram: {tg_users} чел. · {tg_gens} ген.",
        f"  🔵 ВКонтакте: {vk_users} чел. · {vk_gens} ген.",
    ]
    if unknown_users:
        lines.append(f"  ❓ Неизвестно: {unknown_users} чел.")

    lines += [
        "",
        f"🎨 Генераций всего: <b>{total_gens}</b>",
        "",
        "<b>Модели:</b>",
    ]
    for m, cnt in model_counts.items():
        label = AVAILABLE_MODELS.get(m, {}).get("label", m)
        lines.append(f"  {label}: {cnt} чел.")

    active_keys = len(api_keys_store.get_all_keys())
    lines.append(f"\n🔑 API ключей активно: {active_keys}")

    return "\n".join(lines)


@router.message(Command("adminmrxgyt"))
async def admin_login(message: Message) -> None:
    args = message.text.split(maxsplit=1)
    password = args[1].strip() if len(args) > 1 else ""

    try:
        await message.delete()
    except TelegramBadRequest:
        pass

    if password != ADMIN_PASSWORD:
        await message.answer("❌ Неверный пароль.")
        return

    _admin_sessions.add(message.from_user.id)
    await message.answer(
        "🔐 <b>Админ-панель</b>\n\nДобро пожаловать!",
        reply_markup=_get_admin_main_keyboard(),
    )


@router.callback_query(lambda c: c.data == "adm_back")
async def admin_back(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа")
        return
    _pending_key_input.discard(callback.from_user.id)
    try:
        await callback.message.edit_text(
            "🔐 <b>Админ-панель</b>\n\nВыберите раздел:",
            reply_markup=_get_admin_main_keyboard(),
        )
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(lambda c: c.data == "adm_users")
async def admin_users(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа")
        return
    text, kb = _users_text(page=0)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("adm_users_p_"))
async def admin_users_page(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа")
        return
    page = int(callback.data.replace("adm_users_p_", ""))
    text, kb = _users_text(page=page)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(lambda c: c.data == "adm_keys")
async def admin_keys(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа")
        return
    try:
        await callback.message.edit_text(
            _keys_status_text(),
            reply_markup=_get_keys_keyboard(),
        )
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(lambda c: c.data == "adm_add_key")
async def admin_add_key_prompt(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа")
        return
    _pending_key_input.add(callback.from_user.id)
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_cancel_key")],
    ])
    try:
        await callback.message.edit_text(
            "🔑 <b>Добавить API ключ</b>\n\n"
            "Отправьте ваш Google Cloud API ключ следующим сообщением.\n"
            "Он начинается с <code>AIza</code>.",
            reply_markup=cancel_kb,
        )
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(lambda c: c.data == "adm_cancel_key")
async def admin_cancel_key(callback: CallbackQuery) -> None:
    _pending_key_input.discard(callback.from_user.id)
    try:
        await callback.message.edit_text(
            _keys_status_text(),
            reply_markup=_get_keys_keyboard(),
        )
    except TelegramBadRequest:
        pass
    await callback.answer("Отменено")


@router.callback_query(lambda c: c.data and c.data.startswith("adm_del_key_"))
async def admin_delete_key(callback: CallbackQuery, **kwargs: Any) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа")
        return

    try:
        idx = int(callback.data.replace("adm_del_key_", ""))
    except ValueError:
        await callback.answer("Ошибка")
        return

    removed = api_keys_store.remove_key(idx)
    if removed:
        from bot.services.vertex_ai_service import VertexAIService
        vertex_service: VertexAIService | None = kwargs.get("vertex_service")
        if vertex_service:
            vertex_service.reload_keys()
        await callback.answer("🗑 Ключ удалён")
    else:
        await callback.answer("Ключ не найден")

    try:
        await callback.message.edit_text(
            _keys_status_text(),
            reply_markup=_get_keys_keyboard(),
        )
    except TelegramBadRequest:
        pass


@router.message(lambda m: m.from_user and m.from_user.id in _pending_key_input and m.text)
async def admin_receive_key(message: Message, **kwargs: Any) -> None:
    uid = message.from_user.id
    _pending_key_input.discard(uid)

    key = (message.text or "").strip()

    try:
        await message.delete()
    except TelegramBadRequest:
        pass

    if not key:
        await message.answer(
            "⚠️ Пустой ключ. Попробуйте ещё раз.",
            reply_markup=_get_admin_main_keyboard(),
        )
        return

    added = api_keys_store.add_key(key)

    if not added:
        await message.answer(
            "⚠️ Этот ключ уже добавлен.",
            reply_markup=_get_keys_keyboard(),
        )
        return

    from bot.services.vertex_ai_service import VertexAIService
    vertex_service: VertexAIService | None = kwargs.get("vertex_service")
    if vertex_service:
        vertex_service.reload_keys()

    total = len(api_keys_store.get_all_keys())
    await message.answer(
        f"✅ API ключ добавлен!\n\n"
        f"🔑 <code>{api_keys_store.mask_key(key)}</code>\n\n"
        f"Всего активных ключей: {total}\nПрименено без перезагрузки.",
        parse_mode="HTML",
        reply_markup=_get_keys_keyboard(),
    )


@router.callback_query(lambda c: c.data == "adm_stats")
async def admin_stats(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")],
    ])
    try:
        await callback.message.edit_text(_stats_text(), reply_markup=kb)
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("adm_user_"))
async def admin_user_card(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа")
        return
    try:
        uid = int(callback.data.replace("adm_user_", ""))
    except ValueError:
        await callback.answer("Ошибка")
        return
    try:
        await callback.message.edit_text(
            _user_card_text(uid),
            reply_markup=_get_user_card_keyboard(uid),
        )
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("adm_addcr_"))
async def admin_add_credits(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа")
        return
    try:
        uid = int(callback.data.replace("adm_addcr_", ""))
    except ValueError:
        await callback.answer("Ошибка")
        return
    new_balance = add_credits(uid, 30)
    try:
        await callback.message.edit_text(
            _user_card_text(uid),
            reply_markup=_get_user_card_keyboard(uid),
        )
    except TelegramBadRequest:
        pass
    await callback.answer(f"✅ +30 кредитов. Теперь: {new_balance}")


@router.callback_query(lambda c: c.data and c.data.startswith("adm_blk_"))
async def admin_toggle_block(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа")
        return
    try:
        uid = int(callback.data.replace("adm_blk_", ""))
    except ValueError:
        await callback.answer("Ошибка")
        return
    s = get_user_settings(uid)
    currently_blocked = s.get("blocked", False)
    set_blocked(uid, not currently_blocked)
    action = "разблокирован" if currently_blocked else "заблокирован"
    try:
        await callback.message.edit_text(
            _user_card_text(uid),
            reply_markup=_get_user_card_keyboard(uid),
        )
    except TelegramBadRequest:
        pass
    await callback.answer(f"{'✅' if currently_blocked else '🚫'} Пользователь {action}")


@router.callback_query(lambda c: c.data == "adm_logout")
async def admin_logout(callback: CallbackQuery) -> None:
    _admin_sessions.discard(callback.from_user.id)
    _pending_key_input.discard(callback.from_user.id)
    try:
        await callback.message.edit_text("🔐 Вы вышли из админ-панели.")
    except TelegramBadRequest:
        pass
    await callback.answer("Вы вышли")
