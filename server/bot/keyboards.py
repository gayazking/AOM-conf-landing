"""Inline and reply keyboard builders."""
from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

import config
import content


# --------------------------------------------------------------------------- #
# Callback data prefixes (kept short and namespaced)
# --------------------------------------------------------------------------- #
CB_RECHECK = "sub:recheck"
CB_CONSENT_OK = "consent:ok"
CB_MENU = "menu"
CB_PRICES = "menu:prices"
CB_SPEAKERS = "menu:speakers"
CB_PROGRAM = "menu:program"
CB_PAY = "menu:pay"
CB_REMIND = "menu:remind"
CB_CONTACT = "menu:contact"
CB_SATO = "menu:sato"
CB_TRAVEL = "menu:travel"
CB_PAY_PKG = "pay:"          # + package_key
CB_I_PAID = "paid:claim:"    # + package_key (or 'none')
CB_ADMIN_CONFIRM = "admin:confirm:"  # + tg_id


def subscription_gate_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Подписаться на {content.CHANNEL_DISPLAY}",
                    url=config.CHANNEL_URL,
                )
            ],
            [InlineKeyboardButton(text="Я подписался ✅", callback_data=CB_RECHECK)],
        ]
    )


def phone_request_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=content.REG_PHONE_BUTTON, request_contact=True)]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Нажмите кнопку ниже",
    )


def remove_kb() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=content.BTN_PRICES, callback_data=CB_PRICES)],
            [
                InlineKeyboardButton(text=content.BTN_SPEAKERS, callback_data=CB_SPEAKERS),
                InlineKeyboardButton(text=content.BTN_PROGRAM, callback_data=CB_PROGRAM),
            ],
            [InlineKeyboardButton(text=content.BTN_PAY, callback_data=CB_PAY)],
            [InlineKeyboardButton(text=content.BTN_SATO, callback_data=CB_SATO)],
            [InlineKeyboardButton(text=content.BTN_TRAVEL, callback_data=CB_TRAVEL)],
            [
                InlineKeyboardButton(text=content.BTN_REMIND, callback_data=CB_REMIND),
                InlineKeyboardButton(text=content.BTN_CONTACT, callback_data=CB_CONTACT),
            ],
        ]
    )


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=content.BTN_BACK, callback_data=CB_MENU)]
        ]
    )


def packages_kb() -> InlineKeyboardMarkup:
    rows = []
    for key in content.PACKAGE_ORDER:
        pkg = content.PACKAGES[key]
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{pkg['title']} — {pkg['price']}",
                    callback_data=CB_PAY_PKG + key,
                )
            ]
        )
    rows.append([InlineKeyboardButton(text=content.BTN_BACK, callback_data=CB_MENU)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pay_actions_kb(package_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=content.BTN_I_PAID,
                    callback_data=CB_I_PAID + package_key,
                )
            ],
            [InlineKeyboardButton(text=content.BTN_CONTACT, callback_data=CB_CONTACT)],
            [InlineKeyboardButton(text=content.BTN_BACK, callback_data=CB_MENU)],
        ]
    )


def funnel_cta_kb(kind: str) -> InlineKeyboardMarkup:
    """CTA keyboard attached to each drip message.

    When speakers/program are still placeholders, the funnel must NOT drive
    leads to empty screens — fall back to prices/contact instead.
    """
    pay_btn = InlineKeyboardButton(text=content.BTN_PAY, callback_data=CB_PAY)
    prices_btn = InlineKeyboardButton(text=content.BTN_PRICES, callback_data=CB_PRICES)
    contact_btn = InlineKeyboardButton(text=content.BTN_CONTACT, callback_data=CB_CONTACT)

    if kind == "funnel_1h":
        if content.PROGRAM_READY:
            rows = [[InlineKeyboardButton(text=content.BTN_PROGRAM, callback_data=CB_PROGRAM)]]
        else:
            rows = [[prices_btn]]
    elif kind == "funnel_1d":
        if content.SPEAKERS_READY:
            rows = [[InlineKeyboardButton(text=content.BTN_SPEAKERS, callback_data=CB_SPEAKERS)]]
        else:
            rows = [[prices_btn]]
    elif kind == "funnel_3d":
        rows = [[pay_btn]]
    elif kind == "funnel_7d":
        rows = [[pay_btn], [contact_btn]]
    else:
        rows = [[InlineKeyboardButton(text=content.BTN_BACK, callback_data=CB_MENU)]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_confirm_kb(tg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить оплату",
                    callback_data=CB_ADMIN_CONFIRM + str(tg_id),
                )
            ]
        ]
    )


def consent_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принимаю и даю согласие", callback_data=CB_CONSENT_OK)],
    ])
