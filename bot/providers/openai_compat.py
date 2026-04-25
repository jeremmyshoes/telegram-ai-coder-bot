"""Универсальный OpenAI-совместимый клиент (OpenAI / OpenRouter / DeepSeek / Groq / xAI / Mistral / Together / Gemini-OAI / любой кастомный baseURL)."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

from openai import APIError, AsyncOpenAI

from bot.providers.base import (
    LLMResponse,
    Message,
    ProviderError,
    ToolCall,
    ToolDefinition,
)


@dataclass
class GeneratedImage:
    data: bytes
    mime: str = "image/png"
    revised_prompt: str | None = None


# Префиксы моделей OpenAI, которые поддерживают только дефолтный temperature=1
# и используют max_completion_tokens вместо max_tokens (reasoning-семейство).
_REASONING_MODEL_PREFIXES: tuple[str, ...] = (
    "o1",
    "o3",
    "o4",
    "gpt-5",
)


def _is_reasoning_model(model: str) -> bool:
    name = model.lower()
    return any(name.startswith(p) for p in _REASONING_MODEL_PREFIXES)


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
        reasoning = _is_reasoning_model(model)
        payload: dict[str, Any] = {
            "model": model,
            "messages": [m.to_dict() for m in messages],
        }
        if not reasoning:
            payload["temperature"] = temperature
        if max_tokens is not None:
            if reasoning:
                payload["max_completion_tokens"] = max_tokens
            else:
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

    async def generate_image(
        self,
        *,
        prompt: str,
        model: str = "dall-e-3",
        size: str = "1024x1024",
        quality: str | None = None,
        n: int = 1,
    ) -> list[GeneratedImage]:
        """Генерация изображения. Поддерживает OpenAI-совместимые images API
        (OpenAI: dall-e-2, dall-e-3, gpt-image-1)."""
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "size": size,
            "n": n,
        }
        # gpt-image-1 всегда возвращает b64; dall-e-* поддерживают response_format
        if not model.startswith("gpt-image"):
            payload["response_format"] = "b64_json"
        if quality is not None:
            payload["quality"] = quality

        try:
            resp = await self._client.images.generate(**payload)
        except APIError as exc:
            raise ProviderError(f"{self.name}: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"{self.name}: {exc}") from exc

        images: list[GeneratedImage] = []
        for item in resp.data or []:
            b64 = getattr(item, "b64_json", None)
            url = getattr(item, "url", None)
            revised = getattr(item, "revised_prompt", None)
            if b64:
                try:
                    raw = base64.b64decode(b64)
                except Exception as exc:  # noqa: BLE001
                    raise ProviderError(f"{self.name}: bad base64 in image response: {exc}") from exc
                images.append(GeneratedImage(data=raw, revised_prompt=revised))
            elif url:
                # fallback: скачать сами через httpx
                try:
                    import httpx
                except ImportError as exc:
                    raise ProviderError(
                        f"{self.name}: ответ только URL, нужен httpx для скачивания"
                    ) from exc
                try:
                    async with httpx.AsyncClient(timeout=60.0) as http:
                        r = await http.get(url)
                        r.raise_for_status()
                        images.append(GeneratedImage(data=r.content, revised_prompt=revised))
                except Exception as exc:  # noqa: BLE001
                    raise ProviderError(
                        f"{self.name}: не удалось скачать изображение по URL: {exc}"
                    ) from exc
            else:
                raise ProviderError(f"{self.name}: пустой image response без b64_json/url")
        if not images:
            raise ProviderError(f"{self.name}: нет изображений в ответе API")
        return images
