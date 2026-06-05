"""Payment flow: choose package -> show external link -> 'Я оплатил' -> admin verify.

No card data is ever collected or stored — only external payment links.
"""
from __future__ import annotations

import base64
import logging

import aiohttp
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, Message

import config
import content
import db
import funnel
import keyboards

from . import common

log = logging.getLogger(__name__)
router = Router(name="payment")


async def _emit_amo(tg_id: int, event: str) -> None:
    """Best-effort: tell the backend to reflect a funnel event into amoCRM."""
    try:
        base = config.LEAD_API_URL.split("/api/")[0]
        async with aiohttp.ClientSession() as s:
            await s.post(
                base + "/api/amo/event",
                json={"tg_id": tg_id, "event": event},
                headers={"X-Internal-Token": config.INTERNAL_TOKEN, "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=8),
            )
    except Exception as exc:
        log.warning("amo emit %s failed: %s", event, exc)


@router.callback_query(F.data == keyboards.CB_PAY)
async def choose_package(cb: CallbackQuery) -> None:
    # Check registration FIRST so the nudge toast is the single answer
    # (a second cb.answer() on an already-answered query is silently dropped).
    if not await common.is_registered(cb.from_user.id):
        await common.safe_answer_callback(
            cb, "Сначала завершите регистрацию — /start"
        )
        return
    await common.safe_answer_callback(cb)
    await common.safe_edit_or_send(
        cb, content.PAY_CHOOSE, reply_markup=keyboards.packages_kb()
    )


@router.callback_query(F.data.startswith(keyboards.CB_PAY_PKG))
async def pay_package(cb: CallbackQuery) -> None:
    await common.safe_answer_callback(cb)
    if not await common.is_registered(cb.from_user.id):
        return
    key = cb.data[len(keyboards.CB_PAY_PKG):]
    pkg = content.PACKAGES.get(key)
    if not pkg:
        await common.safe_edit_or_send(
            cb, content.PAY_CHOOSE, reply_markup=keyboards.packages_kb()
        )
        return
    url = config.PAYMENT_URLS.get(key)
    if url:
        text = content.PAY_LINK.format(
            title=pkg["title"], price=pkg["price"], url=url
        )
    else:
        text = content.PAY_NO_LINK.format(title=pkg["title"], price=pkg["price"])
    await common.safe_edit_or_send(
        cb, text, reply_markup=keyboards.pay_actions_kb(key)
    )
    if url:
        await _emit_amo(cb.from_user.id, "sbp_shown")


@router.callback_query(F.data.startswith(keyboards.CB_I_PAID))
async def i_paid(cb: CallbackQuery, bot: Bot) -> None:
    user = cb.from_user
    if not await common.is_registered(user.id):
        await common.safe_answer_callback(
            cb, "Сначала завершите регистрацию — /start"
        )
        return
    await common.safe_answer_callback(cb, "Спасибо! 🙏")

    record = await db.get_user(user.id)
    stage = record["funnel_stage"] if record else ""
    # Debounce: if already paid / pending, acknowledge and DON'T re-ping admins.
    if record and (record["paid"] or stage in ("paid", "paid_pending")):
        await common.safe_edit_or_send(
            cb, content.PAID_PENDING_ALREADY, reply_markup=keyboards.back_kb()
        )
        return

    key = cb.data[len(keyboards.CB_I_PAID):]
    pkg = content.PACKAGES.get(key)
    pkg_title = pkg["title"] if pkg else key

    # Move to pending AND pause the drip immediately so a paying customer is
    # not nagged with scarcity/last-call during the verification window.
    await db.set_funnel_stage(user.id, "paid_pending")
    await funnel.stop_drip(user.id)
    await _emit_amo(user.id, "i_paid")

    await common.safe_edit_or_send(
        cb, content.PAID_PENDING_USER, reply_markup=keyboards.pay_actions_kb(key)
    )

    # Notify admins to verify with a one-tap confirm button.
    uname = f"@{user.username}" if user.username else "(нет username)"
    note = (
        "💰 <b>Отметка об оплате</b>\n"
        f"Пользователь: {uname} (id {user.id})\n"
        f"Пакет: {pkg_title}\n"
        "Проверьте оплату и подтвердите."
    )
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id, note, reply_markup=keyboards.admin_confirm_kb(user.id)
            )
        except Exception:
            log.warning("Could not notify admin %s about payment", admin_id)


@router.callback_query(F.data.startswith(keyboards.CB_ADMIN_CONFIRM))
async def admin_confirm_payment(cb: CallbackQuery, bot: Bot) -> None:
    if not common.is_admin(cb.from_user.id):
        await common.safe_answer_callback(cb, content.ADMIN_NOT_ALLOWED)
        return
    await common.safe_answer_callback(cb, "Подтверждено")
    try:
        target_id = int(cb.data[len(keyboards.CB_ADMIN_CONFIRM):])
    except ValueError:
        return
    await _confirm_paid(bot, target_id, by_admin=cb.from_user.id)
    try:
        await cb.message.edit_text(
            (cb.message.html_text or "") + "\n\n✅ Подтверждено."
        )
    except Exception:
        pass


# NB: the /paid command is handled by handlers/tickets.py (it issues the pretix
# ticket via the backend). Here we keep only the shared confirm used by the inline
# "Подтвердить" button, now wired to issue + deliver the ticket too.
async def _confirm_paid(bot: Bot, target_id: int, *, by_admin: int) -> None:
    user = await db.get_user(target_id)
    if not user:
        log.warning("Confirm paid: unknown user %s", target_id)
        return
    # Idempotent: if already paid, don't re-notify / re-DM (avoids double pings).
    if user["paid"]:
        log.info("User %s already marked paid; confirm by %s is a no-op",
                 target_id, by_admin)
        return
    await db.set_paid(target_id, True)
    await funnel.stop_drip(target_id)
    log.info("User %s marked paid by admin %s", target_id, by_admin)
    try:
        await bot.send_message(target_id, content.PAID_CONFIRMED_USER)
    except Exception:
        log.info("Could not notify user %s of confirmation", target_id)
    # Issue the pretix ticket via backend (also reflects paid+ticket to amoCRM) and deliver.
    try:
        base = config.LEAD_API_URL.split("/api/")[0]
        hdr = {"X-Internal-Token": config.INTERNAL_TOKEN, "Content-Type": "application/json"}
        async with aiohttp.ClientSession() as s:
            async with s.get(base + "/api/reg/find", params={"q": str(target_id)}, headers=hdr,
                             timeout=aiohttp.ClientTimeout(total=12)) as r:
                f = await r.json()
            if f.get("found"):
                async with s.post(base + "/api/issue_ticket", json={"reg_id": f["reg"]["id"]},
                                  headers=hdr, timeout=aiohttp.ClientTimeout(total=25)) as r2:
                    t = await r2.json()
                if t.get("ok"):
                    cap = ("🎫 Ваш билет на саммит «Казань — Токио».\nКод: %s\n"
                           "Предъявите QR на входе (вход однократный)." % t.get("human_code"))
                    await bot.send_photo(target_id, BufferedInputFile(
                        base64.b64decode(t["png_b64"]), "ticket-qr.png"), caption=cap)
                    await bot.send_document(target_id, BufferedInputFile(
                        base64.b64decode(t["pdf_b64"]), "ticket.pdf"))
    except Exception as exc:
        log.warning("confirm_paid: ticket issue/deliver failed: %s", exc)
