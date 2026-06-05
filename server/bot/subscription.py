"""Channel subscription gate logic.

Uses bot.get_chat_member. The bot MUST be an admin of CHANNEL_USERNAME for this
to work for arbitrary users. On API failure we alert admins (with a cooldown)
and surface a friendly message — we never crash.
"""
from __future__ import annotations

import logging
import time

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

import config

log = logging.getLogger(__name__)

_SUBSCRIBED_STATUSES = {"member", "administrator", "creator"}

# Sentinel: returned when the API itself failed (distinct from "not subscribed").
GATE_ERROR = "error"

# Re-alert admins at most once per cooldown window, so a *persistent* outage
# (e.g. bot lost channel-admin) keeps producing visibility instead of going
# silent forever after the first alert.
_ALERT_COOLDOWN = 3600.0
_last_alert_ts = 0.0


async def check_subscription(bot: Bot, user_id: int) -> bool | str:
    """Return True/False if known, or GATE_ERROR string on API failure."""
    if not config.CHANNEL_CHAT_ID:
        # No gate configured — treat everyone as subscribed.
        return True
    try:
        member = await bot.get_chat_member(config.CHANNEL_CHAT_ID, user_id)
    except TelegramAPIError as exc:
        log.error(
            "get_chat_member failed for channel=%s user=%s: %s",
            config.CHANNEL_CHAT_ID, user_id, exc,
        )
        await _alert_admins(bot, str(exc))
        return GATE_ERROR
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("Unexpected get_chat_member error: %s", exc)
        await _alert_admins(bot, str(exc))
        return GATE_ERROR
    # Successful check — clear the cooldown so the next outage alerts promptly.
    reset_alert()
    return member.status in _SUBSCRIBED_STATUSES


async def _alert_admins(bot: Bot, detail: str) -> None:
    global _last_alert_ts
    now = time.time()
    if now - _last_alert_ts < _ALERT_COOLDOWN:
        return
    _last_alert_ts = now
    text = (
        "⚠️ Не удаётся проверить подписку через get_chat_member.\n"
        f"Канал: {config.CHANNEL_USERNAME}\n"
        "Убедитесь, что бот добавлен АДМИНИСТРАТОРОМ канала.\n"
        f"Детали: {detail[:300]}"
    )
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception:  # pragma: no cover - defensive
            log.warning("Could not alert admin %s", admin_id)


def reset_alert() -> None:
    """Allow the next outage to re-alert immediately (called after success)."""
    global _last_alert_ts
    _last_alert_ts = 0.0
