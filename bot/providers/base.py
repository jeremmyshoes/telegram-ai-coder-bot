"""Базовый интерфейс LLM-провайдеров (унифицирован под формат OpenAI tool-calling)."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any, Protocol


class ProviderError(Exception):
    """Ошибка провайдера / API."""


@dataclass
class ImageData:
    """Изображение, прикладываемое к user-сообщению (multimodal vision)."""

    data: bytes
    mime: str = "image/jpeg"

    def to_data_url(self) -> str:
        b64 = base64.b64encode(self.data).decode("ascii")
        return f"data:{self.mime};base64,{b64}"


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON-схема параметров

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Message:
    role: str  # 'system' | 'user' | 'assistant' | 'tool'
    content: str | None = None
    images: list[ImageData] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None  # для role='tool'
    name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role}
        if self.images and self.role == "user":
            parts: list[dict[str, Any]] = []
            if self.content:
                parts.append({"type": "text", "text": self.content})
            for img in self.images:
                parts.append({"type": "image_url", "image_url": {"url": img.to_data_url()}})
            out["content"] = parts
        elif self.content is not None:
            out["content"] = self.content
        if self.tool_calls:
            out["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in self.tool_calls
            ]
        if self.tool_call_id is not None:
            out["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            out["name"] = self.name
        return out


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall]
    raw: dict[str, Any] | None = None
    finish_reason: str | None = None


class LLMProvider(Protocol):
    name: str

    async def complete(
        self,
        *,
        messages: list[Message],
        model: str,
        tools: list[ToolDefinition] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> LLMResponse: ...

    async def list_models(self) -> list[str]: ...
