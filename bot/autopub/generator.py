"""
bot/autopub/generator.py
~~~~~~~~~~~~~~~~~~~~~~~~
Uses Gemini (via VertexAIService) to:
1. Search the internet for current trending topics/memes (Google Search grounding)
2. Invent a post idea + detailed image prompt + caption based on the trend
3. Generate the image
4. Upload the draft image to Telegram (log channel) → get file_id for later use
"""
from __future__ import annotations

import logging
import os
import random
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bot.services.vertex_ai_service import VertexAIService

logger = logging.getLogger(__name__)

_TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

_DEFAULT_TOPICS = [
    "домашний уют", "lifestyle фото", "мода и стиль", "природа",
    "кофе и утро", "путешествия", "красота и макияж", "прогулки",
    "зимние вечера", "книги и отдых", "спорт и здоровье", "романтика",
    "примерка одежды", "дизайн интерьера", "расстановка мебели",
    "аватар для соцсетей", "арт-стиль (аниме, Ghibli, Pixar)",
    "фото в стиле ретро", "кинопостер из портрета", "food-фото",
    "портрет питомца", "смена причёски", "визуализация ремонта",
    "фото из путешествия", "профессиональное портфолио",
]

# ─── Trend search ────────────────────────────────────────────────────────────

_TREND_SEARCH_PROMPT = """Сегодня {today_date} ({today_weekday}).
{holiday_block}

Используй поиск в интернете и найди 5-7 АКТУАЛЬНЫХ трендов, мемов или вирусных тем, \
которые сейчас (сегодня) популярны в русскоязычном интернете, Instagram, TikTok, ВКонтакте или Telegram.

ВАЖНО: Разнообразие! Каждый раз предлагай РАЗНЫЕ категории:
— Мода/стиль/примерка одежды
— Интерьер/мебель/ремонт
— Арт-стили (аниме, Ghibli, Pixar, масло, акварель)
— Портреты/селфи/аватары
— Путешествия/локации
— Еда/рецепты
— Питомцы
— Праздники и события (если есть ближайшие)
— Ретро/винтаж/эпохи
— Кино/сериалы/поп-культура

Уже использованные темы (НЕ повторяй их):
{used_topics}

Верни ответ строго в формате JSON-массива (без markdown, без ```):
[
  {{"trend": "Название тренда", "context": "1-2 предложения что это и почему вирусное"}},
  ...
]

Требования:
- Только реальные актуальные тренды, которые сейчас обсуждают
- Подходящие для визуального контента (можно сгенерировать красивое изображение)
- Без политики, новостей катастроф, 18+ тематики
- РАЗНЫЕ категории в каждом запросе — не зацикливайся на одной теме!
- Если сегодня или скоро праздник — обязательно включи 1-2 тренда по нему"""


_RU_HOLIDAYS = {
    (1, 1): "Новый год 🎄", (1, 7): "Рождество",
    (2, 14): "День святого Валентина 💕", (2, 23): "День защитника Отечества",
    (3, 8): "Международный женский день 🌷", (3, 14): "День числа Пи",
    (4, 1): "День смеха 😂", (4, 12): "День космонавтики 🚀",
    (5, 1): "Первомай", (5, 9): "День Победы 🎖️",
    (6, 1): "День защиты детей", (6, 12): "День России",
    (7, 8): "День семьи, любви и верности 💑",
    (8, 22): "День Государственного флага",
    (9, 1): "День знаний 📚",
    (10, 5): "День учителя", (10, 31): "Хэллоуин 🎃",
    (11, 4): "День народного единства",
    (12, 25): "Рождество (западное) 🎄", (12, 31): "Канун Нового года 🎆",
}
_RU_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

def _get_holiday_block() -> tuple[str, str, str]:
    import datetime
    msk = datetime.timezone(datetime.timedelta(hours=3))
    now = datetime.datetime.now(msk)
    today_str = now.strftime("%d %B %Y")
    weekday = _RU_WEEKDAYS[now.weekday()]

    holidays_near = []
    for delta in range(0, 7):
        d = now + datetime.timedelta(days=delta)
        h = _RU_HOLIDAYS.get((d.month, d.day))
        if h:
            prefix = "Сегодня" if delta == 0 else ("Завтра" if delta == 1 else f"Через {delta} дней")
            holidays_near.append(f"{prefix}: {h} ({d.day}.{d.month:02d})")

    if holidays_near:
        block = "Ближайшие праздники:\n" + "\n".join(f"  🎉 {h}" for h in holidays_near)
    else:
        block = ""
    return today_str, weekday, block


async def search_current_trends(
    vertex_service: "VertexAIService",
    used_topics: list[str] | None = None,
) -> list[dict] | None:
    """
    Ask Gemini (with Google Search) for today's trending topics.
    Returns list of {trend, context} dicts or None on error.
    """
    import json

    used_str = "\n".join(f"- {t}" for t in (used_topics or [])[:30]) or "(нет)"
    today_date, today_weekday, holiday_block = _get_holiday_block()
    prompt_text = _TREND_SEARCH_PROMPT.format(
        used_topics=used_str,
        today_date=today_date,
        today_weekday=today_weekday,
        holiday_block=holiday_block,
    )

    search_model = getattr(vertex_service, "SEARCH_MODEL", "gemini-3.1-flash-lite-preview")
    logger.info("[autopub gen] поиск актуальных трендов через Google Search (модель=%s)...", search_model)
    try:
        text = await vertex_service.chat_text([
            {"role": "user", "parts": [{"text": prompt_text}]}
        ], model_override=search_model, use_search=True)
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        trends = json.loads(text)
        if isinstance(trends, list) and trends:
            logger.info("[autopub gen] найдено %d трендов", len(trends))
            for t in trends:
                logger.info("[autopub gen]   • %s", t.get("trend", "?"))
            return trends
        logger.warning("[autopub gen] trends parse OK but empty list")
    except Exception as exc:
        logger.error("[autopub gen] ошибка поиска трендов: %s", exc)
    return None


async def search_idea_context(
    vertex_service: "VertexAIService",
    user_idea: str,
) -> str:
    """
    Search the web for context around the admin's idea so Gemini can create
    a richer, more specific post.  Returns a plain-text summary string.
    """
    prompt_text = (
        f"Используй поиск в интернете и найди актуальную информацию, вдохновляющие детали "
        f"и интересные факты по теме: «{user_idea}»\n\n"
        "Напиши 3-5 предложений на русском языке: что сейчас популярно или интересно "
        "в этой теме, какие детали и нюансы сделают визуальный контент более современным "
        "и привлекательным. Без JSON — просто полезный текстовый контекст."
    )
    search_model = getattr(vertex_service, "SEARCH_MODEL", "gemini-3.1-flash-lite-preview")
    logger.info("[autopub gen] поиск контекста по идее «%s» (модель=%s)...", user_idea[:60], search_model)
    try:
        text = await vertex_service.chat_text([
            {"role": "user", "parts": [{"text": prompt_text}]}
        ], model_override=search_model, use_search=True)
        result = text.strip() if text else user_idea
        logger.info("[autopub gen] контекст идеи получен, %d символов", len(result))
        return result
    except Exception as exc:
        logger.error("[autopub gen] ошибка поиска контекста идеи: %s", exc)
        return user_idea


# ─── Idea generation ─────────────────────────────────────────────────────────

_IDEA_PROMPT_TEMPLATE = """Ты — креативный контент-менеджер для Telegram-канала об AI-генерации изображений.

Тематика канала: {topics}
Стиль изображений: {style}
{trend_block}
{feedback_block}
Придумай одну идею для поста. В посте будет ДВА промпта:

1. «prompt» — промпт ДЛЯ ПОЛЬЗОВАТЕЛЕЙ. Они скопируют его, отправят в бот со своими фото и получат результат.
   ПРАВИЛА:
   — Промпт должен быть ТЕМАТИЧЕСКИМ — точно про тему поста! Если тема про мебель — промпт про мебель. Если про стиль портрета — про стиль портрета.
   — Пиши простым языком, понятным любому человеку.
   — НЕ описывай конкретные объекты (какой именно диван, какая комната) — пользователь сам приложит свои фото.
   — Используй слова «this», «my» для ссылки на фото пользователя.
   — Примеры: "Place this piece of furniture into my room and show how it would look with natural lighting" (тема — мебель), "Transform my portrait into a cinematic movie poster" (тема — кино-постер), "Dress me in this outfit and show a full look" (тема — примерка одежды)

2. «image_prompt» — ВНУТРЕННИЙ промпт для генерации ПРИМЕРА-ИЛЛЮСТРАЦИИ. Пользователи НЕ увидят. Подробный, технический, с конкретикой.

Верни ответ строго в формате JSON (без markdown, без ```):
{{
  "topic": "Краткое название темы (до 40 символов)",
  "caption": "Текст поста (до 800 символов). Объясни идею, покажи как пользоваться (какие фото отправить + промпт). CTA. Без хэштегов.",
  "prompt": "Тематический промпт на английском (до 800 символов). Точно про тему поста. Простой язык. Ссылается на фото пользователя через this/my. Без конкретных объектов — пользователь приложит свои фото.",
  "image_prompt": "Детальный технический промпт на английском (до 800 символов) для иллюстрации. Формат 4:5 (portrait). Конкретная сцена, освещение, стиль.",
  "caption_intro": "Короткий эмоциональный заголовок (до 60 символов)"
}}"""

_USER_IDEA_PROMPT_TEMPLATE = """Ты — главный креативный директор Telegram/VK канала @picgenai об AI-генерации изображений.

Администратор дал тебе ИДЕЮ для поста:
«{user_idea}»

Вот дополнительный контекст из интернета по этой теме:
{idea_context}

Стиль изображений канала: {style}

У тебя ПОЛНАЯ ТВОРЧЕСКАЯ СВОБОДА. Твоя главная цель — создать пост, который заставит людей захотеть попробовать это самим через бота. Подумай глубоко:

— Как лучше всего «продать» эту идею подписчикам?
— Какой визуальный пример будет максимально впечатляющим — до/после, сравнение стилей, wow-эффект, неожиданный ракурс, контраст?
— Как подать текст: вопросом, провокацией, историей, вдохновением, лайфхаком?
— Что зацепит зрителя: эмоция, практическая польза, эстетика, удивление?

В посте будет ДВА промпта:

1. «prompt» — промпт ДЛЯ ПОЛЬЗОВАТЕЛЕЙ. Они скопируют его, отправят в бот со своими фото и получат результат.
   ПРАВИЛА:
   — Промпт должен быть ТЕМАТИЧЕСКИМ — точно про тему/идею поста!
   — Простым языком, понятным любому.
   — НЕ описывай конкретные объекты — пользователь приложит свои фото.
   — Используй «this», «my» для ссылки на фото пользователя.
   — Примеры: "Place this piece of furniture into my room and show how it would look" (мебель), "Transform my portrait into a cinematic movie poster" (кино), "Dress me in this outfit and show a full look" (одежда)
   
2. «image_prompt» — ВНУТРЕННИЙ промпт для генерации ПРИМЕРА-ИЛЛЮСТРАЦИИ. Пользователи НЕ увидят. Подробный, конкретный, технический.

Ограничение: текст поста (caption) — максимум 800 символов.

{feedback_block}

Верни ответ строго в формате JSON (без markdown, без ```):
{{
  "topic": "Краткое название темы (до 40 символов)",
  "caption": "Текст поста (до 800 символов). Объясни идею и КАК пользоваться. CTA. Без хэштегов.",
  "prompt": "Тематический промпт на английском (до 800 символов). Точно по теме поста. Простой язык. this/my для фото пользователя.",
  "image_prompt": "Детальный технический промпт на английском (до 800 символов) для иллюстрации. Формат 4:5 (portrait). Конкретная сцена, стиль.",
  "caption_intro": "Короткий эмоциональный заголовок (до 60 символов)"
}}

Удиви."""


async def generate_post_idea(
    vertex_service: "VertexAIService",
    topic_hints: str = "",
    image_style: str = "",
    trend_context: str = "",
    admin_feedback: str = "",
    on_thought: Any | None = None,
    user_idea: str = "",
    idea_context: str = "",
) -> dict | None:
    """Ask Gemini Pro to invent a topic + prompt + caption. Returns dict or None."""
    import json

    style = image_style.strip() or "lifestyle, editorial, реалистичная фотография"

    feedback_block = ""
    if admin_feedback:
        feedback_block = (
            f"\nПредыдущий пост был отклонён с комментарием:\n"
            f"\"{admin_feedback}\"\n"
            f"Учти этот фидбэк и сделай пост лучше.\n"
        )

    if user_idea:
        idea_prompt = _USER_IDEA_PROMPT_TEMPLATE.format(
            user_idea=user_idea,
            idea_context=idea_context or "(нет дополнительного контекста)",
            style=style,
            feedback_block=feedback_block,
        )
    else:
        topics = topic_hints.strip() or ", ".join(random.sample(_DEFAULT_TOPICS, 4))
        trend_block = ""
        if trend_context:
            trend_block = f"\nАктуальный тренд для поста:\n{trend_context}\nИспользуй этот тренд как основу идеи.\n"
        idea_prompt = _IDEA_PROMPT_TEMPLATE.format(
            topics=topics, style=style,
            trend_block=trend_block, feedback_block=feedback_block,
        )

    pro_model = getattr(vertex_service, "CHAT_MODEL", "gemini-3.1-pro-preview")
    logger.info("[autopub gen] генерирую идею поста модель=%s (trend=%s feedback=%s)",
                pro_model, bool(trend_context), bool(admin_feedback))
    try:
        text = await vertex_service.chat_text(
            [{"role": "user", "parts": [{"text": idea_prompt}]}],
            on_thought=on_thought,
        )
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        data = json.loads(text)
        if not all(k in data for k in ("topic", "prompt")):
            raise ValueError(f"Missing keys in response: {list(data.keys())}")
        if "caption_intro" not in data:
            data["caption_intro"] = data.get("caption", data["topic"])[:60]
        if "caption" not in data:
            data["caption"] = data.get("caption_intro", "")
        data["caption"] = data["caption"][:900]
        original_prompt_len = len(data["prompt"])
        data["prompt"] = data["prompt"][:800]
        if original_prompt_len > 800:
            logger.warning("[autopub gen] user prompt обрезан с %d до 800 символов", original_prompt_len)
        if "image_prompt" in data:
            orig_ip = len(data["image_prompt"])
            data["image_prompt"] = data["image_prompt"][:800]
            if orig_ip > 800:
                logger.warning("[autopub gen] image_prompt обрезан с %d до 800 символов", orig_ip)
        else:
            data["image_prompt"] = data["prompt"]
            logger.info("[autopub gen] image_prompt не найден, используем prompt")
        logger.info("[autopub gen] идея OK: topic=%r  prompt_len=%d  image_prompt_len=%d  caption_len=%d",
                    data["topic"], len(data["prompt"]), len(data["image_prompt"]), len(data["caption"]))
        return data
    except Exception as exc:
        logger.error("[autopub gen] ошибка генерации идеи: %s", exc)
        return None


# ─── Image generation ────────────────────────────────────────────────────────

async def generate_image_for_post(
    vertex_service: "VertexAIService",
    prompt: str,
    model: str = "",
) -> bytes | None:
    """Generate image bytes using VertexAIService."""
    use_model = model or "gemini-3.1-flash-image-preview"
    logger.info("[autopub gen] генерирую изображение, модель=%s", use_model)
    try:
        image_bytes = await vertex_service.generate_image(
            prompt=prompt,
            model_override=use_model,
            aspect_ratio="4:5",
        )
        return image_bytes
    except Exception as exc:
        logger.error("[autopub gen] ошибка генерации изображения: %s", exc)
        return None


_IMAGE_VARIATION_SUFFIXES = [
    "",
    " Use a different creative angle, composition, and color palette. Show a fresh perspective.",
    " Create a completely different artistic interpretation with unique lighting and mood.",
]


async def generate_multiple_images(
    vertex_service: "VertexAIService",
    prompt: str,
    count: int = 3,
    model: str = "",
) -> list[bytes]:
    """Generate multiple image variations for a post. Returns list of image bytes."""
    results: list[bytes] = []
    for i in range(count):
        suffix = _IMAGE_VARIATION_SUFFIXES[i] if i < len(_IMAGE_VARIATION_SUFFIXES) else f" Variation {i+1}, unique composition."
        varied_prompt = prompt + suffix
        logger.info("[autopub gen] генерирую изображение %d/%d...", i + 1, count)
        img = await generate_image_for_post(vertex_service, varied_prompt, model)
        if img:
            results.append(img)
            logger.info("[autopub gen] изображение %d/%d OK — %.1f KB", i + 1, count, len(img) / 1024)
        else:
            logger.warning("[autopub gen] изображение %d/%d FAILED — пропускаю", i + 1, count)
    return results


# ─── TG upload ───────────────────────────────────────────────────────────────

async def upload_draft_to_telegram(image_bytes: bytes, caption: str) -> tuple[str, str] | None:
    """
    Send image to the Telegram log channel to get a stable file_id.
    Returns (file_id, file_unique_id) or None on error.
    """
    import aiohttp

    if not _TG_TOKEN:
        logger.warning("[autopub gen] TELEGRAM_BOT_TOKEN не задан — нельзя загрузить черновик")
        return None

    from bot.log_channel import LOG_CHANNEL_ID

    url = f"https://api.telegram.org/bot{_TG_TOKEN}/sendPhoto"
    logger.info("[autopub gen] загружаю черновик в TG лог-канал %s...", LOG_CHANNEL_ID)
    try:
        data = aiohttp.FormData()
        data.add_field("chat_id", str(LOG_CHANNEL_ID))
        safe_cap = caption[:200].replace("<", "").replace(">", "").replace("&", "")
        data.add_field("caption", f"📝 [autopub draft]\n{safe_cap}")
        data.add_field("photo", image_bytes, filename="draft.jpg", content_type="image/jpeg")

        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                body = await resp.json(content_type=None)

        if not body.get("ok"):
            logger.error("[autopub gen] TG upload failed: %s", body)
            return None

        photos = body["result"].get("photo", [])
        if not photos:
            logger.error("[autopub gen] TG upload: нет фото в ответе")
            return None
        largest = max(photos, key=lambda p: p.get("file_size", 0))
        logger.info("[autopub gen] TG upload OK: file_id=%s...", largest["file_id"][:20])
        return largest["file_id"], largest["file_unique_id"]
    except Exception as exc:
        logger.error("[autopub gen] TG upload exception: %s", exc)
        return None


async def upload_extra_images_to_telegram(images: list[bytes]) -> list[str]:
    """Upload extra images to TG log channel, return list of file_ids."""
    import aiohttp
    if not _TG_TOKEN or not images:
        return []
    from bot.log_channel import LOG_CHANNEL_ID
    file_ids: list[str] = []
    url = f"https://api.telegram.org/bot{_TG_TOKEN}/sendPhoto"
    try:
        async with aiohttp.ClientSession() as session:
            for i, img_bytes in enumerate(images):
                data = aiohttp.FormData()
                data.add_field("chat_id", str(LOG_CHANNEL_ID))
                data.add_field("caption", f"📝 [autopub extra {i+2}]")
                data.add_field("photo", img_bytes, filename=f"extra_{i+2}.jpg", content_type="image/jpeg")
                async with session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    body = await resp.json(content_type=None)
                if body.get("ok"):
                    photos = body["result"].get("photo", [])
                    if photos:
                        largest = max(photos, key=lambda p: p.get("file_size", 0))
                        file_ids.append(largest["file_id"])
                        logger.info("[autopub gen] extra image %d upload OK: %s...", i + 2, largest["file_id"][:20])
                else:
                    logger.warning("[autopub gen] extra image %d upload failed: %s", i + 2, str(body)[:100])
    except Exception as exc:
        logger.error("[autopub gen] extra images upload exception: %s", exc)
    return file_ids


# ─── Post text builder ───────────────────────────────────────────────────────

def build_post_text(
    topic: str,
    caption_intro: str,
    prompt: str,
    post_template: str,
    post_cta: str,
    bot_username: str,
    gemini_caption: str = "",
) -> str:
    """Assemble final post text as HTML (for Telegram text message with <code> prompt block).

    Structure:
      {title}

      • Переходи в бот и выбирай раздел свое описание @bot
      • Отправляй качественное фото, где чётко видны пропорции лица.

      • Перед отправкой добавляй промт в описание (копируй текст одним касанием 👇):

      <code>prompt</code>

      ✅После исправления описания, отправляйте на генерацию.
      ✅Наслаждаемся эксклюзивом и благодарим разработчика бота, за чудесные фото.
      ✅Делись своим шедевром в комментариях)))
    """
    # Custom template takes priority (user-defined, no HTML enforced)
    if post_template.strip():
        try:
            result = post_template.format(
                topic=topic,
                caption_intro=caption_intro,
                prompt=prompt,
                bot_username=bot_username,
                cta=post_cta,
            )
            return result
        except Exception:
            pass

    title = (caption_intro.strip() or topic.strip())[:80]
    bot_at = f"@{bot_username}" if bot_username else ""

    # Escape prompt for HTML (< > &)
    safe_prompt = prompt.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # Build all parts except prompt so we know how much space is left (TG caption limit 1024)
    bot_line = f"\n\n🤖 Отправь своё фото + этот промт в {bot_at} — получи AI-портрет" if bot_at else ""
    header = f"{title}\n\nСкопируй промт одним нажатием 👇\n\n<code>"
    footer = f"</code>{bot_line}"
    max_prompt_len = 1024 - len(header) - len(footer) - 1
    if max_prompt_len < 50:
        max_prompt_len = 50

    if len(safe_prompt) <= max_prompt_len:
        # Fits perfectly — no truncation needed
        trimmed_prompt = safe_prompt
    else:
        # Cut at last space before limit (word boundary)
        cut = safe_prompt[:max_prompt_len].rfind(" ")
        if cut < max_prompt_len // 2:
            cut = max_prompt_len
        trimmed_prompt = safe_prompt[:cut].rstrip()
        # Avoid cutting inside an HTML entity (&amp; &lt; &gt;)
        amp_pos = trimmed_prompt.rfind("&")
        if amp_pos >= 0 and ";" not in trimmed_prompt[amp_pos:]:
            trimmed_prompt = trimmed_prompt[:amp_pos].rstrip()

    return f"{header}{trimmed_prompt}{footer}"


def build_vk_post_text(
    topic: str,
    caption_intro: str,
    prompt: str,
    vk_community: str = "picgenai",
) -> str:
    """Assemble VK wall post text (plain text, no HTML).

    Structure:
      {title}

      {prompt}

      🤖 Отправь своё фото + этот промт в сообщения @picgenai — получи AI-портрет
    """
    title = (caption_intro.strip() or topic.strip())[:80]
    community = vk_community.lstrip("@") or "picgenai"
    footer_line = f"\n\n🤖 Отправь своё фото + этот промт в сообщения сообщества @{community} — получи AI-портрет"
    return f"{title}\n\n{prompt}{footer_line}"
