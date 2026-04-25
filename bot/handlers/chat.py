"""Обработчик обычных текстовых сообщений → агент."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress

from aiogram import Dispatcher, F
from aiogram.types import Message as TgMessage

from bot.agent import Agent, AgentEvent
from bot.handlers.common import AppContext, is_allowed, send_long
from bot.providers.base import ProviderError
from bot.tools import build_tool_registry
from bot.tools.sandbox import build_sandbox

logger = logging.getLogger(__name__)


def register_chat_handlers(dp: Dispatcher, ctx: AppContext) -> None:
    @dp.message(F.text & ~F.text.startswith("/"))
    async def on_message(message: TgMessage) -> None:
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
            await message.answer("Доступ запрещён.")
            return
        assert message.from_user and message.text

        provider_data = await ctx.get_provider_for(message.from_user.id)
        if provider_data is None:
            await message.answer(
                "Не настроены провайдер/модель/ключ. Используйте /help."
            )
            return
        provider, model = provider_data
        user_settings = await ctx.db.ensure_user(message.from_user.id)
        history = await ctx.load_history(message.from_user.id)

        if user_settings.mode == "agent":
            workdir = ctx.workdir_for(message.from_user.id)
            sandbox = build_sandbox(workdir, max_output=ctx.settings.sandbox_max_output)
            tools = build_tool_registry(
                sandbox=sandbox,
                sandbox_timeout=ctx.settings.sandbox_timeout,
            )
        else:
            tools = None

        agent = Agent(
            provider=provider,
            model=model,
            tools=tools,
            max_iterations=ctx.settings.max_agent_iterations,
        )

        # Промежуточные обновления — редактируем единое сообщение, чтобы не флудить.
        progress = await message.answer("⏳ Думаю…")
        last_edit = 0.0
        log_lines: list[str] = []

        async def on_event(ev: AgentEvent) -> None:
            nonlocal last_edit
            if ev.kind == "thinking":
                line = f"💭 {ev.text}"
            elif ev.kind == "tool_call":
                line = f"🛠 {ev.text}"
            elif ev.kind == "tool_result":
                line = f"   → {ev.text[:200]}"
            elif ev.kind == "error":
                line = f"⚠ {ev.text}"
            elif ev.kind == "final":
                return  # финал отдаём отдельным сообщением
            else:
                line = ev.text
            log_lines.append(line)
            now = time.monotonic()
            if now - last_edit < 1.2:
                return
            last_edit = now
            text = "\n".join(log_lines[-12:])
            with suppress(Exception):
                await progress.edit_text(text[-3500:])

        try:
            result = await agent.run(history, message.text, on_event=on_event)
        except ProviderError as exc:
            await progress.edit_text(f"⚠ Ошибка провайдера: {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("agent failed")
            await progress.edit_text(f"⚠ Ошибка: {exc}")
            return

        # Финальный лог
        with suppress(Exception):
            await progress.edit_text("\n".join(log_lines[-12:])[-3500:] or "Готово.")

        await ctx.save_messages(message.from_user.id, result.new_messages)

        final = result.final_text or "(пустой ответ)"
        await send_long(message, final)

    # Гарантия, что мы не блокируем event loop в случае одновременных сообщений
    _ = asyncio  # noqa: SLF001
