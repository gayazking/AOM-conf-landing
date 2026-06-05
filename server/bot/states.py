"""FSM state groups."""
from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class Registration(StatesGroup):
    consent = State()
    phone = State()
    name = State()
    city = State()
