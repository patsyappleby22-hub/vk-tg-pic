# PicGenAI — Telegram + VK Image Generation Bot

## Overview
An asynchronous multi-platform bot (Telegram + VK) for AI image, video, and music generation using Google Gemini / Vertex AI. Built with Python 3.12, aiogram 3.x (Telegram), and vkbottle (VK). Includes a credit-based monetization system with FreeKassa (VK) and Pally.info (TG) payment integration.

## Architecture
- **Entry point**: `start_all.py` — runs Telegram bot, VK bot, and a web server concurrently via asyncio
- **Web server**: aiohttp on port 5000 (dev) / 8080 (Northflank) — landing page, payment pages, webhooks
- **Bot logic**: `bot/` — Telegram handlers, middlewares, services
- **VK logic**: `vk_bot/` — VK handlers
- **Shared services**: `bot/services/vertex_ai_service.py` — Google Gemini AI client for images, Veo video, and Lyria music
- **Payment (FreeKassa)**: `bot/services/freekassa_service.py` — URL-based payment for VK
- **Payment (Pally)**: `bot/services/payment_service.py` — Pally.info API for Telegram
- **Web pages**: `web/templates/` — landing (index.html), success.html, fail.html (+ fallback in code)
- **Web chat**: `bot/web_chat.py` — user-facing single-page chat UI at `/chat` (Russian, dark/lavender theme, no emoji). Login by 6-digit code delivered via TG bot or VK community message; HMAC-signed cookie session (30 d). Modes: chat (Gemini/Grok), image, video, music. Reuses bot credit balance (`reserve/confirm/release_credits`) and unified log-channel posting. Per-user chats + messages persisted in `bot_web_chats` / `bot_web_messages`; media files served through `/chat/api/media/<id>` with on-disk LRU cache in `/tmp/web_media_cache`.
- **Webhooks**: `bot/web_server.py` — FreeKassa + Pally webhook handlers with signature verification, idempotency
- **Config**: `bot/config.py` — pydantic-settings from environment variables
- **Database**: `bot/db.py` — PostgreSQL persistence (users, API keys, SA files, payments, key history)
- **Credential storage**: API keys (`bot_api_keys`) and Service-Account JSON files (`bot_sa_files`) persist in PostgreSQL. SA files are also mirrored to `data/service_accounts/` so `vertex_ai_service` can load them by path. Per-key request history (`bot_key_history`) is keyed by a stable `slot_label` (`api:<sha1[:10]>` for API keys, filename stem for SA), so it survives reordering / adding / removing of other slots.

## Required Secrets
- `TELEGRAM_BOT_TOKEN` — Bot token from @BotFather
- `VK_BOT_TOKEN` — VK community token
- `GOOGLE_CLOUD_API_KEY` — Google Cloud API key with Vertex AI access

## Payment Secrets (FreeKassa — VK)
- `FREEKASSA_SHOP_ID` — FreeKassa shop ID
- `FREEKASSA_SECRET1` — Secret word 1 (for payment URL signing)
- `FREEKASSA_SECRET2` — Secret word 2 (for webhook verification)
- `FREEKASSA_API_KEY` — API key

## Payment Secrets (Pally — TG)
- `PALLY_SHOP_ID` — Pally.info shop ID
- `PALLY_TOKEN` — Pally.info API token
- `BASE_URL` — Public URL for webhooks

## Other Secrets
- `DATABASE_URL` — PostgreSQL connection string
- `LOG_CHANNEL_ID` — Telegram channel id for generation log/media re-hosting (used by web chat too)
- `WEB_SESSION_SECRET` — optional; HMAC key for `/chat` session cookies (falls back to a key derived from `ADMIN_PASSWORD`)

## Running
- Workflow: "Start application" runs `python start_all.py`
- Port: 5000 (dev, from PORT env var), 8080 (Northflank default)
- Bots start automatically if their respective tokens are set
- Admin panel: `/adminmrxgyt` command in Telegram

## Web Endpoints
- `GET /` — Landing page (PicGenAI)
- `GET /shop-verification-WG76VJD7xl.txt` — Pally site verification
- `GET /payment/success` — Payment success redirect
- `GET /payment/fail` — Payment failure redirect
- `POST /webhook/pally` — Pally.info payment webhook (signature-verified)
- `POST /webhook/pally/refund` — Refund webhook
- `POST /webhook/pally/chargeback` — Chargeback webhook
- `POST /api/freekassa/notification` — FreeKassa payment webhook (MD5 signature-verified)

## Credits System
- 5 free credits on registration (FREE_CREDITS = 5)
- 1 credit per image generation, 2 credits for 4K
- Video: cost is calculated dynamically by `calc_video_credits(model, duration, audio)` in `bot/user_settings.py`.
  Formula: `ceil((google_usd_per_sec * duration / PRICE_MARKDOWN) / CREDIT_USD)` with `PRICE_MARKDOWN=3.0`, `CREDIT_USD=1.40/30`.
  Pricing matrix (per Google):
    - veo-3.1-generate-001: $0.20/s video + $0.20/s audio
    - veo-3.1-fast-generate-001: $0.10/s + $0.05/s audio
    - veo-3.1-lite-generate-001: $0.05/s + $0.03/s audio
  Resulting credits, e.g. 8 sec: Standard 12/23 (no/with audio), Fast 6/9, Lite 3/5. Image-to-video & extension force 8 sec.
- Music: 4 credits (Lyria 3 Pro full song, $0.08), 2 credits (Lyria 3 30s clip, $0.04)
- Packages: 30 credits (99₽), 100 credits (299₽), 200 credits (549₽)

## Video Generation (Veo 3.1)
- Models: veo-3.1-generate-001, veo-3.1-fast-generate-001, veo-3.1-lite-generate-001
- All three models support: text→video, image→video, video extension, audio
- Settings: duration (4/6/8 sec), resolution (720p/1080p/4K — Lite up to 1080p), aspect ratio (16:9, 9:16), audio (on/off)
- For image→video and extension, duration is forced to 8 sec (Google API limitation)
- Implementation: `VertexAIService.generate_video()` uses Gemini Developer API for API-key slots with polling (10s interval, 600s timeout)
- Supported on both Telegram (reply_video) and VK (document upload as .mp4)
- User settings: video_duration, video_resolution, video_aspect_ratio, video_audio (persisted)
- Interactive video panel: after selecting a video model, opens unified panel with all settings + toggle buttons + cost info
- Panel callbacks: `vp_aspect_*`, `vp_dur_*`, `vp_res_*`, `vp_audio` — each re-renders panel in-place
- Settings summary shows compact "🎬 Видео: Xs • Xp • 🔊 (X кр.)" button that opens the panel

## Music Generation (Lyria 3)
- Models: lyria-3-pro-preview (full song, 4 credits) and lyria-3-clip-preview (30s clip, 2 credits)
- Inputs: text prompts and image + prompt; output is MP3 audio
- Implementation: `VertexAIService.generate_music()` uses `generate_content` with AUDIO/TEXT response modalities and extracts MP3 bytes from inline data; runs against Vertex AI when a service-account slot is selected, against Gemini Developer API when an api-key slot is selected.

## Authentication & API key rotation
- Two slot types in `bot/services/vertex_ai_service.py`:
  - `_ApiKeySlot` — Google API key (Vertex Express) for Imagen/Gemini text/chat. Veo & Lyria fall back to Gemini Developer API endpoint with the same key.
  - `_CredSlot` — service-account JSON loaded from `data/service_accounts/`. Supports image, chat, **video (Veo)**, and **music (Lyria)** all through Vertex AI — required to spend Google's $300 trial credit on Veo/Lyria.
- Both slot types are loaded together; rotation cycles all of them. 429 → 60s cooldown.
- **Routing policy** (`_filter_slots_for_model`):
  - Image / chat → any slot (API key OR service account).
  - **Video (Veo)** and **Music (Lyria)** → **only** `_CredSlot` (Vertex AI / service account). If no SA available, request raises `QuotaExceededError` → user sees the standard "модель сейчас перегружена 😔" message. This guarantees Google's $300 trial credit is the funding source for Veo/Lyria.
- Admin panel `/admin/api-keys`:
  - Add/edit/delete API keys (with optional project ID for Veo via API key).
  - Upload service-account JSON via file picker → endpoint `POST /admin/api/keys/sa/upload` (multipart) → validated (`type=service_account`, `project_id`, `private_key`, `client_email`) → stored in `data/service_accounts/` (chmod 600) → `vertex_service.reload_keys()` applies it instantly.
  - List/delete uploaded SAs: `GET /admin/api/keys/sa/list`, `POST /admin/api/keys/sa/delete`.
- Supported on both Telegram (`reply_audio`) and VK (document upload as .mp3)
- Music models appear in the same unified model/settings picker as image and video models

## Bot UI
- Persistent keyboard: Menu, Ideas, Settings, Balance, Stop
- Balance screen: shows total/free/purchased credits + 3 purchase buttons
- Menu: displays credits with visual separator (purchased vs free)

## PostgreSQL Tables
- `bot_user_settings` — user_id BIGINT PK, data TEXT
- `bot_api_keys` — id SERIAL PK, key TEXT UNIQUE
- `bot_payments` — order_id TEXT PK, payment_id, user_id, pack_key, amount, status, timestamps

## Bot Links
- Telegram: https://t.me/PicGenAI_26_bot
- VK: https://vk.ru/picgenai
- Support: https://t.me/ShadowsockTM
- GitHub: https://github.com/mrxgyt/vk-tg-pic

## Deployment
- Northflank (Docker): Dockerfile in root, auto-builds from GitHub
- Templates fallback: if web/templates/ files missing, built-in HTML strings used
- FreeKassa notification URL: https://vk-tg-picgenai.ru/api/freekassa/notification

## Error Handling & Resilience
- **VK block caching**: VK API errors 5/8/27 trigger a 10-minute cooldown (`VK_BLOCK_COOLDOWN=600`). During cooldown, VK publishing is skipped entirely (no repeated failing API calls). Block status checked at scheduler level and inside photo upload loop for immediate abort.
- **503 vs 429 separation**: Google API 503 (Service Unavailable) gets a short 15s cooldown vs 60s for 429 (rate limit). This allows faster recovery from temporary server issues.
- **API key history**: Each key slot tracks last 200 requests with status, duration, error details. Viewable in admin panel.

## Dependencies
Managed via `requirements.txt` with pip. Key packages:
- aiogram>=3.15, vkbottle>=4.8, google-genai installed with Lyria-capable 1.52+ runtime
- pydantic-settings>=2.7, Pillow>=11.0, aiohttp>=3.9
- psycopg2-binary>=2.9

## Broadcasts (рассылка)
Admin section "Рассылки" with full lifecycle: drafts → schedule → sending → done/cancelled.
- Tables: `bot_broadcasts`, `bot_broadcast_recipients` (UNIQUE bid+uid+plat), `bot_broadcast_clicks`, `bot_broadcast_templates`.
- Audience builder filters by platform (tg/vk/all), credits range, generations range, paid/unpaid, active days, include/exclude IDs, exclude_blocked.
- Senders: TG via aiogram (FSInputFile, retry on flood, blocked detection, file_id caching); VK via direct HTTP API (`messages.send`) with `keyboard` JSON inline buttons.
- Personalization tokens: `{name}`, `{credits}`, `{user_id}`.
- Click tracking: `/r/{bid}/{uid}/{plat}/{idx}` redirect with atomic counter inc.
- Scheduler: `bot.broadcasts.scheduler.broadcast_loop` (tick=5s) — picks due broadcasts, materializes audience, drains queue at `rate_per_sec`, honors pause/cancel.
- Media: `data/broadcast_media` (env `BROADCAST_MEDIA_DIR`), 50MB cap, ext-allowlisted.
- Schedule input is MSK; converted to UTC before DB write.
- Custom date/time picker (in `_compose_html`): replaces native `<input type="datetime-local">` with a dark themed popup (calendar grid Mon-first, hour/minute spinners ±5, presets +15м/+1ч/+3ч/Завтра 09:00/12:00/+неделя). Hidden `#bc-when-dt` keeps `YYYY-MM-DDTHH:MM` (MSK). On edit pages with prefilled schedule, `syncWhenFromHidden()` IIFE auto-switches `#bc-when` to `schedule` mode so launch hits `/schedule` and not `/send_now`.

## Autopub Admin Redesign (2026-05)
Раздел `/admin/autopub` (`bot/web_admin.py` `handle_autopub`) переведён на тот же тёмно-фиолетовый стиль, что и рассылки.
- CSS-префикс `.ap-*` (status pill, tabs с счётчиком, post card 220px+grid, status pills с dot-индикаторами, banner/bar/log/trends/think для генератора, warn/info/flags для настроек, modal-h).
- Все эмодзи убраны из заголовков, статусов, кнопок и подписей. Сохранены только Unicode-знаки `✓ ✗ ×` и стрелки `›` в `<summary>`.
- Все ID и обработчики событий сохранены: `gen-btn`, `gen-banner`, `gen-bar`, `gen-log`, `gen-trend-list`, `gen-think-wrap`, `edit-modal`, `feedback-modal`, `queue-list`, `lightbox`, `post-card-{pid}`, `prompt-{pid}`. JS заменён только в плане ярлыков и стилизации (через CSS-классы `.ap-log-entry`, `.ap-trend-btn`, `is-error`/`is-done` на баннере вместо inline-стилей).
- `_autopub_status_badge()` рендерит `<span class="ap-pill is-{status}">` — статусы draft/approved/publishing/published/error/rejected.
- `_render_post_card()` — карточка `220px / 1fr`, тематика крупным заголовком, метки трендов/фидбэка как `.ap-tag`, текст с маской-фейдом, промпт-toggle с моноширинным фоном.
- Исправлен предсуществующий баг в JS: `var _editPosts = {{}};` (от f-string) → `{}`, что устраняло SyntaxError на странице с пустой очередью.
- `bot/autopub/publisher.py` НЕ ТРОНУТ — TG `sendMediaGroup` (1-3 фото) и VK 3-step (`photos.getWallUploadServer` → multipart upload → `photos.saveWallPhoto` → `wall.post`) работают без изменений; `VK_USER_TOKEN` нужен для постинга в стену группы.
