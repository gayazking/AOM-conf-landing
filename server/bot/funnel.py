"""Nurture funnel: APScheduler drip + boot re-arm + generic event reminder.

Jobs are persisted in db.scheduled_jobs (idempotent on job_id). On startup we
re-arm every pending job from the DB, so restarts never lose or duplicate sends.
A drip stops the moment the user pays (or marks paid_pending), checked at fire
time.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramAPIError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
import content
import db
import keyboards

log = logging.getLogger(__name__)

# kind -> delay_seconds from registration.
# Arc stretched for a high-ticket B2B purchase with a long consideration window:
# value -> authority -> scarcity (practice cap) -> dated last-call.
DAY = 24 * 60 * 60
DRIP: dict[str, tuple[int, str]] = {
    "funnel_1h": (2 * 60 * 60, content.FUNNEL_1H),   # +2h  value
    "funnel_1d": (2 * DAY, content.FUNNEL_1D),        # +2d  authority
    "funnel_3d": (5 * DAY, content.FUNNEL_3D),        # +5d  scarcity (practice cap)
    "funnel_7d": (12 * DAY, content.FUNNEL_7D),       # +12d last-call (dated)
}
DRIP_ORDER = ["funnel_1h", "funnel_1d", "funnel_3d", "funnel_7d"]

# Stages at which the drip must stay silent (already converting / converted).
_STOP_STAGES = {"paid", "paid_pending"}

_scheduler: AsyncIOScheduler | None = None
_bot: Bot | None = None


def setup(bot: Bot) -> AsyncIOScheduler:
    """Start the scheduler. MUST be called from inside the running event loop
    (it is, via dp.startup.register -> _on_startup)."""
    global _scheduler, _bot
    _bot = bot
    _scheduler = AsyncIOScheduler(
        event_loop=asyncio.get_running_loop(), timezone="UTC"
    )
    _scheduler.start()
    log.info("APScheduler started")
    return _scheduler


def shutdown() -> None:
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        log.info("APScheduler stopped")


def _drip_text(kind: str) -> str:
    """Resolve the drip body, injecting the dated early-bird line if set."""
    _, text = DRIP[kind]
    if kind == "funnel_7d":
        if config.EARLY_BIRD_DEADLINE:
            deadline_line = content.FUNNEL_7D_DEADLINE.format(
                deadline=config.EARLY_BIRD_DEADLINE
            )
        else:
            deadline_line = ""
        text = text.format(deadline_line=deadline_line)
    return text


async def schedule_drip_for_user(tg_id: int, base_ts: int | None = None) -> None:
    """Persist + arm the full drip for a freshly-registered user."""
    base = base_ts if base_ts is not None else int(time.time())
    for kind in DRIP_ORDER:
        delay, _ = DRIP[kind]
        run_at = base + delay
        job_id = f"{tg_id}:{kind}"
        await db.add_job(job_id, tg_id, kind, run_at)
        _arm_job(job_id, tg_id, kind, run_at)


def _arm_job(job_id: str, tg_id: int, kind: str, run_at: int) -> None:
    """Register the job with APScheduler (idempotent on job_id).

    If run_at is already in the past (e.g. re-armed after an outage longer than
    the misfire grace), schedule it for a few seconds from now so an extended
    downtime never silently drops a drip message.
    """
    assert _scheduler is not None
    now = time.time()
    if run_at <= now:
        run_dt = datetime.fromtimestamp(now + 5, tz=timezone.utc)
    else:
        run_dt = datetime.fromtimestamp(run_at, tz=timezone.utc)
    _scheduler.add_job(
        _fire_drip,
        trigger="date",
        run_date=run_dt,
        args=[job_id, tg_id, kind],
        id=job_id,
        replace_existing=True,
        # None = run regardless of how late, so a long outage still delivers.
        misfire_grace_time=None,
        coalesce=True,
    )


async def rearm_from_db() -> None:
    """Re-schedule all pending jobs after a restart."""
    jobs = await db.pending_jobs()
    count = 0
    for job in jobs:
        _arm_job(job["job_id"], job["tg_id"], job["kind"], job["run_at"])
        count += 1
    log.info("Re-armed %d pending funnel jobs from DB", count)
    schedule_event_reminder()


async def _fire_drip(job_id: str, tg_id: int, kind: str) -> None:
    """Executed by the scheduler. Sends one drip message if still eligible."""
    if _bot is None:
        return
    try:
        user = await db.get_user(tg_id)
        if not user:
            await db.mark_job_sent(job_id)
            return
        # Stop conditions: paid / paid_pending, inactive, not registered.
        if (
            user["paid"]
            or user["funnel_stage"] in _STOP_STAGES
            or not user["active"]
            or not user["registered"]
        ):
            await db.mark_job_sent(job_id)
            return
        # The +2h touch (funnel_1h) is an AI helper: the bot reaches out first,
        # asks if questions remain, gently nudges to payment, and the user's
        # reply is then handled by the live AI agent (answers + escalates to the
        # senior operator). Falls back to the static drip if AI is off/unavailable.
        if kind == "funnel_1h" and config.AI_ENABLED and config.AI_MODE in ("hybrid", "autopilot"):
            if await _send_ai_nudge(tg_id, user):
                await db.mark_job_sent(job_id)
                log.info("AI nudge (funnel_1h) sent to %s", tg_id)
                return
            # else fall through to the static drip below
        text = _drip_text(kind)
        await _bot.send_message(
            tg_id, text, reply_markup=keyboards.funnel_cta_kb(kind)
        )
        await db.mark_job_sent(job_id)
        log.info("Drip %s sent to %s", kind, tg_id)
    except TelegramForbiddenError:
        log.info("User %s blocked the bot — marking inactive", tg_id)
        await db.set_active(tg_id, False)
        await db.mark_job_sent(job_id)
    except TelegramAPIError as exc:
        log.warning("Drip %s to %s failed: %s", kind, tg_id, exc)
        await db.mark_job_sent(job_id)  # do not infinite-retry marketing msgs
    except Exception:  # pragma: no cover - defensive
        log.exception("Unexpected error firing drip %s for %s", kind, tg_id)


async def _send_ai_nudge(tg_id: int, user: dict) -> bool:
    """Proactive AI opener for the +2h unpaid touch. Returns True if sent.

    Skips (→ static fallback) if a manager already owns the chat (AI paused).
    Seeds the assistant opener into AI memory so the client's reply continues
    the same thread; mirrors the outgoing line into the amoCRM deal chat."""
    try:
        import ai
        import ai_memory

        if not await db.is_ai_active(tg_id):
            return False  # human took over — don't talk over them

        full_name = (user.get("name") or "").strip()
        first = full_name.split()[0] if full_name else ""
        situation = (
            "[ПРОАКТИВНОЕ СООБЩЕНИЕ — ты пишешь первой, клиент не отвечал]. "
            "Прошло ~2 часа после регистрации клиента в боте, оплаты пока нет. "
            "Напиши короткое тёплое сообщение (2–4 предложения, на «вы»): мягко "
            "спроси, остались ли вопросы и с чем помочь, ненавязчиво подведи к "
            "выбору пакета и оплате. Без давления, без вызова инструментов — только текст."
            + (f" Имя клиента: {first}." if first else "")
        )
        text, items = await ai.run_proactive(tg_id, situation)
        if not text:
            return False
        await _bot.send_message(
            tg_id, text, parse_mode=None,
            reply_markup=keyboards.funnel_cta_kb("funnel_1h"),
        )
        for it in items:
            try:
                await ai_memory.append_item(tg_id, it)
            except Exception:
                log.exception("persist nudge item failed for %s", tg_id)
        # mirror the outgoing nudge into the amoCRM deal chat (best-effort)
        try:
            import amojo_bridge

            await amojo_bridge.push_to_amojo(
                tg_id, name=full_name, username=user.get("username"),
                phone=user.get("phone"), text=f"[бот/ИИ] {text}", message_id=None,
            )
        except Exception:
            log.exception("amojo mirror (nudge) failed for %s", tg_id)
        return True
    except Exception:
        log.exception("AI nudge failed for %s", tg_id)
        return False


async def stop_drip(tg_id: int) -> None:
    """Cancel all pending drip jobs for a user (called when paid/paid_pending)."""
    await db.cancel_jobs_for_user(tg_id)
    if _scheduler is not None:
        for kind in DRIP_ORDER:
            job_id = f"{tg_id}:{kind}"
            try:
                _scheduler.remove_job(job_id)
            except Exception:
                pass  # job may already have fired / not exist
    log.info("Stopped drip for user %s", tg_id)


# --------------------------------------------------------------------------- #
# Generic pre-event reminder (optional, configurable date)
# --------------------------------------------------------------------------- #
def schedule_event_reminder() -> None:
    if not config.EVENT_DATE or _scheduler is None:
        return
    try:
        dt = datetime.fromisoformat(config.EVENT_DATE)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        log.error("EVENT_DATE %r is not valid ISO 8601 — reminder disabled",
                  config.EVENT_DATE)
        return
    if dt <= datetime.now(tz=timezone.utc):
        log.info("EVENT_DATE is in the past — reminder not scheduled")
        return
    _scheduler.add_job(
        _fire_event_reminder,
        trigger="date",
        run_date=dt,
        id="event_reminder",
        replace_existing=True,
        misfire_grace_time=6 * 60 * 60,
        coalesce=True,
    )
    log.info("Event reminder scheduled for %s", dt.isoformat())


async def _fire_event_reminder() -> None:
    if _bot is None:
        return
    users = await db.all_active_users()
    delay = 1.0 / max(config.SEND_RATE, 1.0)
    for user in users:
        try:
            await _bot.send_message(user["tg_id"], content.EVENT_REMINDER)
        except TelegramForbiddenError:
            await db.set_active(user["tg_id"], False)
        except TelegramAPIError as exc:
            log.warning("Event reminder to %s failed: %s", user["tg_id"], exc)
        await asyncio.sleep(delay)
