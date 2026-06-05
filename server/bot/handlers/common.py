"""Shared helpers used by multiple handler modules."""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import CallbackQuery, Message

import config
import content
import db
import keyboards
import subscription

log = logging.getLogger(__name__)


async def ensure_subscribed_then_gate(
    bot: Bot, chat_id: int, user_id: int
) -> bool:
    """Check subscription; if not subscribed/erroring, show gate and return False.

    Returns True only when the user is confirmed subscribed.
    """
    result = await subscription.check_subscription(bot, user_id)
    if result is subscription.GATE_ERROR:
        await bot.send_message(chat_id, content.GATE_TEMP_ERROR)
        return False
    if result is True:
        await db.set_subscribed(user_id, True)
        return True
    # Not subscribed.
    await db.set_subscribed(user_id, False)
    await bot.send_message(
        chat_id,
        content.GATE_NOT_SUBSCRIBED,
        reply_markup=keyboards.subscription_gate_kb(),
        disable_web_page_preview=True,
    )
    return False


async def is_registered(user_id: int) -> bool:
    user = await db.get_user(user_id)
    return bool(user and user["registered"])


async def show_main_menu(bot: Bot, chat_id: int) -> None:
    await bot.send_message(
        chat_id, content.MAIN_MENU_TEXT, reply_markup=keyboards.main_menu_kb()
    )


async def safe_answer_callback(cb: CallbackQuery, text: str | None = None) -> None:
    try:
        await cb.answer(text or "")
    except Exception:
        pass


async def safe_edit_or_send(
    cb: CallbackQuery, text: str, reply_markup=None
) -> None:
    """Edit the message in place, falling back to a new message."""
    try:
        await cb.message.edit_text(
            text, reply_markup=reply_markup, disable_web_page_preview=True
        )
    except Exception:
        try:
            await cb.message.answer(
                text, reply_markup=reply_markup, disable_web_page_preview=True
            )
        except Exception:
            log.exception("safe_edit_or_send failed")


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS
