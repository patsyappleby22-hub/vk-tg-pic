"""
bot/api_keys_store.py
~~~~~~~~~~~~~~~~~~~~~
Persistent storage for Google API keys managed via the admin panel.
Keys are saved to data/api_keys.json.
On first startup env-var keys are migrated into the store automatically.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_STORE_PATH = Path(__file__).resolve().parent.parent / "data" / "api_keys.json"


def _load() -> list[str]:
    try:
        if _STORE_PATH.exists():
            data = json.loads(_STORE_PATH.read_text(encoding="utf-8"))
            return [k for k in data if isinstance(k, str) and k.strip()]
    except Exception as e:
        logger.warning("api_keys_store: failed to load: %s", e)
    return []


def _save(keys: list[str]) -> None:
    try:
        _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STORE_PATH.write_text(json.dumps(keys, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("api_keys_store: failed to save: %s", e)


def migrate_env_keys() -> None:
    """
    Import env-var keys into the store on first-time setup only.
    If the store file already exists, skip — the admin has full control.
    """
    if _STORE_PATH.exists():
        return

    env_keys: list[str] = []
    for var in ("GOOGLE_CLOUD_API_KEY", "GOOGLE_CLOUD_API_KEY_1",
                "GOOGLE_CLOUD_API_KEY_2", "GOOGLE_CLOUD_API_KEY_3"):
        val = os.environ.get(var, "").strip()
        if val and val not in env_keys:
            env_keys.append(val)

    if not env_keys:
        return

    _save(env_keys)
    logger.info("api_keys_store: first-time setup, migrated %d env key(s) into store", len(env_keys))


def get_all_keys() -> list[str]:
    """Return all stored API keys."""
    return _load()


def add_key(key: str) -> bool:
    """Add a new API key. Returns False if already present."""
    key = key.strip()
    if not key:
        return False
    keys = _load()
    if key in keys:
        return False
    keys.append(key)
    _save(keys)
    return True


def remove_key(index: int) -> str | None:
    """Remove key by index. Returns removed key or None."""
    keys = _load()
    if index < 0 or index >= len(keys):
        return None
    removed = keys.pop(index)
    _save(keys)
    return removed


def mask_key(key: str) -> str:
    """Show only first 8 and last 4 chars."""
    if len(key) <= 12:
        return key[:4] + "..." + key[-2:]
    return key[:8] + "..." + key[-4:]
