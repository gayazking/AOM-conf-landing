"""Handler routers, aggregated for one-shot registration in the dispatcher.

Order matters: admin first (commands), then start/gate, registration FSM,
then menu/payment callbacks, and finally the free-text AI router (LAST) so the
FSM, menu callbacks and slash commands always keep priority over the AI.
"""
from __future__ import annotations

from aiogram import Router

from . import admin, start, registration, menu, payment, tickets, ai_chat


def build_root_router() -> Router:
    root = Router(name="root")
    root.include_router(admin.router)
    root.include_router(start.router)
    root.include_router(registration.router)
    root.include_router(menu.router)
    root.include_router(payment.router)
    root.include_router(tickets.router)
    # AI free-text router must be LAST — it catches anything not handled above.
    root.include_router(ai_chat.router)
    return root
