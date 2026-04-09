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
)
from bot.keyboards import ASPECT_RATIOS
from core.exceptions import BotError, QuotaExceededError, SafetyFilterError

from vk_bot.keyboards import (
    get_persistent_keyboard,
    get_settings_keyboard,
    get_switch_model_keyboard,
    get_chat_cancel_keyboard,
    get_balance_keyboard,
)
from vk_bot.photo_upload import upload_photo_to_vk, upload_document_to_vk, download_vk_photo

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

    @bot.on.message(text=list(SETTINGS_TEXTS))
    async def cmd_settings(message: Message):
        uid = message.from_id
        await message.answer(
            "⚙️ Настройки\n\nВыберите что изменить:",
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

        purchased = max(0, credits - FREE_CREDITS) if credits > FREE_CREDITS else 0
        free_left = min(credits, FREE_CREDITS)

        text = "💰 Ваш баланс\n\n"
        text += f"🔋 Всего кредитов: {credits}\n"
        if purchased > 0:
            text += f"💎 Купленные: {purchased}\n"
            text += f"🎁 Бесплатные: {free_left}\n"
        else:
            text += f"🎁 Бесплатные: {free_left} из {FREE_CREDITS}\n"
        text += f"🎨 Сгенерировано: {generations}\n\n"
        text += "Выберите пакет для пополнения:"

        await message.answer(text, keyboard=get_balance_keyboard())

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
        uid = message.from_id
        _chat_sessions[uid] = []
        await message.answer(
            "💬 Режим «Чат» — gemini-3.1-pro-preview\n\n"
            "Принимаю: текст, фото, голосовые, аудио, документы (PDF/текст).\n\n"
            "Для выхода нажмите ⛔ Стоп",
            keyboard=get_chat_cancel_keyboard(),
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
            await edit_msg("⚙️ Настройки\n\nВыберите что изменить:", get_settings_keyboard(uid))

        elif cmd == "choose_model":
            from vk_bot.keyboards import get_model_keyboard
            lines = ["🤖 Выберите модель:\n"]
            for model_id, info in AVAILABLE_MODELS.items():
                lines.append(f"  {info['label']}\n  {info['desc']}\n")
            await edit_msg("\n".join(lines), get_model_keyboard(uid))

        elif cmd == "set_model":
            model_id = data.get("id", "")
            if model_id in AVAILABLE_MODELS:
                settings = get_user_settings(uid)
                settings["model"] = model_id
                save_user_settings(uid)
            await edit_msg("⚙️ Настройки\n\nВыберите что изменить:", get_settings_keyboard(uid))

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

        elif cmd == "switch_model":
            model_id = data.get("id", "")
            if model_id in AVAILABLE_MODELS:
                settings = get_user_settings(uid)
                settings["model"] = model_id
                save_user_settings(uid)
                info = AVAILABLE_MODELS[model_id]
                await edit_msg(f"✅ Модель переключена на {info['label']}\n\nОтправьте запрос ещё раз.")

        elif cmd == "buy":
            from bot.services.freekassa_service import create_payment_url, CREDIT_PACKAGES as FK_PACKAGES
            pack_key = data.get("pack", "")
            pack = FK_PACKAGES.get(pack_key)
            if not pack:
                await edit_msg("Неизвестный пакет.")
                return
            result = create_payment_url(uid, pack_key)
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
            for att in message.attachments:
                if att.photo:
                    caption = text or ""
                    if not caption:
                        await message.answer(
                            "📷 Фото получено! Добавьте описание — что нужно сделать с изображением."
                        )
                        return
                    photo_bytes = await download_vk_photo(bot.api, att.photo.sizes)
                    await _generate_and_send(
                        bot, vertex_service, uid, peer_id, caption,
                        images=[photo_bytes],
                    )
                    return

        if not text:
            await message.answer("Отправьте текстовое описание изображения.")
            return

        await _generate_and_send(bot, vertex_service, uid, peer_id, text)


async def _handle_vk_chat_message(
    bot: Bot, vertex_service: VertexAIService,
    uid: int, peer_id: int, message: Any,
):
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
                    parts.append({"type": "text", "text": f"[прикреплён файл: {fname} — формат не поддерживается]"})

    if not parts:
        await bot.api.messages.send(
            peer_id=peer_id, random_id=0,
            message="Не удалось разобрать сообщение. Попробуйте ещё раз.",
        )
        return

    history.append({"role": "user", "parts": parts})

    thinking_id = await bot.api.messages.send(
        peer_id=peer_id, random_id=0,
        message="💭 Думаю...",
    )

    try:
        contents = _build_chat_api_contents(history)
        response = await vertex_service.chat_text(contents)

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

        reply = response[:4096]
        await bot.api.messages.edit(
            peer_id=peer_id, message_id=thinking_id,
            message=reply,
            keyboard=get_chat_cancel_keyboard(),
        )
        if len(response) > 4096:
            for i in range(4096, len(response), 4096):
                await bot.api.messages.send(
                    peer_id=peer_id, random_id=0,
                    message=response[i:i + 4096],
                )

    except Exception as exc:
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
    credits_cost = 2 if settings.get("resolution") == "4k" else 1

    if not has_credits(uid, credits_cost):
        msg = (
            "💳 Кредиты закончились\n\n"
            "У вас больше нет доступных генераций.\n"
            "Для продолжения работы приобретите пополнение кредитов."
            if credits_cost == 1 else
            "💳 Недостаточно кредитов\n\n"
            "Генерация в разрешении 4K стоит 2 кредита.\n"
            "Понизьте разрешение в настройках или пополните баланс."
        )
        await bot.api.messages.send(peer_id=peer_id, random_id=0, message=msg)
        return

    user_model = settings.get("model", "gemini-3.1-flash-image-preview")
    model_label = AVAILABLE_MODELS.get(user_model, {}).get("label", user_model)
    aspect_ratio = settings.get("aspect_ratio", "1:1")
    thinking_level = settings.get("thinking_level", "low")
    resolution = settings.get("resolution", "original")
    max_side = RESOLUTIONS.get(resolution, {}).get("max_side", 0)

    action = "Редактирую" if images else "Генерирую"
    base_text = f"🎨 {action} изображение...\n🤖 {model_label}"
    processing_id = await bot.api.messages.send(
        peer_id=peer_id, random_id=0,
        message=f"{base_text}\n\n◐ Обработка — 0 сек.",
    )

    animator = VKProgressAnimator(bot, peer_id, processing_id, base_text)
    animator.start()

    start_time = time.monotonic()

    async def _do_generate() -> bytes:
        raw = await vertex_service.generate_image(
            prompt=prompt,
            images=images,
            model_override=user_model,
            aspect_ratio=aspect_ratio,
            thinking_level=thinking_level,
        )
        if max_side > 0:
            loop = asyncio.get_running_loop()
            raw = await loop.run_in_executor(None, _upscale_image, raw, max_side)
        return raw

    gen_task = asyncio.create_task(_do_generate())
    active_tasks[uid] = gen_task

    try:
        image_bytes = await gen_task
        await animator.stop()
        active_tasks.pop(uid, None)
        elapsed = int(time.monotonic() - start_time)

        send_mode = settings.get("send_mode", "photo")
        caption = f"✅ Изображение готово! ({elapsed} сек.)\n{prompt[:200]}"

        upload_action = "📤 Загрузка файла" if send_mode == "document" else "📤 Загрузка фото"
        upload_base = f"🎨 {action} изображение...\n🤖 {model_label}\n\n✅ Готово за {elapsed} сек."
        upload_animator = VKProgressAnimator(
            bot, peer_id, processing_id, upload_base,
            action_text=upload_action,
        )
        upload_animator.start()

        try:
            if send_mode == "document":
                attachment = await upload_document_to_vk(bot.api, peer_id, image_bytes)
            else:
                attachment = await upload_photo_to_vk(bot.api, peer_id, image_bytes)
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
            increment_generations(uid, first_name, platform="vk", credits_cost=credits_cost)
        except Exception:
            pass

        try:
            await bot.api.messages.delete(
                message_ids=[processing_id], delete_for_all=True,
            )
        except Exception:
            pass

    except asyncio.CancelledError:
        await animator.stop()
        active_tasks.pop(uid, None)
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
        logger.exception("VK generation error: %s", exc)
        try:
            await bot.api.messages.edit(
                peer_id=peer_id, message_id=processing_id,
                message="Не удалось сгенерировать изображение.\nПопробуйте ещё раз.",
                keyboard=get_switch_model_keyboard(user_model),
            )
        except Exception:
            pass
