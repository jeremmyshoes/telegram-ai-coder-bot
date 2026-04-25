"""Конфигурация приложения через переменные окружения."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    telegram_bot_token: str = Field(..., description="Токен у @BotFather")
    encryption_key: str = Field(..., description="Fernet ключ для шифрования API-ключей")

    allowed_user_ids: str = Field(default="", description="Список user_id через запятую")
    admin_user_ids: str = Field(default="", description="Список user_id админов")

    data_dir: Path = Field(default=Path("./data"))
    db_path: Path = Field(default=Path("./data/bot.db"))
    workspaces_dir: Path = Field(default=Path("./data/workspaces"))

    sandbox_timeout: int = 60
    sandbox_max_output: int = 8000
    max_agent_iterations: int = 20
    max_history_messages: int = 40

    image_model: str = Field(
        default="dall-e-3",
        description="Модель OpenAI Images API для /img (dall-e-3 / dall-e-2 / gpt-image-1)",
    )
    image_size: str = Field(
        default="1024x1024",
        description="Размер по умолчанию для /img (например 1024x1024, 1024x1792, 1792x1024)",
    )

    google_search_api_key: str = Field(
        default="",
        description="API key из Google Cloud Console (для Custom Search JSON API).",
    )
    google_search_cse_id: str = Field(
        default="",
        description="ID поискового движка из programmablesearchengine.google.com.",
    )

    log_level: str = "INFO"

    @property
    def allowed_user_ids_set(self) -> set[int]:
        return _parse_ids(self.allowed_user_ids)

    @property
    def admin_user_ids_set(self) -> set[int]:
        return _parse_ids(self.admin_user_ids)

    def is_user_allowed(self, user_id: int) -> bool:
        allowed = self.allowed_user_ids_set
        if not allowed:
            return True
        return user_id in allowed or user_id in self.admin_user_ids_set


def _parse_ids(raw: str) -> set[int]:
    out: set[int] = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.add(int(chunk))
        except ValueError:
            continue
    return out


def load_settings() -> Settings:
    settings = Settings()  # type: ignore[call-arg]
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.workspaces_dir.mkdir(parents=True, exist_ok=True)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    return settings
