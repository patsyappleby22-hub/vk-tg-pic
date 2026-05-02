"""
bot/web_chat.py
~~~~~~~~~~~~~~~
User-facing web chat panel for PicGenAI.

Routes:
  GET  /chat                           → SPA shell (login or chat UI)
  POST /chat/api/login/request         → {platform, identifier} → send 6-digit code
  POST /chat/api/login/verify          → {platform, user_id, code} → set session cookie
  POST /chat/api/logout                → clear session
  GET  /chat/api/me                    → current user info + credits
  GET  /chat/api/chats                 → list user's chats
  POST /chat/api/chats                 → create chat
  PATCH /chat/api/chats/{cid}          → rename / archive
  DELETE /chat/api/chats/{cid}         → delete
  GET  /chat/api/chats/{cid}/messages  → list messages
  POST /chat/api/chats/{cid}/send      → send a message (text + optional files)
  GET  /chat/api/gen/{gen_id}/status   → poll long-running generation
  GET  /chat/api/media/{mid}           → stream media bytes for a message

Auth: 6-digit code via the bot DM (TG / VK). HMAC-signed `sid` cookie for
30 days, persisted in `bot_web_sessions`. Credits are shared with the bot
through `bot.user_settings` (reserve / confirm / release).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

import bot.db as _db
from bot.user_settings import (
    AVAILABLE_MODELS,
    CHAT_MODELS,
    DEFAULT_CHAT_MODEL,
    FREE_CREDITS,
    VIDEO_ASPECT_RATIOS,
    VIDEO_DURATIONS,
    VIDEO_RESOLUTIONS,
    calc_video_credits,
    confirm_credits,
    get_chat_daily_limit,
    get_chat_daily_count,
    get_music_credits_cost,
    get_user_settings,
    get_video_resolutions_for_model,
    has_chat_quota,
    has_credits,
    increment_chat_count,
    is_blocked,
    is_music_model,
    is_video_model,
    music_supports_image,
    release_credits,
    reserve_credits,
    video_supports_4k,
    video_supports_audio,
    video_supports_image,
)

logger = logging.getLogger(__name__)


# ─── Configuration ──────────────────────────────────────────────────────────

_SECRET = os.getenv("WEB_SESSION_SECRET", "").strip()
if not _SECRET:
    _SECRET = hashlib.sha256(
        (os.getenv("ADMIN_PASSWORD", "picgenai_default") + "_web_chat_v1").encode()
    ).hexdigest()

_COOKIE_SID = "pg_chat_sid"
_COOKIE_TOK = "pg_chat_tok"
_COOKIE_TTL = 86400 * 30  # 30 days

_CODE_TTL = 300  # seconds — how long a 6-digit code is valid
_CODE_MAX_ATTEMPTS = 5
_CODE_RATE_LIMIT_PER_USER = 3      # per 10 minutes per user
_CODE_RATE_LIMIT_WINDOW = 10       # minutes
_CODE_GLOBAL_PER_IP = 12           # per hour per IP
_CODE_GLOBAL_WINDOW = 60           # minutes

_MAX_CHATS_PER_USER = 50
_MAX_MESSAGES_PER_CHAT = 200
_HISTORY_TURNS_FOR_MODEL = 30
_MAX_UPLOAD_SIZE = 12 * 1024 * 1024   # 12 MiB per file
_MAX_UPLOAD_FILES = 4
_MAX_PROMPT_CHARS = 8000

_MEDIA_CACHE_DIR = Path(os.getenv("WEB_MEDIA_CACHE_DIR", "/tmp/web_media_cache"))
_MEDIA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_MEDIA_CACHE_BUDGET_BYTES = int(os.getenv("WEB_MEDIA_CACHE_MB", "300")) * 1024 * 1024

_PROMPT_TG = (
    "Здравствуйте! Это код для входа в веб-панель PicGenAI:\n\n"
    "<b>{code}</b>\n\n"
    "Никому не сообщайте этот код. Действителен 5 минут."
)
_PROMPT_VK = (
    "Это код для входа в веб-панель PicGenAI:\n\n"
    "{code}\n\n"
    "Никому не сообщайте этот код. Действителен 5 минут."
)


# ─── Vertex service (set from start_all.py) ─────────────────────────────────

_vertex_service: Any = None


def set_vertex_service(svc: Any) -> None:
    global _vertex_service
    _vertex_service = svc


# ─── Long-running generation registry ───────────────────────────────────────
# gen_id → {"status": "queued|running|done|error", "pct": int,
#           "label": str, "msg_id": int|None, "error": str,
#           "user_id": int, "started_at": float}
_gens: dict[str, dict[str, Any]] = {}
_gens_lock = asyncio.Lock()


def _gen_new(user_id: int) -> str:
    gid = uuid.uuid4().hex[:16]
    _gens[gid] = {
        "status": "queued", "pct": 0, "label": "В очереди",
        "msg_id": None, "error": "", "user_id": user_id,
        "started_at": time.monotonic(),
    }
    return gid


def _gen_update(gid: str, **kw: Any) -> None:
    g = _gens.get(gid)
    if g is None:
        return
    g.update(kw)


def _gens_gc() -> None:
    """Drop generations older than 1 hour to bound memory."""
    cutoff = time.monotonic() - 3600
    for k in list(_gens.keys()):
        if _gens[k].get("started_at", 0) < cutoff:
            _gens.pop(k, None)


# ─── Session cookie helpers ─────────────────────────────────────────────────

def _sign_sid(sid: str) -> str:
    return hmac.new(_SECRET.encode(), sid.encode(), hashlib.sha256).hexdigest()


def _set_session_cookies(resp: web.StreamResponse, sid: str) -> None:
    resp.set_cookie(_COOKIE_SID, sid, max_age=_COOKIE_TTL,
                    httponly=True, samesite="Lax", path="/")
    resp.set_cookie(_COOKIE_TOK, _sign_sid(sid), max_age=_COOKIE_TTL,
                    httponly=True, samesite="Lax", path="/")


def _clear_session_cookies(resp: web.StreamResponse) -> None:
    resp.del_cookie(_COOKIE_SID, path="/")
    resp.del_cookie(_COOKIE_TOK, path="/")


def _get_session(request: web.Request) -> dict | None:
    sid = request.cookies.get(_COOKIE_SID, "")
    tok = request.cookies.get(_COOKIE_TOK, "")
    if not sid or not tok:
        return None
    if not hmac.compare_digest(tok, _sign_sid(sid)):
        return None
    sess = _db.web_session_get(sid)
    if sess is None:
        return None
    if is_blocked(sess["user_id"]):
        return None
    _db.web_session_touch(sid)
    return sess


def _require_session(request: web.Request) -> dict:
    s = _get_session(request)
    if s is None:
        raise web.HTTPUnauthorized(text=json.dumps({"error": "unauthorized"}),
                                   content_type="application/json")
    return s


def _client_ip(request: web.Request) -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    peer = request.transport.get_extra_info("peername") if request.transport else None
    return (peer[0] if peer else "")[:64]


def _client_ua(request: web.Request) -> str:
    return request.headers.get("User-Agent", "")[:200]


# ─── Identifier resolution (TG / VK) ────────────────────────────────────────

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")


def _normalize_identifier(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("https://t.me/"):
        raw = raw[len("https://t.me/"):]
    if raw.startswith("https://vk.com/") or raw.startswith("https://vk.ru/"):
        raw = raw.split("/")[-1]
    if raw.startswith("@"):
        raw = raw[1:]
    return raw.split("?")[0]


async def _resolve_tg_user(identifier: str) -> tuple[int | None, str]:
    """Return (user_id, error) for a TG identifier (numeric or @username).

    Requires the user to have started the bot at least once — otherwise
    we cannot DM them with a code.
    """
    from bot.notify import _tg_bot
    if _tg_bot is None:
        return None, "Бот Telegram временно недоступен"
    ident = _normalize_identifier(identifier)
    if ident.isdigit():
        try:
            uid = int(ident)
        except ValueError:
            return None, "Неверный ID"
        if uid <= 0:
            return None, "Неверный ID"
        try:
            await _tg_bot.get_chat(uid)
            return uid, ""
        except Exception as exc:
            logger.info("TG resolve by id %s failed: %s", uid, exc)
            return None, "Не удалось найти пользователя. Сначала напишите боту /start в Telegram."
    if not _USERNAME_RE.match(ident):
        return None, "Неверный username"
    try:
        chat = await _tg_bot.get_chat("@" + ident)
        return int(chat.id), ""
    except Exception as exc:
        logger.info("TG resolve by username @%s failed: %s", ident, exc)
        return None, "Не удалось найти пользователя. Сначала напишите боту /start в Telegram."


async def _resolve_vk_user(identifier: str) -> tuple[int | None, str]:
    token = os.getenv("VK_BOT_TOKEN", "")
    if not token:
        return None, "VK-бот временно недоступен"
    ident = _normalize_identifier(identifier)
    if ident.isdigit():
        try:
            return int(ident), ""
        except ValueError:
            return None, "Неверный ID"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://api.vk.com/method/users.get",
                params={
                    "user_ids": ident,
                    "access_token": token,
                    "v": "5.199",
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                body = await r.json(content_type=None)
        items = body.get("response") or []
        if not items:
            return None, "Не удалось найти пользователя VK"
        return int(items[0]["id"]), ""
    except Exception as exc:
        logger.info("VK resolve %s failed: %s", ident, exc)
        return None, "Не удалось найти пользователя VK"


# ─── Code delivery ──────────────────────────────────────────────────────────

async def _send_code_tg(user_id: int, code: str) -> tuple[bool, str]:
    from bot.notify import _tg_bot
    if _tg_bot is None:
        return False, "Бот Telegram временно недоступен"
    try:
        await _tg_bot.send_message(
            chat_id=user_id,
            text=_PROMPT_TG.format(code=code),
            parse_mode="HTML",
        )
        return True, ""
    except Exception as exc:
        logger.info("TG send code to %s failed: %s", user_id, exc)
        s = str(exc).lower()
        if "blocked" in s or "forbidden" in s or "chat not found" in s or "deactivated" in s:
            return False, "Не удалось отправить код. Откройте бот и нажмите /start, затем попробуйте снова."
        return False, "Не удалось отправить код. Попробуйте позже."


async def _send_code_vk(user_id: int, code: str) -> tuple[bool, str]:
    token = os.getenv("VK_BOT_TOKEN", "")
    if not token:
        return False, "VK-бот временно недоступен"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "https://api.vk.com/method/messages.send",
                data={
                    "access_token": token,
                    "v": "5.199",
                    "user_id": str(user_id),
                    "message": _PROMPT_VK.format(code=code),
                    "random_id": str(int(time.time() * 1000) % (2**31)),
                    "disable_mentions": "1",
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                body = await r.json(content_type=None)
        if "response" in body:
            return True, ""
        err = body.get("error", {})
        code_n = err.get("error_code")
        msg = err.get("error_msg", "")
        logger.info("VK send code to %s failed: %s %s", user_id, code_n, msg)
        if code_n in (901, 902, 7):
            return False, "Не удалось отправить код. Напишите боту в ВК и разрешите сообщения от сообщества."
        return False, "Не удалось отправить код. Попробуйте позже."
    except Exception as exc:
        logger.info("VK send code to %s exception: %s", user_id, exc)
        return False, "Не удалось отправить код. Попробуйте позже."


def _hash_code(user_id: int, platform: str, code: str) -> str:
    return hashlib.sha256(
        f"{user_id}:{platform}:{code}:{_SECRET}".encode()
    ).hexdigest()


# ─── Aiohttp handlers: auth ─────────────────────────────────────────────────

async def handle_login_request(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Неверный запрос"}, status=400)
    platform = (data.get("platform") or "").strip().lower()
    identifier = (data.get("identifier") or "").strip()
    if platform not in ("tg", "vk"):
        return web.json_response({"error": "Выберите Telegram или ВКонтакте"}, status=400)
    if not identifier or len(identifier) > 80:
        return web.json_response({"error": "Введите ID или username"}, status=400)

    ip = _client_ip(request)
    ua = _client_ua(request)

    if platform == "tg":
        uid, err = await _resolve_tg_user(identifier)
    else:
        uid, err = await _resolve_vk_user(identifier)
    if uid is None:
        _db.web_login_log(None, platform, "resolve_fail", ip, ua, err[:200])
        return web.json_response({"error": err or "Не удалось найти пользователя"}, status=404)

    if is_blocked(uid):
        _db.web_login_log(uid, platform, "blocked", ip, ua, "")
        return web.json_response({"error": "Доступ заблокирован администратором"}, status=403)

    recent = _db.web_code_recent_count(uid, platform, _CODE_RATE_LIMIT_WINDOW)
    if recent >= _CODE_RATE_LIMIT_PER_USER:
        _db.web_login_log(uid, platform, "rate_limit", ip, ua, f"recent={recent}")
        return web.json_response(
            {"error": f"Слишком часто. Подождите {_CODE_RATE_LIMIT_WINDOW} минут перед следующей попыткой."},
            status=429,
        )

    code = f"{secrets.randbelow(1_000_000):06d}"
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=_CODE_TTL)).isoformat()
    code_id = _db.web_code_create(_hash_code(uid, platform, code), uid, platform, expires_at, ip)
    if code_id is None:
        _db.web_login_log(uid, platform, "db_unavailable", ip, ua, "")
        return web.json_response(
            {"error": "Сервис временно недоступен, попробуйте через минуту."},
            status=503,
        )

    if platform == "tg":
        ok, send_err = await _send_code_tg(uid, code)
    else:
        ok, send_err = await _send_code_vk(uid, code)

    if not ok:
        _db.web_login_log(uid, platform, "send_fail", ip, ua, send_err[:200])
        return web.json_response({"error": send_err}, status=502)

    _db.web_login_log(uid, platform, "code_sent", ip, ua, "")
    return web.json_response({"ok": True, "user_id": uid, "ttl": _CODE_TTL})


async def handle_login_verify(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Неверный запрос"}, status=400)
    platform = (data.get("platform") or "").strip().lower()
    code = (data.get("code") or "").strip()
    try:
        user_id = int(data.get("user_id") or 0)
    except Exception:
        user_id = 0
    if platform not in ("tg", "vk") or not code or user_id <= 0:
        return web.json_response({"error": "Неверный запрос"}, status=400)
    if not re.match(r"^\d{6}$", code):
        return web.json_response({"error": "Код состоит из 6 цифр"}, status=400)

    ip = _client_ip(request)
    ua = _client_ua(request)

    active = _db.web_code_get_active(user_id, platform)
    if active is None:
        _db.web_login_log(user_id, platform, "verify_no_code", ip, ua, "")
        return web.json_response({"error": "Код истёк или не найден. Запросите новый."}, status=400)

    if active["attempts"] >= _CODE_MAX_ATTEMPTS:
        _db.web_login_log(user_id, platform, "verify_locked", ip, ua, "")
        return web.json_response({"error": "Слишком много попыток. Запросите новый код."}, status=429)

    if not hmac.compare_digest(active["code_hash"], _hash_code(user_id, platform, code)):
        attempts = _db.web_code_increment_attempt(active["id"])
        left = max(0, _CODE_MAX_ATTEMPTS - attempts)
        _db.web_login_log(user_id, platform, "verify_bad", ip, ua, f"attempts={attempts}")
        return web.json_response(
            {"error": f"Неверный код. Осталось попыток: {left}."},
            status=400,
        )

    _db.web_code_mark_used(active["id"])

    sid = secrets.token_urlsafe(24)
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=_COOKIE_TTL)).isoformat()
    _db.web_session_create(sid, user_id, platform, expires_at, ip, ua)
    _db.web_login_log(user_id, platform, "login_ok", ip, ua, "")

    s = get_user_settings(user_id)
    if not s.get("platform"):
        s["platform"] = platform
        try:
            from bot.user_settings import _save_user
            _save_user(user_id)
        except Exception:
            pass

    resp = web.json_response({"ok": True, "user_id": user_id, "platform": platform})
    _set_session_cookies(resp, sid)
    return resp


async def handle_logout(request: web.Request) -> web.Response:
    sid = request.cookies.get(_COOKIE_SID, "")
    if sid:
        _db.web_session_delete(sid)
    resp = web.json_response({"ok": True})
    _clear_session_cookies(resp)
    return resp


# ─── User info ──────────────────────────────────────────────────────────────

async def handle_me(request: web.Request) -> web.Response:
    s = _require_session(request)
    uid = s["user_id"]
    su = get_user_settings(uid)
    return web.json_response({
        "user_id": uid,
        "platform": s["platform"],
        "first_name": su.get("first_name") or str(uid),
        "credits": int(su.get("credits", FREE_CREDITS)),
        "chat_used_today": get_chat_daily_count(uid),
        "chat_limit_today": get_chat_daily_limit(uid),
    })


# ─── Catalog of generation modes / models ───────────────────────────────────

def _catalog() -> dict[str, Any]:
    image_models = {
        mid: {"label": info["label"], "desc": info.get("desc", "")}
        for mid, info in AVAILABLE_MODELS.items() if info.get("type") == "image"
    }
    video_models = {
        mid: {
            "label": info["label"], "desc": info.get("desc", ""),
            "supports_audio": info.get("supports_audio", False),
            "supports_image": info.get("supports_image", False),
            "supports_video_extension": info.get("supports_video_extension", False),
            "supports_4k": info.get("supports_4k", False),
            "resolutions": list(get_video_resolutions_for_model(mid).keys()),
        }
        for mid, info in AVAILABLE_MODELS.items() if info.get("type") == "video"
    }
    music_models = {
        mid: {
            "label": info["label"], "desc": info.get("desc", ""),
            "credits": info.get("credits", 2),
            "supports_image": info.get("supports_image", True),
        }
        for mid, info in AVAILABLE_MODELS.items() if info.get("type") == "music"
    }
    chat_models = {
        cid: {"label": info["label"], "short": info.get("short", info["label"]),
              "desc": info.get("desc", ""), "backend": info["backend"]}
        for cid, info in CHAT_MODELS.items()
    }
    return {
        "image": {
            "models": image_models,
            "aspects": ["1:1", "16:9", "9:16", "4:3", "3:4"],
        },
        "video": {
            "models": video_models,
            "aspects": list(VIDEO_ASPECT_RATIOS.keys()),
            "durations": list(VIDEO_DURATIONS.keys()),
        },
        "music": {"models": music_models},
        "chat": {"models": chat_models, "default": DEFAULT_CHAT_MODEL},
    }


async def handle_catalog(request: web.Request) -> web.Response:
    _require_session(request)
    return web.json_response(_catalog())


# ─── Chats CRUD ─────────────────────────────────────────────────────────────

async def handle_chats_list(request: web.Request) -> web.Response:
    s = _require_session(request)
    archived = request.rel_url.query.get("archived") == "1"
    chats = _db.web_chat_list(s["user_id"], archived=archived)
    return web.json_response({"chats": chats})


async def handle_chats_create(request: web.Request) -> web.Response:
    s = _require_session(request)
    uid = s["user_id"]
    if _db.web_chat_count(uid, archived=False) >= _MAX_CHATS_PER_USER:
        return web.json_response(
            {"error": f"Достигнут лимит чатов ({_MAX_CHATS_PER_USER}). Удалите или архивируйте старые."},
            status=400,
        )
    try:
        data = await request.json()
    except Exception:
        data = {}
    title = (data.get("title") or "Новый чат").strip()[:120] or "Новый чат"
    cid = _db.web_chat_create(uid, s["platform"], title)
    if cid is None:
        return web.json_response({"error": "Не удалось создать чат"}, status=500)
    return web.json_response({"id": cid, "title": title})


async def handle_chats_patch(request: web.Request) -> web.Response:
    s = _require_session(request)
    try:
        cid = int(request.match_info["cid"])
    except Exception:
        return web.json_response({"error": "Неверный ID"}, status=400)
    chat = _db.web_chat_get(cid, s["user_id"])
    if chat is None:
        return web.json_response({"error": "Чат не найден"}, status=404)
    try:
        data = await request.json()
    except Exception:
        data = {}
    if "title" in data:
        title = (data["title"] or "").strip()[:120]
        if not title:
            return web.json_response({"error": "Название не может быть пустым"}, status=400)
        _db.web_chat_update_title(cid, s["user_id"], title)
    if "archived" in data:
        _db.web_chat_set_archived(cid, s["user_id"], bool(data["archived"]))
    return web.json_response({"ok": True})


async def handle_chats_delete(request: web.Request) -> web.Response:
    s = _require_session(request)
    try:
        cid = int(request.match_info["cid"])
    except Exception:
        return web.json_response({"error": "Неверный ID"}, status=400)
    ok = _db.web_chat_delete(cid, s["user_id"])
    return web.json_response({"ok": ok})


async def handle_messages_list(request: web.Request) -> web.Response:
    s = _require_session(request)
    try:
        cid = int(request.match_info["cid"])
    except Exception:
        return web.json_response({"error": "Неверный ID"}, status=400)
    chat = _db.web_chat_get(cid, s["user_id"])
    if chat is None:
        return web.json_response({"error": "Чат не найден"}, status=404)
    msgs = _db.web_msg_list(cid, limit=_MAX_MESSAGES_PER_CHAT)
    return web.json_response({"chat": chat, "messages": msgs})


# ─── Media proxy with LRU disk cache ────────────────────────────────────────

def _cache_path(file_unique_id: str, kind: str) -> Path:
    ext = {"image": "jpg", "video": "mp4", "audio": "mp3"}.get(kind, "bin")
    safe = re.sub(r"[^A-Za-z0-9_.-]", "", file_unique_id)[:64] or "x"
    return _MEDIA_CACHE_DIR / f"{safe}.{ext}"


async def _cache_evict_if_needed() -> None:
    try:
        files = sorted(
            _MEDIA_CACHE_DIR.iterdir(),
            key=lambda p: p.stat().st_mtime,
        )
        total = sum(p.stat().st_size for p in files if p.is_file())
        while total > _MEDIA_CACHE_BUDGET_BYTES and files:
            victim = files.pop(0)
            try:
                sz = victim.stat().st_size
                victim.unlink()
                total -= sz
            except Exception:
                pass
    except Exception:
        pass


async def _fetch_tg_media(file_id: str, file_unique_id: str, kind: str) -> bytes | None:
    """Download a TG file_id via the aiogram bot. Cache on disk."""
    cp = _cache_path(file_unique_id or file_id[-32:], kind)
    if cp.exists():
        try:
            cp.touch()
            return cp.read_bytes()
        except Exception:
            pass
    from bot.notify import _tg_bot
    if _tg_bot is None:
        return None
    try:
        f = await _tg_bot.get_file(file_id)
        buf = io.BytesIO()
        await _tg_bot.download_file(f.file_path, buf)
        data = buf.getvalue()
        try:
            cp.write_bytes(data)
            await _cache_evict_if_needed()
        except Exception:
            pass
        return data
    except Exception as exc:
        logger.warning("media proxy: failed to fetch file_id=%s…: %s", file_id[:24], exc)
        return None


async def handle_media(request: web.Request) -> web.Response:
    s = _require_session(request)
    try:
        mid = int(request.match_info["mid"])
    except Exception:
        return web.Response(status=400, text="bad id")
    msgs = _db.web_msg_list(0, limit=0)  # noop just to assure import
    # Look up the message and verify chat ownership
    if not _db.is_available():
        return web.Response(status=503, text="db unavailable")
    try:
        conn = _db._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT m.file_id, m.file_unique_id, m.file_kind, c.user_id "
                "FROM bot_web_messages m JOIN bot_web_chats c ON c.id=m.chat_id "
                "WHERE m.id=%s",
                (mid,),
            )
            row = cur.fetchone()
    except Exception:
        return web.Response(status=500, text="db error")
    if not row:
        return web.Response(status=404, text="not found")
    if int(row[3]) != s["user_id"]:
        return web.Response(status=403, text="forbidden")
    file_id, fuid, kind = row[0], row[1], row[2] or ""
    if not file_id:
        return web.Response(status=404, text="no media")
    data = await _fetch_tg_media(file_id, fuid, kind)
    if data is None:
        return web.Response(status=502, text="media unavailable")
    ctype = {"image": "image/jpeg", "video": "video/mp4", "audio": "audio/mpeg"}.get(kind, "application/octet-stream")
    headers = {
        "Cache-Control": "private, max-age=86400",
        "Content-Length": str(len(data)),
    }
    return web.Response(body=data, content_type=ctype, headers=headers)


# ─── Send message: dispatch by mode ────────────────────────────────────────

def _ext_for_mime(mime: str) -> str:
    if mime.startswith("image/jpeg"):
        return "jpg"
    if mime.startswith("image/png"):
        return "png"
    if mime.startswith("image/webp"):
        return "webp"
    if mime.startswith("video/"):
        return "mp4"
    return "bin"


async def _read_multipart(request: web.Request) -> tuple[dict, list[tuple[bytes, str]]]:
    """Read multipart: a 'payload' JSON field and zero or more 'files' uploads."""
    payload: dict = {}
    files: list[tuple[bytes, str]] = []
    reader = await request.multipart()
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "payload":
            raw = await part.text()
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {}
        elif part.name == "files":
            buf = bytearray()
            mime = part.headers.get("Content-Type", "application/octet-stream")
            while True:
                chunk = await part.read_chunk(1 << 16)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > _MAX_UPLOAD_SIZE:
                    raise web.HTTPRequestEntityTooLarge(
                        max_size=_MAX_UPLOAD_SIZE, actual_size=len(buf),
                    )
            if buf:
                files.append((bytes(buf), mime))
            if len(files) > _MAX_UPLOAD_FILES:
                raise web.HTTPRequestEntityTooLarge(
                    max_size=_MAX_UPLOAD_FILES, actual_size=len(files),
                )
    return payload, files


async def handle_send(request: web.Request) -> web.Response:
    s = _require_session(request)
    uid = s["user_id"]
    try:
        cid = int(request.match_info["cid"])
    except Exception:
        return web.json_response({"error": "Неверный ID"}, status=400)
    chat = _db.web_chat_get(cid, uid)
    if chat is None:
        return web.json_response({"error": "Чат не найден"}, status=404)
    if _db.web_msg_count(cid) >= _MAX_MESSAGES_PER_CHAT:
        return web.json_response(
            {"error": f"В чате достигнут лимит {_MAX_MESSAGES_PER_CHAT} сообщений. Создайте новый чат."},
            status=400,
        )

    ctype = (request.headers.get("Content-Type") or "").lower()
    if ctype.startswith("multipart/"):
        try:
            payload, files = await _read_multipart(request)
        except web.HTTPException:
            raise
        except Exception:
            return web.json_response({"error": "Не удалось прочитать запрос"}, status=400)
    else:
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "Неверный запрос"}, status=400)
        files = []

    mode = (payload.get("mode") or "chat").strip().lower()
    text = (payload.get("text") or "").strip()
    if len(text) > _MAX_PROMPT_CHARS:
        return web.json_response({"error": f"Текст слишком длинный (макс {_MAX_PROMPT_CHARS} знаков)"}, status=400)
    if not text and not files and mode != "chat":
        return web.json_response({"error": "Введите описание"}, status=400)
    if not text and not files and mode == "chat":
        return web.json_response({"error": "Введите сообщение"}, status=400)

    user_first = get_user_settings(uid).get("first_name") or str(uid)

    # Persist the user message immediately (with first attached image as preview)
    user_extras: dict[str, Any] = {}
    user_file_id = ""
    user_fuid = ""
    user_kind = ""
    if files:
        first_bytes, first_mime = files[0]
        if first_mime.startswith("image/"):
            from bot.log_channel import log_generation
            res = await log_generation(
                image_bytes=first_bytes,
                prompt=f"[upload] {text[:120]}",
                user_id=uid,
                user_name=user_first,
                platform="web",
                model="upload",
            )
            if res:
                user_file_id, user_fuid = res
                user_kind = "image"
        user_extras["upload_count"] = len(files)

    user_msg_id = _db.web_msg_add(
        chat_id=cid, role="user", mode=mode, content_text=text,
        model=(payload.get("model") or "")[:80],
        file_id=user_file_id, file_unique_id=user_fuid, file_kind=user_kind,
        extras_json=json.dumps(user_extras, ensure_ascii=False),
    )

    # Auto-rename chat from the first user message
    if text and chat["title"] in ("Новый чат", "") and _db.web_msg_count(cid) <= 2:
        new_title = text.replace("\n", " ")[:60]
        _db.web_chat_update_title(cid, uid, new_title)

    if mode == "chat":
        return await _run_chat(uid, user_first, cid, payload, files, user_msg_id)
    if mode == "image":
        return await _run_image(uid, user_first, cid, payload, files, user_msg_id)
    if mode == "video":
        return await _run_video(uid, user_first, cid, payload, files, user_msg_id)
    if mode == "music":
        return await _run_music(uid, user_first, cid, payload, files, user_msg_id)
    return web.json_response({"error": "Неизвестный режим"}, status=400)


# ─── Chat (text) ────────────────────────────────────────────────────────────

def _build_chat_history(chat_id: int, new_text: str, new_files: list[tuple[bytes, str]],
                        backend: str) -> list[dict[str, Any]]:
    """Build conversation history for chat_text / chat_grok.

    Returns the internal multimodal format (list of {"role", "parts"}).
    `chat_text` accepts dict-form genai contents directly; `chat_grok` will
    convert this format internally.
    """
    history: list[dict[str, Any]] = []
    recent = _db.web_msg_recent(chat_id, limit=_HISTORY_TURNS_FOR_MODEL)
    for m in recent[:-1]:  # last one is the just-saved user msg, we add it explicitly
        if m["mode"] != "chat":
            # Reference-only for non-chat results — don't try to re-feed the file
            label = {
                "image": "[ассистент сгенерировал изображение]",
                "video": "[ассистент сгенерировал видео]",
                "music": "[ассистент сгенерировал музыкальный трек]",
            }.get(m["mode"], "[ассистент выполнил генерацию]")
            history.append({"role": "model" if m["role"] == "assistant" else "user",
                            "parts": [{"type": "text", "text": label + ": " + (m["content_text"] or "")}]})
            continue
        role = "model" if m["role"] == "assistant" else "user"
        history.append({"role": role, "parts": [{"type": "text", "text": m["content_text"] or ""}]})

    # Current user turn
    parts: list[dict[str, Any]] = []
    if new_text:
        parts.append({"type": "text", "text": new_text})
    for data, mime in new_files:
        parts.append({"type": "media", "mime_type": mime, "data": data})
    history.append({"role": "user", "parts": parts})
    return history


def _internal_to_genai_contents(history: list[dict[str, Any]]) -> list[Any]:
    """Convert internal history → list of dicts accepted by google.genai SDK."""
    out: list[Any] = []
    for msg in history:
        role = msg.get("role")
        gen_role = "model" if role == "model" else "user"
        gparts: list[Any] = []
        for p in msg.get("parts", []):
            if p.get("type") == "text":
                gparts.append({"text": p.get("text", "")})
            elif p.get("type") == "media":
                data = p.get("data")
                mime = p.get("mime_type") or "application/octet-stream"
                if data:
                    gparts.append({"inline_data": {"mime_type": mime, "data": data}})
        if gparts:
            out.append({"role": gen_role, "parts": gparts})
    return out


async def _run_chat(uid: int, user_first: str, cid: int,
                    payload: dict, files: list[tuple[bytes, str]],
                    user_msg_id: int | None) -> web.Response:
    if _vertex_service is None:
        return web.json_response({"error": "Сервис временно недоступен"}, status=503)
    if not has_chat_quota(uid):
        return web.json_response({"error": "Дневной лимит чата исчерпан. Пополните баланс."}, status=429)

    chat_model_key = payload.get("model") or DEFAULT_CHAT_MODEL
    if chat_model_key not in CHAT_MODELS:
        chat_model_key = DEFAULT_CHAT_MODEL
    info = CHAT_MODELS[chat_model_key]
    backend = info["backend"]
    text = (payload.get("text") or "").strip()
    enable_search = bool(payload.get("search", False))

    # Persist uploaded files into the user message extras for redisplay
    if files and user_msg_id:
        user_first_image_bytes = None
        for d, m in files:
            if m.startswith("image/"):
                user_first_image_bytes = (d, m)
                break

    history = _build_chat_history(cid, text, files, backend)

    try:
        if backend == "grok":
            answer = await _vertex_service.chat_grok(history, enable_search=enable_search)
        else:
            contents = _internal_to_genai_contents(history)
            answer = await _vertex_service.chat_text(
                contents=contents,
                model_override=info.get("model_id"),
                use_search=enable_search,
            )
    except Exception as exc:
        logger.warning("web chat: %s", exc)
        msg = "Не удалось получить ответ модели. Попробуйте ещё раз."
        if "quota" in str(exc).lower() or "exhausted" in str(exc).lower():
            msg = "Все слоты модели заняты. Подождите минуту и повторите."
        return web.json_response({"error": msg}, status=502)

    if not answer:
        answer = "(модель вернула пустой ответ)"

    increment_chat_count(uid)

    asst_id = _db.web_msg_add(
        chat_id=cid, role="assistant", mode="chat",
        content_text=answer, model=chat_model_key,
    )
    return web.json_response({
        "ok": True,
        "user_msg_id": user_msg_id,
        "assistant": {
            "id": asst_id, "role": "assistant", "mode": "chat",
            "content_text": answer, "model": chat_model_key,
        },
    })


# ─── Image generation ───────────────────────────────────────────────────────

def _image_credits_for_resolution(_resolution: str) -> int:
    # 1 credit for any web-side image generation (matches free-tier mapping in bot)
    return 1


async def _run_image(uid: int, user_first: str, cid: int,
                     payload: dict, files: list[tuple[bytes, str]],
                     user_msg_id: int | None) -> web.Response:
    if _vertex_service is None:
        return web.json_response({"error": "Сервис временно недоступен"}, status=503)
    model = (payload.get("model") or "gemini-3.1-flash-image-preview").strip()
    info = AVAILABLE_MODELS.get(model)
    if not info or info.get("type") != "image":
        return web.json_response({"error": "Неверная модель изображения"}, status=400)
    aspect = (payload.get("aspect_ratio") or "1:1").strip()
    if aspect not in {"1:1", "16:9", "9:16", "4:3", "3:4"}:
        aspect = "1:1"
    text = (payload.get("text") or "").strip()
    cost = _image_credits_for_resolution(payload.get("resolution") or "1080p")
    if not reserve_credits(uid, cost):
        return web.json_response({"error": "Недостаточно кредитов"}, status=402)

    confirmed = False
    try:
        image_inputs = [d for d, m in files if m.startswith("image/")][:4]
        try:
            image_bytes = await _vertex_service.generate_image(
                prompt=text or "Без описания",
                images=image_inputs or None,
                model_override=model,
                aspect_ratio=aspect,
                user_id=uid,
                username=user_first,
            )
        except Exception as exc:
            logger.warning("web image gen: %s", exc)
            return web.json_response(
                {"error": "Не удалось сгенерировать изображение. Попробуйте ещё раз."},
                status=502,
            )
        if not image_bytes:
            return web.json_response({"error": "Модель не вернула изображение"}, status=502)

        from bot.log_channel import log_generation
        res = await log_generation(
            image_bytes=image_bytes, prompt=text, user_id=uid,
            user_name=user_first, platform="web", model=model,
        )
        if not res:
            return web.json_response({"error": "Не удалось сохранить результат"}, status=500)
        file_id, fuid = res
        confirm_credits(uid, cost, user_first, platform="web",
                        prompt=text, model=model, gen_type="image")
        confirmed = True
        asst_id = _db.web_msg_add(
            chat_id=cid, role="assistant", mode="image",
            content_text="", model=model,
            file_id=file_id, file_unique_id=fuid, file_kind="image",
            extras_json=json.dumps({"aspect": aspect, "prompt": text}, ensure_ascii=False),
        )
        return web.json_response({
            "ok": True,
            "user_msg_id": user_msg_id,
            "assistant": {
                "id": asst_id, "role": "assistant", "mode": "image",
                "content_text": "", "model": model, "file_kind": "image",
                "extras": {"aspect": aspect, "prompt": text},
            },
        })
    finally:
        if not confirmed:
            release_credits(uid, cost)


# ─── Video generation (long-running with status polling) ───────────────────

async def _run_video(uid: int, user_first: str, cid: int,
                     payload: dict, files: list[tuple[bytes, str]],
                     user_msg_id: int | None) -> web.Response:
    if _vertex_service is None:
        return web.json_response({"error": "Сервис временно недоступен"}, status=503)
    model = (payload.get("model") or "veo-3.1-fast-generate-001").strip()
    info = AVAILABLE_MODELS.get(model)
    if not info or info.get("type") != "video":
        return web.json_response({"error": "Неверная модель видео"}, status=400)
    aspect = (payload.get("aspect_ratio") or "16:9").strip()
    if aspect not in VIDEO_ASPECT_RATIOS:
        aspect = "16:9"
    try:
        duration = int(payload.get("duration") or 8)
    except Exception:
        duration = 8
    if duration not in (4, 6, 8):
        duration = 8
    resolution = (payload.get("resolution") or "720p").strip()
    if resolution not in get_video_resolutions_for_model(model):
        resolution = "720p"
    audio = bool(payload.get("audio", True)) and video_supports_audio(model)
    text = (payload.get("text") or "").strip()

    cost = calc_video_credits(model, duration_seconds=duration,
                              audio=audio, resolution=resolution)
    if not reserve_credits(uid, cost):
        return web.json_response({"error": "Недостаточно кредитов"}, status=402)

    image_input = None
    video_input = None
    for d, m in files:
        if m.startswith("image/") and image_input is None and video_supports_image(model):
            image_input = d
        elif m.startswith("video/") and video_input is None and info.get("supports_video_extension"):
            video_input = d

    gen_id = _gen_new(uid)
    _gens_gc()

    async def _runner():
        try:
            _gen_update(gen_id, status="running", label="Генерация видео…", pct=5)

            def on_progress(elapsed: float):
                # Approximate: assume ~120s baseline; cap at 90%
                pct = min(90, int(5 + elapsed * 0.7))
                _gen_update(gen_id, pct=pct, label=f"Генерация… {int(elapsed)}с")

            try:
                video_bytes = await _vertex_service.generate_video(
                    prompt=text or "Без описания",
                    model=model,
                    aspect_ratio=aspect,
                    duration_seconds=duration,
                    resolution=resolution,
                    person_generation="allow_adult",
                    generate_audio=audio,
                    on_progress=on_progress,
                    image=image_input,
                    video=video_input,
                )
            except Exception as exc:
                logger.warning("web video gen: %s", exc)
                release_credits(uid, cost)
                _gen_update(gen_id, status="error",
                            error="Не удалось сгенерировать видео. Попробуйте ещё раз.")
                return

            if not video_bytes:
                release_credits(uid, cost)
                _gen_update(gen_id, status="error", error="Модель не вернула видео")
                return

            _gen_update(gen_id, pct=95, label="Загрузка результата…")
            from bot.log_channel import log_generation_video
            res = await log_generation_video(
                video_bytes=video_bytes, prompt=text, user_id=uid,
                user_name=user_first, platform="web", model=model,
            )
            if not res:
                release_credits(uid, cost)
                _gen_update(gen_id, status="error", error="Не удалось сохранить результат")
                return
            file_id, fuid = res
            confirm_credits(uid, cost, user_first, platform="web",
                            prompt=text, model=model, gen_type="video")
            asst_id = _db.web_msg_add(
                chat_id=cid, role="assistant", mode="video",
                content_text="", model=model,
                file_id=file_id, file_unique_id=fuid, file_kind="video",
                extras_json=json.dumps({
                    "aspect": aspect, "duration": duration,
                    "resolution": resolution, "audio": audio,
                    "prompt": text,
                }, ensure_ascii=False),
            )
            _gen_update(gen_id, status="done", pct=100,
                        label="Готово", msg_id=asst_id)
        except Exception as exc:
            logger.exception("web video runner crashed")
            release_credits(uid, cost)
            _gen_update(gen_id, status="error", error=str(exc)[:200] or "Ошибка")

    asyncio.create_task(_runner())
    return web.json_response({
        "ok": True, "gen_id": gen_id,
        "user_msg_id": user_msg_id,
        "estimated_credits": cost,
    })


# ─── Music generation ───────────────────────────────────────────────────────

async def _run_music(uid: int, user_first: str, cid: int,
                     payload: dict, files: list[tuple[bytes, str]],
                     user_msg_id: int | None) -> web.Response:
    if _vertex_service is None:
        return web.json_response({"error": "Сервис временно недоступен"}, status=503)
    model = (payload.get("model") or "lyria-3-clip-preview").strip()
    info = AVAILABLE_MODELS.get(model)
    if not info or info.get("type") != "music":
        return web.json_response({"error": "Неверная модель"}, status=400)
    text = (payload.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "Опишите музыку"}, status=400)

    cost = get_music_credits_cost(model)
    if not reserve_credits(uid, cost):
        return web.json_response({"error": "Недостаточно кредитов"}, status=402)

    image_input = None
    if music_supports_image(model):
        for d, m in files:
            if m.startswith("image/"):
                image_input = d
                break

    gen_id = _gen_new(uid)
    _gens_gc()

    async def _runner():
        try:
            _gen_update(gen_id, status="running", label="Генерация музыки…", pct=10)
            try:
                audio_bytes = await _vertex_service.generate_music(
                    prompt=text, model=model,
                    user_id=uid, username=user_first,
                    image=image_input,
                )
            except Exception as exc:
                logger.warning("web music gen: %s", exc)
                release_credits(uid, cost)
                _gen_update(gen_id, status="error",
                            error="Не удалось сгенерировать музыку. Попробуйте ещё раз.")
                return
            if not audio_bytes:
                release_credits(uid, cost)
                _gen_update(gen_id, status="error", error="Модель не вернула аудио")
                return
            _gen_update(gen_id, pct=90, label="Загрузка результата…")
            from bot.log_channel import log_generation_audio
            res = await log_generation_audio(
                audio_bytes=audio_bytes, prompt=text, user_id=uid,
                user_name=user_first, platform="web", model=model,
            )
            if not res:
                release_credits(uid, cost)
                _gen_update(gen_id, status="error", error="Не удалось сохранить результат")
                return
            file_id, fuid = res
            confirm_credits(uid, cost, user_first, platform="web",
                            prompt=text, model=model, gen_type="music")
            asst_id = _db.web_msg_add(
                chat_id=cid, role="assistant", mode="music",
                content_text="", model=model,
                file_id=file_id, file_unique_id=fuid, file_kind="audio",
                extras_json=json.dumps({"prompt": text}, ensure_ascii=False),
            )
            _gen_update(gen_id, status="done", pct=100,
                        label="Готово", msg_id=asst_id)
        except Exception as exc:
            logger.exception("web music runner crashed")
            release_credits(uid, cost)
            _gen_update(gen_id, status="error", error=str(exc)[:200] or "Ошибка")

    asyncio.create_task(_runner())
    return web.json_response({
        "ok": True, "gen_id": gen_id,
        "user_msg_id": user_msg_id,
        "estimated_credits": cost,
    })


# ─── Generation status polling ──────────────────────────────────────────────

async def handle_gen_status(request: web.Request) -> web.Response:
    s = _require_session(request)
    gid = request.match_info.get("gen_id", "")
    g = _gens.get(gid)
    if g is None:
        return web.json_response({"error": "not found"}, status=404)
    if g.get("user_id") != s["user_id"]:
        return web.json_response({"error": "forbidden"}, status=403)
    out = {
        "status": g["status"], "pct": g["pct"], "label": g["label"],
        "error": g.get("error", ""),
    }
    msg_id = g.get("msg_id")
    if msg_id:
        out["msg_id"] = msg_id
        # Pull the assistant message for display
        try:
            conn = _db._get_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, role, mode, content_text, model, file_kind, "
                    "extras_json, created_at "
                    "FROM bot_web_messages WHERE id=%s",
                    (msg_id,),
                )
                row = cur.fetchone()
            if row:
                try:
                    extras = json.loads(row[6]) if row[6] else {}
                except Exception:
                    extras = {}
                out["assistant"] = {
                    "id": int(row[0]), "role": row[1], "mode": row[2],
                    "content_text": row[3], "model": row[4],
                    "file_kind": row[5], "extras": extras,
                    "created_at": row[7].isoformat() if row[7] else "",
                }
        except Exception:
            pass
    return web.json_response(out)


# ─── HTML UI ────────────────────────────────────────────────────────────────

def _shell_html() -> str:
    """Single-page chat UI shell. JS bootstraps state from /chat/api/me."""
    return """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Веб-чат — PicGenAI</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@500;600;700&family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#050507;--surface:#0a0a0e;--surface2:#0f0f14;--surface3:#15151c;
  --border:rgba(255,255,255,.06);--border-md:rgba(255,255,255,.10);
  --text:#ededf2;--muted:#5e5e76;--muted2:#8a8aa6;
  --accent:#9b8afb;--accent-bright:#b8acff;
  --accent-dim:rgba(155,138,251,.07);--accent-glow:rgba(155,138,251,.18);
  --green:#6ee7b7;--red:#fb7185;--yellow:#fcd34d;
  --radius:12px;
}
html,body{height:100%}
body{font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif;
  background:var(--bg);color:var(--text);overflow:hidden;
  -webkit-font-smoothing:antialiased;font-weight:400}
button,input,textarea,select{font-family:inherit;color:inherit;background:none;border:none;outline:none}
button{cursor:pointer}
a{color:var(--accent);text-decoration:none}
a:hover{opacity:.8}

/* ── Login screen ── */
#login{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;
  padding:20px;background:radial-gradient(circle at 30% 20%,rgba(155,138,251,.08),transparent 50%),var(--bg)}
.login-card{width:100%;max-width:420px;background:var(--surface);
  border:1px solid var(--border);border-radius:18px;padding:36px 32px}
.login-logo{font-family:'Syne',sans-serif;font-size:1.6em;font-weight:700;
  letter-spacing:-.01em;margin-bottom:6px}
.login-logo span{color:var(--accent)}
.login-sub{color:var(--muted2);font-size:.92em;margin-bottom:28px;line-height:1.55}
.tabs{display:flex;gap:8px;background:var(--surface2);border:1px solid var(--border);
  border-radius:10px;padding:4px;margin-bottom:18px}
.tab{flex:1;padding:9px 14px;border-radius:8px;font-size:.88em;color:var(--muted2);
  font-weight:500;transition:all .15s}
.tab.active{background:var(--accent-dim);color:var(--text)}
.field{display:flex;flex-direction:column;gap:7px;margin-bottom:14px}
.field label{font-size:.78em;color:var(--muted2);text-transform:uppercase;letter-spacing:.06em;font-weight:500}
.field input{padding:11px 14px;background:var(--surface2);border:1px solid var(--border);
  border-radius:10px;font-size:.95em;transition:border-color .15s}
.field input:focus{border-color:var(--accent)}
.help{color:var(--muted2);font-size:.82em;line-height:1.5;margin:6px 0 0}
.btn-primary{width:100%;padding:12px 18px;border-radius:10px;
  background:var(--accent);color:#1a0e3d;font-weight:600;font-size:.95em;
  transition:background .15s,transform .05s}
.btn-primary:hover{background:var(--accent-bright)}
.btn-primary:active{transform:translateY(1px)}
.btn-primary[disabled]{opacity:.45;cursor:not-allowed}
.error-box{background:rgba(251,113,133,.07);border:1px solid rgba(251,113,133,.25);
  color:var(--red);padding:10px 14px;border-radius:10px;font-size:.88em;margin-bottom:14px}
.success-box{background:rgba(110,231,183,.06);border:1px solid rgba(110,231,183,.25);
  color:var(--green);padding:10px 14px;border-radius:10px;font-size:.88em;margin-bottom:14px}

/* ── App layout ── */
#app{position:fixed;inset:0;display:flex;background:var(--bg)}
#app.hidden{display:none}
#login.hidden{display:none}

.sidebar{width:268px;background:var(--surface);border-right:1px solid var(--border);
  display:flex;flex-direction:column;flex-shrink:0}
.sb-head{padding:18px 18px 14px;display:flex;align-items:center;gap:10px;
  border-bottom:1px solid var(--border)}
.sb-logo{font-family:'Syne',sans-serif;font-size:1.05em;font-weight:700;letter-spacing:-.01em}
.sb-logo span{color:var(--accent)}
.sb-new{margin:14px 18px 6px;padding:10px 14px;border-radius:10px;
  background:var(--accent-dim);border:1px solid rgba(155,138,251,.18);
  color:var(--text);font-size:.86em;font-weight:500;display:flex;align-items:center;gap:8px;
  justify-content:center;transition:all .15s}
.sb-new:hover{background:var(--accent-glow)}
.sb-list{flex:1;overflow-y:auto;padding:10px 8px 12px}
.sb-item{display:flex;align-items:center;gap:8px;padding:9px 10px;border-radius:8px;
  color:var(--muted2);font-size:.86em;cursor:pointer;transition:all .15s;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;position:relative}
.sb-item:hover{background:var(--surface2);color:var(--text)}
.sb-item.active{background:var(--accent-dim);color:var(--text)}
.sb-item-title{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sb-item-actions{display:none;gap:4px}
.sb-item:hover .sb-item-actions,.sb-item.active .sb-item-actions{display:flex}
.sb-item-btn{padding:4px;border-radius:6px;color:var(--muted);transition:color .15s}
.sb-item-btn:hover{color:var(--text);background:rgba(255,255,255,.05)}
.sb-item-btn svg{width:14px;height:14px;stroke:currentColor;fill:none;stroke-width:1.7}
.sb-foot{padding:14px 18px;border-top:1px solid var(--border);display:flex;flex-direction:column;gap:8px}
.sb-credits{display:flex;justify-content:space-between;align-items:center;
  font-size:.82em;color:var(--muted2)}
.sb-credits b{color:var(--accent-bright);font-family:'Syne',sans-serif;font-weight:600;font-size:1.18em}
.sb-user{display:flex;align-items:center;gap:8px;color:var(--muted2);font-size:.82em}
.sb-logout{font-size:.78em;color:var(--muted2);padding:4px 0;text-align:left}
.sb-logout:hover{color:var(--red)}
.sb-empty{color:var(--muted);font-size:.84em;padding:14px 12px;text-align:center}

/* ── Main column ── */
.main{flex:1;display:flex;flex-direction:column;min-width:0;background:var(--bg)}
.main-head{padding:14px 24px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;gap:14px}
.main-title{font-family:'Syne',sans-serif;font-size:1.05em;font-weight:600;
  flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.main-actions{display:flex;gap:8px}
.btn-icon{width:34px;height:34px;border-radius:8px;display:flex;align-items:center;
  justify-content:center;color:var(--muted2);transition:all .15s}
.btn-icon:hover{background:var(--surface2);color:var(--text)}
.btn-icon svg{width:16px;height:16px;stroke:currentColor;fill:none;stroke-width:1.7}

.messages{flex:1;overflow-y:auto;padding:24px 24px 12px;
  display:flex;flex-direction:column;gap:18px}
.empty-state{margin:auto;text-align:center;color:var(--muted2);max-width:520px;padding:40px 20px}
.empty-state h2{font-family:'Syne',sans-serif;font-weight:600;font-size:1.5em;
  margin-bottom:10px;color:var(--text);letter-spacing:-.01em}
.empty-state p{font-size:.92em;line-height:1.6;margin-bottom:22px}
.starter-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;text-align:left}
.starter-card{padding:14px 16px;background:var(--surface);border:1px solid var(--border);
  border-radius:12px;cursor:pointer;transition:all .15s}
.starter-card:hover{border-color:var(--border-md);background:var(--surface2)}
.starter-card .sc-mode{font-size:.72em;color:var(--accent);text-transform:uppercase;
  letter-spacing:.08em;margin-bottom:4px;font-weight:500}
.starter-card .sc-text{font-size:.88em;color:var(--text);line-height:1.4}

.msg{display:flex;gap:14px;max-width:920px;width:100%;margin:0 auto}
.msg-avatar{width:32px;height:32px;border-radius:50%;flex-shrink:0;
  display:flex;align-items:center;justify-content:center;
  font-size:.78em;font-weight:600;letter-spacing:.02em}
.msg.user .msg-avatar{background:var(--surface3);color:var(--text)}
.msg.assistant .msg-avatar{background:var(--accent-dim);color:var(--accent-bright);
  border:1px solid rgba(155,138,251,.2)}
.msg-body{flex:1;min-width:0}
.msg-meta{display:flex;align-items:center;gap:8px;font-size:.78em;color:var(--muted);margin-bottom:4px}
.msg-author{color:var(--text);font-weight:500}
.msg-mode{padding:1px 7px;border-radius:5px;background:var(--surface2);
  color:var(--muted2);text-transform:uppercase;letter-spacing:.06em;font-size:.92em}
.msg-mode.image{color:#a5d6ff}
.msg-mode.video{color:#ffb8d6}
.msg-mode.music{color:#fcd34d}
.msg-content{color:var(--text);font-size:.95em;line-height:1.65;
  white-space:pre-wrap;word-wrap:break-word}
.msg-content p{margin:0 0 8px}
.msg-content p:last-child{margin:0}
.msg-content code{font-family:'JetBrains Mono',monospace;font-size:.88em;
  background:var(--surface2);padding:1px 6px;border-radius:4px}
.msg-content pre{font-family:'JetBrains Mono',monospace;font-size:.84em;
  background:var(--surface2);border:1px solid var(--border);border-radius:8px;
  padding:12px 14px;overflow-x:auto;margin:8px 0}
.msg-content pre code{background:none;padding:0}

.msg-attach{margin-top:10px;display:flex;flex-direction:column;gap:8px;max-width:520px}
.msg-image{display:block;max-width:100%;max-height:480px;border-radius:10px;
  border:1px solid var(--border);background:var(--surface);cursor:pointer}
.msg-video{display:block;max-width:100%;max-height:480px;border-radius:10px;background:#000}
.msg-audio{width:100%;max-width:480px}
.msg-pending{padding:12px 16px;background:var(--surface);border:1px solid var(--border);
  border-radius:10px;color:var(--muted2);font-size:.88em;display:flex;align-items:center;gap:10px}
.spinner{width:14px;height:14px;border:2px solid var(--border-md);
  border-top-color:var(--accent);border-radius:50%;animation:spin .8s linear infinite;flex-shrink:0}
@keyframes spin{to{transform:rotate(360deg)}}
.progress-bar{height:3px;background:var(--surface2);border-radius:2px;overflow:hidden;margin-top:6px}
.progress-bar > div{height:100%;background:var(--accent);transition:width .3s}

.input-wrap{padding:12px 24px 18px;border-top:1px solid var(--border);background:var(--bg)}
.input-card{max-width:920px;margin:0 auto;background:var(--surface);
  border:1px solid var(--border);border-radius:14px;
  padding:10px 12px 8px;transition:border-color .15s}
.input-card:focus-within{border-color:var(--accent-glow)}
.input-row{display:flex;gap:8px;align-items:flex-end}
.input-text{flex:1;min-height:40px;max-height:200px;padding:8px 10px;
  font-size:.95em;line-height:1.5;resize:none;overflow-y:auto}
.input-text::placeholder{color:var(--muted)}
.input-actions{display:flex;gap:6px;align-items:center;padding-bottom:6px}
.btn-send{padding:9px 14px;background:var(--accent);color:#1a0e3d;border-radius:9px;
  font-weight:600;font-size:.88em;display:flex;align-items:center;gap:6px}
.btn-send:hover{background:var(--accent-bright)}
.btn-send[disabled]{opacity:.4;cursor:not-allowed}
.btn-attach{width:32px;height:32px;border-radius:8px;display:flex;align-items:center;
  justify-content:center;color:var(--muted2);transition:all .15s}
.btn-attach:hover{background:var(--surface2);color:var(--text)}
.btn-attach svg{width:18px;height:18px;stroke:currentColor;fill:none;stroke-width:1.6}

.pending-files{display:flex;gap:6px;flex-wrap:wrap;padding:6px 0}
.pf{display:flex;align-items:center;gap:6px;padding:4px 8px 4px 6px;
  background:var(--surface2);border:1px solid var(--border);border-radius:7px;font-size:.78em}
.pf img{width:24px;height:24px;border-radius:4px;object-fit:cover}
.pf-name{max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pf-x{color:var(--muted2);padding:2px;line-height:0}
.pf-x:hover{color:var(--red)}

.input-toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;
  padding:6px 4px 0;border-top:1px solid var(--border);margin-top:6px}
.mode-pill{padding:5px 10px;border-radius:6px;font-size:.78em;
  color:var(--muted2);border:1px solid transparent;transition:all .15s;font-weight:500}
.mode-pill:hover{background:var(--surface2);color:var(--text)}
.mode-pill.active{background:var(--accent-dim);color:var(--text);border-color:rgba(155,138,251,.18)}
.tb-spacer{flex:1}
.tb-select{padding:5px 10px;border-radius:6px;background:var(--surface2);
  border:1px solid var(--border);color:var(--text);font-size:.78em;cursor:pointer}
.tb-select:hover{border-color:var(--border-md)}
.tb-toggle{display:inline-flex;align-items:center;gap:5px;padding:5px 10px;border-radius:6px;
  font-size:.78em;color:var(--muted2);background:var(--surface2);border:1px solid var(--border)}
.tb-toggle.on{color:var(--accent-bright);border-color:rgba(155,138,251,.25);background:var(--accent-dim)}
.tb-cost{font-size:.76em;color:var(--muted2)}
.tb-cost b{color:var(--accent-bright);font-weight:500}

.mobile-toggle{display:none}
.scrim{display:none}

@media (max-width: 880px){
  .sidebar{position:fixed;inset:0 auto 0 0;width:280px;z-index:30;
    transform:translateX(-100%);transition:transform .2s}
  .sidebar.open{transform:translateX(0)}
  .scrim{position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:29;
    opacity:0;pointer-events:none;transition:opacity .2s}
  .scrim.show{opacity:1;pointer-events:auto;display:block}
  .mobile-toggle{display:flex}
  .main-head{padding:12px 14px}
  .messages{padding:14px 14px 10px}
  .input-wrap{padding:10px 12px 14px}
  .starter-grid{grid-template-columns:1fr}
}

::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border-md);border-radius:4px}
::-webkit-scrollbar-thumb:hover{background:var(--accent-glow)}
</style>
</head>
<body>

<!-- LOGIN -->
<div id="login">
  <div class="login-card">
    <div class="login-logo">Pic<span>Gen</span>AI · Веб-чат</div>
    <div class="login-sub">Войдите по коду из бота. Кредиты и история — общие с Telegram и ВКонтакте.</div>

    <div class="tabs" id="loginTabs">
      <button class="tab active" data-platform="tg">Telegram</button>
      <button class="tab" data-platform="vk">ВКонтакте</button>
    </div>

    <div id="loginErr" class="error-box" style="display:none"></div>
    <div id="loginOk" class="success-box" style="display:none"></div>

    <div id="step1">
      <div class="field">
        <label id="idLabel">Telegram ID или @username</label>
        <input id="identInput" type="text" placeholder="например 123456789 или username" autocomplete="off">
      </div>
      <p class="help" id="step1Help">
        Сначала откройте бота и нажмите /start, чтобы он мог отправить вам код.
      </p>
      <div style="height:14px"></div>
      <button class="btn-primary" id="reqBtn">Получить код</button>
    </div>

    <div id="step2" style="display:none">
      <div class="field">
        <label>Код из бота</label>
        <input id="codeInput" type="text" inputmode="numeric" maxlength="6" placeholder="000000" autocomplete="one-time-code">
      </div>
      <p class="help">Код действует 5 минут. Не нашли — проверьте сообщения от бота.</p>
      <div style="height:14px"></div>
      <button class="btn-primary" id="verifyBtn">Войти</button>
      <div style="height:8px"></div>
      <button class="sb-logout" id="backBtn" style="text-align:center;width:100%;color:var(--muted2)">← Изменить</button>
    </div>
  </div>
</div>

<!-- APP -->
<div id="app" class="hidden">
  <div class="scrim" id="scrim"></div>
  <aside class="sidebar" id="sidebar">
    <div class="sb-head">
      <div class="sb-logo">Pic<span>Gen</span>AI</div>
    </div>
    <button class="sb-new" id="newChatBtn">
      <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14M5 12h14"/></svg>
      Новый чат
    </button>
    <div class="sb-list" id="chatsList"></div>
    <div class="sb-foot">
      <div class="sb-credits"><span>Баланс</span><b id="creditsLbl">—</b></div>
      <div class="sb-user" id="userLbl"></div>
      <button class="sb-logout" id="logoutBtn">Выйти</button>
    </div>
  </aside>

  <div class="main">
    <div class="main-head">
      <button class="btn-icon mobile-toggle" id="menuBtn" title="Меню">
        <svg viewBox="0 0 24 24"><path d="M3 6h18M3 12h18M3 18h18"/></svg>
      </button>
      <div class="main-title" id="mainTitle">PicGenAI</div>
      <div class="main-actions">
        <button class="btn-icon" id="renameBtn" title="Переименовать">
          <svg viewBox="0 0 24 24"><path d="M12 20h9M16.5 3.5a2.12 2.12 0 1 1 3 3L7 19l-4 1 1-4 12.5-12.5z"/></svg>
        </button>
        <button class="btn-icon" id="archiveBtn" title="Архивировать">
          <svg viewBox="0 0 24 24"><path d="M3 4h18v4H3zM5 8v12h14V8M10 12h4"/></svg>
        </button>
        <button class="btn-icon" id="deleteBtn" title="Удалить">
          <svg viewBox="0 0 24 24"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>
        </button>
      </div>
    </div>

    <div class="messages" id="messages"></div>

    <div class="input-wrap">
      <div class="input-card">
        <div class="pending-files" id="pendingFiles" style="display:none"></div>
        <div class="input-row">
          <textarea class="input-text" id="textInput" placeholder="Спросите что-нибудь или опишите, что сгенерировать…" rows="1"></textarea>
          <div class="input-actions">
            <button class="btn-attach" id="attachBtn" title="Прикрепить файл">
              <svg viewBox="0 0 24 24"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
            </button>
            <button class="btn-send" id="sendBtn">
              <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12h14M13 5l7 7-7 7"/></svg>
              <span>Отправить</span>
            </button>
          </div>
        </div>
        <div class="input-toolbar">
          <button class="mode-pill active" data-mode="chat">Чат</button>
          <button class="mode-pill" data-mode="image">Изображение</button>
          <button class="mode-pill" data-mode="video">Видео</button>
          <button class="mode-pill" data-mode="music">Музыка</button>
          <div class="tb-spacer"></div>
          <select class="tb-select" id="modelSelect"></select>
          <select class="tb-select" id="aspectSelect" style="display:none"></select>
          <select class="tb-select" id="durationSelect" style="display:none"></select>
          <select class="tb-select" id="resolutionSelect" style="display:none"></select>
          <button class="tb-toggle" id="audioToggle" style="display:none">Со звуком</button>
          <button class="tb-toggle" id="searchToggle" style="display:none">Поиск</button>
          <span class="tb-cost" id="costLbl"></span>
        </div>
        <input type="file" id="fileInput" multiple accept="image/*,video/mp4" style="display:none">
      </div>
    </div>
  </div>
</div>

<script>
(()=>{
  const $ = (id) => document.getElementById(id);
  const state = {
    me: null, catalog: null,
    chats: [], currentChatId: null, messages: [],
    mode: "chat", model: "",
    aspect: "1:1", duration: 8, resolution: "720p", audio: true, search: false,
    pendingFiles: [],   // [{name, type, size, dataUrl, blob}]
    sending: false,
    activeGen: null,
    polling: null,
  };

  function showError(msg) {
    const el = $("loginErr");
    el.textContent = msg; el.style.display = "block";
    $("loginOk").style.display = "none";
  }
  function showOk(msg) {
    const el = $("loginOk");
    el.textContent = msg; el.style.display = "block";
    $("loginErr").style.display = "none";
  }
  function clearMsgs() {
    $("loginErr").style.display = "none"; $("loginOk").style.display = "none";
  }
  function fmtCredits(n) {
    if (n == null) return "—";
    return n.toString();
  }
  function escapeHtml(s) {
    return (s||"").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;"}[c]));
  }
  function renderText(s) {
    if (!s) return "";
    let t = escapeHtml(s);
    t = t.replace(/```([\\s\\S]*?)```/g, (_, code) => "<pre><code>" + code + "</code></pre>");
    t = t.replace(/`([^`]+)`/g, "<code>$1</code>");
    t = t.replace(/\\*\\*([^*]+)\\*\\*/g, "<b>$1</b>");
    return t;
  }

  // ── Login ────────────────────────────────────────────────
  let loginPlatform = "tg";
  document.querySelectorAll("#loginTabs .tab").forEach(t => {
    t.addEventListener("click", () => {
      document.querySelectorAll("#loginTabs .tab").forEach(x => x.classList.remove("active"));
      t.classList.add("active");
      loginPlatform = t.dataset.platform;
      $("idLabel").textContent = loginPlatform === "vk"
        ? "VK ID или короткое имя"
        : "Telegram ID или @username";
      $("identInput").placeholder = loginPlatform === "vk"
        ? "например 12345 или durov"
        : "например 123456789 или username";
      $("step1Help").textContent = loginPlatform === "vk"
        ? "Сначала откройте чат с сообществом ВКонтакте и разрешите ему писать вам."
        : "Сначала откройте бота и нажмите /start, чтобы он мог отправить вам код.";
    });
  });
  $("identInput").addEventListener("keydown", e => { if (e.key === "Enter") $("reqBtn").click(); });
  $("codeInput").addEventListener("keydown", e => { if (e.key === "Enter") $("verifyBtn").click(); });

  let pendingUserId = null;

  $("reqBtn").addEventListener("click", async () => {
    clearMsgs();
    const ident = $("identInput").value.trim();
    if (!ident) { showError("Введите ID или username"); return; }
    $("reqBtn").disabled = true;
    try {
      const r = await fetch("/chat/api/login/request", {
        method: "POST", headers: {"Content-Type":"application/json"},
        body: JSON.stringify({platform: loginPlatform, identifier: ident}),
      });
      const j = await r.json();
      if (!r.ok) { showError(j.error || "Ошибка"); return; }
      pendingUserId = j.user_id;
      $("step1").style.display = "none";
      $("step2").style.display = "block";
      showOk("Код отправлен. Откройте бота и введите шестизначный код.");
      setTimeout(() => $("codeInput").focus(), 50);
    } catch (e) {
      showError("Сеть недоступна");
    } finally { $("reqBtn").disabled = false; }
  });

  $("backBtn").addEventListener("click", () => {
    $("step1").style.display = "block"; $("step2").style.display = "none";
    clearMsgs();
  });

  $("verifyBtn").addEventListener("click", async () => {
    clearMsgs();
    const code = $("codeInput").value.trim();
    if (!/^\\d{6}$/.test(code)) { showError("Введите шестизначный код"); return; }
    $("verifyBtn").disabled = true;
    try {
      const r = await fetch("/chat/api/login/verify", {
        method: "POST", headers: {"Content-Type":"application/json"},
        body: JSON.stringify({platform: loginPlatform, user_id: pendingUserId, code}),
      });
      const j = await r.json();
      if (!r.ok) { showError(j.error || "Ошибка"); return; }
      await bootApp();
    } catch (e) { showError("Сеть недоступна"); }
    finally { $("verifyBtn").disabled = false; }
  });

  // ── App boot ─────────────────────────────────────────────
  async function bootApp() {
    try {
      const me = await (await fetch("/chat/api/me")).json();
      if (me.error) { return; }
      state.me = me;
      const cat = await (await fetch("/chat/api/catalog")).json();
      state.catalog = cat;
      $("login").classList.add("hidden");
      $("app").classList.remove("hidden");
      $("creditsLbl").textContent = fmtCredits(me.credits);
      $("userLbl").textContent = (me.platform === "vk" ? "VK · " : "TG · ") + (me.first_name || me.user_id);
      await loadChats();
      bindModeUI();
      switchMode("chat");
    } catch (e) { console.error(e); }
  }

  async function refreshMe() {
    try {
      const me = await (await fetch("/chat/api/me")).json();
      if (me.user_id) {
        state.me = me;
        $("creditsLbl").textContent = fmtCredits(me.credits);
      }
    } catch {}
  }

  // ── Chats list ──────────────────────────────────────────
  async function loadChats() {
    const r = await fetch("/chat/api/chats");
    const j = await r.json();
    state.chats = j.chats || [];
    renderChats();
    if (state.currentChatId == null) {
      if (state.chats.length) selectChat(state.chats[0].id);
      else renderEmptyState();
    }
  }
  function renderChats() {
    const list = $("chatsList");
    if (!state.chats.length) {
      list.innerHTML = '<div class="sb-empty">Чатов пока нет — нажмите «Новый чат».</div>';
      return;
    }
    list.innerHTML = state.chats.map(c =>
      `<div class="sb-item${c.id === state.currentChatId ? " active":""}" data-id="${c.id}">
        <div class="sb-item-title">${escapeHtml(c.title)}</div>
        <div class="sb-item-actions">
          <button class="sb-item-btn" data-act="rename" data-id="${c.id}" title="Переименовать">
            <svg viewBox="0 0 24 24"><path d="M12 20h9M16.5 3.5a2.12 2.12 0 1 1 3 3L7 19l-4 1 1-4 12.5-12.5z"/></svg>
          </button>
          <button class="sb-item-btn" data-act="delete" data-id="${c.id}" title="Удалить">
            <svg viewBox="0 0 24 24"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>
          </button>
        </div>
      </div>`
    ).join("");
    list.querySelectorAll(".sb-item").forEach(el => {
      el.addEventListener("click", e => {
        if (e.target.closest(".sb-item-btn")) return;
        const id = +el.dataset.id;
        selectChat(id);
        if (window.innerWidth < 880) closeSidebar();
      });
    });
    list.querySelectorAll(".sb-item-btn").forEach(b => {
      b.addEventListener("click", e => {
        e.stopPropagation();
        const id = +b.dataset.id;
        if (b.dataset.act === "rename") promptRename(id);
        else if (b.dataset.act === "delete") confirmDelete(id);
      });
    });
  }
  $("newChatBtn").addEventListener("click", async () => {
    const r = await fetch("/chat/api/chats", {
      method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({title: "Новый чат"}),
    });
    const j = await r.json();
    if (j.error) { alert(j.error); return; }
    await loadChats();
    selectChat(j.id);
    $("textInput").focus();
    if (window.innerWidth < 880) closeSidebar();
  });

  async function selectChat(id) {
    state.currentChatId = id;
    state.messages = [];
    renderChats();
    const r = await fetch(`/chat/api/chats/${id}/messages`);
    const j = await r.json();
    if (j.error) { alert(j.error); return; }
    state.messages = j.messages || [];
    $("mainTitle").textContent = j.chat?.title || "Чат";
    renderMessages();
  }

  async function promptRename(id) {
    const cur = state.chats.find(c => c.id === id);
    const t = prompt("Новое название чата:", cur?.title || "");
    if (t == null) return;
    const title = t.trim().slice(0,120);
    if (!title) return;
    await fetch(`/chat/api/chats/${id}`, {
      method: "PATCH", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({title}),
    });
    await loadChats();
    if (state.currentChatId === id) $("mainTitle").textContent = title;
  }
  async function confirmDelete(id) {
    if (!confirm("Удалить чат и всю переписку?")) return;
    await fetch(`/chat/api/chats/${id}`, {method: "DELETE"});
    if (state.currentChatId === id) {
      state.currentChatId = null;
      state.messages = [];
    }
    await loadChats();
    if (!state.chats.length) renderEmptyState();
  }
  $("renameBtn").addEventListener("click", () => {
    if (state.currentChatId) promptRename(state.currentChatId);
  });
  $("deleteBtn").addEventListener("click", () => {
    if (state.currentChatId) confirmDelete(state.currentChatId);
  });
  $("archiveBtn").addEventListener("click", async () => {
    if (!state.currentChatId) return;
    if (!confirm("Архивировать чат? Он будет скрыт из списка.")) return;
    await fetch(`/chat/api/chats/${state.currentChatId}`, {
      method: "PATCH", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({archived: true}),
    });
    state.currentChatId = null;
    state.messages = [];
    await loadChats();
    if (!state.chats.length) renderEmptyState();
  });

  // ── Messages render ──────────────────────────────────────
  function renderEmptyState() {
    $("mainTitle").textContent = "PicGenAI";
    $("messages").innerHTML = `
      <div class="empty-state">
        <h2>Чат, который сразу всё умеет</h2>
        <p>Общайтесь с моделью, генерируйте изображения, видео и музыку — всё в одном окне.
        Выберите режим внизу или начните с одного из примеров.</p>
        <div class="starter-grid">
          <div class="starter-card" data-prompt="Сделай план поста про осенние тренды в дизайне" data-mode="chat">
            <div class="sc-mode">Чат</div>
            <div class="sc-text">Сделай план поста про осенние тренды в дизайне</div>
          </div>
          <div class="starter-card" data-prompt="Стилизованный портрет девушки в неоновом дожде, кинематографично" data-mode="image">
            <div class="sc-mode">Изображение</div>
            <div class="sc-text">Стилизованный портрет девушки в неоновом дожде</div>
          </div>
          <div class="starter-card" data-prompt="Закат над горами, медленный пролёт дрона" data-mode="video">
            <div class="sc-mode">Видео</div>
            <div class="sc-text">Закат над горами, медленный пролёт дрона</div>
          </div>
          <div class="starter-card" data-prompt="Спокойный лоу-фай бит для работы, 90 BPM" data-mode="music">
            <div class="sc-mode">Музыка</div>
            <div class="sc-text">Спокойный лоу-фай бит для работы, 90 BPM</div>
          </div>
        </div>
      </div>`;
    $("messages").querySelectorAll(".starter-card").forEach(c => {
      c.addEventListener("click", async () => {
        if (state.currentChatId == null) {
          const r = await fetch("/chat/api/chats", {
            method:"POST", headers:{"Content-Type":"application/json"},
            body: JSON.stringify({title:"Новый чат"}),
          });
          const j = await r.json();
          if (j.id) { await loadChats(); selectChat(j.id); }
        }
        switchMode(c.dataset.mode);
        $("textInput").value = c.dataset.prompt;
        $("textInput").focus();
        autosizeText();
      });
    });
  }

  function renderMessages() {
    const m = $("messages");
    if (!state.messages.length) {
      m.innerHTML = `<div class="empty-state"><p>Чат пуст — напишите сообщение или выберите режим генерации.</p></div>`;
      return;
    }
    m.innerHTML = state.messages.map(renderMessage).join("");
    m.scrollTop = m.scrollHeight;
  }
  function renderMessage(msg) {
    const isUser = msg.role === "user";
    const author = isUser ? (state.me?.first_name || "Вы") : "PicGenAI";
    const initials = isUser
      ? (state.me?.first_name || "В").slice(0,2).toUpperCase()
      : "AI";
    const modeLbl = {chat:"чат", image:"изображение", video:"видео", music:"музыка"}[msg.mode] || msg.mode;
    let media = "";
    if (msg.file_kind === "image") {
      media = `<div class="msg-attach"><img class="msg-image" src="/chat/api/media/${msg.id}" alt="" onclick="window.open(this.src,'_blank')"></div>`;
    } else if (msg.file_kind === "video") {
      media = `<div class="msg-attach"><video class="msg-video" controls src="/chat/api/media/${msg.id}"></video></div>`;
    } else if (msg.file_kind === "audio") {
      media = `<div class="msg-attach"><audio class="msg-audio" controls src="/chat/api/media/${msg.id}"></audio></div>`;
    }
    const pending = msg._pending
      ? `<div class="msg-attach"><div class="msg-pending"><div class="spinner"></div><div><div>${escapeHtml(msg._pendingLabel||"Генерация…")}</div><div class="progress-bar"><div style="width:${msg._pendingPct||0}%"></div></div></div></div></div>`
      : "";
    const errBox = msg._error
      ? `<div class="msg-attach"><div class="msg-pending" style="color:var(--red);border-color:rgba(251,113,133,.25)">${escapeHtml(msg._error)}</div></div>`
      : "";
    const content = msg.content_text ? `<div class="msg-content">${renderText(msg.content_text)}</div>` : "";
    return `
      <div class="msg ${isUser?"user":"assistant"}">
        <div class="msg-avatar">${initials}</div>
        <div class="msg-body">
          <div class="msg-meta">
            <span class="msg-author">${escapeHtml(author)}</span>
            <span class="msg-mode ${msg.mode||""}">${modeLbl}</span>
          </div>
          ${content}${media}${pending}${errBox}
        </div>
      </div>`;
  }

  // ── Mode + params UI ─────────────────────────────────────
  function bindModeUI() {
    document.querySelectorAll(".mode-pill").forEach(p => {
      p.addEventListener("click", () => switchMode(p.dataset.mode));
    });
    $("modelSelect").addEventListener("change", () => {
      state.model = $("modelSelect").value;
      reflectModelOptions();
      updateCost();
    });
    $("aspectSelect").addEventListener("change", () => { state.aspect = $("aspectSelect").value; updateCost(); });
    $("durationSelect").addEventListener("change", () => { state.duration = +$("durationSelect").value; updateCost(); });
    $("resolutionSelect").addEventListener("change", () => { state.resolution = $("resolutionSelect").value; updateCost(); });
    $("audioToggle").addEventListener("click", () => {
      state.audio = !state.audio;
      $("audioToggle").classList.toggle("on", state.audio);
      updateCost();
    });
    $("searchToggle").addEventListener("click", () => {
      state.search = !state.search;
      $("searchToggle").classList.toggle("on", state.search);
    });
    $("attachBtn").addEventListener("click", () => $("fileInput").click());
    $("fileInput").addEventListener("change", e => addFiles(e.target.files));
    $("textInput").addEventListener("input", autosizeText);
    $("textInput").addEventListener("keydown", e => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        $("sendBtn").click();
      }
    });
    $("sendBtn").addEventListener("click", sendMessage);
    $("logoutBtn").addEventListener("click", logout);
    $("menuBtn").addEventListener("click", openSidebar);
    $("scrim").addEventListener("click", closeSidebar);
  }
  function openSidebar(){ $("sidebar").classList.add("open"); $("scrim").classList.add("show"); }
  function closeSidebar(){ $("sidebar").classList.remove("open"); $("scrim").classList.remove("show"); }

  function switchMode(m) {
    state.mode = m;
    document.querySelectorAll(".mode-pill").forEach(p => {
      p.classList.toggle("active", p.dataset.mode === m);
    });
    const cat = state.catalog;
    const sel = $("modelSelect");
    let entries = [];
    if (m === "chat") {
      entries = Object.entries(cat.chat.models).map(([k,v]) => [k, v.label]);
      state.model = cat.chat.default;
    } else if (m === "image") {
      entries = Object.entries(cat.image.models).map(([k,v]) => [k, v.label]);
      state.model = entries[0]?.[0] || "";
    } else if (m === "video") {
      entries = Object.entries(cat.video.models).map(([k,v]) => [k, v.label]);
      state.model = "veo-3.1-fast-generate-001";
      if (!cat.video.models[state.model]) state.model = entries[0]?.[0] || "";
    } else if (m === "music") {
      entries = Object.entries(cat.music.models).map(([k,v]) => [k, v.label]);
      state.model = "lyria-3-clip-preview";
      if (!cat.music.models[state.model]) state.model = entries[0]?.[0] || "";
    }
    sel.innerHTML = entries.map(([k,l]) => `<option value="${k}"${k===state.model?" selected":""}>${escapeHtml(l)}</option>`).join("");
    state.model = sel.value || state.model;
    reflectModelOptions();
    updateCost();
    const fileAccept = m === "video" ? "image/*,video/mp4" : "image/*";
    $("fileInput").accept = fileAccept;
  }

  function reflectModelOptions() {
    const m = state.mode, cat = state.catalog;
    $("aspectSelect").style.display = (m === "image" || m === "video") ? "" : "none";
    $("durationSelect").style.display = (m === "video") ? "" : "none";
    $("resolutionSelect").style.display = (m === "video") ? "" : "none";
    $("audioToggle").style.display = (m === "video") ? "" : "none";
    $("searchToggle").style.display = (m === "chat") ? "" : "none";
    if (m === "image") {
      $("aspectSelect").innerHTML = cat.image.aspects
        .map(a => `<option value="${a}"${a===state.aspect?" selected":""}>${a}</option>`).join("");
    } else if (m === "video") {
      const info = cat.video.models[state.model] || {};
      $("aspectSelect").innerHTML = cat.video.aspects
        .map(a => `<option value="${a}"${a===state.aspect?" selected":""}>${a}</option>`).join("");
      $("durationSelect").innerHTML = cat.video.durations
        .map(d => `<option value="${d}"${(+d)===state.duration?" selected":""}>${d} сек</option>`).join("");
      const res = info.resolutions || ["720p","1080p"];
      if (!res.includes(state.resolution)) state.resolution = res[0];
      $("resolutionSelect").innerHTML = res
        .map(r => `<option value="${r}"${r===state.resolution?" selected":""}>${r}</option>`).join("");
      $("audioToggle").classList.toggle("on", state.audio);
      $("audioToggle").textContent = state.audio ? "Со звуком" : "Без звука";
      $("audioToggle").style.display = info.supports_audio ? "" : "none";
    }
    $("searchToggle").classList.toggle("on", state.search);
    $("searchToggle").textContent = state.search ? "Поиск ✓" : "Поиск";
  }

  function updateCost() {
    const lbl = $("costLbl");
    const cat = state.catalog;
    if (state.mode === "chat") { lbl.textContent = ""; return; }
    if (state.mode === "image") { lbl.innerHTML = "Стоимость: <b>1 кредит</b>"; return; }
    if (state.mode === "music") {
      const c = cat.music.models[state.model]?.credits || 2;
      lbl.innerHTML = `Стоимость: <b>${c} кредитов</b>`;
      return;
    }
    if (state.mode === "video") {
      // approximate locally; server is the source of truth
      const dur = +state.duration || 8;
      const audio = state.audio;
      const PRICES = {
        "veo-3.1-generate-001":{"720p":[0.20,0.40],"1080p":[0.20,0.40],"4k":[0.40,0.60]},
        "veo-3.1-fast-generate-001":{"720p":[0.08,0.10],"1080p":[0.10,0.12],"4k":[0.25,0.30]},
        "veo-3.1-lite-generate-001":{"720p":[0.03,0.05],"1080p":[0.05,0.08]},
      };
      const p = PRICES[state.model]?.[state.resolution];
      if (!p) { lbl.textContent = ""; return; }
      const usd = (audio ? p[1] : p[0]) * dur;
      const credits = Math.max(1, Math.ceil((usd / 3.0) / (1.40/30)));
      lbl.innerHTML = `Стоимость: <b>~${credits} кредитов</b>`;
    }
  }

  function autosizeText() {
    const t = $("textInput");
    t.style.height = "auto";
    t.style.height = Math.min(200, t.scrollHeight) + "px";
  }

  function addFiles(fileList) {
    for (const f of fileList) {
      if (state.pendingFiles.length >= 4) break;
      if (f.size > 12*1024*1024) { alert("Файл больше 12 МБ"); continue; }
      const reader = new FileReader();
      reader.onload = e => {
        state.pendingFiles.push({name: f.name, type: f.type, size: f.size, blob: f, dataUrl: e.target.result});
        renderPending();
      };
      reader.readAsDataURL(f);
    }
    $("fileInput").value = "";
  }
  function renderPending() {
    const wrap = $("pendingFiles");
    if (!state.pendingFiles.length) { wrap.style.display = "none"; wrap.innerHTML = ""; return; }
    wrap.style.display = "flex";
    wrap.innerHTML = state.pendingFiles.map((f,i) => `
      <div class="pf">
        ${f.type.startsWith("image/") ? `<img src="${f.dataUrl}">` : ""}
        <span class="pf-name">${escapeHtml(f.name)}</span>
        <button class="pf-x" data-i="${i}" title="Убрать">×</button>
      </div>`).join("");
    wrap.querySelectorAll(".pf-x").forEach(b => {
      b.addEventListener("click", () => {
        state.pendingFiles.splice(+b.dataset.i, 1);
        renderPending();
      });
    });
  }

  // ── Send ─────────────────────────────────────────────────
  async function ensureChat() {
    if (state.currentChatId) return state.currentChatId;
    const r = await fetch("/chat/api/chats", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({title:"Новый чат"}),
    });
    const j = await r.json();
    if (j.error) { alert(j.error); return null; }
    await loadChats();
    state.currentChatId = j.id;
    state.messages = [];
    renderMessages();
    return j.id;
  }

  async function sendMessage() {
    if (state.sending) return;
    const text = $("textInput").value.trim();
    if (!text && !state.pendingFiles.length) return;
    const cid = await ensureChat();
    if (!cid) return;
    state.sending = true;
    $("sendBtn").disabled = true;

    const optimisticUser = {
      id: "tmp_u_" + Date.now(),
      role: "user", mode: state.mode,
      content_text: text, model: state.model,
      file_kind: state.pendingFiles.find(f => f.type.startsWith("image/")) ? "image" : "",
      _localImage: state.pendingFiles.find(f => f.type.startsWith("image/"))?.dataUrl,
    };
    state.messages.push(optimisticUser);
    const assistantPlaceholder = {
      id: "tmp_a_" + Date.now(),
      role: "assistant", mode: state.mode,
      _pending: true, _pendingLabel: state.mode === "chat" ? "Печатает…" : "Генерация…",
      _pendingPct: 5,
    };
    state.messages.push(assistantPlaceholder);
    renderMessages();

    const fd = new FormData();
    const payload = {
      mode: state.mode, text, model: state.model,
      aspect_ratio: state.aspect, duration: state.duration,
      resolution: state.resolution, audio: state.audio,
      search: state.search,
    };
    fd.append("payload", JSON.stringify(payload));
    state.pendingFiles.forEach(f => fd.append("files", f.blob, f.name));

    try {
      const r = await fetch(`/chat/api/chats/${cid}/send`, {method:"POST", body: fd});
      const j = await r.json();
      if (!r.ok || j.error) {
        assistantPlaceholder._pending = false;
        assistantPlaceholder._error = j.error || "Ошибка отправки";
        renderMessages();
        return;
      }
      $("textInput").value = "";
      state.pendingFiles = [];
      renderPending();
      autosizeText();
      if (j.assistant) {
        // Synchronous result (chat / image)
        Object.assign(assistantPlaceholder, j.assistant, {_pending:false});
        renderMessages();
        await refreshMe();
      } else if (j.gen_id) {
        // Long-running generation: poll status
        await pollGen(j.gen_id, assistantPlaceholder);
      }
    } catch (e) {
      assistantPlaceholder._pending = false;
      assistantPlaceholder._error = "Сеть недоступна";
      renderMessages();
    } finally {
      state.sending = false;
      $("sendBtn").disabled = false;
    }
  }

  async function pollGen(gid, placeholder) {
    let tries = 0;
    while (tries < 600) {  // up to ~10 min @1s
      await new Promise(r => setTimeout(r, 1500));
      tries++;
      try {
        const r = await fetch(`/chat/api/gen/${gid}/status`);
        if (!r.ok) { placeholder._pending = false; placeholder._error = "Ошибка статуса"; renderMessages(); return; }
        const j = await r.json();
        if (j.status === "done" && j.assistant) {
          Object.assign(placeholder, j.assistant, {_pending:false});
          renderMessages();
          await refreshMe();
          return;
        }
        if (j.status === "error") {
          placeholder._pending = false;
          placeholder._error = j.error || "Ошибка генерации";
          renderMessages();
          return;
        }
        placeholder._pendingLabel = j.label || "Генерация…";
        placeholder._pendingPct = j.pct || 0;
        renderMessages();
      } catch (e) {
        // transient — keep trying
      }
    }
    placeholder._pending = false;
    placeholder._error = "Таймаут ожидания";
    renderMessages();
  }

  async function logout() {
    await fetch("/chat/api/logout", {method:"POST"});
    location.reload();
  }

  // ── Boot ─────────────────────────────────────────────────
  (async () => {
    try {
      const r = await fetch("/chat/api/me");
      if (r.ok) { await bootApp(); }
    } catch {}
  })();
})();
</script>
</body>
</html>
"""


async def handle_root(request: web.Request) -> web.Response:
    return web.Response(text=_shell_html(), content_type="text/html")


# ─── Route registration ────────────────────────────────────────────────────

def register_chat_routes(app: web.Application) -> None:
    app.router.add_get("/chat", handle_root)
    app.router.add_get("/chat/", handle_root)
    app.router.add_post("/chat/api/login/request", handle_login_request)
    app.router.add_post("/chat/api/login/verify", handle_login_verify)
    app.router.add_post("/chat/api/logout", handle_logout)
    app.router.add_get("/chat/api/me", handle_me)
    app.router.add_get("/chat/api/catalog", handle_catalog)
    app.router.add_get("/chat/api/chats", handle_chats_list)
    app.router.add_post("/chat/api/chats", handle_chats_create)
    app.router.add_patch("/chat/api/chats/{cid}", handle_chats_patch)
    app.router.add_delete("/chat/api/chats/{cid}", handle_chats_delete)
    app.router.add_get("/chat/api/chats/{cid}/messages", handle_messages_list)
    app.router.add_post("/chat/api/chats/{cid}/send", handle_send)
    app.router.add_get("/chat/api/gen/{gen_id}/status", handle_gen_status)
    app.router.add_get("/chat/api/media/{mid}", handle_media)
    logger.info("Web chat routes registered at /chat")
