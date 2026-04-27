"""Точка входа. Запуск: python -m bot"""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from dotenv import load_dotenv

from bot.config import load_settings
from bot.crypto import KeyVault
from bot.db import Database
from bot.handlers import (
    register_chat_handlers,
    register_command_handlers,
    register_file_handlers,
    register_voice_handlers,
    register_yt_handlers,
)
from bot.handlers.audit import install_audit
from bot.handlers.common import AppContext
from bot.handlers.keyboards import ADMIN_BOT_COMMANDS, USER_BOT_COMMANDS

logger = logging.getLogger(__name__)


async def main() -> None:
    load_dotenv()
    settings = load_settings()

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("Запуск бота…")

    db = Database(settings.db_path)
    await db.connect()

    vault = KeyVault(settings.encryption_key)

    ctx = AppContext(settings=settings, db=db, vault=vault)

    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    # Порядок важен. В aiogram 3 хендлеры проверяются в порядке регистрации
    # — первое совпадение выигрывает. /yt должен быть зарегистрирован ДО
    # register_command_handlers, потому что внутри последнего в самом
    # конце есть catch-all `F.text.startswith("/")`, который иначе
    # перехватит /yt и ответит «Неизвестная команда».
    register_yt_handlers(dp, ctx)
    register_voice_handlers(dp, ctx)
    register_command_handlers(dp, ctx)
    register_file_handlers(dp, ctx)
    register_chat_handlers(dp, ctx)

    # Audit-логирование в LOG_CHAT_ID (если задан) — должно ставиться
    # ПОСЛЕ всех register_* (outer_middleware на dp.message пишется в общую
    # очередь, порядок не критичен, но мы хотим что бы наш middleware
    # увидел любые сообщения, в т.ч. дошедшие до cmd_unknown).
    install_audit(dp, bot, settings.log_chat_id)

    try:
        me = await bot.get_me()
        logger.info("Бот @%s готов к работе", me.username)
        # Регистрируем команды в меню Telegram (отображается при клике "/").
        # Дефолт — короткий список для обычных юзеров. Админам — расширенный
        # через scope=BotCommandScopeChat.
        try:
            from aiogram.types import BotCommandScopeChat

            await bot.set_my_commands(USER_BOT_COMMANDS)
            for admin_id in settings.admin_user_ids_set:
                with __import__("contextlib").suppress(Exception):
                    await bot.set_my_commands(
                        ADMIN_BOT_COMMANDS,
                        scope=BotCommandScopeChat(chat_id=admin_id),
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось зарегистрировать команды в меню: %s", exc)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
