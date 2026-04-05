"""
bot/config.py
~~~~~~~~~~~~~
Application configuration loaded from environment variables / .env file.

Uses pydantic-settings so all fields are validated at startup and type-safe
throughout the rest of the codebase.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime configuration for the bot, sourced from the environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    telegram_bot_token: str = Field(
        default="",
        description="Telegram Bot API token obtained from @BotFather.",
    )

    google_cloud_api_key: str = Field(
        default="",
        description="Google Cloud API key for Vertex AI (primary). Can also use GOOGLE_CLOUD_API_KEY_1.",
    )
    google_cloud_api_key_1: str = Field(
        default="",
        description="Google Cloud API key #1.",
    )
    google_cloud_api_key_2: str = Field(
        default="",
        description="Google Cloud API key #2.",
    )
    google_cloud_api_key_3: str = Field(
        default="",
        description="Google Cloud API key #3.",
    )

    vertex_ai_model: str = Field(
        default="gemini-3.1-flash-image-preview",
        description="The default image-generation model to use.",
    )

    max_concurrent_requests: int = Field(
        default=9,
        ge=1,
        le=30,
        description="Maximum simultaneous Vertex AI API requests (semaphore size).",
    )
    max_retry_attempts: int = Field(
        default=5,
        ge=1,
        le=10,
        description="Maximum number of retries on transient / quota errors.",
    )

    @field_validator("telegram_bot_token")
    @classmethod
    def token_not_placeholder(cls, v: str) -> str:
        if v == "your_telegram_bot_token_here":
            raise ValueError(
                "TELEGRAM_BOT_TOKEN is still set to the placeholder value. "
                "Replace it with your real token from @BotFather."
            )
        return v

    def get_api_keys(self) -> list[str]:
        keys: list[str] = []
        for k in [self.google_cloud_api_key_1, self.google_cloud_api_key_2, self.google_cloud_api_key_3]:
            if k.strip():
                keys.append(k.strip())
        if not keys and self.google_cloud_api_key.strip():
            keys.append(self.google_cloud_api_key.strip())
        return keys


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton Settings instance (cached after first call)."""
    return Settings()
