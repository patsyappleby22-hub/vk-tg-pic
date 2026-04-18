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


# ─── Live generation progress (shared state) ─────────────────────────────────
_gen_progress: dict = {
    "active": False, "step": 0, "total": 5, "label": "",
    "pct": 0, "log": [], "done": False, "error": "", "started_at": 0.0,
}

# ─── Trend selection (manual mode: scheduler waits for admin pick) ────────────
_trend_sel_event: "asyncio.Event | None" = None
_trend_sel_result: "dict | None" = None


async def wait_for_trend_selection() -> "dict | None":
    """Scheduler calls this to pause and wait for admin to pick a trend."""
    global _trend_sel_event, _trend_sel_result
    import asyncio as _asyncio
    _trend_sel_event = _asyncio.Event()
    _trend_sel_result = None
    await _trend_sel_event.wait()
    return _trend_sel_result


def set_trend_selection(trend: "dict | None") -> None:
    """Called by the select_trend endpoint to unblock wait_for_trend_selection."""
    global _trend_sel_result, _trend_sel_event
    _trend_sel_result = trend
    if _trend_sel_event is not None:
        _trend_sel_event.set()


def _gen_progress_reset() -> None:
    global _gen_progress
    _gen_progress = {
        "active": True, "step": 0, "total": 5, "label": "Запуск...",
        "pct": 0, "log": [], "done": False, "error": "", "started_at": time.monotonic(),
        "thinking_buf": "",   # full thinking text accumulated so far
        "thinking_sent": 0,   # how many chars of thinking_buf have been sent via SSE
        "last_post_id": None, # id of last generated post (set on step 5)
    }


def update_gen_progress(
    step: int, label: str, pct: int,
    msg: str = "", *, done: bool = False, error: str = "",
    last_post_id: "int | None" = None,
    trends: "list | None" = None,
) -> None:
    """Called from scheduler._run_generate to broadcast pipeline progress."""
    global _gen_progress
    elapsed = round(time.monotonic() - _gen_progress.get("started_at", time.monotonic()), 1)
    entry = {"t": elapsed, "msg": msg or label, "ok": not error, "err": bool(error)}
    _gen_progress["log"].append(entry)
    _gen_progress["step"] = step
    _gen_progress["label"] = label
    _gen_progress["pct"] = pct
    _gen_progress["active"] = not done and not error
    _gen_progress["done"] = done
    _gen_progress["error"] = error
    if last_post_id is not None:
        _gen_progress["last_post_id"] = last_post_id
    # Store trend list for SSE (when waiting for admin to pick)
    _gen_progress["trends"] = trends  # None = no picker needed; list = show picker


def update_gen_thinking(delta: str) -> None:
    """Append a chunk of Gemini thinking text. Called from thread (GIL-safe)."""
    if not delta:
        return
    _gen_progress["thinking_buf"] = _gen_progress.get("thinking_buf", "") + delta

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
_pending_2fa: dict[str, tuple[str, float, int]] = {}  # token → (code, expires_at, attempts)

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
        ("autopub",   "/admin/autopub",    "📣", "Автопост"),
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

  /* ── Form inputs (used by autopub settings, etc.) ── */
  .form-label{{display:block;font-size:.82em;color:var(--muted);margin-bottom:5px;font-weight:500}}
  .form-input{{width:100%;background:var(--surface);border:1px solid var(--border);border-radius:8px;
    padding:9px 12px;color:var(--text);font-size:.9em;outline:none;box-sizing:border-box;
    font-family:inherit;resize:vertical}}
  .form-input:focus{{border-color:var(--accent);box-shadow:0 0 0 2px rgba(167,139,250,.15)}}

  /* ── Alert ── */
  .alert{{padding:12px 16px;border-radius:10px;margin-bottom:16px;font-size:.9em}}
  .alert-success{{background:rgba(52,211,153,.1);border:1px solid rgba(52,211,153,.2);
    color:var(--green)}}
  .alert-error{{background:rgba(248,113,113,.1);border:1px solid rgba(248,113,113,.2);
    color:var(--red)}}

  /* ── Autopub post card ── */
  .post-card{{display:grid;grid-template-columns:190px 1fr;gap:14px;margin-bottom:14px}}
  .pc-img{{min-width:0}}
  .post-card-btns{{display:flex;flex-wrap:wrap;gap:6px}}
  .form-grid-2{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
  .autopub-tabs{{display:flex;gap:0;border-bottom:1px solid var(--border);
    margin-bottom:20px;overflow-x:auto;-webkit-overflow-scrolling:touch;
    scrollbar-width:none}}
  .autopub-tabs::-webkit-scrollbar{{display:none}}
  .autopub-tabs a{{padding:10px 18px;text-decoration:none;white-space:nowrap;
    font-size:.9em;border-bottom:2px solid transparent;flex-shrink:0}}
  .autopub-tabs a.tab-active{{border-bottom-color:var(--accent)}}
  .autopub-header{{display:flex;align-items:center;gap:12px;
    margin-bottom:20px;flex-wrap:wrap}}
  .gen-hint{{font-size:.8em;color:var(--muted)}}

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
    .main{{padding:12px;padding-bottom:80px}}
    .page-title{{font-size:1.3em}}
    .cards{{grid-template-columns:repeat(2,1fr);gap:10px}}
    .card{{padding:14px}}
    .card-value{{font-size:1.5em}}
    .detail-grid{{grid-template-columns:repeat(2,1fr)}}
    .actions-row .btn{{font-size:.82em;padding:7px 12px}}
    .toolbar{{flex-direction:column;align-items:stretch}}
    .search-input{{width:100%;flex:none}}
    select{{width:100%;flex:none}}

    /* autopub mobile */
    .post-card{{grid-template-columns:1fr}}
    .post-card .pc-img img{{max-height:220px;width:100%;object-fit:cover;
      border-radius:8px}}
    .post-card .pc-img>div{{height:100px}}
    .form-grid-2{{grid-template-columns:1fr}}
    .form-grid-2>div[style*="grid-column:1/-1"]{{grid-column:1 !important}}
    .post-card-btns .btn{{flex:1 1 calc(50% - 6px);margin-left:0 !important;
      text-align:center}}
    .autopub-header h2{{font-size:1.2em}}
    .gen-hint{{display:none}}
    .autopub-tabs a{{padding:8px 12px;font-size:.82em}}
    .hist-mobile-hide{{display:none}}
    .hist-table-wrap{{display:none !important}}
    .hist-mob-list{{display:block !important}}
    .hist-card{{background:var(--surface);border:1px solid var(--border);
      border-radius:12px;padding:12px;margin-bottom:10px;display:flex;gap:10px}}
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
                _pending_2fa[tok] = (code, time.time() + _2FA_TTL, 0)
                sent = await _send_2fa_code(code)
                if not sent:
                    logger.warning("2FA code send failed — skipping 2FA, logging in directly")
                    resp = web.HTTPFound("/admin/dashboard")
                    resp.set_cookie(_COOKIE_NAME, _make_token(), max_age=_COOKIE_MAX_AGE, httponly=True, samesite="Lax")
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
            correct_code, expires_at, attempts = entry
            if time.time() > expires_at:
                _pending_2fa.pop(tok, None)
                html = _login_page_html("password", "", "Код истёк. Войдите снова.")
                return web.Response(text=html, content_type="text/html")
            if attempts >= 5:
                _pending_2fa.pop(tok, None)
                html = _login_page_html("password", "", "Превышено число попыток. Войдите снова.")
                return web.Response(text=html, content_type="text/html")
            if hmac.compare_digest(entered, correct_code):
                _pending_2fa.pop(tok, None)
                resp = web.HTTPFound("/admin/dashboard")
                resp.set_cookie(_COOKIE_NAME, _make_token(), max_age=_COOKIE_MAX_AGE, httponly=True, samesite="Lax")
                raise resp
            else:
                _pending_2fa[tok] = (correct_code, expires_at, attempts + 1)
                html = _login_page_html("2fa", tok, f"Неверный код. Попыток осталось: {5 - attempts - 1}.")
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


def _render_image_gallery(
    image_logs: list[dict],
    page: int,
    total: int,
    uid: int,
) -> str:
    """Build the image list (compact table) for a user's generated images.
    Uses server-side pagination — only the current page records are passed in,
    no JSON blob is embedded in the HTML."""
    try:
        if total == 0:
            return '<div class="img-empty">Генераций пока нет</div>'

        start = (page - 1) * _IMG_PAGE_SIZE
        has_next = (start + len(image_logs)) < total
        has_prev = page > 1
        end = start + len(image_logs)

        rows = ""
        for img in image_logs:
            fuid = img["file_unique_id"]
            prompt_esc = (img.get("prompt") or "").replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")
            dt = _msk(img.get("created_at", ""))
            plat = "📱" if img.get("platform") == "tg" else "💙"
            mdl = (img.get("model") or "").split("-")[0] or "—"
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

        # Server-side pagination: links reload the page with ?img_page=N
        base_url = f"/admin/users/{uid}"
        pagination = ""
        if has_prev or has_next:
            pagination = '<div style="display:flex;gap:8px;margin-top:10px">'
            if has_prev:
                pagination += f'<a href="{base_url}?img_page={page-1}" class="btn btn-muted btn-sm">← Назад</a>'
            pagination += f'<span style="align-self:center;color:var(--muted);font-size:.85em">{start+1}–{end} из {total}</span>'
            if has_next:
                pagination += f'<a href="{base_url}?img_page={page+1}" class="btn btn-muted btn-sm">Далее →</a>'
            pagination += '</div>'

        return (
            '<div class="table-wrap"><table>'
            '<thead><tr><th style="width:56px">Фото</th><th>Промпт</th><th>Дата</th><th>Модель</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div>'
            f'{pagination}'
            '<div class="lightbox" id="lightbox" onclick="closeLightbox()">'
            '<span class="lightbox-close" onclick="closeLightbox()">×</span>'
            '<img id="lightbox-img" src="" alt="">'
            '<div class="lightbox-caption" id="lightbox-cap"></div></div>'
            '<script>'
            'function openLightbox(src,cap){document.getElementById("lightbox-img").src=src;'
            'document.getElementById("lightbox-cap").textContent=cap;document.getElementById("lightbox").classList.add("open");}'
            'function openImg(el){openLightbox("/admin/tg-photo/"+el.dataset.fuid,(el.closest("tr").querySelector("td:nth-child(2)").textContent.trim())+" · "+el.dataset.dt);}'
            'function closeLightbox(){document.getElementById("lightbox").classList.remove("open");document.getElementById("lightbox-img").src="";}'
            'document.addEventListener("keydown",function(e){if(e.key==="Escape")closeLightbox();});'
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
        img_page = max(1, int(request.rel_url.query.get("img_page", 1)))
    except (ValueError, TypeError):
        img_page = 1

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
        # Server-side pagination: load only one page of images from DB
        img_offset = (img_page - 1) * _IMG_PAGE_SIZE
        image_logs = _db.get_user_image_logs(uid, limit=_IMG_PAGE_SIZE, offset=img_offset)
        image_total = _db.count_user_image_logs(uid)
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

<div class="section-heading">Генерации ({image_total})</div>
{_render_image_gallery(image_logs, img_page, image_total, uid)}

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

# Shared aiohttp session — reused across all photo-proxy requests to avoid
# the overhead of creating a new TCP connection pool on every thumbnail load.
# Connector limits: max 8 simultaneous connections to Telegram CDN.
_photo_session: _aiohttp.ClientSession | None = None
_photo_connector: _aiohttp.TCPConnector | None = None

_MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB hard cap per image


def _get_photo_session() -> _aiohttp.ClientSession:
    global _photo_session, _photo_connector
    if _photo_session is None or _photo_session.closed:
        _photo_connector = _aiohttp.TCPConnector(limit=8, limit_per_host=8)
        _photo_session = _aiohttp.ClientSession(connector=_photo_connector)
    return _photo_session


@_require_auth
async def handle_tg_photo(request: web.Request) -> web.Response:
    """Proxy a Telegram photo by file_unique_id — streams directly to browser,
    never loads the full image into memory."""
    file_unique_id = request.match_info.get("file_unique_id", "")
    if not file_unique_id or not _TG_TOKEN_FOR_PROXY:
        raise web.HTTPNotFound()
    row = _db.get_image_log_by_unique_id(file_unique_id)
    if not row:
        raise web.HTTPNotFound()
    file_id = row["file_id"]
    try:
        session = _get_photo_session()
        # Step 1: resolve file_path via Telegram API (small JSON, read fully)
        gf_url = f"https://api.telegram.org/bot{_TG_TOKEN_FOR_PROXY}/getFile"
        async with session.get(gf_url, params={"file_id": file_id},
                               timeout=_aiohttp.ClientTimeout(total=10)) as resp:
            gf = await resp.json()
        if not gf.get("ok"):
            raise web.HTTPNotFound()
        file_path = gf["result"]["file_path"]
        # Step 2: stream image bytes directly to browser — never loaded into RAM
        dl_url = f"https://api.telegram.org/file/bot{_TG_TOKEN_FOR_PROXY}/{file_path}"
        async with session.get(dl_url, timeout=_aiohttp.ClientTimeout(total=30)) as dl:
            if dl.status != 200:
                raise web.HTTPBadGateway()
            content_type = dl.headers.get("Content-Type", "image/jpeg")
            stream_resp = web.StreamResponse(
                headers={
                    "Content-Type": content_type,
                    "Cache-Control": "max-age=86400, immutable",
                }
            )
            await stream_resp.prepare(request)
            received = 0
            async for chunk in dl.content.iter_chunked(65536):
                received += len(chunk)
                if received > _MAX_IMAGE_BYTES:
                    logger.warning("handle_tg_photo: image too large, truncating %s", file_unique_id)
                    break
                await stream_resp.write(chunk)
            return stream_resp
    except web.HTTPException:
        raise
    except Exception as exc:
        logger.warning("handle_tg_photo failed for %s: %s", file_unique_id, exc)
        raise web.HTTPBadGateway()


async def handle_tg_photo_by_fileid(request: web.Request) -> web.Response:
    """Proxy a Telegram photo by file_id directly (for extra autopub photos)."""
    file_id = request.match_info.get("file_id", "")
    if not file_id or not _TG_TOKEN_FOR_PROXY:
        raise web.HTTPNotFound()
    try:
        session = _get_photo_session()
        gf_url = f"https://api.telegram.org/bot{_TG_TOKEN_FOR_PROXY}/getFile"
        async with session.get(gf_url, params={"file_id": file_id},
                               timeout=_aiohttp.ClientTimeout(total=10)) as resp:
            gf = await resp.json()
        if not gf.get("ok"):
            raise web.HTTPNotFound()
        file_path = gf["result"]["file_path"]
        dl_url = f"https://api.telegram.org/file/bot{_TG_TOKEN_FOR_PROXY}/{file_path}"
        async with session.get(dl_url, timeout=_aiohttp.ClientTimeout(total=30)) as dl:
            if dl.status != 200:
                raise web.HTTPBadGateway()
            content_type = dl.headers.get("Content-Type", "image/jpeg")
            stream_resp = web.StreamResponse(
                headers={
                    "Content-Type": content_type,
                    "Cache-Control": "max-age=86400, immutable",
                }
            )
            await stream_resp.prepare(request)
            received = 0
            async for chunk in dl.content.iter_chunked(65536):
                received += len(chunk)
                if received > _MAX_IMAGE_BYTES:
                    break
                await stream_resp.write(chunk)
            return stream_resp
    except web.HTTPException:
        raise
    except Exception as exc:
        logger.warning("handle_tg_photo_by_fileid failed: %s", exc)
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
    elif msg == "updated":
        msg_html = '<div class="alert alert-success">✅ Ключ обновлён. Изменения применены.</div>'
    elif msg == "empty":
        msg_html = '<div class="alert alert-error">⚠️ Введите непустой ключ.</div>'

    key_rows = ""
    for i, entry in enumerate(stored_keys):
        if isinstance(entry, str):
            masked = _key_store.mask_key(entry)
            proj = ""
        else:
            masked = _key_store.mask_key(entry["key"])
            proj = entry.get("project_id") or ""
        proj_badge = f'<span class="badge badge-green" style="font-size:.75em">📂 {proj}</span>' if proj else '<span class="badge" style="font-size:.75em;opacity:.6;background:rgba(100,116,139,.12);color:#94a3b8;border:1px solid rgba(100,116,139,.2)">авто</span>'
        key_rows += f"""<tr id="key-row-{i}">
  <td style="font-weight:600;color:var(--muted);width:36px">{i+1}</td>
  <td><code style="font-size:.88em;color:var(--accent)">{masked}</code><br>{proj_badge}</td>
  <td id="st-{i}"><span class="badge badge-yellow" style="opacity:.5">…</span></td>
  <td id="act-{i}" style="font-size:.82em;color:var(--muted)">—</td>
  <td id="load-{i}" style="font-size:.82em;color:var(--muted)">—</td>
  <td id="stat-{i}" style="font-size:.82em;color:var(--muted)">—</td>
  <td style="white-space:nowrap">
    <button class="btn btn-sm" style="background:rgba(139,92,246,.12);color:var(--accent);border:1px solid rgba(139,92,246,.2);margin-right:4px"
      onclick="showEdit({i},'{masked}','{proj}')">✏️</button>
    <button class="btn btn-sm" style="background:rgba(139,92,246,.12);color:var(--accent);border:1px solid rgba(139,92,246,.2);margin-right:4px"
      onclick="showHistory({i})">📋</button>
    <button class="btn btn-sm" style="background:rgba(248,113,113,.12);color:var(--red);border:1px solid rgba(248,113,113,.2)"
      onclick="deleteKey({i})">🗑</button>
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

<div class="card" style="max-width:600px">
  <h3 style="margin-bottom:14px;font-size:1em;color:var(--text)">➕ Добавить Google API ключ</h3>
  <div style="display:flex;flex-direction:column;gap:10px">
    <div style="display:flex;gap:10px;flex-wrap:wrap">
      <input type="text" id="new-key-input" placeholder="AIza..." autocomplete="off"
        style="flex:1;min-width:200px;padding:10px 14px;background:var(--bg);border:1px solid var(--border);
               border-radius:8px;color:var(--text);font-size:.9em;outline:none">
    </div>
    <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
      <input type="text" id="new-project-input" placeholder="ID проекта (для видео Veo)" autocomplete="off"
        style="flex:1;min-width:200px;padding:10px 14px;background:var(--bg);border:1px solid var(--border);
               border-radius:8px;color:var(--text);font-size:.9em;outline:none">
      <button class="btn btn-primary" onclick="addKey()" style="white-space:nowrap">Добавить ключ</button>
    </div>
  </div>
  <p style="color:var(--muted);font-size:.78em;margin-top:10px">
    Ключ хранится в БД и применяется <b>немедленно</b> без перезапуска сервиса.<br>
    📂 <b>ID проекта</b> — необязателен для API-ключей (SDK сам определяет проект). Для сервисных аккаунтов берётся из JSON. Для изображений и чата — не нужен.
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
      const rf=s.req_flash, qf=s.qpm_flash, rp=s.req_pro, qp=s.qpm_pro, rv=s.req_veo||0, qv=s.qpm_veo||2;
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
        + `<span style="color:${{qpmColor(rp,qp)}}">Pro&nbsp;${{rp}}/${{qp}}</span>`
        + `<span style="color:var(--border)"> &nbsp;|&nbsp; </span>`
        + `<span style="color:${{qpmColor(rv,qv)}}">Veo&nbsp;${{rv}}/${{qv}}</span>`;

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
  const proj = document.getElementById('new-project-input').value.trim();
  if (!val) return;
  const body = {{key: val}};
  if (proj) body.project_id = proj;
  const r = await fetch('/admin/api/keys/add', {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body)}});
  const d = await r.json();
  if (d.ok) location.href = '/admin/api-keys?msg=added';
  else if (d.error === 'exists') location.href = '/admin/api-keys?msg=exists';
  else alert('Ошибка: ' + (d.error || 'неизвестная'));
}}

async function deleteKey(idx) {{
  if (!confirm('Удалить ключ #' + (idx+1) + '? Изменение применится немедленно.')) return;
  const r = await fetch('/admin/api/keys/delete', {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{index:idx}})}});
  const d = await r.json();
  if (d.ok) location.href = '/admin/api-keys?msg=deleted';
  else alert('Ошибка: ' + (d.error || 'неизвестная'));
}}

function showEdit(idx, maskedKey, projectId) {{
  const modal = document.getElementById('edit-modal');
  document.getElementById('edit-title').textContent = 'Редактировать ключ #' + (idx+1);
  document.getElementById('edit-idx').value = idx;
  document.getElementById('edit-key-input').value = '';
  document.getElementById('edit-key-input').placeholder = maskedKey + ' (оставьте пустым чтобы не менять)';
  document.getElementById('edit-project-input').value = projectId || '';
  modal.style.display = 'flex';
}}

function closeEdit() {{
  document.getElementById('edit-modal').style.display = 'none';
}}

async function saveEdit() {{
  const idx = parseInt(document.getElementById('edit-idx').value);
  const newKey = document.getElementById('edit-key-input').value.trim();
  const newProj = document.getElementById('edit-project-input').value.trim();
  const body = {{index: idx, project_id: newProj || null}};
  if (newKey) body.key = newKey;
  const r = await fetch('/admin/api/keys/update', {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body)}});
  const d = await r.json();
  if (d.ok) location.href = '/admin/api-keys?msg=updated';
  else alert('Ошибка: ' + (d.error || 'неизвестная'));
}}

function esc(s) {{
  if (!s) return '';
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}}

async function showHistory(idx) {{
  const modal = document.getElementById('history-modal');
  const tbody = document.getElementById('history-tbody');
  const title = document.getElementById('history-title');
  title.textContent = 'История ключа #' + (idx+1);
  tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:20px;color:var(--muted)">Загрузка...</td></tr>';
  modal.style.display = 'flex';
  try {{
    const r = await fetch('/admin/api/keys/' + idx + '/history');
    const data = await r.json();
    const items = data.history || [];
    if (items.length === 0) {{
      tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:20px;color:var(--muted)">История пуста — запросов ещё не было</td></tr>';
      return;
    }}
    tbody.innerHTML = items.map(h => {{
      const dt = new Date(h.ts);
      const time = dt.toLocaleString('ru-RU', {{hour:'2-digit',minute:'2-digit',second:'2-digit',day:'2-digit',month:'2-digit'}});
      const stMap = {{
        'ok': '<span style="color:var(--green)">✅ Успех</span>',
        'safety': '<span style="color:var(--yellow)">🚫 Безопасность</span>',
        'timeout': '<span style="color:var(--red)">⏱ Таймаут</span>',
        'rate_limit': '<span style="color:var(--yellow)">⚡ 429</span>',
        'auth_error': '<span style="color:var(--red)">🔴 Авт. ошибка</span>',
        'error': '<span style="color:var(--red)">❌ Ошибка</span>',
        'text_retry': '<span style="color:var(--yellow)">🔄 Повтор</span>',
      }};
      const stHtml = stMap[h.status] || `<span style="color:var(--muted)">${{esc(h.status)}}</span>`;
      const user = h.username ? `<span style="color:var(--accent)">@${{esc(h.username)}}</span>` : (h.user_id ? `id:${{h.user_id}}` : '—');
      const dur = h.duration_ms ? (h.duration_ms / 1000).toFixed(1) + 'с' : '—';
      const errText = h.error ? h.error.substring(0,60) + (h.error.length>60?'…':'') : '';
      const errHtml = errText ? `<span style="color:var(--red);font-size:.78em" title="${{esc(h.error)}}">${{esc(errText)}}</span>` : '—';
      return `<tr>
        <td style="font-size:.82em;white-space:nowrap">${{time}}</td>
        <td style="font-size:.82em">${{user}}</td>
        <td style="font-size:.82em;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${{esc(h.prompt)}}">${{esc(h.prompt)}}</td>
        <td style="font-size:.82em">${{modelShort(h.model)}}</td>
        <td style="font-size:.82em">${{stHtml}}</td>
        <td style="font-size:.82em">${{dur}}</td>
        <td style="font-size:.78em;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${{errHtml}}</td>
      </tr>`;
    }}).join('');
  }} catch(e) {{
    tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:20px;color:var(--red)">Ошибка загрузки</td></tr>';
  }}
}}

function closeHistory() {{
  document.getElementById('history-modal').style.display = 'none';
}}
</script>

<div id="edit-modal" style="display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.7);z-index:1001;align-items:center;justify-content:center;padding:20px">
  <div style="background:var(--card);border:1px solid var(--border);border-radius:14px;max-width:520px;width:100%;padding:24px">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:18px">
      <h2 id="edit-title" style="margin:0;font-size:1.1em;color:var(--text)">Редактировать ключ</h2>
      <button onclick="closeEdit()" style="background:none;border:none;color:var(--muted);font-size:1.4em;cursor:pointer;padding:4px 8px">&times;</button>
    </div>
    <input type="hidden" id="edit-idx">
    <div style="display:flex;flex-direction:column;gap:12px">
      <div>
        <label style="font-size:.82em;color:var(--muted);margin-bottom:4px;display:block">API ключ</label>
        <input type="text" id="edit-key-input" autocomplete="off"
          style="width:100%;padding:10px 14px;background:var(--bg);border:1px solid var(--border);
                 border-radius:8px;color:var(--text);font-size:.9em;outline:none;box-sizing:border-box">
      </div>
      <div>
        <label style="font-size:.82em;color:var(--muted);margin-bottom:4px;display:block">ID проекта Google Cloud</label>
        <input type="text" id="edit-project-input" placeholder="my-project-123456" autocomplete="off"
          style="width:100%;padding:10px 14px;background:var(--bg);border:1px solid var(--border);
                 border-radius:8px;color:var(--text);font-size:.9em;outline:none;box-sizing:border-box">
        <p style="color:var(--muted);font-size:.75em;margin-top:6px">Обязателен для генерации видео (Veo). Оставьте пустым если ключ только для изображений.</p>
      </div>
      <button class="btn btn-primary" onclick="saveEdit()" style="align-self:flex-end">Сохранить</button>
    </div>
  </div>
</div>

<div id="history-modal" style="display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.7);z-index:1000;align-items:center;justify-content:center;padding:20px">
  <div style="background:var(--card);border:1px solid var(--border);border-radius:14px;max-width:1000px;width:100%;max-height:85vh;display:flex;flex-direction:column;overflow:hidden">
    <div style="display:flex;align-items:center;justify-content:space-between;padding:18px 24px;border-bottom:1px solid var(--border)">
      <h2 id="history-title" style="margin:0;font-size:1.1em;color:var(--text)">История ключа</h2>
      <button onclick="closeHistory()" style="background:none;border:none;color:var(--muted);font-size:1.4em;cursor:pointer;padding:4px 8px">&times;</button>
    </div>
    <div style="overflow:auto;padding:0">
      <table style="width:100%;font-size:.9em">
        <thead><tr>
          <th style="padding:10px 12px">Время</th>
          <th style="padding:10px 12px">Пользователь</th>
          <th style="padding:10px 12px">Промпт</th>
          <th style="padding:10px 12px">Модель</th>
          <th style="padding:10px 12px">Статус</th>
          <th style="padding:10px 12px">Время вып.</th>
          <th style="padding:10px 12px">Ошибка</th>
        </tr></thead>
        <tbody id="history-tbody"></tbody>
      </table>
    </div>
  </div>
</div>
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
        project_id = data.get("project_id", "").strip() or None
        if not key:
            return web.Response(text=json.dumps({"ok": False, "error": "empty"}), content_type="application/json", status=400)
        added = _key_store.add_key(key, project_id=project_id)
        if not added:
            return web.Response(text=json.dumps({"ok": False, "error": "exists"}), content_type="application/json", status=400)
        if _vertex_service is not None:
            _vertex_service.reload_keys()
        logger.info("admin: API key added (masked=%s, project=%s)", _key_store.mask_key(key), project_id or "none")
        return web.Response(text=json.dumps({"ok": True}), content_type="application/json")
    except Exception as e:
        logger.exception("api_keys_add error")
        return web.Response(text=json.dumps({"ok": False, "error": str(e)}), content_type="application/json", status=500)


@_api_require_auth
async def api_keys_update(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        index = int(data.get("index", -1))
        new_key = data.get("key") or None
        new_project_id = data.get("project_id", ...)
        updated = _key_store.update_key(index, new_key=new_key, new_project_id=new_project_id)
        if not updated:
            return web.Response(text=json.dumps({"ok": False, "error": "not_found"}), content_type="application/json", status=404)
        if _vertex_service is not None:
            _vertex_service.reload_keys()
        logger.info("admin: API key #%d updated (project=%s)", index + 1, new_project_id if new_project_id is not ... else "unchanged")
        return web.Response(text=json.dumps({"ok": True}), content_type="application/json")
    except Exception as e:
        logger.exception("api_keys_update error")
        return web.Response(text=json.dumps({"ok": False, "error": str(e)}), content_type="application/json", status=500)


@_api_require_auth
async def api_keys_delete(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        index = int(data.get("index", -1))
        removed = _key_store.remove_key(index)
        if removed is None:
            return web.Response(text=json.dumps({"ok": False, "error": "not_found"}), content_type="application/json", status=404)
        if _vertex_service is not None:
            _vertex_service.reload_keys()
        logger.info("admin: API key removed (masked=%s)", _key_store.mask_key(removed))
        return web.Response(text=json.dumps({"ok": True}), content_type="application/json")
    except Exception as e:
        logger.exception("api_keys_delete error")
        return web.Response(text=json.dumps({"ok": False, "error": str(e)}), content_type="application/json", status=500)


@_api_require_auth
async def api_keys_history(request: web.Request) -> web.Response:
    if _vertex_service is None:
        return web.Response(text=json.dumps({"history": []}), content_type="application/json")
    try:
        idx = int(request.match_info["index"])
        history = _vertex_service.get_slot_history(idx)
        return web.Response(
            text=json.dumps({"history": history}),
            content_type="application/json",
        )
    except Exception as e:
        logger.exception("api_keys_history error")
        return web.Response(
            text=json.dumps({"history": [], "error": str(e)}),
            content_type="application/json",
            status=500,
        )


# ─── Autopub ─────────────────────────────────────────────────────────────────

def _autopub_status_badge(status: str) -> str:
    colors = {
        "draft":      ("var(--muted)",   "⏳ Черновик"),
        "approved":   ("var(--green)",   "✅ Одобрен"),
        "publishing": ("var(--yellow)",  "📤 Публикуется"),
        "published":  ("var(--accent2)", "📣 Опубликован"),
        "error":      ("var(--red)",     "❌ Ошибка"),
        "rejected":   ("var(--red)",     "🚫 Отклонён"),
    }
    color, label = colors.get(status, ("var(--muted)", status))
    return f'<span style="color:{color};font-weight:600">{label}</span>'


def _render_post_card(p: dict) -> str:
    """Render a single autopub post card (module-level, reusable)."""
    pid = p["id"]
    status = p["status"]
    prompt_full = (p.get("prompt") or "").replace('"', "&quot;").replace("<", "&lt;")
    prompt_short = prompt_full[:300]
    caption_short = (p.get("caption") or "")[:300].replace("<", "&lt;")
    topic = p.get("topic", "").replace("<", "&lt;")
    dt = _msk(p.get("created_at", ""))
    fuid = p.get("tg_file_unique", "")
    source_trend = p.get("source_trend", "").replace("<", "&lt;")
    admin_comment = p.get("admin_comment", "").replace("<", "&lt;")
    extra_fids = [fid.strip() for fid in p.get("extra_file_ids", "").split(",") if fid.strip()]
    img_src = f"/admin/tg-photo/{fuid}" if fuid else ""
    if img_src and extra_fids:
        gallery_items = f'<img src="{img_src}" loading="lazy" style="width:100%;max-height:260px;object-fit:cover;border-radius:8px;cursor:pointer;display:block" onclick="openLightboxUrl(\'{img_src}\')">'
        extra_thumbs = ""
        for efid in extra_fids:
            esrc = f"/admin/tg-photo-fid/{efid}"
            extra_thumbs += f'<img src="{esrc}" loading="lazy" style="width:calc(50% - 3px);height:80px;object-fit:cover;border-radius:6px;cursor:pointer" onclick="openLightboxUrl(\'{esrc}\')">'
        img_html = f'{gallery_items}<div style="display:flex;gap:6px;margin-top:6px">{extra_thumbs}</div>'
    elif img_src:
        img_html = f'<img src="{img_src}" loading="lazy" style="width:100%;max-height:260px;object-fit:cover;border-radius:8px;cursor:pointer;display:block" onclick="openLightboxUrl(\'{img_src}\')">'
    else:
        img_html = '<div style="width:100%;height:120px;border-radius:8px;background:rgba(255,255,255,.05);display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:.85em">нет фото</div>'
    trend_badge = (
        f'<div style="font-size:.75em;background:rgba(167,139,250,.12);color:var(--accent);border-radius:6px;padding:3px 8px;margin-bottom:6px;display:inline-block">🔥 {source_trend}</div><br>'
        if source_trend else ""
    )
    feedback_badge = (
        f'<div style="font-size:.75em;background:rgba(251,191,36,.1);color:#fbbf24;border-radius:6px;padding:3px 8px;margin-bottom:6px;display:inline-block">💬 {admin_comment[:60]}{"…" if len(admin_comment) > 60 else ""}</div><br>'
        if admin_comment else ""
    )
    btns = ""
    if status == "draft":
        btns += f'<button class="btn btn-primary btn-sm" onclick="postAction({pid},\'approve\')">✅ Одобрить</button>'
    if status in ("draft", "approved"):
        btns += f'<button class="btn btn-sm" onclick="postAction({pid},\'publish\')" style="background:var(--green);color:#fff">📣 Опубл.</button>'
        btns += f'<button class="btn btn-muted btn-sm" onclick="openEditModal({pid})">✏️ Ред.</button>'
        btns += f'<button class="btn btn-sm" onclick="openFeedbackModal({pid})" style="background:rgba(251,191,36,.15);border:1px solid rgba(251,191,36,.4);color:#fbbf24">🔄 Перед.</button>'
        btns += f'<button class="btn btn-danger btn-sm" onclick="postAction({pid},\'reject\')">🗑</button>'
    return f"""<div class="card post-card" id="post-card-{pid}">
  <div class="pc-img">{img_html}</div>
  <div>
    <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:5px;gap:6px">
      <span style="font-weight:600;font-size:.93em;line-height:1.3">{topic}</span>
      {_autopub_status_badge(status)}
    </div>
    <div style="font-size:.78em;color:var(--muted);margin-bottom:6px">{dt}</div>
    {trend_badge}{feedback_badge}
    <div style="font-size:.82em;color:var(--text);line-height:1.5;margin-bottom:8px;white-space:pre-wrap">{caption_short}{'…' if len(p.get('caption', '')) > 300 else ''}</div>
    <details style="margin-bottom:10px">
      <summary style="font-size:.78em;color:var(--accent);cursor:pointer">Промпт <span style="font-size:.75em;color:var(--muted)">(нажми чтобы скопировать)</span></summary>
      <pre id="prompt-{pid}" onclick="copyPrompt(this)" data-full-prompt="{prompt_full}" title="Нажми чтобы скопировать весь промпт" style="font-size:.74em;color:var(--muted);white-space:pre-wrap;margin-top:6px;overflow-x:auto;cursor:pointer;padding:8px;border-radius:6px;background:rgba(255,255,255,.03);transition:background .2s" onmouseenter="this.style.background='rgba(167,139,250,.08)'" onmouseleave="this.style.background='rgba(255,255,255,.03)'">{prompt_short}{'…' if len(p.get('prompt', '')) > 300 else ''}</pre>
    </details>
    <div class="post-card-btns">{btns}</div>
  </div>
</div>"""


@_require_auth
async def handle_autopub(request: web.Request) -> web.Response:
    tab = request.rel_url.query.get("tab", "queue")
    msg = request.rel_url.query.get("msg", "")

    settings = _db.autopub_get_settings()
    posts_today = _db.autopub_count_published_today()

    # Queue: draft + approved posts
    drafts    = _db.autopub_get_posts(status="draft",     limit=20)
    approved  = _db.autopub_get_posts(status="approved",  limit=20)
    published = _db.autopub_get_posts(status="published", limit=30)
    errors    = _db.autopub_get_posts(status="error",     limit=10)

    queue_posts = drafts + approved
    history_posts = published + errors
    _edit_posts_map = {p['id']: p for p in queue_posts + history_posts}

    # ── Alert ──
    alert_html = ""
    if msg == "saved":
        alert_html = '<div class="alert alert-success">✓ Настройки сохранены</div>'
    elif msg == "generated":
        alert_html = '<div class="alert alert-success">✓ Пост сгенерирован и добавлен в очередь</div>'
    elif msg == "published":
        alert_html = '<div class="alert alert-success">✓ Пост опубликован</div>'
    elif msg == "err":
        alert_html = '<div class="alert alert-error">✗ Ошибка — проверьте настройки и логи</div>'

    enabled_badge = (
        '<span class="badge badge-green">🟢 Включено</span>' if settings.get("enabled")
        else '<span class="badge badge-red">🔴 Выключено</span>'
    )

    # ── Tabs ──
    def tab_cls(t): return "tab active" if tab == t else "tab"

    # ── Settings tab ──
    chk = lambda val: "checked" if val else ""
    settings_html = f"""
<form method="POST" action="/admin/api/autopub/settings">
<div class="form-grid-2">
  <div>
    <label class="form-label">Telegram канал (ID или @username)</label>
    <input name="tg_channel_id" value="{settings.get('tg_channel_id','')}" placeholder="например: -1001234567890 или @mychannel" class="form-input">
  </div>
  <div>
    <label class="form-label">VK группа (ID без минуса)</label>
    <input name="vk_group_id" value="{settings.get('vk_group_id','')}" placeholder="например: 123456789" class="form-input">
    {"" if __import__("os").getenv("VK_USER_TOKEN") else '''<div style="margin-top:8px;background:rgba(251,191,36,.1);border:1px solid rgba(251,191,36,.3);border-radius:8px;padding:10px 12px;font-size:.8em;line-height:1.5;color:#fbbf24">
      ⚠️ <b>VK_USER_TOKEN не задан</b> — публикация в ВК невозможна.<br>
      Токен группы (VK_BOT_TOKEN) не имеет прав на публикацию постов.<br>
      Нужен токен <b>пользователя-администратора</b> с правами <code>wall,photos,offline</code>.<br>
      Получить: <a href="https://vkhost.github.io/" target="_blank" style="color:#fbbf24">vkhost.github.io</a>
      → выбери приложение → выдай права → скопируй токен из URL → добавь в секреты как <code>VK_USER_TOKEN</code>.
    </div>'''}
  </div>
  <div>
    <label class="form-label">Постов в день</label>
    <input name="posts_per_day" type="number" min="1" max="24" value="{settings.get('posts_per_day',3)}" class="form-input">
  </div>
  <div>
    <label class="form-label">Username бота (без @)</label>
    <input name="bot_username" value="{settings.get('bot_username','')}" placeholder="my_ai_bot" class="form-input">
  </div>
  <div style="grid-column:1/-1">
    <label class="form-label">Темы / тематика (через запятую)</label>
    <input name="topic_hints" value="{settings.get('topic_hints','')}" placeholder="домашний уют, мода, путешествия, lifestyle" class="form-input">
  </div>
  <div style="grid-column:1/-1">
    <label class="form-label">Стиль изображений</label>
    <input name="image_style" value="{settings.get('image_style','')}" placeholder="lifestyle фото 9:16, реалистичная фотография, editorial" class="form-input">
  </div>
  <div style="grid-column:1/-1">
    <label class="form-label">CTA-текст поста (оставьте пустым — используется шаблон по умолчанию)</label>
    <textarea name="post_cta" rows="3" class="form-input" placeholder="✅ Переходи в бот @mybot...">{settings.get('post_cta','')}</textarea>
  </div>
  <div style="grid-column:1/-1">
    <label class="form-label">Шаблон поста (переменные: {{topic}}, {{caption_intro}}, {{prompt}}, {{bot_username}}, {{cta}})</label>
    <textarea name="post_template" rows="4" class="form-input" placeholder="Оставьте пустым — используется шаблон по умолчанию">{settings.get('post_template','')}</textarea>
  </div>
  <div style="display:flex;gap:24px;align-items:center;padding:8px 0">
    <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
      <input type="checkbox" name="enabled" value="1" {chk(settings.get('enabled'))} style="width:18px;height:18px;accent-color:var(--accent)">
      <span>Автопостинг включён</span>
    </label>
    <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
      <input type="checkbox" name="auto_approve" value="1" {chk(settings.get('auto_approve'))} style="width:18px;height:18px;accent-color:var(--accent)">
      <span>Автоодобрение постов</span>
    </label>
  </div>
</div>
<div style="margin-top:16px;display:flex;gap:10px;flex-wrap:wrap">
  <button type="submit" class="btn btn-primary">💾 Сохранить настройки</button>
</div>
</form>
<div style="margin-top:20px;padding:14px;background:rgba(167,139,250,.07);border-radius:10px;border:1px solid var(--border);font-size:.83em;color:var(--muted);line-height:1.8">
  <b style="color:var(--text)">Как работает расписание:</b><br>
  Посты публикуются равномерно с 09:00 до 21:00 МСК.<br>
  При <b>{settings.get('posts_per_day',3)} постах в день</b> — интервал ~{int(720/max(1,settings.get('posts_per_day',3)))} минут.<br>
  Без автоодобрения — посты попадают в очередь со статусом <b>Черновик</b> и ждут вашего одобрения.<br>
  Бот генерирует изображение через Gemini, загружает черновик в лог-канал, затем публикует в нужный канал.
</div>"""

    # ── Queue tab ──
    if queue_posts:
        queue_html = "".join(_render_post_card(p) for p in queue_posts)
    else:
        queue_html = '<div id="queue-empty" class="img-empty">Очередь пуста — нажмите «Сгенерировать» чтобы создать первый пост</div>'

    gen_btn = f'''<div style="margin-bottom:14px;display:flex;align-items:center;gap:10px;flex-wrap:wrap">
  <button class="btn btn-primary" id="gen-btn" onclick="openModePicker(this)">⚡ Сгенерировать</button>
  <span class="gen-hint">Задай идею или найди тренды → Gemini придумывает пост и генерирует фото (~60 сек)</span>
</div>

<!-- Mode picker panel -->
<div id="gen-mode-picker" style="display:none;border:1px solid rgba(167,139,250,.3);background:rgba(167,139,250,.07);border-radius:12px;margin-bottom:16px;padding:14px 16px;font-size:.88em">
  <div style="font-weight:600;color:var(--text);margin-bottom:12px">Как создать пост?</div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
    <div onclick="showIdeaInput()" style="cursor:pointer;border:1px solid rgba(167,139,250,.25);border-radius:10px;padding:12px 14px;transition:background .15s" onmouseover="this.style.background=\'rgba(167,139,250,.12)\'" onmouseout="this.style.background=\'transparent\'">
      <div style="font-size:1.1em;margin-bottom:4px">💡 Задать идею</div>
      <div style="font-size:.8em;color:var(--muted);line-height:1.4">Опиши тему словами — ИИ найдёт информацию в интернете и создаст пост</div>
    </div>
    <div onclick="startWithTrends()" style="cursor:pointer;border:1px solid rgba(167,139,250,.25);border-radius:10px;padding:12px 14px;transition:background .15s" onmouseover="this.style.background=\'rgba(167,139,250,.12)\'" onmouseout="this.style.background=\'transparent\'">
      <div style="font-size:1.1em;margin-bottom:4px">📈 Искать тренды</div>
      <div style="font-size:.8em;color:var(--muted);line-height:1.4">Gemini ищет актуальные тренды — ты выбираешь подходящий</div>
    </div>
  </div>
  <div id="gen-idea-input" style="display:none;margin-top:14px;border-top:1px solid rgba(167,139,250,.15);padding-top:12px">
    <div style="font-size:.82em;color:var(--muted);margin-bottom:6px">Опиши идею — тема, настроение, образ, что угодно:</div>
    <textarea id="gen-idea-text" rows="3" placeholder="Например: рассветная прогулка в осеннем лесу..." style="width:100%;box-sizing:border-box;background:rgba(0,0,0,.2);border:1px solid rgba(167,139,250,.3);border-radius:8px;color:var(--text);font-size:.88em;padding:8px 10px;resize:vertical;font-family:inherit" onkeydown="if(event.ctrlKey&&event.key===\'Enter\')startWithIdea()"></textarea>
    <div style="display:flex;gap:8px;margin-top:8px">
      <button class="btn btn-primary" style="flex:1;padding:8px 0" onclick="startWithIdea()">🚀 Поехали</button>
      <button onclick="closeModePicker()" style="padding:8px 16px;background:transparent;border:1px solid var(--border);border-radius:8px;color:var(--muted);cursor:pointer;font-size:.85em">Отмена</button>
    </div>
  </div>
</div>

<div id="gen-banner" style="display:none;border:1px solid rgba(167,139,250,.3);background:rgba(167,139,250,.07);border-radius:12px;margin-bottom:16px;padding:14px 16px;font-size:.88em">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;gap:8px">
    <span id="gen-label" style="font-weight:600;color:var(--text)">⚙️ Генерация...</span>
    <span id="gen-bar-txt" style="color:var(--muted);font-size:.82em;white-space:nowrap">0%</span>
  </div>
  <div style="background:rgba(255,255,255,.08);border-radius:6px;height:6px;margin-bottom:10px;overflow:hidden">
    <div id="gen-bar" style="height:100%;width:0%;background:linear-gradient(90deg,#a78bfa,#60a5fa);border-radius:6px;transition:width .4s ease"></div>
  </div>
  <div id="gen-log" style="max-height:130px;overflow-y:auto;font-family:monospace;line-height:1.5;margin-bottom:6px"></div>
  <!-- Inline trend picker (shown when scheduler waits for selection) -->
  <div id="gen-trend-picker" style="display:none;margin-top:10px;border-top:1px solid rgba(167,139,250,.2);padding-top:12px">
    <div style="font-size:.85em;color:var(--accent);font-weight:600;margin-bottom:8px">📈 Выберите тренд для поста:</div>
    <div id="gen-trend-list" style="display:flex;flex-direction:column;gap:6px"></div>
    <button onclick="submitTrendPick(null)" style="margin-top:8px;width:100%;padding:8px 12px;background:rgba(255,255,255,.05);border:1px solid var(--border);border-radius:8px;color:var(--muted);cursor:pointer;font-size:.82em;text-align:left">🎲 Случайный — пусть Gemini выберет сам</button>
  </div>
  <details id="gen-think-wrap" style="display:none">
    <summary style="cursor:pointer;font-size:.8em;color:var(--accent);user-select:none;padding:4px 0">🧠 Процесс мышления Gemini <span id="gen-think-chars" style="color:var(--muted)"></span></summary>
    <div id="gen-thinking" style="margin-top:8px;padding:10px;background:rgba(0,0,0,.25);border-radius:8px;max-height:220px;overflow-y:auto;font-family:monospace;font-size:.76em;color:rgba(200,200,255,.65);white-space:pre-wrap;line-height:1.55;word-break:break-word"></div>
  </details>
</div>'''

    # ── History tab ──
    if history_posts:
        hist_rows = ""
        mob_cards = ""
        for p in history_posts:
            pid = p["id"]
            fuid = p.get("tg_file_unique","")
            img_src = f"/admin/tg-photo/{fuid}" if fuid else ""
            thumb = f'<img src="{img_src}" loading="lazy" style="width:44px;height:44px;object-fit:cover;border-radius:6px;cursor:pointer" onclick="openLightboxUrl(\'{img_src}\')">' if img_src else '<div style="width:44px;height:44px;border-radius:6px;background:rgba(255,255,255,.05)"></div>'
            topic = p.get("topic","").replace("<","&lt;")[:55]
            dt_pub = _msk(p.get("published_at","")) or _msk(p.get("created_at",""))
            tg_link = f'<a href="https://t.me/c/{p.get("tg_msg_id","")}" target="_blank" style="color:var(--accent2);font-size:.82em">📩 TG</a>' if p.get("tg_msg_id") else ""
            vk_link = f'<a href="https://vk.com/wall-{p.get("vk_post_id","")}" target="_blank" style="color:var(--accent);font-size:.82em">🅥 VK</a>' if p.get("vk_post_id") else ""
            tg_td = f'<a href="https://t.me/c/{p.get("tg_msg_id","")}" target="_blank" style="color:var(--accent2)">TG</a>' if p.get("tg_msg_id") else "—"
            vk_td = f'<a href="https://vk.com/wall-{p.get("vk_post_id","")}" target="_blank" style="color:var(--accent)">VK</a>' if p.get("vk_post_id") else "—"
            err = f'<span style="color:var(--red);font-size:.75em">{p.get("error_text","")[:60]}</span>' if p.get("error_text") else ""
            hist_rows += (
                f"<tr>"
                f"<td style='padding:5px 8px'>{thumb}</td>"
                f"<td style='padding:5px 4px;font-size:.83em'>{topic}</td>"
                f"<td style='padding:5px 8px'>{_autopub_status_badge(p['status'])}</td>"
                f"<td class='hist-mobile-hide' style='padding:5px 8px;white-space:nowrap;color:var(--muted);font-size:.79em'>{dt_pub}</td>"
                f"<td class='hist-mobile-hide' style='padding:5px 8px'>{tg_td}</td>"
                f"<td class='hist-mobile-hide' style='padding:5px 8px'>{vk_td}</td>"
                f"<td style='padding:5px 8px'>{err}<button class='btn btn-danger btn-sm' onclick='postAction({pid},\"delete\")'>🗑</button></td>"
                f"</tr>"
            )
            mc_img = (
                f'<img src="{img_src}" loading="lazy" onclick="openLightboxUrl(\'{img_src}\')" style="cursor:pointer;object-fit:cover;border-radius:8px;width:52px;height:52px;min-width:52px;flex-shrink:0">'
                if img_src else
                f'<div style="width:52px;height:52px;min-width:52px;border-radius:8px;background:rgba(255,255,255,.06);flex-shrink:0"></div>'
            )
            mob_cards += (
                f'<div class="hist-card">'
                f'{mc_img}'
                f'<div style="flex:1;min-width:0">'
                f'<div style="font-size:.86em;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{topic}</div>'
                f'<div style="font-size:.75em;color:var(--muted);margin-top:2px">{dt_pub}</div>'
                f'<div style="margin-top:6px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
                f'{_autopub_status_badge(p["status"])} {tg_link} {vk_link}'
                f'<button class="btn btn-danger btn-sm" onclick="postAction({pid},\'delete\')">🗑</button>'
                f'</div>{err}</div></div>'
            )
        history_html = (
            f'<div class="table-wrap hist-table-wrap">'
            f'<table><thead><tr>'
            f'<th>Фото</th><th>Тема</th><th>Статус</th>'
            f'<th class="hist-mobile-hide">Опубликован</th>'
            f'<th class="hist-mobile-hide">TG</th>'
            f'<th class="hist-mobile-hide">VK</th>'
            f'<th></th>'
            f'</tr></thead><tbody>{hist_rows}</tbody></table></div>'
            f'<div class="hist-mob-list" style="display:none">{mob_cards}</div>'
        )
    else:
        history_html = '<div class="img-empty">Нет опубликованных постов</div>'

    content = f"""
{alert_html}

<div class="autopub-header">
  <h2 style="margin:0;font-size:1.35em">📣 Автопостинг</h2>
  {enabled_badge}
  <span style="color:var(--muted);font-size:.83em">Опубликовано: {posts_today} / {settings.get('posts_per_day',3)}</span>
</div>

<div class="autopub-tabs">
  <a href="/admin/autopub?tab=queue" class="{'tab-active' if tab=='queue' else ''}" style="color:{'var(--text)' if tab=='queue' else 'var(--muted)'}">⏳ Очередь ({len(queue_posts)})</a>
  <a href="/admin/autopub?tab=settings" class="{'tab-active' if tab=='settings' else ''}" style="color:{'var(--text)' if tab=='settings' else 'var(--muted)'}">⚙️ Настройки</a>
  <a href="/admin/autopub?tab=history" class="{'tab-active' if tab=='history' else ''}" style="color:{'var(--text)' if tab=='history' else 'var(--muted)'}">📋 История ({len(history_posts)})</a>
</div>

{'<div>' + gen_btn + '<div id="queue-list">' + queue_html + '</div></div>' if tab == 'queue' else ''}
{'<div>' + settings_html + '</div>' if tab == 'settings' else ''}
{'<div>' + history_html + '</div>' if tab == 'history' else ''}

<!-- Edit modal -->
<div id="edit-modal" class="modal-overlay" style="display:none">
  <div class="modal" style="max-width:640px;width:95vw">
    <h3 style="margin-bottom:14px">✏️ Редактировать пост</h3>
    <input type="hidden" id="edit-post-id">
    <div style="margin-bottom:10px">
      <label class="form-label">Тема</label>
      <input id="edit-topic" class="form-input">
    </div>
    <div style="margin-bottom:10px">
      <label class="form-label">Текст поста</label>
      <textarea id="edit-caption" rows="8" class="form-input"></textarea>
    </div>
    <div style="margin-bottom:16px">
      <label class="form-label">Промпт (только для справки)</label>
      <textarea id="edit-prompt" rows="4" class="form-input"></textarea>
    </div>
    <div style="display:flex;gap:10px;justify-content:flex-end">
      <button class="btn btn-muted" onclick="closeEditModal()">Отмена</button>
      <button class="btn btn-primary" onclick="saveEdit()">💾 Сохранить</button>
    </div>
  </div>
</div>

<!-- Lightbox -->
<div class="lightbox" id="lightbox" onclick="closeLightbox()">
  <span class="lightbox-close" onclick="closeLightbox()">×</span>
  <img id="lightbox-img" src="" alt="">
</div>

<script>
var _editPosts = {json.dumps(_edit_posts_map, ensure_ascii=False).replace('</','<\\/') if _edit_posts_map else '{{}}'};

function openLightboxUrl(src){{
  document.getElementById('lightbox-img').src=src;
  document.getElementById('lightbox').classList.add('open');
}}
function closeLightbox(){{
  document.getElementById('lightbox').classList.remove('open');
  document.getElementById('lightbox-img').src='';
}}
document.addEventListener('keydown',function(e){{if(e.key==='Escape')closeLightbox();}});
function copyPrompt(el){{
  var text = el.dataset.fullPrompt || el.innerText;
  navigator.clipboard.writeText(text).then(function(){{
    var orig = el.style.background;
    el.style.background = 'rgba(74,222,128,.15)';
    var origColor = el.style.color;
    el.style.color = '#4ade80';
    setTimeout(function(){{
      el.style.background = orig;
      el.style.color = origColor;
    }}, 1200);
  }}).catch(function(){{
    var r = document.createRange();
    r.selectNode(el);
    window.getSelection().removeAllRanges();
    window.getSelection().addRange(r);
  }});
}}

/* ── Live generation progress via SSE ── */
var _genES = null;
var _genDone = false;

function _genPanel(){{ return document.getElementById('gen-banner'); }}

function _genSetBar(pct){{
  var b = document.getElementById('gen-bar');
  if(b) b.style.width = pct+'%';
  var t = document.getElementById('gen-bar-txt');
  if(t) t.textContent = pct+'%';
}}

function _genAddLog(entry){{
  var box = document.getElementById('gen-log');
  if(!box) return;
  var div = document.createElement('div');
  div.style.cssText='padding:3px 0;border-bottom:1px solid rgba(255,255,255,.05);font-size:.82em;display:flex;gap:8px;align-items:baseline';
  var ts = document.createElement('span');
  ts.style.cssText='color:var(--muted);min-width:42px;flex-shrink:0';
  ts.textContent = '+'+entry.t+'s';
  var msg = document.createElement('span');
  msg.style.color = entry.err ? '#f87171' : (entry.ok ? 'var(--text)' : 'var(--muted)');
  if(entry.err) msg.textContent = '❌ '+entry.msg;
  else msg.textContent = entry.msg;
  div.appendChild(ts); div.appendChild(msg);
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}}

function _genShowPanel(label, pct){{
  var p = _genPanel();
  if(!p) return;
  p.style.display='block';
  var lbl = document.getElementById('gen-label');
  if(lbl) lbl.textContent = label;
  _genSetBar(pct);
}}

function _genAppendThinking(delta){{
  if(!delta) return;
  var wrap = document.getElementById('gen-think-wrap');
  var box  = document.getElementById('gen-thinking');
  var chars= document.getElementById('gen-think-chars');
  if(!wrap||!box) return;
  wrap.style.display='block';
  box.textContent += delta;
  box.scrollTop = box.scrollHeight;
  if(chars) chars.textContent = '(' + box.textContent.length + ' симв.)';
}}

function _genResetThinking(){{
  var wrap=document.getElementById('gen-think-wrap');
  var box=document.getElementById('gen-thinking');
  var chars=document.getElementById('gen-think-chars');
  if(wrap) wrap.style.display='none';
  if(box) box.textContent='';
  if(chars) chars.textContent='';
}}

async function _genInjectCard(postId){{
  if(!postId) return;
  try{{
    var r=await fetch('/admin/api/autopub/queue-fragment?id='+postId);
    var html=await r.text();
    var list=document.getElementById('queue-list');
    if(list && html.trim()){{
      var empty=document.getElementById('queue-empty');
      if(empty) empty.remove();
      var tmp=document.createElement('div');
      tmp.innerHTML=html;
      list.insertBefore(tmp.firstChild, list.firstChild);
    }}
  }}catch(e){{ console.warn('queue-fragment fetch failed',e); }}
}}

function _genConnectSSE(){{
  if(_genES){{ _genES.close(); _genES=null; }}
  _genES = new EventSource('/admin/api/autopub/stream');
  _genES.onmessage = function(e){{
    var d;
    try{{ d=JSON.parse(e.data); }}catch(ex){{ return; }}

    if(d.new_log && d.new_log.length){{
      d.new_log.forEach(function(entry){{ _genAddLog(entry); }});
    }}

    if(d.thinking_delta){{ _genAppendThinking(d.thinking_delta); }}

    // Trend picker: show when scheduler is waiting for selection
    var picker=document.getElementById('gen-trend-picker');
    var tlist=document.getElementById('gen-trend-list');
    if(d.trends && d.trends.length && picker){{
      if(picker.style.display==='none'){{
        tlist.innerHTML='';
        d.trends.forEach(function(t){{
          var btn=document.createElement('button');
          btn.style.cssText='width:100%;padding:9px 13px;background:rgba(167,139,250,.1);border:1px solid rgba(167,139,250,.25);border-radius:8px;color:var(--text);cursor:pointer;font-size:.85em;text-align:left;transition:.15s';
          btn.onmouseover=function(){{this.style.background='rgba(167,139,250,.22)';}};
          btn.onmouseout=function(){{this.style.background='rgba(167,139,250,.1)';}};
          var ctx=t.context?(' <span style="color:var(--muted);font-size:.8em">— '+t.context.slice(0,80)+'</span>'):'';
          btn.innerHTML='<b>'+t.trend+'</b>'+ctx;
          btn.onclick=(function(trend){{return function(){{submitTrendPick(trend);}};}})(t);
          tlist.appendChild(btn);
        }});
        picker.style.display='block';
      }}
    }} else if(picker && d.trends===null && picker.style.display!=='none'){{
      picker.style.display='none';
    }}

    _genShowPanel(d.label || '⚙️ Генерация...', d.pct || 0);

    if(d.error){{
      _genES.close(); _genES=null;
      var p=_genPanel();
      if(p){{
        p.style.borderColor='rgba(248,113,113,.4)';
        p.style.background='rgba(248,113,113,.08)';
      }}
      var lbl=document.getElementById('gen-label');
      if(lbl) lbl.textContent='❌ Ошибка: '+d.error;
      _genSetBar(0);
      var gb=document.getElementById('gen-btn');
      if(gb){{ gb.disabled=false; gb.textContent='⚡ Сгенерировать пост'; }}
      return;
    }}

    if(d.done){{
      _genDone=true;
      _genES.close(); _genES=null;
      _genSetBar(100);
      var p=_genPanel();
      if(p){{ p.style.borderColor='rgba(52,211,153,.4)'; p.style.background='rgba(52,211,153,.08)'; }}
      var gb=document.getElementById('gen-btn');
      if(gb){{ gb.disabled=false; gb.textContent='⚡ Сгенерировать пост'; }}
      _genInjectCard(d.last_post_id).then(function(){{
        setTimeout(function(){{
          var p2=_genPanel();
          if(p2) p2.style.display='none';
          _genResetThinking();
        }}, 3500);
      }});
    }}
  }};
  _genES.onerror = function(){{
    if(_genDone){{ _genES.close(); _genES=null; }}
  }};
}}

async function submitTrendPick(trend){{
  // trend = object or null (random)
  var picker=document.getElementById('gen-trend-picker');
  if(picker) picker.style.display='none';
  try{{
    await fetch('/admin/api/autopub/select_trend',{{
      method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify(trend ? {{trend:trend}} : {{}}),
    }});
  }}catch(e){{ console.warn('select_trend error',e); }}
}}

function openModePicker(btn){{
  btn.disabled=true;
  var picker=document.getElementById('gen-mode-picker');
  if(picker){{ picker.style.display='block'; }}
  var ideaInp=document.getElementById('gen-idea-input');
  if(ideaInp){{ ideaInp.style.display='none'; }}
  var ta=document.getElementById('gen-idea-text');
  if(ta){{ ta.value=''; }}
}}

function closeModePicker(){{
  var picker=document.getElementById('gen-mode-picker');
  if(picker){{ picker.style.display='none'; }}
  var btn=document.getElementById('gen-btn');
  if(btn){{ btn.disabled=false; btn.textContent='⚡ Сгенерировать'; }}
}}

function showIdeaInput(){{
  var inp=document.getElementById('gen-idea-input');
  if(inp){{ inp.style.display='block'; }}
  var ta=document.getElementById('gen-idea-text');
  if(ta){{ ta.focus(); }}
}}

async function startWithIdea(){{
  var ta=document.getElementById('gen-idea-text');
  var idea=(ta?ta.value:'').trim();
  if(!idea){{ if(ta) ta.focus(); return; }}
  var picker=document.getElementById('gen-mode-picker');
  if(picker){{ picker.style.display='none'; }}
  await _doGenerate({{user_idea:idea}});
}}

async function startWithTrends(){{
  var picker=document.getElementById('gen-mode-picker');
  if(picker){{ picker.style.display='none'; }}
  await _doGenerate({{}});
}}

async function _doGenerate(body){{
  var btn=document.getElementById('gen-btn');
  if(btn){{ btn.disabled=true; btn.textContent='⏳ Генерация...'; }}
  try{{
    var r=await fetch('/admin/api/autopub/generate',{{
      method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify(body),
    }});
    var d=await r.json();
    if(d.ok){{
      _genDone=false;
      var p=_genPanel();
      if(p){{
        p.style.display='block';
        p.style.borderColor='rgba(167,139,250,.3)';
        p.style.background='rgba(167,139,250,.07)';
        var logBox=document.getElementById('gen-log');
        if(logBox) logBox.innerHTML='';
        _genSetBar(0);
        _genResetThinking();
      }}
      _genConnectSSE();
    }} else {{
      alert('Ошибка: '+(d.error||'неизвестная'));
      if(btn){{ btn.disabled=false; btn.textContent='⚡ Сгенерировать'; }}
    }}
  }}catch(e){{
    alert('Fetch error: '+e);
    if(btn){{ btn.disabled=false; btn.textContent='⚡ Сгенерировать'; }}
  }}
}}

/* Auto-connect SSE if generation is already running (after page reload) */
(function(){{
  fetch('/admin/api/autopub/status').then(function(r){{ return r.json(); }}).then(function(d){{
    if(d.active){{
      _genDone=false;
      var p=_genPanel();
      if(p){{ p.style.display='block'; p.style.borderColor='rgba(167,139,250,.3)'; p.style.background='rgba(167,139,250,.07)'; }}
      _genConnectSSE();
      var gb=document.getElementById('gen-btn');
      if(gb){{ gb.disabled=true; gb.textContent='⏳ Генерация...'; }}
    }}
  }}).catch(function(){{}});
}})();

async function postAction(id, action){{
  if(action==='reject'||action==='delete'){{
    if(!confirm('Удалить пост?')) return;
  }}
  var r=await fetch('/admin/api/autopub/posts/'+id+'/'+action,{{method:'POST'}});
  var d=await r.json();
  if(d.ok){{
    if(action==='publish') location.href='/admin/autopub?tab=history&msg=published';
    else location.reload();
  }} else alert('Ошибка: '+(d.error||d));
}}

function openEditModal(id){{
  var p=_editPosts[id];
  if(!p) return;
  document.getElementById('edit-post-id').value=id;
  document.getElementById('edit-topic').value=p.topic||'';
  document.getElementById('edit-caption').value=p.caption||'';
  document.getElementById('edit-prompt').value=p.prompt||'';
  document.getElementById('edit-modal').style.display='flex';
}}
function closeEditModal(){{ document.getElementById('edit-modal').style.display='none'; }}

async function saveEdit(){{
  var id=document.getElementById('edit-post-id').value;
  var body={{
    topic: document.getElementById('edit-topic').value,
    caption: document.getElementById('edit-caption').value,
    prompt: document.getElementById('edit-prompt').value,
  }};
  var r=await fetch('/admin/api/autopub/posts/'+id+'/edit',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body)}});
  var d=await r.json();
  if(d.ok){{ closeEditModal(); location.reload(); }}
  else alert('Ошибка: '+(d.error||d));
}}

function openFeedbackModal(id){{
  document.getElementById('feedback-post-id').value=id;
  document.getElementById('feedback-text').value='';
  document.getElementById('feedback-modal').style.display='flex';
  setTimeout(()=>document.getElementById('feedback-text').focus(),100);
}}
function closeFeedbackModal(){{ document.getElementById('feedback-modal').style.display='none'; }}

async function submitFeedback(btn){{
  var id=document.getElementById('feedback-post-id').value;
  var comment=document.getElementById('feedback-text').value.trim();
  if(!comment){{ alert('Напишите комментарий — что именно исправить'); return; }}
  btn.disabled=true; btn.textContent='⏳ Отправляю...';
  try{{
    var r=await fetch('/admin/api/autopub/posts/'+id+'/reject_feedback',{{
      method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{comment:comment}})
    }});
    var d=await r.json();
    if(d.ok){{
      closeFeedbackModal();
      _genDone=false;
      var p=_genPanel();
      if(p){{
        p.style.display='block';
        p.style.borderColor='rgba(251,191,36,.3)';
        p.style.background='rgba(251,191,36,.07)';
        var logBox=document.getElementById('gen-log');
        if(logBox) logBox.innerHTML='';
        _genSetBar(0);
        _genResetThinking();
        var lbl=document.getElementById('gen-label');
        if(lbl) lbl.textContent='🔄 Перегенерация с фидбэком...';
      }}
      _genConnectSSE();
      var gb=document.getElementById('gen-btn');
      if(gb){{ gb.disabled=true; gb.textContent='⏳ Перегенерация...'; }}
    }} else alert('Ошибка: '+(d.error||d));
  }}catch(e){{ alert('Ошибка: '+e); }}
  finally{{ btn.disabled=false; btn.textContent='🔄 Переделать'; }}
}}
</script>

<div id="feedback-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:1000;align-items:center;justify-content:center">
  <div style="background:#14122a;border:1px solid var(--border);border-radius:16px;padding:28px;width:min(480px,94vw);position:relative">
    <h3 style="margin:0 0 6px">🔄 Переделать пост</h3>
    <p style="color:var(--muted);font-size:.85em;margin:0 0 16px">Напишите что не так — ИИ учтёт фидбэк и сгенерирует новый пост</p>
    <input type="hidden" id="feedback-post-id">
    <textarea id="feedback-text" class="form-input" rows="4"
      placeholder="Например: слишком общая тема, хочу про весну и цветы; или: картинка должна быть более минималистичной; или: текст слишком длинный"></textarea>
    <div style="display:flex;gap:10px;justify-content:flex-end;margin-top:14px">
      <button class="btn btn-muted" onclick="closeFeedbackModal()">Отмена</button>
      <button class="btn btn-primary" onclick="submitFeedback(this)" style="background:rgba(251,191,36,.2);border-color:rgba(251,191,36,.5);color:#fbbf24">🔄 Переделать</button>
    </div>
  </div>
</div>"""

    return web.Response(text=_layout("Автопостинг", content, "autopub"), content_type="text/html")


@_api_require_auth
async def api_autopub_settings(request: web.Request) -> web.Response:
    """Save autopub settings from form POST."""
    try:
        data = await request.post()
        s = {
            "enabled":       "enabled" in data,
            "auto_approve":  "auto_approve" in data,
            "tg_channel_id": data.get("tg_channel_id","").strip(),
            "vk_group_id":   data.get("vk_group_id","").strip(),
            "posts_per_day": max(1, min(24, int(data.get("posts_per_day", 3)))),
            "topic_hints":   data.get("topic_hints","").strip(),
            "post_template": data.get("post_template","").strip(),
            "post_cta":      data.get("post_cta","").strip(),
            "bot_username":  data.get("bot_username","").strip().lstrip("@"),
            "image_style":   data.get("image_style","").strip(),
        }
        _db.autopub_save_settings(s)
        raise web.HTTPFound("/admin/autopub?tab=settings&msg=saved")
    except web.HTTPException:
        raise
    except Exception as e:
        logger.exception("api_autopub_settings error")
        raise web.HTTPFound("/admin/autopub?tab=settings&msg=err")


@_api_require_auth
async def api_autopub_fetch_trends(request: web.Request) -> web.Response:
    """Fetch current trends (for manual trend picker before generation)."""
    if _vertex_service is None:
        return web.Response(text=json.dumps({"ok": False, "error": "vertex service not ready"}),
                            content_type="application/json", status=503)
    try:
        from bot.autopub.generator import search_current_trends
        import bot.db as _db2
        recent_topics = _db2.autopub_get_recent_topics(limit=30)
        trends = await search_current_trends(_vertex_service, used_topics=recent_topics)
        return web.Response(
            text=json.dumps({"ok": True, "trends": trends or []}),
            content_type="application/json",
        )
    except Exception as e:
        logger.exception("[autopub] api_autopub_fetch_trends error")
        return web.Response(text=json.dumps({"ok": False, "error": str(e)}),
                            content_type="application/json", status=500)


@_api_require_auth
async def api_autopub_generate(request: web.Request) -> web.Response:
    """Trigger immediate post generation (runs in background task).
    Body (optional JSON): {"chosen_trend": {"trend": "...", "context": "..."}}
    """
    logger.info("[autopub] admin нажал 'Сгенерировать пост'")
    if _vertex_service is None:
        logger.error("[autopub] _vertex_service is None — VertexAI не инициализирован")
        return web.Response(text=json.dumps({"ok": False, "error": "vertex service not ready"}),
                            content_type="application/json", status=503)
    try:
        chosen_trend = None
        user_idea = ""
        try:
            body = await request.json()
            chosen_trend = body.get("chosen_trend") or None
            user_idea = (body.get("user_idea") or "").strip()
        except Exception:
            pass
        settings = _db.autopub_get_settings()
        logger.info("[autopub] настройки: tg_channel=%r  vk_group=%r  bot=%r  trend=%r  idea=%r",
                    settings.get("tg_channel_id"), settings.get("vk_group_id"),
                    settings.get("bot_username"),
                    chosen_trend.get("trend") if chosen_trend else "random",
                    user_idea[:40] if user_idea else "")
        import asyncio
        from bot.autopub.scheduler import _run_generate
        _gen_progress_reset()
        asyncio.create_task(_run_generate(
            _vertex_service, settings,
            chosen_trend=chosen_trend,
            manual=True,
            user_idea=user_idea,
        ))
        logger.info("[autopub] задача генерации запущена в фоне (manual=True)")
        return web.Response(text=json.dumps({"ok": True}), content_type="application/json")
    except Exception as e:
        logger.exception("[autopub] api_autopub_generate error")
        return web.Response(text=json.dumps({"ok": False, "error": str(e)}),
                            content_type="application/json", status=500)


@_api_require_auth
async def api_autopub_select_trend(request: web.Request) -> web.Response:
    """Admin picks a trend from the SSE trend picker. Body: {"trend": {...}} or {} for random."""
    try:
        body = await request.json()
        trend = body.get("trend") or None  # None = pick random
        set_trend_selection(trend)
        logger.info("[autopub] trend selected: %r", trend.get("trend") if trend else "random")
        return web.Response(text=json.dumps({"ok": True}), content_type="application/json")
    except Exception as e:
        logger.exception("[autopub] api_autopub_select_trend error")
        return web.Response(text=json.dumps({"ok": False, "error": str(e)}),
                            content_type="application/json", status=500)


@_api_require_auth
async def api_autopub_post_action(request: web.Request) -> web.Response:
    """Approve / reject / delete / publish / edit a single post."""
    try:
        action = request.match_info["action"]
        post_id = int(request.match_info["post_id"])

        if action == "approve":
            _db.autopub_update_post(post_id, status="approved")
            return web.Response(text=json.dumps({"ok": True}), content_type="application/json")

        elif action in ("reject", "delete"):
            _db.autopub_delete_post(post_id)
            return web.Response(text=json.dumps({"ok": True}), content_type="application/json")

        elif action == "publish":
            posts = _db.autopub_get_posts(limit=100)
            post = next((p for p in posts if p["id"] == post_id), None)
            if not post:
                return web.Response(text=json.dumps({"ok": False, "error": "not found"}),
                                    content_type="application/json", status=404)
            settings = _db.autopub_get_settings()
            import asyncio
            from bot.autopub.scheduler import _run_publish as _pub
            # Mark as approved so _run_publish picks it up
            _db.autopub_update_post(post_id, status="approved")

            async def _do_pub():
                from bot.autopub.publisher import publish_to_telegram, publish_to_vk
                from bot.autopub.generator import build_vk_post_text
                import datetime
                _MSK2 = datetime.timezone(datetime.timedelta(hours=3))
                tg_channel = settings.get("tg_channel_id","").strip()
                vk_group   = settings.get("vk_group_id","").strip()
                tg_msg_id  = await publish_to_telegram(tg_channel, post["tg_file_id"], post["caption"]) if tg_channel else None
                vk_text    = build_vk_post_text(
                    topic=post.get("topic", ""),
                    caption_intro=post.get("topic", ""),
                    prompt=post.get("prompt", ""),
                    vk_community="picgenai",
                )
                vk_post_id = await publish_to_vk(vk_group, post["tg_file_id"], vk_text) if vk_group else None
                status = "published" if (tg_msg_id or vk_post_id) else "error"
                _db.autopub_update_post(post_id, status=status, tg_msg_id=tg_msg_id,
                                        vk_post_id=vk_post_id,
                                        published_at=datetime.datetime.now(_MSK2).isoformat())
            asyncio.create_task(_do_pub())
            return web.Response(text=json.dumps({"ok": True}), content_type="application/json")

        elif action == "edit":
            data = await request.json()
            fields = {}
            if "topic"   in data: fields["topic"]   = str(data["topic"])
            if "caption" in data: fields["caption"] = str(data["caption"])
            if "prompt"  in data: fields["prompt"]  = str(data["prompt"])
            if fields:
                _db.autopub_update_post(post_id, **fields)
            return web.Response(text=json.dumps({"ok": True}), content_type="application/json")

        elif action == "reject_feedback":
            if _vertex_service is None:
                return web.Response(text=json.dumps({"ok": False, "error": "vertex service not ready"}),
                                    content_type="application/json", status=503)
            data = await request.json()
            comment = str(data.get("comment", "")).strip()
            if not comment:
                return web.Response(text=json.dumps({"ok": False, "error": "comment required"}),
                                    content_type="application/json", status=400)
            logger.info("[autopub] admin отклонил пост id=%s с фидбэком: %r", post_id, comment[:80])
            _db.autopub_delete_post(post_id)
            settings = _db.autopub_get_settings()
            import asyncio
            from bot.autopub.scheduler import _run_generate
            _gen_progress_reset()
            asyncio.create_task(_run_generate(_vertex_service, settings, admin_feedback=comment))
            logger.info("[autopub] запущена перегенерация с фидбэком")
            return web.Response(text=json.dumps({"ok": True}), content_type="application/json")

        return web.Response(text=json.dumps({"ok": False, "error": "unknown action"}),
                            content_type="application/json", status=400)
    except Exception as e:
        logger.exception("api_autopub_post_action error")
        return web.Response(text=json.dumps({"ok": False, "error": str(e)}),
                            content_type="application/json", status=500)


@_api_require_auth
async def api_autopub_status(request: web.Request) -> web.Response:
    """Return current queue count and generation active flag."""
    try:
        drafts   = _db.autopub_get_posts(status="draft",    limit=100)
        approved = _db.autopub_get_posts(status="approved", limit=100)
        queue_count = len(drafts) + len(approved)
        return web.Response(text=json.dumps({
            "ok": True,
            "queue_count": queue_count,
            "active": _gen_progress.get("active", False),
        }), content_type="application/json")
    except Exception as e:
        return web.Response(text=json.dumps({"ok": False, "queue_count": 0, "active": False, "error": str(e)}),
                            content_type="application/json", status=500)


async def api_autopub_stream(request: web.Request) -> web.StreamResponse:
    """SSE stream — pushes live generation progress to the browser."""
    import asyncio as _aio

    # Auth check — same stateless HMAC as all other admin handlers
    if not _is_auth(request):
        return web.Response(status=403, text="Unauthorized")

    resp = web.StreamResponse(headers={
        "Content-Type":  "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })
    await resp.prepare(request)

    last_log_len = 0
    last_thinking_len = 0
    try:
        while True:
            prog = _gen_progress
            # New log entries
            current_log = prog.get("log", [])
            new_entries = current_log[last_log_len:]
            last_log_len = len(current_log)
            # Thinking delta (new chars since last send)
            thinking_full = prog.get("thinking_buf", "")
            thinking_delta = thinking_full[last_thinking_len:]
            last_thinking_len = len(thinking_full)

            payload = {
                "active":        prog.get("active", False),
                "step":          prog.get("step", 0),
                "total":         prog.get("total", 5),
                "label":         prog.get("label", ""),
                "pct":           prog.get("pct", 0),
                "done":          prog.get("done", False),
                "error":         prog.get("error", ""),
                "new_log":       new_entries,
                "thinking_delta": thinking_delta,
                "last_post_id":  prog.get("last_post_id"),
                "trends":        prog.get("trends"),  # list when trend picker needed, else None
            }
            data = json.dumps(payload, ensure_ascii=False)
            await resp.write(f"data: {data}\n\n".encode())

            if prog.get("done") or prog.get("error"):
                break

            await _aio.sleep(0.4)
    except (ConnectionResetError, Exception):
        pass

    return resp


@_require_auth
async def api_autopub_queue_fragment(request: web.Request) -> web.Response:
    """Return rendered HTML for a single post card (for live injection)."""
    post_id_str = request.rel_url.query.get("id", "")
    try:
        post_id = int(post_id_str)
    except (ValueError, TypeError):
        return web.Response(text="", content_type="text/html")
    posts = _db.autopub_get_posts(status="draft", limit=50) + _db.autopub_get_posts(status="approved", limit=50)
    post = next((p for p in posts if p["id"] == post_id), None)
    if post is None:
        return web.Response(text="", content_type="text/html")
    return web.Response(text=_render_post_card(post), content_type="text/html")


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
    app.router.add_get("/admin/autopub",                  handle_autopub)
    app.router.add_post("/admin/api/autopub/settings",       api_autopub_settings)
    app.router.add_get("/admin/api/autopub/trends",          api_autopub_fetch_trends)
    app.router.add_post("/admin/api/autopub/generate",       api_autopub_generate)
    app.router.add_post("/admin/api/autopub/select_trend",   api_autopub_select_trend)
    app.router.add_get("/admin/api/autopub/status",          api_autopub_status)
    app.router.add_get("/admin/api/autopub/stream",          api_autopub_stream)
    app.router.add_get("/admin/api/autopub/queue-fragment",  api_autopub_queue_fragment)
    app.router.add_post("/admin/api/autopub/posts/{post_id}/{action}", api_autopub_post_action)
    app.router.add_post("/admin/api/users/{uid}/credits",    api_credits)
    app.router.add_post("/admin/api/users/{uid}/block",      api_block)
    app.router.add_post("/admin/api/users/{uid}/reset_gens", api_reset_gens)
    app.router.add_post("/admin/api/users/{uid}/delete",     api_delete)
    app.router.add_post("/admin/api/test-log-channel",       api_test_log_channel)
    app.router.add_get("/admin/api/keys/status",             api_keys_status)
    app.router.add_post("/admin/api/keys/add",               api_keys_add)
    app.router.add_post("/admin/api/keys/update",            api_keys_update)
    app.router.add_post("/admin/api/keys/delete",            api_keys_delete)
    app.router.add_get("/admin/api/keys/{index}/history",    api_keys_history)
    app.router.add_get("/admin/tg-photo/{file_unique_id}",   handle_tg_photo)
    app.router.add_get("/admin/tg-photo-fid/{file_id}",     handle_tg_photo_by_fileid)
    logger.info("Admin panel routes registered at /admin")
