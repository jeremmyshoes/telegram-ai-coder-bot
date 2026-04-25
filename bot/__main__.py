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
)
from bot.handlers.common import AppContext

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

    # Порядок важен: сначала команды (чтобы не перехватывал общий chat handler), потом файлы, потом chat
    register_command_handlers(dp, ctx)
    register_file_handlers(dp, ctx)
    register_chat_handlers(dp, ctx)

    try:
        me = await bot.get_me()
        logger.info("Бот @%s готов к работе", me.username)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
