import hashlib
import hmac
import json
import logging
import os
from pathlib import Path

from aiohttp import web

import bot.db as _db
from bot.user_settings import add_credits

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATES_CANDIDATES = [
    _PROJECT_ROOT / "web" / "templates",
    Path("/app/web/templates"),
    Path("/app") / "web" / "templates",
]

PALLY_SHOP_ID = os.getenv("PALLY_SHOP_ID", "")
PALLY_TOKEN = os.getenv("PALLY_TOKEN", "")

CREDIT_PACKAGES = {
    "pack_30": {"credits": 30, "amount": 99.00, "label": "30 кредитов"},
    "pack_99": {"credits": 99, "amount": 299.00, "label": "99 кредитов"},
}


def _find_templates_dir() -> Path | None:
    for d in _TEMPLATES_CANDIDATES:
        if d.is_dir():
            return d
    return None


def _read_template(name: str) -> str:
    tdir = _find_templates_dir()
    if tdir:
        f = tdir / name
        if f.exists():
            return f.read_text(encoding="utf-8")
    logger.warning("Template %s not found, using fallback", name)
    return _FALLBACK_TEMPLATES.get(name, "<h1>PicGenAI</h1>")


_FALLBACK_TEMPLATES = {
    "index.html": """<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>PicGenAI</title><style>*{margin:0;padding:0;box-sizing:border-box}body{font-family:-apple-system,sans-serif;background:#08070e;color:#e4e4ef;min-height:100vh;display:flex;align-items:center;justify-content:center;text-align:center}.c{max-width:600px;padding:40px}h1{font-size:2.5em;margin-bottom:16px;background:linear-gradient(135deg,#a78bfa,#60a5fa);-webkit-background-clip:text;-webkit-text-fill-color:transparent}p{color:#8888a8;line-height:1.7;margin-bottom:24px}.btn{display:inline-block;padding:14px 28px;border-radius:12px;text-decoration:none;font-weight:600;color:#fff;margin:8px;background:linear-gradient(135deg,#7c3aed,#6366f1)}.btn.vk{background:#4C75A3}</style></head><body><div class="c"><h1>PicGenAI</h1><p>Генерация изображений с помощью ИИ. Работает в Telegram и ВКонтакте.</p><a href="https://t.me/PicGenAI_26_bot" class="btn">Telegram Bot</a><a href="https://vk.ru/picgenai" class="btn vk">ВКонтакте</a></div></body></html>""",
    "success.html": """<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>Оплата успешна</title><style>*{margin:0;padding:0;box-sizing:border-box}body{font-family:-apple-system,sans-serif;background:#08070e;color:#e4e4ef;min-height:100vh;display:flex;align-items:center;justify-content:center;text-align:center}.c{max-width:440px;padding:48px 40px;background:rgba(255,255,255,.03);border:1px solid rgba(52,211,153,.2);border-radius:24px}h1{color:#34d399;margin:16px 0}p{color:#8888a8;line-height:1.7}</style></head><body><div class="c"><h1>Оплата прошла успешно!</h1><p>Кредиты начислены. Вернитесь в бота.</p><p><a href="https://t.me/PicGenAI_26_bot" style="color:#a78bfa">Вернуться в бота</a></p></div></body></html>""",
    "fail.html": """<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>Ошибка оплаты</title><style>*{margin:0;padding:0;box-sizing:border-box}body{font-family:-apple-system,sans-serif;background:#08070e;color:#e4e4ef;min-height:100vh;display:flex;align-items:center;justify-content:center;text-align:center}.c{max-width:440px;padding:48px 40px;background:rgba(255,255,255,.03);border:1px solid rgba(248,113,113,.2);border-radius:24px}h1{color:#f87171;margin:16px 0}p{color:#8888a8;line-height:1.7}</style></head><body><div class="c"><h1>Оплата не прошла</h1><p>Платёж не был завершён. Попробуйте ещё раз.</p><p><a href="https://t.me/PicGenAI_26_bot" style="color:#a78bfa">Вернуться в бота</a></p></div></body></html>""",
}


def _verify_sign(data: dict, token: str) -> bool:
    sign = data.get("sign", "")
    if not sign:
        return False
    filtered = {k: v for k, v in data.items() if k != "sign"}
    sorted_keys = sorted(filtered.keys())
    values = [str(filtered[k]) for k in sorted_keys]
    raw = ":".join(values) + ":" + token
    expected = hashlib.sha256(raw.encode()).hexdigest()
    return hmac.compare_digest(expected, sign)


async def handle_index(request: web.Request) -> web.Response:
    html = _read_template("index.html")
    return web.Response(text=html, content_type="text/html")


async def handle_success(request: web.Request) -> web.Response:
    html = _read_template("success.html")
    return web.Response(text=html, content_type="text/html")


async def handle_fail(request: web.Request) -> web.Response:
    html = _read_template("fail.html")
    return web.Response(text=html, content_type="text/html")


def _parse_webhook_body(data: dict) -> web.Response | None:
    if not PALLY_TOKEN:
        logger.error("Webhook called but PALLY_TOKEN is not configured — rejecting")
        return web.Response(text="NOT CONFIGURED", status=503)
    if not _verify_sign(data, PALLY_TOKEN):
        logger.warning("Pally webhook: invalid signature")
        return web.Response(text="INVALID SIGN", status=403)
    return None


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

    err = _parse_webhook_body(data)
    if err:
        return err

    status = data.get("status", "")
    order_id = data.get("order_id", "")
    payment_id = data.get("payment_id", "")
    received_amount = data.get("amount")
    received_shop_id = data.get("shop_id", "")

    if PALLY_SHOP_ID and received_shop_id and received_shop_id != PALLY_SHOP_ID:
        logger.warning("Webhook shop_id mismatch: expected=%s got=%s", PALLY_SHOP_ID, received_shop_id)
        return web.Response(text="SHOP MISMATCH", status=403)

    if status == "success" and order_id:
        if _db.is_available():
            stored = _db.get_payment(order_id)
            if stored:
                if stored["status"] == "success":
                    logger.info("Duplicate webhook for already-completed order %s — skipping", order_id)
                    return web.Response(text="OK", status=200)
                if received_amount is not None:
                    try:
                        if abs(float(received_amount) - stored["amount"]) > 0.01:
                            logger.warning(
                                "Amount mismatch for %s: expected=%.2f got=%s",
                                order_id, stored["amount"], received_amount,
                            )
                            return web.Response(text="AMOUNT MISMATCH", status=400)
                    except (ValueError, TypeError):
                        pass
                if not _db.complete_payment(order_id, payment_id):
                    logger.info("Order %s already completed (race) — skipping", order_id)
                    return web.Response(text="OK", status=200)
                new_balance = add_credits(stored["user_id"], CREDIT_PACKAGES[stored["pack_key"]]["credits"])
                logger.info(
                    "Credits added: user=%s, pack=%s, credits=+%d, balance=%d",
                    stored["user_id"], stored["pack_key"],
                    CREDIT_PACKAGES[stored["pack_key"]]["credits"], new_balance,
                )
            else:
                if not _db.mark_order_processed_memory(order_id):
                    logger.info("Duplicate in-memory webhook for %s — skipping", order_id)
                    return web.Response(text="OK", status=200)
                _credit_from_order_id(order_id)
        else:
            if not _db.mark_order_processed_memory(order_id):
                logger.info("Duplicate in-memory webhook for %s — skipping", order_id)
                return web.Response(text="OK", status=200)
            _credit_from_order_id(order_id)

    elif status in ("refund", "chargeback") and order_id:
        logger.warning("Pally %s for order %s", status, order_id)

    return web.Response(text="OK", status=200)


def _credit_from_order_id(order_id: str) -> None:
    parts = order_id.split("_")
    if len(parts) >= 3:
        try:
            user_id = int(parts[0])
            pack_key = "_".join(parts[1:3])
            pack = CREDIT_PACKAGES.get(pack_key)
            if pack:
                new_balance = add_credits(user_id, pack["credits"])
                logger.info(
                    "Credits added (fallback): user=%s, pack=%s, credits=+%d, balance=%d",
                    user_id, pack_key, pack["credits"], new_balance,
                )
            else:
                logger.warning("Unknown pack in order_id: %s", order_id)
        except (ValueError, IndexError):
            logger.error("Cannot parse order_id: %s", order_id)
    else:
        logger.warning("Unexpected order_id format: %s", order_id)


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


async def handle_verification(request: web.Request) -> web.Response:
    return web.Response(text="shop-verification-WG76VJD7xl", content_type="text/plain")


def create_web_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/shop-verification-WG76VJD7xl.txt", handle_verification)
    app.router.add_get("/payment/success", handle_success)
    app.router.add_get("/payment/fail", handle_fail)
    app.router.add_post("/webhook/pally", handle_webhook)
    app.router.add_post("/webhook/pally/refund", handle_refund)
    app.router.add_post("/webhook/pally/chargeback", handle_chargeback)
    return app
