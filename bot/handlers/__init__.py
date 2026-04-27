"""Telegram handlers."""

from bot.handlers.chat import register_chat_handlers
from bot.handlers.commands import register_command_handlers
from bot.handlers.files import register_file_handlers
from bot.handlers.voice import register_voice_handlers
from bot.handlers.yt import register_yt_handlers

__all__ = [
    "register_command_handlers",
    "register_chat_handlers",
    "register_file_handlers",
    "register_voice_handlers",
    "register_yt_handlers",
]
