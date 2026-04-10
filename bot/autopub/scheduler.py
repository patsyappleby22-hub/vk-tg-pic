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
from typing import TYPE_CHECKING

import bot.db as _db

if TYPE_CHECKING:
    from bot.services.vertex_ai_service import VertexAIService

logger = logging.getLogger(__name__)

# How often the scheduler loop ticks
_TICK_SECONDS = 60

_MSK = datetime.timezone(datetime.timedelta(hours=3))


def _now_msk() -> datetime.datetime:
    return datetime.datetime.now(_MSK)


def _should_generate(settings: dict, posts_today: int) -> bool:
    """True if we should generate & queue a new post right now."""
    if not settings.get("enabled"):
        return False
    per_day = max(1, settings.get("posts_per_day", 3))
    if posts_today >= per_day:
        return False
    return True


def _should_publish_now(settings: dict, posts_today: int) -> bool:
    """True if it's time to publish the next queued post."""
    if not settings.get("enabled"):
        return False
    per_day = max(1, settings.get("posts_per_day", 3))
    now = _now_msk()
    # Distribute posts evenly from 09:00 to 21:00 MSK
    start_hour = 9
    end_hour = 21
    span_minutes = (end_hour - start_hour) * 60
    interval = span_minutes // per_day
    minutes_since_start = (now.hour - start_hour) * 60 + now.minute
    if minutes_since_start < 0 or now.hour >= end_hour:
        return False
    slot = minutes_since_start // interval
    return slot >= posts_today


async def _run_generate(vertex_service: "VertexAIService", settings: dict) -> None:
    from bot.autopub.generator import (
        generate_post_idea,
        generate_image_for_post,
        upload_draft_to_telegram,
        build_post_text,
    )

    logger.info("autopub scheduler: generating new post idea...")
    idea = await generate_post_idea(
        vertex_service,
        topic_hints=settings.get("topic_hints", ""),
        image_style=settings.get("image_style", ""),
    )
    if not idea:
        logger.warning("autopub scheduler: idea generation returned None")
        return

    topic = idea["topic"]
    prompt = idea["prompt"]
    caption_intro = idea["caption_intro"]

    caption = build_post_text(
        topic=topic,
        caption_intro=caption_intro,
        prompt=prompt,
        post_template=settings.get("post_template", ""),
        post_cta=settings.get("post_cta", ""),
        bot_username=settings.get("bot_username", ""),
    )

    logger.info("autopub scheduler: generating image for topic: %s", topic)
    image_bytes = await generate_image_for_post(vertex_service, prompt)
    if not image_bytes:
        logger.warning("autopub scheduler: image generation failed")
        return

    logger.info("autopub scheduler: uploading draft to Telegram...")
    tg_result = await upload_draft_to_telegram(image_bytes, caption)
    if not tg_result:
        logger.warning("autopub scheduler: TG upload failed")
        return

    file_id, file_unique = tg_result
    auto_approve = settings.get("auto_approve", False)
    status = "approved" if auto_approve else "draft"

    post_id = _db.autopub_create_post(
        topic=topic,
        caption=caption,
        prompt=prompt,
        tg_file_id=file_id,
        tg_file_unique=file_unique,
        status=status,
    )
    logger.info("autopub scheduler: created post id=%s status=%s topic=%s", post_id, status, topic)


async def _run_publish(settings: dict) -> None:
    from bot.autopub.publisher import publish_to_telegram, publish_to_vk

    approved = _db.autopub_get_posts(status="approved", limit=1)
    if not approved:
        logger.debug("autopub scheduler: no approved posts to publish")
        return

    post = approved[0]
    post_id = post["id"]
    tg_channel = settings.get("tg_channel_id", "").strip()
    vk_group = settings.get("vk_group_id", "").strip()

    if not tg_channel and not vk_group:
        logger.warning("autopub scheduler: no channel/group configured")
        return

    logger.info("autopub scheduler: publishing post id=%s topic=%s", post_id, post["topic"])
    _db.autopub_update_post(post_id, status="publishing")

    errors = []
    tg_msg_id = None
    vk_post_id = None

    if tg_channel:
        tg_msg_id = await publish_to_telegram(tg_channel, post["tg_file_id"], post["caption"])
        if tg_msg_id:
            logger.info("autopub: published to TG channel, msg_id=%s", tg_msg_id)
        else:
            errors.append("TG publish failed")

    if vk_group:
        vk_post_id = await publish_to_vk(vk_group, post["tg_file_id"], post["caption"])
        if vk_post_id:
            logger.info("autopub: published to VK group, post_id=%s", vk_post_id)
        else:
            errors.append("VK publish failed")

    now_iso = datetime.datetime.now(_MSK).isoformat()
    if tg_msg_id or vk_post_id:
        _db.autopub_update_post(
            post_id,
            status="published",
            tg_msg_id=tg_msg_id,
            vk_post_id=vk_post_id,
            published_at=now_iso,
            error_text="; ".join(errors),
        )
    else:
        _db.autopub_update_post(
            post_id,
            status="error",
            error_text="; ".join(errors),
        )
        logger.error("autopub scheduler: all publish targets failed for post %s", post_id)


async def autopub_loop(vertex_service: "VertexAIService") -> None:
    """Main scheduler loop — runs forever, ticks every minute."""
    logger.info("autopub scheduler: started")
    while True:
        try:
            settings = _db.autopub_get_settings()
            if settings.get("enabled"):
                posts_today = _db.autopub_count_published_today()

                if _should_generate(settings, posts_today):
                    # Check if we have enough approved/draft posts already
                    pending = _db.autopub_get_posts(status="draft", limit=5)
                    pending += _db.autopub_get_posts(status="approved", limit=5)
                    per_day = settings.get("posts_per_day", 3)
                    if len(pending) < per_day:
                        await _run_generate(vertex_service, settings)

                if _should_publish_now(settings, posts_today):
                    await _run_publish(settings)

        except Exception as exc:
            logger.exception("autopub scheduler: unexpected error: %s", exc)

        await asyncio.sleep(_TICK_SECONDS)
