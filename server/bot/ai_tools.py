"""Tool definitions (flat Responses-API schema, strict) + dispatcher.

Tools:
  send_payment_link(package)        -> sends the external payment URL to client
  escalate_to_manager(reason, ...)  -> pauses AI for the user, pings manager
  save_note_to_crm(text)            -> persists a lead note (ai_notes table)
  set_funnel_stage(stage)           -> maps to db.set_funnel_stage (whitelisted)

dispatch_tool(name, args, tg_id) is awaited by ai.run_turn for each call.
Side effects are validated/whitelisted here because strict schemas constrain
the model but we still execute real actions.
"""
from __future__ import annotations

import logging

import config
import content
import db

log = logging.getLogger(__name__)

# aiogram Bot, wired by ai.set_bot() -> ai_tools.set_bot(). Used to deliver the
# payment link to the client byte-exact instead of trusting the model to repeat
# a long tokenized URL verbatim.
_bot = None


def set_bot(bot) -> None:
    global _bot
    _bot = bot


# Flat Responses-API tool schema (no nested "function" wrapper, strict).
TOOLS = [
    {
        "type": "function",
        "name": "send_payment_link",
        "description": (
            "Send the payment/registration link for a chosen summit package. "
            "Call ONLY when the user clearly intends to pay or asks for the link."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "package": {
                    "type": "string",
                    # Single source of truth: derive valid keys from content so
                    # the schema can never advertise/reject a renamed package.
                    "enum": list(content.PACKAGE_ORDER),
                    "description": "Package key from the summit package list.",
                }
            },
            "required": ["package"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "escalate_to_manager",
        "description": (
            "Hand the dialog to a human manager. Call for hot leads ready to "
            "buy, complex/edge questions, complaints, payment issues, or any "
            "request you cannot answer factually."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "enum": [
                        "hot_lead",
                        "complaint",
                        "complex_question",
                        "payment_issue",
                        "explicit_request",
                    ],
                },
                "summary": {
                    "type": "string",
                    "description": "1-2 sentence context for the manager, in Russian.",
                },
            },
            "required": ["reason", "summary"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "save_note_to_crm",
        "description": (
            "Persist an important fact about the lead (specialty, clinic size, "
            "objection, budget, timing)."
        ),
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "set_funnel_stage",
        "description": "Update the lead's sales funnel stage after each meaningful step.",
        "parameters": {
            "type": "object",
            "properties": {
                "stage": {
                    "type": "string",
                    "enum": ["new", "engaged", "objection", "ready_to_pay", "paid", "lost"],
                }
            },
            "required": ["stage"],
            "additionalProperties": False,
        },
    },
]

_VALID_STAGES = {"new", "engaged", "objection", "ready_to_pay", "paid", "lost"}


async def dispatch_tool(name: str, args: dict, tg_id: int) -> dict:
    """Execute a tool call; always returns a JSON-serialisable dict, never raises.

    Imports of bot/notify helpers are local to avoid import cycles
    (amojo_bridge/ai import nothing from here at module load).
    """
    try:
        if name == "send_payment_link":
            return await _send_payment_link(args, tg_id)
        if name == "escalate_to_manager":
            return await _escalate(args, tg_id)
        if name == "save_note_to_crm":
            return await _save_note(args, tg_id)
        if name == "set_funnel_stage":
            return await _set_stage(args, tg_id)
        return {"ok": False, "error": f"unknown tool {name}"}
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("Tool %s failed for %s", name, tg_id)
        return {"ok": False, "error": str(exc)[:200]}


async def _send_payment_link(args: dict, tg_id: int) -> dict:
    key = str(args.get("package", "")).strip()
    pkg = content.PACKAGES.get(key)
    if not pkg:
        return {"ok": False, "error": "unknown_package"}
    url = config.PAYMENT_URLS.get(key)
    if not url:
        # No link configured -> escalate so a human sends manual details.
        return {
            "ok": False,
            "error": "no_link_configured",
            "title": pkg["title"],
            "price": pkg["price"],
            "instruction": "Ссылки нет — предложи связать с менеджером.",
        }
    # Send the link to the client OUT-OF-BAND so it is byte-exact (the model
    # must never retype a long tokenized URL — it would truncate/alter it).
    sent = False
    if _bot is not None:
        msg = (
            f"💳 <b>{pkg['title']}</b> — {pkg['price']}\n\n"
            f'Ссылка для оплаты:\n<a href="{url}">Оплатить {pkg["title"]}</a>'
        )
        try:
            await _bot.send_message(tg_id, msg)
            sent = True
        except Exception:
            log.warning("Failed to send payment link to %s", tg_id)
    if not sent:
        # Bot not wired or send failed -> let the model surface the URL itself.
        return {"ok": True, "title": pkg["title"], "price": pkg["price"], "link": url}
    # Link already delivered; the model should only confirm in prose (NOT repeat
    # the URL) so it cannot corrupt the conversion-critical string.
    return {
        "ok": True,
        "sent": True,
        "title": pkg["title"],
        "price": pkg["price"],
        "note": "Ссылка уже отправлена клиенту отдельным сообщением. "
        "Не повторяй URL — просто подтверди и подскажи следующий шаг.",
    }


async def _escalate(args: dict, tg_id: int) -> dict:
    reason = str(args.get("reason", "explicit_request"))
    summary = str(args.get("summary", "")).strip()
    # Pause AI for this user so a human owns the chat (auto-resumes by timeout).
    await db.set_ai_paused(tg_id, True, minutes=config.AI_PAUSE_MINUTES)
    # Notify the manager chat / admins (local import avoids cycle).
    from ai import notify_manager  # noqa: WPS433

    await notify_manager(
        tg_id,
        f"🤝 Эскалация ({reason}).\nКлиент tg_id={tg_id}.\n{summary}",
    )
    return {"ok": True, "escalated": True, "note": "Менеджер подключится к диалогу."}


async def _save_note(args: dict, tg_id: int) -> dict:
    text = str(args.get("text", "")).strip()
    if not text:
        return {"ok": False, "error": "empty"}
    await db.append_ai_note(tg_id, text[:2000])
    return {"ok": True, "saved": True}


async def _set_stage(args: dict, tg_id: int) -> dict:
    stage = str(args.get("stage", "")).strip()
    if stage not in _VALID_STAGES:
        return {"ok": False, "error": "invalid_stage"}
    await db.set_funnel_stage(tg_id, stage)
    return {"ok": True, "stage": stage}
