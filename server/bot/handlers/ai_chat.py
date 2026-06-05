"""Free-text AI router — registered LAST so FSM/menu/commands keep priority.

Catches private text messages that are NOT part of the registration FSM and
NOT a slash command. Dispatches by config.AI_MODE:
  hybrid    — AI answers when active for the user; manager takeover pauses it.
  autopilot — AI answers unless a manager has taken over (pause is honoured).
  suggester — AI drafts only; draft goes to the manager chat, nothing auto-sent.

Every inbound client message is also mirrored into amoCRM via amojo_bridge, and
the AI's outgoing reply is mirrored too so the deal chat shows the full dialog.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

import config
import db

log = logging.getLogger(__name__)
router = Router(name="ai_chat")

# Only private text that is not a command and not an FSM step reaches here.
_NON_COMMAND_TEXT = F.chat.type == "private"

# Per-user serialization: two quick messages from the same user must not run
# overlapping run_turn() calls that interleave-append items and corrupt the
# function_call/output ordering in SQLite memory.
_user_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
# Per-user sliding-window rate limiter (cost-DoS guard).
_user_hits: dict[int, deque] = defaultdict(deque)


def _rate_limited(tg_id: int) -> bool:
    """True if the user exceeded AI_RATE_MAX_TURNS within AI_RATE_WINDOW_SEC."""
    now = time.monotonic()
    window = config.AI_RATE_WINDOW_SEC
    hits = _user_hits[tg_id]
    while hits and now - hits[0] > window:
        hits.popleft()
    if len(hits) >= config.AI_RATE_MAX_TURNS:
        return True
    hits.append(now)
    return False


@router.message(_NON_COMMAND_TEXT, F.text, ~F.text.startswith("/"))
async def on_free_text(message: Message, bot: Bot, state: FSMContext) -> None:
    # If a registration (or any) FSM is active, let it own the message.
    if await state.get_state() is not None:
        return

    user = message.from_user
    text = (message.text or "").strip()
    if not text:
        return

    # Mirror client message into amoCRM (best-effort, gated internally).
    await _mirror_to_amojo(message)

    if not config.AI_ENABLED:
        return

    tg_id = user.id

    # Manager-takeover pause must silence the AI in BOTH hybrid AND autopilot,
    # AND skip generating suggester drafts after a human took over. Otherwise an
    # escalate_to_manager (which sets ai_paused) would be ignored in autopilot
    # and the bot would talk over the human it just summoned.
    if not await db.is_ai_active(tg_id):
        return

    # Cheap cost guard: drop excess turns from a spamming user.
    if _rate_limited(tg_id):
        log.info("AI rate limit hit for %s; dropping turn", tg_id)
        return

    # Lazy import so the bot starts even without the openai SDK present.
    import ai
    import ai_memory

    lock = _user_locks[tg_id]
    async with lock:
        try:
            reply, new_items = await ai.run_turn(tg_id, text)
        except Exception:  # pragma: no cover - run_turn is already defensive
            log.exception("AI turn crashed for %s", tg_id)
            return

        # Persist the turn's items regardless of mode (memory is shared).
        for item in new_items:
            try:
                await ai_memory.append_item(tg_id, item)
            except Exception:
                log.exception("Failed to persist AI item for %s", tg_id)

    if config.AI_MODE == "suggester":
        await _send_draft_to_manager(bot, tg_id, user.username, text, reply)
        return

    # autopilot + hybrid(active): send the reply to the client.
    # parse_mode=None — the model writes plain text; a stray '<'/'&' would 400
    # under the bot's default HTML parse mode and silently drop the reply.
    if reply:
        await message.answer(reply, parse_mode=None)
        # Mirror the AI's outgoing answer into the amoCRM deal chat too, so the
        # manager watching the chat sees the full dialog, not just client lines.
        await _mirror_outgoing_to_amojo(message, reply)


async def _mirror_to_amojo(message: Message) -> None:
    try:
        import amojo_bridge

        user = message.from_user
        record = await db.get_user(user.id)
        phone = record.get("phone") if record else None
        name = (record.get("name") if record else None) or (user.full_name or "")
        await amojo_bridge.push_to_amojo(
            user.id,
            name=name,
            username=user.username,
            phone=phone,
            text=(message.text or ""),
            message_id=message.message_id,
        )
    except Exception:  # pragma: no cover - defensive
        log.exception("amojo mirror failed")


async def _mirror_outgoing_to_amojo(message: Message, reply: str) -> None:
    """Mirror the AI's outgoing reply into the deal chat (labelled), so the
    manager sees answers too, not only client questions. Best-effort."""
    try:
        import amojo_bridge

        user = message.from_user
        record = await db.get_user(user.id)
        phone = record.get("phone") if record else None
        name = (record.get("name") if record else None) or (user.full_name or "")
        await amojo_bridge.push_to_amojo(
            user.id,
            name=name,
            username=user.username,
            phone=phone,
            text=f"[бот/ИИ] {reply}",
            message_id=None,
        )
    except Exception:  # pragma: no cover - defensive
        log.exception("amojo outgoing mirror failed")


async def _send_draft_to_manager(
    bot: Bot, tg_id: int, username: str | None, client_text: str, draft: str
) -> None:
    target = config.MANAGER_CHAT_ID or (config.ADMIN_IDS[0] if config.ADMIN_IDS else 0)
    if not target:
        log.warning("suggester mode but no MANAGER_CHAT_ID/ADMIN_IDS configured")
        return
    uname = f"@{username}" if username else "(нет username)"
    # parse_mode=None: client_text and draft are user/model-authored and may
    # contain '<', '>' or '&'. Under HTML parse mode send_message would 400 and
    # the manager would silently never see the draft.
    note = (
        "✍️ Черновик ответа ИИ\n"
        f"Клиент {uname} (id {tg_id}):\n«{client_text[:500]}»\n\n"
        f"Черновик:\n{draft}\n\n"
        f"Отправьте клиенту вручную или используйте /takeover {tg_id}."
    )
    try:
        await bot.send_message(target, note, parse_mode=None)
    except Exception:
        log.error("Could not send suggester draft to %s", target)
