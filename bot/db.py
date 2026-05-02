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
import threading
from typing import Any

logger = logging.getLogger(__name__)

_DATABASE_URL: str | None = os.environ.get("DATABASE_URL", "").strip() or None

# Thread-local storage: each thread (main asyncio thread, VK bot thread, etc.)
# gets its own psycopg2 connection — psycopg2 connections are NOT thread-safe.
_local = threading.local()

# Global lock for write operations that touch many rows (save_all_users)
_write_lock = threading.Lock()


def _get_conn():
    """Return a psycopg2 connection for the current thread."""
    conn = getattr(_local, "conn", None)
    if conn is None or conn.closed:
        import psycopg2
        conn = psycopg2.connect(_DATABASE_URL)
        conn.autocommit = True
        _local.conn = conn
        logger.debug("db: opened new connection for thread '%s'", threading.current_thread().name)
    return conn


def _close_conn() -> None:
    """Close the current thread's connection (call on thread exit)."""
    conn = getattr(_local, "conn", None)
    if conn and not conn.closed:
        try:
            conn.close()
        except Exception:
            pass
    _local.conn = None


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
                    key TEXT UNIQUE NOT NULL,
                    project_id TEXT
                )
            """)
            try:
                cur.execute("ALTER TABLE bot_api_keys ADD COLUMN IF NOT EXISTS project_id TEXT")
            except Exception:
                pass
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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_image_logs (
                    id              SERIAL PRIMARY KEY,
                    user_id         BIGINT NOT NULL,
                    user_name       TEXT NOT NULL DEFAULT '',
                    platform        TEXT NOT NULL DEFAULT 'tg',
                    prompt          TEXT NOT NULL DEFAULT '',
                    model           TEXT NOT NULL DEFAULT '',
                    file_id         TEXT NOT NULL DEFAULT '',
                    file_unique_id  TEXT NOT NULL DEFAULT '',
                    created_at      TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_image_logs_user_id
                ON bot_image_logs (user_id, created_at DESC)
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS autopub_posts (
                    id              SERIAL PRIMARY KEY,
                    topic           TEXT NOT NULL DEFAULT '',
                    caption         TEXT NOT NULL DEFAULT '',
                    prompt          TEXT NOT NULL DEFAULT '',
                    tg_file_id      TEXT NOT NULL DEFAULT '',
                    tg_file_unique  TEXT NOT NULL DEFAULT '',
                    status          TEXT NOT NULL DEFAULT 'draft',
                    tg_msg_id       BIGINT,
                    vk_post_id      BIGINT,
                    error_text      TEXT DEFAULT '',
                    created_at      TIMESTAMP DEFAULT NOW(),
                    published_at    TIMESTAMP,
                    source_trend    TEXT DEFAULT '',
                    admin_comment   TEXT DEFAULT ''
                )
            """)
            cur.execute("ALTER TABLE autopub_posts ADD COLUMN IF NOT EXISTS source_trend TEXT DEFAULT ''")
            cur.execute("ALTER TABLE autopub_posts ADD COLUMN IF NOT EXISTS admin_comment TEXT DEFAULT ''")
            cur.execute("ALTER TABLE autopub_posts ADD COLUMN IF NOT EXISTS extra_file_ids TEXT DEFAULT ''")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_sa_files (
                    id          SERIAL PRIMARY KEY,
                    name        TEXT UNIQUE NOT NULL,
                    content     TEXT NOT NULL,
                    project_id  TEXT,
                    client_email TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_credit_history (
                    id              SERIAL PRIMARY KEY,
                    user_id         BIGINT NOT NULL,
                    change_type     TEXT NOT NULL DEFAULT 'spend',
                    credits_change  INT NOT NULL,
                    balance_after   INT NOT NULL DEFAULT 0,
                    model           TEXT NOT NULL DEFAULT '',
                    gen_type        TEXT NOT NULL DEFAULT '',
                    prompt          TEXT NOT NULL DEFAULT '',
                    platform        TEXT NOT NULL DEFAULT '',
                    note            TEXT NOT NULL DEFAULT '',
                    created_at      TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_credit_history_user
                ON bot_credit_history (user_id, created_at DESC)
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_key_history (
                    id          SERIAL PRIMARY KEY,
                    slot_index  INT NOT NULL,
                    slot_label  TEXT NOT NULL DEFAULT '',
                    ts          TEXT NOT NULL,
                    user_id     BIGINT,
                    username    TEXT NOT NULL DEFAULT '',
                    prompt      TEXT NOT NULL DEFAULT '',
                    model       TEXT NOT NULL DEFAULT '',
                    status      TEXT NOT NULL DEFAULT '',
                    error       TEXT NOT NULL DEFAULT '',
                    duration_ms INT NOT NULL DEFAULT 0,
                    created_at  TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_key_history_slot
                ON bot_key_history (slot_index, created_at DESC)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_key_history_label
                ON bot_key_history (slot_label, created_at DESC)
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS autopub_settings (
                    id              INT PRIMARY KEY DEFAULT 1,
                    enabled         BOOLEAN NOT NULL DEFAULT FALSE,
                    tg_channel_id   TEXT NOT NULL DEFAULT '',
                    vk_group_id     TEXT NOT NULL DEFAULT '',
                    posts_per_day   INT NOT NULL DEFAULT 3,
                    auto_approve    BOOLEAN NOT NULL DEFAULT FALSE,
                    topic_hints     TEXT NOT NULL DEFAULT '',
                    post_template   TEXT NOT NULL DEFAULT '',
                    post_cta        TEXT NOT NULL DEFAULT '',
                    bot_username    TEXT NOT NULL DEFAULT '',
                    image_style     TEXT NOT NULL DEFAULT ''
                )
            """)
            # Ensure one settings row always exists
            cur.execute("""
                INSERT INTO autopub_settings (id) VALUES (1)
                ON CONFLICT (id) DO NOTHING
            """)
            # ── Broadcasts (mass mailing campaigns) ──────────────────────
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_broadcasts (
                    id               SERIAL PRIMARY KEY,
                    title            TEXT NOT NULL DEFAULT '',
                    status           TEXT NOT NULL DEFAULT 'draft',
                    text             TEXT NOT NULL DEFAULT '',
                    parse_mode       TEXT NOT NULL DEFAULT 'HTML',
                    media_type       TEXT NOT NULL DEFAULT 'none',
                    media_path       TEXT NOT NULL DEFAULT '',
                    media_url        TEXT NOT NULL DEFAULT '',
                    media_tg_file_id TEXT NOT NULL DEFAULT '',
                    media_vk_attach  TEXT NOT NULL DEFAULT '',
                    buttons_json     TEXT NOT NULL DEFAULT '[]',
                    disable_preview  BOOLEAN NOT NULL DEFAULT FALSE,
                    silent           BOOLEAN NOT NULL DEFAULT FALSE,
                    protect_content  BOOLEAN NOT NULL DEFAULT FALSE,
                    pin              BOOLEAN NOT NULL DEFAULT FALSE,
                    personalize      BOOLEAN NOT NULL DEFAULT FALSE,
                    target_platform  TEXT NOT NULL DEFAULT 'all',
                    target_filter    TEXT NOT NULL DEFAULT '{}',
                    scheduled_at     TIMESTAMP,
                    rate_per_sec     INT NOT NULL DEFAULT 20,
                    total_recipients INT NOT NULL DEFAULT 0,
                    sent_count       INT NOT NULL DEFAULT 0,
                    failed_count     INT NOT NULL DEFAULT 0,
                    blocked_count    INT NOT NULL DEFAULT 0,
                    skipped_count    INT NOT NULL DEFAULT 0,
                    clicked_count    INT NOT NULL DEFAULT 0,
                    ab_variant       TEXT NOT NULL DEFAULT '',
                    ab_parent_id     INT,
                    ab_split_pct     INT NOT NULL DEFAULT 50,
                    notes            TEXT NOT NULL DEFAULT '',
                    created_at       TIMESTAMP DEFAULT NOW(),
                    started_at       TIMESTAMP,
                    finished_at      TIMESTAMP,
                    created_by       BIGINT
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_broadcasts_status
                ON bot_broadcasts (status, scheduled_at)
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_broadcast_recipients (
                    id            SERIAL PRIMARY KEY,
                    broadcast_id  INT NOT NULL REFERENCES bot_broadcasts(id) ON DELETE CASCADE,
                    user_id       BIGINT NOT NULL,
                    platform      TEXT NOT NULL DEFAULT 'tg',
                    status        TEXT NOT NULL DEFAULT 'queued',
                    error_text    TEXT NOT NULL DEFAULT '',
                    attempted_at  TIMESTAMP,
                    sent_at       TIMESTAMP,
                    clicks        INT NOT NULL DEFAULT 0
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_recipients_broadcast
                ON bot_broadcast_recipients (broadcast_id, status)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_recipients_queue
                ON bot_broadcast_recipients (broadcast_id, status, id)
            """)
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_recipients_user
                ON bot_broadcast_recipients (broadcast_id, user_id, platform)
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_broadcast_clicks (
                    id            SERIAL PRIMARY KEY,
                    broadcast_id  INT NOT NULL,
                    user_id       BIGINT NOT NULL,
                    platform      TEXT NOT NULL DEFAULT 'tg',
                    button_idx    INT NOT NULL DEFAULT 0,
                    url           TEXT NOT NULL DEFAULT '',
                    clicked_at    TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_bclicks_bcast
                ON bot_broadcast_clicks (broadcast_id, clicked_at DESC)
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_broadcast_templates (
                    id           SERIAL PRIMARY KEY,
                    name         TEXT NOT NULL DEFAULT '',
                    payload      TEXT NOT NULL DEFAULT '{}',
                    created_at   TIMESTAMP DEFAULT NOW()
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


def load_one_user(user_id: int) -> dict[str, Any] | None:
    """Return settings dict for one user from DB, or None if not found."""
    if not _DATABASE_URL:
        return None
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM bot_user_settings WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
        if row:
            return json.loads(row[0])
        return None
    except Exception:
        logger.exception("db: failed to load user %s", user_id)
        return None


def delete_one_user(user_id: int) -> None:
    """Delete a single user row from DB."""
    if not _DATABASE_URL:
        return
    with _write_lock:
        try:
            conn = _get_conn()
            with conn.cursor() as cur:
                cur.execute("DELETE FROM bot_user_settings WHERE user_id = %s", (user_id,))
        except Exception:
            logger.exception("db: failed to delete user %s", user_id)


def save_all_users(snapshot: dict[int, dict[str, Any]]) -> None:
    """Upsert all users in one transaction."""
    if not _DATABASE_URL:
        return
    with _write_lock:
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


def save_one_user(user_id: int, data: dict[str, Any]) -> None:
    """Upsert a single user — faster than save_all_users."""
    if not _DATABASE_URL:
        return
    with _write_lock:
        try:
            conn = _get_conn()
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO bot_user_settings (user_id, data)
                    VALUES (%s, %s)
                    ON CONFLICT (user_id) DO UPDATE SET data = EXCLUDED.data
                """, (user_id, json.dumps(data, ensure_ascii=False)))
        except Exception:
            logger.exception("db: failed to save user %s", user_id)


# ── API keys ───────────────────────────────────────────────────────────────────

def load_api_keys() -> list[dict]:
    if not _DATABASE_URL:
        return []
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT key, project_id FROM bot_api_keys ORDER BY id")
            return [{"key": row[0], "project_id": row[1]} for row in cur.fetchall()]
    except Exception:
        logger.exception("db: failed to load api keys")
        return []


def save_api_keys(keys: list[dict]) -> None:
    if not _DATABASE_URL:
        return
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM bot_api_keys")
            for entry in keys:
                if isinstance(entry, str):
                    entry = {"key": entry, "project_id": None}
                cur.execute(
                    "INSERT INTO bot_api_keys (key, project_id) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET project_id = EXCLUDED.project_id",
                    (entry["key"], entry.get("project_id"))
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


# ── Image logs ─────────────────────────────────────────────────────────────────

def save_image_log(
    user_id: int,
    user_name: str,
    platform: str,
    prompt: str,
    model: str,
    file_id: str,
    file_unique_id: str,
) -> None:
    if not _DATABASE_URL:
        return
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bot_image_logs
                    (user_id, user_name, platform, prompt, model, file_id, file_unique_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (user_id, user_name, platform, prompt[:500], model, file_id, file_unique_id))
    except Exception:
        logger.exception("db: failed to save image log for user %s", user_id)


def get_user_image_logs(user_id: int, limit: int = 50, offset: int = 0) -> list[dict]:
    """Return recent image generations for a user, newest first."""
    if not _DATABASE_URL:
        return []
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, platform, prompt, model, file_id, file_unique_id, created_at
                FROM bot_image_logs
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
            """, (user_id, limit, offset))
            rows = cur.fetchall()
        return [
            {
                "id": r[0],
                "platform": r[1],
                "prompt": r[2],
                "model": r[3],
                "file_id": r[4],
                "file_unique_id": r[5],
                "created_at": r[6].isoformat() if r[6] else "",
            }
            for r in rows
        ]
    except Exception:
        logger.exception("db: failed to get image logs for user %s", user_id)
        return []


def count_user_image_logs(user_id: int) -> int:
    """Return total number of image generations for a user."""
    if not _DATABASE_URL:
        return 0
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM bot_image_logs WHERE user_id = %s",
                (user_id,),
            )
            return cur.fetchone()[0]
    except Exception:
        logger.exception("db: failed to count image logs for user %s", user_id)
        return 0


def get_all_image_logs(limit: int = 200) -> list[dict]:
    """Return recent image generations across all users, newest first."""
    if not _DATABASE_URL:
        return []
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, user_id, user_name, platform, prompt, model, file_id, file_unique_id, created_at
                FROM bot_image_logs
                ORDER BY created_at DESC
                LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
        return [
            {
                "id": r[0],
                "user_id": r[1],
                "user_name": r[2],
                "platform": r[3],
                "prompt": r[4],
                "model": r[5],
                "file_id": r[6],
                "file_unique_id": r[7],
                "created_at": r[8].isoformat() if r[8] else "",
            }
            for r in rows
        ]
    except Exception:
        logger.exception("db: failed to get all image logs")
        return []


def get_image_log_by_unique_id(file_unique_id: str) -> dict | None:
    """Return a single image log row by file_unique_id.
    Falls back to autopub_posts if not found in bot_image_logs."""
    if not _DATABASE_URL:
        return None
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT file_id, file_unique_id, user_id, user_name, prompt, model, platform
                FROM bot_image_logs
                WHERE file_unique_id = %s
                LIMIT 1
            """, (file_unique_id,))
            row = cur.fetchone()
        if row:
            return {
                "file_id": row[0], "file_unique_id": row[1],
                "user_id": row[2], "user_name": row[3],
                "prompt": row[4], "model": row[5], "platform": row[6],
            }
    except Exception:
        logger.exception("db: failed to get image log for unique_id %s", file_unique_id)
    # Fallback: look in autopub_posts
    return autopub_get_file_id_by_unique(file_unique_id)


def autopub_get_file_id_by_unique(file_unique_id: str) -> dict | None:
    """Look up tg_file_id from autopub_posts by tg_file_unique."""
    if not _DATABASE_URL:
        return None
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT tg_file_id, tg_file_unique
                FROM autopub_posts
                WHERE tg_file_unique = %s
                LIMIT 1
            """, (file_unique_id,))
            row = cur.fetchone()
        if row:
            return {"file_id": row[0], "file_unique_id": row[1]}
    except Exception:
        logger.exception("db: failed to get autopub file for unique_id %s", file_unique_id)
    return None


def get_image_log_stats() -> dict:
    """Total generation count from image_logs table."""
    if not _DATABASE_URL:
        return {"total": 0}
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM bot_image_logs")
            row = cur.fetchone()
        return {"total": row[0] or 0}
    except Exception:
        return {"total": 0}


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


# ── Credit history ─────────────────────────────────────────────────────────────

def save_credit_log(
    user_id: int,
    change_type: str,
    credits_change: int,
    balance_after: int,
    model: str = "",
    gen_type: str = "",
    prompt: str = "",
    platform: str = "",
    note: str = "",
) -> None:
    """Log a credit change (spend or top-up)."""
    if not _DATABASE_URL:
        return
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bot_credit_history
                    (user_id, change_type, credits_change, balance_after,
                     model, gen_type, prompt, platform, note)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (user_id, change_type, credits_change, balance_after,
                  model, gen_type, prompt[:300], platform, note[:200]))
    except Exception:
        logger.debug("db: failed to save credit log for user %d", user_id)


def get_user_credit_logs(user_id: int, limit: int = 100, offset: int = 0) -> list[dict]:
    """Return credit history for a user, newest first."""
    if not _DATABASE_URL:
        return []
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT change_type, credits_change, balance_after,
                       model, gen_type, prompt, platform, note, created_at
                FROM bot_credit_history
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
            """, (user_id, limit, offset))
            return [
                {
                    "change_type": r[0],
                    "credits_change": r[1],
                    "balance_after": r[2],
                    "model": r[3],
                    "gen_type": r[4],
                    "prompt": r[5],
                    "platform": r[6],
                    "note": r[7],
                    "created_at": r[8].isoformat() if r[8] else "",
                }
                for r in cur.fetchall()
            ]
    except Exception:
        logger.debug("db: failed to get credit logs for user %d", user_id)
        return []


def count_user_credit_logs(user_id: int) -> int:
    if not _DATABASE_URL:
        return 0
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM bot_credit_history WHERE user_id = %s", (user_id,))
            return cur.fetchone()[0]
    except Exception:
        return 0


# ── Key history ─────────────────────────────────────────────────────────────────

def save_key_history_entry(
    slot_index: int,
    slot_label: str,
    ts: str,
    user_id: int | None,
    username: str,
    prompt: str,
    model: str,
    status: str,
    error: str,
    duration_ms: int,
) -> None:
    """Insert one history entry and prune old entries (keep last 200 per slot)."""
    if not _DATABASE_URL:
        return
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bot_key_history
                    (slot_index, slot_label, ts, user_id, username, prompt, model, status, error, duration_ms)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (slot_index, slot_label, ts, user_id, username, prompt[:300], model, status, error[:500], duration_ms))
            # Keep last 200 rows per stable slot_label (so reordering keys
            # doesn't evict another key's history). Fall back to slot_index
            # pruning only when label is empty (legacy rows).
            if slot_label:
                cur.execute("""
                    DELETE FROM bot_key_history
                    WHERE slot_label = %s
                      AND id NOT IN (
                          SELECT id FROM bot_key_history
                          WHERE slot_label = %s
                          ORDER BY created_at DESC
                          LIMIT 200
                      )
                """, (slot_label, slot_label))
            else:
                cur.execute("""
                    DELETE FROM bot_key_history
                    WHERE slot_index = %s
                      AND (slot_label IS NULL OR slot_label = '')
                      AND id NOT IN (
                          SELECT id FROM bot_key_history
                          WHERE slot_index = %s
                            AND (slot_label IS NULL OR slot_label = '')
                          ORDER BY created_at DESC
                          LIMIT 200
                      )
                """, (slot_index, slot_index))
    except Exception:
        logger.debug("db: failed to save key history for slot %d", slot_index)


def load_key_history(slot_index: int, limit: int = 200) -> list[dict]:
    """Return history entries for a slot, newest first.
    Strict legacy-only lookup: only rows with NO slot_label are returned, so
    labeled rows belonging to other (replaced) keys aren't misattributed."""
    if not _DATABASE_URL:
        return []
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ts, user_id, username, prompt, model, status, error, duration_ms
                FROM bot_key_history
                WHERE slot_index = %s
                  AND (slot_label IS NULL OR slot_label = '')
                ORDER BY created_at DESC
                LIMIT %s
            """, (slot_index, limit))
            return [
                {
                    "ts": r[0], "user_id": r[1], "username": r[2],
                    "prompt": r[3], "model": r[4], "status": r[5],
                    "error": r[6], "duration_ms": r[7],
                }
                for r in cur.fetchall()
            ]
    except Exception:
        logger.debug("db: failed to load key history for slot %d", slot_index)
        return []


def load_key_history_by_label(slot_label: str, limit: int = 200) -> list[dict]:
    """Return history entries by stable slot_label (survives reordering of slots)."""
    if not _DATABASE_URL or not slot_label:
        return []
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ts, user_id, username, prompt, model, status, error, duration_ms
                FROM bot_key_history
                WHERE slot_label = %s
                ORDER BY created_at DESC
                LIMIT %s
            """, (slot_label, limit))
            return [
                {
                    "ts": r[0], "user_id": r[1], "username": r[2],
                    "prompt": r[3], "model": r[4], "status": r[5],
                    "error": r[6], "duration_ms": r[7],
                }
                for r in cur.fetchall()
            ]
    except Exception:
        logger.debug("db: failed to load key history by label '%s'", slot_label)
        return []


# ── Service Account JSON files ──────────────────────────────────────────────────

def load_sa_files() -> list[dict]:
    """Return list of SA files as [{name, content, project_id, client_email}]."""
    if not _DATABASE_URL:
        return []
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT name, content, project_id, client_email FROM bot_sa_files ORDER BY id")
            return [
                {"name": r[0], "content": r[1], "project_id": r[2], "client_email": r[3]}
                for r in cur.fetchall()
            ]
    except Exception:
        logger.exception("db: failed to load sa files")
        return []


def save_sa_file(name: str, content: str, project_id: str | None, client_email: str | None) -> bool:
    """Insert or replace a SA file record. Returns True on success."""
    if not _DATABASE_URL:
        return False
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bot_sa_files (name, content, project_id, client_email)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (name) DO UPDATE
                    SET content = EXCLUDED.content,
                        project_id = EXCLUDED.project_id,
                        client_email = EXCLUDED.client_email
            """, (name, content, project_id, client_email))
        return True
    except Exception:
        logger.exception("db: failed to save sa file %s", name)
        return False


def delete_sa_file(name: str) -> bool:
    """Delete a SA file record by name. Returns True if a row was deleted."""
    if not _DATABASE_URL:
        return False
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM bot_sa_files WHERE name = %s", (name,))
            return cur.rowcount > 0
    except Exception:
        logger.exception("db: failed to delete sa file %s", name)
        return False


# ── Autopub ────────────────────────────────────────────────────────────────────

def autopub_get_settings() -> dict:
    """Return autopub configuration (always a dict, never None)."""
    defaults = {
        "enabled": False, "tg_channel_id": "", "vk_group_id": "",
        "posts_per_day": 3, "auto_approve": False, "topic_hints": "",
        "post_template": "", "post_cta": "", "bot_username": "", "image_style": "",
    }
    if not _DATABASE_URL:
        return defaults
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT enabled,tg_channel_id,vk_group_id,posts_per_day,
                       auto_approve,topic_hints,post_template,post_cta,
                       bot_username,image_style
                FROM autopub_settings WHERE id=1
            """)
            r = cur.fetchone()
        if r:
            return {
                "enabled": bool(r[0]), "tg_channel_id": r[1] or "",
                "vk_group_id": r[2] or "", "posts_per_day": r[3] or 3,
                "auto_approve": bool(r[4]), "topic_hints": r[5] or "",
                "post_template": r[6] or "", "post_cta": r[7] or "",
                "bot_username": r[8] or "", "image_style": r[9] or "",
            }
    except Exception:
        logger.exception("db: failed to get autopub settings")
    return defaults


def autopub_save_settings(s: dict) -> None:
    if not _DATABASE_URL:
        return
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO autopub_settings
                    (id,enabled,tg_channel_id,vk_group_id,posts_per_day,
                     auto_approve,topic_hints,post_template,post_cta,bot_username,image_style)
                VALUES (1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET
                    enabled=EXCLUDED.enabled,
                    tg_channel_id=EXCLUDED.tg_channel_id,
                    vk_group_id=EXCLUDED.vk_group_id,
                    posts_per_day=EXCLUDED.posts_per_day,
                    auto_approve=EXCLUDED.auto_approve,
                    topic_hints=EXCLUDED.topic_hints,
                    post_template=EXCLUDED.post_template,
                    post_cta=EXCLUDED.post_cta,
                    bot_username=EXCLUDED.bot_username,
                    image_style=EXCLUDED.image_style
            """, (
                bool(s.get("enabled")), s.get("tg_channel_id",""),
                s.get("vk_group_id",""), int(s.get("posts_per_day",3)),
                bool(s.get("auto_approve")), s.get("topic_hints",""),
                s.get("post_template",""), s.get("post_cta",""),
                s.get("bot_username",""), s.get("image_style",""),
            ))
    except Exception:
        logger.exception("db: failed to save autopub settings")


def autopub_create_post(topic: str, caption: str, prompt: str,
                        tg_file_id: str, tg_file_unique: str,
                        status: str = "draft",
                        source_trend: str = "",
                        admin_comment: str = "",
                        extra_file_ids: str = "") -> int | None:
    """Insert a new autopub post, return its id."""
    if not _DATABASE_URL:
        return None
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO autopub_posts
                    (topic,caption,prompt,tg_file_id,tg_file_unique,status,source_trend,admin_comment,extra_file_ids)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, (topic, caption, prompt, tg_file_id, tg_file_unique, status, source_trend, admin_comment, extra_file_ids))
            row = cur.fetchone()
        return row[0] if row else None
    except Exception:
        logger.exception("db: failed to create autopub post")
        return None


def autopub_get_posts(status: str | None = None, limit: int = 50) -> list[dict]:
    if not _DATABASE_URL:
        return []
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            if status:
                cur.execute("""
                    SELECT id,topic,caption,prompt,tg_file_id,tg_file_unique,
                           status,tg_msg_id,vk_post_id,error_text,created_at,published_at,
                           COALESCE(source_trend,'') AS source_trend,
                           COALESCE(admin_comment,'') AS admin_comment,
                           COALESCE(extra_file_ids,'') AS extra_file_ids
                    FROM autopub_posts WHERE status=%s
                    ORDER BY created_at DESC LIMIT %s
                """, (status, limit))
            else:
                cur.execute("""
                    SELECT id,topic,caption,prompt,tg_file_id,tg_file_unique,
                           status,tg_msg_id,vk_post_id,error_text,created_at,published_at,
                           COALESCE(source_trend,'') AS source_trend,
                           COALESCE(admin_comment,'') AS admin_comment,
                           COALESCE(extra_file_ids,'') AS extra_file_ids
                    FROM autopub_posts
                    ORDER BY created_at DESC LIMIT %s
                """, (limit,))
            rows = cur.fetchall()
        return [
            {
                "id": r[0], "topic": r[1], "caption": r[2], "prompt": r[3],
                "tg_file_id": r[4], "tg_file_unique": r[5], "status": r[6],
                "tg_msg_id": r[7], "vk_post_id": r[8], "error_text": r[9] or "",
                "created_at": r[10].isoformat() if r[10] else "",
                "published_at": r[11].isoformat() if r[11] else "",
                "source_trend": r[12] or "", "admin_comment": r[13] or "",
                "extra_file_ids": r[14] or "",
            }
            for r in rows
        ]
    except Exception:
        logger.exception("db: failed to get autopub posts")
        return []


def autopub_get_recent_topics(limit: int = 30) -> list[str]:
    """Return recent post topics to avoid repeats in generation."""
    if not _DATABASE_URL:
        return []
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT topic FROM autopub_posts
                ORDER BY created_at DESC LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
        return [r[0] for r in rows if r[0]]
    except Exception:
        logger.exception("db: failed to get recent topics")
        return []


def autopub_update_post(post_id: int, **fields) -> None:
    if not _DATABASE_URL or not fields:
        return
    allowed = {"topic","caption","prompt","tg_file_id","tg_file_unique",
               "status","tg_msg_id","vk_post_id","error_text","published_at",
               "source_trend","admin_comment","extra_file_ids"}
    safe = {k: v for k, v in fields.items() if k in allowed}
    if not safe:
        return
    try:
        conn = _get_conn()
        parts = ", ".join(f"{k}=%s" for k in safe)
        vals = list(safe.values()) + [post_id]
        with conn.cursor() as cur:
            cur.execute(f"UPDATE autopub_posts SET {parts} WHERE id=%s", vals)
    except Exception:
        logger.exception("db: failed to update autopub post %s", post_id)


def autopub_delete_post(post_id: int) -> None:
    if not _DATABASE_URL:
        return
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM autopub_posts WHERE id=%s", (post_id,))
    except Exception:
        logger.exception("db: failed to delete autopub post %s", post_id)


def autopub_count_published_today() -> int:
    """Count posts published since midnight Moscow time today."""
    if not _DATABASE_URL:
        return 0
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM autopub_posts
                WHERE status='published'
                AND published_at >= (NOW() AT TIME ZONE 'Europe/Moscow')::date
            """)
            row = cur.fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


# ── Broadcasts ─────────────────────────────────────────────────────────────────

_BROADCAST_FIELDS = (
    "id, title, status, text, parse_mode, media_type, media_path, media_url, "
    "media_tg_file_id, media_vk_attach, buttons_json, disable_preview, silent, "
    "protect_content, pin, personalize, target_platform, target_filter, "
    "scheduled_at, rate_per_sec, total_recipients, sent_count, failed_count, "
    "blocked_count, skipped_count, clicked_count, ab_variant, ab_parent_id, "
    "ab_split_pct, notes, created_at, started_at, finished_at, created_by"
)
_BROADCAST_KEYS = [k.strip() for k in _BROADCAST_FIELDS.split(",")]


def _row_to_broadcast(row) -> dict:
    if not row:
        return {}
    d: dict[str, Any] = {}
    for i, k in enumerate(_BROADCAST_KEYS):
        v = row[i]
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        d[k] = v
    return d


def broadcast_create(data: dict) -> int | None:
    """Insert a new broadcast row. Returns new id."""
    if not _DATABASE_URL:
        return None
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bot_broadcasts
                    (title, status, text, parse_mode, media_type, media_path,
                     media_url, media_tg_file_id, media_vk_attach, buttons_json,
                     disable_preview, silent, protect_content, pin, personalize,
                     target_platform, target_filter, scheduled_at, rate_per_sec,
                     ab_variant, ab_parent_id, ab_split_pct, notes, created_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, (
                data.get("title", ""),
                data.get("status", "draft"),
                data.get("text", ""),
                data.get("parse_mode", "HTML"),
                data.get("media_type", "none"),
                data.get("media_path", ""),
                data.get("media_url", ""),
                data.get("media_tg_file_id", ""),
                data.get("media_vk_attach", ""),
                json.dumps(data.get("buttons", []), ensure_ascii=False),
                bool(data.get("disable_preview", False)),
                bool(data.get("silent", False)),
                bool(data.get("protect_content", False)),
                bool(data.get("pin", False)),
                bool(data.get("personalize", False)),
                data.get("target_platform", "all"),
                json.dumps(data.get("target_filter", {}), ensure_ascii=False),
                data.get("scheduled_at"),
                int(data.get("rate_per_sec", 20)),
                data.get("ab_variant", ""),
                data.get("ab_parent_id"),
                int(data.get("ab_split_pct", 50)),
                data.get("notes", ""),
                data.get("created_by"),
            ))
            row = cur.fetchone()
        return int(row[0]) if row else None
    except Exception:
        logger.exception("db: failed to create broadcast")
        return None


def broadcast_update(bid: int, data: dict) -> bool:
    """Partial update by keys present in data. Returns True on success."""
    if not _DATABASE_URL or not data:
        return False
    allowed = {
        "title", "status", "text", "parse_mode", "media_type", "media_path",
        "media_url", "media_tg_file_id", "media_vk_attach", "buttons_json",
        "disable_preview", "silent", "protect_content", "pin", "personalize",
        "target_platform", "target_filter", "scheduled_at", "rate_per_sec",
        "total_recipients", "sent_count", "failed_count", "blocked_count",
        "skipped_count", "clicked_count", "ab_variant", "ab_parent_id",
        "ab_split_pct", "notes", "started_at", "finished_at",
    }
    sets, vals = [], []
    for k, v in data.items():
        if k not in allowed:
            continue
        sets.append(f"{k}=%s")
        vals.append(v)
    if not sets:
        return False
    vals.append(bid)
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE bot_broadcasts SET {', '.join(sets)} WHERE id=%s",
                tuple(vals),
            )
            return cur.rowcount > 0
    except Exception:
        logger.exception("db: failed to update broadcast %s", bid)
        return False


def broadcast_inc(bid: int, field: str, delta: int = 1) -> None:
    """Atomic counter increment (sent/failed/blocked/skipped/clicked)."""
    if not _DATABASE_URL:
        return
    if field not in {"sent_count", "failed_count", "blocked_count",
                     "skipped_count", "clicked_count"}:
        return
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE bot_broadcasts SET {field}={field}+%s WHERE id=%s",
                (delta, bid),
            )
    except Exception:
        logger.exception("db: failed to increment broadcast %s.%s", bid, field)


def broadcast_get(bid: int) -> dict:
    if not _DATABASE_URL:
        return {}
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_BROADCAST_FIELDS} FROM bot_broadcasts WHERE id=%s",
                (bid,),
            )
            row = cur.fetchone()
        return _row_to_broadcast(row)
    except Exception:
        logger.exception("db: failed to load broadcast %s", bid)
        return {}


def broadcast_list(status: str | None = None, limit: int = 200) -> list[dict]:
    if not _DATABASE_URL:
        return []
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            if status:
                cur.execute(
                    f"SELECT {_BROADCAST_FIELDS} FROM bot_broadcasts "
                    f"WHERE status=%s ORDER BY id DESC LIMIT %s",
                    (status, limit),
                )
            else:
                cur.execute(
                    f"SELECT {_BROADCAST_FIELDS} FROM bot_broadcasts "
                    f"ORDER BY id DESC LIMIT %s",
                    (limit,),
                )
            rows = cur.fetchall()
        return [_row_to_broadcast(r) for r in rows]
    except Exception:
        logger.exception("db: failed to list broadcasts")
        return []


def broadcast_count_by_status() -> dict[str, int]:
    if not _DATABASE_URL:
        return {}
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, COUNT(*) FROM bot_broadcasts GROUP BY status"
            )
            rows = cur.fetchall()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}


def broadcast_due_for_send(now_utc=None) -> list[dict]:
    """Return `scheduled` broadcasts whose time has come.
    Orphaned `sending` rows from a prior process are converted back to
    `scheduled` once at scheduler startup via `broadcast_recover_orphan_sending`,
    so this query stays strictly aligned with the CAS in `broadcast_claim_for_send`."""
    if not _DATABASE_URL:
        return []
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_BROADCAST_FIELDS} FROM bot_broadcasts "
                f"WHERE status='scheduled' "
                f"AND (scheduled_at IS NULL OR scheduled_at <= NOW()) "
                f"ORDER BY id ASC LIMIT 10"
            )
            rows = cur.fetchall()
        return [_row_to_broadcast(r) for r in rows]
    except Exception:
        logger.exception("db: failed to fetch due broadcasts")
        return []


def broadcast_claim_for_send(bid: int) -> bool:
    """True compare-and-swap: `scheduled` → `sending`. Returns True only for
    the single caller that wins the transition. Orphan `sending` rows from a
    crashed prior run must first be recovered by `broadcast_recover_orphan_sending`."""
    if not _DATABASE_URL:
        return False
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bot_broadcasts SET status='sending' "
                "WHERE id=%s AND status='scheduled' "
                "RETURNING id",
                (bid,),
            )
            return cur.fetchone() is not None
    except Exception:
        logger.exception("db: failed to claim broadcast %s", bid)
        return False


def broadcast_recover_orphan_sending() -> int:
    """One-shot recovery (single-process scheduler): move any `sending`
    broadcasts back to `scheduled` so the claim loop can pick them up.
    Call once at scheduler startup before entering the main tick loop."""
    if not _DATABASE_URL:
        return 0
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bot_broadcasts SET status='scheduled' "
                "WHERE status='sending'"
            )
            return cur.rowcount or 0
    except Exception:
        logger.exception("db: failed to recover orphan sending broadcasts")
        return 0


def broadcast_update_if_status(bid: int, expected: tuple[str, ...],
                                data: dict) -> bool:
    """Conditional update: applies `data` only if current status ∈ `expected`.
    Returns True on success."""
    if not _DATABASE_URL or not data or not expected:
        return False
    allowed = {
        "title", "status", "text", "parse_mode", "media_type", "media_path",
        "media_url", "media_tg_file_id", "media_vk_attach", "buttons_json",
        "disable_preview", "silent", "protect_content", "pin", "personalize",
        "target_platform", "target_filter", "scheduled_at", "rate_per_sec",
        "total_recipients", "sent_count", "failed_count", "blocked_count",
        "skipped_count", "clicked_count", "ab_variant", "ab_parent_id",
        "ab_split_pct", "notes", "started_at", "finished_at",
    }
    sets, vals = [], []
    for k, v in data.items():
        if k not in allowed:
            continue
        sets.append(f"{k}=%s")
        vals.append(v)
    if not sets:
        return False
    placeholders = ",".join(["%s"] * len(expected))
    vals.extend([bid, *expected])
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE bot_broadcasts SET {', '.join(sets)} "
                f"WHERE id=%s AND status IN ({placeholders})",
                tuple(vals),
            )
            return cur.rowcount > 0
    except Exception:
        logger.exception("db: failed conditional update broadcast %s", bid)
        return False


def broadcast_delete(bid: int) -> None:
    if not _DATABASE_URL:
        return
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM bot_broadcasts WHERE id=%s", (bid,))
    except Exception:
        logger.exception("db: failed to delete broadcast %s", bid)


def broadcast_recipients_bulk_insert(bid: int, rows: list[tuple[int, str]]) -> int:
    """Insert (user_id, platform) pairs. Returns inserted count."""
    if not _DATABASE_URL or not rows:
        return 0
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            from psycopg2.extras import execute_values
            execute_values(
                cur,
                "INSERT INTO bot_broadcast_recipients "
                "(broadcast_id, user_id, platform) VALUES %s "
                "ON CONFLICT DO NOTHING",
                [(bid, uid, plat) for uid, plat in rows],
            )
            return cur.rowcount or 0
    except Exception:
        logger.exception("db: failed bulk insert recipients for %s", bid)
        return 0


def broadcast_recipients_count(bid: int) -> int:
    if not _DATABASE_URL:
        return 0
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM bot_broadcast_recipients WHERE broadcast_id=%s",
                (bid,),
            )
            r = cur.fetchone()
        return int(r[0]) if r else 0
    except Exception:
        return 0


def broadcast_next_queued(bid: int, batch: int = 50) -> list[dict]:
    """[Deprecated] Non-locking peek — use `broadcast_claim_recipients`."""
    if not _DATABASE_URL:
        return []
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, user_id, platform FROM bot_broadcast_recipients "
                "WHERE broadcast_id=%s AND status='queued' "
                "ORDER BY id ASC LIMIT %s",
                (bid, batch),
            )
            rows = cur.fetchall()
        return [{"id": r[0], "user_id": r[1], "platform": r[2]} for r in rows]
    except Exception:
        logger.exception("db: failed to fetch queued recipients for %s", bid)
        return []


def broadcast_claim_recipients(bid: int, batch: int = 20) -> list[dict]:
    """Atomically claim a batch of `queued` recipients → `sending` using
    `FOR UPDATE SKIP LOCKED`. Safe for concurrent workers."""
    if not _DATABASE_URL:
        return []
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bot_broadcast_recipients "
                "SET status='sending', attempted_at=NOW() "
                "WHERE id IN ("
                "  SELECT id FROM bot_broadcast_recipients "
                "  WHERE broadcast_id=%s AND status='queued' "
                "  ORDER BY id ASC LIMIT %s FOR UPDATE SKIP LOCKED"
                ") RETURNING id, user_id, platform",
                (bid, batch),
            )
            rows = cur.fetchall()
        return [{"id": r[0], "user_id": r[1], "platform": r[2]} for r in rows]
    except Exception:
        logger.exception("db: failed to claim recipients for %s", bid)
        return []


def broadcast_recipients_recover_stale(bid: int) -> int:
    """Reset any `sending` recipients (orphaned by previous process crash)
    back to `queued`. Call once at the start of each broadcast task."""
    if not _DATABASE_URL:
        return 0
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bot_broadcast_recipients SET status='queued' "
                "WHERE broadcast_id=%s AND status='sending'",
                (bid,),
            )
            return cur.rowcount or 0
    except Exception:
        logger.exception("db: failed to recover stale recipients for %s", bid)
        return 0


def broadcast_recipient_set_status(rid: int, status: str, error_text: str = "") -> None:
    if not _DATABASE_URL:
        return
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            if status == "sent":
                cur.execute(
                    "UPDATE bot_broadcast_recipients "
                    "SET status=%s, attempted_at=NOW(), sent_at=NOW(), error_text='' "
                    "WHERE id=%s",
                    (status, rid),
                )
            elif status == "queued":
                # Rollback (e.g. cancel/pause caught a claimed but unsent row)
                cur.execute(
                    "UPDATE bot_broadcast_recipients "
                    "SET status='queued', attempted_at=NULL, error_text='' "
                    "WHERE id=%s",
                    (rid,),
                )
            else:
                cur.execute(
                    "UPDATE bot_broadcast_recipients "
                    "SET status=%s, attempted_at=NOW(), error_text=%s "
                    "WHERE id=%s",
                    (status, error_text[:500], rid),
                )
    except Exception:
        logger.exception("db: failed to update recipient %s", rid)


def broadcast_recipients_count_status(bid: int, status: str) -> int:
    """Count recipients of a broadcast with a specific status."""
    if not _DATABASE_URL:
        return 0
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM bot_broadcast_recipients "
                "WHERE broadcast_id=%s AND status=%s",
                (bid, status),
            )
            r = cur.fetchone()
        return int(r[0]) if r else 0
    except Exception:
        return 0


def broadcast_recipients_summary(bid: int) -> dict[str, int]:
    if not _DATABASE_URL:
        return {}
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, COUNT(*) FROM bot_broadcast_recipients "
                "WHERE broadcast_id=%s GROUP BY status",
                (bid,),
            )
            rows = cur.fetchall()
        return {r[0]: int(r[1]) for r in rows}
    except Exception:
        return {}


def broadcast_recipients_page(
    bid: int, status: str = "", limit: int = 100, offset: int = 0,
) -> list[dict]:
    if not _DATABASE_URL:
        return []
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            if status:
                cur.execute(
                    "SELECT id, user_id, platform, status, error_text, "
                    "attempted_at, sent_at, clicks "
                    "FROM bot_broadcast_recipients "
                    "WHERE broadcast_id=%s AND status=%s "
                    "ORDER BY id DESC LIMIT %s OFFSET %s",
                    (bid, status, limit, offset),
                )
            else:
                cur.execute(
                    "SELECT id, user_id, platform, status, error_text, "
                    "attempted_at, sent_at, clicks "
                    "FROM bot_broadcast_recipients "
                    "WHERE broadcast_id=%s "
                    "ORDER BY id DESC LIMIT %s OFFSET %s",
                    (bid, limit, offset),
                )
            rows = cur.fetchall()
        return [
            {
                "id": r[0], "user_id": r[1], "platform": r[2], "status": r[3],
                "error_text": r[4],
                "attempted_at": r[5].isoformat() if r[5] else "",
                "sent_at": r[6].isoformat() if r[6] else "",
                "clicks": r[7],
            }
            for r in rows
        ]
    except Exception:
        logger.exception("db: failed to load recipients page for %s", bid)
        return []


def broadcast_log_click(bid: int, uid: int, platform: str,
                        button_idx: int, url: str) -> None:
    if not _DATABASE_URL:
        return
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO bot_broadcast_clicks "
                "(broadcast_id, user_id, platform, button_idx, url) "
                "VALUES (%s,%s,%s,%s,%s)",
                (bid, uid, platform, button_idx, url[:500]),
            )
            cur.execute(
                "UPDATE bot_broadcast_recipients SET clicks=clicks+1 "
                "WHERE broadcast_id=%s AND user_id=%s AND platform=%s",
                (bid, uid, platform),
            )
            cur.execute(
                "UPDATE bot_broadcasts SET clicked_count=clicked_count+1 WHERE id=%s",
                (bid,),
            )
    except Exception:
        logger.exception("db: failed to log click bid=%s uid=%s", bid, uid)


def broadcast_recent_recipients_after(bid: int, after_id: int, limit: int = 50) -> list[dict]:
    """Return recipients with id > after_id (for live progress polling)."""
    if not _DATABASE_URL:
        return []
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, user_id, platform, status, error_text, attempted_at "
                "FROM bot_broadcast_recipients "
                "WHERE broadcast_id=%s AND id>%s AND status<>'queued' "
                "ORDER BY id ASC LIMIT %s",
                (bid, after_id, limit),
            )
            rows = cur.fetchall()
        return [
            {"id": r[0], "user_id": r[1], "platform": r[2], "status": r[3],
             "error_text": r[4],
             "attempted_at": r[5].isoformat() if r[5] else ""}
            for r in rows
        ]
    except Exception:
        return []


def broadcast_user_paid_set() -> set[int]:
    """Return set of user_ids that have at least one successful payment."""
    if not _DATABASE_URL:
        return set()
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT user_id FROM bot_payments WHERE status='success'"
            )
            rows = cur.fetchall()
        return {int(r[0]) for r in rows}
    except Exception:
        return set()


def broadcast_user_active_set(days: int) -> set[int]:
    """Return set of user_ids with activity in bot_image_logs in last N days."""
    if not _DATABASE_URL or days <= 0:
        return set()
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT user_id FROM bot_image_logs "
                "WHERE created_at >= NOW() - %s::interval",
                (f"{int(days)} days",),
            )
            rows = cur.fetchall()
        return {int(r[0]) for r in rows}
    except Exception:
        return set()


def broadcast_template_create(name: str, payload: dict) -> int | None:
    if not _DATABASE_URL:
        return None
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO bot_broadcast_templates (name, payload) "
                "VALUES (%s, %s) RETURNING id",
                (name, json.dumps(payload, ensure_ascii=False)),
            )
            row = cur.fetchone()
        return int(row[0]) if row else None
    except Exception:
        logger.exception("db: failed to create template")
        return None


def broadcast_template_list() -> list[dict]:
    if not _DATABASE_URL:
        return []
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, payload, created_at "
                "FROM bot_broadcast_templates ORDER BY id DESC"
            )
            rows = cur.fetchall()
        out = []
        for r in rows:
            try:
                payload = json.loads(r[2])
            except Exception:
                payload = {}
            out.append({
                "id": r[0], "name": r[1], "payload": payload,
                "created_at": r[3].isoformat() if r[3] else "",
            })
        return out
    except Exception:
        return []


def broadcast_template_delete(tid: int) -> None:
    if not _DATABASE_URL:
        return
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM bot_broadcast_templates WHERE id=%s", (tid,))
    except Exception:
        pass
