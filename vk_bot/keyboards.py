from __future__ import annotations

from vkbottle import Keyboard, KeyboardButtonColor, Text, Callback

from bot.user_settings import (
    get_user_settings, AVAILABLE_MODELS, SEND_MODES, RESOLUTIONS, THINKING_LEVELS,
    VIDEO_DURATIONS, VIDEO_RESOLUTIONS, VIDEO_ASPECT_RATIOS, VIDEO_TASKS,
    is_video_model, get_video_credits_cost, video_supports_audio, video_supports_image,
    get_video_resolutions_for_model, get_available_tasks_for_model,
)
from bot.keyboards import ASPECT_RATIOS


def get_persistent_keyboard() -> str:
    kb = Keyboard(one_time=False, inline=False)
    kb.add(Text("📋 Меню"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("💬 Чат"), color=KeyboardButtonColor.POSITIVE)
    kb.row()
    kb.add(Text("⚙️ Настройки"), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text("💰 Баланс"), color=KeyboardButtonColor.POSITIVE)
    kb.row()
    kb.add(Text("⛔ Стоп"), color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()


def _is_flash_model(model_id: str) -> bool:
    return "flash" in model_id.lower() and "lite" not in model_id.lower()


def get_settings_keyboard(user_id: int) -> str:
    settings = get_user_settings(user_id)
    current_model = settings.get("model", "gemini-3.1-flash-image-preview")
    model_info = AVAILABLE_MODELS.get(current_model, {})
    model_label = model_info.get("label", current_model)

    kb = Keyboard(inline=True)
    kb.add(Callback(f"🤖 {model_label}", payload={"cmd": "choose_model"}))
    kb.row()

    if is_video_model(current_model):
        aspect = settings.get("video_aspect_ratio", "16:9")
        dur = settings.get("video_duration", 8)
        res = settings.get("video_resolution", "720p")
        audio = settings.get("video_audio", True)
        has_audio = video_supports_audio(current_model)
        avail_res = get_video_resolutions_for_model(current_model)
        if res not in avail_res:
            res = "1080p"

        for key, label in VIDEO_ASPECT_RATIOS.items():
            text = f"✅ {label}" if key == aspect else label
            kb.add(Callback(text, payload={"cmd": "vp_aspect", "id": key}))
        kb.row()

        for d in VIDEO_DURATIONS:
            text = f"✅ {d}с" if d == dur else f"{d}с"
            kb.add(Callback(text, payload={"cmd": "vp_dur", "id": d}))
        kb.row()

        for r in avail_res:
            r_label = avail_res[r].get("label", r).replace("📺 ", "").replace("🖥 ", "").replace("📽 ", "")
            text = f"✅ {r_label}" if r == res else r_label
            kb.add(Callback(text, payload={"cmd": "vp_res", "id": r}))
        kb.row()

        if has_audio:
            audio_text = "✅ 🔊 Аудио вкл" if audio else "🔇 Аудио выкл"
            kb.add(Callback(audio_text, payload={"cmd": "vp_audio"}))
    else:
        aspect_label = ASPECT_RATIOS.get(settings.get("aspect_ratio", "1:1"), "1:1")
        kb.add(Callback(f"📐 Размер: {aspect_label}", payload={"cmd": "choose_aspect"}))
        kb.row()

        if _is_flash_model(current_model):
            thinking_info = THINKING_LEVELS.get(settings.get("thinking_level", "low"), {})
            thinking_label = thinking_info.get("label", "💭 Лёгкий")
            kb.add(Callback(f"🧠 Мышление: {thinking_label}", payload={"cmd": "choose_thinking"}))
            kb.row()

        send_info = SEND_MODES.get(settings.get("send_mode", "photo"), {})
        send_label = send_info.get("label", "🖼 Фото")
        res_info = RESOLUTIONS.get(settings.get("resolution", "original"), {})
        res_label = res_info.get("label", "📷 Оригинал")

        kb.add(Callback(f"🔍 Качество: {res_label}", payload={"cmd": "choose_resolution"}))
        kb.row()
        kb.add(Callback(f"📤 Формат: {send_label}", payload={"cmd": "choose_send_mode"}))
    return kb.get_json()


def get_model_keyboard(user_id: int) -> str:
    settings = get_user_settings(user_id)
    current = settings.get("model", "gemini-3.1-flash-image-preview")

    image_models = {k: v for k, v in AVAILABLE_MODELS.items() if v.get("type") != "video"}
    video_models = {k: v for k, v in AVAILABLE_MODELS.items() if v.get("type") == "video"}

    kb = Keyboard(inline=True)

    for model_id, info in image_models.items():
        label = info["label"]
        if model_id == current:
            label = "✅ " + label
        kb.add(Callback(label, payload={"cmd": "set_model", "id": model_id}))
        kb.row()

    if video_models:
        kb.add(Callback("── 🎬 Видео модели ──", payload={"cmd": "noop"}))
        kb.row()
        for model_id, info in video_models.items():
            label = info["label"]
            if model_id == current:
                label = "✅ " + label
            kb.add(Callback(label, payload={"cmd": "set_model", "id": model_id}))
            kb.row()

    kb.add(Callback("◀️ Назад", payload={"cmd": "back_settings"}))
    return kb.get_json()


def get_video_duration_keyboard(user_id: int) -> str:
    settings = get_user_settings(user_id)
    current = settings.get("video_duration", 8)

    kb = Keyboard(inline=True)
    for dur, info in VIDEO_DURATIONS.items():
        label = info["label"]
        if dur == current:
            label = "✅ " + label
        kb.add(Callback(label, payload={"cmd": "set_video_duration", "id": dur}))
        kb.row()
    kb.add(Callback("◀️ Назад", payload={"cmd": "back_settings"}))
    return kb.get_json()


def get_video_resolution_keyboard(user_id: int) -> str:
    settings = get_user_settings(user_id)
    current = settings.get("video_resolution", "720p")

    kb = Keyboard(inline=True)
    for res, info in VIDEO_RESOLUTIONS.items():
        label = info["label"]
        if res == current:
            label = "✅ " + label
        kb.add(Callback(label, payload={"cmd": "set_video_resolution", "id": res}))
        kb.row()
    kb.add(Callback("◀️ Назад", payload={"cmd": "back_settings"}))
    return kb.get_json()


def get_video_aspect_keyboard(user_id: int) -> str:
    settings = get_user_settings(user_id)
    current = settings.get("video_aspect_ratio", "16:9")

    kb = Keyboard(inline=True)
    for ratio, label in VIDEO_ASPECT_RATIOS.items():
        text = f"✅ {label}" if ratio == current else label
        kb.add(Callback(text, payload={"cmd": "set_video_aspect", "id": ratio}))
        kb.row()
    kb.add(Callback("◀️ Назад", payload={"cmd": "back_settings"}))
    return kb.get_json()


def get_video_panel_text(user_id: int) -> str:
    settings = get_user_settings(user_id)
    model_id = settings.get("model", "veo-3.1-generate-001")
    model_info = AVAILABLE_MODELS.get(model_id, {})
    model_label = model_info.get("label", model_id)
    credits = model_info.get("credits", 3)
    has_audio = video_supports_audio(model_id)

    task_id = settings.get("video_task", "text-to-video")
    avail_tasks = get_available_tasks_for_model(model_id)
    if task_id not in avail_tasks:
        task_id = "text-to-video"
    task_info = VIDEO_TASKS.get(task_id, {})
    task_label = task_info.get("label", task_id)

    aspect = settings.get("video_aspect_ratio", "16:9")
    aspect_label = VIDEO_ASPECT_RATIOS.get(aspect, aspect)
    dur = settings.get("video_duration", 8)
    res = settings.get("video_resolution", "720p")
    avail_res = get_video_resolutions_for_model(model_id)
    if res not in avail_res:
        res = "1080p"
    res_info = VIDEO_RESOLUTIONS.get(res, {})
    res_label = res_info.get("label", res)
    audio = settings.get("video_audio", True)

    lines = [
        f"⚙️ Настройки — {model_label}",
        "",
        "┌─────────────────────",
        f"│ 🎯 Задача: {task_label}",
        f"│ 📐 Формат: {aspect_label}",
        f"│ ⏱ Длительность: {dur} сек",
        f"│ 📺 Разрешение: {res_label}",
    ]
    if has_audio:
        lines.append(f"│ 🔊 Аудио: {'Вкл' if audio else 'Выкл'}")
    lines += [
        "├─────────────────────",
        f"│ 💰 Стоимость: {credits} кр.",
        f"│ 📋 24 FPS • MP4",
        "└─────────────────────",
        "",
        "Нажмите на параметр чтобы изменить:",
    ]
    return "\n".join(lines)


def get_video_task_keyboard(user_id: int) -> str:
    settings = get_user_settings(user_id)
    model_id = settings.get("model", "veo-3.1-generate-001")
    current = settings.get("video_task", "text-to-video")
    avail = get_available_tasks_for_model(model_id)

    kb = Keyboard(inline=True)
    for tid, tinfo in avail.items():
        label = tinfo["label"]
        if tinfo.get("coming_soon"):
            label += " (скоро)"
        if tid == current:
            label = "✅ " + label
        kb.add(Callback(label, payload={"cmd": "set_vtask", "id": tid}))
        kb.row()
    kb.add(Callback("◀️ Назад", payload={"cmd": "back_settings"}))
    return kb.get_json()


def get_video_panel_keyboard(user_id: int) -> str:
    settings = get_user_settings(user_id)
    model_id = settings.get("model", "veo-3.1-generate-001")
    aspect = settings.get("video_aspect_ratio", "16:9")
    dur = settings.get("video_duration", 8)
    res = settings.get("video_resolution", "720p")
    audio = settings.get("video_audio", True)
    has_audio = video_supports_audio(model_id)
    avail_res = get_video_resolutions_for_model(model_id)
    if res not in avail_res:
        res = "1080p"
    task_id = settings.get("video_task", "text-to-video")
    task_label = VIDEO_TASKS.get(task_id, {}).get("label", task_id)

    kb = Keyboard(inline=True)

    kb.add(Callback(f"🎯 Задача: {task_label}", payload={"cmd": "choose_vtask"}))
    kb.row()

    for key, label in VIDEO_ASPECT_RATIOS.items():
        text = f"✅ {label}" if key == aspect else label
        kb.add(Callback(text, payload={"cmd": "vp_aspect", "id": key}))
    kb.row()

    for d in VIDEO_DURATIONS:
        text = f"✅ {d}с" if d == dur else f"{d}с"
        kb.add(Callback(text, payload={"cmd": "vp_dur", "id": d}))
    kb.row()

    for r in avail_res:
        r_label = avail_res[r].get("label", r).replace("📺 ", "").replace("🖥 ", "").replace("📽 ", "")
        text = f"✅ {r_label}" if r == res else r_label
        kb.add(Callback(text, payload={"cmd": "vp_res", "id": r}))
    kb.row()

    if has_audio:
        audio_text = "✅ 🔊 Аудио вкл" if audio else "🔇 Аудио выкл"
        kb.add(Callback(audio_text, payload={"cmd": "vp_audio"}))
        kb.row()

    kb.add(Callback("◀️ Назад к настройкам", payload={"cmd": "back_settings"}))
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


def get_chat_cancel_keyboard() -> str:
    kb = Keyboard(inline=True)
    kb.add(Callback("❌ Завершить чат", payload={"cmd": "chat_cancel"}))
    return kb.get_json()


def get_balance_keyboard() -> str:
    kb = Keyboard(inline=True)
    kb.add(Callback("🔹 3 кредита — 10₽", payload={"cmd": "buy", "pack": "pack_3"}))
    kb.row()
    kb.add(Callback("💎 30 кредитов — 99₽", payload={"cmd": "buy", "pack": "pack_30"}))
    kb.row()
    kb.add(Callback("💎 100 кредитов — 299₽", payload={"cmd": "buy", "pack": "pack_100"}))
    kb.row()
    kb.add(Callback("💎 200 кредитов — 549₽", payload={"cmd": "buy", "pack": "pack_200"}))
    return kb.get_json()


def get_payment_method_keyboard(pack_key: str) -> str:
    kb = Keyboard(inline=True)
    kb.add(Callback("💳 Банковская карта", payload={"cmd": "pay_method", "pack": pack_key, "method": "card"}))
    kb.row()
    kb.add(Callback("🏦 СБП", payload={"cmd": "pay_method", "pack": pack_key, "method": "sbp"}))
    kb.row()
    kb.add(Callback("🇷🇺 МИР", payload={"cmd": "pay_method", "pack": pack_key, "method": "mir"}))
    kb.row()
    kb.add(Callback("🟣 ЮMoney", payload={"cmd": "pay_method", "pack": pack_key, "method": "yoomoney"}))
    kb.row()
    kb.add(Callback("◀️ Назад", payload={"cmd": "back_balance"}))
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
