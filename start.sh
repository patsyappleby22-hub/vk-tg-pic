#!/usr/bin/env bash
# start.sh — Install dependencies and launch the Telegram bot.
# Run this from the telegram-bot/ directory or from the workspace root.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "==> Installing Python dependencies..."
pip install -q -r requirements.txt

echo "==> Starting Telegram bot..."
exec python -m bot.main
