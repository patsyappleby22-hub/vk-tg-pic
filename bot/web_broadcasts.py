"""
bot/web_broadcasts.py
~~~~~~~~~~~~~~~~~~~~~
Admin UI + REST API for the mass-mailing engine.

Routes (registered via register_broadcast_routes in web_server.py):
  GET   /admin/broadcasts                 → list page
  GET   /admin/broadcasts/new             → compose form
  GET   /admin/broadcasts/{bid}           → detail page
  POST  /admin/api/broadcasts             → create draft
  POST  /admin/api/broadcasts/{bid}       → update draft
  POST  /admin/api/broadcasts/{bid}/{action}
        action: schedule|send_now|pause|resume|cancel|delete|clone|test|estimate
  POST  /admin/api/broadcasts/upload-media  (multipart) → returns {path, url}
  GET   /admin/api/broadcasts/{bid}/progress  → JSON live stats
  GET   /admin/api/broadcasts/{bid}/recipients?status=&offset=
  GET   /admin/broadcast-media/{filename}     → serve uploaded media
  GET   /r/{bid}/{uid}/{plat}/{idx}            → click redirect
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from aiohttp import web

import bot.db as _db
from bot.broadcasts.sender import build_audience, send_one
from bot.user_settings import user_settings

logger = logging.getLogger(__name__)

MEDIA_DIR = Path(os.getenv("BROADCAST_MEDIA_DIR", "telegram-bot/data/broadcast_media"))
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXT = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif",
    ".mp4", ".mov", ".webm",
    ".pdf", ".zip", ".doc", ".docx", ".xls", ".xlsx", ".txt",
    ".mp3", ".ogg", ".m4a", ".wav",
}
EXT_TO_TYPE = {
    ".jpg": "photo", ".jpeg": "photo", ".png": "photo", ".webp": "photo",
    ".gif": "animation",
    ".mp4": "video", ".mov": "video", ".webm": "video",
    ".pdf": "document", ".zip": "document", ".doc": "document",
    ".docx": "document", ".xls": "document", ".xlsx": "document", ".txt": "document",
    ".mp3": "audio", ".ogg": "audio", ".m4a": "audio", ".wav": "audio",
}

STATUS_LABEL = {
    "draft": "Черновик",
    "scheduled": "Запланирована",
    "sending": "Отправляется",
    "paused": "На паузе",
    "completed": "Завершена",
    "cancelled": "Отменена",
    "failed": "Ошибка",
}
STATUS_COLOR = {
    "draft": "yellow",
    "scheduled": "blue",
    "sending": "purple",
    "paused": "yellow",
    "completed": "green",
    "cancelled": "red",
    "failed": "red",
}


# ── Auth helpers (re-use web_admin's session) ────────────────────────────────

def _is_auth(request: web.Request) -> bool:
    from bot.web_admin import _is_auth as _wa_is_auth
    return _wa_is_auth(request)


def _require_auth(fn):
    async def wrapper(request: web.Request):
        if not _is_auth(request):
            raise web.HTTPFound("/admin/login")
        return await fn(request)
    return wrapper


def _api_require_auth(fn):
    async def wrapper(request: web.Request):
        if not _is_auth(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        return await fn(request)
    return wrapper


def _layout(title: str, body: str, active: str = "broadcasts") -> str:
    from bot.web_admin import _layout as _wa_layout
    return _wa_layout(title, body, active)


# ── List page ────────────────────────────────────────────────────────────────

@_require_auth
async def handle_broadcasts(request: web.Request) -> web.Response:
    status_filter = request.rel_url.query.get("status", "")
    items = _db.broadcast_list(status=status_filter or None, limit=300)
    counts = _db.broadcast_count_by_status()

    total_users = len(user_settings)
    tg_users = sum(1 for s in user_settings.values()
                   if (s.get("platform") or "tg") == "tg" and not s.get("blocked"))
    vk_users = sum(1 for s in user_settings.values()
                   if (s.get("platform") or "") == "vk" and not s.get("blocked"))

    tabs = [
        ("",          f"Все ({sum(counts.values())})"),
        ("draft",     f"Черновики ({counts.get('draft', 0)})"),
        ("scheduled", f"Запланированы ({counts.get('scheduled', 0)})"),
        ("sending",   f"Активны ({counts.get('sending', 0) + counts.get('paused', 0)})"),
        ("completed", f"Завершены ({counts.get('completed', 0)})"),
    ]
    tabs_html = "".join(
        f'<a href="/admin/broadcasts?status={key}" '
        f'class="bc-tab{" bc-tab-active" if status_filter == key else ""}">{label}</a>'
        for key, label in tabs
    )

    rows = []
    if not items:
        rows.append(
            '<tr><td colspan="7" style="text-align:center;color:var(--muted2);'
            'padding:40px 20px">Пока нет ни одной рассылки. '
            'Создайте первую — кнопка справа сверху.</td></tr>'
        )
    for it in items:
        st = it.get("status", "draft")
        color = STATUS_COLOR.get(st, "yellow")
        title = (it.get("title") or "(без названия)").strip()
        sched = it.get("scheduled_at") or ""
        sched_disp = _fmt_dt(sched) if sched else "—"
        sent = it.get("sent_count", 0)
        total = it.get("total_recipients", 0)
        progress = f"{sent}/{total}" if total else "—"
        clicked = it.get("clicked_count", 0)
        plat = it.get("target_platform", "all")
        plat_lbl = {"all": "TG + VK", "tg": "TG", "vk": "VK"}.get(plat, plat)
        rows.append(
            f'<tr>'
            f'<td style="font-weight:600">'
            f'<a href="/admin/broadcasts/{it["id"]}" '
            f'style="color:var(--text)">#{it["id"]} {_esc(title)}</a></td>'
            f'<td><span class="badge badge-{color}">{STATUS_LABEL.get(st, st)}</span></td>'
            f'<td style="color:var(--muted)">{plat_lbl}</td>'
            f'<td style="color:var(--muted)">{sched_disp}</td>'
            f'<td>{progress}</td>'
            f'<td>{clicked}</td>'
            f'<td style="text-align:right;color:var(--muted2);font-size:.85em">'
            f'{_fmt_dt(it.get("created_at",""))}</td>'
            f'</tr>'
        )

    body = f"""
<div class="autopub-header" style="justify-content:space-between">
  <div>
    <h2 style="margin:0;font-family:Syne,Inter,sans-serif;font-weight:700">Рассылки</h2>
    <div style="color:var(--muted2);font-size:.9em;margin-top:4px">
      База получателей: всего {total_users}, активные TG {tg_users}, VK {vk_users}
    </div>
  </div>
  <a href="/admin/broadcasts/new" class="btn btn-primary">+ Новая рассылка</a>
</div>

<div class="bc-tabs">{tabs_html}</div>

<div class="card-table">
  <table class="data-table">
    <thead><tr>
      <th>Рассылка</th><th>Статус</th><th>Платформа</th>
      <th>Запуск</th><th>Отправлено</th><th>Кликов</th>
      <th style="text-align:right">Создана</th>
    </tr></thead>
    <tbody>{"".join(rows)}</tbody>
  </table>
</div>

<style>
  .bc-tabs{{display:flex;gap:4px;margin:16px 0 14px;flex-wrap:wrap;
    border-bottom:1px solid var(--border)}}
  .bc-tab{{padding:9px 14px;color:var(--muted);font-size:.92em;
    border-bottom:2px solid transparent;text-decoration:none}}
  .bc-tab:hover{{color:var(--text)}}
  .bc-tab-active{{color:var(--accent-bright);border-bottom-color:var(--accent)}}
</style>
"""

    return web.Response(text=_layout("Рассылки", body), content_type="text/html")


# ── Compose form (new) ───────────────────────────────────────────────────────

@_require_auth
async def handle_broadcast_new(request: web.Request) -> web.Response:
    return web.Response(text=_layout("Новая рассылка", _compose_html(None)),
                        content_type="text/html")


@_require_auth
async def handle_broadcast_detail(request: web.Request) -> web.Response:
    bid = int(request.match_info["bid"])
    b = _db.broadcast_get(bid)
    if not b:
        raise web.HTTPNotFound(text="Broadcast not found")
    if b.get("status") == "draft":
        return web.Response(text=_layout(f"Рассылка #{bid}", _compose_html(b)),
                            content_type="text/html")
    return web.Response(text=_layout(f"Рассылка #{bid}", _detail_html(b)),
                        content_type="text/html")


# ── HTML builders ────────────────────────────────────────────────────────────

def _compose_html(b: dict | None) -> str:
    is_new = b is None
    bid = (b or {}).get("id", 0)
    title = _esc((b or {}).get("title", ""))
    text = _esc((b or {}).get("text", ""))
    parse_mode = (b or {}).get("parse_mode", "HTML")
    media_type = (b or {}).get("media_type", "none")
    media_path = (b or {}).get("media_path", "")
    media_url = (b or {}).get("media_url", "")
    target_platform = (b or {}).get("target_platform", "all")
    rate_per_sec = (b or {}).get("rate_per_sec", 20)
    notes = _esc((b or {}).get("notes", ""))
    silent = bool((b or {}).get("silent"))
    protect = bool((b or {}).get("protect_content"))
    pin = bool((b or {}).get("pin"))
    disable_preview = bool((b or {}).get("disable_preview"))
    personalize = bool((b or {}).get("personalize"))

    try:
        buttons = json.loads((b or {}).get("buttons_json") or "[]") or []
    except Exception:
        buttons = []
    try:
        target_filter = json.loads((b or {}).get("target_filter") or "{}") or {}
    except Exception:
        target_filter = {}

    sched_at = (b or {}).get("scheduled_at") or ""
    sched_local = _to_local_input(sched_at) if sched_at else ""

    media_preview = ""
    if media_path:
        media_url_view = f"/admin/broadcast-media/{Path(media_path).name}"
        if media_type == "photo":
            media_preview = (
                f'<img src="{media_url_view}" '
                f'style="max-width:100%;max-height:240px;border-radius:10px;'
                f'border:1px solid var(--border);margin-top:8px">'
            )
        else:
            media_preview = (
                f'<div style="color:var(--muted);font-size:.85em;margin-top:8px">'
                f'Файл: <a href="{media_url_view}" target="_blank" '
                f'style="color:var(--accent-bright)">{_esc(Path(media_path).name)}</a></div>'
            )

    page_title_h = "Новая рассылка" if is_new else f"Рассылка #{bid} (черновик)"

    js_state = _safe_json_for_html({
        "bid": bid,
        "buttons": buttons,
        "target_filter": target_filter,
        "media_path": media_path,
        "media_type": media_type,
        "media_url_view": (
            f"/admin/broadcast-media/{Path(media_path).name}" if media_path else ""
        ),
    })

    sel = lambda v, opt: ' selected' if v == opt else ''
    chk = lambda v: ' checked' if v else ''

    return f"""
<div style="display:flex;align-items:center;gap:12px;margin-bottom:16px">
  <a href="/admin/broadcasts" class="btn btn-ghost">← К списку</a>
  <h2 style="margin:0;font-family:Syne,Inter,sans-serif">{page_title_h}</h2>
</div>

<div class="bc-grid">
  <div class="bc-col-main">

    <div class="bc-card">
      <div class="bc-card-h">Содержимое</div>

      <label class="bc-lbl">Название (видно только в админке)</label>
      <input id="bc-title" class="bc-inp" maxlength="200"
             value="{title}" placeholder="Например: Промо июнь">

      <label class="bc-lbl">Текст сообщения</label>
      <textarea id="bc-text" class="bc-inp bc-textarea" rows="9"
        placeholder="Поддерживает HTML: <b>жирный</b>, <i>курсив</i>, <a href=&quot;...&quot;>ссылка</a>.&#10;Переменные: {{name}}, {{credits}}, {{user_id}}, {{generations}}">{text}</textarea>
      <div class="bc-help">Лимит: 4096 символов для текста, 1024 для подписи к фото/видео.</div>

      <div class="bc-row3">
        <div>
          <label class="bc-lbl">Форматирование</label>
          <select id="bc-parsemode" class="bc-inp">
            <option value="HTML"{sel(parse_mode,"HTML")}>HTML</option>
            <option value="MarkdownV2"{sel(parse_mode,"MarkdownV2")}>MarkdownV2</option>
            <option value="none"{sel(parse_mode,"none")}>Без форматирования</option>
          </select>
        </div>
        <div>
          <label class="bc-lbl">Скорость отправки</label>
          <input id="bc-rate" type="number" class="bc-inp" min="1" max="30"
                 value="{rate_per_sec}">
          <div class="bc-help">сообщений/сек (TG лимит ≈ 30/с)</div>
        </div>
        <div>
          <label class="bc-lbl">Платформа</label>
          <select id="bc-platform" class="bc-inp">
            <option value="all"{sel(target_platform,"all")}>TG + VK</option>
            <option value="tg"{sel(target_platform,"tg")}>Только Telegram</option>
            <option value="vk"{sel(target_platform,"vk")}>Только ВКонтакте</option>
          </select>
        </div>
      </div>

      <div class="bc-flags">
        <label><input type="checkbox" id="bc-silent"{chk(silent)}> Без звука</label>
        <label><input type="checkbox" id="bc-disable-preview"{chk(disable_preview)}> Без превью ссылок</label>
        <label><input type="checkbox" id="bc-protect"{chk(protect)}> Запретить пересылку</label>
        <label><input type="checkbox" id="bc-pin"{chk(pin)}> Закрепить у пользователя</label>
        <label><input type="checkbox" id="bc-personalize"{chk(personalize)}> Подставлять имя/баланс</label>
      </div>
    </div>

    <div class="bc-card">
      <div class="bc-card-h">Медиа (необязательно)</div>
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <label class="btn btn-ghost" style="cursor:pointer">
          <input type="file" id="bc-file" style="display:none"
                 accept="image/*,video/*,audio/*,.pdf,.doc,.docx,.zip">
          Загрузить файл
        </label>
        <span id="bc-media-info" style="color:var(--muted);font-size:.9em">
          {("Тип: " + media_type) if media_path else "Файл не выбран"}
        </span>
        {('<button class="btn btn-ghost" id="bc-media-clear">Удалить</button>'
          if media_path else '')}
      </div>
      <div id="bc-media-preview">{media_preview}</div>

      <label class="bc-lbl" style="margin-top:14px">или ссылка на медиа (URL)</label>
      <input id="bc-media-url" class="bc-inp" value="{_esc(media_url)}"
             placeholder="https://...">
    </div>

    <div class="bc-card">
      <div class="bc-card-h">Кнопки (URL-кнопки под сообщением)</div>
      <div id="bc-buttons"></div>
      <button class="btn btn-ghost" id="bc-add-btn" style="margin-top:8px">
        + Добавить кнопку
      </button>
      <div class="bc-help">Каждая кнопка отправит пользователя по ссылке.
        Клики автоматически считаются.</div>
    </div>

    <div class="bc-card">
      <div class="bc-card-h">Аудитория</div>

      <div class="bc-row3">
        <div>
          <label class="bc-lbl">Сегмент</label>
          <select id="bc-aud" class="bc-inp">
            <option value="all">Все пользователи</option>
            <option value="paid">Только платившие</option>
            <option value="unpaid">Только не платившие</option>
            <option value="active">Активные за N дней</option>
            <option value="inactive">Неактивные за N дней</option>
          </select>
        </div>
        <div>
          <label class="bc-lbl">N дней (для актив./неактив.)</label>
          <input id="bc-active-days" type="number" class="bc-inp" min="1" max="365" value="7">
        </div>
        <div>
          <label class="bc-lbl">Исключить заблокированных</label>
          <select id="bc-excl-blocked" class="bc-inp">
            <option value="1">Да (рекомендуется)</option>
            <option value="0">Нет</option>
          </select>
        </div>
      </div>

      <div class="bc-row4">
        <div>
          <label class="bc-lbl">Кредиты — мин.</label>
          <input id="bc-cmin" type="number" class="bc-inp" placeholder="—">
        </div>
        <div>
          <label class="bc-lbl">Кредиты — макс.</label>
          <input id="bc-cmax" type="number" class="bc-inp" placeholder="—">
        </div>
        <div>
          <label class="bc-lbl">Генераций — мин.</label>
          <input id="bc-gmin" type="number" class="bc-inp" placeholder="—">
        </div>
        <div>
          <label class="bc-lbl">Генераций — макс.</label>
          <input id="bc-gmax" type="number" class="bc-inp" placeholder="—">
        </div>
      </div>

      <div class="bc-row2">
        <div>
          <label class="bc-lbl">Включить только эти ID (через запятую)</label>
          <input id="bc-incl-ids" class="bc-inp" placeholder="123, 456">
        </div>
        <div>
          <label class="bc-lbl">Исключить эти ID</label>
          <input id="bc-excl-ids" class="bc-inp" placeholder="789">
        </div>
      </div>

      <div style="margin-top:12px;display:flex;align-items:center;gap:10px">
        <button class="btn btn-ghost" id="bc-estimate">Подсчитать получателей</button>
        <span id="bc-estimate-result" style="color:var(--muted);font-size:.9em"></span>
      </div>
    </div>

    <div class="bc-card">
      <div class="bc-card-h">Запуск</div>
      <div class="bc-row3">
        <div>
          <label class="bc-lbl">Когда</label>
          <select id="bc-when" class="bc-inp">
            <option value="now">Сразу при отправке</option>
            <option value="schedule">Запланировать на дату/время</option>
          </select>
        </div>
        <div id="bc-when-dt-wrap" style="display:none">
          <label class="bc-lbl">Дата и время (МСК)</label>
          <input id="bc-when-dt" class="bc-inp" type="datetime-local"
                 value="{sched_local}">
        </div>
        <div></div>
      </div>
    </div>

    <div class="bc-card">
      <div class="bc-card-h">Заметки</div>
      <textarea id="bc-notes" class="bc-inp bc-textarea" rows="3"
        placeholder="Внутренние заметки для команды (не отправляются)">{notes}</textarea>
    </div>

  </div>

  <div class="bc-col-side">
    <div class="bc-card bc-sticky">
      <div class="bc-card-h">Действия</div>
      <button class="btn btn-primary" id="bc-save-draft" style="width:100%">
        Сохранить черновик
      </button>
      <button class="btn btn-primary" id="bc-launch" style="width:100%;margin-top:8px">
        Запустить рассылку
      </button>

      <div style="border-top:1px solid var(--border);margin:14px 0"></div>

      <div class="bc-lbl">Тестовая отправка</div>
      <div style="display:flex;gap:6px">
        <input id="bc-test-uid" class="bc-inp" type="number"
               placeholder="user_id" style="flex:1">
        <select id="bc-test-plat" class="bc-inp" style="width:80px">
          <option value="tg">TG</option><option value="vk">VK</option>
        </select>
        <button class="btn btn-ghost" id="bc-test-send">→</button>
      </div>
      <div id="bc-test-result" style="margin-top:6px;font-size:.85em;color:var(--muted)"></div>

      <div style="border-top:1px solid var(--border);margin:14px 0"></div>

      <div class="bc-lbl">Предпросмотр</div>
      <div id="bc-preview" class="bc-preview-box"></div>
    </div>
  </div>
</div>

<style>
  .bc-grid{{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:18px}}
  .bc-card{{background:var(--surface);border:1px solid var(--border);
    border-radius:14px;padding:18px;margin-bottom:14px}}
  .bc-card-h{{font-family:Syne,Inter,sans-serif;font-weight:600;font-size:1.05em;
    margin-bottom:12px;color:var(--text)}}
  .bc-lbl{{display:block;font-size:.78em;color:var(--muted);
    text-transform:uppercase;letter-spacing:.04em;margin:8px 0 5px}}
  .bc-inp{{width:100%;padding:9px 12px;background:var(--bg);
    border:1px solid var(--border);border-radius:8px;color:var(--text);
    font-size:.95em;font-family:Inter,sans-serif}}
  .bc-inp:focus{{outline:none;border-color:var(--accent)}}
  .bc-textarea{{font-family:'JetBrains Mono',Menlo,monospace;font-size:.88em;
    line-height:1.5;resize:vertical;min-height:140px}}
  .bc-row2{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
  .bc-row3{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}
  .bc-row4{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}
  .bc-flags{{display:flex;flex-wrap:wrap;gap:14px;margin-top:14px;
    color:var(--muted);font-size:.9em}}
  .bc-flags label{{display:flex;align-items:center;gap:6px;cursor:pointer}}
  .bc-help{{color:var(--muted2);font-size:.78em;margin-top:4px}}
  .bc-sticky{{position:sticky;top:18px}}
  .bc-preview-box{{background:#101016;border:1px solid var(--border);
    border-radius:10px;padding:12px;font-size:.88em;color:var(--text);
    white-space:pre-wrap;word-break:break-word;min-height:80px}}
  .bc-btn-row{{display:grid;grid-template-columns:1fr 1fr 40px;gap:6px;margin-bottom:6px}}
  @media(max-width:980px){{
    .bc-grid{{grid-template-columns:1fr}}
    .bc-sticky{{position:static}}
    .bc-row3,.bc-row4{{grid-template-columns:1fr 1fr}}
  }}
</style>

<script id="bc-state-data" type="application/json">{js_state}</script>
<script>
const BC_INIT = JSON.parse(document.getElementById('bc-state-data').textContent);
const BC_AUD = BC_INIT.target_filter || {{}};

function $(id) {{ return document.getElementById(id); }}

function renderButtons() {{
  const wrap = $('bc-buttons');
  wrap.innerHTML = '';
  BC_INIT.buttons.forEach((b, i) => {{
    const row = document.createElement('div');
    row.className = 'bc-btn-row';
    row.innerHTML = `
      <input class="bc-inp bc-bt" data-i="${{i}}" data-k="text"
             placeholder="Текст кнопки" value="${{(b.text||'').replace(/"/g,'&quot;')}}">
      <input class="bc-inp bc-bt" data-i="${{i}}" data-k="url"
             placeholder="https://..." value="${{(b.url||'').replace(/"/g,'&quot;')}}">
      <button class="btn btn-ghost bc-bt-rm" data-i="${{i}}">×</button>`;
    wrap.appendChild(row);
  }});
  wrap.querySelectorAll('.bc-bt').forEach(el => {{
    el.addEventListener('input', e => {{
      const i = +e.target.dataset.i, k = e.target.dataset.k;
      BC_INIT.buttons[i] = BC_INIT.buttons[i] || {{text:'',url:''}};
      BC_INIT.buttons[i][k] = e.target.value;
      updatePreview();
    }});
  }});
  wrap.querySelectorAll('.bc-bt-rm').forEach(el => {{
    el.addEventListener('click', e => {{
      BC_INIT.buttons.splice(+e.target.dataset.i, 1);
      renderButtons(); updatePreview();
    }});
  }});
}}

$('bc-add-btn').addEventListener('click', () => {{
  if (BC_INIT.buttons.length >= 8) {{
    alert('Не более 8 кнопок'); return;
  }}
  BC_INIT.buttons.push({{text:'',url:''}});
  renderButtons();
}});
renderButtons();

// Restore audience filters
function restoreAud() {{
  if (BC_AUD.audience) $('bc-aud').value = BC_AUD.audience;
  if (BC_AUD.active_days) $('bc-active-days').value = BC_AUD.active_days;
  $('bc-excl-blocked').value = (BC_AUD.exclude_blocked === false) ? '0' : '1';
  if (BC_AUD.credits_min != null) $('bc-cmin').value = BC_AUD.credits_min;
  if (BC_AUD.credits_max != null) $('bc-cmax').value = BC_AUD.credits_max;
  if (BC_AUD.generations_min != null) $('bc-gmin').value = BC_AUD.generations_min;
  if (BC_AUD.generations_max != null) $('bc-gmax').value = BC_AUD.generations_max;
  if ((BC_AUD.include_user_ids || []).length)
    $('bc-incl-ids').value = BC_AUD.include_user_ids.join(', ');
  if ((BC_AUD.exclude_user_ids || []).length)
    $('bc-excl-ids').value = BC_AUD.exclude_user_ids.join(', ');
}}
restoreAud();

function parseIds(s) {{
  return (s||'').split(/[,\\s]+/).map(x => x.trim()).filter(Boolean)
                .map(x => parseInt(x,10)).filter(n => !isNaN(n));
}}

function getPayload() {{
  return {{
    title: $('bc-title').value.trim(),
    text: $('bc-text').value,
    parse_mode: $('bc-parsemode').value,
    rate_per_sec: parseInt($('bc-rate').value || '20', 10),
    target_platform: $('bc-platform').value,
    silent: $('bc-silent').checked,
    disable_preview: $('bc-disable-preview').checked,
    protect_content: $('bc-protect').checked,
    pin: $('bc-pin').checked,
    personalize: $('bc-personalize').checked,
    media_url: $('bc-media-url').value.trim(),
    media_path: BC_INIT.media_path || '',
    media_type: BC_INIT.media_type || 'none',
    buttons: BC_INIT.buttons.filter(b => b.text && b.url),
    target_filter: {{
      audience: $('bc-aud').value,
      active_days: parseInt($('bc-active-days').value || '7', 10),
      exclude_blocked: $('bc-excl-blocked').value === '1',
      credits_min: $('bc-cmin').value === '' ? null : parseInt($('bc-cmin').value,10),
      credits_max: $('bc-cmax').value === '' ? null : parseInt($('bc-cmax').value,10),
      generations_min: $('bc-gmin').value === '' ? null : parseInt($('bc-gmin').value,10),
      generations_max: $('bc-gmax').value === '' ? null : parseInt($('bc-gmax').value,10),
      include_user_ids: parseIds($('bc-incl-ids').value),
      exclude_user_ids: parseIds($('bc-excl-ids').value),
    }},
    notes: $('bc-notes').value,
  }};
}}

async function saveDraft() {{
  const data = getPayload();
  const url = BC_INIT.bid ? `/admin/api/broadcasts/${{BC_INIT.bid}}` : '/admin/api/broadcasts';
  const r = await fetch(url, {{
    method:'POST', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify(data),
  }});
  const j = await r.json();
  if (!j.ok) {{ alert('Ошибка сохранения: ' + (j.error || 'unknown')); return null; }}
  if (!BC_INIT.bid && j.id) {{
    BC_INIT.bid = j.id;
    history.replaceState(null,'',`/admin/broadcasts/${{j.id}}`);
  }}
  return j;
}}

$('bc-save-draft').addEventListener('click', async () => {{
  const j = await saveDraft();
  if (j) flash('Черновик сохранён');
}});

$('bc-launch').addEventListener('click', async () => {{
  const j = await saveDraft();
  if (!j) return;
  const when = $('bc-when').value;
  let body = {{}};
  if (when === 'schedule') {{
    const dt = $('bc-when-dt').value;
    if (!dt) {{ alert('Укажите дату и время запуска'); return; }}
    body.scheduled_at = dt;
  }}
  const action = (when === 'schedule') ? 'schedule' : 'send_now';
  if (!confirm('Запустить рассылку? Это действие необратимо.')) return;
  const r = await fetch(`/admin/api/broadcasts/${{BC_INIT.bid}}/${{action}}`, {{
    method:'POST', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify(body),
  }});
  const res = await r.json();
  if (res.ok) location.href = `/admin/broadcasts/${{BC_INIT.bid}}`;
  else alert('Ошибка: ' + (res.error || 'unknown'));
}});

$('bc-when').addEventListener('change', e => {{
  $('bc-when-dt-wrap').style.display = (e.target.value === 'schedule') ? '' : 'none';
}});

$('bc-estimate').addEventListener('click', async () => {{
  const r = await fetch('/admin/api/broadcasts/0/estimate', {{
    method:'POST', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify(getPayload()),
  }});
  const j = await r.json();
  if (j.ok) $('bc-estimate-result').innerText =
    `Получателей: ${{j.count}} (TG: ${{j.tg}}, VK: ${{j.vk}})`;
  else $('bc-estimate-result').innerText = 'Ошибка: ' + (j.error || 'unknown');
}});

$('bc-test-send').addEventListener('click', async () => {{
  const j = await saveDraft();
  if (!j) return;
  const uid = parseInt($('bc-test-uid').value, 10);
  const plat = $('bc-test-plat').value;
  if (!uid) {{ alert('Укажите user_id'); return; }}
  $('bc-test-result').innerText = 'Отправляю...';
  const r = await fetch(`/admin/api/broadcasts/${{BC_INIT.bid}}/test`, {{
    method:'POST', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{user_id: uid, platform: plat}}),
  }});
  const res = await r.json();
  $('bc-test-result').innerText = res.ok
    ? '✓ Отправлено' : ('✗ ' + (res.error || res.status || 'fail'));
}});

// Media upload
$('bc-file').addEventListener('change', async e => {{
  const f = e.target.files[0];
  if (!f) return;
  if (f.size > 50 * 1024 * 1024) {{ alert('Файл больше 50 МБ'); return; }}
  const fd = new FormData();
  fd.append('file', f);
  $('bc-media-info').innerText = 'Загружаю...';
  const r = await fetch('/admin/api/broadcasts/upload-media', {{ method:'POST', body: fd }});
  const j = await r.json();
  if (!j.ok) {{ $('bc-media-info').innerText = 'Ошибка: ' + j.error; return; }}
  BC_INIT.media_path = j.path;
  BC_INIT.media_type = j.media_type;
  BC_INIT.media_url_view = j.url;
  $('bc-media-info').innerText = `✓ ${{f.name}} (${{j.media_type}})`;
  $('bc-media-preview').innerHTML = j.media_type === 'photo'
    ? `<img src="${{j.url}}" style="max-width:100%;max-height:240px;border-radius:10px;border:1px solid var(--border);margin-top:8px">`
    : `<div style="color:var(--muted);font-size:.85em;margin-top:8px">Файл загружен</div>`;
}});

const clearBtn = $('bc-media-clear');
if (clearBtn) clearBtn.addEventListener('click', e => {{
  e.preventDefault();
  BC_INIT.media_path = '';
  BC_INIT.media_type = 'none';
  $('bc-media-preview').innerHTML = '';
  $('bc-media-info').innerText = 'Файл не выбран';
}});

// Live preview
function updatePreview() {{
  const t = $('bc-text').value || '(пустой текст)';
  const btns = BC_INIT.buttons.filter(b => b.text && b.url);
  const btnHtml = btns.length
    ? '<div style="margin-top:10px;display:flex;flex-direction:column;gap:6px">' +
      btns.map(b => `<div style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:8px 12px;font-size:.9em;color:var(--accent-bright)">▷ ${{b.text}}</div>`).join('') + '</div>'
    : '';
  $('bc-preview').innerHTML = t.replace(/</g,'&lt;').replace(/\\n/g,'<br>') + btnHtml;
}}
['bc-text'].forEach(id => $(id).addEventListener('input', updatePreview));
updatePreview();

function flash(msg) {{
  const el = document.createElement('div');
  el.style = 'position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:var(--accent);color:#fff;padding:10px 18px;border-radius:8px;z-index:999;font-size:.9em';
  el.innerText = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2200);
}}
</script>
"""


def _detail_html(b: dict) -> str:
    bid = b["id"]
    st = b.get("status", "draft")
    color = STATUS_COLOR.get(st, "yellow")
    title = b.get("title") or "(без названия)"
    sent = b.get("sent_count", 0)
    failed = b.get("failed_count", 0)
    blocked = b.get("blocked_count", 0)
    skipped = b.get("skipped_count", 0)
    clicked = b.get("clicked_count", 0)
    total = b.get("total_recipients", 0) or 0
    progress_pct = int(round(((sent + failed + blocked + skipped) / total) * 100)) if total else 0
    sched = _fmt_dt(b.get("scheduled_at", ""))
    started = _fmt_dt(b.get("started_at", ""))
    finished = _fmt_dt(b.get("finished_at", ""))
    plat = b.get("target_platform", "all")
    plat_lbl = {"all": "TG + VK", "tg": "Telegram", "vk": "ВКонтакте"}.get(plat, plat)
    rate = b.get("rate_per_sec", 20)

    text_preview = _esc((b.get("text") or "")[:600])

    can_pause = st == "sending"
    can_resume = st == "paused"
    can_cancel = st in ("sending", "paused", "scheduled")
    can_delete = st in ("draft", "completed", "cancelled", "failed")

    actions = []
    if can_pause:
        actions.append('<button class="btn btn-ghost" data-act="pause">Пауза</button>')
    if can_resume:
        actions.append('<button class="btn btn-primary" data-act="resume">Продолжить</button>')
    if can_cancel:
        actions.append('<button class="btn btn-ghost" data-act="cancel">Отменить</button>')
    actions.append('<button class="btn btn-ghost" data-act="clone">Клонировать</button>')
    if can_delete:
        actions.append('<button class="btn btn-ghost" data-act="delete">Удалить</button>')

    return f"""
<div style="display:flex;align-items:center;gap:12px;margin-bottom:16px">
  <a href="/admin/broadcasts" class="btn btn-ghost">← К списку</a>
  <h2 style="margin:0;font-family:Syne,Inter,sans-serif">
    Рассылка #{bid} — {_esc(title)}
  </h2>
  <span class="badge badge-{color}">{STATUS_LABEL.get(st, st)}</span>
</div>

<div class="bc-grid">
  <div class="bc-col-main">
    <div class="bc-card">
      <div class="bc-card-h">Прогресс</div>
      <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:14px">
        <div class="bc-stat"><div class="bc-stat-v">{sent}</div><div class="bc-stat-l">Доставлено</div></div>
        <div class="bc-stat"><div class="bc-stat-v" style="color:var(--red)">{failed}</div><div class="bc-stat-l">Ошибок</div></div>
        <div class="bc-stat"><div class="bc-stat-v" style="color:var(--yellow)">{blocked}</div><div class="bc-stat-l">Заблок.</div></div>
        <div class="bc-stat"><div class="bc-stat-v">{skipped}</div><div class="bc-stat-l">Пропущ.</div></div>
        <div class="bc-stat"><div class="bc-stat-v" style="color:var(--accent-bright)">{clicked}</div><div class="bc-stat-l">Кликов</div></div>
      </div>
      <div class="bc-progress-wrap">
        <div class="bc-progress-bar" id="bc-pbar" style="width:{progress_pct}%"></div>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:.85em;color:var(--muted);margin-top:6px">
        <span id="bc-progress-txt">{sent + failed + blocked + skipped}/{total} ({progress_pct}%)</span>
        <span>Скорость: {rate}/с</span>
      </div>
    </div>

    <div class="bc-card">
      <div class="bc-card-h">Последние получатели</div>
      <div id="bc-recipients">Загружаю…</div>
    </div>

    <div class="bc-card">
      <div class="bc-card-h">Сообщение</div>
      <div class="bc-preview-box">{text_preview}</div>
    </div>
  </div>

  <div class="bc-col-side">
    <div class="bc-card bc-sticky">
      <div class="bc-card-h">Параметры</div>
      <div class="bc-meta">
        <div><span>Платформа</span><b>{plat_lbl}</b></div>
        <div><span>Запуск</span><b>{sched or '—'}</b></div>
        <div><span>Старт</span><b>{started or '—'}</b></div>
        <div><span>Финиш</span><b>{finished or '—'}</b></div>
        <div><span>Получателей</span><b>{total}</b></div>
      </div>
      <div style="border-top:1px solid var(--border);margin:14px 0"></div>
      <div style="display:flex;flex-direction:column;gap:6px" id="bc-actions">
        {''.join(actions)}
      </div>
    </div>
  </div>
</div>

<style>
  .bc-grid{{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:18px}}
  .bc-card{{background:var(--surface);border:1px solid var(--border);
    border-radius:14px;padding:18px;margin-bottom:14px}}
  .bc-card-h{{font-family:Syne,Inter,sans-serif;font-weight:600;font-size:1.05em;
    margin-bottom:12px;color:var(--text)}}
  .bc-stat{{background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:12px;text-align:center}}
  .bc-stat-v{{font-family:Syne,Inter,sans-serif;font-size:1.6em;font-weight:700}}
  .bc-stat-l{{font-size:.72em;color:var(--muted2);text-transform:uppercase;
    letter-spacing:.04em;margin-top:4px}}
  .bc-progress-wrap{{height:8px;background:var(--bg);border-radius:4px;overflow:hidden}}
  .bc-progress-bar{{height:100%;background:linear-gradient(90deg,var(--accent),var(--accent-bright));transition:width .4s}}
  .bc-meta{{display:flex;flex-direction:column;gap:8px;font-size:.9em}}
  .bc-meta>div{{display:flex;justify-content:space-between;color:var(--muted)}}
  .bc-meta b{{color:var(--text);font-weight:600;text-align:right}}
  .bc-preview-box{{background:#101016;border:1px solid var(--border);
    border-radius:10px;padding:12px;font-size:.88em;color:var(--text);
    white-space:pre-wrap;word-break:break-word;min-height:80px}}
  .bc-sticky{{position:sticky;top:18px}}
  @media(max-width:980px){{
    .bc-grid{{grid-template-columns:1fr}}
    .bc-sticky{{position:static}}
  }}
  .rcpt-row{{display:grid;grid-template-columns:80px 60px 80px 1fr 110px;gap:8px;
    padding:6px 4px;border-bottom:1px solid var(--border);font-size:.85em}}
  .rcpt-row:last-child{{border-bottom:none}}
  .rcpt-st{{font-weight:600}}
  .rcpt-st.sent{{color:var(--green)}}
  .rcpt-st.failed{{color:var(--red)}}
  .rcpt-st.blocked{{color:var(--yellow)}}
</style>

<script>
const BID = {bid};
async function refresh() {{
  try {{
    const r = await fetch(`/admin/api/broadcasts/${{BID}}/progress`);
    const j = await r.json();
    if (!j.ok) return;
    const done = j.sent + j.failed + j.blocked + j.skipped;
    const pct = j.total ? Math.round(done * 100 / j.total) : 0;
    $('bc-pbar').style.width = pct + '%';
    $('bc-progress-txt').innerText = `${{done}}/${{j.total}} (${{pct}}%)`;
    if (j.status !== '{st}') location.reload();
  }} catch(e) {{}}
}}
async function loadRcpt() {{
  try {{
    const r = await fetch(`/admin/api/broadcasts/${{BID}}/recipients?limit=50`);
    const j = await r.json();
    if (!j.ok) return;
    if (!j.items.length) {{
      $('bc-recipients').innerHTML = '<div style="color:var(--muted2);font-size:.9em">Получателей пока нет.</div>';
      return;
    }}
    $('bc-recipients').innerHTML = j.items.map(it => `
      <div class="rcpt-row">
        <span style="color:var(--muted)">${{it.platform.toUpperCase()}}</span>
        <span style="color:var(--muted)">${{it.user_id}}</span>
        <span class="rcpt-st ${{it.status}}">${{it.status}}</span>
        <span style="color:var(--muted2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
              title="${{(it.error_text||'').replace(/"/g,'&quot;')}}">${{it.error_text||''}}</span>
        <span style="color:var(--muted2);text-align:right">${{(it.attempted_at||'').slice(11,19)}}</span>
      </div>`).join('');
  }} catch(e) {{}}
}}
function $(id) {{ return document.getElementById(id); }}
loadRcpt();
setInterval(refresh, 3000);
setInterval(loadRcpt, 5000);

document.querySelectorAll('#bc-actions [data-act]').forEach(btn => {{
  btn.addEventListener('click', async () => {{
    const act = btn.dataset.act;
    if (act === 'cancel' && !confirm('Отменить рассылку?')) return;
    if (act === 'delete' && !confirm('Удалить рассылку и всю историю?')) return;
    const r = await fetch(`/admin/api/broadcasts/${{BID}}/${{act}}`, {{ method:'POST' }});
    const j = await r.json();
    if (!j.ok) {{ alert('Ошибка: ' + (j.error || 'unknown')); return; }}
    if (act === 'delete') location.href = '/admin/broadcasts';
    else if (act === 'clone' && j.id) location.href = '/admin/broadcasts/' + j.id;
    else location.reload();
  }});
}});
</script>
"""


# ── API: CRUD + actions ──────────────────────────────────────────────────────

def _coerce_payload(p: dict) -> dict:
    """Sanitize compose form payload before persisting."""
    out = {
        "title": (p.get("title") or "").strip()[:200],
        "text": (p.get("text") or "")[:8000],
        "parse_mode": p.get("parse_mode") or "HTML",
        "media_type": p.get("media_type") or "none",
        "media_path": p.get("media_path") or "",
        "media_url": (p.get("media_url") or "").strip()[:1000],
        "buttons": [
            {"text": (b.get("text") or "")[:60], "url": (b.get("url") or "")[:1000]}
            for b in (p.get("buttons") or []) if b.get("text") and b.get("url")
        ][:8],
        "disable_preview": bool(p.get("disable_preview")),
        "silent": bool(p.get("silent")),
        "protect_content": bool(p.get("protect_content")),
        "pin": bool(p.get("pin")),
        "personalize": bool(p.get("personalize")),
        "target_platform": p.get("target_platform") or "all",
        "target_filter": p.get("target_filter") or {},
        "rate_per_sec": max(1, min(30, int(p.get("rate_per_sec") or 20))),
        "notes": (p.get("notes") or "")[:2000],
    }
    return out


@_api_require_auth
async def api_broadcast_create(request: web.Request) -> web.Response:
    try:
        p = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    data = _coerce_payload(p)
    data["status"] = "draft"
    bid = _db.broadcast_create(data)
    if not bid:
        return web.json_response({"ok": False, "error": "db error"}, status=500)
    return web.json_response({"ok": True, "id": bid})


@_api_require_auth
async def api_broadcast_update(request: web.Request) -> web.Response:
    bid = int(request.match_info["bid"])
    try:
        p = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    cur = _db.broadcast_get(bid)
    if not cur:
        return web.json_response({"ok": False, "error": "not found"}, status=404)
    if cur.get("status") not in ("draft",):
        return web.json_response({"ok": False, "error": "only drafts can be edited"},
                                 status=400)
    data = _coerce_payload(p)
    upd = {
        "title": data["title"], "text": data["text"],
        "parse_mode": data["parse_mode"], "media_type": data["media_type"],
        "media_path": data["media_path"], "media_url": data["media_url"],
        "buttons_json": json.dumps(data["buttons"], ensure_ascii=False),
        "disable_preview": data["disable_preview"], "silent": data["silent"],
        "protect_content": data["protect_content"], "pin": data["pin"],
        "personalize": data["personalize"],
        "target_platform": data["target_platform"],
        "target_filter": json.dumps(data["target_filter"], ensure_ascii=False),
        "rate_per_sec": data["rate_per_sec"], "notes": data["notes"],
    }
    _db.broadcast_update(bid, upd)
    return web.json_response({"ok": True, "id": bid})


@_api_require_auth
async def api_broadcast_action(request: web.Request) -> web.Response:
    bid_raw = request.match_info["bid"]
    action = request.match_info["action"]

    if action == "estimate":
        try:
            p = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad json"}, status=400)
        b_stub = {
            "target_platform": p.get("target_platform", "all"),
            "target_filter": json.dumps(p.get("target_filter") or {}, ensure_ascii=False),
        }
        audience = build_audience(b_stub)
        tg = sum(1 for _, plat in audience if plat == "tg")
        vk = sum(1 for _, plat in audience if plat == "vk")
        return web.json_response({"ok": True, "count": len(audience), "tg": tg, "vk": vk})

    bid = int(bid_raw)
    b = _db.broadcast_get(bid)
    if not b:
        return web.json_response({"ok": False, "error": "not found"}, status=404)
    st = b.get("status", "draft")

    if action == "send_now":
        if st != "draft":
            return web.json_response({"ok": False, "error": f"cannot send from {st}"},
                                     status=400)
        if not (b.get("text") or "").strip() and (b.get("media_type") == "none"):
            return web.json_response({"ok": False, "error": "пустое сообщение"},
                                     status=400)
        _db.broadcast_update(bid, {"status": "scheduled", "scheduled_at": None})
        return web.json_response({"ok": True})

    if action == "schedule":
        if st != "draft":
            return web.json_response({"ok": False, "error": f"cannot schedule from {st}"},
                                     status=400)
        try:
            p = await request.json()
        except Exception:
            p = {}
        sched = (p.get("scheduled_at") or "").strip()
        if not sched:
            return web.json_response({"ok": False, "error": "scheduled_at required"},
                                     status=400)
        # Treat input as Moscow time, convert to UTC for DB
        try:
            dt = datetime.fromisoformat(sched)
            dt_utc = dt - timedelta(hours=3)
        except Exception:
            return web.json_response({"ok": False, "error": "bad datetime"}, status=400)
        _db.broadcast_update(bid, {"status": "scheduled", "scheduled_at": dt_utc})
        return web.json_response({"ok": True})

    if action == "pause":
        if st != "sending":
            return web.json_response({"ok": False, "error": f"cannot pause from {st}"},
                                     status=400)
        _db.broadcast_update(bid, {"status": "paused"})
        return web.json_response({"ok": True})

    if action == "resume":
        if st != "paused":
            return web.json_response({"ok": False, "error": f"cannot resume from {st}"},
                                     status=400)
        # If the in-process task is still alive (paused mid-flight) we can
        # simply flip back to 'sending' and the same coroutine resumes its
        # loop. If the task died (process restarted while paused), set
        # 'scheduled' so the scheduler claims it via the normal path.
        from bot.broadcasts.scheduler import is_running as _br_running
        if _br_running(bid):
            _db.broadcast_update(bid, {"status": "sending"})
        else:
            _db.broadcast_update(bid, {"status": "scheduled", "scheduled_at": None})
        return web.json_response({"ok": True})

    if action == "cancel":
        if st in ("completed", "cancelled", "failed"):
            return web.json_response({"ok": False, "error": "already finished"},
                                     status=400)
        _db.broadcast_update(bid, {"status": "cancelled",
                                   "finished_at": datetime.utcnow()})
        return web.json_response({"ok": True})

    if action == "delete":
        if st in ("sending", "paused"):
            return web.json_response({"ok": False, "error": "сначала отмените"},
                                     status=400)
        _db.broadcast_delete(bid)
        return web.json_response({"ok": True})

    if action == "clone":
        new_data = {
            "title": (b.get("title") or "") + " (копия)",
            "text": b.get("text", ""), "parse_mode": b.get("parse_mode", "HTML"),
            "media_type": b.get("media_type", "none"),
            "media_path": b.get("media_path", ""), "media_url": b.get("media_url", ""),
            "media_tg_file_id": "", "media_vk_attach": "",
            "buttons": json.loads(b.get("buttons_json") or "[]"),
            "disable_preview": b.get("disable_preview"), "silent": b.get("silent"),
            "protect_content": b.get("protect_content"), "pin": b.get("pin"),
            "personalize": b.get("personalize"),
            "target_platform": b.get("target_platform", "all"),
            "target_filter": json.loads(b.get("target_filter") or "{}"),
            "rate_per_sec": b.get("rate_per_sec", 20),
            "notes": b.get("notes", ""), "status": "draft",
        }
        new_id = _db.broadcast_create(new_data)
        return web.json_response({"ok": True, "id": new_id})

    if action == "test":
        try:
            p = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad json"}, status=400)
        uid = int(p.get("user_id") or 0)
        plat = (p.get("platform") or "tg").lower()
        if not uid:
            return web.json_response({"ok": False, "error": "user_id required"},
                                     status=400)
        try:
            status_str, err = await send_one(b, {"user_id": uid, "platform": plat})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)[:300]})
        if status_str == "sent":
            return web.json_response({"ok": True, "status": "sent"})
        return web.json_response({"ok": False, "status": status_str, "error": err})

    return web.json_response({"ok": False, "error": "unknown action"}, status=400)


@_api_require_auth
async def api_broadcast_progress(request: web.Request) -> web.Response:
    bid = int(request.match_info["bid"])
    b = _db.broadcast_get(bid)
    if not b:
        return web.json_response({"ok": False, "error": "not found"}, status=404)
    return web.json_response({
        "ok": True,
        "status": b.get("status"),
        "total": b.get("total_recipients", 0),
        "sent": b.get("sent_count", 0),
        "failed": b.get("failed_count", 0),
        "blocked": b.get("blocked_count", 0),
        "skipped": b.get("skipped_count", 0),
        "clicked": b.get("clicked_count", 0),
    })


@_api_require_auth
async def api_broadcast_recipients(request: web.Request) -> web.Response:
    bid = int(request.match_info["bid"])
    status = request.rel_url.query.get("status", "")
    offset = int(request.rel_url.query.get("offset", "0"))
    limit = min(200, int(request.rel_url.query.get("limit", "100")))
    items = _db.broadcast_recipients_page(bid, status=status, limit=limit, offset=offset)
    return web.json_response({"ok": True, "items": items})


@_api_require_auth
async def api_upload_media(request: web.Request) -> web.Response:
    reader = await request.multipart()
    field = await reader.next()
    if field is None or field.name != "file":
        return web.json_response({"ok": False, "error": "no file field"}, status=400)
    filename = field.filename or "upload.bin"
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        return web.json_response({"ok": False, "error": f"тип {ext} не поддерживается"},
                                 status=400)
    safe = secrets.token_hex(8) + ext
    target = MEDIA_DIR / safe
    size = 0
    with open(target, "wb") as f:
        while True:
            chunk = await field.read_chunk(64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > 50 * 1024 * 1024:
                f.close()
                target.unlink(missing_ok=True)
                return web.json_response({"ok": False, "error": "файл больше 50 МБ"},
                                         status=413)
            f.write(chunk)
    media_type = EXT_TO_TYPE.get(ext, "document")
    return web.json_response({
        "ok": True,
        "path": str(target),
        "url": f"/admin/broadcast-media/{safe}",
        "media_type": media_type,
        "filename": filename,
        "size": size,
    })


@_require_auth
async def handle_media(request: web.Request) -> web.Response:
    name = request.match_info["filename"]
    # Prevent traversal
    if "/" in name or ".." in name:
        raise web.HTTPNotFound()
    p = MEDIA_DIR / name
    if not p.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(p)


# ── Click redirect (public, no auth) ─────────────────────────────────────────

async def handle_click_redirect(request: web.Request) -> web.Response:
    try:
        bid = int(request.match_info["bid"])
        uid = int(request.match_info["uid"])
        plat = request.match_info["plat"]
        idx = int(request.match_info["idx"])
    except Exception:
        raise web.HTTPBadRequest()
    target = request.rel_url.query.get("u", "")
    if not target.startswith(("http://", "https://", "tg://", "vk://")):
        raise web.HTTPBadRequest(text="bad url")
    try:
        _db.broadcast_log_click(bid, uid, plat, idx, target)
    except Exception:
        logger.exception("click log failed")
    raise web.HTTPFound(target)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _esc(s: Any) -> str:
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                  .replace(">", "&gt;").replace('"', "&quot;"))


def _safe_json_for_html(obj: Any) -> str:
    """Encode JSON safely for embedding inside an HTML <script> tag.

    Escapes characters that could allow an HTML parser break-out (`<`, `>`, `&`)
    and the JSON-significant line/paragraph separators (U+2028/U+2029).
    Result is still valid JSON parseable by `JSON.parse(textContent)`.
    """
    return (json.dumps(obj, ensure_ascii=False)
                .replace("<",       "\\u003c")
                .replace(">",       "\\u003e")
                .replace("&",       "\\u0026")
                .replace("\u2028",  "\\u2028")
                .replace("\u2029",  "\\u2029"))


def _fmt_dt(s: Any) -> str:
    """Stored timestamps are naive UTC — render in MSK (UTC+3)."""
    if not s:
        return ""
    try:
        if isinstance(s, str):
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            dt = s
        if dt.tzinfo is not None:
            dt = dt.astimezone(tz=None).replace(tzinfo=None)
        dt_msk = dt + timedelta(hours=3)
        return dt_msk.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return str(s)[:16]


def _to_local_input(s: Any) -> str:
    """Convert UTC datetime string to <input type=datetime-local> value (MSK)."""
    if not s:
        return ""
    try:
        if isinstance(s, str):
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            dt = s
        if dt.tzinfo is None:
            dt = dt + timedelta(hours=3)
        return dt.strftime("%Y-%m-%dT%H:%M")
    except Exception:
        return ""


# ── Route registration ──────────────────────────────────────────────────────

def register_broadcast_routes(app: web.Application) -> None:
    app.router.add_get ("/admin/broadcasts",                         handle_broadcasts)
    app.router.add_get ("/admin/broadcasts/new",                     handle_broadcast_new)
    app.router.add_get ("/admin/broadcasts/{bid:\\d+}",              handle_broadcast_detail)
    app.router.add_post("/admin/api/broadcasts",                     api_broadcast_create)
    app.router.add_post("/admin/api/broadcasts/upload-media",        api_upload_media)
    app.router.add_post("/admin/api/broadcasts/{bid:\\d+}",          api_broadcast_update)
    app.router.add_post("/admin/api/broadcasts/{bid}/{action}",      api_broadcast_action)
    app.router.add_get ("/admin/api/broadcasts/{bid:\\d+}/progress", api_broadcast_progress)
    app.router.add_get ("/admin/api/broadcasts/{bid:\\d+}/recipients", api_broadcast_recipients)
    app.router.add_get ("/admin/broadcast-media/{filename}",         handle_media)
    app.router.add_get ("/r/{bid:\\d+}/{uid:\\d+}/{plat}/{idx:\\d+}", handle_click_redirect)
