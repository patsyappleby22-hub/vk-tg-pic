from __future__ import annotations

import asyncio
import io
import logging
import re
import time
import unicodedata
from typing import Any

from vkbottle.bot import Bot, Message
from vkbottle import GroupEventType

from bot.services.vertex_ai_service import VertexAIService
from bot.user_settings import (
    get_user_settings, save_user_settings, increment_generations,
    AVAILABLE_MODELS, SEND_MODES, RESOLUTIONS, THINKING_LEVELS,
    is_blocked, has_credits, FREE_CREDITS,
    has_chat_quota, increment_chat_count,
    get_chat_daily_count, get_chat_daily_limit,
    is_video_model, get_video_credits_cost,
    is_music_model, get_music_credits_cost,
    reserve_credits, release_credits, confirm_credits,
)
from bot.keyboards import ASPECT_RATIOS
from core.exceptions import BotError, QuotaExceededError, SafetyFilterError

from vk_bot.keyboards import (
    get_persistent_keyboard,
    get_settings_keyboard,
    get_switch_model_keyboard,
    get_balance_keyboard,
)
from vk_bot.photo_upload import upload_photo_to_vk, upload_document_to_vk, download_vk_photo
from bot.log_channel import log_generation_vk

logger = logging.getLogger(__name__)

SPINNER = ["◐", "◓", "◑", "◒"]
ANIMATION_INTERVAL = 3.0  # seconds between edits
_VK_FLOOD_RETRY_DELAY = 1.5  # seconds to wait before retrying on flood control


async def _vk_safe_edit(api: Any, *, retries: int = 3, **kwargs) -> None:
    """Call messages.edit, retrying on VK flood-control (error 9)."""
    for attempt in range(retries):
        try:
            await api.messages.edit(**kwargs)
            return
        except Exception as exc:
            err = str(exc)
            is_flood = (
                "flood" in err.lower()
                or "VKAPIError_9" in type(exc).__name__
                or "[9]" in err
            )
            if is_flood and attempt < retries - 1:
                await asyncio.sleep(_VK_FLOOD_RETRY_DELAY * (attempt + 1))
                continue
            raise


class VKProgressAnimator:
    """Edits a VK message every few seconds to show elapsed time."""

    def __init__(
        self, bot: Bot, peer_id: int, message_id: int, base_text: str,
        action_text: str = "Обработка",
    ) -> None:
        self._bot = bot
        self._peer_id = peer_id
        self._message_id = message_id
        self._base_text = base_text
        self._action_text = action_text
        self._task: asyncio.Task | None = None
        self._stopped = False
        self._start_time = 0.0

    def start(self) -> None:
        self._start_time = time.monotonic()
        self._task = asyncio.create_task(self._animate())

    async def stop(self) -> None:
        self._stopped = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _animate(self) -> None:
        tick = 0
        await asyncio.sleep(ANIMATION_INTERVAL)
        while not self._stopped:
            elapsed = int(time.monotonic() - self._start_time)
            spin = SPINNER[tick % len(SPINNER)]
            text = f"{self._base_text}\n\n{spin} {self._action_text} — {elapsed} сек."
            try:
                await _vk_safe_edit(
                    self._bot.api,
                    peer_id=self._peer_id,
                    message_id=self._message_id,
                    message=text,
                )
            except Exception:
                break
            tick += 1
            await asyncio.sleep(ANIMATION_INTERVAL)


MENU_TEXTS = {"📋 меню", "📋 Меню", "меню", "menu"}
SETTINGS_TEXTS = {"⚙️ настройки", "⚙️ Настройки", "настройки", "settings"}
STOP_TEXTS = {"⛔ стоп", "⛔ Стоп", "стоп", "stop", "отмена", "cancel"}
CHAT_TEXTS = {"💬 чат", "💬 Чат", "чат"}
BALANCE_TEXTS = {"💰 баланс", "💰 Баланс", "баланс", "balance"}
RESERVED_TEXTS = MENU_TEXTS | SETTINGS_TEXTS | STOP_TEXTS | CHAT_TEXTS | BALANCE_TEXTS

_chat_sessions: dict[int, list[dict[str, Any]]] = {}


def _vk_chat_intro_text(active_chat_model_key: str) -> str:
    from bot.user_settings import CHAT_MODELS
    info = CHAT_MODELS.get(active_chat_model_key) or CHAT_MODELS["gemini-3.1-pro"]
    if info["backend"] == "grok":
        return (
            f"💬 Чат с {info['short']}\n\n"
            "🧠 Рассуждение шаг за шагом\n"
            "🌐 Поиск свежей информации в интернете\n"
            "🖼 Понимает текст и фото\n"
            "🎯 Объясняет, решает задачи, генерирует идеи\n\n"
            "Кнопкой ниже можно сменить модель.\n"
            "Для выхода — кнопка ⛔ Стоп в меню"
        )
    return (
        f"💬 Чат с {info['short']}\n\n"
        "🧠 Анализирую текст, код, фото, видео, аудио и документы\n"
        "🌍 Отвечаю на любом языке\n"
        "📎 Разбираю PDF и файлы\n"
        "🎯 Решаю задачи, объясняю, генерирую идеи\n\n"
        "Кнопками ниже можно сменить модель.\n"
        "Для выхода — ⛔ Стоп"
    )

active_tasks: dict[int, asyncio.Task] = {}

_TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
})


def _prompt_to_filename(prompt: str, max_words: int = 6) -> str:
    text = prompt.lower().translate(_TRANSLIT)
    text = unicodedata.normalize("NFKD", text)
    text = re.sub(r"[^a-z0-9\s]", "", text)
    words = text.split()[:max_words]
    slug = "_".join(words) if words else "image"
    slug = slug[:60]
    return f"{slug}.png"


def _prompt_to_audio_filename(prompt: str) -> str:
    return _prompt_to_filename(prompt).replace(".png", ".mp3")


def _upscale_image(image_bytes: bytes, max_side: int) -> bytes:
    if max_side <= 0:
        return image_bytes
    from PIL import Image
    img = Image.open(io.BytesIO(image_bytes))
    w, h = img.size
    if max(w, h) >= max_side:
        return image_bytes
    scale = max_side / max(w, h)
    new_w = int(w * scale)
    new_h = int(h * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()



_SUPPORTED_IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"}
_SUPPORTED_AUDIO_MIMES = {
    "audio/x-aac", "audio/flac", "audio/mp3", "audio/m4a", "audio/mpeg",
    "audio/mpga", "audio/mp4", "audio/ogg", "audio/pcm", "audio/wav", "audio/webm",
}
_SUPPORTED_DOC_MIMES = {"application/pdf", "text/plain"}
_ALL_SUPPORTED_MIMES = _SUPPORTED_IMAGE_MIMES | _SUPPORTED_AUDIO_MIMES | _SUPPORTED_DOC_MIMES

_MIME_ALIASES: dict[str, str] = {
    "audio/x-opus+ogg": "audio/ogg",
    "audio/opus": "audio/ogg",
    "image/jpg": "image/jpeg",
}


def _normalize_mime_vk(mime: str | None) -> str | None:
    if not mime:
        return None
    mime = _MIME_ALIASES.get(mime, mime)
    return mime if mime in _ALL_SUPPORTED_MIMES else None


async def _download_url(url: str) -> bytes:
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.read()


_VIDEO_DOC_EXTS = {"mp4", "mov", "avi", "mpeg", "mpg", "webm", "mkv", "m4v", "3gp", "qt"}
_IMAGE_DOC_EXTS = {"jpg", "jpeg", "png", "webp", "heic", "heif", "gif", "bmp"}
_MAX_VIDEO_BYTES = 200 * 1024 * 1024  # 200 MB safety cap for video processing


async def _download_vk_video_attachment(api: Any, video: Any) -> bytes:
    """Resolve a VK video attachment to a downloadable mp4 URL and fetch it."""
    owner_id = getattr(video, "owner_id", None)
    video_id = getattr(video, "id", None)
    access_key = getattr(video, "access_key", None)
    if owner_id is None or video_id is None:
        raise ValueError("Не удалось определить идентификатор видео.")

    vid_str = f"{owner_id}_{video_id}"
    if access_key:
        vid_str += f"_{access_key}"

    resp = await api.video.get(videos=vid_str)
    items = getattr(resp, "items", None) or []
    if not items:
        raise ValueError("Видео недоступно — возможно удалено или скрыто настройками приватности.")

    files = getattr(items[0], "files", None)
    if not files:
        raise ValueError("Не удалось получить ссылку на скачивание видео.")

    if getattr(files, "external", None):
        raise ValueError("Это внешнее видео (например, YouTube). Прикрепите файл напрямую.")

    for attr in (
        "mp4_2160", "mp4_1440", "mp4_1080", "mp4_720",
        "mp4_480", "mp4_360", "mp4_240", "mp4_144",
    ):
        url = getattr(files, attr, None)
        if url:
            return await _download_url(url)

    raise ValueError("Не удалось получить ссылку на mp4-файл видео.")


def _build_chat_api_contents(history: list[dict[str, Any]]) -> list[Any]:
    from google.genai import types as genai_types
    contents = []
    for msg in history:
        api_parts = []
        for part in msg["parts"]:
            if part["type"] == "text":
                api_parts.append(genai_types.Part.from_text(text=part["text"]))
            elif part["type"] == "media":
                api_parts.append(
                    genai_types.Part.from_bytes(data=part["data"], mime_type=part["mime_type"])
                )
        if api_parts:
            contents.append(genai_types.Content(role=msg["role"], parts=api_parts))
    return contents


def _build_vk_menu_text(first_name: str, generations: int, credits: int, blocked: bool) -> str:
    greeting = f"👋 Привет, {first_name}!\n\n" if first_name else "👋 Главное меню\n\n"
    if blocked:
        credit_line = "🚫 Доступ закрыт. Обратитесь к администратору.\n\n"
    else:
        purchased = max(0, credits - FREE_CREDITS) if credits > FREE_CREDITS else 0
        free_left = min(credits, FREE_CREDITS)
        credit_line = (
            "┌─────────────────────\n"
            f"│ 🔋 Баланс: {credits} кредитов\n"
        )
        if purchased > 0:
            credit_line += f"│ 💎 Купленные: {purchased}\n"
            credit_line += f"│ 🎁 Бесплатные: {free_left}\n"
        else:
            credit_line += f"│ 🎁 Бесплатные: {free_left} из {FREE_CREDITS}\n"
        credit_line += (
            f"│ 🎨 Сгенерировано: {generations}\n"
            "└─────────────────────\n\n"
        )
    return f"{greeting}{credit_line}Отправьте текст или фото с описанием:"


def register_handlers(bot: Bot, vertex_service: VertexAIService) -> None:

    @bot.on.message(text=["/start", "/начать", "начать", "Начать"])
    async def cmd_start(message: Message):
        uid = message.from_id
        settings = get_user_settings(uid)
        first_name = ""
        try:
            users = await bot.api.users.get(user_ids=[uid])
            if users:
                first_name = users[0].first_name or ""
        except Exception:
            pass
        settings["first_name"] = first_name
        if not settings.get("platform"):
            settings["platform"] = "vk"
        save_user_settings(uid)
        credits = settings.get("credits", FREE_CREDITS)
        blocked = settings.get("blocked", False)
        generations = settings.get("generations_count", 0)

        await message.answer(
            _build_vk_menu_text(first_name, generations, credits, blocked),
            keyboard=get_persistent_keyboard(),
        )

    @bot.on.message(text=list(MENU_TEXTS))
    async def cmd_menu(message: Message):
        uid = message.from_id
        settings = get_user_settings(uid)
        first_name = settings.get("first_name", "")
        credits = settings.get("credits", FREE_CREDITS)
        blocked = settings.get("blocked", False)
        generations = settings.get("generations_count", 0)

        await message.answer(
            _build_vk_menu_text(first_name, generations, credits, blocked),
            keyboard=get_persistent_keyboard(),
        )

    def _vk_get_settings_text(user_id: int) -> str:
        from bot.user_settings import (
            VIDEO_RESOLUTIONS as _VR, VIDEO_ASPECT_RATIOS as _VA,
            video_supports_audio as _vsa, video_supports_image as _vsi,
            get_video_resolutions_for_model as _gvrm,
        )
        s = get_user_settings(user_id)
        mid = s.get("model", "gemini-3.1-flash-image-preview")
        if is_music_model(mid):
            mi = AVAILABLE_MODELS.get(mid, {})
            return "\n".join([
                f"⚙️ Настройки — {mi.get('label', mid)}",
                "",
                "┌─────────────────────",
                f"│ 🎵 Длительность: {mi.get('duration_label', 'аудио')}",
                f"│ 💰 Стоимость: {mi.get('credits', 2)} кр.",
                "│ 📥 Вход: текст или фото",
                "│ 📤 Выход: MP3",
                "└─────────────────────",
                "",
                "Чтобы изменить музыкальную модель, нажмите кнопку модели.",
            ])
        if not is_video_model(mid):
            return "⚙️ Настройки\n\nВыберите что изменить:"
        mi = AVAILABLE_MODELS.get(mid, {})
        ml = mi.get("label", mid)
        cr = mi.get("credits", 3)
        has_audio = _vsa(mid)
        has_image = _vsi(mid)
        al = _VA.get(s.get("video_aspect_ratio", "16:9"), "16:9")
        d = s.get("video_duration", 8)
        res = s.get("video_resolution", "720p")
        avail_res = _gvrm(mid)
        if res not in avail_res:
            res = "1080p"
        rl = _VR.get(res, {}).get("label", res)
        au = s.get("video_audio", True)
        input_type = "текст + фото" if has_image else "только текст"
        lines = [
            f"⚙️ Настройки — {ml}",
            "",
            "┌─────────────────────",
            f"│ 📐 Формат: {al}",
            f"│ ⏱ Длительность: {d} сек",
            f"│ 📺 Разрешение: {rl}",
        ]
        if has_audio:
            lines.append(f"│ 🔊 Аудио: {'Вкл' if au else 'Выкл'}")
        lines += [
            "├─────────────────────",
            f"│ 💰 Стоимость: {cr} кр.",
            f"│ 📋 24 FPS • MP4 • {input_type}",
            "└─────────────────────",
            "",
            "Нажмите на параметр чтобы изменить:",
        ]
        return "\n".join(lines)

    @bot.on.message(text=list(SETTINGS_TEXTS))
    async def cmd_settings(message: Message):
        uid = message.from_id
        await message.answer(
            _vk_get_settings_text(uid),
            keyboard=get_settings_keyboard(uid),
        )

    @bot.on.message(text=list(STOP_TEXTS))
    async def cmd_stop(message: Message):
        uid = message.from_id
        task = active_tasks.pop(uid, None)
        cancelled = False
        if task and not task.done():
            task.cancel()
            cancelled = True
        was_chat = uid in _chat_sessions
        _chat_sessions.pop(uid, None)

        if cancelled or was_chat:
            text = "⛔ Отменено.\n\nОтправьте новый промпт или откройте меню."
            if was_chat:
                text = "⛔ Чат завершён.\n\nОтправьте промпт для генерации или начните чат заново."
            await message.answer(text)
        else:
            await message.answer("ℹ️ Нет активной генерации для отмены.")

    @bot.on.message(text=list(BALANCE_TEXTS))
    async def cmd_balance(message: Message):
        uid = message.from_id
        settings = get_user_settings(uid)
        credits = settings.get("credits", FREE_CREDITS)
        generations = settings.get("generations_count", 0)
        chat_used = get_chat_daily_count(uid)
        chat_limit = get_chat_daily_limit(uid)

        purchased = max(0, credits - FREE_CREDITS) if credits > FREE_CREDITS else 0
        free_left = min(credits, FREE_CREDITS)

        lines = ["💰 Ваш баланс", ""]
        lines.append("┌─────────────────────")
        lines.append(f"│ 🔋 Кредитов: {credits}")
        if purchased > 0:
            lines.append(f"│ 💎 Купленные: {purchased}")
            lines.append(f"│ 🎁 Бесплатные: {free_left}")
        else:
            lines.append(f"│ 🎁 Бесплатные: {free_left} из {FREE_CREDITS}")
        lines.append(f"│ 🎨 Сгенерировано: {generations}")
        lines.append("└─────────────────────")
        lines.append("")
        lines.append("📋 Стоимость генерации:")
        lines.append("🖼 Фото:")
        lines.append("▫️ 2К, Full HD и ниже — 1 кредит")
        lines.append("▫️ 4K — 2 кредита")
        lines.append("🎬 Видео:")
        lines.append("▫️ Veo 3.1 — 5 кредитов")
        lines.append("▫️ Veo 3.1 Fast — 3 кредита")
        lines.append("▫️ Veo 3.1 Lite — 2 кредита")
        lines.append("🎵 Музыка:")
        lines.append("▫️ Lyria 3 Pro (полная песня) — 4 кредита")
        lines.append("▫️ Lyria 3 (30 сек.) — 2 кредита")
        lines.append("")
        lines.append("💬 Чат с ИИ (в день):")
        lines.append(f"▫️ Использовано: {chat_used} из {chat_limit}")
        lines.append(f"▫️ Дневной лимит: {chat_limit} запросов")
        lines.append("")
        lines.append("💳 Выберите пакет для пополнения:")

        await message.answer("\n".join(lines), keyboard=get_balance_keyboard())

    @bot.on.message(text=["/info", "info", "Info", "📁 Документы"])
    async def cmd_info(message: Message):
        BASE = "https://www.vk-tg-picgenai.ru"
        text = (
            "📁 Правовые документы и условия использования:\n\n"
            "Вы можете ознакомиться с нашими документами по ссылкам ниже:\n\n"
            f"📁 ПУБЛИЧНАЯ ОФЕРТА:\n{BASE}/offer\n\n"
            f"📁 Политика обработки данных:\n{BASE}/privacy\n\n"
            f"✅ Согласие на обработку:\n{BASE}/consent\n\n"
            f"💰 Условия возврата:\n{BASE}/refund"
        )
        await message.answer(text)

    @bot.on.message(text=list(CHAT_TEXTS))
    async def cmd_chat(message: Message):
        from bot.user_settings import get_chat_model
        from vk_bot.keyboards import get_chat_model_keyboard
        uid = message.from_id
        _chat_sessions[uid] = []
        active = get_chat_model(uid)
        await message.answer(
            _vk_chat_intro_text(active),
            keyboard=get_chat_model_keyboard(active),
        )

    @bot.on.raw_event(GroupEventType.MESSAGE_EVENT, dataclass=dict)
    async def handle_callback(event: dict):
        payload = event.get("object", {})
        uid = payload.get("user_id")
        peer_id = payload.get("peer_id")
        event_id = payload.get("event_id")
        cmid = payload.get("conversation_message_id")  # ID of the message with the button
        data = payload.get("payload", {})
        cmd = data.get("cmd", "")

        try:
            await bot.api.messages.send_message_event_answer(
                event_id=event_id, user_id=uid, peer_id=peer_id,
            )
        except Exception:
            pass

        async def edit_msg(message: str, keyboard=None):
            """Edit the message that contained the pressed button (flood-safe)."""
            kwargs = dict(peer_id=peer_id, conversation_message_id=cmid, message=message)
            if keyboard is not None:
                kwargs["keyboard"] = keyboard
            await _vk_safe_edit(bot.api, **kwargs)

        if cmd == "back_settings":
            await edit_msg(_vk_get_settings_text(uid), get_settings_keyboard(uid))

        elif cmd == "choose_model":
            from vk_bot.keyboards import get_model_keyboard
            lines = ["🤖 Выберите модель:\n"]
            sections = {"image": "🖼 Изображения", "video": "🎬 Видео", "music": "🎵 Музыка"}
            current_section = None
            for model_id, info in AVAILABLE_MODELS.items():
                section = info.get("type", "image")
                if section != current_section:
                    current_section = section
                    lines.append(f"\n{sections.get(section, section)}:")
                lines.append(f"  {info['label']} — {info['desc']}")
            await edit_msg("\n".join(lines), get_model_keyboard(uid))

        elif cmd == "set_model":
            model_id = data.get("id", "")
            if model_id in AVAILABLE_MODELS:
                settings = get_user_settings(uid)
                settings["model"] = model_id
                save_user_settings(uid)
            await edit_msg(_vk_get_settings_text(uid), get_settings_keyboard(uid))

        elif cmd == "choose_aspect":
            from vk_bot.keyboards import get_aspect_ratio_keyboard
            await edit_msg("📐 Выберите соотношение сторон:", get_aspect_ratio_keyboard(uid, 0))

        elif cmd == "aspect_page":
            from vk_bot.keyboards import get_aspect_ratio_keyboard
            page = data.get("page", 0)
            await edit_msg("📐 Выберите соотношение сторон:", get_aspect_ratio_keyboard(uid, page))

        elif cmd == "set_aspect":
            ratio = data.get("id", "")
            if ratio in ASPECT_RATIOS:
                settings = get_user_settings(uid)
                settings["aspect_ratio"] = ratio
                save_user_settings(uid)
            await edit_msg("⚙️ Настройки\n\nВыберите что изменить:", get_settings_keyboard(uid))

        elif cmd == "choose_thinking":
            from vk_bot.keyboards import get_thinking_keyboard
            lines = ["🧠 Уровень мышления (Flash):\n"]
            for level_id, info in THINKING_LEVELS.items():
                lines.append(f"  {info['label']}\n  {info['desc']}\n")
            await edit_msg("\n".join(lines), get_thinking_keyboard(uid))

        elif cmd == "set_thinking":
            level = data.get("id", "")
            if level in THINKING_LEVELS:
                settings = get_user_settings(uid)
                settings["thinking_level"] = level
                save_user_settings(uid)
            await edit_msg("⚙️ Настройки\n\nВыберите что изменить:", get_settings_keyboard(uid))

        elif cmd == "choose_resolution":
            from vk_bot.keyboards import get_resolution_keyboard
            lines = ["🔍 Выберите качество:\n"]
            for res_id, info in RESOLUTIONS.items():
                lines.append(f"  {info['label']}\n  {info['desc']}\n")
            await edit_msg("\n".join(lines), get_resolution_keyboard(uid))

        elif cmd == "set_resolution":
            res_id = data.get("id", "")
            if res_id in RESOLUTIONS:
                settings = get_user_settings(uid)
                settings["resolution"] = res_id
                save_user_settings(uid)
            await edit_msg("⚙️ Настройки\n\nВыберите что изменить:", get_settings_keyboard(uid))

        elif cmd == "choose_send_mode":
            from vk_bot.keyboards import get_send_mode_keyboard
            lines = ["📤 Формат отправки:\n"]
            for mode_id, info in SEND_MODES.items():
                lines.append(f"  {info['label']}\n  {info['desc']}\n")
            await edit_msg("\n".join(lines), get_send_mode_keyboard(uid))

        elif cmd == "set_send_mode":
            mode_id = data.get("id", "")
            if mode_id in SEND_MODES:
                settings = get_user_settings(uid)
                settings["send_mode"] = mode_id
                save_user_settings(uid)
            await edit_msg("⚙️ Настройки\n\nВыберите что изменить:", get_settings_keyboard(uid))

        elif cmd == "noop":
            pass

        elif cmd == "open_video_panel":
            await edit_msg(_vk_get_settings_text(uid), get_settings_keyboard(uid))

        elif cmd == "vp_aspect":
            from bot.user_settings import VIDEO_ASPECT_RATIOS
            key = data.get("id", "16:9")
            if key in VIDEO_ASPECT_RATIOS:
                settings = get_user_settings(uid)
                settings["video_aspect_ratio"] = key
                save_user_settings(uid)
            await edit_msg(_vk_get_settings_text(uid), get_settings_keyboard(uid))

        elif cmd == "vp_dur":
            from bot.user_settings import VIDEO_DURATIONS
            dur = data.get("id", 8)
            if dur in VIDEO_DURATIONS:
                settings = get_user_settings(uid)
                settings["video_duration"] = dur
                save_user_settings(uid)
            await edit_msg(_vk_get_settings_text(uid), get_settings_keyboard(uid))

        elif cmd == "vp_res":
            from bot.user_settings import VIDEO_RESOLUTIONS, get_video_resolutions_for_model as _gvrm2
            res = data.get("id", "720p")
            settings = get_user_settings(uid)
            avail = _gvrm2(settings.get("model", ""))
            if res in VIDEO_RESOLUTIONS and res in avail:
                settings["video_resolution"] = res
                save_user_settings(uid)
            await edit_msg(_vk_get_settings_text(uid), get_settings_keyboard(uid))

        elif cmd == "vp_audio":
            settings = get_user_settings(uid)
            from bot.user_settings import video_supports_audio as _vsa2
            model_id = settings.get("model", "")
            if _vsa2(model_id):
                settings["video_audio"] = not settings.get("video_audio", True)
                save_user_settings(uid)
            await edit_msg(_vk_get_settings_text(uid), get_settings_keyboard(uid))

        elif cmd == "choose_vtask":
            from vk_bot.keyboards import get_video_task_keyboard
            from bot.user_settings import VIDEO_TASKS as _VT, get_available_tasks_for_model as _gatm
            settings = get_user_settings(uid)
            model_id = settings.get("model", "")
            avail = _gatm(model_id)
            lines = ["🎯 Тип задачи:\n"]
            for tid, tinfo in avail.items():
                suffix = " (скоро)" if tinfo.get("coming_soon") else ""
                lines.append(f"  {tinfo['label']}{suffix}\n  {tinfo['desc']}\n")
            await edit_msg("\n".join(lines), get_video_task_keyboard(uid))

        elif cmd == "set_vtask":
            from bot.user_settings import VIDEO_TASKS as _VT2, get_available_tasks_for_model as _gatm2
            task_id = data.get("id", "text-to-video")
            if task_id in _VT2 and not _VT2[task_id].get("coming_soon"):
                settings = get_user_settings(uid)
                model_id = settings.get("model", "")
                avail = _gatm2(model_id)
                if task_id in avail:
                    settings["video_task"] = task_id
                    save_user_settings(uid)
            await edit_msg(_vk_get_settings_text(uid), get_settings_keyboard(uid))

        elif cmd == "choose_video_duration":
            from vk_bot.keyboards import get_video_duration_keyboard
            from bot.user_settings import VIDEO_DURATIONS
            lines = ["⏱ Длительность видео:\n"]
            for dur, info in VIDEO_DURATIONS.items():
                lines.append(f"  {info['label']}\n")
            await edit_msg("\n".join(lines), get_video_duration_keyboard(uid))

        elif cmd == "set_video_duration":
            from bot.user_settings import VIDEO_DURATIONS
            dur = data.get("id", 8)
            if dur in VIDEO_DURATIONS:
                settings = get_user_settings(uid)
                settings["video_duration"] = dur
                save_user_settings(uid)
            await edit_msg("⚙️ Настройки\n\nВыберите что изменить:", get_settings_keyboard(uid))

        elif cmd == "choose_video_resolution":
            from vk_bot.keyboards import get_video_resolution_keyboard
            from bot.user_settings import VIDEO_RESOLUTIONS
            lines = ["📺 Разрешение видео:\n"]
            for res, info in VIDEO_RESOLUTIONS.items():
                lines.append(f"  {info['label']}\n")
            await edit_msg("\n".join(lines), get_video_resolution_keyboard(uid))

        elif cmd == "set_video_resolution":
            from bot.user_settings import VIDEO_RESOLUTIONS
            res = data.get("id", "720p")
            if res in VIDEO_RESOLUTIONS:
                settings = get_user_settings(uid)
                settings["video_resolution"] = res
                save_user_settings(uid)
            await edit_msg("⚙️ Настройки\n\nВыберите что изменить:", get_settings_keyboard(uid))

        elif cmd == "choose_video_aspect":
            from vk_bot.keyboards import get_video_aspect_keyboard
            from bot.user_settings import VIDEO_ASPECT_RATIOS
            lines = ["📐 Соотношение сторон видео:\n"]
            for ratio, label in VIDEO_ASPECT_RATIOS.items():
                lines.append(f"  {label}\n")
            await edit_msg("\n".join(lines), get_video_aspect_keyboard(uid))

        elif cmd == "set_video_aspect":
            from bot.user_settings import VIDEO_ASPECT_RATIOS
            ratio = data.get("id", "16:9")
            if ratio in VIDEO_ASPECT_RATIOS:
                settings = get_user_settings(uid)
                settings["video_aspect_ratio"] = ratio
                save_user_settings(uid)
            await edit_msg("⚙️ Настройки\n\nВыберите что изменить:", get_settings_keyboard(uid))

        elif cmd == "switch_model":
            model_id = data.get("id", "")
            if model_id in AVAILABLE_MODELS:
                settings = get_user_settings(uid)
                settings["model"] = model_id
                save_user_settings(uid)
                info = AVAILABLE_MODELS[model_id]
                await edit_msg(f"✅ Модель переключена на {info['label']}\n\nОтправьте запрос ещё раз.")

        elif cmd == "buy":
            from bot.services.lava_service import create_payment_url, CREDIT_PACKAGES as LAVA_PACKAGES
            pack_key = data.get("pack", "")
            pack = LAVA_PACKAGES.get(pack_key)
            if not pack:
                await edit_msg("Неизвестный пакет.")
                return
            result = await create_payment_url(uid, pack_key, source="vk")
            if result["ok"]:
                await edit_msg(
                    f"💳 Оплата: {pack['label']}\n\n"
                    f"Перейдите по ссылке для оплаты:\n{result['pay_url']}\n\n"
                    "Кредиты будут начислены автоматически после оплаты."
                )
            else:
                await edit_msg(f"Ошибка: {result.get('error', 'неизвестная')}")

        elif cmd == "chat_cancel":
            _chat_sessions.pop(uid, None)
            await edit_msg("❌ Чат завершён.\n\nМожете отправить промпт для генерации изображения.", get_persistent_keyboard())

        elif cmd == "chat_model":
            from bot.user_settings import CHAT_MODELS, get_chat_model, set_chat_model
            from vk_bot.keyboards import get_chat_model_keyboard
            new_key = data.get("id", "")
            if new_key not in CHAT_MODELS:
                return
            current = get_chat_model(uid)
            if new_key == current:
                return
            set_chat_model(uid, new_key)
            # Reset chat history so the new model starts fresh.
            _chat_sessions[uid] = []
            await edit_msg(_vk_chat_intro_text(new_key), get_chat_model_keyboard(new_key))

    @bot.on.message()
    async def handle_text(message: Message):
        uid = message.from_id
        peer_id = message.peer_id
        text = (message.text or "").strip()

        if text.lower() in {t.lower() for t in RESERVED_TEXTS}:
            return

        if text.startswith("/"):
            return

        if uid in _chat_sessions:
            await _handle_vk_chat_message(bot, vertex_service, uid, peer_id, message)
            return

        if message.attachments:
            photos: list[bytes] = []
            videos: list[bytes] = []
            video_errors: list[str] = []

            for att in message.attachments:
                try:
                    if att.photo:
                        photos.append(await download_vk_photo(bot.api, att.photo.sizes))
                    elif att.video:
                        try:
                            videos.append(await _download_vk_video_attachment(bot.api, att.video))
                        except Exception as ve:
                            video_errors.append(str(ve))
                            logger.warning("VK video download failed: %s", ve)
                    elif att.doc:
                        doc = att.doc
                        ext = (getattr(doc, "ext", "") or "").lower()
                        size = getattr(doc, "size", 0) or 0
                        url = getattr(doc, "url", None)
                        if not url:
                            continue
                        if ext in _IMAGE_DOC_EXTS:
                            photos.append(await _download_url(url))
                        elif ext in _VIDEO_DOC_EXTS:
                            if size and size > _MAX_VIDEO_BYTES:
                                video_errors.append(
                                    f"Видео слишком большое ({size // (1024*1024)} МБ). "
                                    f"Максимум — {_MAX_VIDEO_BYTES // (1024*1024)} МБ."
                                )
                                continue
                            videos.append(await _download_url(url))
                except Exception as e:
                    logger.warning("VK attachment processing failed: %s", e)

            if videos:
                await _handle_vk_video_extension(
                    bot, vertex_service, uid, peer_id, message,
                    video_bytes=videos[0], caption=text or "",
                )
                return

            if video_errors and not photos:
                await message.answer("⚠️ " + video_errors[0])
                return

            if photos:
                if not text:
                    settings_now = get_user_settings(uid)
                    model_now = settings_now.get("model", "gemini-3.1-flash-image-preview")
                    if is_video_model(model_now):
                        from bot.user_settings import video_supports_image as _vsi_hint
                        if _vsi_hint(model_now):
                            hint = (
                                "📷 Фото получено! Добавьте подпись — "
                                "что должно происходить в видео.\n\n"
                                "Например: «Камера медленно облетает этот объект»"
                            )
                        else:
                            model_label = AVAILABLE_MODELS.get(model_now, {}).get("label", model_now)
                            hint = (
                                f"🎬 Модель {model_label} принимает только текстовые запросы.\n\n"
                                "Переключите модель на Veo 3.1 / Veo 3.1 Fast для генерации видео по фото."
                            )
                    elif is_music_model(model_now):
                        hint = (
                            "📷 Фото получено! Добавьте описание музыки в подписи к фото.\n\n"
                            "Например: «Атмосферный синтвейв по настроению этого изображения»"
                        )
                    else:
                        hint = (
                            f"📷 Фото получено ({len(photos)} шт.)! Добавьте описание — "
                            "что нужно сделать с изображением.\n\n"
                            "Например: «Сделай фон ярче» или «Добавь закат на задний план»"
                        )
                    await message.answer(hint)
                    return

                await _generate_and_send(
                    bot, vertex_service, uid, peer_id, text,
                    images=photos,
                )
                return

        if not text:
            await message.answer("Отправьте текстовое описание изображения или прикрепите фото/видео.")
            return

        # Hint when user sends text but is in image-to-video / video-extension mode
        settings_now = get_user_settings(uid)
        model_now = settings_now.get("model", "gemini-3.1-flash-image-preview")
        if is_video_model(model_now):
            from bot.user_settings import (
                video_supports_image as _vsi_hint,
                video_supports_video_extension as _vse_hint,
            )
            video_task_now = settings_now.get("video_task", "text-to-video")
            if video_task_now == "image-to-video" and _vsi_hint(model_now):
                await message.answer(
                    "🖼 Режим Image-to-video\n\n"
                    "Прикрепите изображение с подписью (описанием) — "
                    "что должно происходить в видео.\n\n"
                    "Например: «Камера медленно облетает этот объект»"
                )
                return
            if video_task_now == "video-extension" and _vse_hint(model_now):
                await message.answer(
                    "🔄 Режим Video extension\n\n"
                    "Прикрепите видео (можно с подписью — как продолжить видео).\n\n"
                    "Например: «Camera slowly zooms out»"
                )
                return

        await _generate_and_send(bot, vertex_service, uid, peer_id, text)


def _clean_latex(text: str) -> str:
    """Convert LaTeX math notation to readable Unicode."""
    for _ in range(4):
        text = re.sub(r'\\frac\{([^{}]+)\}\{([^{}]+)\}', r'(\1/\2)', text)
    text = re.sub(r'\\sqrt\{([^{}]+)\}', r'√\1', text)
    text = re.sub(r'\\sqrt', '√', text)
    for cmd in (r'\\text', r'\\mathrm', r'\\mathbf', r'\\mathit', r'\\mathbb'):
        text = re.sub(cmd + r'\{([^}]*)\}', r'\1', text)
    _sup = {'0':'⁰','1':'¹','2':'²','3':'³','4':'⁴','5':'⁵','6':'⁶','7':'⁷','8':'⁸','9':'⁹',
            '+':'⁺','-':'⁻','n':'ⁿ','i':'ⁱ','T':'ᵀ','a':'ᵃ','b':'ᵇ'}
    text = re.sub(r'\^\{([^{}]+)\}', lambda m: ''.join(_sup.get(c, c) for c in m.group(1)), text)
    text = re.sub(r'\^([0-9nix])', lambda m: _sup.get(m.group(1), m.group(1)), text)
    _sub = {'0':'₀','1':'₁','2':'₂','3':'₃','4':'₄','5':'₅','6':'₆','7':'₇','8':'₈','9':'₉',
            '+':'₊','-':'₋','n':'ₙ','i':'ᵢ','k':'ₖ'}
    text = re.sub(r'_\{([^{}]+)\}', lambda m: ''.join(_sub.get(c, c) for c in m.group(1)), text)
    text = re.sub(r'_([0-9nk])', lambda m: _sub.get(m.group(1), m.group(1)), text)
    _syms = [
        (r'\\approx', '≈'), (r'\\cdot', '·'), (r'\\times', '×'), (r'\\div', '÷'),
        (r'\\pm', '±'), (r'\\mp', '∓'), (r'\\leq', '≤'), (r'\\geq', '≥'),
        (r'\\neq', '≠'), (r'\\ne', '≠'), (r'\\infty', '∞'),
        (r'\\implies', '⟹'), (r'\\Rightarrow', '⟹'), (r'\\rightarrow', '→'),
        (r'\\leftarrow', '←'), (r'\\pi', 'π'), (r'\\alpha', 'α'), (r'\\beta', 'β'),
        (r'\\gamma', 'γ'), (r'\\delta', 'δ'), (r'\\Delta', 'Δ'), (r'\\theta', 'θ'),
        (r'\\lambda', 'λ'), (r'\\mu', 'μ'), (r'\\sigma', 'σ'), (r'\\Sigma', 'Σ'),
        (r'\\phi', 'φ'), (r'\\omega', 'ω'), (r'\\Omega', 'Ω'), (r'\\rho', 'ρ'),
        (r'\\epsilon', 'ε'), (r'\\eta', 'η'), (r'\\tau', 'τ'), (r'\\partial', '∂'),
        (r'\\nabla', '∇'), (r'\\forall', '∀'), (r'\\exists', '∃'),
        (r'\\in', '∈'), (r'\\notin', '∉'), (r'\\ldots', '…'), (r'\\cdots', '⋯'),
        (r'\\left\(', '('), (r'\\right\)', ')'), (r'\\left\[', '['), (r'\\right\]', ']'),
        (r'\\left', ''), (r'\\right', ''), (r'\\langle', '⟨'), (r'\\rangle', '⟩'),
    ]
    for pat, sym in _syms:
        text = re.sub(pat, sym, text)
    text = re.sub(r'\\[a-zA-Z]+\*?', '', text)
    text = re.sub(r'\$\$(.+?)\$\$', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'\$(.+?)\$', r'\1', text)
    text = text.replace('{', '').replace('}', '')
    text = re.sub(r'  +', ' ', text)
    return text


def _strip_md(text: str) -> str:
    """Strip Markdown formatting and LaTeX for plain-text VK messages."""
    # LaTeX math → Unicode first
    text = _clean_latex(text)
    # Code blocks → keep content only
    text = re.sub(r"```(?:[^\n`]*)?\n?(.*?)```", lambda m: m.group(1).strip(), text, flags=re.DOTALL)
    # Inline code → keep content
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    # Headings
    text = re.sub(r"^#{1,6} ", "", text, flags=re.MULTILINE)
    # Bold **text** or __text__
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"\1", text, flags=re.DOTALL)
    # Italic *text* or _text_
    text = re.sub(r"\*([^*\n]+?)\*", r"\1", text)
    text = re.sub(r"_([^_\n]+?)_", r"\1", text)
    # Bullet points * item / - item → • item
    text = re.sub(r"^[*\-] ", "• ", text, flags=re.MULTILINE)
    return text.strip()


_THINKING_FRAMES = ["💭 Думаю.", "💭 Думаю..", "💭 Думаю..."]


async def _animate_thinking_vk(
    bot: Bot, peer_id: int, message_id: int, stop: asyncio.Event
) -> None:
    i = 1
    while not stop.is_set():
        await asyncio.sleep(3)
        if stop.is_set():
            break
        try:
            await bot.api.messages.edit(
                peer_id=peer_id,
                message_id=message_id,
                message=_THINKING_FRAMES[i % 3],
            )
        except Exception:
            pass
        i += 1


async def _handle_vk_chat_message(
    bot: Bot, vertex_service: VertexAIService,
    uid: int, peer_id: int, message: Any,
):
    if not has_chat_quota(uid):
        limit = get_chat_daily_limit(uid)
        await bot.api.messages.send(
            peer_id=peer_id, random_id=0,
            message=(
                f"⛔ Лимит чата на сегодня исчерпан ({limit} запросов).\n\n"
                "Лимит сбрасывается каждую ночь в 00:00. "
                "Пополните баланс чтобы увеличить дневной лимит."
            ),
        )
        return

    history = _chat_sessions[uid]
    text = (message.text or "").strip()

    parts: list[dict] = []
    if text:
        parts.append({"type": "text", "text": text})

    if message.attachments:
        for att in message.attachments:
            if att.photo:
                try:
                    photo_bytes = await download_vk_photo(bot.api, att.photo.sizes)
                    parts.append({"type": "media", "data": photo_bytes, "mime_type": "image/jpeg"})
                except Exception as e:
                    logger.warning("VK photo download failed in chat: %s", e)
                    parts.append({"type": "text", "text": "[изображение — не удалось загрузить]"})
            elif getattr(att, "audio_message", None):
                am = att.audio_message
                url = getattr(am, "link_ogg", None) or getattr(am, "link_mp3", None)
                if url:
                    try:
                        audio_bytes = await _download_url(url)
                        mime = "audio/ogg" if "ogg" in url else "audio/mpeg"
                        parts.append({"type": "media", "data": audio_bytes, "mime_type": mime})
                    except Exception as e:
                        logger.warning("VK audio message download failed: %s", e)
                        parts.append({"type": "text", "text": "[голосовое сообщение — не удалось загрузить]"})
                else:
                    parts.append({"type": "text", "text": "[голосовое сообщение]"})
            elif getattr(att, "doc", None):
                doc = att.doc
                url = getattr(doc, "url", None)
                raw_mime = getattr(doc, "mime_type", None) or ""
                ext = getattr(doc, "ext", "") or ""
                if not raw_mime and ext == "pdf":
                    raw_mime = "application/pdf"
                elif not raw_mime and ext in ("txt", "text"):
                    raw_mime = "text/plain"
                mime = _normalize_mime_vk(raw_mime)
                fname = getattr(doc, "title", "") or f"document.{ext}"
                if mime and url:
                    try:
                        doc_bytes = await _download_url(url)
                        parts.append({"type": "media", "data": doc_bytes, "mime_type": mime})
                        if not text:
                            parts.insert(0, {"type": "text", "text": f"[документ: {fname}]"})
                    except Exception as e:
                        logger.warning("VK doc download failed: %s", e)
                        parts.append({"type": "text", "text": f"[документ {fname} — не удалось загрузить]"})
                else:
                    if ext in _VIDEO_DOC_EXTS and url:
                        try:
                            video_bytes = await _download_url(url)
                            parts.append({"type": "media", "data": video_bytes, "mime_type": "video/mp4"})
                            if not text:
                                parts.insert(0, {"type": "text", "text": f"[видео: {fname}]"})
                        except Exception as e:
                            logger.warning("VK video-doc download in chat failed: %s", e)
                            parts.append({"type": "text", "text": f"[видео {fname} — не удалось загрузить]"})
                    elif ext in _IMAGE_DOC_EXTS and url:
                        try:
                            img_bytes = await _download_url(url)
                            parts.append({"type": "media", "data": img_bytes, "mime_type": "image/jpeg"})
                        except Exception as e:
                            logger.warning("VK image-doc download in chat failed: %s", e)
                            parts.append({"type": "text", "text": f"[изображение {fname} — не удалось загрузить]"})
                    else:
                        parts.append({"type": "text", "text": f"[прикреплён файл: {fname} — формат не поддерживается]"})
            elif getattr(att, "video", None):
                try:
                    video_bytes = await _download_vk_video_attachment(bot.api, att.video)
                    parts.append({"type": "media", "data": video_bytes, "mime_type": "video/mp4"})
                except Exception as e:
                    logger.warning("VK video download in chat failed: %s", e)
                    parts.append({"type": "text", "text": "[видео — не удалось загрузить]"})
            elif getattr(att, "audio", None):
                au = att.audio
                url = getattr(au, "url", None)
                if url:
                    try:
                        audio_bytes = await _download_url(url)
                        title = getattr(au, "title", None) or "аудио"
                        parts.append({"type": "media", "data": audio_bytes, "mime_type": "audio/mpeg"})
                        if not text:
                            parts.insert(0, {"type": "text", "text": f"[аудио: {title}]"})
                    except Exception as e:
                        logger.warning("VK audio download in chat failed: %s", e)
                        parts.append({"type": "text", "text": "[аудио — не удалось загрузить]"})

    if not parts:
        await bot.api.messages.send(
            peer_id=peer_id, random_id=0,
            message="Не удалось разобрать сообщение. Попробуйте ещё раз.",
        )
        return

    history.append({"role": "user", "parts": parts})

    thinking_id = await bot.api.messages.send(
        peer_id=peer_id, random_id=0,
        message="💭 Думаю.",
    )
    stop_event = asyncio.Event()
    anim_task = asyncio.create_task(
        _animate_thinking_vk(bot, peer_id, thinking_id, stop_event)
    )

    try:
        from bot.user_settings import CHAT_MODELS, get_chat_model
        chat_model_key = get_chat_model(uid)
        chat_info = CHAT_MODELS.get(chat_model_key, CHAT_MODELS["gemini-3.1-pro"])
        if chat_info["backend"] == "grok":
            response = await vertex_service.chat_grok(history, enable_search=True)
        else:
            contents = _build_chat_api_contents(history)
            response = await vertex_service.chat_text(contents)

        stop_event.set()
        anim_task.cancel()

        if not response:
            history.pop()
            await bot.api.messages.edit(
                peer_id=peer_id, message_id=thinking_id,
                message="Не удалось получить ответ, попробуйте ещё раз.",
            )
            return

        history.append({"role": "model", "parts": [{"type": "text", "text": response}]})

        if len(history) > 42:
            _chat_sessions[uid] = history[:2] + history[-40:]

        increment_chat_count(uid)
        cleaned = _strip_md(response)
        vk_chunks: list[str] = []
        tmp = cleaned
        while len(tmp) > 4096:
            split_at = tmp.rfind("\n", 0, 4096)
            if split_at <= 0:
                split_at = 4096
            vk_chunks.append(tmp[:split_at].rstrip())
            tmp = tmp[split_at:].lstrip()
        vk_chunks.append(tmp)

        try:
            await bot.api.messages.edit(
                peer_id=peer_id, message_id=thinking_id,
                message=vk_chunks[0],
            )
        except Exception:
            await bot.api.messages.send(
                peer_id=peer_id, random_id=0,
                message=vk_chunks[0],
            )
        for chunk in vk_chunks[1:]:
            await bot.api.messages.send(
                peer_id=peer_id, random_id=0,
                message=chunk,
            )

    except Exception as exc:
        stop_event.set()
        anim_task.cancel()
        logger.exception("VK chat error: %s", exc)
        err_text = str(exc).lower()
        if "429" in err_text or "quota" in err_text:
            msg = "⏳ API перегружен. Подождите пару минут."
        else:
            msg = "Произошла ошибка. Попробуйте ещё раз."
        try:
            await bot.api.messages.edit(
                peer_id=peer_id, message_id=thinking_id,
                message=msg,
            )
        except Exception:
            try:
                await bot.api.messages.send(
                    peer_id=peer_id, random_id=0, message=msg,
                )
            except Exception:
                pass


async def _handle_vk_video_extension(
    bot: Bot, vertex_service: VertexAIService,
    uid: int, peer_id: int, message: Any,
    video_bytes: bytes, caption: str,
) -> None:
    """Recreate Telegram's handle_video_extension for VK attachments."""
    from bot.user_settings import (
        video_supports_audio, calc_video_credits,
        video_supports_video_extension,
    )

    if is_blocked(uid):
        await message.answer("⛔ Ваш аккаунт заблокирован. Обратитесь к администратору.")
        return

    settings = get_user_settings(uid)
    user_model = settings.get("model", "gemini-3.1-flash-image-preview")
    model_label = AVAILABLE_MODELS.get(user_model, {}).get("label", user_model)

    if not is_video_model(user_model):
        await message.answer(
            "🎬 Видео получено, но сейчас выбран не видео-режим.\n\n"
            "Переключите модель на Veo 3.1 Lite и выберите задачу "
            "«🔄 Video extension» в настройках."
        )
        return

    video_task = settings.get("video_task", "text-to-video")
    if video_task != "video-extension":
        if not video_supports_video_extension(user_model):
            await message.answer(
                f"🎬 Модель {model_label} не поддерживает расширение видео.\n\n"
                "Используйте Veo 3.1 Lite для этой задачи."
            )
        else:
            await message.answer(
                "🎬 Видео получено! Чтобы расширить его, "
                "переключите задачу на «🔄 Video extension» в настройках видео.\n\n"
                f"Текущая задача: {video_task}"
            )
        return

    if not video_supports_video_extension(user_model):
        await message.answer(
            f"🎬 Модель {model_label} не поддерживает расширение видео.\n\n"
            "Используйте Veo 3.1 Lite для этой задачи."
        )
        return

    video_audio = settings.get("video_audio", True) and video_supports_audio(user_model)
    credits_cost = calc_video_credits(user_model, duration_seconds=8, audio=video_audio)
    if not reserve_credits(uid, credits_cost):
        await message.answer(
            "💳 Недостаточно кредитов\n\n"
            f"Расширение видео стоит {credits_cost} кредитов.\n"
            "Пополните баланс для продолжения."
        )
        return

    video_aspect = settings.get("video_aspect_ratio", "16:9")
    video_resolution = settings.get("video_resolution", "720p")
    prompt_display = caption[:100] if caption else "без дополнительного описания"

    base_text = (
        f"🔄 Расширяю видео…\n"
        f"🤖 {model_label}\n"
        f"📐 {video_aspect} • 7 сек • {video_resolution}\n"
        f"{prompt_display}{'…' if len(caption) > 100 else ''}"
    )
    processing_id = await bot.api.messages.send(
        peer_id=peer_id, random_id=0,
        message=f"{base_text}\n\n◐ Обработка — 0 сек.",
    )
    animator = VKProgressAnimator(bot, peer_id, processing_id, base_text)
    animator.start()
    start_time = time.monotonic()

    async def _do_video_ext() -> bytes:
        return await vertex_service.generate_video(
            prompt=caption or "Continue the video naturally",
            model=user_model,
            aspect_ratio=video_aspect,
            duration_seconds=7,
            resolution=video_resolution,
            generate_audio=video_audio,
            user_id=uid,
            username=f"vk:{uid}",
            video=video_bytes,
        )

    gen_task = asyncio.create_task(_do_video_ext())
    active_tasks[uid] = gen_task

    try:
        result_bytes = await gen_task
        await animator.stop()
        active_tasks.pop(uid, None)
        elapsed = int(time.monotonic() - start_time)

        upload_base = (
            f"🔄 Расширяю видео…\n🤖 {model_label}\n\n"
            f"✅ Готово за {elapsed} сек."
        )
        upload_animator = VKProgressAnimator(
            bot, peer_id, processing_id, upload_base,
            action_text="📤 Загрузка видео",
        )
        upload_animator.start()
        try:
            attachment = await upload_document_to_vk(
                bot.api, peer_id, result_bytes, filename="extended_video.mp4",
            )
        finally:
            await upload_animator.stop()

        result_caption = (
            f"✅ Видео расширено! ({elapsed} сек.)\n"
            f"{caption[:200] if caption else 'Без описания'}"
        )
        await bot.api.messages.send(
            peer_id=peer_id, random_id=0,
            message=result_caption,
            attachment=attachment,
            keyboard=get_persistent_keyboard(),
        )
        try:
            first_name = settings.get("first_name", "")
            confirm_credits(uid, credits_cost, first_name, platform="vk", gen_type="video_ext")
        except Exception:
            pass
        try:
            await bot.api.messages.delete(message_ids=[processing_id], delete_for_all=True)
        except Exception:
            pass

    except asyncio.CancelledError:
        await animator.stop()
        active_tasks.pop(uid, None)
        release_credits(uid, credits_cost)
        try:
            await bot.api.messages.edit(
                peer_id=peer_id, message_id=processing_id,
                message="⛔ Генерация отменена.",
            )
        except Exception:
            pass

    except SafetyFilterError as exc:
        await animator.stop()
        active_tasks.pop(uid, None)
        release_credits(uid, credits_cost)
        try:
            await bot.api.messages.edit(
                peer_id=peer_id, message_id=processing_id,
                message=f"🚫 Запрос заблокирован фильтрами безопасности\n\n{exc.user_message}",
            )
        except Exception:
            pass

    except QuotaExceededError:
        await animator.stop()
        active_tasks.pop(uid, None)
        release_credits(uid, credits_cost)
        try:
            await bot.api.messages.edit(
                peer_id=peer_id, message_id=processing_id,
                message=(
                    f"Модель {model_label} сейчас перегружена.\n\n"
                    "Попробуйте через пару минут или переключите модель."
                ),
                keyboard=get_switch_model_keyboard(user_model),
            )
        except Exception:
            pass

    except BotError as exc:
        await animator.stop()
        active_tasks.pop(uid, None)
        release_credits(uid, credits_cost)
        try:
            await bot.api.messages.edit(
                peer_id=peer_id, message_id=processing_id,
                message=exc.user_message,
                keyboard=get_switch_model_keyboard(user_model),
            )
        except Exception:
            pass

    except Exception as exc:
        await animator.stop()
        active_tasks.pop(uid, None)
        release_credits(uid, credits_cost)
        logger.exception("VK video extension error: %s", exc)
        try:
            await bot.api.messages.edit(
                peer_id=peer_id, message_id=processing_id,
                message="Не удалось расширить видео. Попробуйте ещё раз.",
                keyboard=get_switch_model_keyboard(user_model),
            )
        except Exception:
            pass


async def _generate_and_send(
    bot: Bot, vertex_service: VertexAIService,
    uid: int, peer_id: int, prompt: str,
    images: list[bytes] | None = None,
):
    if is_blocked(uid):
        await bot.api.messages.send(
            peer_id=peer_id, random_id=0,
            message="⛔ Ваш аккаунт заблокирован. Обратитесь к администратору.",
        )
        return

    settings = get_user_settings(uid)
    user_model = settings.get("model", "gemini-3.1-flash-image-preview")
    _is_video = is_video_model(user_model)
    _is_music = is_music_model(user_model)

    if _is_video and images:
        from bot.user_settings import video_supports_image as _vsi_check
        if not _vsi_check(user_model):
            model_label = AVAILABLE_MODELS.get(user_model, {}).get("label", user_model)
            await bot.api.messages.send(
                peer_id=peer_id, random_id=0,
                message=f"🎬 Модель {model_label} принимает только текстовые запросы.\n\n"
                        "Отправьте текстовое описание для генерации видео, "
                        "или переключите модель на Veo 3.1 / Veo 3.1 Fast для генерации видео по фото.",
            )
            return

    if _is_video:
        from bot.user_settings import calc_video_credits, video_supports_audio as _vsa
        _vd_cost = settings.get("video_duration", 8)
        if images:
            _vd_cost = 8  # image-to-video is forced to 8s
        _va_cost = settings.get("video_audio", True) and _vsa(user_model)
        credits_cost = calc_video_credits(user_model, duration_seconds=_vd_cost, audio=_va_cost)
    elif _is_music:
        credits_cost = get_music_credits_cost(user_model)
    else:
        credits_cost = 2 if settings.get("resolution") == "4k" else 1

    if not reserve_credits(uid, credits_cost):
        cost_label = f"{credits_cost} кредитов" if credits_cost > 1 else "1 кредит"
        msg = (
            f"💳 Недостаточно кредитов\n\n"
            f"Генерация {'видео' if _is_video else 'музыки' if _is_music else 'изображения'} стоит {cost_label}.\n"
            "Пополните баланс для продолжения."
        )
        await bot.api.messages.send(peer_id=peer_id, random_id=0, message=msg)
        return

    model_label = AVAILABLE_MODELS.get(user_model, {}).get("label", user_model)
    aspect_ratio = settings.get("aspect_ratio", "1:1")
    thinking_level = settings.get("thinking_level", "low")
    resolution = settings.get("resolution", "original")
    max_side = RESOLUTIONS.get(resolution, {}).get("max_side", 0)

    gen_type = "видео" if _is_video else "музыку" if _is_music else "изображение"
    action = "Редактирую" if images and not _is_video and not _is_music else "Генерирую"
    base_text = f"🎨 {action} {gen_type}...\n🤖 {model_label}"
    if _is_video:
        dur = settings.get("video_duration", 8)
        vres = settings.get("video_resolution", "720p")
        base_text += f"\n⏱ {dur} сек • 📺 {vres}"
    elif _is_music:
        duration_label = AVAILABLE_MODELS.get(user_model, {}).get("duration_label", "MP3")
        base_text += f"\n⏱ {duration_label} • MP3"

    processing_id = await bot.api.messages.send(
        peer_id=peer_id, random_id=0,
        message=f"{base_text}\n\n◐ Обработка — 0 сек.",
    )

    animator = VKProgressAnimator(bot, peer_id, processing_id, base_text)
    animator.start()

    start_time = time.monotonic()

    if _is_video:
        from bot.user_settings import video_supports_audio, video_supports_image as _vsi2
        video_aspect = settings.get("video_aspect_ratio", "16:9")
        video_duration = settings.get("video_duration", 8)
        video_resolution = settings.get("video_resolution", "720p")
        video_audio = settings.get("video_audio", True) and video_supports_audio(user_model)
        _ref_image: bytes | None = images[0] if images and _vsi2(user_model) else None
        if _ref_image is not None:
            video_duration = 8

        async def _do_generate() -> bytes:
            return await vertex_service.generate_video(
                prompt=prompt,
                model=user_model,
                aspect_ratio=video_aspect,
                duration_seconds=video_duration,
                resolution=video_resolution,
                generate_audio=video_audio,
                user_id=uid,
                username=f"vk:{uid}",
                image=_ref_image,
            )
    elif _is_music:
        async def _do_generate() -> bytes:
            return await vertex_service.generate_music(
                prompt=prompt,
                model=user_model,
                user_id=uid,
                username=f"vk:{uid}",
                image=images[0] if images else None,
            )
    else:
        async def _do_generate() -> bytes:
            raw = await vertex_service.generate_image(
                prompt=prompt,
                images=images,
                model_override=user_model,
                aspect_ratio=aspect_ratio,
                thinking_level=thinking_level,
                user_id=uid,
                username=f"vk:{uid}",
            )
            if max_side > 0:
                loop = asyncio.get_running_loop()
                raw = await loop.run_in_executor(None, _upscale_image, raw, max_side)
            return raw

    gen_task = asyncio.create_task(_do_generate())
    active_tasks[uid] = gen_task

    try:
        result_bytes = await gen_task
        await animator.stop()
        active_tasks.pop(uid, None)
        elapsed = int(time.monotonic() - start_time)

        if _is_video:
            caption = f"✅ Видео готово! ({elapsed} сек.)\n{prompt[:200]}"
            upload_base = f"🎨 {action} {gen_type}...\n🤖 {model_label}\n\n✅ Готово за {elapsed} сек."
            upload_animator = VKProgressAnimator(
                bot, peer_id, processing_id, upload_base,
                action_text="📤 Загрузка видео",
            )
            upload_animator.start()
            try:
                attachment = await upload_document_to_vk(bot.api, peer_id, result_bytes, filename="video.mp4")
            finally:
                await upload_animator.stop()
        elif _is_music:
            caption = f"✅ Музыка готова! ({elapsed} сек.)\n{prompt[:200]}"
            upload_base = f"🎨 {action} {gen_type}...\n🤖 {model_label}\n\n✅ Готово за {elapsed} сек."
            upload_animator = VKProgressAnimator(
                bot, peer_id, processing_id, upload_base,
                action_text="📤 Загрузка MP3",
            )
            upload_animator.start()
            try:
                attachment = await upload_document_to_vk(
                    bot.api,
                    peer_id,
                    result_bytes,
                    filename=_prompt_to_audio_filename(prompt),
                )
            finally:
                await upload_animator.stop()
        else:
            send_mode = settings.get("send_mode", "photo")
            caption = f"✅ Изображение готово! ({elapsed} сек.)\n{prompt[:200]}"
            upload_action = "📤 Загрузка файла" if send_mode == "document" else "📤 Загрузка фото"
            upload_base = f"🎨 {action} {gen_type}...\n🤖 {model_label}\n\n✅ Готово за {elapsed} сек."
            upload_animator = VKProgressAnimator(
                bot, peer_id, processing_id, upload_base,
                action_text=upload_action,
            )
            upload_animator.start()
            try:
                if send_mode == "document":
                    attachment = await upload_document_to_vk(bot.api, peer_id, result_bytes)
                else:
                    attachment = await upload_photo_to_vk(bot.api, peer_id, result_bytes)
            finally:
                await upload_animator.stop()

        await bot.api.messages.send(
            peer_id=peer_id, random_id=0,
            message=caption,
            attachment=attachment,
            keyboard=get_persistent_keyboard(),
        )

        try:
            first_name = settings.get("first_name", "")
            _gen_type_log = "video" if _is_video else "music" if _is_music else "image"
            confirm_credits(uid, credits_cost, first_name, platform="vk", prompt=prompt, model=user_model, gen_type=_gen_type_log)
        except Exception:
            pass

        if not _is_video and not _is_music:
            asyncio.create_task(log_generation_vk(
                image_bytes=result_bytes,
                prompt=prompt,
                user_id=uid,
                user_name=settings.get("first_name") or str(uid),
                model=user_model,
            ))

        try:
            await bot.api.messages.delete(
                message_ids=[processing_id], delete_for_all=True,
            )
        except Exception:
            pass

    except asyncio.CancelledError:
        await animator.stop()
        active_tasks.pop(uid, None)
        release_credits(uid, credits_cost)
        try:
            await bot.api.messages.edit(
                peer_id=peer_id, message_id=processing_id,
                message="⛔ Генерация отменена.",
            )
        except Exception:
            pass

    except SafetyFilterError as exc:
        await animator.stop()
        active_tasks.pop(uid, None)
        release_credits(uid, credits_cost)
        try:
            await bot.api.messages.edit(
                peer_id=peer_id, message_id=processing_id,
                message=f"🚫 Запрос заблокирован фильтрами безопасности\n\n{exc.user_message}",
            )
        except Exception:
            pass

    except QuotaExceededError:
        await animator.stop()
        active_tasks.pop(uid, None)
        release_credits(uid, credits_cost)
        current_name = AVAILABLE_MODELS.get(user_model, {}).get("label", user_model)
        try:
            await bot.api.messages.edit(
                peer_id=peer_id, message_id=processing_id,
                message=f"Модель {current_name} сейчас перегружена.\n\n"
                        "Попробуйте через пару минут или переключите модель.",
                keyboard=get_switch_model_keyboard(user_model),
            )
        except Exception:
            pass

    except BotError as exc:
        await animator.stop()
        active_tasks.pop(uid, None)
        release_credits(uid, credits_cost)
        try:
            await bot.api.messages.edit(
                peer_id=peer_id, message_id=processing_id,
                message=exc.user_message,
                keyboard=get_switch_model_keyboard(user_model),
            )
        except Exception:
            pass

    except Exception as exc:
        await animator.stop()
        active_tasks.pop(uid, None)
        release_credits(uid, credits_cost)
        logger.exception("VK generation error: %s", exc)
        try:
            await bot.api.messages.edit(
                peer_id=peer_id, message_id=processing_id,
                message=f"Не удалось сгенерировать {gen_type}.\nПопробуйте ещё раз.",
                keyboard=get_switch_model_keyboard(user_model),
            )
        except Exception:
            pass
