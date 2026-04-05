from __future__ import annotations

from vkbottle import Keyboard, KeyboardButtonColor, Text, Callback

from bot.user_settings import (
    get_user_settings, AVAILABLE_MODELS, SEND_MODES, RESOLUTIONS, THINKING_LEVELS,
)
from bot.keyboards import ASPECT_RATIOS


def get_persistent_keyboard() -> str:
    kb = Keyboard(one_time=False, inline=False)
    kb.add(Text("📋 Меню"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("💡 Идеи"), color=KeyboardButtonColor.POSITIVE)
    kb.row()
    kb.add(Text("⚙️ Настройки"), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text("⛔ Стоп"), color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()


def _is_flash_model(model_id: str) -> bool:
    return "flash" in model_id.lower() and "lite" not in model_id.lower()


def get_settings_keyboard(user_id: int) -> str:
    settings = get_user_settings(user_id)
    current_model = settings.get("model", "gemini-3.1-flash-image-preview")
    model_info = AVAILABLE_MODELS.get(current_model, {})
    model_label = model_info.get("label", current_model)

    send_info = SEND_MODES.get(settings.get("send_mode", "photo"), {})
    send_label = send_info.get("label", "🖼 Фото")

    res_info = RESOLUTIONS.get(settings.get("resolution", "original"), {})
    res_label = res_info.get("label", "📷 Оригинал")

    aspect_label = ASPECT_RATIOS.get(settings.get("aspect_ratio", "1:1"), "1:1")

    kb = Keyboard(inline=True)
    kb.add(Callback(f"🤖 {model_label}", payload={"cmd": "choose_model"}))
    kb.row()
    kb.add(Callback(f"📐 Размер: {aspect_label}", payload={"cmd": "choose_aspect"}))
    kb.row()

    if _is_flash_model(current_model):
        thinking_info = THINKING_LEVELS.get(settings.get("thinking_level", "low"), {})
        thinking_label = thinking_info.get("label", "💭 Лёгкий")
        kb.add(Callback(f"🧠 Мышление: {thinking_label}", payload={"cmd": "choose_thinking"}))
        kb.row()

    kb.add(Callback(f"🔍 Качество: {res_label}", payload={"cmd": "choose_resolution"}))
    kb.row()
    kb.add(Callback(f"📤 Формат: {send_label}", payload={"cmd": "choose_send_mode"}))
    return kb.get_json()


def get_model_keyboard(user_id: int) -> str:
    settings = get_user_settings(user_id)
    current = settings.get("model", "gemini-3.1-flash-image-preview")

    kb = Keyboard(inline=True)
    for model_id, info in AVAILABLE_MODELS.items():
        label = info["label"]
        if model_id == current:
            label = "✅ " + label
        kb.add(Callback(label, payload={"cmd": "set_model", "id": model_id}))
        kb.row()
    kb.add(Callback("◀️ Назад", payload={"cmd": "back_settings"}))
    return kb.get_json()


def get_aspect_ratio_keyboard(user_id: int, page: int = 0) -> str:
    settings = get_user_settings(user_id)
    current = settings.get("aspect_ratio", "1:1")

    items = list(ASPECT_RATIOS.items())
    page_size = 8
    total_pages = (len(items) + page_size - 1) // page_size
    page = max(0, min(page, total_pages - 1))
    page_items = items[page * page_size : (page + 1) * page_size]

    kb = Keyboard(inline=True)
    count = 0
    for key, label in page_items:
        text = f"✅ {label}" if key == current else label
        kb.add(Callback(text, payload={"cmd": "set_aspect", "id": key}))
        count += 1
        if count % 2 == 0:
            kb.row()
    if count % 2 != 0:
        kb.row()

    if total_pages > 1:
        if page > 0:
            kb.add(Callback("⬅️", payload={"cmd": "aspect_page", "page": page - 1}))
        if page < total_pages - 1:
            kb.add(Callback("➡️", payload={"cmd": "aspect_page", "page": page + 1}))
        kb.row()

    kb.add(Callback("◀️ Назад", payload={"cmd": "back_settings"}))
    return kb.get_json()


def get_thinking_keyboard(user_id: int) -> str:
    settings = get_user_settings(user_id)
    current = settings.get("thinking_level", "low")

    kb = Keyboard(inline=True)
    for level_id, info in THINKING_LEVELS.items():
        label = info["label"]
        if level_id == current:
            label = "✅ " + label
        kb.add(Callback(label, payload={"cmd": "set_thinking", "id": level_id}))
        kb.row()
    kb.add(Callback("◀️ Назад", payload={"cmd": "back_settings"}))
    return kb.get_json()


def get_resolution_keyboard(user_id: int) -> str:
    settings = get_user_settings(user_id)
    current = settings.get("resolution", "original")

    kb = Keyboard(inline=True)
    for res_id, info in RESOLUTIONS.items():
        label = info["label"]
        if res_id == current:
            label = "✅ " + label
        kb.add(Callback(label, payload={"cmd": "set_resolution", "id": res_id}))
        kb.row()
    kb.add(Callback("◀️ Назад", payload={"cmd": "back_settings"}))
    return kb.get_json()


def get_send_mode_keyboard(user_id: int) -> str:
    settings = get_user_settings(user_id)
    current = settings.get("send_mode", "photo")

    kb = Keyboard(inline=True)
    for mode_id, info in SEND_MODES.items():
        label = info["label"]
        if mode_id == current:
            label = "✅ " + label
        kb.add(Callback(label, payload={"cmd": "set_send_mode", "id": mode_id}))
        kb.row()
    kb.add(Callback("◀️ Назад", payload={"cmd": "back_settings"}))
    return kb.get_json()


def get_creative_prompt_keyboard() -> str:
    kb = Keyboard(inline=True)
    kb.add(Callback("🎨 Генерируй!", payload={"cmd": "creative_generate"}))
    kb.row()
    kb.add(Callback("✏️ Изменить", payload={"cmd": "creative_edit"}))
    kb.row()
    kb.add(Callback("❌ Отмена", payload={"cmd": "creative_cancel"}))
    return kb.get_json()


def get_creative_auto_keyboard() -> str:
    kb = Keyboard(inline=True)
    kb.add(Callback("🪄 Дополни сам и генерируй", payload={"cmd": "creative_auto"}))
    kb.row()
    kb.add(Callback("❌ Отмена", payload={"cmd": "creative_cancel"}))
    return kb.get_json()


def get_switch_model_keyboard(current_model: str) -> str:
    kb = Keyboard(inline=True)
    for model_id, info in AVAILABLE_MODELS.items():
        if model_id != current_model:
            kb.add(Callback(
                f"🔄 {info['label']}",
                payload={"cmd": "switch_model", "id": model_id},
            ))
            kb.row()
    return kb.get_json()
