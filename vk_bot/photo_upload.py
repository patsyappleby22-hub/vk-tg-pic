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
MAX_DOC_SIDE = 3840  # max side for document uploads (4K)
MAX_DOC_BYTES = 4 * 1024 * 1024  # 4 MB target — reduce if exceeded
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


async def upload_document_to_vk(api: Any, peer_id: int, image_bytes: bytes, filename: str = "image.png") -> str:
    """Upload image as a document (no compression, full quality PNG)."""
    # Ensure it's a valid PNG
    img = Image.open(io.BytesIO(image_bytes))

    # Convert to RGB if needed (PNG supports RGBA but keep it)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")

    # Limit max side to MAX_DOC_SIDE
    w, h = img.size
    if max(w, h) > MAX_DOC_SIDE:
        scale = MAX_DOC_SIDE / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    # Save with maximum PNG compression to reduce file size
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True, compress_level=9)
    png_bytes = buf.getvalue()

    # If still too large, downscale progressively until under MAX_DOC_BYTES
    scale_factor = 0.8
    while len(png_bytes) > MAX_DOC_BYTES and max(img.size) > 800:
        w, h = img.size
        img = img.resize((int(w * scale_factor), int(h * scale_factor)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True, compress_level=9)
        png_bytes = buf.getvalue()

    logger.info("Prepared document for VK: %dx%d -> %d bytes PNG", img.size[0], img.size[1], len(png_bytes))

    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            upload_server = await api.docs.get_messages_upload_server(type="doc", peer_id=peer_id)
            upload_url = upload_server.upload_url
            logger.info("VK doc upload URL obtained (attempt %d), uploading %d bytes...", attempt + 1, len(png_bytes))

            form = aiohttp.FormData()
            form.add_field(
                "file",
                io.BytesIO(png_bytes),
                filename=filename,
                content_type="image/png",
            )

            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(upload_url, data=form) as resp:
                    status = resp.status
                    raw_text = await resp.text()
                    logger.info("VK doc upload response (attempt %d): status=%d, body=%s", attempt + 1, status, raw_text[:300])

                    if status == 405:
                        # VK returned a stale/invalid upload URL — retry immediately with a fresh one
                        raise ValueError(f"VK returned 405 (stale upload URL), retrying...")
                    if status != 200:
                        raise ValueError(f"VK upload returned HTTP {status}")

                    result = json.loads(raw_text)

            file_field = result.get("file", "")
            if not file_field:
                raise ValueError(f"VK doc upload returned empty file field: {result}")

            saved = await api.docs.save(file=file_field, title=filename)
            doc = saved.doc
            attachment = f"doc{doc.owner_id}_{doc.id}"
            logger.info("VK document saved: %s", attachment)
            return attachment

        except Exception as exc:
            last_err = exc
            is_stale_url = "405" in str(exc) or "stale upload URL" in str(exc)
            logger.warning("VK doc upload attempt %d failed: %s", attempt + 1, exc)
            if attempt < MAX_RETRIES - 1:
                # No sleep for stale URL (405) — just get a new URL immediately
                if not is_stale_url:
                    await asyncio.sleep(2)

    raise last_err


async def download_vk_photo(api: Any, photo_sizes: list) -> bytes:
    best = max(photo_sizes, key=lambda s: s.width * s.height)
    url = best.url

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.read()
