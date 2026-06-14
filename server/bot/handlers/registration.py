"""Registration FSM: phone (contact only) -> name -> city -> lead push -> menu."""
from __future__ import annotations

import logging
import re

import aiohttp

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery

import config
import amojo_bridge
import content
import db
import funnel
import keyboards
import lead
from states import Registration

from . import common

log = logging.getLogger(__name__)
router = Router(name="registration")

_DIGITS = re.compile(r"\d")


def _valid_phone(raw: str) -> bool:
    return len(_DIGITS.findall(raw or "")) >= 10


def _normalize_phone(raw: str) -> str:
    """Normalize to a consistent E.164-ish form so amoCRM dedupe is stable."""
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return raw.strip()
    return "+" + digits


@router.message(Registration.phone, F.contact)
async def reg_phone_contact(message: Message, state: FSMContext) -> None:
    contact = message.contact
    # Only accept the user's OWN shared contact.
    if contact.user_id is not None and contact.user_id != message.from_user.id:
        await message.answer(content.REG_PHONE_NEED_BUTTON)
        return
    phone = contact.phone_number or ""
    if not _valid_phone(phone):
        await message.answer(content.REG_PHONE_INVALID)
        return
    await state.update_data(phone=_normalize_phone(phone))
    await message.answer(content.REG_ASK_NAME, reply_markup=keyboards.remove_kb())
    await state.set_state(Registration.name)


@router.message(Registration.phone)
async def reg_phone_typed(message: Message, state: FSMContext) -> None:
    """Reject any non-contact input while waiting for phone."""
    await message.answer(
        content.REG_PHONE_NEED_BUTTON,
        reply_markup=keyboards.phone_request_kb(),
    )


@router.message(Registration.name, F.text)
async def reg_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer(content.REG_NAME_INVALID)
        return
    await state.update_data(name=name)
    await message.answer(content.REG_ASK_CITY)
    await state.set_state(Registration.city)


@router.message(Registration.name)
async def reg_name_invalid(message: Message, state: FSMContext) -> None:
    await message.answer(content.REG_NAME_INVALID)


@router.message(Registration.city, F.text)
async def reg_city(message: Message, state: FSMContext, bot: Bot) -> None:
    city = (message.text or "").strip()
    if len(city) < 2:
        await message.answer(content.REG_CITY_INVALID)
        return
    data = await state.get_data()
    name = data.get("name", "")
    phone = data.get("phone", "")
    user = message.from_user

    # Don't double-push a lead if this user is somehow already registered.
    existing = await db.get_user(user.id)
    already_registered = bool(existing and existing["registered"])

    await db.complete_registration(
        user.id, user.username, name, phone, city
    )
    await state.clear()

    if not already_registered:
        # Push to amoCRM backend (never blocks/crashes; queues on failure).
        payload = lead.build_lead_payload(
            name=name, phone=phone, city=city,
            username=user.username, tg_id=user.id,
        )
        await lead.send_lead(user.id, payload)

        # Open the amoJo chat early with a registration summary so the manager
        # sees the new lead in the deal chat (no-op if amoJo disabled).
        await amojo_bridge.push_to_amojo(
            user.id,
            name=name,
            username=user.username,
            phone=phone,
            text=f"Зарегистрировался через бот. Город/клиника: {city}.",
            message_id=message.message_id,
        )

        # Arm the nurture funnel.
        try:
            await funnel.schedule_drip_for_user(user.id)
        except Exception:
            log.exception("Failed to schedule drip for %s", user.id)
    else:
        log.info("User %s re-registered; skipping duplicate lead/drip", user.id)

    await message.answer(content.REG_DONE)
    await common.show_main_menu(bot, message.chat.id)


@router.message(Registration.city)
async def reg_city_invalid(message: Message, state: FSMContext) -> None:
    await message.answer(content.REG_CITY_INVALID)


@router.callback_query(F.data == keyboards.CB_CONSENT_OK)
async def reg_consent_ok(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if await state.get_state() != Registration.consent.state:
        await cb.answer()
        return
    try:
        base = config.LEAD_API_URL.split("/api/")[0]
        async with aiohttp.ClientSession() as sess:
            await sess.post(
                base + "/api/consent",
                json={"telegram_user_id": cb.from_user.id,
                      "docs": ["oferta", "pdn", "image_152_1"],
                      "doc_version": "Редакция №1 от 01.06.2026",
                      "action": "granted"},
                headers={"X-Internal-Token": getattr(config, "INTERNAL_TOKEN", ""),
                         "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10),
            )
    except Exception:
        log.warning("consent log failed for %s", cb.from_user.id)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await cb.answer("Согласие зафиксировано ✅")
    # Site→TG deep-link: данные уже есть из заявки → не спрашиваем телефон заново.
    pf = (await state.get_data()).get("prefill")
    if pf and pf.get("phone"):
        await _finish_from_prefill(cb, state, bot, pf)
        return
    await bot.send_message(cb.message.chat.id, content.REG_ASK_PHONE, reply_markup=keyboards.phone_request_kb())
    await state.set_state(Registration.phone)


async def _finish_from_prefill(cb: CallbackQuery, state: FSMContext, bot: Bot, pf: dict) -> None:
    """Завершить регистрацию из заявки с сайта (deep-link), без перезаполнения.
    lead.send_lead с тем же телефоном → backend link_identity сольёт сайт+TG в одну
    сделку и проставит telegram_user_id (доставка билета/инфы в TG)."""
    user = cb.from_user
    name = pf.get("name") or (user.full_name or "Гость")
    phone = pf.get("phone") or ""
    city = pf.get("city") or ""
    existing = await db.get_user(user.id)
    already = bool(existing and existing["registered"])
    await db.complete_registration(user.id, user.username, name, phone, city)
    await state.clear()
    if not already:
        payload = lead.build_lead_payload(
            name=name, phone=phone, city=city, username=user.username, tg_id=user.id,
        )
        payload["message"] = "Завершение заявки с сайта в боте (deep-link). " + payload["message"]
        await lead.send_lead(user.id, payload)
        await amojo_bridge.push_to_amojo(
            user.id, name=name, username=user.username, phone=phone,
            text=("Завершил регистрацию из заявки с сайта."
                  + (f" Тариф: {pf.get('package')}." if pf.get("package") else "")),
            message_id=cb.message.message_id,
        )
        try:
            await funnel.schedule_drip_for_user(user.id)
        except Exception:
            log.exception("Failed to schedule drip for %s", user.id)
    await bot.send_message(cb.message.chat.id, content.REG_DONE)
    await common.show_main_menu(bot, cb.message.chat.id)
