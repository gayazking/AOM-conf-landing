"""Per-user conversation memory backed by the shared SQLite connection.

Stores each Responses-API input item (dict) as a JSON row in `messages`
(created in db.py SCHEMA). Trimming happens on load by a token budget so the
context stays small and cheap. tiktoken is used when available; a length-based
fallback keeps the bot working if tiktoken is not installed.
"""
from __future__ import annotations

import json
import logging

import db

log = logging.getLogger(__name__)

MAX_HISTORY_TOKENS = 3000
MAX_TURNS = 20

try:  # tiktoken is optional at runtime — degrade gracefully.
    import tiktoken

    _ENC = tiktoken.get_encoding("o200k_base")
except Exception:  # pragma: no cover - optional dep / offline
    _ENC = None
    log.info("tiktoken unavailable; using char-based history trimming")


def _n_tokens(item: dict) -> int:
    content = item.get("content")
    txt = content if isinstance(content, str) else json.dumps(item, ensure_ascii=False)
    if _ENC is not None:
        try:
            return len(_ENC.encode(txt)) + 4
        except Exception:  # pragma: no cover - defensive
            pass
    # Rough fallback: ~4 chars per token.
    return (len(txt) // 4) + 4


async def append_item(tg_id: int, item: dict) -> None:
    """Persist one Responses input/output item for the user."""
    await db.append_message(tg_id, json.dumps(item, ensure_ascii=False))


async def load_history(tg_id: int) -> list[dict]:
    """Return chronological history items trimmed to the token budget.

    The Responses API rejects orphaned tool/reasoning items, so the window must
    start AND end on complete units:
      * a leading function_call_output (its function_call was trimmed) -> 400,
      * a trailing function_call with no following function_call_output -> 400
        ("No tool output found for function call ..."),
      * a leading reasoning item whose function_call was trimmed.
    We drop those edges. We also `continue` (not `break`) past a single oversized
    item so one big row never truncates the whole window.
    """
    rows = await db.load_messages(tg_id, MAX_TURNS * 4)  # newest-first
    items: list[dict] = []
    budget = MAX_HISTORY_TOKENS
    for raw in rows:  # newest -> oldest
        try:
            it = json.loads(raw)
        except Exception:  # pragma: no cover - corrupt row
            continue
        t = _n_tokens(it)
        if budget - t < 0:
            # Skip this oversized/over-budget item but keep scanning older ones
            # that may still fit (don't truncate the whole window on one row).
            continue
        budget -= t
        items.append(it)
    items.reverse()  # chronological

    # Drop leading orphans (their preceding pair-member was trimmed away).
    while items and items[0].get("type") in ("function_call_output", "reasoning"):
        items.pop(0)

    # Drop a trailing function_call that has no matching function_call_output
    # (e.g. a previous turn crashed between the call and its output).
    def _last_call_unpaired() -> bool:
        if not items or items[-1].get("type") != "function_call":
            return False
        cid = items[-1].get("call_id")
        return not any(
            it.get("type") == "function_call_output" and it.get("call_id") == cid
            for it in items
        )

    while _last_call_unpaired():
        items.pop()
        # A reasoning item may immediately precede the dropped call; drop it too.
        if items and items[-1].get("type") == "reasoning":
            items.pop()

    return items


async def forget(tg_id: int) -> None:
    """Erase the user's conversation memory (GDPR / /forget)."""
    await db.forget_user_messages(tg_id)
