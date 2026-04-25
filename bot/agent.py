"""Агентный цикл: общение с LLM и автоматические вызовы инструментов."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from bot.providers.base import ImageData, LLMProvider, Message, ToolCall
from bot.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


SYSTEM_PROMPT_AGENT = """Ты — Coder, AI-агент в Telegram-боте, аналог opencode/Cursor.

Возможности:
- Работаешь в персональной рабочей директории пользователя (Docker sandbox).
- Можешь выполнять shell-команды (bash), читать/писать/редактировать файлы (read_file, write_file, edit_file, ls).
- Между сообщениями состояние workdir сохраняется.

Правила:
1. Когда задача требует кода/проверки — используй инструменты, не выдумывай результаты.
2. Отвечай по-русски, кратко. Длинные результаты команд не цитируй полностью.
3. Безопасность: не выполняй команды, способные навредить системе хоста (rm -rf /, fork-бомбы и т.п.). Если пользователь просит — предупреди.
4. Перед изменением файла — прочитай его (read_file).
5. После завершения — сообщи итог человеческими словами.
"""

SYSTEM_PROMPT_CHAT = """Ты — полезный AI-ассистент в Telegram-боте.
Отвечай ясно и по делу. Если нужен код — оформляй в markdown ```код```.
Tool-инструменты выключены — не предлагай их использовать.
"""


@dataclass
class AgentEvent:
    """Событие, которое можно показать пользователю в Telegram."""

    kind: str  # 'thinking' | 'tool_call' | 'tool_result' | 'final' | 'error'
    text: str
    extra: dict[str, Any] = field(default_factory=dict)


EventCallback = Callable[[AgentEvent], Awaitable[None]]


@dataclass
class AgentResult:
    final_text: str
    new_messages: list[Message]  # сообщения, которые нужно сохранить в историю
    iterations: int


class Agent:
    def __init__(
        self,
        *,
        provider: LLMProvider,
        model: str,
        tools: ToolRegistry | None,
        max_iterations: int = 20,
        temperature: float = 0.7,
    ) -> None:
        self.provider = provider
        self.model = model
        self.tools = tools
        self.max_iterations = max_iterations
        self.temperature = temperature

    async def run(
        self,
        history: list[Message],
        user_message: str,
        *,
        on_event: EventCallback | None = None,
        system_prompt: str | None = None,
        images: list[ImageData] | None = None,
    ) -> AgentResult:
        sys_text = system_prompt or (SYSTEM_PROMPT_AGENT if self.tools else SYSTEM_PROMPT_CHAT)

        user_msg = Message(role="user", content=user_message, images=list(images or []))
        messages: list[Message] = [Message(role="system", content=sys_text)]
        messages.extend(history)
        messages.append(user_msg)

        # В историю сохраняем только текст (изображения через JSON не сериализуются)
        new_messages: list[Message] = [Message(role="user", content=user_message)]
        tool_defs = self.tools.definitions() if self.tools else None

        for iteration in range(1, self.max_iterations + 1):
            logger.debug("agent iteration %d / %d", iteration, self.max_iterations)
            if on_event:
                # В chat-режиме (без инструментов) итерация всегда одна — нет смысла
                # показывать «Итерация N…», это сбивает с толку.
                if self.tools is None:
                    await on_event(AgentEvent(kind="thinking", text="⏳ Думаю…"))
                else:
                    await on_event(AgentEvent(kind="thinking", text=f"Итерация {iteration}…"))

            response = await self.provider.complete(
                messages=messages,
                model=self.model,
                tools=tool_defs,
                temperature=self.temperature,
            )

            assistant_msg = Message(
                role="assistant",
                content=response.content,
                tool_calls=list(response.tool_calls),
            )
            messages.append(assistant_msg)
            new_messages.append(assistant_msg)

            if not response.tool_calls:
                final = (response.content or "").strip()
                if on_event:
                    await on_event(AgentEvent(kind="final", text=final))
                return AgentResult(final_text=final, new_messages=new_messages, iterations=iteration)

            if self.tools is None:
                # Модель попыталась вызвать инструмент, но они отключены.
                fallback = (response.content or "").strip() or "(модель попыталась вызвать инструменты, но режим chat без них)"
                if on_event:
                    await on_event(AgentEvent(kind="final", text=fallback))
                return AgentResult(final_text=fallback, new_messages=new_messages, iterations=iteration)

            for tc in response.tool_calls:
                await self._execute_tool(tc, messages, new_messages, on_event)

        # лимит исчерпан
        warning = "Достигнут лимит итераций агента."
        if on_event:
            await on_event(AgentEvent(kind="error", text=warning))
        return AgentResult(final_text=warning, new_messages=new_messages, iterations=self.max_iterations)

    async def _execute_tool(
        self,
        tc: ToolCall,
        messages: list[Message],
        new_messages: list[Message],
        on_event: EventCallback | None,
    ) -> None:
        assert self.tools is not None
        if on_event:
            await on_event(
                AgentEvent(
                    kind="tool_call",
                    text=f"`{tc.name}` {_short_args(tc.arguments)}",
                    extra={"tool": tc.name, "args": tc.arguments},
                )
            )

        result_text = await self.tools.call(tc.name, tc.arguments)

        if on_event:
            await on_event(
                AgentEvent(
                    kind="tool_result",
                    text=_truncate(result_text, 800),
                    extra={"tool": tc.name},
                )
            )

        tool_msg = Message(
            role="tool",
            content=result_text,
            tool_call_id=tc.id,
            name=tc.name,
        )
        messages.append(tool_msg)
        new_messages.append(tool_msg)


def _short_args(args: dict[str, Any], limit: int = 200) -> str:
    parts = []
    for k, v in args.items():
        sv = str(v).replace("\n", " ")
        if len(sv) > 60:
            sv = sv[:57] + "..."
        parts.append(f"{k}={sv}")
    s = ", ".join(parts)
    if len(s) > limit:
        s = s[: limit - 3] + "..."
    return s


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
