"""
bot/db.py
~~~~~~~~~
Optional PostgreSQL persistence layer.

If DATABASE_URL env-var is set, all user settings and API keys
are stored in PostgreSQL. Otherwise the module is a no-op and
the file-based fallback is used.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_DATABASE_URL: str | None = os.environ.get("DATABASE_URL", "").strip() or None
_conn = None


def _get_conn():
    global _conn
    if _conn is None or _conn.closed:
        import psycopg2
        _conn = psycopg2.connect(_DATABASE_URL)
        _conn.autocommit = True
    return _conn


def is_available() -> bool:
    return bool(_DATABASE_URL)


def init_tables() -> None:
    if not _DATABASE_URL:
        return
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_user_settings (
                    user_id BIGINT PRIMARY KEY,
                    data    TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_api_keys (
                    id  SERIAL PRIMARY KEY,
                    key TEXT UNIQUE NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_payments (
                    order_id    TEXT PRIMARY KEY,
                    payment_id  TEXT,
                    user_id     BIGINT NOT NULL,
                    pack_key    TEXT NOT NULL,
                    amount      REAL NOT NULL,
                    status      TEXT NOT NULL DEFAULT 'pending',
                    created_at  TIMESTAMP DEFAULT NOW(),
                    completed_at TIMESTAMP
                )
            """)
        logger.info("db: tables ready (PostgreSQL)")
    except Exception:
        logger.exception("db: failed to init tables")


# ── User settings ──────────────────────────────────────────────────────────────

def load_all_users() -> dict[int, dict[str, Any]]:
    """Return {user_id: settings_dict} for all rows."""
    if not _DATABASE_URL:
        return {}
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT user_id, data FROM bot_user_settings")
            rows = cur.fetchall()
        result = {}
        for uid, raw in rows:
            try:
                result[int(uid)] = json.loads(raw)
            except Exception:
                pass
        logger.info("db: loaded %d users from PostgreSQL", len(result))
        return result
    except Exception:
        logger.exception("db: failed to load users")
        return {}


def save_all_users(snapshot: dict[int, dict[str, Any]]) -> None:
    """Upsert all users in one transaction."""
    if not _DATABASE_URL:
        return
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            for uid, data in snapshot.items():
                cur.execute("""
                    INSERT INTO bot_user_settings (user_id, data)
                    VALUES (%s, %s)
                    ON CONFLICT (user_id) DO UPDATE SET data = EXCLUDED.data
                """, (uid, json.dumps(data, ensure_ascii=False)))
        logger.info("db: saved %d users to PostgreSQL", len(snapshot))
    except Exception:
        logger.exception("db: failed to save users")


# ── API keys ───────────────────────────────────────────────────────────────────

def load_api_keys() -> list[str]:
    if not _DATABASE_URL:
        return []
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT key FROM bot_api_keys ORDER BY id")
            return [row[0] for row in cur.fetchall()]
    except Exception:
        logger.exception("db: failed to load api keys")
        return []


def save_api_keys(keys: list[str]) -> None:
    if not _DATABASE_URL:
        return
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM bot_api_keys")
            for key in keys:
                cur.execute(
                    "INSERT INTO bot_api_keys (key) VALUES (%s) ON CONFLICT DO NOTHING",
                    (key,)
                )
        logger.info("db: saved %d api keys to PostgreSQL", len(keys))
    except Exception:
        logger.exception("db: failed to save api keys")


def save_payment(order_id: str, user_id: int, pack_key: str, amount: float) -> None:
    if not _DATABASE_URL:
        return
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bot_payments (order_id, user_id, pack_key, amount, status)
                VALUES (%s, %s, %s, %s, 'pending')
                ON CONFLICT (order_id) DO NOTHING
            """, (order_id, user_id, pack_key, amount))
    except Exception:
        logger.exception("db: failed to save payment %s", order_id)


def complete_payment(order_id: str, payment_id: str = "") -> bool:
    if not _DATABASE_URL:
        return True
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE bot_payments
                SET status = 'success', payment_id = %s, completed_at = NOW()
                WHERE order_id = %s AND status = 'pending'
            """, (payment_id, order_id))
            return cur.rowcount > 0
    except Exception:
        logger.exception("db: failed to complete payment %s", order_id)
        return False


def get_payment(order_id: str) -> dict | None:
    if not _DATABASE_URL:
        return None
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT order_id, user_id, pack_key, amount, status FROM bot_payments WHERE order_id = %s",
                (order_id,),
            )
            row = cur.fetchone()
            if row:
                return {
                    "order_id": row[0],
                    "user_id": row[1],
                    "pack_key": row[2],
                    "amount": row[3],
                    "status": row[4],
                }
    except Exception:
        logger.exception("db: failed to get payment %s", order_id)
    return None


_processed_orders: set[str] = set()


def mark_order_processed_memory(order_id: str) -> bool:
    if order_id in _processed_orders:
        return False
    _processed_orders.add(order_id)
    return True


def get_all_payments(limit: int = 1000) -> list[dict]:
    """Return recent payments, newest first."""
    if not _DATABASE_URL:
        return []
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT order_id, payment_id, user_id, pack_key, amount, status, created_at, completed_at
                FROM bot_payments
                ORDER BY created_at DESC
                LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
        return [
            {
                "order_id": r[0], "payment_id": r[1], "user_id": r[2],
                "pack_key": r[3], "amount": r[4], "status": r[5],
                "created_at": r[6].isoformat() if r[6] else "",
                "completed_at": r[7].isoformat() if r[7] else "",
            }
            for r in rows
        ]
    except Exception:
        logger.exception("db: failed to get all payments")
        return []


def get_user_payments(user_id: int) -> list[dict]:
    """Return payments for a specific user, newest first."""
    if not _DATABASE_URL:
        return []
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT order_id, payment_id, pack_key, amount, status, created_at, completed_at
                FROM bot_payments WHERE user_id = %s ORDER BY created_at DESC
            """, (user_id,))
            rows = cur.fetchall()
        return [
            {
                "order_id": r[0], "payment_id": r[1], "pack_key": r[2],
                "amount": r[3], "status": r[4],
                "created_at": r[5].isoformat() if r[5] else "",
                "completed_at": r[6].isoformat() if r[6] else "",
            }
            for r in rows
        ]
    except Exception:
        logger.exception("db: failed to get user payments for %s", user_id)
        return []


def get_payment_stats() -> dict:
    """Aggregate payment statistics."""
    if not _DATABASE_URL:
        return {"success_count": 0, "total_revenue": 0.0, "total_count": 0}
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE status='success') AS success_count,
                    COALESCE(SUM(amount) FILTER (WHERE status='success'), 0) AS total_revenue,
                    COUNT(*) AS total_count
                FROM bot_payments
            """)
            row = cur.fetchone()
        return {
            "success_count": row[0] or 0,
            "total_revenue": float(row[1] or 0),
            "total_count": row[2] or 0,
        }
    except Exception:
        logger.exception("db: failed to get payment stats")
        return {"success_count": 0, "total_revenue": 0.0, "total_count": 0}


def api_keys_table_has_rows() -> bool:
    """Check if any API keys exist in DB (used for migration guard)."""
    if not _DATABASE_URL:
        return False
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM bot_api_keys LIMIT 1")
            return cur.fetchone() is not None
    except Exception:
        return False
