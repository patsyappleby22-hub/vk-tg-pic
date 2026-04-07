import hashlib
import logging
import os
import time

import bot.db as _db

logger = logging.getLogger(__name__)

FREEKASSA_SHOP_ID = os.getenv("FREEKASSA_SHOP_ID", "")
FREEKASSA_SECRET1 = os.getenv("FREEKASSA_SECRET1", "")
FREEKASSA_SECRET2 = os.getenv("FREEKASSA_SECRET2", "")

CREDIT_PACKAGES = {
    "pack_30": {"credits": 30, "amount": 99.00, "label": "30 кредитов — 99₽"},
    "pack_100": {"credits": 100, "amount": 299.00, "label": "100 кредитов — 299₽"},
    "pack_200": {"credits": 200, "amount": 549.00, "label": "200 кредитов — 549₽"},
}


def _make_payment_sign(shop_id: str, amount: str, secret: str, currency: str, order_id: str) -> str:
    raw = f"{shop_id}:{amount}:{secret}:{currency}:{order_id}"
    return hashlib.md5(raw.encode()).hexdigest()


def _make_notification_sign(shop_id: str, amount: str, secret2: str, order_id: str) -> str:
    raw = f"{shop_id}:{amount}:{secret2}:{order_id}"
    return hashlib.md5(raw.encode()).hexdigest()


def create_payment_url(user_id: int, pack_key: str) -> dict:
    pack = CREDIT_PACKAGES.get(pack_key)
    if not pack:
        return {"ok": False, "error": "Неизвестный пакет"}

    if not FREEKASSA_SHOP_ID or not FREEKASSA_SECRET1:
        return {"ok": False, "error": "Платёжная система не настроена"}

    order_id = f"{user_id}_{pack_key}_{int(time.time())}"
    amount = f"{pack['amount']:.2f}"
    currency = "RUB"

    sign = _make_payment_sign(FREEKASSA_SHOP_ID, amount, FREEKASSA_SECRET1, currency, order_id)

    _db.save_payment(order_id, user_id, pack_key, pack["amount"])

    pay_url = (
        f"https://pay.freekassa.com/"
        f"?m={FREEKASSA_SHOP_ID}"
        f"&oc={amount}"
        f"&o={order_id}"
        f"&s={sign}"
        f"&currency={currency}"
        f"&us_userid={user_id}"
    )

    logger.info("FreeKassa payment URL created: order=%s, user=%s, pack=%s", order_id, user_id, pack_key)

    return {
        "ok": True,
        "pay_url": pay_url,
        "order_id": order_id,
    }


def verify_notification_sign(data: dict) -> bool:
    merchant_id = str(data.get("MERCHANT_ID", ""))
    amount = str(data.get("AMOUNT", ""))
    order_id = str(data.get("MERCHANT_ORDER_ID", ""))
    received_sign = str(data.get("SIGN", ""))

    if not all([merchant_id, amount, order_id, received_sign]):
        return False

    expected_sign = _make_notification_sign(merchant_id, amount, FREEKASSA_SECRET2, order_id)
    return expected_sign == received_sign
