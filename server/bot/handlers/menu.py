"""Main menu navigation. All content is gated behind registration."""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery

import content
import keyboards

from . import common

log = logging.getLogger(__name__)
router = Router(name="menu")


async def _require_registered(cb: CallbackQuery) -> bool:
    if await common.is_registered(cb.from_user.id):
        return True
    await common.safe_answer_callback(
        cb, "Сначала завершите регистрацию — нажмите /start"
    )
    return False


@router.callback_query(F.data == keyboards.CB_MENU)
async def open_menu(cb: CallbackQuery) -> None:
    await common.safe_answer_callback(cb)
    if not await _require_registered(cb):
        return
    await common.safe_edit_or_send(
        cb, content.MAIN_MENU_TEXT, reply_markup=keyboards.main_menu_kb()
    )


@router.callback_query(F.data == keyboards.CB_PRICES)
async def show_prices(cb: CallbackQuery) -> None:
    await common.safe_answer_callback(cb)
    if not await _require_registered(cb):
        return
    await common.safe_edit_or_send(
        cb, content.prices_text(), reply_markup=keyboards.back_kb()
    )


@router.callback_query(F.data == keyboards.CB_SPEAKERS)
async def show_speakers(cb: CallbackQuery) -> None:
    await common.safe_answer_callback(cb)
    if not await _require_registered(cb):
        return
    await common.safe_edit_or_send(
        cb, content.SPEAKERS, reply_markup=keyboards.back_kb()
    )


@router.callback_query(F.data == keyboards.CB_PROGRAM)
async def show_program(cb: CallbackQuery) -> None:
    await common.safe_answer_callback(cb)
    if not await _require_registered(cb):
        return
    await common.safe_edit_or_send(
        cb, content.PROGRAM, reply_markup=keyboards.back_kb()
    )


@router.callback_query(F.data == keyboards.CB_CONTACT)
async def show_contact(cb: CallbackQuery) -> None:
    await common.safe_answer_callback(cb)
    if not await _require_registered(cb):
        return
    await common.safe_edit_or_send(
        cb, content.CONTACT_TEXT, reply_markup=keyboards.back_kb()
    )


@router.callback_query(F.data == keyboards.CB_REMIND)
async def set_reminder(cb: CallbackQuery) -> None:
    await common.safe_answer_callback(cb, "Готово 🔔")
    if not await _require_registered(cb):
        return
    await common.safe_edit_or_send(
        cb, content.REMIND_SET, reply_markup=keyboards.back_kb()
    )
