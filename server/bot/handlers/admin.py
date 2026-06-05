"""Admin commands: /stats, /broadcast, and AI/takeover controls.

AI controls:
  /takeover <tg_id> — manager seizes a chat (pauses AI for that user).
  /release  <tg_id> — resume AI for that user.
  /ai_on  <tg_id>   — alias of /release.
  /ai_off <tg_id>   — alias of /takeover.
  /forget <tg_id>   — erase the user's AI conversation memory (GDPR).
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Router
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter, TelegramAPIError
from aiogram.filters import Command
from aiogram.types import Message

import config
import content
import db

from . import common

log = logging.getLogger(__name__)
router = Router(name="admin")


def _parse_target(text: str) -> int | None:
    parts = (text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        return None
    return int(parts[1])


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not common.is_admin(message.from_user.id):
        await message.answer(content.ADMIN_NOT_ALLOWED)
        return
    s = await db.stats()
    stages = "\n".join(
        f"  • {stage}: {count}" for stage, count in sorted(s["by_stage"].items())
    ) or "  (нет данных)"
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"Всего пользователей: {s['users']}\n"
        f"Зарегистрировано: {s['registered']}\n"
        f"Оплатило: {s['paid']}\n"
        f"Активных: {s['active']}\n\n"
        f"По стадиям воронки:\n{stages}\n\n"
        f"Режим ИИ: {config.AI_MODE if config.AI_ENABLED else 'выключен'}"
    )
    await message.answer(text)


@router.message(Command("takeover"))
async def cmd_takeover(message: Message) -> None:
    if not common.is_admin(message.from_user.id):
        await message.answer(content.ADMIN_NOT_ALLOWED)
        return
    target = _parse_target(message.text or "")
    if target is None:
        await message.answer("Использование: /takeover <tg_id>")
        return
    await db.set_ai_paused(target, True, minutes=config.AI_PAUSE_MINUTES)
    await message.answer(
        f"🤝 Диалог с id {target} переведён на менеджера. ИИ на паузе "
        f"(автовозобновление через {config.AI_PAUSE_MINUTES} мин)."
    )


@router.message(Command("ai_off"))
async def cmd_ai_off(message: Message) -> None:
    await cmd_takeover(message)


@router.message(Command("release"))
async def cmd_release(message: Message) -> None:
    if not common.is_admin(message.from_user.id):
        await message.answer(content.ADMIN_NOT_ALLOWED)
        return
    target = _parse_target(message.text or "")
    if target is None:
        await message.answer("Использование: /release <tg_id>")
        return
    await db.set_ai_paused(target, False)
    await message.answer(f"✅ ИИ снова отвечает клиенту id {target}.")


@router.message(Command("ai_on"))
async def cmd_ai_on(message: Message) -> None:
    await cmd_release(message)


@router.message(Command("forget"))
async def cmd_forget(message: Message) -> None:
    if not common.is_admin(message.from_user.id):
        await message.answer(content.ADMIN_NOT_ALLOWED)
        return
    target = _parse_target(message.text or "")
    if target is None:
        await message.answer("Использование: /forget <tg_id>")
        return
    await db.forget_user_messages(target)
    await message.answer(f"🗑 История диалога ИИ для id {target} удалена.")


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, bot: Bot) -> None:
    if not common.is_admin(message.from_user.id):
        await message.answer(content.ADMIN_NOT_ALLOWED)
        return

    # Two modes: reply-to-message (copy it) or inline text after the command.
    reply = message.reply_to_message
    inline_text = ""
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1:
        inline_text = parts[1].strip()

    if reply is None and not inline_text:
        await message.answer(content.ADMIN_BROADCAST_USAGE)
        return

    users = await db.all_active_users()
    total = len(users)
    await message.answer(f"Начинаю рассылку для {total} активных пользователей…")

    delivered = 0
    failed = 0
    blocked = 0
    delay = 1.0 / max(config.SEND_RATE, 1.0)

    for user in users:
        tg_id = user["tg_id"]
        try:
            if reply is not None:
                await bot.copy_message(
                    chat_id=tg_id,
                    from_chat_id=message.chat.id,
                    message_id=reply.message_id,
                )
            else:
                await bot.send_message(tg_id, inline_text)
            delivered += 1
        except TelegramRetryAfter as exc:
            # Respect Telegram flood control, then retry once.
            await asyncio.sleep(exc.retry_after + 1)
            try:
                if reply is not None:
                    await bot.copy_message(
                        chat_id=tg_id,
                        from_chat_id=message.chat.id,
                        message_id=reply.message_id,
                    )
                else:
                    await bot.send_message(tg_id, inline_text)
                delivered += 1
            except Exception:
                failed += 1
        except TelegramForbiddenError:
            blocked += 1
            await db.set_active(tg_id, False)
        except TelegramAPIError as exc:
            failed += 1
            log.warning("Broadcast to %s failed: %s", tg_id, exc)
        except Exception:
            failed += 1
            log.exception("Unexpected broadcast error to %s", tg_id)
        await asyncio.sleep(delay)

    await message.answer(
        "✅ Рассылка завершена.\n"
        f"Доставлено: {delivered}\n"
        f"Заблокировали бота (помечены неактивными): {blocked}\n"
        f"Ошибок: {failed}"
    )
