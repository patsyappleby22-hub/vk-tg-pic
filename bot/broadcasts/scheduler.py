"""
bot.broadcasts.scheduler
~~~~~~~~~~~~~~~~~~~~~~~~
Background loop that:
  - picks scheduled broadcasts whose time has come,
  - materializes the recipient list,
  - sends to each recipient with a per-broadcast rate limit,
  - honours pause/cancel mid-flight (status changes in DB).
"""
from __future__ import annotations

import asyncio
import logging

import bot.db as _db
from bot.broadcasts.sender import build_audience, send_one

logger = logging.getLogger(__name__)

TICK_SEC = 5
_active_tasks: dict[int, asyncio.Task] = {}


def is_running(bid: int) -> bool:
    t = _active_tasks.get(bid)
    return bool(t and not t.done())


async def broadcast_loop() -> None:
    """Top-level loop — schedules new broadcasts as their tasks."""
    # One-time crash/restart recovery: any broadcast left in `sending` from a
    # prior process has no active worker; flip it back to `scheduled` so the
    # claim loop below can re-acquire it cleanly. Single-process assumption.
    try:
        recovered = _db.broadcast_recover_orphan_sending()
        if recovered:
            logger.info("broadcasts: recovered %d orphan sending broadcasts",
                        recovered)
    except Exception:
        logger.exception("broadcasts: orphan recovery failed")

    logger.info("broadcasts: scheduler started (tick=%ds)", TICK_SEC)
    while True:
        try:
            due = _db.broadcast_due_for_send()
            for b in due:
                bid = int(b["id"])
                if is_running(bid):
                    continue
                # Atomic claim: only one process / coroutine wins the transition.
                if not _db.broadcast_claim_for_send(bid):
                    continue
                task = asyncio.create_task(_run_broadcast(bid),
                                           name=f"broadcast-{bid}")
                _active_tasks[bid] = task
            # Reap finished tasks
            for bid, t in list(_active_tasks.items()):
                if t.done():
                    _active_tasks.pop(bid, None)
        except Exception:
            logger.exception("broadcasts: scheduler tick failed")
        await asyncio.sleep(TICK_SEC)


async def _run_broadcast(bid: int) -> None:
    """Materialize recipients (if needed), then drain the queue."""
    try:
        b = _db.broadcast_get(bid)
        if not b:
            return

        # Materialize on first run (when no recipients yet)
        existing = _db.broadcast_recipients_count(bid)
        if existing == 0:
            audience = build_audience(b)
            inserted = _db.broadcast_recipients_bulk_insert(bid, audience)
            _db.broadcast_update(bid, {
                "total_recipients": inserted,
                "started_at": _now_sql(),
            })
            logger.info("broadcasts: bid=%s materialized %d recipients", bid, inserted)
            if inserted == 0:
                _db.broadcast_update(bid, {
                    "status": "completed",
                    "finished_at": _now_sql(),
                })
                return
        else:
            # Resume: ensure started_at is set
            if not b.get("started_at"):
                _db.broadcast_update(bid, {"started_at": _now_sql()})
            # Recover any recipients orphaned in `sending` from a prior crash
            recovered = _db.broadcast_recipients_recover_stale(bid)
            if recovered:
                logger.info("broadcasts: bid=%s recovered %d stale recipients",
                            bid, recovered)

        rate = max(1, int(b.get("rate_per_sec") or 20))
        delay = 1.0 / rate

        while True:
            # Re-fetch broadcast to pick up status changes (pause/cancel)
            b = _db.broadcast_get(bid)
            if not b:
                return
            status = b.get("status")
            if status == "paused":
                logger.info("broadcasts: bid=%s paused — sleeping 10s", bid)
                await asyncio.sleep(10)
                continue
            if status in ("cancelled", "completed", "failed"):
                logger.info("broadcasts: bid=%s exit (status=%s)", bid, status)
                return

            # Atomic claim: queued → sending (FOR UPDATE SKIP LOCKED)
            batch = _db.broadcast_claim_recipients(bid, batch=20)
            if not batch:
                # Nothing left to claim. Confirm there are truly no queued rows
                # (could be concurrent worker mid-claim — give one more tick).
                if _db.broadcast_recipients_count_status(bid, "queued") == 0:
                    # Allow either 'sending' (steady-state) or 'scheduled'
                    # (resume-just-flipped) → 'completed'.
                    _db.broadcast_update_if_status(
                        bid, ("sending", "scheduled"),
                        {"status": "completed", "finished_at": _now_sql()},
                    )
                    logger.info("broadcasts: bid=%s done", bid)
                    return
                await asyncio.sleep(1.0)
                continue

            for rec in batch:
                # Re-check status before each send (so cancel is responsive)
                fresh = _db.broadcast_get(bid)
                if not fresh or fresh.get("status") in ("paused", "cancelled"):
                    # Roll back unsent claims back to queued
                    _db.broadcast_recipient_set_status(rec["id"], "queued", "")
                    continue
                status_str, err = await send_one(b, rec)
                _db.broadcast_recipient_set_status(rec["id"], status_str, err)
                counter = {
                    "sent": "sent_count",
                    "blocked": "blocked_count",
                    "failed": "failed_count",
                    "skipped": "skipped_count",
                }.get(status_str)
                if counter:
                    _db.broadcast_inc(bid, counter, 1)
                await asyncio.sleep(delay)

    except asyncio.CancelledError:
        logger.info("broadcasts: bid=%s task cancelled", bid)
        raise
    except Exception:
        logger.exception("broadcasts: bid=%s crashed", bid)
        try:
            _db.broadcast_update(bid, {
                "status": "failed",
                "finished_at": _now_sql(),
            })
        except Exception:
            pass


def _now_sql():
    """Return SQL NOW() compatible value (Python datetime in UTC)."""
    import datetime as _dt
    return _dt.datetime.utcnow()


# ── External controls ────────────────────────────────────────────────────────

async def send_test(b: dict, user_id: int, platform: str) -> tuple[str, str]:
    """Send a one-off test message right now (does not touch counters)."""
    from bot.broadcasts.sender import send_one
    return await send_one(b, {"user_id": user_id, "platform": platform})
