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

# Провайдеры с поддержкой OpenAI Images API (`/v1/images/generations`).
# Используется в find_image_key(), чтобы не пытаться генерить картинки на
# чисто-чат провайдерах вроде groq/cerebras/deepseek.
IMAGE_CAPABLE_PROVIDERS: tuple[str, ...] = ("openai", "acedata", "custom")


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

    async def _build_provider_for_user(
        self, user_id: int
    ) -> tuple[LLMProvider, str] | None:
        """Строит (provider, model) только из персональных настроек user_id.
        Возвращает None если у пользователя нет своего provider/model или ключа.
        """
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

    def _fallback_admin_ids(self) -> list[int]:
        """Список user_id, чьи настройки используются как «общие».

        Логика: командные настройки (provider/key/model) теперь меняет только
        админ. Чтобы non-admin (например, сестра) тоже мог пользоваться ботом,
        при отсутствии собственного ключа/модели подставляются настройки
        админа.
        """
        admins = self.settings.admin_user_ids_set
        if admins:
            return sorted(admins)
        # ADMIN_USER_IDS не задан → все allowed-юзеры считаются админами.
        # Берём первого по порядку из ALLOWED_USER_IDS как «главного».
        return sorted(self.settings.allowed_user_ids_set)

    async def get_provider_for(self, user_id: int) -> tuple[LLMProvider, str] | None:
        """Возвращает (provider, model) для запроса от user_id.

        Сначала пробуем персональные настройки. Если их нет — fallback
        на настройки администратора, чтобы обычные allowed-юзеры могли
        пользоваться ботом без собственного /setkey.
        """
        own = await self._build_provider_for_user(user_id)
        if own is not None:
            return own
        for admin_id in self._fallback_admin_ids():
            if admin_id == user_id:
                continue
            shared = await self._build_provider_for_user(admin_id)
            if shared is not None:
                return shared
        return None

    async def find_image_key(
        self, user_id: int, explicit_provider: str | None = None
    ) -> tuple[Any, str] | None:
        """Подбирает (key_row, provider_name) для команды /img.

        Сначала смотрит персональные ключи user_id, потом — ключи
        администратора (fallback для не-админов).

        При отсутствии явного `-p`: предпочитаем провайдеров, у которых
        реально есть Images API (openai, acedata). Текущий провайдер юзера
        используется только если он входит в этот список — иначе мы бы
        ходили в `/v1/images/generations` на Groq/Deepseek/Cerebras и
        получали 404. Anthropic тоже исключён.

        Возвращает None если нигде ничего не нашлось.
        """
        candidates: list[int] = [user_id]
        for admin_id in self._fallback_admin_ids():
            if admin_id != user_id and admin_id not in candidates:
                candidates.append(admin_id)

        for uid in candidates:
            if explicit_provider:
                if explicit_provider == "anthropic":
                    return None
                key_row = await self.db.get_key(uid, explicit_provider)
                if key_row is not None:
                    return key_row, explicit_provider
                continue
            user = await self.db.ensure_user(uid)
            # 1. Текущий провайдер юзера, если он умеет картинки.
            if user.provider in IMAGE_CAPABLE_PROVIDERS:
                k = await self.db.get_key(uid, user.provider)
                if k is not None:
                    return k, user.provider
            # 2. Прямой перебор известных image-capable провайдеров.
            for prov in IMAGE_CAPABLE_PROVIDERS:
                if prov == user.provider:
                    continue  # уже пробовали выше
                k = await self.db.get_key(uid, prov)
                if k is not None:
                    return k, prov
        return None

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


def is_admin(settings: Settings, user_id: int | None) -> bool:
    """Юзер считается админом если:
    - ADMIN_USER_IDS задан → строго проверяем по этому списку.
    - ADMIN_USER_IDS пуст → все allowed-юзеры являются админами
      (поведение по умолчанию, чтобы юзер мог настраивать бота
      сразу после установки без отдельной правки .env).
    """
    if user_id is None:
        return False
    admins = settings.admin_user_ids_set
    if admins:
        return user_id in admins
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


def _split_for_telegram(text: str) -> list[str]:
    """Режет текст на куски ≤ TELEGRAM_LIMIT символов, по \\n когда возможно."""
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= TELEGRAM_LIMIT:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, TELEGRAM_LIMIT)
        if cut <= 0:
            cut = TELEGRAM_LIMIT
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    return chunks


async def send_llm_response(message: TgMessage, text: str) -> None:
    """Отправляет ответ LLM, конвертируя Markdown в Telegram HTML.

    Большинство моделей возвращают `**жирный**`, `*курсив*`, ``` `code` ```,
    тройные бэктики и т.п. — Telegram без parse_mode эту разметку не понимает,
    поэтому пользователь видит сырые звёздочки. Здесь делаем безопасную
    конвертацию и шлём с parse_mode='HTML'. На случай если конкретный chunk
    после конвертации даст невалидный HTML (Telegram очень капризен), для
    этого chunk'а шлём оригинальный markdown-текст без parse_mode.
    """
    from bot.handlers.markdown import md_to_telegram_html

    if not text:
        return

    raw_chunks = _split_for_telegram(text)
    for raw in raw_chunks:
        html = md_to_telegram_html(raw)
        try:
            await message.answer(html, parse_mode="HTML")
        except Exception:  # noqa: BLE001
            # HTML-парсер Telegram отказался — шлём исходный markdown-кусок.
            try:
                await message.answer(raw)
            except Exception:  # noqa: BLE001
                logger.exception("send_llm_response failed for chunk len=%d", len(raw))


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
