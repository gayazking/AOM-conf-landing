"""Payment flow: choose package -> show external link -> 'Я оплатил' -> admin verify.

No card data is ever collected or stored — only external payment links.
"""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

import config
import content
import db
import funnel
import keyboards

from . import common

log = logging.getLogger(__name__)
router = Router(name="payment")


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


@router.message(Command("paid"))
async def cmd_paid(message: Message, bot: Bot) -> None:
    """Admin command: /paid <tg_id> — manually confirm a payment."""
    if not common.is_admin(message.from_user.id):
        await message.answer(content.ADMIN_NOT_ALLOWED)
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.answer("Использование: /paid <tg_id>")
        return
    target_id = int(parts[1])
    await _confirm_paid(bot, target_id, by_admin=message.from_user.id)
    await message.answer(f"✅ Оплата подтверждена для id {target_id}.")


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
