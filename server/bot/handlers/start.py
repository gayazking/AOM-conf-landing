"""/start, greeting, consent line and the subscription gate recheck."""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import content
import db
import keyboards
import subscription
from states import Registration

from . import common

log = logging.getLogger(__name__)
router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot, state: FSMContext) -> None:
    await state.clear()
    user = message.from_user
    await db.upsert_user_basic(user.id, user.username)

    await message.answer(
        content.GREETING, disable_web_page_preview=True
    )

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
