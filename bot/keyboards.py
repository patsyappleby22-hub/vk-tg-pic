"""
bot/keyboards.py
~~~~~~~~~~~~~~~~~
Inline keyboard layouts for the Telegram bot.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from bot.user_settings import (
    get_user_settings, AVAILABLE_MODELS, SEND_MODES, RESOLUTIONS, THINKING_LEVELS,
    VIDEO_DURATIONS, VIDEO_RESOLUTIONS, VIDEO_ASPECT_RATIOS, VIDEO_TASKS,
    is_video_model, get_video_credits_cost, video_supports_audio, video_supports_image,
    get_video_resolutions_for_model, get_available_tasks_for_model,
    is_music_model,
)

BTN_MENU = "📋 Меню"
BTN_STOP = "⛔ Стоп"
BTN_SETTINGS = "⚙️ Настройки"
BTN_CHAT = "💬 Чат"
BTN_BALANCE = "💰 Баланс"
BTN_WEB_CHAT = "🌐 Веб-чат"

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
    # Web-chat button intentionally removed from persistent keyboard —
    # it now lives as an inline button inside the menu message itself.
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

    image_models = {k: v for k, v in AVAILABLE_MODELS.items() if v.get("type") == "image"}
    video_models = {k: v for k, v in AVAILABLE_MODELS.items() if v.get("type") == "video"}
    music_models = {k: v for k, v in AVAILABLE_MODELS.items() if v.get("type") == "music"}

    rows.append([InlineKeyboardButton(text="── 🖼 Изображения ──", callback_data="noop")])
    for model_id, info in image_models.items():
        label = info["label"]
        if model_id == current:
            label = "✅ " + label
        rows.append([
            InlineKeyboardButton(text=label, callback_data=f"model_{model_id}")
        ])

    rows.append([InlineKeyboardButton(text="── 🎬 Видео ──", callback_data="noop")])
    for model_id, info in video_models.items():
        label = info["label"]
        if model_id == current:
            label = "✅ " + label
        rows.append([
            InlineKeyboardButton(text=label, callback_data=f"model_{model_id}")
        ])

    rows.append([InlineKeyboardButton(text="── 🎵 Музыка ──", callback_data="noop")])
    for model_id, info in music_models.items():
        label = info["label"]
        if model_id == current:
            label = "✅ " + label
        rows.append([
            InlineKeyboardButton(text=label, callback_data=f"model_{model_id}")
        ])

    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_video_duration_keyboard(user_id: int) -> InlineKeyboardMarkup:
    settings = get_user_settings(user_id)
    current = settings.get("video_duration", 8)

    rows: list[list[InlineKeyboardButton]] = []
    for dur, info in VIDEO_DURATIONS.items():
        label = info["label"]
        if dur == current:
            label = "✅ " + label
        rows.append([
            InlineKeyboardButton(text=label, callback_data=f"vdur_{dur}")
        ])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_video_resolution_keyboard(user_id: int) -> InlineKeyboardMarkup:
    settings = get_user_settings(user_id)
    current = settings.get("video_resolution", "720p")

    rows: list[list[InlineKeyboardButton]] = []
    for res_id, info in VIDEO_RESOLUTIONS.items():
        label = info["label"]
        if res_id == current:
            label = "✅ " + label
        rows.append([
            InlineKeyboardButton(text=label, callback_data=f"vres_{res_id}")
        ])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_video_aspect_keyboard(user_id: int) -> InlineKeyboardMarkup:
    settings = get_user_settings(user_id)
    current = settings.get("video_aspect_ratio", "16:9")

    rows: list[list[InlineKeyboardButton]] = []
    for key, label_text in VIDEO_ASPECT_RATIOS.items():
        label = label_text
        if key == current:
            label = "✅ " + label
        rows.append([
            InlineKeyboardButton(text=label, callback_data=f"vaspect_{key}")
        ])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_video_panel_text(user_id: int) -> str:
    from bot.user_settings import calc_video_credits
    settings = get_user_settings(user_id)
    model_id = settings.get("model", "veo-3.1-generate-001")
    model_info = AVAILABLE_MODELS.get(model_id, {})
    model_label = model_info.get("label", model_id)
    has_audio = video_supports_audio(model_id)
    _aud = settings.get("video_audio", True) and has_audio
    _task = settings.get("video_task", "text-to-video")
    _dur = 7 if _task == "video-extension" else settings.get("video_duration", 8)
    res = settings.get("video_resolution", "720p")
    avail_res = get_video_resolutions_for_model(model_id)
    if res not in avail_res:
        res = "1080p"
    credits = calc_video_credits(model_id, duration_seconds=_dur, audio=_aud, resolution=res)

    task_id = settings.get("video_task", "text-to-video")
    avail_tasks = get_available_tasks_for_model(model_id)
    if task_id not in avail_tasks:
        task_id = "text-to-video"
    task_info = VIDEO_TASKS.get(task_id, {})
    task_label = task_info.get("label", task_id)

    aspect = settings.get("video_aspect_ratio", "16:9")
    aspect_label = VIDEO_ASPECT_RATIOS.get(aspect, aspect)
    dur = settings.get("video_duration", 8)
    res_info = VIDEO_RESOLUTIONS.get(res, {})
    res_label = res_info.get("label", res)
    audio = settings.get("video_audio", True)

    lines = [
        f"⚙️ <b>Настройки — {model_label}</b>",
        "",
        "┌─────────────────────",
        f"│ 🎯 Задача: <b>{task_label}</b>",
        f"│ 📐 Формат: <b>{aspect_label}</b>",
        f"│ ⏱ Длительность: <b>{dur} сек</b>",
        f"│ 📺 Разрешение: <b>{res_label}</b>",
    ]
    if has_audio:
        lines.append(f"│ 🔊 Аудио: <b>{'Вкл' if audio else 'Выкл'}</b>")
    lines += [
        "├─────────────────────",
        f"│ 💰 Стоимость: <b>{credits} кр.</b>",
        f"│ 📋 24 FPS • MP4",
        "└─────────────────────",
        "",
        "Нажмите на параметр чтобы изменить:",
    ]
    return "\n".join(lines)


def get_video_task_keyboard(user_id: int) -> InlineKeyboardMarkup:
    settings = get_user_settings(user_id)
    model_id = settings.get("model", "veo-3.1-generate-001")
    current = settings.get("video_task", "text-to-video")
    avail = get_available_tasks_for_model(model_id)

    rows: list[list[InlineKeyboardButton]] = []
    for tid, tinfo in avail.items():
        label = tinfo["label"]
        if tinfo.get("coming_soon"):
            label += " (скоро)"
        if tid == current:
            label = "✅ " + label
        rows.append([InlineKeyboardButton(text=label, callback_data=f"vtask_{tid}")])

    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_video_panel_keyboard(user_id: int) -> InlineKeyboardMarkup:
    from bot.user_settings import calc_video_credits
    settings = get_user_settings(user_id)
    model_id = settings.get("model", "veo-3.1-generate-001")
    aspect = settings.get("video_aspect_ratio", "16:9")
    dur = settings.get("video_duration", 8)
    res = settings.get("video_resolution", "720p")
    audio = settings.get("video_audio", True)
    has_audio = video_supports_audio(model_id)
    avail_res = get_video_resolutions_for_model(model_id)
    task_id = settings.get("video_task", "text-to-video")
    task_label = VIDEO_TASKS.get(task_id, {}).get("label", task_id)
    _audio_eff = audio and has_audio
    _dur_eff = 7 if task_id == "video-extension" else dur

    rows: list[list[InlineKeyboardButton]] = []

    rows.append([InlineKeyboardButton(text=f"🎯 Задача: {task_label}", callback_data="choose_video_task")])

    rows.append([InlineKeyboardButton(text="── 📐 Формат ──", callback_data="noop")])
    aspect_row: list[InlineKeyboardButton] = []
    for key, label in VIDEO_ASPECT_RATIOS.items():
        text = f"✅ {label}" if key == aspect else label
        aspect_row.append(InlineKeyboardButton(text=text, callback_data=f"vp_aspect_{key}"))
    rows.append(aspect_row)

    rows.append([InlineKeyboardButton(text="── ⏱ Длительность ──", callback_data="noop")])
    dur_row: list[InlineKeyboardButton] = []
    _ext_locked = task_id == "video-extension"  # only extension is truly locked
    _c_ext = calc_video_credits(model_id, duration_seconds=7, audio=_audio_eff, resolution=res)
    if _ext_locked:
        # video-extension: fixed 7s — show price but no choice
        dur_row.append(InlineKeyboardButton(text=f"🔒 7с ({_c_ext}кр)", callback_data="noop"))
    else:
        # text-to-video and image-to-video: full 4/6/8s choice
        for d in VIDEO_DURATIONS:
            c = calc_video_credits(model_id, duration_seconds=d, audio=_audio_eff, resolution=res)
            text = f"✅ {d}с ({c}кр)" if d == dur else f"{d}с ({c}кр)"
            dur_row.append(InlineKeyboardButton(text=text, callback_data=f"vp_dur_{d}"))
    rows.append(dur_row)

    rows.append([InlineKeyboardButton(text="── 📺 Разрешение ──", callback_data="noop")])
    res_row: list[InlineKeyboardButton] = []
    for r in avail_res:
        r_label = avail_res[r].get("label", r).replace("📺 ", "").replace("🖥 ", "").replace("📽 ", "")
        text = f"✅ {r_label}" if r == res else r_label
        res_row.append(InlineKeyboardButton(text=text, callback_data=f"vp_res_{r}"))
    rows.append(res_row)

    if has_audio:
        c_on = calc_video_credits(model_id, duration_seconds=_dur_eff, audio=True, resolution=res)
        c_off = calc_video_credits(model_id, duration_seconds=_dur_eff, audio=False, resolution=res)
        audio_text = f"✅ 🔊 Аудио вкл ({c_on}кр)" if audio else f"🔇 Аудио выкл ({c_off}кр)"
        rows.append([InlineKeyboardButton(text=audio_text, callback_data="vp_audio")])

    rows.append([InlineKeyboardButton(text="◀️ Назад к настройкам", callback_data="back_to_settings")])
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

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=f"🤖 {model_label}",
                callback_data="choose_model",
            ),
        ],
    ]

    if is_video_model(current_model):
        task_id = settings.get("video_task", "text-to-video")
        task_label = VIDEO_TASKS.get(task_id, {}).get("label", task_id)
        rows.append([InlineKeyboardButton(text=f"🎯 {task_label}", callback_data="choose_video_task")])

        aspect = settings.get("video_aspect_ratio", "16:9")
        dur = settings.get("video_duration", 8)
        res = settings.get("video_resolution", "720p")
        audio = settings.get("video_audio", True)
        has_audio = video_supports_audio(current_model)
        avail_res = get_video_resolutions_for_model(current_model)
        if res not in avail_res:
            res = "1080p"

        aspect_row: list[InlineKeyboardButton] = []
        for key, label in VIDEO_ASPECT_RATIOS.items():
            text = f"✅ {label}" if key == aspect else label
            aspect_row.append(InlineKeyboardButton(text=text, callback_data=f"vp_aspect_{key}"))
        rows.append(aspect_row)

        dur_row: list[InlineKeyboardButton] = []
        for d in VIDEO_DURATIONS:
            text = f"✅ {d}с" if d == dur else f"{d}с"
            dur_row.append(InlineKeyboardButton(text=text, callback_data=f"vp_dur_{d}"))
        rows.append(dur_row)

        res_row: list[InlineKeyboardButton] = []
        for r in avail_res:
            r_label = avail_res[r].get("label", r).replace("📺 ", "").replace("🖥 ", "").replace("📽 ", "")
            text = f"✅ {r_label}" if r == res else r_label
            res_row.append(InlineKeyboardButton(text=text, callback_data=f"vp_res_{r}"))
        rows.append(res_row)

        if has_audio:
            audio_text = "✅ 🔊 Аудио вкл" if audio else "🔇 Аудио выкл"
            rows.append([InlineKeyboardButton(text=audio_text, callback_data="vp_audio")])
    elif is_music_model(current_model):
        model_info = AVAILABLE_MODELS.get(current_model, {})
        duration_label = model_info.get("duration_label", "аудио")
        credits = model_info.get("credits", 2)
        rows.append([
            InlineKeyboardButton(
                text=f"🎵 Музыка: {duration_label} • {credits} кр.",
                callback_data="noop",
            ),
        ])
        rows.append([
            InlineKeyboardButton(
                text="🖼 Вход: текст или фото",
                callback_data="noop",
            ),
        ])
    else:
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

        send_info = SEND_MODES.get(settings.get("send_mode", "photo"), {})
        send_label = send_info.get("label", "🖼 Фото")
        res_info = RESOLUTIONS.get(settings.get("resolution", "original"), {})
        res_label = res_info.get("label", "📷 Оригинал")

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
        [InlineKeyboardButton(text="🔹 3 кредита — 10₽", callback_data="buy_pack_3")],
        [InlineKeyboardButton(text="💎 30 кредитов — 99₽", callback_data="buy_pack_30")],
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
