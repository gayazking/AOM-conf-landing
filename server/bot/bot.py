"""Entry point: build the bot/dispatcher, wire everything, run long-polling.

Graceful startup/shutdown, global error handler, lead-retry worker, APScheduler
funnel re-armed from the DB on boot, plus (optional) the amoJo internal
delivery server and the AI agent wiring.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent

import amojo_bridge
import config
import content
import db
import funnel
import lead
from handlers import build_root_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("sato-bot")

_stop_event = asyncio.Event()


async def _on_startup(bot: Bot) -> None:
    await db.init()
    log.info(config.summary())
    funnel.setup(bot)
    await funnel.rearm_from_db()
    # Wire the AI agent so tools can notify the manager (no-op if AI disabled).
    if config.AI_ENABLED:
        try:
            import ai

            ai.set_bot(bot)
        except Exception:
            log.exception("Failed to wire AI agent")
    me = await bot.get_me()
    log.info("Bot @%s (id=%s) started", me.username, me.id)
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(admin_id, f"✅ Бот @{me.username} запущен.")
        except Exception:
            log.info("Could not ping admin %s on startup", admin_id)


async def _on_shutdown(bot: Bot) -> None:
    log.info("Shutting down…")
    _stop_event.set()
    funnel.shutdown()
    await db.close()


def _register_error_handler(dp: Dispatcher) -> None:
    @dp.errors()
    async def global_error_handler(event: ErrorEvent) -> bool:
        log.exception("Unhandled error: %s", event.exception)
        # Try to inform the user without ever raising again.
        update = event.update
        try:
            if update.message:
                await update.message.answer(content.GENERIC_ERROR)
            elif update.callback_query:
                await update.callback_query.answer(
                    content.GENERIC_ERROR, show_alert=False
                )
        except Exception:
            pass
        return True  # mark handled so polling continues


async def main() -> None:
    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_root_router())
    _register_error_handler(dp)

    dp.startup.register(_on_startup)
    dp.shutdown.register(_on_shutdown)

    # Background workers run alongside polling.
    retry_task = asyncio.create_task(lead.retry_worker(_stop_event))
    bridge_task = asyncio.create_task(
        amojo_bridge.run_internal_server(bot, _stop_event)
    )

    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        _stop_event.set()
        for task in (retry_task, bridge_task):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Interrupted, exiting.")
