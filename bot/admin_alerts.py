"""
bot/admin_alerts.py
~~~~~~~~~~~~~~~~~~~
Send critical alerts to admin via Telegram when API keys fail.

Alert types:
  - ALL_KEYS_AUTH_ERROR: every key has an authentication error (invalid/revoked)
  - ALL_KEYS_QUOTA: all keys hit rate limits, no free capacity for users
  - KEYS_NEED_ATTENTION: some keys have auth errors, reducing total capacity
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

ADMIN_TG_ID = 6014789391
_TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

_last_alert_times: dict[str, float] = {}
_ALERT_COOLDOWN = 300  # 5 min between same alert type


async def _send_tg_alert(text: str) -> bool:
    if not _TG_TOKEN:
        logger.warning("admin_alerts: TELEGRAM_BOT_TOKEN not set")
        return False
    try:
        url = f"https://api.telegram.org/bot{_TG_TOKEN}/sendMessage"
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json={
                    "chat_id": ADMIN_TG_ID,
                    "text": text,
                    "parse_mode": "HTML",
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status == 200 and body.get("ok"):
                    logger.info("admin_alerts: alert sent to admin")
                    return True
                logger.warning("admin_alerts: send failed: %s", str(body)[:200])
                return False
    except Exception as exc:
        logger.warning("admin_alerts: failed to send: %s", exc)
        return False


def _should_send(alert_type: str) -> bool:
    now = time.time()
    last = _last_alert_times.get(alert_type, 0)
    if now - last < _ALERT_COOLDOWN:
        return False
    _last_alert_times[alert_type] = now
    return True


async def alert_all_keys_auth_error(
    total_keys: int,
    error_details: list[str] | None = None,
) -> None:
    if not _should_send("ALL_KEYS_AUTH_ERROR"):
        return

    details = ""
    if error_details:
        details = "\n".join(f"  • {d}" for d in error_details[:10])
        details = f"\n\n<b>Ошибки:</b>\n{details}"

    text = (
        "🚨 <b>КРИТИЧНО: Все API ключи не работают!</b>\n\n"
        f"Из {total_keys} ключ(ей) — у всех ошибка авторизации.\n"
        "Бот не может обрабатывать запросы пользователей."
        f"{details}\n\n"
        "⚡ <b>Что делать:</b>\n"
        "1. Откройте /admin → Ключи API\n"
        "2. Проверьте ключи в Google Cloud Console\n"
        "3. Добавьте новые ключи или исправьте существующие"
    )
    await _send_tg_alert(text)


async def alert_all_keys_quota(
    total_keys: int,
    model: str = "",
) -> None:
    if not _should_send("ALL_KEYS_QUOTA"):
        return

    text = (
        "⚠️ <b>Все ключи перегружены!</b>\n\n"
        f"Все {total_keys} ключ(ей) достигли лимита запросов"
        f"{f' для модели <code>{model}</code>' if model else ''}.\n"
        "Пользователи ждут в очереди.\n\n"
        "⚡ <b>Что делать:</b>\n"
        "1. Откройте /admin → Ключи API\n"
        "2. Добавьте больше API ключей для увеличения пропускной способности\n"
        "3. Или дождитесь сброса лимитов (~60 сек)"
    )
    await _send_tg_alert(text)


async def alert_keys_degraded(
    total_keys: int,
    auth_error_count: int,
    error_details: list[str] | None = None,
) -> None:
    if not _should_send("KEYS_DEGRADED"):
        return

    working = total_keys - auth_error_count
    details = ""
    if error_details:
        details = "\n".join(f"  • {d}" for d in error_details[:5])
        details = f"\n\n<b>Сломанные ключи:</b>\n{details}"

    text = (
        "⚠️ <b>Часть API ключей не работает</b>\n\n"
        f"Работает: {working} из {total_keys} ключей.\n"
        f"С ошибкой авторизации: {auth_error_count}"
        f"{details}\n\n"
        "Бот работает, но с пониженной пропускной способностью.\n"
        "Откройте /admin → Ключи API для проверки."
    )
    await _send_tg_alert(text)
