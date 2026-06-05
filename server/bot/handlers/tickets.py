"""Bot side: manager marks paid -> ticket to buyer, + buyer /myticket.

/paid <phone|@username|tg_id|reg_id>  (ADMIN_IDS only): finds the registration
via the backend, asks the backend to issue the ticket, delivers QR+PDF to the
buyer's Telegram. /myticket: buyer re-fetches their own ticket.
"""
from __future__ import annotations

import base64
import logging

import aiohttp
from aiogram import Router, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, BufferedInputFile

import config

log = logging.getLogger(__name__)
router = Router(name="tickets")


def _base():
    url = getattr(config, "LEAD_API_URL", "http://127.0.0.1:8081/api/lead")
    return url.split("/api/")[0]


def _hdr():
    return {"X-Internal-Token": getattr(config, "INTERNAL_TOKEN", ""), "Content-Type": "application/json"}


async def _find(q):
    async with aiohttp.ClientSession() as s:
        async with s.get(_base() + "/api/reg/find", params={"q": q}, headers=_hdr(),
                         timeout=aiohttp.ClientTimeout(total=12)) as r:
            return await r.json()


async def _issue(reg_id):
    async with aiohttp.ClientSession() as s:
        async with s.post(_base() + "/api/issue_ticket", json={"reg_id": reg_id}, headers=_hdr(),
                          timeout=aiohttp.ClientTimeout(total=20)) as r:
            return await r.json()


async def _deliver(bot, tg_id, png_b64, pdf_b64, human_code, name, package):
    cap = "🎫 Ваш билет на саммит «Казань — Токио».\nКод: %s\nПредъявите QR на входе (вход однократный)." % human_code
    await bot.send_photo(tg_id, BufferedInputFile(base64.b64decode(png_b64), "ticket-qr.png"), caption=cap)
    await bot.send_document(tg_id, BufferedInputFile(base64.b64decode(pdf_b64), "ticket.pdf"))


@router.message(Command("paid"))
async def cmd_paid(message: Message, command: CommandObject, bot):
    if message.from_user.id not in getattr(config, "ADMIN_IDS", []):
        return
    q = (command.args or "").strip()
    if not q:
        await message.answer("Использование: /paid <телефон | @username | tg_id | reg_id>")
        return
    try:
        f = await _find(q)
    except Exception:
        await message.answer("Ошибка обращения к серверу. Попробуйте позже.")
        return
    if not f.get("found"):
        await message.answer("Регистрация не найдена по «%s»." % q)
        return
    r = f["reg"]
    try:
        t = await _issue(r["id"])
    except Exception:
        await message.answer("Не удалось выпустить билет (сервер).")
        return
    if not t.get("ok"):
        await message.answer("Ошибка выпуска билета: %s" % t.get("error"))
        return
    buyer_tg = r.get("telegram_user_id")
    if buyer_tg:
        try:
            await _deliver(bot, int(buyer_tg), t["png_b64"], t["pdf_b64"], t["human_code"],
                           r.get("full_name"), r.get("package"))
            await message.answer("✅ Оплата отмечена, билет отправлен клиенту %s (tg %s). Код: %s" %
                                 (r.get("full_name"), buyer_tg, t["human_code"]))
        except Exception as exc:
            log.warning("deliver to buyer failed: %s", exc)
            await message.answer("Билет выпущен (код %s), но не доставлен в Telegram клиента "
                                 "(возможно не писал боту). Отправьте вручную / на email." % t["human_code"])
            # also send the ticket to the manager so they can forward it
            try:
                await _deliver(bot, message.from_user.id, t["png_b64"], t["pdf_b64"], t["human_code"],
                               r.get("full_name"), r.get("package"))
            except Exception:
                pass
    else:
        await message.answer("Билет выпущен (код %s). У клиента нет Telegram — отправляю билет вам, перешлите/на email." % t["human_code"])
        try:
            await _deliver(bot, message.from_user.id, t["png_b64"], t["pdf_b64"], t["human_code"],
                           r.get("full_name"), r.get("package"))
        except Exception:
            pass


@router.message(Command("myticket"))
async def cmd_myticket(message: Message, bot):
    try:
        f = await _find(str(message.from_user.id))
    except Exception:
        await message.answer("Ошибка сервера, попробуйте позже.")
        return
    if not f.get("found"):
        await message.answer("Регистрация не найдена. Пройдите регистрацию через /start.")
        return
    r = f["reg"]
    if r.get("status") not in ("paid", "ticket_issued", "checked_in") and not r.get("ticket_id"):
        await message.answer("Оплата ещё не подтверждена. После подтверждения менеджером билет придёт автоматически.")
        return
    try:
        t = await _issue(r["id"])
        if t.get("ok"):
            await _deliver(bot, message.from_user.id, t["png_b64"], t["pdf_b64"], t["human_code"],
                           r.get("full_name"), r.get("package"))
        else:
            await message.answer("Не удалось получить билет: %s" % t.get("error"))
    except Exception:
        await message.answer("Ошибка выдачи билета, попробуйте позже.")
