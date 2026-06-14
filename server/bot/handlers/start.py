"""/start, greeting, consent line and the subscription gate recheck.

Site→Telegram handoff: лендинг ведёт в `t.me/<bot>?start=<reg_id>`. Резолвим reg_id
в бэкенде, префиллим регистрацию из заявки с сайта (имя/телефон/город/тариф) и не
переспрашиваем — одна сделка, без дубля (link_identity сольёт сайт+TG по телефону)."""
from __future__ import annotations

import logging
import re

import aiohttp
from aiogram import Bot, F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import config
import content
import db
import keyboards
import subscription
from states import Registration

from . import common

log = logging.getLogger(__name__)
router = Router(name="start")

_REGID_RE = re.compile(r"^[0-9a-fA-F]{16,40}$")


async def _fetch_prefill(reg_id: str) -> dict | None:
    """Resolve a site lead by reg_id via the loopback backend → prefill dict."""
    try:
        base = config.LEAD_API_URL.split("/api/")[0]
        async with aiohttp.ClientSession() as s:
            async with s.get(base + "/api/reg/find", params={"q": reg_id},
                             headers={"X-Internal-Token": getattr(config, "INTERNAL_TOKEN", "")},
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                d = await r.json()
        if d.get("found") and d.get("reg", {}).get("phone_e164"):
            rg = d["reg"]
            return {"reg_id": rg.get("id"), "name": rg.get("full_name") or "",
                    "phone": rg.get("phone_e164") or "", "city": rg.get("city") or "",
                    "package": rg.get("package"), "format": rg.get("format")}
    except Exception:
        log.warning("prefill fetch failed for deep-link payload")
    return None


@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot, state: FSMContext,
                    command: CommandObject) -> None:
    await state.clear()
    user = message.from_user
    await db.upsert_user_basic(user.id, user.username)

    pf = None
    payload = (command.args or "").strip()
    if _REGID_RE.match(payload):
        pf = await _fetch_prefill(payload)
        if pf:
            await state.update_data(prefill=pf)

    greet = content.GREETING
    if pf and pf.get("name"):
        greet = ("С возвращением, %s! 👋\nЗавершим регистрацию по вашей заявке с сайта — "
                 "данные уже у меня, перезаполнять не нужно." % pf["name"])
    await message.answer(greet, disable_web_page_preview=True)

    subscribed = await common.ensure_subscribed_then_gate(
        bot, message.chat.id, user.id
    )
    if not subscribed:
        return
    await _post_gate(message.chat.id, user.id, bot, state)


@router.callback_query(F.data == keyboards.CB_RECHECK)
async def recheck_subscription(
    cb: CallbackQuery, bot: Bot, state: FSMContext
) -> None:
    await common.safe_answer_callback(cb)
    user = cb.from_user

    result = await subscription.check_subscription(bot, user.id)
    if result is subscription.GATE_ERROR:
        await cb.message.answer(content.GATE_TEMP_ERROR)
        return
    if result is not True:
        await db.set_subscribed(user.id, False)
        # Re-send the gate keyboard so retry is one tap, not a dead end.
        await cb.message.answer(
            content.GATE_STILL_NOT_SUBSCRIBED,
            reply_markup=keyboards.subscription_gate_kb(),
            disable_web_page_preview=True,
        )
        return
    await db.set_subscribed(user.id, True)
    # Remove the gate keyboard to avoid confusion.
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _post_gate(cb.message.chat.id, user.id, bot, state)


async def _post_gate(
    chat_id: int, user_id: int, bot: Bot, state: FSMContext
) -> None:
    """After confirmed subscription: route to registration or main menu."""
    if await common.is_registered(user_id):
        await common.show_main_menu(bot, chat_id)
        return
    # If a registration is already in progress, don't reset the FSM.
    current = await state.get_state()
    if current is not None:
        return
    # Begin registration FSM — ask for phone via contact button.
    await bot.send_message(
        chat_id,
        content.CONSENT_ASK,
        reply_markup=keyboards.consent_kb(),
        disable_web_page_preview=True,
    )
    await state.set_state(Registration.consent)
