"""Anthropic Claude провайдер с адаптацией под формат OpenAI tool-calling."""

from __future__ import annotations

import base64
from typing import Any

from anthropic import APIError, AsyncAnthropic

from bot.providers.base import (
    LLMResponse,
    Message,
    ProviderError,
    ToolCall,
    ToolDefinition,
)

# Известные модели Claude (актуальный список лучше получать через API/доку, но
# Anthropic не отдаёт стабильный listing — фиксируем разумный набор).
_KNOWN_MODELS = [
    "claude-opus-4-1-20250805",
    "claude-opus-4-20250514",
    "claude-sonnet-4-5-20250929",
    "claude-sonnet-4-20250514",
    "claude-3-7-sonnet-latest",
    "claude-3-5-sonnet-latest",
    "claude-3-5-haiku-latest",
    "claude-3-haiku-20240307",
]


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, *, api_key: str, base_url: str | None = None) -> None:
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = AsyncAnthropic(**kwargs)

    async def complete(
        self,
        *,
        messages: list[Message],
        model: str,
        tools: list[ToolDefinition] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        system_chunks: list[str] = []
        anthro_msgs: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                if m.content:
                    system_chunks.append(m.content)
                continue
            if m.role == "tool":
                anthro_msgs.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": m.tool_call_id or "",
                                "content": m.content or "",
                            }
                        ],
                    }
                )
                continue
            if m.role == "assistant":
                blocks: list[dict[str, Any]] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                for tc in m.tool_calls:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        }
                    )
                if blocks:
                    anthro_msgs.append({"role": "assistant", "content": blocks})
                continue
            # default: user
            if m.images:
                blocks: list[dict[str, Any]] = []
                for img in m.images:
                    blocks.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": img.mime,
                                "data": base64.b64encode(img.data).decode("ascii"),
                            },
                        }
                    )
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                anthro_msgs.append({"role": "user", "content": blocks})
            else:
                anthro_msgs.append({"role": "user", "content": m.content or ""})

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": anthro_msgs,
            "max_tokens": max_tokens or 4096,
            "temperature": temperature,
        }
        if system_chunks:
            kwargs["system"] = "\n\n".join(system_chunks)
        if tools:
            kwargs["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters,
                }
                for t in tools
            ]

        try:
            resp = await self._client.messages.create(**kwargs)
        except APIError as exc:
            raise ProviderError(f"anthropic: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"anthropic: {exc}") from exc

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in resp.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(getattr(block, "text", "") or "")
            elif btype == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=getattr(block, "id", ""),
                        name=getattr(block, "name", ""),
                        arguments=dict(getattr(block, "input", {}) or {}),
                    )
                )

        content = "\n".join(text_parts).strip() or None
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=getattr(resp, "stop_reason", None),
        )

    async def list_models(self) -> list[str]:
        return list(_KNOWN_MODELS)
