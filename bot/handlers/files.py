"""Обработчик загрузки файлов: сохраняем в рабочую папку пользователя."""

from __future__ import annotations

import logging
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.types import Document, PhotoSize
from aiogram.types import Message as TgMessage

from bot.handlers.common import AppContext, is_allowed

logger = logging.getLogger(__name__)


MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 МБ


def register_file_handlers(dp: Dispatcher, ctx: AppContext) -> None:
    @dp.message(F.document)
    async def on_document(message: TgMessage, bot: Bot) -> None:
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
            return
        assert message.from_user
        doc: Document = message.document  # type: ignore[assignment]
        if doc.file_size and doc.file_size > MAX_FILE_SIZE:
            await message.answer(f"Файл слишком большой (>{MAX_FILE_SIZE // 1024 // 1024} МБ).")
            return
        wd = ctx.workdir_for(message.from_user.id)
        target = _safe_target(wd, doc.file_name or f"file_{doc.file_id}")
        await bot.download(doc, destination=target)
        await message.answer(
            f"Сохранено: <code>{target.relative_to(wd)}</code> ({target.stat().st_size} байт)\n"
            "Теперь можно сослаться на файл в чате — агент сможет его прочитать.",
            parse_mode="HTML",
        )

    @dp.message(F.photo)
    async def on_photo(message: TgMessage, bot: Bot) -> None:
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
            return
        assert message.from_user and message.photo
        photo: PhotoSize = message.photo[-1]
        wd = ctx.workdir_for(message.from_user.id)
        target = _safe_target(wd, f"photo_{photo.file_unique_id}.jpg")
        await bot.download(photo, destination=target)
        await message.answer(f"Фото сохранено: <code>{target.relative_to(wd)}</code>", parse_mode="HTML")


def _safe_target(workdir: Path, filename: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "._- " else "_" for c in filename).strip() or "file"
    target = workdir / safe
    base, _, ext = safe.rpartition(".")
    counter = 1
    while target.exists():
        if base:
            target = workdir / f"{base}_{counter}.{ext}"
        else:
            target = workdir / f"{safe}_{counter}"
        counter += 1
    return target
