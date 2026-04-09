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
- 1 credit per generation, 2 credits for 4K
- Packages: 30 credits (99₽), 100 credits (299₽), 200 credits (549₽)

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

## Dependencies
Managed via `requirements.txt` with pip. Key packages:
- aiogram>=3.15, vkbottle>=4.8, google-genai>=1.9
- pydantic-settings>=2.7, Pillow>=11.0, aiohttp>=3.9
- psycopg2-binary>=2.9
