"""
bot/web_admin.py
~~~~~~~~~~~~~~~~~
Web-based admin panel for PicGenAI.

Routes:
  GET  /admin                  → redirect to /admin/dashboard
  GET  /admin/login            → login form
  POST /admin/login            → authenticate
  GET  /admin/logout           → clear session
  GET  /admin/dashboard        → stats overview
  GET  /admin/users            → paginated user list
  GET  /admin/users/{uid}      → user detail + payments
  GET  /admin/payments         → all payments history
  POST /admin/api/users/{uid}/credits     → {"action":"add"|"set","amount":N}
  POST /admin/api/users/{uid}/block       → toggle block
  POST /admin/api/users/{uid}/delete      → delete user
  POST /admin/api/users/{uid}/reset_gens  → reset generation counter
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import random
import secrets
import time
from typing import Any

import aiohttp as _aiohttp
from aiohttp import web

import bot.db as _db
import bot.api_keys_store as _key_store
from bot.user_settings import (
    user_settings as _users,
    add_credits,
    set_credits,
    set_blocked,
    delete_user,
    reset_generations,
    get_user_settings,
)

logger = logging.getLogger(__name__)

# Vertex AI service reference — set by start_all.py after boot
_vertex_service: "Any | None" = None


def set_vertex_service(svc: "Any") -> None:
    """Called from start_all.py so the admin panel can query slot statuses."""
    global _vertex_service
    _vertex_service = svc

_MSK_OFFSET_HOURS = 3  # UTC+3


def _msk(dt_str: str) -> str:
    """Convert a UTC ISO datetime string to Moscow time (UTC+3), formatted as 'DD.MM.YYYY HH:MM'."""
    if not dt_str:
        return "—"
    try:
        from datetime import datetime, timedelta, timezone
        # PostgreSQL isoformat may include microseconds or +00:00
        clean = dt_str.replace("Z", "+00:00")
        if "+" not in clean and clean.count("-") <= 2:
            # naive — assume UTC
            dt = datetime.fromisoformat(clean)
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = datetime.fromisoformat(clean)
        msk = dt + timedelta(hours=_MSK_OFFSET_HOURS)
        return msk.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return dt_str[:16].replace("T", " ")


_ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "mrxgyt02")
_SESSION_SECRET = hashlib.sha256((_ADMIN_PASSWORD + "_picgenai_admin_v1").encode()).hexdigest()
_COOKIE_NAME = "admin_tok"
_COOKIE_MAX_AGE = 86400 * 7  # 7 days
_PAGE_SIZE = 50

_ADMIN_TG_ID = 6014789391          # admin Telegram user ID for 2FA codes
_2FA_TTL = 300                      # seconds the code is valid
_pending_2fa: dict[str, tuple[str, float]] = {}  # token → (code, expires_at)


# ─── Auth ────────────────────────────────────────────────────────────────────

def _make_token() -> str:
    return hmac.new(_SESSION_SECRET.encode(), b"admin_authenticated", hashlib.sha256).hexdigest()


def _is_auth(request: web.Request) -> bool:
    tok = request.cookies.get(_COOKIE_NAME, "")
    if not tok:
        return False
    return hmac.compare_digest(tok, _make_token())


def _require_auth(fn):
    async def wrapper(request: web.Request):
        if not _is_auth(request):
            raise web.HTTPFound("/admin/login")
        return await fn(request)
    return wrapper


# ─── Shared layout ───────────────────────────────────────────────────────────

def _layout(title: str, content: str, active: str = "") -> str:
    nav_items = [
        ("dashboard", "/admin/dashboard",  "📊", "Дашборд"),
        ("users",     "/admin/users",      "👥", "Пользователи"),
        ("payments",  "/admin/payments",   "💳", "Платежи"),
        ("apikeys",   "/admin/api-keys",   "🔑", "API ключи"),
    ]
    sidebar_nav = ""
    bottom_nav = ""
    for key, href, icon, label in nav_items:
        active_cls = " active" if active == key else ""
        sidebar_nav += f'<a href="{href}" class="nav-link{active_cls}"><span class="nav-icon">{icon}</span><span class="nav-label">{label}</span></a>\n'
        bottom_nav += f'<a href="{href}" class="bot-link{active_cls}"><span>{icon}</span><span class="bot-label">{label}</span></a>\n'

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — PicGenAI Admin</title>
<style>
  *{{margin:0;padding:0;box-sizing:border-box}}
  :root{{
    --bg:#08070e;--surface:#0f0e1a;--border:rgba(167,139,250,.15);
    --accent:#a78bfa;--accent2:#60a5fa;--text:#e4e4ef;--muted:#8888a8;
    --green:#34d399;--red:#f87171;--yellow:#fbbf24;--orange:#fb923c;
  }}
  body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
    background:var(--bg);color:var(--text);min-height:100vh;display:flex;
    flex-direction:column}}
  a{{color:var(--accent);text-decoration:none}}
  a:hover{{opacity:.8}}

  /* ── Desktop layout ── */
  .layout{{display:flex;flex:1;min-height:0}}
  .sidebar{{width:220px;background:var(--surface);border-right:1px solid var(--border);
    padding:24px 0;display:flex;flex-direction:column;flex-shrink:0}}
  .sidebar-logo{{padding:0 20px 24px;font-size:1.15em;font-weight:700;
    background:linear-gradient(135deg,var(--accent),var(--accent2));
    -webkit-background-clip:text;-webkit-text-fill-color:transparent;
    white-space:nowrap;overflow:hidden}}
  .nav-link{{display:flex;align-items:center;gap:10px;padding:10px 20px;
    color:var(--muted);font-size:.95em;border-left:3px solid transparent;
    transition:.15s;white-space:nowrap}}
  .nav-link:hover{{color:var(--text);background:rgba(167,139,250,.06);opacity:1}}
  .nav-link.active{{color:var(--accent);border-left-color:var(--accent);
    background:rgba(167,139,250,.08)}}
  .nav-icon{{font-size:1.1em;flex-shrink:0}}
  .sidebar-bottom{{margin-top:auto;padding:20px}}
  .logout-btn{{display:block;padding:9px 16px;background:rgba(248,113,113,.1);
    border:1px solid rgba(248,113,113,.2);border-radius:8px;color:var(--red);
    text-align:center;font-size:.9em}}
  .logout-btn:hover{{background:rgba(248,113,113,.2);opacity:1}}

  /* ── Main content ── */
  .main{{flex:1;padding:32px;overflow-x:auto;min-width:0}}
  .page-title{{font-size:1.6em;font-weight:700;margin-bottom:24px;
    background:linear-gradient(135deg,var(--accent),var(--accent2));
    -webkit-background-clip:text;-webkit-text-fill-color:transparent}}

  /* ── Cards ── */
  .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
    gap:16px;margin-bottom:28px}}
  .card{{background:var(--surface);border:1px solid var(--border);
    border-radius:14px;padding:20px}}
  .card-label{{color:var(--muted);font-size:.82em;margin-bottom:6px}}
  .card-value{{font-size:1.9em;font-weight:700}}
  .card-value.green{{color:var(--green)}}
  .card-value.purple{{color:var(--accent)}}
  .card-value.blue{{color:var(--accent2)}}
  .card-value.yellow{{color:var(--yellow)}}
  .card-value.red{{color:var(--red)}}

  /* ── Tables ── */
  .table-wrap{{background:var(--surface);border:1px solid var(--border);
    border-radius:14px;overflow:auto;-webkit-overflow-scrolling:touch}}
  table{{width:100%;border-collapse:collapse;font-size:.9em}}
  thead th{{padding:12px 16px;text-align:left;color:var(--muted);
    font-weight:600;font-size:.8em;text-transform:uppercase;letter-spacing:.05em;
    border-bottom:1px solid var(--border);white-space:nowrap}}
  tbody td{{padding:11px 16px;border-bottom:1px solid rgba(255,255,255,.04);
    white-space:nowrap}}
  tbody tr:last-child td{{border-bottom:none}}
  tbody tr:hover{{background:rgba(167,139,250,.04)}}
  .badge{{display:inline-block;padding:3px 9px;border-radius:20px;
    font-size:.78em;font-weight:600;white-space:nowrap}}
  .badge-green{{background:rgba(52,211,153,.12);color:var(--green)}}
  .badge-red{{background:rgba(248,113,113,.12);color:var(--red)}}
  .badge-yellow{{background:rgba(251,191,36,.12);color:var(--yellow)}}
  .badge-blue{{background:rgba(96,165,250,.12);color:var(--accent2)}}
  .badge-purple{{background:rgba(167,139,250,.12);color:var(--accent)}}

  /* ── Toolbar ── */
  .toolbar{{display:flex;gap:10px;margin-bottom:16px;flex-wrap:wrap;align-items:center}}
  .search-input{{background:var(--surface);border:1px solid var(--border);
    border-radius:8px;padding:8px 14px;color:var(--text);font-size:.9em;
    min-width:0;flex:1 1 200px;outline:none}}
  .search-input:focus{{border-color:var(--accent)}}
  select{{background:var(--surface);border:1px solid var(--border);
    border-radius:8px;padding:8px 12px;color:var(--text);font-size:.9em;
    flex:1 1 140px;min-width:0}}

  /* ── Buttons ── */
  .btn{{display:inline-block;padding:8px 18px;border-radius:8px;border:none;
    cursor:pointer;font-size:.9em;font-weight:600;transition:.15s;
    white-space:nowrap;text-align:center}}
  .btn-primary{{background:linear-gradient(135deg,#7c3aed,#6366f1);color:#fff}}
  .btn-primary:hover{{opacity:.85}}
  .btn-danger{{background:rgba(248,113,113,.15);color:var(--red);
    border:1px solid rgba(248,113,113,.25)}}
  .btn-danger:hover{{background:rgba(248,113,113,.25)}}
  .btn-success{{background:rgba(52,211,153,.15);color:var(--green);
    border:1px solid rgba(52,211,153,.25)}}
  .btn-success:hover{{background:rgba(52,211,153,.25)}}
  .btn-muted{{background:rgba(255,255,255,.06);color:var(--muted);
    border:1px solid var(--border)}}
  .btn-muted:hover{{background:rgba(255,255,255,.1);color:var(--text)}}
  .btn-sm{{padding:5px 12px;font-size:.82em}}

  /* ── Detail grid ── */
  .section-heading{{font-size:1.05em;font-weight:600;color:var(--accent);
    margin:28px 0 14px}}
  .detail-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
    gap:12px;margin-bottom:24px}}
  .detail-card{{background:var(--surface);border:1px solid var(--border);
    border-radius:12px;padding:16px}}
  .detail-card-label{{color:var(--muted);font-size:.8em;margin-bottom:4px}}
  .detail-card-value{{font-size:1.05em;font-weight:600;word-break:break-all}}
  .actions-row{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:24px}}

  /* ── Image list ── */
  .img-empty{{color:var(--muted);text-align:center;padding:24px;
    border:1px dashed var(--border);border-radius:10px;margin-bottom:24px}}

  /* Lightbox */
  .lightbox{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.9);
    z-index:200;align-items:center;justify-content:center;padding:16px;flex-direction:column}}
  .lightbox.open{{display:flex}}
  .lightbox img{{max-width:90vw;max-height:80vh;border-radius:8px;object-fit:contain}}
  .lightbox-caption{{color:#ccc;font-size:.9em;margin-top:12px;max-width:600px;
    text-align:center;line-height:1.4}}
  .lightbox-close{{position:absolute;top:16px;right:20px;font-size:2em;
    color:#fff;cursor:pointer;line-height:1}}

  /* ── Modal ── */
  .modal-overlay{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);
    z-index:100;align-items:center;justify-content:center;padding:16px}}
  .modal-overlay.open{{display:flex}}
  .modal{{background:#14122a;border:1px solid var(--border);border-radius:16px;
    padding:24px;width:100%;max-width:400px}}
  .modal h3{{margin-bottom:16px;font-size:1.1em}}
  .modal input{{width:100%;background:var(--surface);border:1px solid var(--border);
    border-radius:8px;padding:9px 14px;color:var(--text);font-size:.95em;
    margin-bottom:14px;outline:none}}
  .modal input:focus{{border-color:var(--accent)}}
  .modal-btns{{display:flex;gap:10px;justify-content:flex-end}}

  /* ── Pagination ── */
  .pagination{{display:flex;gap:6px;margin-top:16px;flex-wrap:wrap}}
  .page-btn{{padding:6px 12px;border-radius:7px;background:var(--surface);
    border:1px solid var(--border);color:var(--muted);font-size:.85em}}
  .page-btn:hover,.page-btn.cur{{background:rgba(167,139,250,.15);color:var(--accent);
    border-color:rgba(167,139,250,.4)}}

  /* ── Alert ── */
  .alert{{padding:12px 16px;border-radius:10px;margin-bottom:16px;font-size:.9em}}
  .alert-success{{background:rgba(52,211,153,.1);border:1px solid rgba(52,211,153,.2);
    color:var(--green)}}
  .alert-error{{background:rgba(248,113,113,.1);border:1px solid rgba(248,113,113,.2);
    color:var(--red)}}

  /* ── Bottom nav (mobile only) ── */
  .bottom-nav{{display:none}}

  /* ── Mobile ── */
  @media(max-width:680px){{
    .sidebar{{display:none}}
    .bottom-nav{{
      display:flex;position:fixed;bottom:0;left:0;right:0;z-index:50;
      background:var(--surface);border-top:1px solid var(--border);
      padding:6px 0 env(safe-area-inset-bottom,6px)
    }}
    .bot-link{{
      flex:1;display:flex;flex-direction:column;align-items:center;
      gap:3px;padding:6px 4px;color:var(--muted);font-size:.7em;
      border-top:2px solid transparent;transition:.15s
    }}
    .bot-link>span:first-child{{font-size:1.4em;line-height:1}}
    .bot-link.active{{color:var(--accent);border-top-color:var(--accent)}}
    .bot-logout{{
      flex:1;display:flex;flex-direction:column;align-items:center;
      gap:3px;padding:6px 4px;color:var(--red);font-size:.7em
    }}
    .bot-logout>span:first-child{{font-size:1.4em;line-height:1}}
    .main{{padding:16px;padding-bottom:80px}}
    .page-title{{font-size:1.3em}}
    .cards{{grid-template-columns:repeat(2,1fr);gap:10px}}
    .card{{padding:14px}}
    .card-value{{font-size:1.5em}}
    .detail-grid{{grid-template-columns:repeat(2,1fr)}}
    .actions-row .btn{{font-size:.82em;padding:7px 12px}}
    .toolbar{{flex-direction:column;align-items:stretch}}
    .search-input{{width:100%;flex:none}}
    select{{width:100%;flex:none}}
  }}
</style>
</head>
<body>
<div class="layout">
  <nav class="sidebar">
    <div class="sidebar-logo">⚡ PicGenAI</div>
    {sidebar_nav}
    <div class="sidebar-bottom">
      <a href="/admin/logout" class="logout-btn">🚪 Выйти</a>
    </div>
  </nav>
  <main class="main">
    <div class="page-title">{title}</div>
    {content}
  </main>
</div>
<nav class="bottom-nav">
  {bottom_nav}
  <a href="/admin/logout" class="bot-logout">
    <span>🚪</span><span>Выйти</span>
  </a>
</nav>
</body>
</html>"""


# ─── Login ───────────────────────────────────────────────────────────────────

def _login_page_html(step: str, token: str, error: str) -> str:
    error_html = f'<div class="alert">{error}</div>' if error else ""
    if step == "2fa":
        subtitle = "Код отправлен в Telegram. Введите его ниже."
        form_body = f"""
    <label>6-значный код из Telegram</label>
    <input type="text" name="code" inputmode="numeric" pattern="[0-9]{{6}}"
      maxlength="6" placeholder="000000" autofocus autocomplete="one-time-code">
    <input type="hidden" name="tok" value="{token}">
    <button type="submit">Подтвердить</button>
    <a href="/admin/login" style="display:block;text-align:center;margin-top:14px;
      color:#8888a8;font-size:.85em">← Ввести пароль заново</a>"""
    else:
        subtitle = "Панель управления"
        form_body = """
    <label>Пароль администратора</label>
    <input type="password" name="password" placeholder="••••••••" autofocus>
    <button type="submit">Войти</button>"""

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Вход — PicGenAI Admin</title>
<style>
  *{{margin:0;padding:0;box-sizing:border-box}}
  body{{font-family:-apple-system,sans-serif;background:#08070e;color:#e4e4ef;
    min-height:100vh;display:flex;align-items:center;justify-content:center;padding:16px}}
  .box{{background:#0f0e1a;border:1px solid rgba(167,139,250,.2);border-radius:20px;
    padding:40px;width:100%;max-width:360px}}
  h1{{font-size:1.5em;margin-bottom:6px;background:linear-gradient(135deg,#a78bfa,#60a5fa);
    -webkit-background-clip:text;-webkit-text-fill-color:transparent}}
  .sub{{color:#8888a8;font-size:.9em;margin-bottom:24px}}
  label{{display:block;color:#8888a8;font-size:.82em;margin-bottom:6px}}
  input[type=password],input[type=text]{{width:100%;background:#08070e;
    border:1px solid rgba(167,139,250,.2);border-radius:10px;padding:11px 14px;
    color:#e4e4ef;font-size:.95em;margin-bottom:18px;outline:none;
    letter-spacing:.15em;text-align:center;font-size:1.3em}}
  input[type=password]{{letter-spacing:normal;font-size:.95em;text-align:left}}
  input:focus{{border-color:#a78bfa}}
  button{{width:100%;padding:12px;background:linear-gradient(135deg,#7c3aed,#6366f1);
    border:none;border-radius:10px;color:#fff;font-size:1em;font-weight:700;
    cursor:pointer;margin-bottom:4px}}
  button:hover{{opacity:.9}}
  .alert{{padding:10px 14px;border-radius:8px;margin-bottom:16px;font-size:.88em;
    background:rgba(248,113,113,.1);border:1px solid rgba(248,113,113,.2);color:#f87171}}
  .step-badge{{display:inline-block;padding:3px 10px;border-radius:20px;font-size:.78em;
    margin-bottom:18px;background:rgba(167,139,250,.1);color:#a78bfa;border:1px solid rgba(167,139,250,.2)}}
</style>
</head>
<body>
<div class="box">
  <h1>⚡ PicGenAI Admin</h1>
  <div class="sub">{subtitle}</div>
  <div class="step-badge">{"🔐 Шаг 2 из 2 — 2FA" if step == "2fa" else "🔑 Шаг 1 из 2 — Пароль"}</div>
  {error_html}
  <form method="post">
    {form_body}
  </form>
</div>
</body>
</html>"""


async def _send_2fa_code(code: str) -> bool:
    """Send 2FA code to admin Telegram ID. Returns True on success."""
    from bot.notify import _tg_bot
    if _tg_bot is None:
        logger.warning("2FA: TG bot not available, cannot send code")
        return False
    try:
        await _tg_bot.send_message(
            chat_id=_ADMIN_TG_ID,
            text=(
                f"🔐 <b>Код входа в Admin Panel</b>\n\n"
                f"<code>{code}</code>\n\n"
                f"Действителен 5 минут. Никому не сообщайте."
            ),
            parse_mode="HTML",
        )
        return True
    except Exception as exc:
        logger.warning("2FA send failed: %s", exc)
        return False


async def handle_login(request: web.Request) -> web.Response:
    if _is_auth(request):
        raise web.HTTPFound("/admin/dashboard")

    step = request.rel_url.query.get("step", "password")

    if request.method == "POST":
        data = await request.post()

        # ── Step 1: password ─────────────────────────────────
        if step == "password":
            pwd = data.get("password", "")
            if hmac.compare_digest(
                hashlib.sha256(pwd.encode()).hexdigest(),
                hashlib.sha256(_ADMIN_PASSWORD.encode()).hexdigest(),
            ):
                code = f"{random.randint(0, 999999):06d}"
                tok = secrets.token_urlsafe(24)
                _pending_2fa[tok] = (code, time.time() + _2FA_TTL)
                sent = await _send_2fa_code(code)
                if not sent:
                    # If TG unavailable fall back — skip 2FA and log in directly
                    logger.warning("2FA code send failed — skipping 2FA, logging in directly")
                    resp = web.HTTPFound("/admin/dashboard")
                    resp.set_cookie(_COOKIE_NAME, _make_token(), max_age=_COOKIE_MAX_AGE, httponly=True)
                    raise resp
                raise web.HTTPFound(f"/admin/login?step=2fa&tok={tok}")
            else:
                html = _login_page_html("password", "", "Неверный пароль")
                return web.Response(text=html, content_type="text/html")

        # ── Step 2: 2FA code ─────────────────────────────────
        if step == "2fa":
            tok = data.get("tok", "")
            entered = data.get("code", "").strip()
            entry = _pending_2fa.get(tok)
            if entry is None:
                html = _login_page_html("password", "", "Сессия истекла. Введите пароль снова.")
                return web.Response(text=html, content_type="text/html")
            correct_code, expires_at = entry
            if time.time() > expires_at:
                _pending_2fa.pop(tok, None)
                html = _login_page_html("password", "", "Код истёк. Войдите снова.")
                return web.Response(text=html, content_type="text/html")
            if hmac.compare_digest(entered, correct_code):
                _pending_2fa.pop(tok, None)
                resp = web.HTTPFound("/admin/dashboard")
                resp.set_cookie(_COOKIE_NAME, _make_token(), max_age=_COOKIE_MAX_AGE, httponly=True)
                raise resp
            else:
                html = _login_page_html("2fa", tok, "Неверный код. Попробуйте ещё раз.")
                return web.Response(text=html, content_type="text/html")

    # ── GET ──────────────────────────────────────────────────
    tok = request.rel_url.query.get("tok", "")
    html = _login_page_html(step, tok, "")
    return web.Response(text=html, content_type="text/html")


async def handle_logout(request: web.Request) -> web.Response:
    resp = web.HTTPFound("/admin/login")
    resp.del_cookie(_COOKIE_NAME)
    raise resp


async def handle_admin_root(request: web.Request) -> web.Response:
    if _is_auth(request):
        raise web.HTTPFound("/admin/dashboard")
    raise web.HTTPFound("/admin/login")


# ─── Dashboard ───────────────────────────────────────────────────────────────

@_require_auth
async def handle_dashboard(request: web.Request) -> web.Response:
    # Always read fresh data from DB for accurate stats
    if _db.is_available():
        fresh_users = _db.load_all_users()
        users = list(fresh_users.values())
        uid_map = {uid: u.get("first_name", str(uid)) for uid, u in fresh_users.items()}
    else:
        users = list(_users.values())
        uid_map = {uid: u.get("first_name", str(uid)) for uid, u in _users.items()}
    total_users = len(users)
    blocked_users = sum(1 for u in users if u.get("blocked"))
    tg_users = sum(1 for u in users if u.get("platform") == "tg")
    vk_users = sum(1 for u in users if u.get("platform") == "vk")
    total_gens = sum(u.get("generations_count", 0) for u in users)
    zero_credits = sum(1 for u in users if u.get("credits", 0) == 0)

    pstats = _db.get_payment_stats()
    revenue = pstats.get("total_revenue", 0)
    paid_count = pstats.get("success_count", 0)

    recent_payments = _db.get_all_payments(limit=10)

    payments_rows = ""
    for p in recent_payments:
        uid = p["user_id"]
        name = uid_map.get(uid, str(uid))
        status_badge = (
            '<span class="badge badge-green">✓ успешно</span>' if p["status"] == "success"
            else '<span class="badge badge-yellow">⏳ ожидание</span>'
        )
        dt = _msk(p["created_at"])
        payments_rows += f"""<tr>
          <td><a href="/admin/users/{uid}">{name}</a></td>
          <td>{p['pack_key']}</td>
          <td style="color:#34d399;font-weight:600">{p['amount']:.0f}₽</td>
          <td>{status_badge}</td>
          <td style="color:#8888a8">{dt}</td>
        </tr>"""

    if not payments_rows:
        payments_rows = '<tr><td colspan="5" style="color:#8888a8;text-align:center;padding:20px">Нет платежей</td></tr>'

    content = f"""
<div class="cards">
  <div class="card">
    <div class="card-label">Всего пользователей</div>
    <div class="card-value purple">{total_users}</div>
  </div>
  <div class="card">
    <div class="card-label">Выручка (успешные)</div>
    <div class="card-value green">{revenue:.0f}₽</div>
  </div>
  <div class="card">
    <div class="card-label">Успешных платежей</div>
    <div class="card-value blue">{paid_count}</div>
  </div>
  <div class="card">
    <div class="card-label">Всего генераций</div>
    <div class="card-value yellow">{total_gens:,}</div>
  </div>
  <div class="card">
    <div class="card-label">Заблокированных</div>
    <div class="card-value red">{blocked_users}</div>
  </div>
  <div class="card">
    <div class="card-label">Без кредитов</div>
    <div class="card-value red">{zero_credits}</div>
  </div>
</div>

<div class="cards" style="grid-template-columns:repeat(auto-fit,minmax(140px,1fr))">
  <div class="card">
    <div class="card-label">Telegram</div>
    <div class="card-value blue">{tg_users}</div>
  </div>
  <div class="card">
    <div class="card-label">ВКонтакте</div>
    <div class="card-value" style="color:#4c75a3">{vk_users}</div>
  </div>
  <div class="card">
    <div class="card-label">Другие</div>
    <div class="card-value" style="color:#8888a8">{total_users - tg_users - vk_users}</div>
  </div>
</div>

<div class="section-heading">Последние платежи</div>
<div class="table-wrap">
<table>
  <thead><tr>
    <th>Пользователь</th><th>Пакет</th><th>Сумма</th><th>Статус</th><th>Дата</th>
  </tr></thead>
  <tbody>{payments_rows}</tbody>
</table>
</div>
<div style="margin-top:12px">
  <a href="/admin/payments" class="btn btn-muted btn-sm">Все платежи →</a>
</div>
"""
    return web.Response(text=_layout("Дашборд", content, "dashboard"), content_type="text/html")


# ─── Users list ──────────────────────────────────────────────────────────────

@_require_auth
async def handle_users(request: web.Request) -> web.Response:
    q = request.rel_url.query.get("q", "").strip().lower()
    sort = request.rel_url.query.get("sort", "gens")
    order = request.rel_url.query.get("order", "desc")
    page = max(1, int(request.rel_url.query.get("page", 1)))
    filter_blocked = request.rel_url.query.get("blocked", "")
    filter_platform = request.rel_url.query.get("platform", "")

    # Always read fresh data from DB so list is always accurate
    if _db.is_available():
        db_users = _db.load_all_users()
        users_list = [(uid, u) for uid, u in db_users.items()]
    else:
        users_list = [(uid, u) for uid, u in _users.items()]

    if q:
        users_list = [
            (uid, u) for uid, u in users_list
            if q in str(uid) or q in u.get("first_name", "").lower()
        ]
    if filter_blocked == "1":
        users_list = [(uid, u) for uid, u in users_list if u.get("blocked")]
    elif filter_blocked == "0":
        users_list = [(uid, u) for uid, u in users_list if not u.get("blocked")]
    if filter_platform in ("tg", "vk"):
        users_list = [(uid, u) for uid, u in users_list if u.get("platform") == filter_platform]

    sort_keys = {
        "gens":    lambda x: x[1].get("generations_count", 0),
        "credits": lambda x: x[1].get("credits", 0),
        "name":    lambda x: (x[1].get("first_name") or "").lower(),
        "id":      lambda x: x[0],
    }
    sort_fn = sort_keys.get(sort, sort_keys["gens"])
    reverse = (order == "desc")
    users_list.sort(key=sort_fn, reverse=reverse)

    total = len(users_list)
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = min(page, total_pages)
    offset = (page - 1) * _PAGE_SIZE
    page_users = users_list[offset: offset + _PAGE_SIZE]

    col_labels = {
        "id": "ID",
        "name": "Имя",
        "credits": "Кредиты",
        "gens": "Генераций",
    }

    def sort_link(s, label):
        is_active = sort == s
        new_order = "asc" if (is_active and order == "desc") else "desc"
        arrow = (" ▼" if order == "desc" else " ▲") if is_active else ""
        color = "var(--accent)" if is_active else "var(--muted)"
        href = f"?q={q}&sort={s}&order={new_order}&blocked={filter_blocked}&platform={filter_platform}"
        return (
            f'<a href="{href}" style="color:{color};cursor:pointer;'
            f'font-weight:{"700" if is_active else "600"};'
            f'text-decoration:none;white-space:nowrap">'
            f'{label}{arrow}</a>'
        )

    rows = ""
    for uid, u in page_users:
        name = u.get("first_name") or "—"
        credits_ = u.get("credits", 0)
        gens = u.get("generations_count", 0)
        platform = u.get("platform", "")
        blocked = u.get("blocked", False)

        plat_badge = (
            '<span class="badge badge-blue">TG</span>' if platform == "tg"
            else '<span class="badge" style="background:rgba(76,117,163,.15);color:#4c75a3">VK</span>' if platform == "vk"
            else '<span class="badge badge-yellow">—</span>'
        )
        blocked_badge = (
            '<span class="badge badge-red">🚫 Блок</span>' if blocked
            else '<span class="badge badge-green">✓ Активен</span>'
        )
        cred_color = "var(--red)" if credits_ == 0 else "var(--green)" if credits_ > 50 else "var(--yellow)"

        rows += f"""<tr>
          <td style="font-family:monospace;color:var(--muted);font-size:.85em">{uid}</td>
          <td><a href="/admin/users/{uid}" style="color:var(--text);font-weight:500">{name}</a></td>
          <td>{plat_badge}</td>
          <td style="color:{cred_color};font-weight:600">{credits_}</td>
          <td style="color:var(--muted)">{gens}</td>
          <td>{blocked_badge}</td>
          <td>
            <a href="/admin/users/{uid}" class="btn btn-muted btn-sm">Открыть</a>
          </td>
        </tr>"""

    if not rows:
        rows = '<tr><td colspan="7" style="color:#8888a8;text-align:center;padding:20px">Пользователи не найдены</td></tr>'

    def page_url(p):
        return f"?q={q}&sort={sort}&order={order}&blocked={filter_blocked}&platform={filter_platform}&page={p}"

    pages_html = ""
    for p in range(1, total_pages + 1):
        if abs(p - page) <= 3 or p == 1 or p == total_pages:
            cls = "page-btn cur" if p == page else "page-btn"
            pages_html += f'<a href="{page_url(p)}" class="{cls}">{p}</a>'
        elif abs(p - page) == 4:
            pages_html += '<span style="color:var(--muted);padding:6px">…</span>'

    filter_opts = f"""
<form method="get" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
  <input class="search-input" name="q" value="{q}" placeholder="Поиск по имени или ID">
  <select name="platform">
    <option value="" {"selected" if not filter_platform else ""}>Все платформы</option>
    <option value="tg" {"selected" if filter_platform=="tg" else ""}>Telegram</option>
    <option value="vk" {"selected" if filter_platform=="vk" else ""}>ВКонтакте</option>
  </select>
  <select name="blocked">
    <option value="" {"selected" if filter_blocked=="" else ""}>Все статусы</option>
    <option value="0" {"selected" if filter_blocked=="0" else ""}>Активные</option>
    <option value="1" {"selected" if filter_blocked=="1" else ""}>Заблокированные</option>
  </select>
  <input type="hidden" name="sort" value="{sort}">
  <input type="hidden" name="order" value="{order}">
  <button type="submit" class="btn btn-primary">Найти</button>
  <a href="/admin/users" class="btn btn-muted">Сбросить</a>
</form>"""

    content = f"""
<div style="margin-bottom:6px;color:var(--muted);font-size:.9em">
  Найдено: <strong style="color:var(--text)">{total}</strong> пользователей
  &nbsp;·&nbsp; Сортировка: <strong style="color:var(--accent)">{col_labels.get(sort, sort)}</strong>
  {"▼" if order == "desc" else "▲"}
</div>
<div class="toolbar">{filter_opts}</div>
<div class="table-wrap">
<table>
  <thead><tr>
    <th>{sort_link("id", "ID")}</th>
    <th>{sort_link("name", "Имя")}</th>
    <th>Платформа</th>
    <th>{sort_link("credits", "Кредиты")}</th>
    <th>{sort_link("gens", "Генераций")}</th>
    <th>Статус</th>
    <th></th>
  </tr></thead>
  <tbody>{rows}</tbody>
</table>
</div>
<div class="pagination">{pages_html}</div>
"""
    return web.Response(text=_layout(f"Пользователи ({total})", content, "users"), content_type="text/html")


_IMG_PAGE_SIZE = 10


def _render_image_gallery(image_logs: list[dict], page: int = 1) -> str:
    """Build the image list (compact table) for a user's generated images."""
    try:
        if not image_logs:
            return '<div class="img-empty">Генераций пока нет</div>'

        total = len(image_logs)
        start = (page - 1) * _IMG_PAGE_SIZE
        end = start + _IMG_PAGE_SIZE
        page_items = image_logs[start:end]
        has_next = end < total
        has_prev = page > 1

        rows = ""
        for img in page_items:
            fuid = img["file_unique_id"]
            prompt_esc = (img.get("prompt") or "").replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")
            dt = _msk(img.get("created_at", ""))
            plat = "📱" if img.get("platform") == "tg" else "💙"
            mdl = (img.get("model") or "").split("-")[0] or "—"
            # Use data-* attributes to avoid any JS-injection via prompt text
            rows += (
                f'<tr>'
                f'<td style="width:56px;padding:6px 8px">'
                f'<img src="/admin/tg-photo/{fuid}" loading="lazy"'
                f' data-fuid="{fuid}" data-dt="{dt}"'
                f' style="width:48px;height:48px;object-fit:cover;border-radius:6px;cursor:pointer;display:block"'
                f' onclick="openImg(this)" onerror="this.style.opacity=\'.3\'"></td>'
                f'<td style="font-size:.85em;max-width:320px;word-break:break-word;padding:6px 4px">{plat} {prompt_esc[:120]}</td>'
                f'<td style="white-space:nowrap;color:var(--muted);font-size:.78em;padding:6px 8px">{dt}</td>'
                f'<td style="white-space:nowrap;font-size:.78em;color:#8888a8;padding:6px 8px">{mdl}</td>'
                f'</tr>'
            )

        pagination = ""
        if has_prev or has_next:
            pagination = '<div style="display:flex;gap:8px;margin-top:10px">'
            if has_prev:
                pagination += f'<button class="btn btn-muted btn-sm" onclick="loadImgPage({page-1})">← Назад</button>'
            pagination += f'<span style="align-self:center;color:var(--muted);font-size:.85em">{start+1}–{min(end,total)} из {total}</span>'
            if has_next:
                pagination += f'<button class="btn btn-muted btn-sm" onclick="loadImgPage({page+1})">Далее →</button>'
            pagination += '</div>'

        # Safely embed JSON — escape </script> to prevent tag injection
        safe_json = json.dumps(image_logs, ensure_ascii=False).replace("</", "<\\/")

        return (
            '<div class="table-wrap" id="img-table-wrap"><table>'
            '<thead><tr><th style="width:56px">Фото</th><th>Промпт</th><th>Дата</th><th>Модель</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div>'
            f'<div id="img-pagination">{pagination}</div>'
            '<div class="lightbox" id="lightbox" onclick="closeLightbox()">'
            '<span class="lightbox-close" onclick="closeLightbox()">×</span>'
            '<img id="lightbox-img" src="" alt="">'
            '<div class="lightbox-caption" id="lightbox-cap"></div></div>'
            f'<script>var _imgData={safe_json};var _pgSz={_IMG_PAGE_SIZE};'
            'function toMsk(s){if(!s)return"—";var d=new Date(s.endsWith("Z")?s:s+"Z");d.setHours(d.getHours()+3);'
            'var p=function(n){return String(n).padStart(2,"0")};'
            'return p(d.getDate())+"."+p(d.getMonth()+1)+"."+d.getFullYear()+" "+p(d.getHours())+":"+p(d.getMinutes());}'
            'function openLightbox(src,cap){document.getElementById("lightbox-img").src=src;'
            'document.getElementById("lightbox-cap").textContent=cap;document.getElementById("lightbox").classList.add("open");}'
            'function openImg(el){openLightbox("/admin/tg-photo/"+el.dataset.fuid,(el.closest("tr").querySelector("td:nth-child(2)").textContent.trim())+" · "+el.dataset.dt);}'
            'function closeLightbox(){document.getElementById("lightbox").classList.remove("open");document.getElementById("lightbox-img").src="";}'
            'document.addEventListener("keydown",function(e){if(e.key==="Escape")closeLightbox();});'
            'function loadImgPage(pg){'
            'var s=(pg-1)*_pgSz,e=s+_pgSz,items=_imgData.slice(s,e);'
            'var html=items.map(function(img){'
            'var fuid=img.file_unique_id,dt=toMsk(img.created_at),plat=img.platform==="tg"?"📱":"💙";'
            'var pr=(img.prompt||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/"/g,"&quot;").slice(0,120);'
            'var mdl=(img.model||"").split("-")[0]||"—";'
            'return"<tr><td style=\\"width:56px;padding:6px 8px\\"><img src=\\"/admin/tg-photo/"+fuid+"\\" loading=\\"lazy\\"'
            ' data-fuid=\\""+fuid+"\\" data-dt=\\""+dt+"\\"'
            ' style=\\"width:48px;height:48px;object-fit:cover;border-radius:6px;cursor:pointer;display:block\\"'
            ' onclick=\\"openImg(this)\\" onerror=\\"this.style.opacity=.3\\"></td>'
            '<td style=\\"font-size:.85em;max-width:320px;word-break:break-word;padding:6px 4px\\">"+plat+" "+pr+"</td>'
            '<td style=\\"white-space:nowrap;color:var(--muted);font-size:.78em;padding:6px 8px\\">"+dt+"</td>'
            '<td style=\\"white-space:nowrap;font-size:.78em;color:#8888a8;padding:6px 8px\\">"+mdl+"</td></tr>";'
            '}).join("");'
            'document.querySelector("#img-table-wrap tbody").innerHTML=html;'
            'var tot=_imgData.length,hP=pg>1,hN=e<tot,pg2=document.getElementById("img-pagination"),h="";'
            'if(hP||hN){h="<div style=\\"display:flex;gap:8px;margin-top:10px\\">";'
            'if(hP)h+="<button class=\\"btn btn-muted btn-sm\\" onclick=\\"loadImgPage("+(pg-1)+")\\">← Назад</button>";'
            'h+="<span style=\\"align-self:center;color:var(--muted);font-size:.85em\\">"+(s+1)+"–"+Math.min(e,tot)+" из "+tot+"</span>";'
            'if(hN)h+="<button class=\\"btn btn-muted btn-sm\\" onclick=\\"loadImgPage("+(pg+1)+")\\">Далее →</button>";'
            'h+="</div>";}if(pg2)pg2.innerHTML=h;}'
            '</script>'
        )
    except Exception as exc:
        logger.warning("_render_image_gallery error: %s", exc)
        return '<div class="img-empty">Ошибка при загрузке галереи</div>'


# ─── User detail ─────────────────────────────────────────────────────────────

@_require_auth
async def handle_user_detail(request: web.Request) -> web.Response:
    try:
        uid = int(request.match_info["uid"])
    except (ValueError, KeyError):
        raise web.HTTPNotFound()

    msg = request.rel_url.query.get("msg", "")

    try:
        # Always load fresh data from DB for accurate profile display
        if _db.is_available():
            db_data = _db.load_one_user(uid)
            if db_data is None:
                raise web.HTTPNotFound()
            # Merge DB data over in-memory defaults so we always have all keys
            u = {**get_user_settings(uid), **db_data}
            # Sync in-memory dict with fresh DB data
            get_user_settings(uid).update(db_data)
        else:
            u = get_user_settings(uid)
            if uid not in _users:
                raise web.HTTPNotFound()
        payments = _db.get_user_payments(uid)
        image_logs = _db.get_user_image_logs(uid, limit=60)
    except web.HTTPNotFound:
        raise
    except Exception as exc:
        logger.error("handle_user_detail db error uid=%s: %s", uid, exc)
        err_content = f'<div class="alert alert-error">Ошибка загрузки данных пользователя: {exc}</div><a href="/admin/users">← Назад</a>'
        return web.Response(text=_layout("Ошибка", err_content, "users"), content_type="text/html")

    name = u.get("first_name") or f"Пользователь {uid}"
    platform = u.get("platform", "—")
    credits_ = u.get("credits", 0)
    gens = u.get("generations_count", 0)
    blocked = u.get("blocked", False)
    model = u.get("model", "—")

    plat_badge = (
        '<span class="badge badge-blue">Telegram</span>' if platform == "tg"
        else '<span class="badge" style="background:rgba(76,117,163,.15);color:#4c75a3">ВКонтакте</span>' if platform == "vk"
        else f'<span class="badge badge-yellow">{platform}</span>'
    )
    blocked_badge = (
        '<span class="badge badge-red">🚫 Заблокирован</span>' if blocked
        else '<span class="badge badge-green">✓ Активен</span>'
    )
    cred_color = "var(--red)" if credits_ == 0 else "var(--green)" if credits_ > 50 else "var(--yellow)"

    total_paid = sum(p["amount"] for p in payments if p["status"] == "success")

    alert_html = ""
    if msg == "credits_ok":
        alert_html = '<div class="alert alert-success">✓ Кредиты обновлены</div>'
    elif msg == "block_ok":
        alert_html = '<div class="alert alert-success">✓ Статус блокировки изменён</div>'
    elif msg == "gens_ok":
        alert_html = '<div class="alert alert-success">✓ Счётчик генераций сброшен</div>'
    elif msg == "delete_ok":
        return web.HTTPFound("/admin/users?msg=deleted")
    elif msg == "err":
        alert_html = '<div class="alert alert-error">Ошибка при выполнении действия</div>'

    pay_rows = ""
    for p in payments:
        status_badge = (
            '<span class="badge badge-green">✓ успешно</span>' if p["status"] == "success"
            else '<span class="badge badge-yellow">⏳ ожидание</span>'
        )
        dt = _msk(p["created_at"])
        pay_rows += f"""<tr>
          <td style="font-family:monospace;font-size:.82em;color:var(--muted)">{p['order_id'][:16]}…</td>
          <td>{p['pack_key']}</td>
          <td style="color:var(--green);font-weight:600">{p['amount']:.0f}₽</td>
          <td>{status_badge}</td>
          <td style="color:var(--muted)">{dt}</td>
        </tr>"""
    if not pay_rows:
        pay_rows = '<tr><td colspan="5" style="color:#8888a8;text-align:center;padding:16px">Нет платежей</td></tr>'

    block_btn_class = "btn-success" if blocked else "btn-danger"
    block_btn_text = "Разблокировать" if blocked else "Заблокировать"

    try:
        content = f"""
{alert_html}
<div style="margin-bottom:16px">
  <a href="/admin/users" style="color:var(--muted);font-size:.9em">← Все пользователи</a>
</div>

<div class="detail-grid">
  <div class="detail-card">
    <div class="detail-card-label">Имя</div>
    <div class="detail-card-value">{name}</div>
  </div>
  <div class="detail-card">
    <div class="detail-card-label">ID</div>
    <div class="detail-card-value" style="font-family:monospace">{uid}</div>
  </div>
  <div class="detail-card">
    <div class="detail-card-label">Платформа</div>
    <div class="detail-card-value">{plat_badge}</div>
  </div>
  <div class="detail-card">
    <div class="detail-card-label">Кредиты</div>
    <div class="detail-card-value" style="color:{cred_color}">{credits_}</div>
  </div>
  <div class="detail-card">
    <div class="detail-card-label">Генераций</div>
    <div class="detail-card-value" style="color:var(--accent2)">{gens}</div>
  </div>
  <div class="detail-card">
    <div class="detail-card-label">Итого оплачено</div>
    <div class="detail-card-value" style="color:var(--green)">{total_paid:.0f}₽</div>
  </div>
  <div class="detail-card">
    <div class="detail-card-label">Статус</div>
    <div class="detail-card-value">{blocked_badge}</div>
  </div>
  <div class="detail-card">
    <div class="detail-card-label">Модель</div>
    <div class="detail-card-value" style="font-size:.9em">{model.split("-")[0]}…</div>
  </div>
</div>

<div class="section-heading">Управление</div>
<div class="actions-row">
  <button class="btn btn-primary" onclick="openModal('credits-modal')">💰 Изменить кредиты</button>
  <button class="btn {block_btn_class}" onclick="doAction('block')">{block_btn_text}</button>
  <button class="btn btn-muted" onclick="doAction('reset_gens')">🔄 Сбросить генерации</button>
  <button class="btn btn-danger" onclick="confirmDelete()">🗑 Удалить пользователя</button>
</div>

<div class="section-heading">Генерации ({len(image_logs)})</div>
{_render_image_gallery(image_logs)}

<div class="section-heading">История платежей ({len(payments)})</div>
<div class="table-wrap">
<table>
  <thead><tr>
    <th>Order ID</th><th>Пакет</th><th>Сумма</th><th>Статус</th><th>Дата</th>
  </tr></thead>
  <tbody>{pay_rows}</tbody>
</table>
</div>

<!-- Credits modal -->
<div class="modal-overlay" id="credits-modal">
  <div class="modal">
    <h3>💰 Изменить кредиты</h3>
    <p id="mode-label" style="color:var(--muted);font-size:.85em;margin-bottom:6px">Режим: <b>Добавить к текущему</b></p>
    <div style="display:flex;gap:8px;margin-bottom:14px">
      <button class="btn btn-primary btn-sm" id="mode-add" onclick="setMode('add')" style="flex:1">➕ Добавить</button>
      <button class="btn btn-muted btn-sm" id="mode-set" onclick="setMode('set')" style="flex:1">✏️ Установить</button>
    </div>
    <input type="number" id="credits-amount" placeholder="Количество кредитов" min="0">
    <div class="modal-btns">
      <button class="btn btn-muted" onclick="closeModal('credits-modal')">Отмена</button>
      <button class="btn btn-primary" onclick="applyCredits()">Применить</button>
    </div>
  </div>
</div>

<script>
  const UID = {uid};
  let credMode = 'add';

  function setMode(m) {{
    credMode = m;
    document.getElementById('mode-add').className = m==='add' ? 'btn btn-primary btn-sm' : 'btn btn-muted btn-sm';
    document.getElementById('mode-set').className = m==='set' ? 'btn btn-primary btn-sm' : 'btn btn-muted btn-sm';
    document.getElementById('mode-label').innerHTML =
      m === 'add'
        ? 'Режим: <b>Добавить к текущему</b>'
        : 'Режим: <b>Установить точное значение</b>';
  }}
  function openModal(id) {{
    if (id === 'credits-modal') {{
      // Reset to default state every time modal opens
      credMode = 'add';
      setMode('add');
      document.getElementById('credits-amount').value = '';
    }}
    document.getElementById(id).classList.add('open');
  }}
  function closeModal(id) {{ document.getElementById(id).classList.remove('open'); }}

  async function applyCredits() {{
    const amount = parseInt(document.getElementById('credits-amount').value);
    if (isNaN(amount) || amount < 0) return alert('Введите корректное число');
    const r = await fetch('/admin/api/users/' + UID + '/credits', {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{action: credMode, amount}})
    }});
    const d = await r.json();
    if (d.ok) location.href = '/admin/users/' + UID + '?msg=credits_ok';
    else alert('Ошибка: ' + (d.error || 'неизвестная'));
  }}

  async function doAction(action) {{
    const labels = {{block:'Изменить статус блокировки?', reset_gens:'Сбросить счётчик генераций?'}};
    if (!confirm(labels[action] || 'Выполнить?')) return;
    const r = await fetch('/admin/api/users/' + UID + '/' + action, {{method:'POST'}});
    const d = await r.json();
    if (d.ok) location.href = '/admin/users/' + UID + '?msg=' + action + '_ok';
    else alert('Ошибка: ' + (d.error || 'неизвестная'));
  }}

  function confirmDelete() {{
    if (!confirm('УДАЛИТЬ пользователя {uid}? Это действие необратимо!')) return;
    if (!confirm('Вы уверены? Данные будут удалены.')) return;
    fetch('/admin/api/users/' + UID + '/delete', {{method:'POST'}})
      .then(r => r.json()).then(d => {{
        if (d.ok) location.href = '/admin/users';
        else alert('Ошибка: ' + (d.error || 'неизвестная'));
      }});
  }}
</script>
"""
        return web.Response(text=_layout(f"👤 {name}", content, "users"), content_type="text/html")
    except Exception as exc:
        logger.error("handle_user_detail render error uid=%s: %s", uid, exc, exc_info=True)
        err_content = f'<div class="alert alert-error">Ошибка рендеринга страницы пользователя. Обратитесь к разработчику.<br><code>{exc}</code></div><a href="/admin/users">← Назад</a>'
        return web.Response(text=_layout("Ошибка", err_content, "users"), content_type="text/html")


# ─── Payments ────────────────────────────────────────────────────────────────

@_require_auth
async def handle_payments(request: web.Request) -> web.Response:
    status_filter = request.rel_url.query.get("status", "")
    q = request.rel_url.query.get("q", "").strip().lower()
    page = max(1, int(request.rel_url.query.get("page", 1)))

    payments = _db.get_all_payments(limit=2000)

    if status_filter in ("success", "pending"):
        payments = [p for p in payments if p["status"] == status_filter]
    if q:
        payments = [
            p for p in payments
            if q in str(p["user_id"]) or q in p["pack_key"].lower()
            or q in p["order_id"].lower()
        ]

    total = len(payments)
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = min(page, total_pages)
    offset = (page - 1) * _PAGE_SIZE
    page_payments = payments[offset: offset + _PAGE_SIZE]

    uid_map = {uid: u.get("first_name", str(uid)) for uid, u in _users.items()}
    total_revenue = sum(p["amount"] for p in payments if p["status"] == "success")

    rows = ""
    for p in page_payments:
        uid = p["user_id"]
        name = uid_map.get(uid, str(uid))
        status_badge = (
            '<span class="badge badge-green">✓ успешно</span>' if p["status"] == "success"
            else '<span class="badge badge-yellow">⏳ ожидание</span>'
        )
        dt = _msk(p["created_at"])
        rows += f"""<tr>
          <td style="font-family:monospace;font-size:.82em;color:var(--muted)">{p['order_id'][:18]}…</td>
          <td><a href="/admin/users/{uid}">{name}</a></td>
          <td>{p['pack_key']}</td>
          <td style="color:var(--green);font-weight:600">{p['amount']:.0f}₽</td>
          <td>{status_badge}</td>
          <td style="color:var(--muted)">{dt}</td>
        </tr>"""

    if not rows:
        rows = '<tr><td colspan="6" style="color:#8888a8;text-align:center;padding:20px">Платежей не найдено</td></tr>'

    def page_url(p):
        return f"?q={q}&status={status_filter}&page={p}"

    pages_html = ""
    for p in range(1, total_pages + 1):
        if abs(p - page) <= 3 or p == 1 or p == total_pages:
            cls = "page-btn cur" if p == page else "page-btn"
            pages_html += f'<a href="{page_url(p)}" class="{cls}">{p}</a>'
        elif abs(p - page) == 4:
            pages_html += '<span style="color:var(--muted);padding:6px">…</span>'

    content = f"""
<div class="cards" style="grid-template-columns:repeat(auto-fit,minmax(160px,1fr));margin-bottom:20px">
  <div class="card">
    <div class="card-label">Всего записей</div>
    <div class="card-value purple">{total}</div>
  </div>
  <div class="card">
    <div class="card-label">Итого выручка</div>
    <div class="card-value green">{total_revenue:.0f}₽</div>
  </div>
</div>

<form method="get" class="toolbar">
  <input class="search-input" name="q" value="{q}" placeholder="Order ID, user ID или пакет">
  <select name="status" style="background:var(--surface);border:1px solid var(--border);
    border-radius:8px;padding:8px 12px;color:var(--text);font-size:.9em">
    <option value="" {"selected" if not status_filter else ""}>Все статусы</option>
    <option value="success" {"selected" if status_filter=="success" else ""}>Успешные</option>
    <option value="pending" {"selected" if status_filter=="pending" else ""}>Ожидание</option>
  </select>
  <button type="submit" class="btn btn-primary">Найти</button>
  <a href="/admin/payments" class="btn btn-muted">Сбросить</a>
</form>

<div class="table-wrap">
<table>
  <thead><tr>
    <th>Order ID</th><th>Пользователь</th><th>Пакет</th>
    <th>Сумма</th><th>Статус</th><th>Дата</th>
  </tr></thead>
  <tbody>{rows}</tbody>
</table>
</div>
<div class="pagination">{pages_html}</div>
"""
    return web.Response(text=_layout(f"Платежи ({total})", content, "payments"), content_type="text/html")


# ─── API endpoints ───────────────────────────────────────────────────────────

def _api_require_auth(fn):
    async def wrapper(request: web.Request):
        if not _is_auth(request):
            return web.Response(text=json.dumps({"ok": False, "error": "unauthorized"}),
                                content_type="application/json", status=401)
        return await fn(request)
    return wrapper


@_api_require_auth
async def api_credits(request: web.Request) -> web.Response:
    try:
        uid = int(request.match_info["uid"])
        data = await request.json()
        action = data.get("action", "add")
        amount = int(data.get("amount", 0))
        if amount < 0:
            raise ValueError("negative")
        if action == "add":
            new_val = add_credits(uid, amount)
        else:
            new_val = set_credits(uid, amount)
        return web.Response(
            text=json.dumps({"ok": True, "credits": new_val}),
            content_type="application/json"
        )
    except Exception as e:
        logger.exception("api_credits error")
        return web.Response(
            text=json.dumps({"ok": False, "error": str(e)}),
            content_type="application/json", status=400
        )


@_api_require_auth
async def api_block(request: web.Request) -> web.Response:
    try:
        uid = int(request.match_info["uid"])
        u = get_user_settings(uid)
        new_blocked = not u.get("blocked", False)
        set_blocked(uid, new_blocked)
        return web.Response(
            text=json.dumps({"ok": True, "blocked": new_blocked}),
            content_type="application/json"
        )
    except Exception as e:
        logger.exception("api_block error")
        return web.Response(
            text=json.dumps({"ok": False, "error": str(e)}),
            content_type="application/json", status=400
        )


@_api_require_auth
async def api_reset_gens(request: web.Request) -> web.Response:
    try:
        uid = int(request.match_info["uid"])
        reset_generations(uid)
        return web.Response(
            text=json.dumps({"ok": True}),
            content_type="application/json"
        )
    except Exception as e:
        logger.exception("api_reset_gens error")
        return web.Response(
            text=json.dumps({"ok": False, "error": str(e)}),
            content_type="application/json", status=400
        )


@_api_require_auth
async def api_delete(request: web.Request) -> web.Response:
    try:
        uid = int(request.match_info["uid"])
        ok = delete_user(uid)
        return web.Response(
            text=json.dumps({"ok": ok}),
            content_type="application/json"
        )
    except Exception as e:
        logger.exception("api_delete error")
        return web.Response(
            text=json.dumps({"ok": False, "error": str(e)}),
            content_type="application/json", status=400
        )


_TG_TOKEN_FOR_PROXY = os.getenv("TELEGRAM_BOT_TOKEN", "")


@_require_auth
async def handle_tg_photo(request: web.Request) -> web.Response:
    """Proxy a Telegram photo by file_unique_id so it shows in the browser."""
    file_unique_id = request.match_info.get("file_unique_id", "")
    if not file_unique_id or not _TG_TOKEN_FOR_PROXY:
        raise web.HTTPNotFound()
    row = _db.get_image_log_by_unique_id(file_unique_id)
    if not row:
        raise web.HTTPNotFound()
    file_id = row["file_id"]
    try:
        async with _aiohttp.ClientSession() as session:
            # Step 1: get file path
            gf_url = f"https://api.telegram.org/bot{_TG_TOKEN_FOR_PROXY}/getFile"
            async with session.get(gf_url, params={"file_id": file_id},
                                   timeout=_aiohttp.ClientTimeout(total=10)) as resp:
                gf = await resp.json()
            if not gf.get("ok"):
                raise web.HTTPNotFound()
            file_path = gf["result"]["file_path"]
            # Step 2: download the file
            dl_url = f"https://api.telegram.org/file/bot{_TG_TOKEN_FOR_PROXY}/{file_path}"
            async with session.get(dl_url, timeout=_aiohttp.ClientTimeout(total=30)) as dl:
                img_bytes = await dl.read()
        return web.Response(
            body=img_bytes,
            content_type="image/jpeg",
            headers={"Cache-Control": "max-age=86400"},
        )
    except web.HTTPException:
        raise
    except Exception as exc:
        logger.warning("handle_tg_photo failed for %s: %s", file_unique_id, exc)
        raise web.HTTPBadGateway()


@_api_require_auth
async def api_test_log_channel(request: web.Request) -> web.Response:
    """Send a test image to the log channel to verify configuration."""
    import io
    from bot.log_channel import log_generation, LOG_CHANNEL_ID
    from bot.notify import _tg_bot
    # Create a minimal 1x1 white JPEG for test
    try:
        test_bytes = (
            b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00'
            b'\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c'
            b'\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c'
            b'\x1c $.\' ",#\x1c\x1c(7),01444\x1f\'9=82<.342\x1e\x14\x1c\x1c '
            b'.....\x00\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4'
            b'\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00'
            b'\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xc4\x00\xb5'
            b'\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x04\x00\x00\x01}\x01'
            b'\x02\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa\x07"q\x142\x81\x91\xa1\x08'
            b'#B\xb1\xc1\x15R\xd1\xf0$3br\x82\t\n\x16\x17\x18\x19\x1a%&\'()*'
            b'456789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x83\x84\x85\x86\x87\x88\x89'
            b'\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8'
            b'\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7'
            b'\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe1\xe2\xe3\xe4\xe5'
            b'\xe6\xe7\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xda'
            b'\x00\x08\x01\x01\x00\x00?\x00\xfb\xd4P\x00\x00\x00\x1f\xff\xd9'
        )
        bot_ok = _tg_bot is not None
        await log_generation(
            image_bytes=test_bytes,
            prompt="TEST: диагностика канала логов",
            user_id=0,
            user_name="admin_test",
            platform="tg",
            model="test",
        )
        return web.Response(
            text=json.dumps({
                "ok": True,
                "bot_initialized": bot_ok,
                "channel_id": LOG_CHANNEL_ID,
                "note": "Тест отправлен — проверь канал. Если не пришло, смотри WARNING в логах."
            }),
            content_type="application/json"
        )
    except Exception as e:
        logger.exception("api_test_log_channel error")
        return web.Response(
            text=json.dumps({"ok": False, "error": str(e)}),
            content_type="application/json", status=500
        )


# ─── API Keys management ──────────────────────────────────────────────────────

@_require_auth
async def handle_api_keys(request: web.Request) -> web.Response:
    msg = request.rel_url.query.get("msg", "")

    stored_keys = _key_store.get_all_keys()
    total = len(stored_keys)

    msg_html = ""
    if msg == "added":
        msg_html = '<div class="alert alert-success">✅ Ключ добавлен. Перезапустите сервис чтобы он вступил в силу.</div>'
    elif msg == "exists":
        msg_html = '<div class="alert alert-error">⚠️ Такой ключ уже есть.</div>'
    elif msg == "deleted":
        msg_html = '<div class="alert alert-success">🗑 Ключ удалён. Перезапустите сервис для применения.</div>'
    elif msg == "empty":
        msg_html = '<div class="alert alert-error">⚠️ Введите непустой ключ.</div>'

    # Build static rows — JS will fill in live status cells
    key_rows = ""
    for i, key in enumerate(stored_keys):
        masked = _key_store.mask_key(key)
        key_rows += f"""<tr id="key-row-{i}">
  <td style="font-weight:600;color:var(--muted);width:36px">{i+1}</td>
  <td><code style="font-size:.88em;color:var(--accent)">{masked}</code></td>
  <td id="st-{i}"><span class="badge badge-yellow" style="opacity:.5">…</span></td>
  <td id="act-{i}" style="font-size:.82em;color:var(--muted)">—</td>
  <td id="load-{i}" style="font-size:.82em;color:var(--muted)">—</td>
  <td id="stat-{i}" style="font-size:.82em;color:var(--muted)">—</td>
  <td>
    <button class="btn btn-sm" style="background:rgba(248,113,113,.12);color:var(--red);border:1px solid rgba(248,113,113,.2);white-space:nowrap"
      onclick="deleteKey({i})">🗑 Удалить</button>
  </td>
</tr>
"""

    if not key_rows:
        key_rows = '<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:28px">Ключей нет — добавьте первый ниже</td></tr>'

    content = f"""
<div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;margin-bottom:20px">
  <h1 class="page-title" style="margin:0">🔑 API ключи</h1>
  <div style="display:flex;align-items:center;gap:8px">
    <span id="live-dot" style="width:8px;height:8px;border-radius:50%;background:var(--muted);display:inline-block"></span>
    <span id="live-label" style="font-size:.8em;color:var(--muted)">подключение…</span>
  </div>
</div>
{msg_html}

<!-- Summary cards — updated by JS -->
<div class="cards" style="grid-template-columns:repeat(auto-fit,minmax(120px,1fr));margin-bottom:24px">
  <div class="card"><div class="card-label">Всего ключей</div><div class="card-value purple">{total}</div></div>
  <div class="card"><div class="card-label">🟢 Активны</div><div class="card-value green" id="cnt-ok">—</div></div>
  <div class="card"><div class="card-label">⚡ В работе</div><div class="card-value" style="color:var(--accent2)" id="cnt-active">—</div></div>
  <div class="card"><div class="card-label">⏳ Кулдаун</div><div class="card-value yellow" id="cnt-cool">—</div></div>
  <div class="card"><div class="card-label">🔴 Ошибка</div><div class="card-value red" id="cnt-err">—</div></div>
</div>

<div class="table-wrap" style="margin-bottom:24px">
<table>
  <thead><tr>
    <th>#</th>
    <th>Ключ</th>
    <th>Статус</th>
    <th>В работе</th>
    <th>Нагрузка (60с)</th>
    <th>Всего ok/err</th>
    <th>Действие</th>
  </tr></thead>
  <tbody id="keys-tbody">{key_rows}</tbody>
</table>
</div>

<div class="card" style="max-width:520px">
  <h3 style="margin-bottom:14px;font-size:1em;color:var(--text)">➕ Добавить Google API ключ</h3>
  <div style="display:flex;gap:10px;flex-wrap:wrap">
    <input type="text" id="new-key-input" placeholder="AIza..." autocomplete="off"
      style="flex:1;min-width:200px;padding:10px 14px;background:var(--bg);border:1px solid var(--border);
             border-radius:8px;color:var(--text);font-size:.9em;outline:none">
    <button class="btn btn-primary" onclick="addKey()" style="white-space:nowrap">Добавить ключ</button>
  </div>
  <p style="color:var(--muted);font-size:.78em;margin-top:10px">
    Ключ хранится в БД. После добавления/удаления нужен <b>перезапуск сервиса</b> чтобы изменения вступили в силу.
  </p>
  <p style="color:var(--muted);font-size:.78em;margin-top:6px;line-height:1.6">
    ⚡ <b>В работе</b> — прямо сейчас обрабатывает запрос(ы)<br>
    🟢 <b>Активен</b> — готов принимать запросы<br>
    ⏳ <b>Кулдаун</b> — получил 429, ждёт 60с<br>
    🔴 <b>Ошибка авт.</b> — биллинг отключён или ключ отозван
  </p>
</div>

<script>
const TOTAL_KEYS = {total};

function fmtAgo(sec) {{
  if (sec === null || sec === undefined) return '—';
  if (sec < 5) return 'только что';
  if (sec < 60) return sec + 'с назад';
  return Math.floor(sec/60) + 'м назад';
}}

function modelShort(m) {{
  if (!m) return '';
  if (m.includes('flash-image')) return 'Flash🖼';
  if (m.includes('pro-image'))   return 'Pro🖼';
  if (m.includes('pro-preview')) return 'Pro💬';
  if (m.includes('flash'))       return 'Flash💬';
  return m.split('-').slice(-2).join('-');
}}

function statusBadge(s, cd) {{
  if (s === 'active')     return '<span class="badge badge-blue" style="animation:pulse 1s infinite">⚡ В работе</span>';
  if (s === 'auth_error') return '<span class="badge badge-red">🔴 Ошибка авт.</span>';
  if (s === 'cooldown')   return `<span class="badge badge-yellow">⏳ Кулдаун ${{cd}}с</span>`;
  return '<span class="badge badge-green">🟢 Активен</span>';
}}

async function poll() {{
  try {{
    const r = await fetch('/admin/api/keys/status');
    if (!r.ok) throw new Error(r.status);
    const data = await r.json();

    // Update live indicator
    document.getElementById('live-dot').style.background = 'var(--green)';
    document.getElementById('live-label').textContent = 'live';

    const slots = data.slots || [];
    let cntOk=0, cntActive=0, cntCool=0, cntErr=0;

    slots.forEach((s, i) => {{
      if (s.status === 'ok')         cntOk++;
      if (s.status === 'active')     cntActive++;
      if (s.status === 'cooldown')   cntCool++;
      if (s.status === 'auth_error') cntErr++;

      const st   = document.getElementById('st-'   + i);
      const act  = document.getElementById('act-'  + i);
      const load = document.getElementById('load-' + i);
      const stat = document.getElementById('stat-' + i);
      if (!st) return;

      st.innerHTML   = statusBadge(s.status, s.cooldown_remaining);

      // Active requests cell
      if (s.active_requests > 0) {{
        const model = modelShort(s.last_model);
        act.innerHTML = `<span style="color:var(--accent2);font-weight:600">⚡ ${{s.active_requests}} шт</span>`
                      + (model ? ` <span style="color:var(--muted)">[${{model}}]</span>` : '');
      }} else {{
        const ago = fmtAgo(s.last_used_ago);
        const model = modelShort(s.last_model);
        act.innerHTML = s.last_used_ago !== null
          ? `<span style="color:var(--muted)">${{ago}}</span>` + (model ? ` <span style="opacity:.6">[${{model}}]</span>` : '')
          : '<span style="color:var(--muted)">не использовался</span>';
      }}

      // Load cell — QPM usage as colored text
      const rf=s.req_flash, qf=s.qpm_flash, rp=s.req_pro, qp=s.qpm_pro;
      function qpmColor(used, max) {{
        if (used === 0) return 'var(--muted)';
        const pct = used / max;
        if (pct >= 1)   return 'var(--red)';
        if (pct >= 0.8) return 'var(--yellow)';
        return 'var(--green)';
      }}
      load.innerHTML =
        `<span style="color:${{qpmColor(rf,qf)}}">Flash&nbsp;${{rf}}/${{qf}}</span>`
        + `<span style="color:var(--border)"> &nbsp;|&nbsp; </span>`
        + `<span style="color:${{qpmColor(rp,qp)}}">Pro&nbsp;${{rp}}/${{qp}}</span>`;

      // Total stats
      const ok_ = s.total_ok, err_ = s.total_err;
      stat.innerHTML = `<span style="color:var(--green)">✓${{ok_}}</span> / <span style="color:var(--red)">✗${{err_}}</span>`;
    }});

    document.getElementById('cnt-ok').textContent     = cntOk;
    document.getElementById('cnt-active').textContent  = cntActive;
    document.getElementById('cnt-cool').textContent   = cntCool;
    document.getElementById('cnt-err').textContent    = cntErr;

  }} catch(e) {{
    document.getElementById('live-dot').style.background  = 'var(--red)';
    document.getElementById('live-label').textContent = 'нет связи';
  }}
}}

// Add pulse animation
const style = document.createElement('style');
style.textContent = '@keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:.4}} }}';
document.head.appendChild(style);

poll();
setInterval(poll, 2000);

async function addKey() {{
  const val = document.getElementById('new-key-input').value.trim();
  if (!val) return;
  const r = await fetch('/admin/api/keys/add', {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{key:val}})}});
  const d = await r.json();
  if (d.ok) location.href = '/admin/api-keys?msg=added';
  else if (d.error === 'exists') location.href = '/admin/api-keys?msg=exists';
  else alert('Ошибка: ' + (d.error || 'неизвестная'));
}}

async function deleteKey(idx) {{
  if (!confirm('Удалить ключ #' + (idx+1) + '?\\nПосле удаления нужен перезапуск сервиса.')) return;
  const r = await fetch('/admin/api/keys/delete', {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{index:idx}})}});
  const d = await r.json();
  if (d.ok) location.href = '/admin/api-keys?msg=deleted';
  else alert('Ошибка: ' + (d.error || 'неизвестная'));
}}
</script>
"""
    return web.Response(text=_layout("API ключи", content, "apikeys"), content_type="text/html")


@_api_require_auth
async def api_keys_status(request: web.Request) -> web.Response:
    """Return live slot statuses as JSON — polled by the admin page every 2s."""
    if _vertex_service is None:
        return web.Response(
            text=json.dumps({"slots": [], "no_service": True}),
            content_type="application/json",
        )
    try:
        slots = _vertex_service.get_slots_status()
        return web.Response(
            text=json.dumps({"slots": slots, "no_service": False}),
            content_type="application/json",
        )
    except Exception as e:
        logger.exception("api_keys_status error")
        return web.Response(
            text=json.dumps({"slots": [], "error": str(e)}),
            content_type="application/json",
            status=500,
        )


@_api_require_auth
async def api_keys_add(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        key = data.get("key", "").strip()
        if not key:
            return web.Response(text=json.dumps({"ok": False, "error": "empty"}), content_type="application/json", status=400)
        added = _key_store.add_key(key)
        if not added:
            return web.Response(text=json.dumps({"ok": False, "error": "exists"}), content_type="application/json", status=400)
        logger.info("admin: API key added (masked=%s)", _key_store.mask_key(key))
        return web.Response(text=json.dumps({"ok": True}), content_type="application/json")
    except Exception as e:
        logger.exception("api_keys_add error")
        return web.Response(text=json.dumps({"ok": False, "error": str(e)}), content_type="application/json", status=500)


@_api_require_auth
async def api_keys_delete(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        index = int(data.get("index", -1))
        removed = _key_store.remove_key(index)
        if removed is None:
            return web.Response(text=json.dumps({"ok": False, "error": "not_found"}), content_type="application/json", status=404)
        logger.info("admin: API key removed (masked=%s)", _key_store.mask_key(removed))
        return web.Response(text=json.dumps({"ok": True}), content_type="application/json")
    except Exception as e:
        logger.exception("api_keys_delete error")
        return web.Response(text=json.dumps({"ok": False, "error": str(e)}), content_type="application/json", status=500)


# ─── Register routes ─────────────────────────────────────────────────────────

def register_admin_routes(app: web.Application) -> None:
    app.router.add_get("/admin",                          handle_admin_root)
    app.router.add_get("/admin/login",                    handle_login)
    app.router.add_post("/admin/login",                   handle_login)
    app.router.add_get("/admin/logout",                   handle_logout)
    app.router.add_get("/admin/dashboard",                handle_dashboard)
    app.router.add_get("/admin/users",                    handle_users)
    app.router.add_get("/admin/users/{uid}",              handle_user_detail)
    app.router.add_get("/admin/payments",                 handle_payments)
    app.router.add_get("/admin/api-keys",                 handle_api_keys)
    app.router.add_post("/admin/api/users/{uid}/credits",    api_credits)
    app.router.add_post("/admin/api/users/{uid}/block",      api_block)
    app.router.add_post("/admin/api/users/{uid}/reset_gens", api_reset_gens)
    app.router.add_post("/admin/api/users/{uid}/delete",     api_delete)
    app.router.add_post("/admin/api/test-log-channel",       api_test_log_channel)
    app.router.add_get("/admin/api/keys/status",             api_keys_status)
    app.router.add_post("/admin/api/keys/add",               api_keys_add)
    app.router.add_post("/admin/api/keys/delete",            api_keys_delete)
    app.router.add_get("/admin/tg-photo/{file_unique_id}",   handle_tg_photo)
    logger.info("Admin panel routes registered at /admin")
