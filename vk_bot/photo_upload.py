from __future__ import annotations

import asyncio
import io
import json
import logging
from typing import Any

import aiohttp
from PIL import Image

logger = logging.getLogger(__name__)

MAX_VK_SIDE = 2560
MAX_RETRIES = 3


def _prepare_image_for_vk(image_bytes: bytes) -> tuple[bytes, str, str]:
    img = Image.open(io.BytesIO(image_bytes))

    if img.mode == "RGBA":
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[3])
        img = background
    elif img.mode != "RGB":
        img = img.convert("RGB")

    w, h = img.size
    if max(w, h) > MAX_VK_SIDE:
        scale = MAX_VK_SIDE / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    jpg_bytes = buf.getvalue()
    logger.info("Prepared image for VK: %dx%d -> %d bytes JPEG", w, h, len(jpg_bytes))
    return jpg_bytes, "image.jpg", "image/jpeg"


async def upload_photo_to_vk(api: Any, peer_id: int, image_bytes: bytes) -> str:
    jpg_bytes, filename, content_type = _prepare_image_for_vk(image_bytes)

    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            upload_server = await api.photos.get_messages_upload_server(peer_id=peer_id)
            upload_url = upload_server.upload_url
            logger.info("VK upload URL obtained (attempt %d), uploading %d bytes...", attempt + 1, len(jpg_bytes))

            form = aiohttp.FormData()
            form.add_field(
                "photo",
                io.BytesIO(jpg_bytes),
                filename=filename,
                content_type=content_type,
            )

            async with aiohttp.ClientSession() as session:
                async with session.post(upload_url, data=form) as resp:
                    raw_text = await resp.text()
                    logger.info("VK upload raw response (attempt %d): status=%d, body=%s", attempt + 1, resp.status, raw_text[:500])
                    result = json.loads(raw_text)

            photo_field = result.get("photo", "")
            if not photo_field or photo_field == "[]":
                raise ValueError(f"VK upload returned empty photo field: {result}")

            saved = await api.photos.save_messages_photo(
                photo=result["photo"],
                server=result["server"],
                hash=result["hash"],
            )

            photo = saved[0]
            access = f"_{photo.access_key}" if photo.access_key else ""
            attachment = f"photo{photo.owner_id}_{photo.id}{access}"
            logger.info("VK photo saved: %s", attachment)
            return attachment

        except Exception as exc:
            last_err = exc
            logger.warning("VK photo upload attempt %d failed: %s", attempt + 1, exc)
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(2)

    raise last_err


async def download_vk_photo(api: Any, photo_sizes: list) -> bytes:
    best = max(photo_sizes, key=lambda s: s.width * s.height)
    url = best.url

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.read()
