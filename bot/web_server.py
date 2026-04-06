import hashlib
import json
import logging
import os
from pathlib import Path

from aiohttp import web

from bot.user_settings import add_credits, get_user_settings, save_user_settings

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "web" / "templates"

PALLY_SHOP_ID = os.getenv("PALLY_SHOP_ID", "")
PALLY_TOKEN = os.getenv("PALLY_TOKEN", "")

CREDIT_PACKAGES = {
    "pack_30": {"credits": 30, "amount": 99.00, "label": "30 кредитов"},
    "pack_99": {"credits": 99, "amount": 299.00, "label": "99 кредитов"},
}


def _read_template(name: str) -> str:
    return (TEMPLATES_DIR / name).read_text(encoding="utf-8")


def _verify_sign(data: dict, token: str) -> bool:
    sign = data.get("sign", "")
    if not sign:
        return False
    filtered = {k: v for k, v in data.items() if k != "sign"}
    sorted_keys = sorted(filtered.keys())
    values = [str(filtered[k]) for k in sorted_keys]
    raw = ":".join(values) + ":" + token
    expected = hashlib.sha256(raw.encode()).hexdigest()
    return expected == sign


async def handle_index(request: web.Request) -> web.Response:
    html = _read_template("index.html")
    return web.Response(text=html, content_type="text/html")


async def handle_success(request: web.Request) -> web.Response:
    html = _read_template("success.html")
    return web.Response(text=html, content_type="text/html")


async def handle_fail(request: web.Request) -> web.Response:
    html = _read_template("fail.html")
    return web.Response(text=html, content_type="text/html")


async def handle_webhook(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        try:
            data = dict(await request.post())
        except Exception:
            logger.error("Webhook: cannot parse body")
            return web.Response(text="BAD REQUEST", status=400)

    logger.info("Pally webhook received: %s", json.dumps(data, ensure_ascii=False))

    if PALLY_TOKEN and not _verify_sign(data, PALLY_TOKEN):
        logger.warning("Pally webhook: invalid signature")
        return web.Response(text="INVALID SIGN", status=403)

    status = data.get("status", "")
    order_id = data.get("order_id", "")

    if status == "success" and order_id:
        parts = order_id.split("_")
        if len(parts) >= 3:
            try:
                user_id = int(parts[0])
                pack_key = "_".join(parts[1:3])
                pack = CREDIT_PACKAGES.get(pack_key)
                if pack:
                    new_balance = add_credits(user_id, pack["credits"])
                    logger.info(
                        "Credits added: user=%s, pack=%s, credits=+%d, balance=%d",
                        user_id, pack_key, pack["credits"], new_balance,
                    )
                else:
                    logger.warning("Unknown pack in order_id: %s", order_id)
            except (ValueError, IndexError):
                logger.error("Cannot parse order_id: %s", order_id)
        else:
            logger.warning("Unexpected order_id format: %s", order_id)

    elif status in ("refund", "chargeback") and order_id:
        logger.warning("Pally %s for order %s", status, order_id)

    return web.Response(text="OK", status=200)


async def handle_refund(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        try:
            data = dict(await request.post())
        except Exception:
            data = {}

    logger.warning("Pally REFUND webhook: %s", json.dumps(data, ensure_ascii=False))
    return web.Response(text="OK", status=200)


async def handle_chargeback(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        try:
            data = dict(await request.post())
        except Exception:
            data = {}

    logger.warning("Pally CHARGEBACK webhook: %s", json.dumps(data, ensure_ascii=False))
    return web.Response(text="OK", status=200)


def create_web_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/payment/success", handle_success)
    app.router.add_get("/payment/fail", handle_fail)
    app.router.add_post("/webhook/pally", handle_webhook)
    app.router.add_post("/webhook/pally/refund", handle_refund)
    app.router.add_post("/webhook/pally/chargeback", handle_chargeback)
    return app
