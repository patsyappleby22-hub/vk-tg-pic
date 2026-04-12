"""
bot/autopub/publisher.py
~~~~~~~~~~~~~~~~~~~~~~~~
Publishes approved autopub posts to Telegram channels and/or VK groups.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

import aiohttp

logger = logging.getLogger(__name__)

_TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_VK_TOKEN = os.getenv("VK_BOT_TOKEN", "")
# For wall posting VK requires a USER token (not community/group token).
# Generate at: https://vkhost.github.io/ or via VK OAuth with scope: wall,photos,offline
# Then add it as env secret VK_USER_TOKEN.
_VK_USER_TOKEN = os.getenv("VK_USER_TOKEN", "")

_TG_CAPTION_LIMIT = 1024


def _strip_html(text: str) -> str:
    """Remove HTML tags and unescape common entities."""
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&quot;", '"')
    return text


def _clean_caption(caption: str, limit: int = _TG_CAPTION_LIMIT) -> str:
    """Strip HTML, collapse extra blank lines, trim to limit."""
    text = _strip_html(caption)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) <= limit:
        return text
    # Trim at last sentence boundary before limit
    cut = text[:limit - 1]
    last_nl = cut.rfind("\n")
    last_dot = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    pos = max(last_nl, last_dot)
    if pos > limit // 2:
        return text[: pos + 1].rstrip() + "…"
    return cut.rstrip() + "…"


async def _tg_download_file(file_id: str) -> bytes | None:
    """Download a file from Telegram by file_id."""
    if not _TG_TOKEN:
        logger.error("[autopub publisher] TELEGRAM_BOT_TOKEN не задан")
        return None
    try:
        async with aiohttp.ClientSession() as session:
            gf_url = f"https://api.telegram.org/bot{_TG_TOKEN}/getFile"
            async with session.get(gf_url, params={"file_id": file_id},
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                gf = await resp.json(content_type=None)
            if not gf.get("ok"):
                logger.error("[autopub publisher] getFile failed: %s", gf)
                return None
            file_path = gf["result"]["file_path"]
            dl_url = f"https://api.telegram.org/file/bot{_TG_TOKEN}/{file_path}"
            async with session.get(dl_url, timeout=aiohttp.ClientTimeout(total=30)) as dl:
                data = await dl.read()
        logger.debug("[autopub publisher] скачано %.1f KB", len(data) / 1024)
        return data
    except Exception as exc:
        logger.error("[autopub publisher] ошибка скачивания из TG: %s", exc)
        return None


def _tg_title_from_caption(caption: str) -> str:
    """Extract the first non-empty line as the short photo caption (plain text)."""
    plain = _strip_html(caption)
    for line in plain.splitlines():
        line = line.strip()
        if line:
            return line[:200]
    return ""


async def _tg_api_post(session: aiohttp.ClientSession, method: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{_TG_TOKEN}/{method}"
    async with session.post(url, json=payload,
                            timeout=aiohttp.ClientTimeout(total=20)) as resp:
        return await resp.json(content_type=None)


async def publish_to_telegram(
    channel_id: str,
    file_id: str,
    caption: str,
    extra_file_ids: list[str] | None = None,
) -> int | None:
    """Send photo(s) to Telegram channel.

    If extra_file_ids are provided, sends a media group (multiple photos).
    Otherwise sends a single photo with HTML caption.
    Returns message_id or None on failure.
    """
    if not _TG_TOKEN:
        logger.error("[autopub TG] TELEGRAM_BOT_TOKEN не задан — публикация невозможна")
        return None
    if not channel_id:
        logger.error("[autopub TG] channel_id пустой — публикация невозможна")
        return None
    if not file_id:
        logger.error("[autopub TG] file_id пустой — публикация без фото запрещена")
        return None

    caption_text = caption.strip() if caption else ""
    all_file_ids = [file_id] + (extra_file_ids or [])
    t0 = time.monotonic()

    try:
        async with aiohttp.ClientSession() as session:
            if len(all_file_ids) >= 2:
                media = []
                for i, fid in enumerate(all_file_ids):
                    item: dict = {"type": "photo", "media": fid}
                    if i == 0 and caption_text:
                        item["caption"] = caption_text
                        item["parse_mode"] = "HTML"
                    media.append(item)
                payload = {"chat_id": channel_id, "media": media}
                logger.info("[autopub TG] sendMediaGroup → channel=%s  %d photos  caption=%d chars",
                            channel_id, len(media), len(caption_text))
                body = await _tg_api_post(session, "sendMediaGroup", payload)

                if body.get("ok"):
                    results = body["result"]
                    msg_id = results[0]["message_id"] if results else None
                    logger.info("[autopub TG] ✓ медиа-группа опубликована  %d фото  message_id=%s (%.1fs)",
                                len(results), msg_id, time.monotonic() - t0)
                    return msg_id
            else:
                payload = {
                    "chat_id": channel_id,
                    "photo": file_id,
                    "parse_mode": "HTML",
                }
                if caption_text:
                    payload["caption"] = caption_text
                logger.info("[autopub TG] sendPhoto → channel=%s  caption=%d chars",
                            channel_id, len(caption_text))
                body = await _tg_api_post(session, "sendPhoto", payload)

                if body.get("ok"):
                    msg_id = body["result"]["message_id"]
                    logger.info("[autopub TG] ✓ опубликовано  message_id=%s (%.1fs)",
                                msg_id, time.monotonic() - t0)
                    return msg_id

        err = body.get("description", str(body))
        logger.error("[autopub TG] ✗ ошибка от API: %s  (channel=%s)", err, channel_id)
        if "chat not found" in err.lower():
            logger.error("[autopub TG] → бот не добавлен в канал или ID неверный")
        elif "not enough rights" in err.lower() or "forbidden" in err.lower():
            logger.error("[autopub TG] → бот не является администратором канала")
        elif "can't parse" in err.lower():
            logger.error("[autopub TG] → ошибка HTML разметки в подписи")
        return None
    except Exception as exc:
        logger.error("[autopub TG] ✗ исключение при публикации: %s", exc)
        return None


def _vk_active_token() -> str:
    """Return the best available VK token: user token preferred over group token."""
    return _VK_USER_TOKEN or _VK_TOKEN


async def _vk_get_wall_upload_url(group_id: str) -> str | None:
    token = _vk_active_token()
    if not token:
        logger.error("[autopub VK] нет VK токена (VK_USER_TOKEN или VK_BOT_TOKEN)")
        return None
    if not _VK_USER_TOKEN and _VK_TOKEN:
        logger.warning("[autopub VK] ⚠ VK_USER_TOKEN не задан — используется группой токен. "
                       "Для публикации фото нужен токен пользователя-администратора. "
                       "Получить: https://vkhost.github.io/ (scope: wall,photos,offline)")
    try:
        data = {
            "access_token": token,
            "v": "5.199",
            "group_id": group_id.lstrip("-"),
        }
        async with aiohttp.ClientSession() as session:
            async with session.post("https://api.vk.com/method/photos.getWallUploadServer",
                                    data=data,
                                    timeout=aiohttp.ClientTimeout(total=15)) as resp:
                raw = await resp.text()
        body = json.loads(raw) if raw.strip() else {}
        if "response" in body:
            url = body["response"]["upload_url"]
            logger.debug("[autopub VK] upload_url получен")
            return url
        logger.error("[autopub VK] getWallUploadServer failed: %s", body)
        if "error" in body:
            code = body["error"].get("error_code")
            msg  = body["error"].get("error_msg", "")
            logger.error("[autopub VK] код ошибки=%s  сообщение=%s", code, msg)
            if code == 5:
                logger.error("[autopub VK] → токен недействителен или истёк")
            elif code == 15:
                logger.error("[autopub VK] → нет прав: включите 'Фотографии' в настройках токена")
    except Exception as exc:
        logger.error("[autopub VK] исключение getWallUploadServer: %s", exc)
    return None


async def _vk_save_wall_photo(group_id: str, server: int, photo: str, photo_hash: str) -> str | None:
    token = _vk_active_token()
    if not token:
        return None
    try:
        params = {
            "access_token": token,
            "v": "5.199",
            "group_id": group_id.lstrip("-"),
            "server": server,
            "photo": photo,
            "hash": photo_hash,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post("https://api.vk.com/method/photos.saveWallPhoto",
                                    data=params,
                                    timeout=aiohttp.ClientTimeout(total=15)) as resp:
                raw = await resp.text()
        body = json.loads(raw) if raw.strip() else {}
        if "response" in body and body["response"]:
            p = body["response"][0]
            access = f"_{p['access_key']}" if p.get("access_key") else ""
            att = f"photo{p['owner_id']}_{p['id']}{access}"
            logger.debug("[autopub VK] saveWallPhoto OK  attachment=%s", att)
            return att
        logger.error("[autopub VK] saveWallPhoto failed: %s", body)
    except Exception as exc:
        logger.error("[autopub VK] saveWallPhoto exception: %s", exc)
    return None


async def _vk_wall_post(group_id: str, message: str, attachment: str = "") -> int | None:
    """Post to VK community WALL (for traditional groups, not channels)."""
    token = _vk_active_token()
    if not token:
        logger.error("[autopub VK] нет VK токена для публикации")
        return None
    gid = group_id.lstrip("-")
    data: dict = {
        "access_token": token,
        "v": "5.199",
        "owner_id": f"-{gid}",
        "message": message,
        "from_group": "1",
    }
    if attachment:
        data["attachments"] = attachment
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post("https://api.vk.com/method/wall.post",
                                    data=data,
                                    timeout=aiohttp.ClientTimeout(total=20)) as resp:
                raw = await resp.text()
        body = json.loads(raw) if raw.strip() else {}
        if "response" in body:
            post_id = body["response"]["post_id"]
            logger.info("[autopub VK] ✓ wall.post опубликован  post_id=%s", post_id)
            return post_id
        logger.error("[autopub VK] wall.post failed: %s", body)
        if "error" in body:
            code = body["error"].get("error_code")
            msg  = body["error"].get("error_msg", "")
            logger.error("[autopub VK] код=%s  сообщение=%s", code, msg)
    except Exception as exc:
        logger.error("[autopub VK] wall.post exception: %s", exc)
    return None


async def _vk_get_msg_upload_url(peer_id: str) -> str | None:
    """Get photo upload server URL for VK messages/channels."""
    token = _VK_TOKEN or _vk_active_token()
    if not token:
        return None
    try:
        data = {"access_token": token, "v": "5.199", "peer_id": peer_id}
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.vk.com/method/photos.getMessagesUploadServer",
                data=data, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                raw = await resp.text()
        body = json.loads(raw) if raw.strip() else {}
        if "response" in body:
            return body["response"]["upload_url"]
        logger.error("[autopub VK] getMessagesUploadServer failed: %s", body)
    except Exception as exc:
        logger.error("[autopub VK] getMessagesUploadServer exception: %s", exc)
    return None


async def _vk_save_msg_photo(server: int, photo: str, photo_hash: str) -> str | None:
    """Save uploaded photo for VK message. Returns attachment string."""
    token = _VK_TOKEN or _vk_active_token()
    if not token:
        return None
    try:
        data = {
            "access_token": token, "v": "5.199",
            "server": server, "photo": photo, "hash": photo_hash,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.vk.com/method/photos.saveMessagesPhoto",
                data=data, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                raw = await resp.text()
        body = json.loads(raw) if raw.strip() else {}
        if "response" in body and body["response"]:
            p = body["response"][0]
            att = f"photo{p['owner_id']}_{p['id']}"
            logger.debug("[autopub VK] saveMessagesPhoto OK  attachment=%s", att)
            return att
        logger.error("[autopub VK] saveMessagesPhoto failed: %s", body)
    except Exception as exc:
        logger.error("[autopub VK] saveMessagesPhoto exception: %s", exc)
    return None


async def _vk_channel_send(peer_id: str, message: str, attachment: str = "") -> int | None:
    """Post to VK Channel using messages.send (for new VK Channels format)."""
    token = _VK_TOKEN or _vk_active_token()
    if not token:
        return None
    import random as _random
    data: dict = {
        "access_token": token,
        "v": "5.199",
        "peer_id": peer_id,
        "message": message,
        "random_id": _random.randint(0, 2**31),
    }
    if attachment:
        data["attachment"] = attachment
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post("https://api.vk.com/method/messages.send",
                                    data=data,
                                    timeout=aiohttp.ClientTimeout(total=20)) as resp:
                raw = await resp.text()
        body = json.loads(raw) if raw.strip() else {}
        if "response" in body:
            msg_id = body["response"]
            logger.info("[autopub VK] ✓ messages.send OK  msg_id=%s", msg_id)
            return msg_id
        logger.error("[autopub VK] messages.send failed: %s", body)
        if "error" in body:
            code = body["error"].get("error_code")
            msg_err = body["error"].get("error_msg", "")
            logger.error("[autopub VK] код=%s  сообщение=%s", code, msg_err)
            if code == 917:
                logger.error("[autopub VK] → не администратор канала / нет доступа к каналу")
            elif code == 7:
                logger.error("[autopub VK] → токен не имеет прав messages")
    except Exception as exc:
        logger.error("[autopub VK] messages.send exception: %s", exc)
    return None


def _vk_jpg_bytes(image_bytes: bytes) -> bytes:
    """Convert image to JPEG bytes using PIL, fall back to original."""
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        return buf.getvalue()
    except Exception as exc:
        logger.warning("[autopub VK] PIL конвертация не удалась (%s), использую оригинал", exc)
        return image_bytes


async def _vk_upload_photo(upload_url: str, jpg_bytes: bytes) -> dict:
    """Upload JPEG bytes to VK server. Returns upload result dict."""
    form = aiohttp.FormData()
    form.add_field("photo", jpg_bytes, filename="post.jpg", content_type="image/jpeg")
    async with aiohttp.ClientSession() as session:
        async with session.post(upload_url, data=form,
                                timeout=aiohttp.ClientTimeout(total=30)) as resp:
            raw = await resp.text()
    return json.loads(raw) if raw.strip() else {}


async def publish_to_vk(
    group_id: str,
    file_id: str,
    caption: str,
    extra_file_ids: list[str] | None = None,
) -> int | None:
    """Publish to VK Group wall (wall.post) with multiple photos.

    Strategy:
      1. Download all images from Telegram
      2. Upload all to VK wall photo server
      3. Post with all attachments
    """
    if not (_VK_TOKEN or _VK_USER_TOKEN):
        logger.error("[autopub VK] нет VK токена — публикация невозможна")
        return None
    if not group_id:
        logger.error("[autopub VK] group_id пустой — публикация невозможна")
        return None

    vk_text = _strip_html(caption)
    gid = group_id.lstrip("-")
    peer_id = f"-{gid}"

    all_tg_file_ids = [file_id] + (extra_file_ids or [])
    logger.info("[autopub VK] начинаю публикацию  group=%s  peer=%s  photos=%d", gid, peer_id, len(all_tg_file_ids))
    t0 = time.monotonic()

    # ── Step 1: Download all images from Telegram ────────────────────────────
    logger.info("[autopub VK] 1/3 скачиваю %d изображений из Telegram...", len(all_tg_file_ids))
    all_jpg: list[bytes] = []
    for i, fid in enumerate(all_tg_file_ids):
        image_bytes = await _tg_download_file(fid)
        if image_bytes:
            jpg = _vk_jpg_bytes(image_bytes)
            all_jpg.append(jpg)
            logger.info("[autopub VK] 1/3 фото %d/%d OK — %.1f KB", i + 1, len(all_tg_file_ids), len(jpg) / 1024)
        else:
            logger.warning("[autopub VK] 1/3 фото %d/%d FAILED — пропускаю", i + 1, len(all_tg_file_ids))
    if not all_jpg:
        logger.error("[autopub VK] 1/3 FAILED — не удалось скачать ни одно фото")
        return None

    # ── Step 2: Upload all photos to VK wall ─────────────────────────────────
    logger.info("[autopub VK] 2/3 загружаю %d фото на VK wall...", len(all_jpg))
    wall_upload_url = await _vk_get_wall_upload_url(gid)
    if not wall_upload_url:
        logger.error("[autopub VK] 2/3 FAILED — не удалось получить upload URL")
        return None

    attachments: list[str] = []
    for i, jpg in enumerate(all_jpg):
        try:
            upload_result = await _vk_upload_photo(wall_upload_url, jpg)
            att = await _vk_save_wall_photo(
                gid,
                server=upload_result.get("server", 0),
                photo=upload_result.get("photo", ""),
                photo_hash=upload_result.get("hash", ""),
            )
            if att:
                attachments.append(att)
                logger.info("[autopub VK] 2/3 фото %d/%d uploaded: %s", i + 1, len(all_jpg), att)
        except Exception as exc:
            logger.error("[autopub VK] 2/3 фото %d/%d upload failed: %s", i + 1, len(all_jpg), exc)

    if not attachments:
        logger.error("[autopub VK] все фото-методы не сработали — публикация без фото запрещена")
        return None

    # ── Step 3: Post with all attachments ────────────────────────────────────
    logger.info("[autopub VK] 3/3 wall.post  group=%s  attachments=%d...", gid, len(attachments))
    combined_attachments = ",".join(attachments)
    post_id = await _vk_wall_post(gid, vk_text, combined_attachments)
    if post_id:
        logger.info("[autopub VK] ✓ опубликовано %d фото в группу wall (%.1fs)", len(attachments), time.monotonic() - t0)
        return post_id

    logger.error("[autopub VK] wall.post failed")
    return None
