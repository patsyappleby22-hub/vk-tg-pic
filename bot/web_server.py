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
    "pack_30": {"credits": 30, "amount": 10.00, "label": "30 кредитов"},
    "pack_100": {"credits": 100, "amount": 299.00, "label": "100 кредитов"},
    "pack_200": {"credits": 200, "amount": 549.00, "label": "200 кредитов"},
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
    "index.html": """<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>PicGenAI — Генерация изображений ИИ</title><style>*{margin:0;padding:0;box-sizing:border-box}body{font-family:-apple-system,sans-serif;background:#08070e;color:#e4e4ef;min-height:100vh}a{color:#a78bfa;text-decoration:none}.header{text-align:center;padding:60px 20px 40px}.header h1{font-size:2.8em;margin-bottom:12px;background:linear-gradient(135deg,#a78bfa,#60a5fa);-webkit-background-clip:text;-webkit-text-fill-color:transparent}.header p{color:#8888a8;font-size:1.1em;line-height:1.7;max-width:600px;margin:0 auto 28px}.buttons{display:flex;gap:16px;justify-content:center;flex-wrap:wrap}.btn{display:inline-block;padding:14px 28px;border-radius:12px;text-decoration:none;font-weight:600;color:#fff;font-size:1em}.btn.tg{background:linear-gradient(135deg,#7c3aed,#6366f1)}.btn.vk{background:#4C75A3}.section{max-width:800px;margin:0 auto;padding:40px 20px}.section h2{font-size:1.6em;margin-bottom:20px;color:#c4b5fd}.services{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:20px;margin-bottom:40px}.service-card{background:rgba(255,255,255,.03);border:1px solid rgba(167,139,250,.15);border-radius:16px;padding:24px;text-align:center}.service-card .price{font-size:1.8em;font-weight:700;color:#a78bfa;margin:8px 0}.service-card .credits{font-size:1.1em}.service-card .desc{color:#8888a8;font-size:.9em}.contacts{background:rgba(255,255,255,.03);border:1px solid rgba(167,139,250,.1);border-radius:16px;padding:28px;margin-bottom:40px}.contacts p{color:#c4c4d8;line-height:1.8}.footer{border-top:1px solid rgba(255,255,255,.06);padding:30px 20px;text-align:center;max-width:800px;margin:0 auto}.footer-links{display:flex;flex-wrap:wrap;justify-content:center;gap:12px 24px;margin-bottom:16px}.footer-links a{color:#8888a8;font-size:.9em}.footer .copy{color:#555;font-size:.85em}</style></head><body><div class="header"><h1>PicGenAI</h1><p>Сервис генерации изображений с помощью искусственного интеллекта. Создавайте уникальные картинки по текстовому описанию прямо в Telegram и ВКонтакте.</p><div class="buttons"><a href="https://t.me/PicGenAI_26_bot" class="btn tg">Telegram Bot</a><a href="https://vk.ru/picgenai" class="btn vk">ВКонтакте</a></div></div><div class="section"><h2>Услуги и цены</h2><div class="services"><div class="service-card"><div class="credits">30 кредитов</div><div class="price">99 ₽</div><div class="desc">Для первого знакомства</div></div><div class="service-card"><div class="credits">100 кредитов</div><div class="price">299 ₽</div><div class="desc">Оптимальный выбор</div></div><div class="service-card"><div class="credits">200 кредитов</div><div class="price">549 ₽</div><div class="desc">Максимум возможностей</div></div></div><p style="color:#8888a8;font-size:.9em;text-align:center;margin-bottom:40px">При регистрации начисляется 20 бесплатных кредитов. 1 генерация = 1 кредит, генерация в 4K = 2 кредита.</p><h2>Контакты</h2><div class="contacts"><p><strong>Владелец сервиса:</strong> Худайбердиев Гайрат</p><p><strong>Email:</strong> <a href="mailto:mistermackalister@gmail.com">mistermackalister@gmail.com</a></p><p><strong>Телефон:</strong> <a href="tel:+79503183091">+7 (950) 318-30-91</a></p><p><strong>Поддержка:</strong> <a href="https://t.me/ShadowsockTM">@ShadowsockTM</a> (Telegram)</p></div></div><div class="footer"><div class="footer-links"><a href="/offer">Договор оферты</a><a href="/privacy">Политика конфиденциальности</a><a href="/consent">Согласие на обработку данных</a><a href="/refund">Условия возврата</a></div><div class="copy">© 2025–2026 PicGenAI. Все права защищены.</div></div></body></html>""",
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


_TG_BOT_URL = "https://t.me/PicGenAI_26_bot"
_VK_BOT_URL = "https://vk.me/picgenai"


async def handle_success(request: web.Request) -> web.Response:
    src = request.rel_url.query.get("src", "tg")
    bot_url = _VK_BOT_URL if src == "vk" else _TG_BOT_URL
    bot_label = "ВКонтакте" if src == "vk" else "Telegram"
    html = _read_template("success.html")
    html = html.replace("https://t.me/PicGenAI_26_bot", bot_url)
    html = html.replace("Вернуться в бота", f"Вернуться в {bot_label}-бот")
    return web.Response(text=html, content_type="text/html")


async def handle_fail(request: web.Request) -> web.Response:
    src = request.rel_url.query.get("src", "tg")
    bot_url = _VK_BOT_URL if src == "vk" else _TG_BOT_URL
    bot_label = "ВКонтакте" if src == "vk" else "Telegram"
    html = _read_template("fail.html")
    html = html.replace("https://t.me/PicGenAI_26_bot", bot_url)
    html = html.replace("Вернуться в бота", f"Вернуться в {bot_label}-бот")
    return web.Response(text=html, content_type="text/html")


async def handle_offer(request: web.Request) -> web.Response:
    html = _read_template("offer.html")
    return web.Response(text=html, content_type="text/html")


async def handle_privacy(request: web.Request) -> web.Response:
    html = _read_template("privacy.html")
    return web.Response(text=html, content_type="text/html")


async def handle_consent(request: web.Request) -> web.Response:
    html = _read_template("consent.html")
    return web.Response(text=html, content_type="text/html")


async def handle_refund_page(request: web.Request) -> web.Response:
    html = _read_template("refund.html")
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


async def handle_freekassa_notification(request: web.Request) -> web.Response:
    from bot.services.freekassa_service import verify_notification_sign, CREDIT_PACKAGES as FK_PACKAGES

    try:
        data = dict(await request.post())
    except Exception:
        logger.error("FreeKassa webhook: cannot parse body")
        return web.Response(text="BAD REQUEST", status=400)

    logger.info("FreeKassa webhook received: %s", json.dumps(data, ensure_ascii=False))

    if not verify_notification_sign(data):
        logger.warning("FreeKassa webhook: invalid signature")
        return web.Response(text="INVALID SIGN", status=403)

    order_id = data.get("MERCHANT_ORDER_ID", "")
    payment_id = data.get("intid", "")
    received_amount = data.get("AMOUNT", "")

    if not order_id:
        return web.Response(text="NO ORDER", status=400)

    if _db.is_available():
        stored = _db.get_payment(order_id)
        if stored:
            if stored["status"] == "success":
                logger.info("FreeKassa: order %s already completed — skipping", order_id)
                return web.Response(text="YES", status=200)
            if received_amount:
                try:
                    if abs(float(received_amount) - stored["amount"]) > 0.01:
                        logger.warning("FreeKassa amount mismatch: expected=%.2f got=%s", stored["amount"], received_amount)
                        return web.Response(text="AMOUNT MISMATCH", status=400)
                except (ValueError, TypeError):
                    pass
            if not _db.complete_payment(order_id, str(payment_id)):
                logger.info("FreeKassa: order %s already completed (race) — skipping", order_id)
                return web.Response(text="YES", status=200)
            pack = FK_PACKAGES.get(stored["pack_key"]) or CREDIT_PACKAGES.get(stored["pack_key"])
            if pack:
                user_id = stored["user_id"]
                new_balance = add_credits(user_id, pack["credits"])
                logger.info(
                    "FreeKassa credits added: user=%s, pack=%s, credits=+%d, balance=%d",
                    user_id, stored["pack_key"], pack["credits"], new_balance,
                )
            return web.Response(text="YES", status=200)

    if not _db.mark_order_processed_memory(f"fk_{order_id}"):
        logger.info("FreeKassa duplicate webhook for %s (in-memory) — skipping", order_id)
        return web.Response(text="YES", status=200)
    _credit_from_order_id(order_id, FK_PACKAGES)
    return web.Response(text="YES", status=200)


def _credit_from_order_id(order_id: str, packages: dict | None = None) -> None:
    if packages is None:
        packages = CREDIT_PACKAGES
    all_packages = {**CREDIT_PACKAGES, **packages}
    parts = order_id.split("_")
    if len(parts) >= 3:
        try:
            user_id = int(parts[0])
            pack_key = "_".join(parts[1:-1])
            pack = all_packages.get(pack_key)
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


async def handle_lava_webhook(request: web.Request) -> web.Response:
    from bot.services.lava_service import verify_webhook_sign, CREDIT_PACKAGES as LAVA_PACKAGES

    try:
        data = await request.json()
    except Exception:
        logger.error("Lava webhook: cannot parse body")
        return web.Response(text="BAD REQUEST", status=400)

    logger.info("Lava webhook received: %s", json.dumps(data, ensure_ascii=False))

    webhook_type = data.get("type")
    if webhook_type != 1:
        return web.Response(text="OK", status=200)

    invoice_id = str(data.get("invoice_id", ""))
    order_id = str(data.get("order_id", ""))
    status = data.get("status", "")
    amount = str(data.get("amount", ""))
    pay_time = str(data.get("pay_time", ""))
    received_sign = str(data.get("sign", ""))
    source = str(data.get("custom_fields", "tg") or "tg")

    if received_sign and invoice_id and amount and pay_time:
        if not verify_webhook_sign(invoice_id, amount, pay_time, received_sign):
            logger.warning("Lava webhook: invalid signature")
            return web.Response(text="INVALID SIGN", status=403)

    if status != "success" or not order_id:
        return web.Response(text="OK", status=200)

    if _db.is_available():
        stored = _db.get_payment(order_id)
        if stored:
            if stored["status"] == "success":
                logger.info("Lava: order %s already completed — skipping", order_id)
                return web.Response(text="OK", status=200)
            if not _db.complete_payment(order_id, invoice_id):
                logger.info("Lava: order %s race condition — skipping", order_id)
                return web.Response(text="OK", status=200)
            pack = LAVA_PACKAGES.get(stored["pack_key"]) or CREDIT_PACKAGES.get(stored["pack_key"])
            if pack:
                new_balance = add_credits(stored["user_id"], pack["credits"])
                logger.info(
                    "Lava credits added: user=%s pack=%s credits=+%d balance=%d",
                    stored["user_id"], stored["pack_key"], pack["credits"], new_balance,
                )
                from bot.notify import notify_payment
                await notify_payment(
                    stored["user_id"], pack["credits"],
                    stored["amount"], pack["label"],
                    source=source,
                )
        else:
            if not _db.mark_order_processed_memory(f"lava_{order_id}"):
                logger.info("Lava duplicate in-memory webhook for %s — skipping", order_id)
                return web.Response(text="OK", status=200)
            _credit_from_order_id(order_id, LAVA_PACKAGES)
    else:
        if not _db.mark_order_processed_memory(f"lava_{order_id}"):
            logger.info("Lava duplicate in-memory webhook for %s — skipping", order_id)
            return web.Response(text="OK", status=200)
        _credit_from_order_id(order_id, LAVA_PACKAGES)

    return web.Response(text="OK", status=200)


def create_web_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/shop-verification-WG76VJD7xl.txt", handle_verification)
    app.router.add_get("/payment/success", handle_success)
    app.router.add_get("/payment/fail", handle_fail)
    app.router.add_get("/offer", handle_offer)
    app.router.add_get("/privacy", handle_privacy)
    app.router.add_get("/consent", handle_consent)
    app.router.add_get("/refund", handle_refund_page)
    app.router.add_post("/webhook/lava", handle_lava_webhook)
    app.router.add_post("/webhook/pally", handle_webhook)
    app.router.add_post("/webhook/pally/refund", handle_refund)
    app.router.add_post("/webhook/pally/chargeback", handle_chargeback)
    app.router.add_post("/api/freekassa/notification", handle_freekassa_notification)
    from bot.web_admin import register_admin_routes
    register_admin_routes(app)
    return app
