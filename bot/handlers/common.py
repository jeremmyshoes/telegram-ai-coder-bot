"""Общие утилиты для handlers."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.types import BufferedInputFile
from aiogram.types import Message as TgMessage

from bot.config import Settings
from bot.crypto import KeyVault
from bot.db import Database
from bot.providers import (
    PROVIDER_PRESETS,
    LLMProvider,
    Message,
    create_provider,
)

logger = logging.getLogger(__name__)


TELEGRAM_LIMIT = 4000  # запас от 4096


class AppContext:
    """Контейнер общих зависимостей, прокидываемый в handlers."""

    def __init__(self, *, settings: Settings, db: Database, vault: KeyVault) -> None:
        self.settings = settings
        self.db = db
        self.vault = vault

    def workdir_for(self, user_id: int) -> Path:
        wd = self.settings.workspaces_dir / str(user_id)
        wd.mkdir(parents=True, exist_ok=True)
        return wd

    async def get_provider_for(self, user_id: int) -> tuple[LLMProvider, str] | None:
        """Возвращает (provider, model) или None если ключ/модель не настроены."""
        user = await self.db.ensure_user(user_id)
        if not user.provider or not user.model:
            return None
        key_row = await self.db.get_key(user_id, user.provider)
        if key_row is None:
            return None
        try:
            api_key = self.vault.decrypt(key_row.encrypted)
        except RuntimeError:
            return None
        provider = create_provider(
            user.provider,
            api_key=api_key,
            base_url=key_row.base_url,
        )
        return provider, user.model

    async def load_history(self, user_id: int) -> list[Message]:
        rows = await self.db.get_history(user_id, limit=self.settings.max_history_messages)
        return [_message_from_dict(r) for r in rows]

    async def save_messages(self, user_id: int, msgs: list[Message]) -> None:
        for m in msgs:
            await self.db.append_message(user_id, m.role, _message_to_dict(m))


def _message_to_dict(m: Message) -> dict[str, Any]:
    out: dict[str, Any] = {"role": m.role}
    if m.content is not None:
        out["content"] = m.content
    if m.tool_calls:
        out["tool_calls"] = [
            {"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in m.tool_calls
        ]
    if m.tool_call_id is not None:
        out["tool_call_id"] = m.tool_call_id
    if m.name is not None:
        out["name"] = m.name
    return out


def _message_from_dict(d: dict[str, Any]) -> Message:
    from bot.providers.base import ToolCall

    tcs = [
        ToolCall(id=t["id"], name=t["name"], arguments=t.get("arguments") or {})
        for t in d.get("tool_calls") or []
    ]
    return Message(
        role=d["role"],
        content=d.get("content"),
        tool_calls=tcs,
        tool_call_id=d.get("tool_call_id"),
        name=d.get("name"),
    )


def is_allowed(settings: Settings, user_id: int | None) -> bool:
    if user_id is None:
        return False
    return settings.is_user_allowed(user_id)


async def send_long(message: TgMessage, text: str, *, parse_mode: str | None = None) -> None:
    """Шлёт длинный текст частями (до 4000 символов)."""
    if not text:
        return
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= TELEGRAM_LIMIT:
            chunks.append(remaining)
            break
        # пытаемся резать по \n
        cut = remaining.rfind("\n", 0, TELEGRAM_LIMIT)
        if cut <= 0:
            cut = TELEGRAM_LIMIT
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    for chunk in chunks:
        try:
            await message.answer(chunk, parse_mode=parse_mode)
        except Exception:  # noqa: BLE001
            await message.answer(chunk)


def provider_titles() -> str:
    out = []
    for p in PROVIDER_PRESETS.values():
        out.append(f"• <code>{p.id}</code> — {p.title}")
    return "\n".join(out)


async def send_file(bot: Bot, chat_id: int, filename: str, content: str) -> None:
    data = content.encode("utf-8")
    await bot.send_document(chat_id, BufferedInputFile(data, filename=filename))


def pretty_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)
