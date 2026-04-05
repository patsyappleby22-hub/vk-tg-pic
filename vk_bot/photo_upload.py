from __future__ import annotations

import asyncio
import io
import json
import logging
from typing import Any

import aiohttp
from PIL import Image

logger = logging.getLogger(__name__)

MAX_VK_SIDE = 2560          # max side for photo mode
MAX_DOC_SIDE = 3840         # max side for document mode (4K cap)
MAX_RETRIES = 5
_504_RETRY_DELAY = 10       # seconds before retry on VK server-side 504

# key insight from VK upload research:
# sock_read=None prevents the timeout from firing while we are still
# sending data — without it aiohttp starts the read-timeout clock at
# the very beginning of the POST, so a large file upload fails before
# VK even gets a chance to respond.
_UPLOAD_TIMEOUT = aiohttp.ClientTimeout(
    total=None,
    connect=15,
    sock_connect=15,
    sock_read=None,
)


def _detect_format(image_bytes: bytes) -> tuple[str, str]:
    """Return (filename, content_type) based on file magic bytes."""
    if image_bytes[:4] == b"\x89PNG":
        return "image.png", "image/png"
    if image_bytes[:2] == b"\xff\xd8":
        return "image.jpg", "image/jpeg"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image.webp", "image/webp"
    return "image.png", "image/png"


def _prepare_image_for_vk(image_bytes: bytes) -> tuple[bytes, str, str]:
    """Resize + convert to JPEG for photo-mode upload (max 2560px)."""
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
    logger.info("Prepared image for VK photo: %dx%d -> %d bytes JPEG", w, h, len(jpg_bytes))
    return jpg_bytes, "image.jpg", "image/jpeg"


def _prepare_document_for_vk(image_bytes: bytes) -> tuple[bytes, str, str]:
    """Convert to JPEG for document-mode upload.

    Raw 4K PNG can be 8-10 MB which reliably triggers VK 504.
    JPEG at quality 93 is visually identical but ~1.5-2.5 MB — well
    within what VK's upload servers handle without timeout.
    """
    img = Image.open(io.BytesIO(image_bytes))
    original_size = len(image_bytes)

    if img.mode == "RGBA":
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[3])
        img = background
    elif img.mode != "RGB":
        img = img.convert("RGB")

    w, h = img.size
    if max(w, h) > MAX_DOC_SIDE:
        scale = MAX_DOC_SIDE / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=93)
    jpg_bytes = buf.getvalue()
    logger.info(
        "Prepared document for VK: %dx%d PNG %d bytes -> JPEG %d bytes (%.0f%% of original)",
        w, h, original_size, len(jpg_bytes), len(jpg_bytes) / original_size * 100,
    )
    return jpg_bytes, "image.jpg", "image/jpeg"


def _make_connector() -> aiohttp.TCPConnector:
    """Fresh connector per upload — avoids stale keep-alive 504s."""
    return aiohttp.TCPConnector(
        keepalive_timeout=30,
        enable_cleanup_closed=True,
        limit=1,
    )


async def upload_photo_to_vk(api: Any, peer_id: int, image_bytes: bytes) -> str:
    loop = asyncio.get_running_loop()
    jpg_bytes, filename, content_type = await loop.run_in_executor(
        None, _prepare_image_for_vk, image_bytes
    )

    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            upload_server = await api.photos.get_messages_upload_server(peer_id=peer_id)
            upload_url = upload_server.upload_url
            logger.info(
                "VK upload URL obtained (attempt %d), uploading %d bytes...",
                attempt + 1, len(jpg_bytes),
            )

            form = aiohttp.FormData()
            form.add_field("photo", io.BytesIO(jpg_bytes), filename=filename, content_type=content_type)

            async with aiohttp.ClientSession(
                connector=_make_connector(), timeout=_UPLOAD_TIMEOUT,
            ) as session:
                async with session.post(upload_url, data=form) as resp:
                    raw_text = await resp.text()
                    logger.info(
                        "VK upload raw response (attempt %d): status=%d, body=%s",
                        attempt + 1, resp.status, raw_text[:500],
                    )
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
            logger.warning("VK photo upload attempt %d/%d failed: %s", attempt + 1, MAX_RETRIES, exc)
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(2)

    raise last_err


async def upload_document_to_vk(api: Any, peer_id: int, image_bytes: bytes, filename: str | None = None) -> str:
    """Upload image as a high-quality JPEG document."""
    loop = asyncio.get_running_loop()
    jpg_bytes, auto_fname, content_type = await loop.run_in_executor(
        None, _prepare_document_for_vk, image_bytes
    )
    fname = filename or auto_fname

    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            upload_server = await api.docs.get_messages_upload_server(type="doc", peer_id=peer_id)
            upload_url = upload_server.upload_url
            logger.info(
                "VK doc upload URL obtained (attempt %d), uploading %d bytes...",
                attempt + 1, len(jpg_bytes),
            )

            form = aiohttp.FormData()
            form.add_field("file", io.BytesIO(jpg_bytes), filename=fname, content_type=content_type)

            async with aiohttp.ClientSession(
                connector=_make_connector(), timeout=_UPLOAD_TIMEOUT,
            ) as session:
                async with session.post(upload_url, data=form) as resp:
                    status = resp.status
                    raw_text = await resp.text()
                    logger.info(
                        "VK doc upload response (attempt %d): status=%d, body=%s",
                        attempt + 1, status, raw_text[:300],
                    )

                    if status == 405:
                        raise ValueError("VK returned 405 (stale upload URL), retrying...")
                    if status != 200:
                        raise ValueError(f"VK upload returned HTTP {status}")

                    result = json.loads(raw_text)

            file_field = result.get("file", "")
            if not file_field:
                raise ValueError(f"VK doc upload returned empty file field: {result}")

            saved = await api.docs.save(file=file_field, title=fname)
            doc = saved.doc
            attachment = f"doc{doc.owner_id}_{doc.id}"
            logger.info("VK document saved: %s", attachment)
            return attachment

        except Exception as exc:
            last_err = exc
            is_stale_url = "405" in str(exc) or "stale upload URL" in str(exc)
            is_gateway_timeout = "504" in str(exc)
            logger.warning(
                "VK doc upload attempt %d/%d failed: %s", attempt + 1, MAX_RETRIES, exc,
            )
            if attempt < MAX_RETRIES - 1:
                if is_gateway_timeout:
                    logger.info(
                        "VK 504 — waiting %ds before retry (server-side timeout)...",
                        _504_RETRY_DELAY,
                    )
                    await asyncio.sleep(_504_RETRY_DELAY)
                elif not is_stale_url:
                    await asyncio.sleep(2)

    raise last_err


async def download_vk_photo(api: Any, photo_sizes: list) -> bytes:
    best = max(photo_sizes, key=lambda s: s.width * s.height)
    url = best.url

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.read()
