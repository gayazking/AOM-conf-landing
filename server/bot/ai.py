"""OpenAI Responses-API sales agent: run_turn() tool round-trip + backoff.

Design (verified June 2026):
  * Responses API (client.responses.create), model-conditional params.
  * gpt-5* are reasoning models: NO temperature; use reasoning.effort +
    text.verbosity. gpt-4o-mini and others accept temperature.
  * Tool round-trip uses items: model emits {type:"function_call", call_id,...};
    we reply with {type:"function_call_output", call_id, output}.
  * store=False — SQLite (ai_memory) is the single source of truth; reasoning
    items are NOT persisted across turns (their opaque ids would 400 on replay).
  * 'developer' role is a valid Responses-API input role (system-style).

Everything here is defensive: API errors fall back to a canned line + escalate,
and nothing raises into the aiogram handler.
"""
from __future__ import annotations

import asyncio
import json
import logging

import config
from ai_prompt import build_system_prompt
from ai_tools import TOOLS, dispatch_tool

log = logging.getLogger(__name__)

_FALLBACK = "Давайте я уточню это у менеджера и вернусь к вам с ответом."

# Lazily-created singletons so importing this module never needs the API key.
_client = None  # AsyncOpenAI
_bot = None     # aiogram Bot, set via set_bot()

try:
    from openai import AsyncOpenAI, APIError, RateLimitError
except Exception:  # pragma: no cover - openai optional until key is set
    AsyncOpenAI = None  # type: ignore

    class APIError(Exception):
        ...

    class RateLimitError(Exception):
        ...


def set_bot(bot) -> None:
    """Wire the aiogram Bot so tools can notify the manager chat / send links."""
    global _bot
    _bot = bot
    # Let ai_tools send byte-exact payment links straight to the client.
    try:
        import ai_tools

        ai_tools.set_bot(bot)
    except Exception:  # pragma: no cover - defensive
        log.exception("Failed to wire bot into ai_tools")


def _get_client():
    global _client
    if _client is None:
        if AsyncOpenAI is None:
            raise RuntimeError("openai SDK not installed")
        _client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
    return _client


def _build_params(model: str, input_items: list) -> dict:
    p = {
        "model": model,
        "input": input_items,
        "max_output_tokens": config.AI_MAX_OUTPUT_TOKENS,
        "store": False,
        "tools": TOOLS,
        "tool_choice": "auto",
    }
    if model.startswith("gpt-5"):
        p["reasoning"] = {"effort": "low"}
        p["text"] = {"verbosity": "low"}
        # NO temperature for reasoning models (HTTP 400 otherwise).
    else:
        p["temperature"] = 0.6
    return p


async def _create_with_backoff(model: str, input_items: list):
    """responses.create with exponential backoff on rate/API errors."""
    client = _get_client()
    delays = [1, 2, 4]
    last_exc: Exception | None = None
    for i in range(len(delays) + 1):
        try:
            return await client.responses.create(**_build_params(model, input_items))
        except (RateLimitError, APIError) as exc:
            last_exc = exc
            if i < len(delays):
                await asyncio.sleep(delays[i])
            else:
                break
        except Exception as exc:  # network / unexpected
            last_exc = exc
            break
    raise last_exc if last_exc else RuntimeError("responses.create failed")


async def run_turn(tg_id: int, user_text: str) -> tuple[str, list[dict]]:
    """Run one user turn through the model + tool loop.

    Returns (reply_text, new_items) where new_items are the items produced this
    turn that are SAFE to persist (the user item + assistant/tool items, but NOT
    reasoning items). On hard failure returns the canned fallback and escalates.
    """
    import ai_memory

    history = await ai_memory.load_history(tg_id)
    user_item = {"role": "user", "content": user_text}
    input_items: list = [
        {"role": "developer", "content": build_system_prompt()},
        *history,
        user_item,
    ]
    # Items we will persist (do NOT persist the developer/system or history).
    new_items: list[dict] = [user_item]

    try:
        # Bound the model<->tool round-trips so a misbehaving model that keeps
        # emitting function_calls can never spin unbounded paid API calls.
        for _round in range(max(1, config.AI_MAX_TOOL_ROUNDS)):
            resp = await _create_with_backoff(config.AI_MODEL, input_items)
            output = list(getattr(resp, "output", []) or [])
            # Append every item to the LIVE context for this turn's tool loop,
            # but only PERSIST items we can safely replay next turn. Reasoning
            # items (gpt-5*, store=False) carry opaque ids the server has no
            # record of; replaying them later yields HTTP 400, so skip them.
            for item in output:
                item_dict = _item_to_dict(item)
                # store=False: reasoning items are NOT persisted server-side, so
                # re-feeding them by their opaque id 404s ("Item ... not found").
                # Skip entirely — also keeps them out of SQLite memory.
                if item_dict.get("type") == "reasoning":
                    continue
                # 'status' is an output-only field; echoing it back on input 400s
                # ("Unknown parameter: input[*].status").
                item_dict.pop("status", None)
                input_items.append(item_dict)
                new_items.append(item_dict)

            calls = [i for i in output if getattr(i, "type", None) == "function_call"]
            if not calls:
                text = (getattr(resp, "output_text", "") or "").strip()
                if not text:
                    # Empty/refusal -> safe fallback + escalate.
                    await _fallback_escalate(tg_id, user_text)
                    return _FALLBACK, new_items
                return text, new_items

            for call in calls:
                try:
                    args = json.loads(call.arguments or "{}")
                except Exception:
                    args = {}
                result = await dispatch_tool(call.name, args, tg_id)
                out_item = {
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": json.dumps(result, ensure_ascii=False),
                }
                input_items.append(out_item)
                new_items.append(out_item)
            # loop again so the model can speak after seeing tool results

        # Ran out of tool rounds without a final text answer -> hand off.
        log.warning("AI run_turn hit max tool rounds for %s", tg_id)
        await _fallback_escalate(tg_id, user_text)
        return _FALLBACK, new_items
    except Exception as exc:
        log.warning("AI run_turn failed for %s: %s", tg_id, type(exc).__name__)
        await _fallback_escalate(tg_id, user_text)
        return _FALLBACK, new_items


def _item_to_dict(item) -> dict:
    """Normalise an SDK output item to a plain dict for storage + re-feeding."""
    if isinstance(item, dict):
        return item
    for attr in ("model_dump", "to_dict", "dict"):
        fn = getattr(item, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:  # pragma: no cover
                pass
    # Last-resort minimal reconstruction.
    return {
        "type": getattr(item, "type", "message"),
        "role": getattr(item, "role", "assistant"),
        "content": getattr(item, "content", ""),
    }


async def _fallback_escalate(tg_id: int, user_text: str) -> None:
    try:
        import db

        await db.set_ai_paused(tg_id, True, minutes=config.AI_PAUSE_MINUTES)
        await notify_manager(
            tg_id,
            f"⚠️ ИИ не смог ответить, нужен менеджер. tg_id={tg_id}.",
        )
    except Exception:  # pragma: no cover - defensive
        log.exception("fallback escalate failed for %s", tg_id)


async def notify_manager(tg_id: int, text: str) -> None:
    """Send a manager-facing notice to MANAGER_CHAT_ID and/or ADMIN_IDS."""
    if _bot is None:
        log.info("notify_manager: bot not wired yet")
        return
    targets: list[int] = []
    if config.MANAGER_CHAT_ID:
        targets.append(config.MANAGER_CHAT_ID)
    for aid in config.ADMIN_IDS:
        if aid not in targets:
            targets.append(aid)
    sent_any = False
    for target in targets:
        try:
            # parse_mode=None: this text embeds model/user-authored content that
            # may contain '<', '>' or '&'; HTML parsing would 400 and silently
            # drop the escalation, defeating the hand-off to a human.
            await _bot.send_message(target, text, parse_mode=None)
            sent_any = True
        except Exception:
            log.warning("Could not notify manager target %s", target)
    if targets and not sent_any:
        # Every target failed — make the missed hand-off alertable.
        log.error("notify_manager: failed to reach ANY manager target for %s", tg_id)
