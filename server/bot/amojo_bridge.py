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

    async def deliver_ticket(request: web.Request) -> web.Response:
        """Backend asks the bot to push a buyer's ticket (QR + PDF) to Telegram.
        Body: {reg_id|tg_id, token}. Fetches the ticket from the backend (idempotent)."""
        if not _check_token(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad_json"}, status=400)
        reg_id = data.get("reg_id")
        tg_id = data.get("tg_id")
        if not reg_id and not tg_id:
            return web.json_response({"ok": False, "error": "missing"}, status=400)
        base = config.LEAD_API_URL.split("/api/")[0]
        hdr = {"X-Internal-Token": config.INTERNAL_TOKEN, "Content-Type": "application/json"}
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(base + "/api/reg/find", params={"q": str(reg_id or tg_id)},
                                 headers=hdr, timeout=aiohttp.ClientTimeout(total=12)) as r:
                    f = await r.json()
                if not f.get("found"):
                    return web.json_response({"ok": False, "error": "reg_not_found"}, status=404)
                reg = f["reg"]
                buyer = reg.get("telegram_user_id") or tg_id
                if not buyer:
                    return web.json_response({"ok": False, "error": "no_tg"})
                async with s.post(base + "/api/issue_ticket", json={"reg_id": reg["id"]},
                                  headers=hdr, timeout=aiohttp.ClientTimeout(total=25)) as r2:
                    t = await r2.json()
            if not t.get("ok"):
                return web.json_response({"ok": False, "error": "issue_failed"}, status=502)
            import base64
            from aiogram.types import BufferedInputFile

            cap = ("🎫 Ваш билет на саммит «Казань — Токио».\nКод: %s\n"
                   "Предъявите QR на входе (вход однократный)." % t.get("human_code"))
            await bot.send_photo(int(buyer), BufferedInputFile(
                base64.b64decode(t["png_b64"]), "ticket-qr.png"), caption=cap)
            await bot.send_document(int(buyer), BufferedInputFile(
                base64.b64decode(t["pdf_b64"]), "ticket.pdf"))
            return web.json_response({"ok": True})
        except Exception as exc:
            log.warning("internal deliver_ticket failed: %s", exc)
            return web.json_response({"ok": False, "error": "exception"}, status=502)

    app.router.add_post("/internal/deliver", deliver)
    app.router.add_post("/internal/deliver_ticket", deliver_ticket)
    return app


async def run_internal_server(bot, stop: asyncio.Event) -> None:
    """Run the loopback delivery server until `stop` is set.

    Mirrors lead.retry_worker lifecycle: started as a task in bot.main() and
    cancelled in the finally block. No-op when amoJo is disabled.
    """
    # The internal server now serves ticket delivery (always needed) AND the amoJo
    # chat bridge. Start it whenever INTERNAL_TOKEN is set, regardless of amoJo.
    if not config.INTERNAL_TOKEN:
        log.info("INTERNAL_TOKEN not set — internal server (ticket+chat) not started")
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
