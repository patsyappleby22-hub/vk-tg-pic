"""
bot/middlewares/album_middleware.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Middleware that collects Telegram media-group messages into a single batch.

Telegram sends each photo in a media group as a separate update. This middleware
buffers them by `media_group_id` and passes all collected messages to the handler
once the group is complete (after a short delay).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message

logger = logging.getLogger(__name__)

ALBUM_COLLECT_DELAY = 0.5


class AlbumMiddleware(BaseMiddleware):

    def __init__(self) -> None:
        super().__init__()
        self._albums: dict[str, list[Message]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        if not event.media_group_id:
            return await handler(event, data)

        mg_id = event.media_group_id

        if mg_id not in self._locks:
            self._locks[mg_id] = asyncio.Lock()

        is_first = mg_id not in self._albums
        if is_first:
            self._albums[mg_id] = []

        self._albums[mg_id].append(event)

        if not is_first:
            return

        await asyncio.sleep(ALBUM_COLLECT_DELAY)

        album = self._albums.pop(mg_id, [])
        self._locks.pop(mg_id, None)

        album.sort(key=lambda m: m.message_id)

        data["album"] = album
        return await handler(event, data)
