import hashlib
import hmac
import json
import logging
import os
import time

import aiohttp

import bot.db as _db

logger = logging.getLogger(__name__)

LAVA_SHOP_ID = os.getenv("LAVA_SHOP_ID", "")
LAVA_SECRET_KEY = os.getenv("LAVA_SECRET_KEY", "")
LAVA_SECRET_KEY_2 = os.getenv("LAVA_SECRET_KEY_2", "")

BASE_URL = (
    os.getenv("BASE_URL", "")
    or f"https://{os.getenv('REPLIT_DEV_DOMAIN', '')}"
).rstrip("/")

CREDIT_PACKAGES = {
    "pack_3":   {"credits": 3,   "amount": 10.00,  "label": "3 кредита — 10₽"},
    "pack_30":  {"credits": 30,  "amount": 99.00,  "label": "30 кредитов — 99₽"},
    "pack_100": {"credits": 100, "amount": 299.00, "label": "100 кредитов — 299₽"},
    "pack_200": {"credits": 200, "amount": 549.00, "label": "200 кредитов — 549₽"},
}

_LAVA_API_URL = "https://api.lava.ru/business/invoice/create"


def _sign(json_str: str) -> str:
    return hmac.new(
        LAVA_SECRET_KEY.encode(),
        json_str.encode(),
        hashlib.sha256,
    ).hexdigest()


def verify_webhook_sign(invoice_id: str, amount: str, pay_time: str, received_sign: str) -> bool:
    raw = f"{invoice_id}:{amount}:{pay_time}:{LAVA_SECRET_KEY_2}"
    expected = hashlib.md5(raw.encode()).hexdigest()
    return hmac.compare_digest(expected, received_sign)


async def create_payment_url(user_id: int, pack_key: str, source: str = "tg") -> dict:
    """
    source: "tg" — Telegram bot, "vk" — VK bot.
    Stored in customFields so the webhook knows where to send the notification.
    """
    pack = CREDIT_PACKAGES.get(pack_key)
    if not pack:
        return {"ok": False, "error": "Неизвестный пакет"}

    if not LAVA_SHOP_ID or not LAVA_SECRET_KEY:
        return {"ok": False, "error": "Платёжная система не настроена"}

    order_id = f"{user_id}_{pack_key}_{int(time.time())}"

    payload: dict = {"shopId": LAVA_SHOP_ID, "sum": pack["amount"], "orderId": order_id}
    if BASE_URL:
        payload["hookUrl"] = f"{BASE_URL}/webhook/lava"
        payload["successUrl"] = f"{BASE_URL}/payment/success?src={source}"
        payload["failUrl"] = f"{BASE_URL}/payment/fail?src={source}"
    payload["comment"] = pack["label"]
    payload["customFields"] = source
    payload["expire"] = 60

    json_str = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    signature = _sign(json_str)

    _db.save_payment(order_id, user_id, pack_key, pack["amount"])

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _LAVA_API_URL,
                data=json_str.encode(),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Signature": signature,
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                resp_data = await resp.json(content_type=None)
    except Exception as exc:
        logger.error("Lava create invoice error: %s", exc)
        return {"ok": False, "error": "Ошибка соединения с платёжной системой"}

    if resp.status != 200 or "data" not in resp_data:
        logger.error("Lava API error: status=%s body=%s", resp.status, resp_data)
        return {"ok": False, "error": resp_data.get("message", "Ошибка платёжной системы")}

    pay_url = resp_data["data"].get("url", "")
    logger.info("Lava invoice created: order=%s user=%s pack=%s source=%s url=%s",
                order_id, user_id, pack_key, source, pay_url)

    return {"ok": True, "pay_url": pay_url, "order_id": order_id}
