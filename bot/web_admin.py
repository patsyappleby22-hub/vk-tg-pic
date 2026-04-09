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

from aiohttp import web

import bot.db as _db
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

_ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "mrxgyt02")
_SESSION_SECRET = hashlib.sha256((_ADMIN_PASSWORD + "_picgenai_admin_v1").encode()).hexdigest()
_COOKIE_NAME = "admin_tok"
_COOKIE_MAX_AGE = 86400 * 7  # 7 days
_PAGE_SIZE = 50


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
        ("dashboard", "/admin/dashboard", "📊 Дашборд"),
        ("users",     "/admin/users",     "👥 Пользователи"),
        ("payments",  "/admin/payments",  "💳 Платежи"),
    ]
    nav_html = ""
    for key, href, label in nav_items:
        cls = "nav-link active" if active == key else "nav-link"
        nav_html += f'<a href="{href}" class="{cls}">{label}</a>\n'

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
    background:var(--bg);color:var(--text);min-height:100vh;display:flex}}
  a{{color:var(--accent);text-decoration:none}}
  a:hover{{opacity:.8}}
  /* Sidebar */
  .sidebar{{width:220px;min-height:100vh;background:var(--surface);
    border-right:1px solid var(--border);padding:24px 0;display:flex;
    flex-direction:column;flex-shrink:0}}
  .sidebar-logo{{padding:0 20px 24px;font-size:1.15em;font-weight:700;
    background:linear-gradient(135deg,var(--accent),var(--accent2));
    -webkit-background-clip:text;-webkit-text-fill-color:transparent}}
  .nav-link{{display:block;padding:10px 20px;color:var(--muted);
    font-size:.95em;border-left:3px solid transparent;transition:.15s}}
  .nav-link:hover{{color:var(--text);background:rgba(167,139,250,.06)}}
  .nav-link.active{{color:var(--accent);border-left-color:var(--accent);
    background:rgba(167,139,250,.08)}}
  .sidebar-bottom{{margin-top:auto;padding:20px}}
  .logout-btn{{display:block;padding:9px 16px;background:rgba(248,113,113,.1);
    border:1px solid rgba(248,113,113,.2);border-radius:8px;color:var(--red);
    text-align:center;font-size:.9em}}
  .logout-btn:hover{{background:rgba(248,113,113,.2);opacity:1}}
  /* Main */
  .main{{flex:1;padding:32px;overflow-x:auto}}
  .page-title{{font-size:1.6em;font-weight:700;margin-bottom:24px;
    background:linear-gradient(135deg,var(--accent),var(--accent2));
    -webkit-background-clip:text;-webkit-text-fill-color:transparent}}
  /* Cards */
  .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
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
  /* Tables */
  .table-wrap{{background:var(--surface);border:1px solid var(--border);
    border-radius:14px;overflow:auto}}
  table{{width:100%;border-collapse:collapse;font-size:.9em}}
  thead th{{padding:12px 16px;text-align:left;color:var(--muted);
    font-weight:600;font-size:.8em;text-transform:uppercase;letter-spacing:.05em;
    border-bottom:1px solid var(--border)}}
  tbody td{{padding:11px 16px;border-bottom:1px solid rgba(255,255,255,.04)}}
  tbody tr:last-child td{{border-bottom:none}}
  tbody tr:hover{{background:rgba(167,139,250,.04)}}
  .badge{{display:inline-block;padding:3px 9px;border-radius:20px;
    font-size:.78em;font-weight:600}}
  .badge-green{{background:rgba(52,211,153,.12);color:var(--green)}}
  .badge-red{{background:rgba(248,113,113,.12);color:var(--red)}}
  .badge-yellow{{background:rgba(251,191,36,.12);color:var(--yellow)}}
  .badge-blue{{background:rgba(96,165,250,.12);color:var(--accent2)}}
  .badge-purple{{background:rgba(167,139,250,.12);color:var(--accent)}}
  /* Search & toolbar */
  .toolbar{{display:flex;gap:10px;margin-bottom:16px;flex-wrap:wrap;align-items:center}}
  .search-input{{background:var(--surface);border:1px solid var(--border);
    border-radius:8px;padding:8px 14px;color:var(--text);font-size:.9em;
    width:260px;outline:none}}
  .search-input:focus{{border-color:var(--accent)}}
  .btn{{display:inline-block;padding:8px 18px;border-radius:8px;border:none;
    cursor:pointer;font-size:.9em;font-weight:600;transition:.15s}}
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
  /* Section heading */
  .section-heading{{font-size:1.05em;font-weight:600;color:var(--accent);
    margin:28px 0 14px}}
  /* User detail */
  .detail-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
    gap:16px;margin-bottom:24px}}
  .detail-card{{background:var(--surface);border:1px solid var(--border);
    border-radius:12px;padding:18px}}
  .detail-card-label{{color:var(--muted);font-size:.8em;margin-bottom:4px}}
  .detail-card-value{{font-size:1.1em;font-weight:600}}
  /* Actions row */
  .actions-row{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:24px}}
  /* Modal */
  .modal-overlay{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);
    z-index:100;align-items:center;justify-content:center}}
  .modal-overlay.open{{display:flex}}
  .modal{{background:#14122a;border:1px solid var(--border);border-radius:16px;
    padding:28px;min-width:300px;max-width:420px;width:100%}}
  .modal h3{{margin-bottom:16px;font-size:1.1em}}
  .modal input{{width:100%;background:var(--surface);border:1px solid var(--border);
    border-radius:8px;padding:9px 14px;color:var(--text);font-size:.95em;
    margin-bottom:14px;outline:none}}
  .modal input:focus{{border-color:var(--accent)}}
  .modal-btns{{display:flex;gap:10px;justify-content:flex-end}}
  /* Pagination */
  .pagination{{display:flex;gap:6px;margin-top:16px;flex-wrap:wrap}}
  .page-btn{{padding:6px 12px;border-radius:7px;background:var(--surface);
    border:1px solid var(--border);color:var(--muted);font-size:.85em;cursor:pointer}}
  .page-btn:hover,.page-btn.cur{{background:rgba(167,139,250,.15);color:var(--accent);
    border-color:rgba(167,139,250,.4)}}
  /* Alert */
  .alert{{padding:12px 16px;border-radius:10px;margin-bottom:16px;font-size:.9em}}
  .alert-success{{background:rgba(52,211,153,.1);border:1px solid rgba(52,211,153,.2);
    color:var(--green)}}
  .alert-error{{background:rgba(248,113,113,.1);border:1px solid rgba(248,113,113,.2);
    color:var(--red)}}
  /* Responsive */
  @media(max-width:700px){{
    .sidebar{{width:60px}}
    .sidebar-logo,.nav-link span,.sidebar-bottom .logout-btn span{{display:none}}
    .nav-link{{text-align:center;font-size:1.3em;padding:12px}}
    .main{{padding:16px}}
  }}
</style>
</head>
<body>
<nav class="sidebar">
  <div class="sidebar-logo">⚡ PicGenAI</div>
  {nav_html}
  <div class="sidebar-bottom">
    <a href="/admin/logout" class="logout-btn">🚪 Выйти</a>
  </div>
</nav>
<main class="main">
  <div class="page-title">{title}</div>
  {content}
</main>
</body>
</html>"""


# ─── Login ───────────────────────────────────────────────────────────────────

async def handle_login(request: web.Request) -> web.Response:
    if _is_auth(request):
        raise web.HTTPFound("/admin/dashboard")
    error = ""
    if request.method == "POST":
        data = await request.post()
        pwd = data.get("password", "")
        if hmac.compare_digest(
            hashlib.sha256(pwd.encode()).hexdigest(),
            hashlib.sha256(_ADMIN_PASSWORD.encode()).hexdigest(),
        ):
            resp = web.HTTPFound("/admin/dashboard")
            resp.set_cookie(_COOKIE_NAME, _make_token(), max_age=_COOKIE_MAX_AGE, httponly=True)
            raise resp
        else:
            error = "Неверный пароль"

    error_html = f'<div class="alert alert-error">{error}</div>' if error else ""
    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Вход — PicGenAI Admin</title>
<style>
  *{{margin:0;padding:0;box-sizing:border-box}}
  body{{font-family:-apple-system,sans-serif;background:#08070e;color:#e4e4ef;
    min-height:100vh;display:flex;align-items:center;justify-content:center}}
  .box{{background:#0f0e1a;border:1px solid rgba(167,139,250,.2);border-radius:20px;
    padding:40px;width:100%;max-width:360px}}
  h1{{font-size:1.5em;margin-bottom:6px;background:linear-gradient(135deg,#a78bfa,#60a5fa);
    -webkit-background-clip:text;-webkit-text-fill-color:transparent}}
  p{{color:#8888a8;font-size:.9em;margin-bottom:24px}}
  label{{display:block;color:#8888a8;font-size:.82em;margin-bottom:6px}}
  input{{width:100%;background:#08070e;border:1px solid rgba(167,139,250,.2);
    border-radius:10px;padding:11px 14px;color:#e4e4ef;font-size:.95em;
    margin-bottom:18px;outline:none}}
  input:focus{{border-color:#a78bfa}}
  button{{width:100%;padding:12px;background:linear-gradient(135deg,#7c3aed,#6366f1);
    border:none;border-radius:10px;color:#fff;font-size:1em;font-weight:700;cursor:pointer}}
  .alert{{padding:10px 14px;border-radius:8px;margin-bottom:16px;font-size:.88em;
    background:rgba(248,113,113,.1);border:1px solid rgba(248,113,113,.2);color:#f87171}}
</style>
</head>
<body>
<div class="box">
  <h1>⚡ PicGenAI Admin</h1>
  <p>Панель управления</p>
  {error_html}
  <form method="post">
    <label>Пароль администратора</label>
    <input type="password" name="password" placeholder="••••••••" autofocus>
    <button type="submit">Войти</button>
  </form>
</div>
</body>
</html>"""
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
    users = list(_users.values())
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
    uid_map = {uid: u.get("first_name", str(uid)) for uid, u in _users.items()}

    payments_rows = ""
    for p in recent_payments:
        uid = p["user_id"]
        name = uid_map.get(uid, str(uid))
        status_badge = (
            '<span class="badge badge-green">✓ успешно</span>' if p["status"] == "success"
            else '<span class="badge badge-yellow">⏳ ожидание</span>'
        )
        dt = p["created_at"][:16].replace("T", " ") if p["created_at"] else "—"
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
    page = max(1, int(request.rel_url.query.get("page", 1)))
    filter_blocked = request.rel_url.query.get("blocked", "")
    filter_platform = request.rel_url.query.get("platform", "")

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

    sort_key = {
        "gens": lambda x: x[1].get("generations_count", 0),
        "credits": lambda x: x[1].get("credits", 0),
        "name": lambda x: x[1].get("first_name", "").lower(),
        "id": lambda x: x[0],
    }.get(sort, lambda x: x[1].get("generations_count", 0))
    users_list.sort(key=sort_key, reverse=(sort != "name"))

    total = len(users_list)
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = min(page, total_pages)
    offset = (page - 1) * _PAGE_SIZE
    page_users = users_list[offset: offset + _PAGE_SIZE]

    def sort_link(s):
        cur_q = f"?q={q}&sort={s}&blocked={filter_blocked}&platform={filter_platform}"
        arrow = " ▲" if sort == s else ""
        return f'<a href="{cur_q}" style="color:inherit">{s.upper()}{arrow}</a>'

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
            '<span class="badge badge-red">🚫 Заблокирован</span>' if blocked
            else '<span class="badge badge-green">✓ Активен</span>'
        )
        cred_color = "var(--red)" if credits_ == 0 else "var(--green)" if credits_ > 50 else "var(--yellow)"

        rows += f"""<tr>
          <td style="font-family:monospace;color:var(--muted)">{uid}</td>
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
        return f"?q={q}&sort={sort}&blocked={filter_blocked}&platform={filter_platform}&page={p}"

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
  <select name="platform" style="background:var(--surface);border:1px solid var(--border);
    border-radius:8px;padding:8px 12px;color:var(--text);font-size:.9em">
    <option value="" {"selected" if not filter_platform else ""}>Все платформы</option>
    <option value="tg" {"selected" if filter_platform=="tg" else ""}>Telegram</option>
    <option value="vk" {"selected" if filter_platform=="vk" else ""}>ВКонтакте</option>
  </select>
  <select name="blocked" style="background:var(--surface);border:1px solid var(--border);
    border-radius:8px;padding:8px 12px;color:var(--text);font-size:.9em">
    <option value="" {"selected" if filter_blocked=="" else ""}>Все статусы</option>
    <option value="0" {"selected" if filter_blocked=="0" else ""}>Активные</option>
    <option value="1" {"selected" if filter_blocked=="1" else ""}>Заблокированные</option>
  </select>
  <input type="hidden" name="sort" value="{sort}">
  <button type="submit" class="btn btn-primary">Найти</button>
  <a href="/admin/users" class="btn btn-muted">Сбросить</a>
</form>"""

    content = f"""
<div style="margin-bottom:6px;color:var(--muted);font-size:.9em">
  Найдено: <strong style="color:var(--text)">{total}</strong> пользователей
</div>
<div class="toolbar">{filter_opts}</div>
<div class="table-wrap">
<table>
  <thead><tr>
    <th>{sort_link("id")}</th>
    <th>{sort_link("name")}</th>
    <th>Платформа</th>
    <th>{sort_link("credits")}</th>
    <th>{sort_link("gens")}</th>
    <th>Статус</th>
    <th></th>
  </tr></thead>
  <tbody>{rows}</tbody>
</table>
</div>
<div class="pagination">{pages_html}</div>
"""
    return web.Response(text=_layout(f"Пользователи ({total})", content, "users"), content_type="text/html")


# ─── User detail ─────────────────────────────────────────────────────────────

@_require_auth
async def handle_user_detail(request: web.Request) -> web.Response:
    try:
        uid = int(request.match_info["uid"])
    except (ValueError, KeyError):
        raise web.HTTPNotFound()

    msg = request.rel_url.query.get("msg", "")
    if uid not in _users:
        raise web.HTTPNotFound()

    u = get_user_settings(uid)
    payments = _db.get_user_payments(uid)

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
        dt = p["created_at"][:16].replace("T", " ") if p["created_at"] else "—"
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
    <div style="display:flex;gap:8px;margin-bottom:14px">
      <button class="btn btn-muted btn-sm" id="mode-add" onclick="setMode('add')" style="flex:1">➕ Добавить</button>
      <button class="btn btn-primary btn-sm" id="mode-set" onclick="setMode('set')" style="flex:1">✏️ Установить</button>
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
  }}
  function openModal(id) {{ document.getElementById(id).classList.add('open'); }}
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
        dt = p["created_at"][:16].replace("T", " ") if p["created_at"] else "—"
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
    app.router.add_post("/admin/api/users/{uid}/credits",    api_credits)
    app.router.add_post("/admin/api/users/{uid}/block",      api_block)
    app.router.add_post("/admin/api/users/{uid}/reset_gens", api_reset_gens)
    app.router.add_post("/admin/api/users/{uid}/delete",     api_delete)
    logger.info("Admin panel routes registered at /admin")
