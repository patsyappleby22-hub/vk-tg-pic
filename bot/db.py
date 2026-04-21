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
