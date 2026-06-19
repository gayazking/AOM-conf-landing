"""Lead push to the existing amoCRM backend + retry queue worker.

The backend at LEAD_API_URL already feeds amoCRM. We only POST JSON to it; on
failure we persist the payload to lead_queue and a background worker retries.
Nothing here ever raises into the bot's request path.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time

import aiohttp

import config
import db

log = logging.getLogger(__name__)

# Backoff schedule (seconds) indexed by attempt count, capped at the last value.
_BACKOFF = [60, 300, 900, 3600, 21600]

_PHONE_LIKE = re.compile(r"[\d][\d\-\s()+]{5,}\d")


def _redact_phone_like(text: str) -> str:
    """Mask long phone-like digit runs in arbitrary text before logging (PII)."""

    def _mask(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if len(digits) < 6:
            return m.group(0)
        return "***" + digits[-2:]

    return _PHONE_LIKE.sub(_mask, text)


def build_lead_payload(
    *, name: str, phone: str, city: str, username: str | None, tg_id: int
) -> dict:
    """Build the exact JSON contract the amoCRM backend expects.

    Includes a stable dedupe key (external_id / tg_id) so the backend can
    upsert instead of creating duplicate leads if registration is repeated.
    """
    uname = f"@{username}" if username else "(нет username)"
    return {
        "name": name,
        "phone": phone,
        "email": "",
        "city": city,
        "format": "Telegram-регистрация",
        "channel": "Telegram",
        "message": (
            f"Регистрация через @sadaosato_bot, tg {uname} id {tg_id}"
        ),
        "consent": True,
        "utm_source": "telegram-bot",
        "utm_medium": "bot",
        "page_url": "https://t.me/sadaosato_bot",
        # Telegram nick -> amo contact field "Telegram" (and local store).
        "username": username,
        "telegram_username": username,
        # Stable idempotency / dedupe keys for the backend & amoCRM.
        "tg_id": tg_id,
        "external_id": f"tgbot-{tg_id}",
    }


async def _post(session: aiohttp.ClientSession, payload: dict) -> bool:
    timeout = aiohttp.ClientTimeout(total=config.LEAD_TIMEOUT)
    try:
        async with session.post(
            config.LEAD_API_URL, json=payload, timeout=timeout
        ) as resp:
            if 200 <= resp.status < 300:
                log.info("Lead posted ok (status=%s)", resp.status)
                return True
            body = await resp.text()
            # Backend error bodies may echo submitted PII — redact before logging.
            log.warning(
                "Lead post failed status=%s body=%s",
                resp.status,
                _redact_phone_like(body[:300]),
            )
            return False
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        log.warning("Lead post error: %s", exc)
        return False


async def send_lead(tg_id: int, payload: dict) -> bool:
    """Try once; on failure enqueue for retry. Never raises."""
    try:
        async with aiohttp.ClientSession() as session:
            ok = await _post(session, payload)
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("Unexpected error sending lead: %s", exc)
        ok = False
    if not ok:
        try:
            await db.enqueue_lead(tg_id, json.dumps(payload, ensure_ascii=False))
            log.info("Lead for tg_id=%s queued for retry", tg_id)
        except Exception:  # pragma: no cover - defensive
            log.exception("Failed to enqueue lead for retry")
    return ok


async def retry_worker(stop: asyncio.Event, interval: float = 60.0) -> None:
    """Background loop draining lead_queue until `stop` is set."""
    log.info("Lead retry worker started (interval=%ss)", interval)
    while not stop.is_set():
        try:
            await _drain_once()
        except Exception:  # pragma: no cover - defensive
            log.exception("Lead retry worker iteration failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    log.info("Lead retry worker stopped")


async def _drain_once() -> None:
    due = await db.due_leads()
    if not due:
        return
    async with aiohttp.ClientSession() as session:
        for item in due:
            payload = json.loads(item["payload"])
            ok = await _post(session, payload)
            if ok:
                await db.delete_lead(item["id"])
            else:
                attempts = item["attempts"] + 1
                delay = _BACKOFF[min(attempts - 1, len(_BACKOFF) - 1)]
                await db.reschedule_lead(
                    item["id"], attempts, int(time.time()) + delay
                )
                log.info(
                    "Lead id=%s retry %s failed, next in %ss",
                    item["id"], attempts, delay,
                )
