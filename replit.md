# PicGenAI — Telegram + VK Image Generation Bot

## Overview
An asynchronous multi-platform bot (Telegram + VK) for AI image generation using Google Gemini / Vertex AI. Built with Python 3.12, aiogram 3.x (Telegram), and vkbottle (VK). Includes a credit-based monetization system with FreeKassa (VK) and Pally.info (TG) payment integration.

## Architecture
- **Entry point**: `start_all.py` — runs Telegram bot, VK bot, and a web server concurrently via asyncio
- **Web server**: aiohttp on port 5000 (dev) / 8080 (Northflank) — landing page, payment pages, webhooks
- **Bot logic**: `bot/` — Telegram handlers, middlewares, services
- **VK logic**: `vk_bot/` — VK handlers
- **Shared services**: `bot/services/vertex_ai_service.py` — Google Gemini AI client
- **Payment (FreeKassa)**: `bot/services/freekassa_service.py` — URL-based payment for VK
- **Payment (Pally)**: `bot/services/payment_service.py` — Pally.info API for Telegram
- **Web pages**: `web/templates/` — landing (index.html), success.html, fail.html (+ fallback in code)
- **Webhooks**: `bot/web_server.py` — FreeKassa + Pally webhook handlers with signature verification, idempotency
- **Admin broadcasts**: `/admin/broadcast` in `bot/web_admin.py` — Telegram/VK mass messaging with targeting, previews, test send, inline/open-link button, progress and DB history
- **Config**: `bot/config.py` — pydantic-settings from environment variables
- **Database**: `bot/db.py` — PostgreSQL persistence (users, API keys, payments)

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
- `GITHUB_PERSONAL_ACCESS_TOKEN` — GitHub push token

## Running
- Workflow: "Start application" runs `python start_all.py`
- Port: 5000 (dev, from PORT env var), 8080 (Northflank default)
- Bots start automatically if their respective tokens are set
- Admin panel: `/adminmrxgyt` command in Telegram
- Web admin broadcasts: `/admin/broadcast`

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
- Video: 5 credits (Veo 3.1), 3 credits (Fast), 2 credits (Lite)
- Packages: 30 credits (99₽), 100 credits (299₽), 200 credits (549₽)

## Video Generation (Veo 3.1)
- Models: veo-3.1-generate-001, veo-3.1-fast-generate-001, veo-3.1-lite-generate-001
- Settings: duration (4/6/8 sec), resolution (720p/1080p), aspect ratio (16:9, 9:16), audio (on/off)
- Video models only accept text prompts — photo input is rejected with a message
- Implementation: `VertexAIService.generate_video()` with async polling (10s interval, 600s timeout)
- Supported on both Telegram (reply_video) and VK (document upload as .mp4)
- User settings: video_duration, video_resolution, video_aspect_ratio, video_audio (persisted)
- Interactive video panel: after selecting a video model, opens unified panel with all settings + toggle buttons + cost info
- Panel callbacks: `vp_aspect_*`, `vp_dur_*`, `vp_res_*`, `vp_audio` — each re-renders panel in-place
- Settings summary shows compact "🎬 Видео: Xs • Xp • 🔊 (X кр.)" button that opens the panel

## Bot UI
- Persistent keyboard: Menu, Ideas, Settings, Balance, Stop
- Balance screen: shows total/free/purchased credits + 3 purchase buttons
- Menu: displays credits with visual separator (purchased vs free)

## PostgreSQL Tables
- `bot_user_settings` — user_id BIGINT PK, data TEXT
- `bot_api_keys` — id SERIAL PK, key TEXT UNIQUE
- `bot_payments` — order_id TEXT PK, payment_id, user_id, pack_key, amount, status, timestamps
- `bot_broadcast_campaigns` — admin broadcast campaigns, filters, progress counters, status, timestamps
- `bot_broadcast_deliveries` — per-user Telegram/VK broadcast delivery logs with error text

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
- aiogram>=3.15, vkbottle>=4.8, google-genai>=1.9
- pydantic-settings>=2.7, Pillow>=11.0, aiohttp>=3.9
- psycopg2-binary>=2.9
