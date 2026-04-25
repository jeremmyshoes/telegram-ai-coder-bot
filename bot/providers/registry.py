"""Реестр поддерживаемых провайдеров и их базовых URL."""

from __future__ import annotations

from dataclasses import dataclass

from bot.providers.anthropic import AnthropicProvider
from bot.providers.base import LLMProvider
from bot.providers.openai_compat import OpenAICompatProvider


@dataclass(frozen=True)
class ProviderPreset:
    id: str
    title: str
    base_url: str | None
    api_key_url: str
    suggested_models: tuple[str, ...]
    notes: str = ""


# Любой провайдер совместимый с OpenAI Chat Completions API.
PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    "openai": ProviderPreset(
        id="openai",
        title="OpenAI",
        base_url=None,  # SDK по умолчанию
        api_key_url="https://platform.openai.com/api-keys",
        suggested_models=(
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4.1",
            "gpt-4.1-mini",
            "o3-mini",
            "o4-mini",
        ),
    ),
    "openrouter": ProviderPreset(
        id="openrouter",
        title="OpenRouter (300+ моделей)",
        base_url="https://openrouter.ai/api/v1",
        api_key_url="https://openrouter.ai/keys",
        suggested_models=(
            "anthropic/claude-sonnet-4",
            "anthropic/claude-3.5-sonnet",
            "openai/gpt-4o",
            "google/gemini-2.5-pro",
            "deepseek/deepseek-chat",
            "meta-llama/llama-3.3-70b-instruct",
            "qwen/qwen-2.5-coder-32b-instruct",
        ),
    ),
    "anthropic": ProviderPreset(
        id="anthropic",
        title="Anthropic Claude",
        base_url=None,
        api_key_url="https://console.anthropic.com/settings/keys",
        suggested_models=(
            "claude-sonnet-4-5-20250929",
            "claude-opus-4-1-20250805",
            "claude-3-5-sonnet-latest",
            "claude-3-5-haiku-latest",
        ),
    ),
    "deepseek": ProviderPreset(
        id="deepseek",
        title="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        api_key_url="https://platform.deepseek.com/api_keys",
        suggested_models=("deepseek-chat", "deepseek-reasoner"),
    ),
    "groq": ProviderPreset(
        id="groq",
        title="Groq (быстрый Llama/Mixtral)",
        base_url="https://api.groq.com/openai/v1",
        api_key_url="https://console.groq.com/keys",
        suggested_models=(
            "llama-3.3-70b-versatile",
            "llama-3.1-70b-versatile",
            "mixtral-8x7b-32768",
        ),
    ),
    "xai": ProviderPreset(
        id="xai",
        title="xAI Grok",
        base_url="https://api.x.ai/v1",
        api_key_url="https://console.x.ai/",
        suggested_models=("grok-4", "grok-3", "grok-3-mini"),
    ),
    "mistral": ProviderPreset(
        id="mistral",
        title="Mistral",
        base_url="https://api.mistral.ai/v1",
        api_key_url="https://console.mistral.ai/api-keys/",
        suggested_models=(
            "mistral-large-latest",
            "codestral-latest",
            "mistral-small-latest",
        ),
    ),
    "together": ProviderPreset(
        id="together",
        title="Together AI",
        base_url="https://api.together.xyz/v1",
        api_key_url="https://api.together.ai/settings/api-keys",
        suggested_models=(
            "meta-llama/Llama-3.3-70B-Instruct-Turbo",
            "Qwen/Qwen2.5-Coder-32B-Instruct",
        ),
    ),
    "google": ProviderPreset(
        id="google",
        title="Google Gemini (OpenAI-compat)",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key_url="https://aistudio.google.com/app/apikey",
        suggested_models=(
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.0-flash",
        ),
    ),
    "custom": ProviderPreset(
        id="custom",
        title="Кастомный (укажите base_url)",
        base_url=None,
        api_key_url="",
        suggested_models=(),
        notes="Используйте для self-hosted/Ollama/LM Studio. Требует base_url.",
    ),
}


def list_presets() -> list[ProviderPreset]:
    return list(PROVIDER_PRESETS.values())


def create_provider(
    provider_id: str,
    *,
    api_key: str,
    base_url: str | None = None,
) -> LLMProvider:
    preset = PROVIDER_PRESETS.get(provider_id)
    effective_base = base_url or (preset.base_url if preset else None)

    if provider_id == "anthropic":
        return AnthropicProvider(api_key=api_key, base_url=effective_base)

    if preset is None and provider_id != "custom":
        raise ValueError(f"Неизвестный провайдер: {provider_id}")

    return OpenAICompatProvider(
        name=provider_id,
        api_key=api_key,
        base_url=effective_base,
    )
