"""
bot/autopub/generator.py
~~~~~~~~~~~~~~~~~~~~~~~~
Uses Gemini (via VertexAIService) to:
1. Invent a trending topic + image prompt + post caption
2. Generate the image
3. Upload the draft image to Telegram (log channel) → get file_id for later use
"""
from __future__ import annotations

import logging
import os
import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.services.vertex_ai_service import VertexAIService

logger = logging.getLogger(__name__)

_TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

_DEFAULT_TOPICS = [
    "домашний уют", "lifestyle фото", "мода и стиль", "природа и лето",
    "кофе и утро", "путешествия", "красота и макияж", "осенние прогулки",
    "зимние вечера", "книги и отдых", "спорт и здоровье", "романтика",
]

_IDEA_PROMPT_TEMPLATE = """Ты — креативный контент-менеджер для Telegram-канала об AI-генерации изображений.

Тематика контента: {topics}
Стиль изображений: {style}

Придумай одну идею для поста. Верни ответ строго в формате JSON (без markdown, без ```):
{{
  "topic": "Краткое название темы (до 40 символов), например: ❤️ Домашний уют",
  "prompt": "Детальный промпт на русском языке для генерации изображения (200-400 слов). Опиши: главный объект, позу/действие, одежду, антураж, освещение, стиль фото (lifestyle, editorial и т.д.), соотношение сторон 9:16",
  "caption_intro": "Короткий эмоциональный заголовок для поста (до 60 символов)"
}}

Промпт должен быть очень подробным — как профессиональное ТЗ для фотографа."""


async def generate_post_idea(
    vertex_service: "VertexAIService",
    topic_hints: str = "",
    image_style: str = "",
) -> dict | None:
    """Ask Gemini to invent a topic + prompt + caption. Returns dict or None on error."""
    import json

    topics = topic_hints.strip() or ", ".join(random.sample(_DEFAULT_TOPICS, 4))
    style = image_style.strip() or "lifestyle, editorial, реалистичная фотография"

    idea_prompt = _IDEA_PROMPT_TEMPLATE.format(topics=topics, style=style)

    try:
        text = await vertex_service.chat_text([
            {"role": "user", "parts": [{"type": "text", "text": idea_prompt}]}
        ])
        text = text.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        data = json.loads(text)
        if not all(k in data for k in ("topic", "prompt", "caption_intro")):
            raise ValueError(f"Missing keys in response: {data}")
        return data
    except Exception as exc:
        logger.error("autopub generator: idea generation failed: %s", exc)
        return None


async def generate_image_for_post(
    vertex_service: "VertexAIService",
    prompt: str,
    model: str = "",
) -> bytes | None:
    """Generate image bytes using VertexAIService."""
    from bot.config import get_settings
    settings = get_settings()
    use_model = model or settings.image_model or "gemini-3.1-flash-image-preview"
    try:
        image_bytes = await vertex_service.generate_image(
            prompt=prompt,
            model=use_model,
            aspect_ratio="9:16",
        )
        return image_bytes
    except Exception as exc:
        logger.error("autopub generator: image generation failed: %s", exc)
        return None


async def upload_draft_to_telegram(image_bytes: bytes, caption: str) -> tuple[str, str] | None:
    """
    Send image to the Telegram log channel to get a stable file_id.
    Returns (file_id, file_unique_id) or None on error.
    """
    import aiohttp

    if not _TG_TOKEN:
        logger.warning("autopub: TELEGRAM_BOT_TOKEN not set, cannot upload draft")
        return None

    from bot.log_channel import LOG_CHANNEL_ID

    url = f"https://api.telegram.org/bot{_TG_TOKEN}/sendPhoto"
    try:
        data = aiohttp.FormData()
        data.add_field("chat_id", str(LOG_CHANNEL_ID))
        data.add_field("caption", f"📝 [autopub draft]\n{caption[:200]}")
        data.add_field("parse_mode", "HTML")
        data.add_field("photo", image_bytes, filename="draft.jpg", content_type="image/jpeg")

        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                body = await resp.json(content_type=None)

        if not body.get("ok"):
            logger.error("autopub: TG upload failed: %s", body)
            return None

        photos = body["result"].get("photo", [])
        if not photos:
            return None
        largest = max(photos, key=lambda p: p.get("file_size", 0))
        return largest["file_id"], largest["file_unique_id"]
    except Exception as exc:
        logger.error("autopub: TG upload exception: %s", exc)
        return None


def build_post_text(
    topic: str,
    caption_intro: str,
    prompt: str,
    post_template: str,
    post_cta: str,
    bot_username: str,
) -> str:
    """Assemble final post caption from components."""
    if post_template.strip():
        try:
            return post_template.format(
                topic=topic,
                caption_intro=caption_intro,
                prompt=prompt,
                bot_username=bot_username,
                cta=post_cta,
            )
        except Exception:
            pass

    bot_link = f"@{bot_username}" if bot_username else ""
    cta = post_cta.strip() or (
        f"✅ Переходи в бот {bot_link} и выбирай раздел <b>своё описание</b>\n"
        f"✅ Отправляй фото с описанием\n"
        f"✅ Перед отправкой добавляй промпт ниже 👇" if bot_username
        else "✅ Используй промпт ниже 👇 в нашем боте"
    )

    return (
        f"{topic}\n\n"
        f"{cta}\n\n"
        f"<code>{prompt}</code>"
    )
