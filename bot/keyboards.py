"""
bot/keyboards.py
~~~~~~~~~~~~~~~~~
Inline keyboard layouts for the Telegram bot.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from bot.user_settings import get_user_settings, AVAILABLE_MODELS, SEND_MODES, RESOLUTIONS, THINKING_LEVELS

BTN_MENU = "📋 Меню"
BTN_STOP = "⛔ Стоп"
BTN_SETTINGS = "⚙️ Настройки"
BTN_CHAT = "💬 Чат"
BTN_BALANCE = "💰 Баланс"

ASPECT_RATIOS: dict[str, str] = {
    "1:1": "1:1 (Квадрат)",
    "16:9": "16:9 (Широкий)",
    "9:16": "9:16 (Вертикальный)",
    "4:3": "4:3 (Стандартный)",
    "3:4": "3:4 (Портрет)",
    "3:2": "3:2 (Фото)",
    "2:3": "2:3 (Книга)",
    "4:5": "4:5 (Инстаграм)",
    "5:4": "5:4 (Печать)",
    "21:9": "21:9 (Кинематограф)",
}


def get_persistent_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_MENU), KeyboardButton(text=BTN_CHAT)],
            [KeyboardButton(text=BTN_SETTINGS), KeyboardButton(text=BTN_BALANCE)],
            [KeyboardButton(text=BTN_STOP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def _model_short_label(model_id: str) -> str:
    info = AVAILABLE_MODELS.get(model_id)
    return info["label"] if info else model_id


def _is_pro_model(model_id: str) -> bool:
    return "flash" not in model_id.lower()


def get_model_keyboard(user_id: int) -> InlineKeyboardMarkup:
    settings = get_user_settings(user_id)
    current = settings.get("model", "gemini-3.1-flash-image-preview")

    rows: list[list[InlineKeyboardButton]] = []
    for model_id, info in AVAILABLE_MODELS.items():
        label = info["label"]
        if model_id == current:
            label = "✅ " + label
        rows.append([
            InlineKeyboardButton(text=label, callback_data=f"model_{model_id}")
        ])

    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_aspect_ratio_keyboard(user_id: int, page: int = 0) -> InlineKeyboardMarkup:
    settings = get_user_settings(user_id)
    current = settings.get("aspect_ratio", "1:1")

    items = list(ASPECT_RATIOS.items())
    page_size = 8
    total_pages = (len(items) + page_size - 1) // page_size
    page = max(0, min(page, total_pages - 1))
    page_items = items[page * page_size:(page + 1) * page_size]

    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for key, label in page_items:
        text = f"✅ {label}" if key == current else label
        row.append(InlineKeyboardButton(text=text, callback_data=f"aspect_{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"aspect_page_{page - 1}"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text="➡️", callback_data=f"aspect_page_{page + 1}"))
        if nav:
            rows.append(nav)

    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_send_mode_keyboard(user_id: int) -> InlineKeyboardMarkup:
    settings = get_user_settings(user_id)
    current = settings.get("send_mode", "photo")

    rows: list[list[InlineKeyboardButton]] = []
    for mode_id, info in SEND_MODES.items():
        label = info["label"]
        if mode_id == current:
            label = "✅ " + label
        rows.append([
            InlineKeyboardButton(text=label, callback_data=f"sendmode_{mode_id}")
        ])

    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_resolution_keyboard(user_id: int) -> InlineKeyboardMarkup:
    settings = get_user_settings(user_id)
    current = settings.get("resolution", "original")

    rows: list[list[InlineKeyboardButton]] = []
    for res_id, info in RESOLUTIONS.items():
        label = info["label"]
        if res_id == current:
            label = "✅ " + label
        rows.append([
            InlineKeyboardButton(text=label, callback_data=f"res_{res_id}")
        ])

    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_thinking_level_keyboard(user_id: int) -> InlineKeyboardMarkup:
    settings = get_user_settings(user_id)
    current = settings.get("thinking_level", "low")

    rows: list[list[InlineKeyboardButton]] = []
    for level_id, info in THINKING_LEVELS.items():
        label = info["label"]
        if level_id == current:
            label = "✅ " + label
        rows.append([
            InlineKeyboardButton(text=label, callback_data=f"thinking_{level_id}")
        ])

    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _is_flash_model(model_id: str) -> bool:
    return "flash" in model_id.lower() and "lite" not in model_id.lower()


def get_settings_summary_keyboard(user_id: int) -> InlineKeyboardMarkup:
    settings = get_user_settings(user_id)
    current_model = settings.get("model", "gemini-3.1-flash-image-preview")
    model_label = _model_short_label(current_model)
    send_info = SEND_MODES.get(settings.get("send_mode", "photo"), {})
    send_label = send_info.get("label", "🖼 Фото")
    res_info = RESOLUTIONS.get(settings.get("resolution", "original"), {})
    res_label = res_info.get("label", "📷 Оригинал")

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=f"🤖 {model_label}",
                callback_data="choose_model",
            ),
        ],
    ]

    aspect_label = ASPECT_RATIOS.get(settings.get("aspect_ratio", "1:1"), "1:1")
    rows.append([
        InlineKeyboardButton(
            text=f"📐 Размер: {aspect_label}",
            callback_data="choose_aspect",
        ),
    ])

    if _is_flash_model(current_model):
        thinking_info = THINKING_LEVELS.get(settings.get("thinking_level", "low"), {})
        thinking_label = thinking_info.get("label", "💭 Лёгкий")
        rows.append([
            InlineKeyboardButton(
                text=f"🧠 Мышление: {thinking_label}",
                callback_data="choose_thinking",
            ),
        ])

    rows.append([
        InlineKeyboardButton(
            text=f"🔍 Качество: {res_label}",
            callback_data="choose_resolution",
        ),
    ])
    rows.append([
        InlineKeyboardButton(
            text=f"📤 Формат: {send_label}",
            callback_data="choose_send_mode",
        ),
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_balance_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 30 кредитов — 10₽", callback_data="buy_pack_30")],
        [InlineKeyboardButton(text="💎 100 кредитов — 299₽", callback_data="buy_pack_100")],
        [InlineKeyboardButton(text="💎 200 кредитов — 549₽", callback_data="buy_pack_200")],
    ])


def get_payment_method_keyboard(pack_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Банковская карта", callback_data=f"pay_{pack_key}_card")],
        [InlineKeyboardButton(text="🏦 СБП", callback_data=f"pay_{pack_key}_sbp")],
        [InlineKeyboardButton(text="🇷🇺 МИР", callback_data=f"pay_{pack_key}_mir")],
        [InlineKeyboardButton(text="🟣 ЮMoney", callback_data=f"pay_{pack_key}_yoomoney")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_balance")],
    ])
