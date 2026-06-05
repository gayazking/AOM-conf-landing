"""amoJo bridge (bot side).

Two pieces, both gated on config.AMOJO_ENABLED:
  * push_to_amojo(...)  — fire-and-forget POST of a client message to the
    backend loopback /amojo/outbound (the backend signs + imports it into the
    amoCRM deal chat). Same never-raise + redact pattern as lead.py.
  * run_internal_server(bot, stop) — a minimal aiohttp server on loopback
    exposing POST /internal/deliver {tg_id, text}, protected by INTERNAL_TOKEN,
    which the backend calls to deliver a manager's amoCRM reply to the client.
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging

import aiohttp
from aiohttp import web

import config
from lead import _redact_phone_like

log = logging.getLogger(__name__)

_OUTBOUND_TIMEOUT = 10.0


async def push_to_amojo(
    tg_id: int,
    *,
    name: str,
    username: str | None,
    phone: str | None,
    text: str,
    message_id: int | None = None,
) -> None:
    """Import a client→amoCRM inbound message. Never raises into the handler."""
    if not config.AMOJO_ENABLED:
        return
    payload = {
        "tg_id": tg_id,
        "conversation_id": f"tgbot-{tg_id}",
        "name": name or "Клиент",
        "username": username or "",
        "phone": phone or "",
        "text": text,
        "msgid": f"tg-{tg_id}-{message_id}" if message_id is not None else f"tg-{tg_id}",
    }
    headers = {"X-Internal-Token": config.INTERNAL_TOKEN}
    timeout = aiohttp.ClientTimeout(total=_OUTBOUND_TIMEOUT)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                config.AMOJO_OUTBOUND_URL,
                json=payload,
                headers=headers,
                timeout=timeout,
            ) as resp:
                if resp.status >= 300:
                    body = await resp.text()
                    log.warning(
                        "amojo outbound failed status=%s body=%s",
                        resp.status,
                        _redact_phone_like(body[:200]),
                    )
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        log.warning("amojo outbound error: %s", exc)
    except Exception:  # pragma: no cover - defensive
        log.exception("amojo outbound unexpected error")


def _check_token(request: web.Request) -> bool:
    got = request.headers.get("X-Internal-Token", "")
    if not config.INTERNAL_TOKEN:
        return False
    return hmac.compare_digest(got, config.INTERNAL_TOKEN)


def _make_app(bot) -> web.Application:
    app = web.Application()

    async def deliver(request: web.Request) -> web.Response:
        if not _check_token(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad_json"}, status=400)
        tg_id = data.get("tg_id")
        text = (data.get("text") or "").strip()
        if not tg_id or not text:
            return web.json_response({"ok": False, "error": "missing_fields"}, status=400)
        try:
            # parse_mode=None: the manager's amoCRM reply is arbitrary text and
            # may contain '<'/'&'; HTML parsing would 400 and lose the reply.
            await bot.send_message(int(tg_id), text, parse_mode=None)
        except Exception as exc:
            log.warning("internal deliver to %s failed: %s", tg_id, exc)
            return web.json_response({"ok": False, "error": "send_failed"}, status=502)
        # Record the manager's reply in AI memory so that, when the AI pause
        # auto-resumes, the model has context of what the human already said and
        # does not contradict the manager or re-ask answered questions.
        try:
            import ai_memory

            await ai_memory.append_item(
                int(tg_id),
                {"role": "assistant", "content": f"[менеджер] {text}"},
            )
        except Exception:  # pragma: no cover - best-effort context sync
            log.debug("could not append manager reply to AI memory for %s", tg_id)
        return web.json_response({"ok": True})

    app.router.add_post("/internal/deliver", deliver)
    return app


async def run_internal_server(bot, stop: asyncio.Event) -> None:
    """Run the loopback delivery server until `stop` is set.

    Mirrors lead.retry_worker lifecycle: started as a task in bot.main() and
    cancelled in the finally block. No-op when amoJo is disabled.
    """
    if not config.AMOJO_ENABLED:
        log.info("amojo disabled — internal delivery server not started")
        return
    if not config.INTERNAL_TOKEN:
        # Should not happen (AMOJO_ENABLED is gated on INTERNAL_TOKEN in config),
        # but make a misconfiguration loud instead of silently 401-ing every
        # manager->client delivery.
        log.error(
            "AMOJO_ENABLED but INTERNAL_TOKEN is empty — refusing to start the "
            "internal delivery server (all deliveries would be rejected)."
        )
        return
    host, port = config.internal_listen_host_port()
    app = _make_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    # Do NOT swallow CancelledError: let it propagate so the task finishes its
    # cancellation, but always clean up the runner (guarded so cleanup never
    # raises out of an already-cancelling task).
    try:
        await site.start()
        log.info("amojo internal delivery server listening on %s:%s", host, port)
        await stop.wait()
    finally:
        with contextlib.suppress(Exception):
            await runner.cleanup()
        log.info("amojo internal delivery server stopped")
