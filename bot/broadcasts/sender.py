"""
bot.broadcasts.sender
~~~~~~~~~~~~~~~~~~~~~
Audience materialization + platform-specific send routines.

Public:
  build_audience(broadcast)              -> list[(user_id, platform)]
  send_one(broadcast, recipient)         -> ("sent"|"blocked"|"failed", err)
  render_text(broadcast, user_id)        -> str  (personalization)
  build_click_url(base, bid, uid, plat, idx)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import urllib.parse
from typing import Any

import aiohttp

import bot.db as _db
from bot.user_settings import user_settings, set_blocked, get_user_settings

logger = logging.getLogger(__name__)

VK_API_VERSION = "5.131"
_VK_TOKEN = os.getenv("VK_BOT_TOKEN", "")


# ── Audience materialization ─────────────────────────────────────────────────

def build_audience(b: dict) -> list[tuple[int, str]]:
    """Return [(user_id, platform), ...] matching this broadcast's filters.

    Filters live in JSON `target_filter`:
      audience      : 'all'|'paid'|'unpaid'|'active'|'inactive'
      credits_min/max, generations_min/max
      active_days   : int (used with audience='active'/'inactive')
      exclude_blocked : bool (default true)
      include_user_ids : list[int] (force include)
      exclude_user_ids : list[int]
    Plus broadcast.target_platform: 'all'|'tg'|'vk'.
    """
    try:
        f = json.loads(b.get("target_filter") or "{}") or {}
    except Exception:
        f = {}

    target_platform = (b.get("target_platform") or "all").lower()
    audience = (f.get("audience") or "all").lower()
    cmin = _opt_int(f.get("credits_min"))
    cmax = _opt_int(f.get("credits_max"))
    gmin = _opt_int(f.get("generations_min"))
    gmax = _opt_int(f.get("generations_max"))
    active_days = _opt_int(f.get("active_days")) or 7
    exclude_blocked = bool(f.get("exclude_blocked", True))
    include_ids = {int(x) for x in (f.get("include_user_ids") or []) if str(x).strip().lstrip("-").isdigit()}
    exclude_ids = {int(x) for x in (f.get("exclude_user_ids") or []) if str(x).strip().lstrip("-").isdigit()}

    paid_set: set[int] = _db.broadcast_user_paid_set() if audience in ("paid", "unpaid") else set()
    active_set: set[int] = _db.broadcast_user_active_set(active_days) if audience in ("active", "inactive") else set()

    out: list[tuple[int, str]] = []
    seen: set[tuple[int, str]] = set()

    for uid, s in user_settings.items():
        platform = (s.get("platform") or "tg").lower()
        if platform not in ("tg", "vk"):
            platform = "tg"
        if target_platform != "all" and platform != target_platform:
            continue
        if uid in exclude_ids:
            continue
        if exclude_blocked and bool(s.get("blocked")):
            continue
        credits = int(s.get("credits", 0) or 0)
        gens = int(s.get("generations_count", 0) or 0)
        if cmin is not None and credits < cmin:
            continue
        if cmax is not None and credits > cmax:
            continue
        if gmin is not None and gens < gmin:
            continue
        if gmax is not None and gens > gmax:
            continue
        if audience == "paid" and uid not in paid_set:
            continue
        if audience == "unpaid" and uid in paid_set:
            continue
        if audience == "active" and uid not in active_set:
            continue
        if audience == "inactive" and uid in active_set:
            continue
        key = (uid, platform)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)

    # Force-include IDs (override filters)
    for uid in include_ids:
        s = user_settings.get(uid) or {}
        platform = (s.get("platform") or "tg").lower() if s else "tg"
        if target_platform != "all":
            platform = target_platform
        if platform not in ("tg", "vk"):
            platform = "tg"
        key = (uid, platform)
        if key not in seen:
            seen.add(key)
            out.append(key)

    # A/B split: if this broadcast is the A/B parent, only half goes here;
    # the variant copy gets the rest. Handled at scheduler level via ab_split_pct.
    return out


def _opt_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except Exception:
        return None


# ── Click tracking URL builder ───────────────────────────────────────────────

def public_base_url() -> str:
    """Best-effort public base URL for click redirects."""
    base = os.getenv("PUBLIC_BASE_URL") or os.getenv("BASE_URL") \
        or os.getenv("REPLIT_DEV_DOMAIN") or ""
    base = base.strip().rstrip("/")
    if base and not base.startswith("http"):
        base = f"https://{base}"
    return base


def build_click_url(bid: int, uid: int, platform: str, idx: int, target: str) -> str:
    """Wrap a button URL in our /r/ redirect to count clicks."""
    base = public_base_url()
    if not base:
        return target
    q = urllib.parse.urlencode({"u": target})
    return f"{base}/r/{bid}/{uid}/{platform}/{idx}?{q}"


# ── Personalization ──────────────────────────────────────────────────────────

def render_text(b: dict, uid: int) -> str:
    text = b.get("text") or ""
    if not b.get("personalize"):
        return text
    s = get_user_settings(uid) or {}
    name = (s.get("first_name") or "").strip() or "друг"
    credits = s.get("credits", 0)
    gens = s.get("generations_count", 0)
    repl = {
        "{name}": name,
        "{first_name}": name,
        "{credits}": str(credits),
        "{user_id}": str(uid),
        "{generations}": str(gens),
    }
    for k, v in repl.items():
        text = text.replace(k, v)
    return text


# ── Send: Telegram ───────────────────────────────────────────────────────────

async def send_tg(b: dict, uid: int) -> tuple[str, str]:
    """Send to one TG user. Returns (status, error_text)."""
    from bot.notify import get_tg_bot
    bot = get_tg_bot()
    if bot is None:
        return ("failed", "TG bot not running")

    text = render_text(b, uid)
    parse_mode = b.get("parse_mode") or "HTML"
    if parse_mode.lower() == "none":
        parse_mode = None
    silent = bool(b.get("silent"))
    protect = bool(b.get("protect_content"))
    disable_preview = bool(b.get("disable_preview"))
    media_type = (b.get("media_type") or "none").lower()
    media_path = b.get("media_path") or ""
    media_tg_file_id = b.get("media_tg_file_id") or ""
    media_url = b.get("media_url") or ""
    pin = bool(b.get("pin"))

    reply_markup = _tg_keyboard(b, uid, "tg")

    try:
        from aiogram.types import FSInputFile, LinkPreviewOptions
        from aiogram.exceptions import (
            TelegramRetryAfter, TelegramForbiddenError,
            TelegramBadRequest, TelegramAPIError,
        )
    except Exception as exc:
        return ("failed", f"aiogram import: {exc}")

    async def _send():
        kw = dict(chat_id=uid, parse_mode=parse_mode,
                  disable_notification=silent, protect_content=protect)
        if media_type == "none" or (not media_tg_file_id and not media_path and not media_url):
            return await bot.send_message(
                text=text,
                link_preview_options=LinkPreviewOptions(is_disabled=True)
                    if disable_preview else None,
                reply_markup=reply_markup, **kw,
            )
        media_arg = media_tg_file_id or media_url or FSInputFile(media_path)
        method = {
            "photo": bot.send_photo,
            "video": bot.send_video,
            "document": bot.send_document,
            "animation": bot.send_animation,
            "audio": bot.send_audio,
        }.get(media_type, bot.send_photo)
        kw_media = dict(kw, caption=text or None, reply_markup=reply_markup)
        if media_type == "photo":
            return await method(photo=media_arg, **kw_media)
        if media_type == "video":
            return await method(video=media_arg, **kw_media)
        if media_type == "document":
            return await method(document=media_arg, **kw_media)
        if media_type == "animation":
            return await method(animation=media_arg, **kw_media)
        if media_type == "audio":
            return await method(audio=media_arg, **kw_media)
        return await bot.send_message(text=text, reply_markup=reply_markup, **kw)

    for attempt in range(3):
        try:
            sent_msg = await _send()
            # Cache TG file_id from first successful upload to skip re-upload
            if (media_type != "none" and not media_tg_file_id
                    and media_path and sent_msg is not None):
                try:
                    fid = _extract_tg_file_id(sent_msg, media_type)
                    if fid:
                        b["media_tg_file_id"] = fid
                        _db.broadcast_update(int(b["id"]), {"media_tg_file_id": fid})
                except Exception:
                    pass
            if pin:
                try:
                    await bot.pin_chat_message(
                        chat_id=uid,
                        message_id=sent_msg.message_id,
                        disable_notification=True,
                    )
                except Exception:
                    pass
            return ("sent", "")
        except TelegramRetryAfter as exc:  # type: ignore
            wait = int(getattr(exc, "retry_after", 1) or 1) + 1
            logger.warning("TG flood: waiting %ds (uid=%s)", wait, uid)
            await asyncio.sleep(min(wait, 60))
            continue
        except TelegramForbiddenError as exc:  # type: ignore
            try:
                set_blocked(uid, True)
            except Exception:
                pass
            return ("blocked", str(exc))
        except TelegramBadRequest as exc:  # type: ignore
            msg = str(exc).lower()
            if "chat not found" in msg or "user is deactivated" in msg \
                    or "peer_id_invalid" in msg:
                return ("blocked", str(exc))
            return ("failed", str(exc))
        except Exception as exc:
            return ("failed", str(exc)[:300])

    return ("failed", "TG retries exhausted")


def _extract_tg_file_id(msg, media_type: str) -> str:
    if media_type == "photo" and getattr(msg, "photo", None):
        return msg.photo[-1].file_id
    obj = getattr(msg, media_type, None)
    if obj is not None:
        return getattr(obj, "file_id", "") or ""
    return ""


def _tg_keyboard(b: dict, uid: int, platform: str):
    try:
        buttons = json.loads(b.get("buttons_json") or "[]") or []
    except Exception:
        buttons = []
    if not buttons:
        return None
    try:
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    except Exception:
        return None
    bid = int(b.get("id") or 0)
    rows = []
    for idx, btn in enumerate(buttons):
        text = (btn.get("text") or "").strip()
        url = (btn.get("url") or "").strip()
        if not text or not url:
            continue
        rows.append([InlineKeyboardButton(
            text=text, url=build_click_url(bid, uid, platform, idx, url),
        )])
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Send: VK ─────────────────────────────────────────────────────────────────

async def send_vk(b: dict, uid: int) -> tuple[str, str]:
    """Send to one VK user. Returns (status, error_text)."""
    token = _VK_TOKEN or os.getenv("VK_BOT_TOKEN", "")
    if not token:
        return ("failed", "VK_BOT_TOKEN not set")

    text = render_text(b, uid)
    media_type = (b.get("media_type") or "none").lower()
    attachment = b.get("media_vk_attach") or ""
    media_url = b.get("media_url") or ""

    # If we have no pre-uploaded VK attachment but there's a URL, append it
    # so previews unfurl naturally; for photo with our file path we skip media.
    if media_type != "none" and not attachment and media_url:
        if media_type == "photo":
            text = f"{text}\n\n{media_url}".strip()
        else:
            text = f"{text}\n\n{media_url}".strip()

    keyboard = _vk_keyboard(b, uid)

    payload = {
        "user_id": uid,
        "message": text,
        "random_id": random.randint(0, 2**31),
        "access_token": token,
        "v": VK_API_VERSION,
        "dont_parse_links": 0,
    }
    if attachment:
        payload["attachment"] = attachment
    if keyboard:
        payload["keyboard"] = keyboard

    for attempt in range(3):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://api.vk.com/method/messages.send",
                    data=payload,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    res = await resp.json(content_type=None)
        except Exception as exc:
            if attempt < 2:
                await asyncio.sleep(1.5)
                continue
            return ("failed", f"http: {exc}")

        if "error" in res:
            err = res["error"]
            code = int(err.get("error_code", 0) or 0)
            msg = err.get("error_msg", "")
            # 901 — bot banned/blocked by user; 7 — perm denied; 902 — privacy
            # 932 — user not member; 6 — too many per second (flood)
            if code in (901, 7, 902, 932):
                try:
                    set_blocked(uid, True)
                except Exception:
                    pass
                return ("blocked", msg)
            if code == 6:
                await asyncio.sleep(1.0)
                continue
            return ("failed", f"vk[{code}]: {msg}")
        return ("sent", "")
    return ("failed", "VK retries exhausted")


def _vk_keyboard(b: dict, uid: int) -> str:
    try:
        buttons = json.loads(b.get("buttons_json") or "[]") or []
    except Exception:
        buttons = []
    if not buttons:
        return ""
    bid = int(b.get("id") or 0)
    rows = []
    for idx, btn in enumerate(buttons):
        text = (btn.get("text") or "").strip()
        url = (btn.get("url") or "").strip()
        if not text or not url:
            continue
        rows.append([{
            "action": {
                "type": "open_link",
                "link": build_click_url(bid, uid, "vk", idx, url),
                "label": text[:40],
            },
        }])
    if not rows:
        return ""
    return json.dumps(
        {"inline": True, "buttons": rows},
        ensure_ascii=False,
    )


# ── Dispatch ─────────────────────────────────────────────────────────────────

async def send_one(b: dict, recipient: dict) -> tuple[str, str]:
    plat = (recipient.get("platform") or "tg").lower()
    uid = int(recipient["user_id"])
    if plat == "vk":
        return await send_vk(b, uid)
    return await send_tg(b, uid)
