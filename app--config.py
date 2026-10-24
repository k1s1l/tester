"""Application settings loaded from environment variables (and optional .env file)."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Telegram ---
    bot_token: SecretStr | None = None  # required to run the bot; scripts (import/seed) work without it
    admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    # Chat/channel for order notifications. If empty, every admin from ADMIN_IDS gets a private message.
    admin_chat_id: int | None = None

    # Optional self-hosted Telegram Bot API server (https://github.com/tdlib/telegram-bot-api)
    telegram_api_url: str | None = None
    bot_mode: Literal["polling", "webhook"] = "polling"
    webhook_base_url: str | None = None
    webhook_path: str = "/telegram/webhook"
    webhook_secret: SecretStr | None = None
    host: str = "0.0.0.0"
    port: int = 8080

    # --- Claude ---
    anthropic_api_key: SecretStr | None = None
    claude_model: str = "claude-opus-5-5"
    claude_effort: Literal["low", "medium", "high"] = "low"
    claude_max_tokens: int = 4096
    claude_timeout_seconds: float = 45.0
    claude_max_retries: int = 2
    # Server-side refusal fallback (beta). Disable for models that do not support it (e.g. Haiku).
    claude_fallbacks: bool = True
    ai_requests_per_user_per_hour: int = 60

    # --- Database ---
    database_url: str
    db_pool_size: int = 5
    db_echo: bool = False

    # --- Business rules ---
    shop_name: str = "Sneaker Shop"
    currency: str = "грн"
    reservation_ttl_minutes: int = 24 * 60          # reservation for a new (pending) order
    confirmed_reservation_ttl_hours: int = 72       # extended once an admin confirms the order
    max_items_per_order: int = 1
    min_size: float = 30.0
    max_size: float = 52.0

    # --- Dialog / privacy ---
    history_max_messages: int = 8
    dialog_ttl_hours: int = 24
    data_retention_days: int = 365
    max_photo_mb: float = 5.0

    # --- Anti-flood ---
    rate_limit_per_minute: int = 20

    # --- Background jobs ---
    worker_interval_seconds: int = 30
    notification_max_attempts: int = 12

    log_level: str = "INFO"

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, value: object) -> object:
        if value is None or value == "":
            return []
        if isinstance(value, int):
            return [value]
        if isinstance(value, str):
            return [int(part) for part in value.replace(";", ",").split(",") if part.strip()]
        return value

    @field_validator("admin_chat_id", mode="before")
    @classmethod
    def _empty_chat_to_none(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("bot_token", "anthropic_api_key", "webhook_secret", "webhook_base_url", "telegram_api_url", mode="before")
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("database_url")
    @classmethod
    def _normalize_db_url(cls, value: str) -> str:
        # Railway/Heroku style URLs -> SQLAlchemy asyncpg URL
        if value.startswith("postgres://"):
            value = "postgresql://" + value[len("postgres://"):]
        if value.startswith("postgresql://"):
            value = "postgresql+asyncpg://" + value[len("postgresql://"):]
        return value

    @property
    def admin_notification_chats(self) -> list[int]:
        if self.admin_chat_id is not None:
            return [self.admin_chat_id]
        return list(self.admin_ids)

    @property
    def webhook_url(self) -> str | None:
        if not self.webhook_base_url:
            return None
        return self.webhook_base_url.rstrip("/") + self.webhook_path

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.admin_ids


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
