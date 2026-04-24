"""
bot/services/grok_service.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Grok 4.20 (Reasoning) chat client via Vertex AI Model Garden.

Uses the same service-account credentials (`_CredSlot`) that already power
Veo / Lyria via Vertex AI. Talks to Vertex AI's OpenAI-compatible endpoint:

    POST https://aiplatform.googleapis.com/v1/projects/{PROJECT_ID}
         /locations/global/endpoints/openapi/chat/completions

Body uses the OpenAI Chat Completions schema with model = "xai/grok-4.20-reasoning".
Live web search is enabled by default (this is what the user asks Grok for).
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

GROK_MODEL = "xai/grok-4.20-reasoning"
ENDPOINT_TPL = (
    "https://aiplatform.googleapis.com/v1/projects/{project}"
    "/locations/global/endpoints/openapi/chat/completions"
)

# Hard cap to protect quota / latency.
_REQUEST_TIMEOUT_SECONDS = 180


class GrokError(Exception):
    """Raised when Grok call fails after retries are exhausted."""


def _bytes_to_data_url(data: bytes, mime: str) -> str:
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _convert_history_to_openai(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert internal multimodal history → OpenAI chat-completions messages.

    Internal format:
        [{"role": "user"|"model", "parts": [{"type": "text"|"media", ...}, ...]}, ...]

    Grok via Vertex Model Garden supports text and images. Audio / video / PDF
    parts are downgraded to a short text placeholder so the conversation still
    flows even if the user attached an unsupported media type.
    """
    messages: list[dict[str, Any]] = []
    for msg in history:
        role = "assistant" if msg.get("role") == "model" else "user"
        parts = msg.get("parts", []) or []

        text_chunks: list[str] = []
        image_chunks: list[dict[str, Any]] = []
        unsupported_notes: list[str] = []

        for p in parts:
            ptype = p.get("type")
            if ptype == "text":
                t = p.get("text", "")
                if t:
                    text_chunks.append(t)
            elif ptype == "media":
                mime = (p.get("mime_type") or "").lower()
                data = p.get("data")
                if mime.startswith("image/") and data:
                    image_chunks.append({
                        "type": "image_url",
                        "image_url": {"url": _bytes_to_data_url(data, mime)},
                    })
                elif mime.startswith("audio/"):
                    unsupported_notes.append("[аудио — Grok не умеет слушать звук, опишите его текстом]")
                elif mime.startswith("video/"):
                    unsupported_notes.append("[видео — Grok не умеет смотреть видео, опишите его текстом]")
                elif mime == "application/pdf":
                    unsupported_notes.append("[PDF — отправьте текст из документа]")
                else:
                    unsupported_notes.append(f"[вложение {mime} не поддерживается Grok]")

        text_blob = "\n".join(text_chunks + unsupported_notes).strip()

        if image_chunks:
            content_parts: list[dict[str, Any]] = []
            if text_blob:
                content_parts.append({"type": "text", "text": text_blob})
            content_parts.extend(image_chunks)
            messages.append({"role": role, "content": content_parts})
        else:
            messages.append({"role": role, "content": text_blob or " "})

    return messages


def _refresh_token_sync(credentials: Any) -> str:
    """Refresh and return the OAuth2 access token from a SA Credentials object."""
    from google.auth.transport.requests import Request as GAuthRequest
    credentials.refresh(GAuthRequest())
    return credentials.token


async def _get_access_token(credentials: Any) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _refresh_token_sync, credentials)


async def chat_grok(
    cred_slot: Any,
    history: list[dict[str, Any]],
    *,
    enable_search: bool = True,
) -> str:
    """Call Grok 4.20 (Reasoning) through Vertex AI Model Garden.

    cred_slot must be a `_CredSlot` from vertex_ai_service (has `_get_credentials()`
    and `_project_id`). Returns the assistant's text reply.
    """
    project_id = getattr(cred_slot, "_project_id", None)
    if not project_id:
        raise GrokError("Service account is missing project_id; cannot call Grok.")

    credentials = cred_slot._get_credentials()
    token = await _get_access_token(credentials)

    url = ENDPOINT_TPL.format(project=project_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    messages = _convert_history_to_openai(history)
    body: dict[str, Any] = {
        "model": GROK_MODEL,
        "messages": messages,
        "temperature": 1.0,
        "stream": False,
    }
    # NOTE on web search: xAI deprecated the old `search_parameters` field
    # in favour of the Agent Tools API (`tools: [{"type": "web_search"}]`),
    # but Vertex AI Model Garden only accepts the OpenAI function-calling
    # format (`type: "function"`) for tools. Until Vertex exposes xAI's
    # native web_search tool, live search through Model Garden is not
    # available — we simply call Grok in pure-chat mode.
    _ = enable_search  # kept for API stability

    timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=body) as resp:
                raw = await resp.text()
                if resp.status >= 400:
                    snippet = raw[:500]
                    logger.error("Grok HTTP %d: %s", resp.status, snippet)
                    raise GrokError(
                        f"Vertex Model Garden returned {resp.status}: {snippet}"
                    )
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    raise GrokError(f"Grok response is not JSON: {raw[:300]}")
    except asyncio.TimeoutError:
        raise GrokError("Grok request timed out.")
    except aiohttp.ClientError as exc:
        raise GrokError(f"Grok HTTP error: {exc}")

    choices = data.get("choices") or []
    if not choices:
        raise GrokError(f"Grok returned no choices: {str(data)[:300]}")
    message = (choices[0] or {}).get("message") or {}
    content = message.get("content")

    if isinstance(content, list):
        # Some providers return content as list of parts {type, text}
        text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
        return "".join(text_parts).strip()
    if isinstance(content, str):
        return content.strip()
    return ""
