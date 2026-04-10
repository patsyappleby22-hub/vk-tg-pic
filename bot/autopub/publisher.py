"""
bot/autopub/publisher.py
~~~~~~~~~~~~~~~~~~~~~~~~
Publishes approved autopub posts to Telegram channels and/or VK groups.
"""
from __future__ import annotations

import logging
import os

import aiohttp

logger = logging.getLogger(__name__)

_TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_VK_TOKEN = os.getenv("VK_BOT_TOKEN", "")


async def _tg_download_file(file_id: str) -> bytes | None:
    """Download a file from Telegram by file_id."""
    if not _TG_TOKEN:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            gf_url = f"https://api.telegram.org/bot{_TG_TOKEN}/getFile"
            async with session.get(gf_url, params={"file_id": file_id},
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                gf = await resp.json(content_type=None)
            if not gf.get("ok"):
                return None
            file_path = gf["result"]["file_path"]
            dl_url = f"https://api.telegram.org/file/bot{_TG_TOKEN}/{file_path}"
            async with session.get(dl_url, timeout=aiohttp.ClientTimeout(total=30)) as dl:
                return await dl.read()
    except Exception as exc:
        logger.error("autopub publisher: TG download failed: %s", exc)
        return None


async def publish_to_telegram(
    channel_id: str,
    file_id: str,
    caption: str,
) -> int | None:
    """
    Send photo to Telegram channel using file_id.
    Returns message_id or None on error.
    """
    if not _TG_TOKEN or not channel_id:
        return None
    try:
        url = f"https://api.telegram.org/bot{_TG_TOKEN}/sendPhoto"
        payload = {
            "chat_id": channel_id,
            "photo": file_id,
            "caption": caption,
            "parse_mode": "HTML",
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload,
                                    timeout=aiohttp.ClientTimeout(total=20)) as resp:
                body = await resp.json(content_type=None)

        if body.get("ok"):
            return body["result"]["message_id"]
        logger.error("autopub TG publish failed: %s", body.get("description", body))
        return None
    except Exception as exc:
        logger.error("autopub TG publish exception: %s", exc)
        return None


async def _vk_get_wall_upload_url(group_id: str) -> str | None:
    """Get VK wall photo upload URL."""
    if not _VK_TOKEN:
        return None
    try:
        params = {
            "access_token": _VK_TOKEN,
            "v": "5.199",
            "group_id": group_id.lstrip("-"),
        }
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.vk.com/method/photos.getWallUploadServer",
                                   params=params,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                body = await resp.json(content_type=None)
        if "response" in body:
            return body["response"]["upload_url"]
        logger.error("autopub VK get upload URL failed: %s", body)
    except Exception as exc:
        logger.error("autopub VK get upload URL exception: %s", exc)
    return None


async def _vk_save_wall_photo(group_id: str, server: int, photo: str, photo_hash: str) -> str | None:
    """Save uploaded photo and return attachment string."""
    if not _VK_TOKEN:
        return None
    try:
        params = {
            "access_token": _VK_TOKEN,
            "v": "5.199",
            "group_id": group_id.lstrip("-"),
            "server": server,
            "photo": photo,
            "hash": photo_hash,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post("https://api.vk.com/method/photos.saveWallPhoto",
                                    params=params,
                                    timeout=aiohttp.ClientTimeout(total=15)) as resp:
                body = await resp.json(content_type=None)
        if "response" in body and body["response"]:
            p = body["response"][0]
            access = f"_{p['access_key']}" if p.get("access_key") else ""
            return f"photo{p['owner_id']}_{p['id']}{access}"
        logger.error("autopub VK save photo failed: %s", body)
    except Exception as exc:
        logger.error("autopub VK save photo exception: %s", exc)
    return None


async def publish_to_vk(
    group_id: str,
    file_id: str,
    caption: str,
) -> int | None:
    """
    Download image from Telegram, upload to VK wall, create wall post.
    Returns post_id or None on error.
    """
    if not _VK_TOKEN or not group_id:
        return None

    # Strip HTML tags for VK (VK uses its own markup, not HTML)
    import re
    vk_caption = re.sub(r"<[^>]+>", "", caption)
    # Convert <code> blocks back to readable text (already stripped above)

    # 1. Download image from Telegram
    image_bytes = await _tg_download_file(file_id)
    if not image_bytes:
        logger.error("autopub VK: failed to download image from TG")
        return None

    # 2. Get wall upload URL
    upload_url = await _vk_get_wall_upload_url(group_id)
    if not upload_url:
        return None

    # 3. Upload photo to VK
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode not in ("RGB",):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        jpg_bytes = buf.getvalue()
    except Exception:
        jpg_bytes = image_bytes

    try:
        form = aiohttp.FormData()
        form.add_field("photo", jpg_bytes, filename="post.jpg", content_type="image/jpeg")
        async with aiohttp.ClientSession() as session:
            async with session.post(upload_url, data=form,
                                    timeout=aiohttp.ClientTimeout(total=30)) as resp:
                upload_result = await resp.json(content_type=None)
    except Exception as exc:
        logger.error("autopub VK upload failed: %s", exc)
        return None

    # 4. Save photo
    attachment = await _vk_save_wall_photo(
        group_id,
        server=upload_result.get("server", 0),
        photo=upload_result.get("photo", ""),
        photo_hash=upload_result.get("hash", ""),
    )
    if not attachment:
        return None

    # 5. Create wall post
    try:
        gid = group_id.lstrip("-")
        params = {
            "access_token": _VK_TOKEN,
            "v": "5.199",
            "owner_id": f"-{gid}",
            "message": vk_caption,
            "attachments": attachment,
            "from_group": 1,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post("https://api.vk.com/method/wall.post",
                                    params=params,
                                    timeout=aiohttp.ClientTimeout(total=15)) as resp:
                body = await resp.json(content_type=None)
        if "response" in body:
            return body["response"]["post_id"]
        logger.error("autopub VK wall.post failed: %s", body)
    except Exception as exc:
        logger.error("autopub VK wall.post exception: %s", exc)
    return None
