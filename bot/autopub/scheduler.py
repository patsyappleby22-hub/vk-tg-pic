"""
bot/autopub/scheduler.py
~~~~~~~~~~~~~~~~~~~~~~~~
Background asyncio task that:
- Checks every minute whether it's time to auto-generate a post
- Publishes approved posts at the correct times during the day
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import random
import time
from typing import TYPE_CHECKING

import bot.db as _db

if TYPE_CHECKING:
    from bot.services.vertex_ai_service import VertexAIService

logger = logging.getLogger(__name__)

_TICK_SECONDS = 60
_MSK = datetime.timezone(datetime.timedelta(hours=3))


def _now_msk() -> datetime.datetime:
    return datetime.datetime.now(_MSK)


def _set_progress(step: int, label: str, pct: int, msg: str = "", *,
                  done: bool = False, error: str = "", last_post_id: int | None = None,
                  trends: "list | None" = None) -> None:
    """Push a progress update to the web panel (SSE stream)."""
    try:
        from bot.web_admin import update_gen_progress
        update_gen_progress(step, label, pct, msg=msg, done=done, error=error,
                            last_post_id=last_post_id, trends=trends)
    except Exception:
        pass


def _on_thought_cb(delta: str) -> None:
    """Callback for Gemini thinking tokens — forwards to SSE buffer."""
    try:
        from bot.web_admin import update_gen_thinking
        update_gen_thinking(delta)
    except Exception:
        pass


def _should_generate(settings: dict, posts_today: int) -> bool:
    if not settings.get("enabled"):
        return False
    per_day = max(1, settings.get("posts_per_day", 3))
    if posts_today >= per_day:
        return False
    return True


def _should_publish_now(settings: dict, posts_today: int) -> bool:
    if not settings.get("enabled"):
        return False
    per_day = max(1, settings.get("posts_per_day", 3))
    now = _now_msk()
    start_hour = 9
    end_hour = 21
    span_minutes = (end_hour - start_hour) * 60
    interval = span_minutes // per_day
    minutes_since_start = (now.hour - start_hour) * 60 + now.minute
    if minutes_since_start < 0 or now.hour >= end_hour:
        return False
    slot = minutes_since_start // interval
    return slot >= posts_today


_TREND_PICK_TIMEOUT = 120  # seconds admin has to pick a trend manually


async def _run_generate(
    vertex_service: "VertexAIService",
    settings: dict,
    admin_feedback: str = "",
    chosen_trend: "dict | None" = None,
    manual: bool = False,
    user_idea: str = "",
) -> None:
    from bot.autopub.generator import (
        search_current_trends,
        search_idea_context,
        generate_post_idea,
        generate_image_for_post,
        generate_multiple_images,
        upload_draft_to_telegram,
        upload_extra_images_to_telegram,
        build_post_text,
        build_vk_post_text,
    )

    t0 = time.monotonic()
    logger.info("━━ [autopub] ГЕНЕРАЦИЯ ПОСТА ━━━━━━━━━━━━━━━━━━━━")
    if admin_feedback:
        logger.info("[autopub] режим: ПЕРЕГЕНЕРАЦИЯ с фидбэком: %r", admin_feedback[:80])

    trend_context = ""
    source_trend = ""
    _idea_context_text = ""

    if user_idea:
        # Admin entered their own idea — search web for context, skip trend picker
        _set_progress(1, "🔍 Изучаю вашу идею...", 5,
                      msg=f"Идея: «{user_idea[:70]}» — ищу контекст в интернете")
        logger.info("[autopub] шаг 1/5 — пользовательская идея: %r, ищу контекст...", user_idea[:80])
        _idea_context_text = await search_idea_context(vertex_service, user_idea)
        source_trend = user_idea
        trend_context = f"{user_idea}: {_idea_context_text}"
        _set_progress(1, "✅ Контекст найден", 18,
                      msg=f"Идея изучена, перехожу к генерации поста")
        logger.info("[autopub] шаг 1/5 OK — контекст по идее получен (%d символов)", len(_idea_context_text))
    elif chosen_trend:
        # Pre-selected trend provided directly
        source_trend = chosen_trend.get("trend", "")
        trend_context = f"{chosen_trend.get('trend', '')}: {chosen_trend.get('context', '')}"
        _set_progress(1, "✅ Тренд задан", 18, msg=f"Тренд: «{source_trend[:70]}»")
        logger.info("[autopub] шаг 1/5 SKIP — тренд задан: %r", source_trend)
    else:
        # Search trends
        _set_progress(1, "🔍 Ищу актуальные тренды...", 5,
                      msg="Запрос к Google Search + Gemini" +
                      (f" | фидбэк: «{admin_feedback[:60]}»" if admin_feedback else ""))
        logger.info("[autopub] шаг 1/5 — ищу актуальные тренды в интернете...")
        recent_topics = _db.autopub_get_recent_topics(limit=30)
        trends = await search_current_trends(vertex_service, used_topics=recent_topics)

        if trends and manual:
            # Manual mode: ask admin to pick a trend via SSE
            logger.info("[autopub] шаг 1/5 — ручной режим, жду выбора тренда (%d найдено)...", len(trends))
            _set_progress(1, "🔍 Выберите тренд", 15,
                          msg=f"Найдено {len(trends)} актуальных трендов. Выберите ниже.",
                          trends=trends)
            try:
                from bot.web_admin import wait_for_trend_selection
                picked = await asyncio.wait_for(
                    wait_for_trend_selection(), timeout=_TREND_PICK_TIMEOUT
                )
                if picked:
                    source_trend = picked.get("trend", "")
                    trend_context = f"{picked.get('trend', '')}: {picked.get('context', '')}"
                    logger.info("[autopub] шаг 1/5 OK — выбран тренд вручную: %r", source_trend)
                else:
                    chosen = random.choice(trends)
                    source_trend = chosen.get("trend", "")
                    trend_context = f"{chosen.get('trend', '')}: {chosen.get('context', '')}"
                    logger.info("[autopub] шаг 1/5 — выбор не произведён, случайный: %r", source_trend)
            except asyncio.TimeoutError:
                chosen = random.choice(trends)
                source_trend = chosen.get("trend", "")
                trend_context = f"{chosen.get('trend', '')}: {chosen.get('context', '')}"
                logger.warning("[autopub] шаг 1/5 — таймаут выбора тренда, случайный: %r", source_trend)
            _set_progress(1, "✅ Тренд выбран", 18, msg=f"Тренд: «{source_trend[:70]}»")
        elif trends:
            # Auto mode: pick random
            chosen = random.choice(trends)
            source_trend = chosen.get("trend", "")
            trend_context = f"{chosen.get('trend', '')}: {chosen.get('context', '')}"
            _set_progress(1, "🔍 Тренды найдены", 18, msg=f"Выбран тренд: «{source_trend[:70]}»")
            logger.info("[autopub] шаг 1/5 OK — выбран тренд: %r", source_trend)
        else:
            _set_progress(1, "🔍 Тренды не найдены", 18, msg="Генерирую без конкретного тренда")
            logger.warning("[autopub] шаг 1/5 — тренды не найдены, генерирую без тренда")

    # Step 2: Idea
    step2_label = "💡 Придумываю креативный пост..." if user_idea else "💡 Придумываю идею поста..."
    _set_progress(2, step2_label, 22,
                  msg="Gemini Pro генерирует тему, промпт, заголовок" +
                  (" (полная творческая свобода)" if user_idea else ""))
    logger.info("[autopub] шаг 2/5 — генерирую идею поста (Pro model)...")
    idea = await generate_post_idea(
        vertex_service,
        topic_hints=settings.get("topic_hints", ""),
        image_style=settings.get("image_style", ""),
        trend_context=trend_context,
        admin_feedback=admin_feedback,
        on_thought=_on_thought_cb,
        user_idea=user_idea,
        idea_context=_idea_context_text,
    )
    if not idea:
        err = "Gemini не вернул идею — проверьте логи"
        _set_progress(2, "❌ Ошибка генерации идеи", 22, error=err)
        logger.error("[autopub] шаг 2/5 FAILED — Gemini не вернул идею (см. ошибки выше)")
        return
    _set_progress(2, "💡 Идея готова", 35,
                  msg=f"Тема: «{idea['topic'][:70]}»")
    logger.info("[autopub] шаг 2/5 OK — topic=%r  caption_intro=%r",
                idea["topic"], idea["caption_intro"])
    logger.debug("[autopub] промпт для изображения: %s", idea["prompt"][:200])

    topic = idea["topic"]
    prompt = idea["prompt"]
    image_prompt = idea.get("image_prompt", prompt)
    caption_intro = idea.get("caption_intro", topic)
    gemini_caption = idea.get("caption", "")

    caption = build_post_text(
        topic=topic,
        caption_intro=caption_intro,
        prompt=prompt,
        post_template=settings.get("post_template", ""),
        post_cta=settings.get("post_cta", ""),
        bot_username=settings.get("bot_username", ""),
        gemini_caption=gemini_caption,
    )
    logger.info("[autopub] текст поста сформирован, длина=%d символов", len(caption))
    if image_prompt != prompt:
        logger.info("[autopub] image_prompt отличается от user prompt (иллюстрация vs пользовательский)")

    # Step 3: Image generation — 3 variations
    _set_progress(3, "🎨 Генерирую 3 иллюстрации 4:5...", 38,
                  msg=f"Промпт: «{image_prompt[:80]}...»")
    logger.info("[autopub] шаг 3/5 — генерирую 3 иллюстрации 4:5...")
    img_t = time.monotonic()
    all_images = await generate_multiple_images(vertex_service, image_prompt, count=3)
    if not all_images:
        err = "Ни одно изображение не сгенерировано — проверьте логи"
        _set_progress(3, "❌ Ошибка генерации изображений", 38, error=err)
        logger.error("[autopub] шаг 3/5 FAILED — ни одно изображение не сгенерировано")
        return
    image_bytes = all_images[0]
    extra_images = all_images[1:]
    total_kb = sum(len(img) / 1024 for img in all_images)
    _set_progress(3, f"🎨 {len(all_images)} изображений готово", 65,
                  msg=f"Всего: {total_kb:.0f} KB  |  время: {time.monotonic()-img_t:.1f}s")
    logger.info("[autopub] шаг 3/5 OK — %d изображений, %.1f KB (%.1fs)",
                len(all_images), total_kb, time.monotonic() - img_t)

    # Step 4: Upload all images to TG log channel
    _set_progress(4, "📤 Загружаю в Telegram...", 70,
                  msg=f"Отправляю {len(all_images)} фото в лог-канал")
    logger.info("[autopub] шаг 4/5 — загружаю %d фото в Telegram (лог-канал)...", len(all_images))
    tg_result = await upload_draft_to_telegram(image_bytes, caption)
    if not tg_result:
        err = "Не удалось загрузить в Telegram — проверьте токен бота"
        _set_progress(4, "❌ Ошибка загрузки в Telegram", 70, error=err)
        logger.error("[autopub] шаг 4/5 FAILED — не удалось загрузить в Telegram")
        return
    file_id, file_unique = tg_result

    extra_file_id_list: list[str] = []
    if extra_images:
        logger.info("[autopub] шаг 4/5 — загружаю %d доп. фото...", len(extra_images))
        extra_file_id_list = await upload_extra_images_to_telegram(extra_images)
        logger.info("[autopub] шаг 4/5 — загружено %d доп. фото", len(extra_file_id_list))

    extra_file_ids_str = ",".join(extra_file_id_list) if extra_file_id_list else ""
    total_photos = 1 + len(extra_file_id_list)
    _set_progress(4, f"📤 Telegram OK ({total_photos} фото)", 85,
                  msg=f"Загружено {total_photos} фото")
    logger.info("[autopub] шаг 4/5 OK — %d фото загружено (main + %d extra)",
                total_photos, len(extra_file_id_list))

    # Step 5: Save to DB
    _set_progress(5, "💾 Сохраняю пост в очередь...", 90,
                  msg="Записываю в базу данных")
    auto_approve = settings.get("auto_approve", False)
    status = "approved" if auto_approve else "draft"
    post_id = _db.autopub_create_post(
        topic=topic,
        caption=caption,
        prompt=prompt,
        tg_file_id=file_id,
        tg_file_unique=file_unique,
        status=status,
        source_trend=source_trend,
        admin_comment=admin_feedback,
        extra_file_ids=extra_file_ids_str,
    )
    elapsed = time.monotonic() - t0
    _set_progress(5, "✅ Пост готов!", 100,
                  msg=f"id={post_id}  статус={status}  тренд=«{source_trend or '—'}»  время={elapsed:.1f}s",
                  done=True, last_post_id=post_id)
    logger.info("[autopub] шаг 5/5 OK — пост сохранён id=%s status=%s trend=%r (всего %.1fs)",
                post_id, status, source_trend or "—", elapsed)
    logger.info("━━ [autopub] ГЕНЕРАЦИЯ ЗАВЕРШЕНА ━━━━━━━━━━━━━━━━")


async def _run_publish(settings: dict) -> None:
    from bot.autopub.publisher import publish_to_telegram, publish_to_vk
    from bot.autopub.generator import build_vk_post_text

    logger.info("━━ [autopub] ПУБЛИКАЦИЯ ━━━━━━━━━━━━━━━━━━━━━━━━━")
    approved = _db.autopub_get_posts(status="approved", limit=1)
    if not approved:
        logger.info("[autopub] нет одобренных постов для публикации")
        return

    post = approved[0]
    post_id = post["id"]
    tg_channel = settings.get("tg_channel_id", "").strip()
    vk_group = settings.get("vk_group_id", "").strip()

    logger.info("[autopub] публикую пост id=%s topic=%r", post_id, post["topic"])
    logger.info("[autopub] каналы: TG=%r  VK=%r", tg_channel or "(не задан)", vk_group or "(не задан)")

    # Refuse to publish without a photo
    if not post.get("tg_file_id"):
        logger.error("[autopub] пост id=%s не имеет фото — публикация без фото запрещена", post_id)
        _db.autopub_update_post(post_id, status="error",
                                error_text="Нет фото: публикация без изображения запрещена")
        return

    if not tg_channel and not vk_group:
        logger.error("[autopub] нет ни одного канала/группы — настройте TG канал или VK группу")
        return

    _db.autopub_update_post(post_id, status="publishing")

    errors = []
    tg_msg_id = None
    vk_post_id = None

    extra_ids = [fid for fid in post.get("extra_file_ids", "").split(",") if fid.strip()]
    total_photos = 1 + len(extra_ids)
    logger.info("[autopub] пост содержит %d фото", total_photos)

    if tg_channel:
        logger.info("[autopub] → Telegram: публикую %d фото в канал %s...", total_photos, tg_channel)
        tg_msg_id = await publish_to_telegram(tg_channel, post["tg_file_id"], post["caption"], extra_file_ids=extra_ids or None)
        if tg_msg_id:
            logger.info("[autopub] → Telegram OK: message_id=%s (%d фото)", tg_msg_id, total_photos)
        else:
            logger.error("[autopub] → Telegram FAILED (см. ошибки publisher выше)")
            errors.append("TG publish failed")

    if vk_group:
        logger.info("[autopub] → VK: публикую %d фото в группу %s...", total_photos, vk_group)
        vk_caption = build_vk_post_text(
            topic=post.get("topic", ""),
            caption_intro=post.get("topic", ""),
            prompt=post.get("prompt", ""),
            vk_community="picgenai",
        )
        vk_post_id = await publish_to_vk(vk_group, post["tg_file_id"], vk_caption, extra_file_ids=extra_ids or None)
        if vk_post_id:
            logger.info("[autopub] → VK OK: post_id=%s", vk_post_id)
        else:
            logger.error("[autopub] → VK FAILED (см. ошибки publisher выше)")
            errors.append("VK publish failed")

    now_iso = datetime.datetime.now(_MSK).isoformat()
    if tg_msg_id or vk_post_id:
        _db.autopub_update_post(
            post_id,
            status="published",
            tg_msg_id=tg_msg_id,
            vk_post_id=vk_post_id,
            published_at=now_iso,
            error_text="; ".join(errors) if errors else None,
        )
        logger.info("[autopub] пост id=%s опубликован ✓ (TG=%s VK=%s)",
                    post_id, tg_msg_id or "—", vk_post_id or "—")
    else:
        _db.autopub_update_post(post_id, status="error", error_text="; ".join(errors))
        logger.error("[autopub] пост id=%s — все каналы упали, статус=error", post_id)

    logger.info("━━ [autopub] ПУБЛИКАЦИЯ ЗАВЕРШЕНА ━━━━━━━━━━━━━━━━")


async def autopub_loop(vertex_service: "VertexAIService") -> None:
    """Main scheduler loop — runs forever, ticks every minute."""
    logger.info("autopub scheduler: started (tick=%ds)", _TICK_SECONDS)
    tick = 0
    while True:
        tick += 1
        try:
            settings = _db.autopub_get_settings()
            enabled = settings.get("enabled")

            if tick % 5 == 1:  # log status every 5 minutes
                now = _now_msk()
                logger.info("[autopub] scheduler tick #%d  enabled=%s  time=%s MSK",
                            tick, enabled, now.strftime("%H:%M"))

            if enabled:
                posts_today = _db.autopub_count_published_today()
                per_day = settings.get("posts_per_day", 3)

                pending_draft    = _db.autopub_get_posts(status="draft",    limit=20)
                pending_approved = _db.autopub_get_posts(status="approved", limit=20)
                pending_total = len(pending_draft) + len(pending_approved)

                if tick % 5 == 1:
                    logger.info("[autopub] сегодня опубликовано=%d/%d  в очереди=%d (draft=%d approved=%d)",
                                posts_today, per_day, pending_total,
                                len(pending_draft), len(pending_approved))

                if _should_generate(settings, posts_today):
                    if pending_total < per_day:
                        logger.info("[autopub] нужна генерация (очередь=%d < plan=%d)", pending_total, per_day)
                        await _run_generate(vertex_service, settings)
                    else:
                        logger.debug("[autopub] очередь полная (%d постов), пропускаю генерацию", pending_total)

                if _should_publish_now(settings, posts_today):
                    await _run_publish(settings)

        except Exception as exc:
            logger.exception("[autopub] неожиданная ошибка в планировщике: %s", exc)

        await asyncio.sleep(_TICK_SECONDS)
