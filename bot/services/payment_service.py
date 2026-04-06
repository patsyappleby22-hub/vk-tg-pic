import logging
import os
import time
import aiohttp

logger = logging.getLogger(__name__)

PALLY_API_URL = "https://pally.info/api/v1"
PALLY_SHOP_ID = os.getenv("PALLY_SHOP_ID", "")
PALLY_TOKEN = os.getenv("PALLY_TOKEN", "")
BASE_URL = os.getenv("BASE_URL", "")

CREDIT_PACKAGES = {
    "pack_30": {"credits": 30, "amount": 99.00, "label": "30 кредитов — 99₽"},
    "pack_99": {"credits": 99, "amount": 299.00, "label": "99 кредитов — 299₽"},
}


async def create_payment(user_id: int, pack_key: str) -> dict:
    pack = CREDIT_PACKAGES.get(pack_key)
    if not pack:
        return {"ok": False, "error": "Неизвестный пакет"}

    if not PALLY_SHOP_ID or not PALLY_TOKEN or not BASE_URL:
        return {"ok": False, "error": "Платёжная система не настроена"}

    order_id = f"{user_id}_{pack_key}_{int(time.time())}"

    payload = {
        "shop_id": PALLY_SHOP_ID,
        "amount": pack["amount"],
        "currency": "RUB",
        "order_id": order_id,
        "description": f"AI Image Bot: {pack['label']}",
        "success_url": f"{BASE_URL}/payment/success",
        "fail_url": f"{BASE_URL}/payment/fail",
        "result_url": f"{BASE_URL}/webhook/pally",
    }

    headers = {
        "Authorization": f"Bearer {PALLY_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{PALLY_API_URL}/payment/create",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()
                logger.info("Pally create_payment response: %s", data)

                if data.get("status") or data.get("pay_url"):
                    return {
                        "ok": True,
                        "pay_url": data["pay_url"],
                        "payment_id": data.get("payment_id", ""),
                        "order_id": order_id,
                    }
                else:
                    return {"ok": False, "error": data.get("message", "Ошибка API")}
    except Exception as e:
        logger.exception("Pally API error")
        return {"ok": False, "error": str(e)}
