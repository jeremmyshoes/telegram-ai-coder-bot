"""Универсальный OpenAI-совместимый клиент (OpenAI / OpenRouter / DeepSeek / Groq / xAI / Mistral / Together / Gemini-OAI / любой кастомный baseURL)."""

from __future__ import annotations

import json
from typing import Any

from openai import APIError, AsyncOpenAI

from bot.providers.base import (
    LLMResponse,
    Message,
    ProviderError,
    ToolCall,
    ToolDefinition,
)


class OpenAICompatProvider:
    """Любой провайдер с OpenAI-совместимым `/chat/completions`."""

    def __init__(self, *, name: str, api_key: str, base_url: str | None = None) -> None:
        self.name = name
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def complete(
        self,
        *,
        messages: list[Message],
        model: str,
        tools: list[ToolDefinition] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [m.to_dict() for m in messages],
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = [t.to_openai() for t in tools]
            payload["tool_choice"] = "auto"

        try:
            resp = await self._client.chat.completions.create(**payload)
        except APIError as exc:
            raise ProviderError(f"{self.name}: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"{self.name}: {exc}") from exc

        choice = resp.choices[0]
        msg = choice.message

        tool_calls: list[ToolCall] = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_raw": tc.function.arguments}
            tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))

        return LLMResponse(
            content=msg.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason,
            raw=resp.model_dump() if hasattr(resp, "model_dump") else None,
        )

    async def list_models(self) -> list[str]:
        try:
            page = await self._client.models.list()
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"{self.name}: не удалось получить список моделей: {exc}") from exc
        models = []
        for m in page.data:
            mid = getattr(m, "id", None)
            if mid:
                models.append(mid)
        return sorted(models)
