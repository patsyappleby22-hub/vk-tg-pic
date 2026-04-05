"""
core/exceptions.py
~~~~~~~~~~~~~~~~~~
Custom exception hierarchy for the Telegram image-generation bot.

All domain-specific errors should subclass BotError so that top-level
handlers can catch them consistently.
"""

from __future__ import annotations


class BotError(Exception):
    """Base class for all bot-specific errors."""

    def __init__(self, message: str, user_message: str | None = None) -> None:
        super().__init__(message)
        # user_message is what gets sent back to the Telegram user.
        # If not provided, the internal message is used (may be technical).
        self.user_message: str = user_message or message


class VertexAIError(BotError):
    """Raised when the Vertex AI / Google Gen AI API returns an error."""


class QuotaExceededError(VertexAIError):
    """Raised when Google API quota / rate-limits are exhausted after retries."""

    def __init__(self) -> None:
        super().__init__(
            message="Vertex AI quota exceeded after maximum retries.",
            user_message=(
                "Сервис генерации сейчас перегружен 😔\n\n"
                "Попробуйте через пару минут или переключитесь на другую модель "
                "в ⚙️ <b>Настройках</b> — возможно, она сейчас свободна."
            ),
        )


class SafetyFilterError(VertexAIError):
    """Raised when the prompt or response is blocked by safety filters."""

    def __init__(self, detail: str = "") -> None:
        base = "The prompt was blocked by Google's safety filters."
        user_msg = (
            "Ваш запрос был заблокирован, так как он может нарушать политику безопасности контента. "
            "Пожалуйста, переформулируйте промпт и попробуйте снова."
        )
        super().__init__(message=base, user_message=user_msg)


class GenerationError(VertexAIError):
    """Raised for unexpected errors during image generation."""

    def __init__(self, detail: str = "") -> None:
        msg = f"Image generation failed: {detail}" if detail else "Image generation failed."
        super().__init__(
            message=msg,
            user_message=(
                "Не удалось сгенерировать изображение 😔\n\n"
                "Попробуйте ещё раз, измените промпт или переключитесь "
                "на другую модель в ⚙️ <b>Настройках</b>."
            ),
        )


class AmbiguousPromptError(GenerationError):
    """Raised when the model returned text instead of an image (prompt too ambiguous)."""

    def __init__(self, detail: str = "") -> None:
        msg = f"Image generation failed: {detail}" if detail else "Image generation failed."
        BotError.__init__(
            self,
            message=msg,
            user_message=(
                "⚠️ <b>Модель не смогла понять запрос</b> — вернула текст вместо картинки.\n\n"
                "Попробуйте описать подробнее, например:\n"
                "<i>«Портрет Петра Великого в царских одеждах»</i>"
            ),
        )


class ConfigurationError(BotError):
    """Raised when the bot configuration is invalid or incomplete."""
