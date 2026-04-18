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
                "Все ключи сейчас заняты или перегружены 😔\n\n"
                "Попробуйте через минуту или переключитесь на другую модель "
                "в ⚙️ <b>Настройках</b> — возможно, она сейчас свободна."
            ),
        )


class SafetyFilterError(VertexAIError):
    """Raised when the prompt or response is blocked by safety filters."""

    _REASON_MAP = {
        "hate": "разжигание ненависти",
        "harassment": "оскорбления или травля",
        "sexual": "откровенный контент",
        "dangerous": "опасный контент",
        "violence": "насилие",
        "prohibited": "запрещённый контент",
    }

    def __init__(self, detail: str = "") -> None:
        base = "The prompt was blocked by Google's safety filters."
        reason = self._extract_reason(detail)
        if reason:
            user_msg = (
                f"🚫 <b>Запрос заблокирован фильтрами безопасности Google</b>\n\n"
                f"Причина: <i>{reason}</i>\n\n"
                "Переформулируйте промпт и попробуйте снова."
            )
        else:
            user_msg = (
                "🚫 <b>Запрос заблокирован фильтрами безопасности Google</b>\n\n"
                "Ваш запрос может нарушать политику безопасности контента.\n"
                "Переформулируйте промпт и попробуйте снова."
            )
        super().__init__(message=base, user_message=user_msg)

    @classmethod
    def _extract_reason(cls, detail: str) -> str:
        if not detail:
            return ""
        lower = detail.lower()
        reasons = []
        for key, label in cls._REASON_MAP.items():
            if key in lower:
                reasons.append(label)
        if reasons:
            return ", ".join(reasons)
        refusal_kw = ("не могу", "cannot", "sorry", "извините", "unable", "нельзя")
        if any(kw in lower for kw in refusal_kw):
            snippet = detail[:200].strip()
            return snippet
        return ""


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


class VideoGenerationError(VertexAIError):
    """Raised when video generation fails — user_message is video-specific and error-specific."""

    def __init__(self, detail: str = "", user_message: str | None = None) -> None:
        msg = f"Video generation failed: {detail}" if detail else "Video generation failed."
        BotError.__init__(
            self,
            message=msg,
            user_message=user_message or (
                "❌ <b>Не удалось сгенерировать видео</b>\n\n"
                "Попробуйте ещё раз или выберите другую модель / параметры."
            ),
        )

    @classmethod
    def timeout(cls) -> "VideoGenerationError":
        return cls(
            detail="poll timeout",
            user_message=(
                "⏱ <b>Время ожидания истекло</b>\n\n"
                "Видео не было создано за 10 минут — Google не успел сгенерировать.\n"
                "Попробуйте ещё раз или выберите меньшую длительность / разрешение."
            ),
        )

    @classmethod
    def no_video(cls, extra: str = "") -> "VideoGenerationError":
        return cls(
            detail=f"no video returned{': ' + extra if extra else ''}",
            user_message=(
                "⚠️ <b>Модель не вернула видео</b>\n\n"
                "Результат пустой — попробуйте другой промпт или перезапустите генерацию."
            ),
        )

    @classmethod
    def auth_error(cls) -> "VideoGenerationError":
        return cls(
            detail="auth error on all slots",
            user_message=(
                "🔑 <b>Ошибка авторизации</b>\n\n"
                "API ключи вернули ошибку аутентификации или доступа.\n"
                "Проверьте настройки ключей и биллинг Google Cloud в /admin."
            ),
        )

    @classmethod
    def server_error(cls, detail: str = "") -> "VideoGenerationError":
        return cls(
            detail=detail or "server error",
            user_message=(
                "🔧 <b>Временная ошибка сервера Google</b>\n\n"
                "Сервис генерации видео временно недоступен.\n"
                "Попробуйте через несколько минут."
            ),
        )

    @classmethod
    def from_http(cls, status: int, err_status: str, err_msg: str) -> "VideoGenerationError":
        """Map Google REST API HTTP error to a user-friendly VideoGenerationError."""
        s = err_status.upper()
        msg_lower = err_msg.lower()
        if status == 400 or s in ("INVALID_ARGUMENT", "BAD_REQUEST"):
            user_msg = (
                "❌ <b>Неверные параметры запроса</b>\n\n"
                f"<i>{err_msg[:200]}</i>\n\n"
                "Попробуйте изменить разрешение, длительность или выбрать другую модель."
            )
        elif status in (401, 403) or s in ("PERMISSION_DENIED", "UNAUTHENTICATED", "FORBIDDEN"):
            user_msg = (
                "🔑 <b>Нет доступа к модели видео</b>\n\n"
                "Проверьте: API ключ включён, биллинг Google Cloud активен, "
                "и Veo API доступен в вашем регионе / проекте."
            )
        elif status == 404 or s == "NOT_FOUND":
            user_msg = (
                "❌ <b>Модель видео недоступна</b>\n\n"
                "Выбранная модель не найдена или временно отключена.\n"
                "Попробуйте другую модель видео."
            )
        elif (
            status == 429
            or "quota" in msg_lower
            or "resource exhausted" in msg_lower
            or "rate limit" in msg_lower
        ):
            user_msg = (
                "⏳ <b>Превышен лимит запросов Google</b>\n\n"
                "Квота исчерпана. Попробуйте через пару минут\n"
                "или переключитесь на другую модель."
            )
        elif status >= 500 or s in ("INTERNAL", "UNAVAILABLE"):
            user_msg = (
                "🔧 <b>Ошибка сервера Google</b>\n\n"
                "Сервис генерации видео временно недоступен.\n"
                "Попробуйте через несколько минут."
            )
        else:
            user_msg = (
                f"❌ <b>Ошибка генерации видео</b> (код {status})\n\n"
                f"<i>{err_msg[:200]}</i>\n\n"
                "Попробуйте ещё раз или выберите другую модель."
            )
        return cls(detail=f"HTTP {status} {err_status}: {err_msg[:150]}", user_message=user_msg)

    @classmethod
    def from_poll_error(cls, err_msg: str) -> "VideoGenerationError":
        """Map a finished-operation error message to a user-friendly VideoGenerationError."""
        msg_lower = err_msg.lower()
        if "quota" in msg_lower or "resource exhausted" in msg_lower:
            user_msg = (
                "⏳ <b>Квота Google исчерпана</b>\n\n"
                "Попробуйте через пару минут или переключитесь на другую модель."
            )
        elif "invalid_argument" in msg_lower or "invalid argument" in msg_lower:
            user_msg = (
                "❌ <b>Неверные параметры генерации</b>\n\n"
                f"<i>{err_msg[:200]}</i>\n\n"
                "Попробуйте изменить разрешение, длительность или модель."
            )
        elif "permission" in msg_lower or "forbidden" in msg_lower or "unauthenticated" in msg_lower:
            user_msg = (
                "🔑 <b>Нет доступа к модели</b>\n\n"
                "Проверьте настройки API ключа и биллинг Google Cloud."
            )
        elif "not found" in msg_lower:
            user_msg = (
                "❌ <b>Модель видео недоступна</b>\n\n"
                "Выбранная модель не найдена. Попробуйте другую."
            )
        else:
            user_msg = (
                "❌ <b>Ошибка при генерации видео</b>\n\n"
                f"<i>{err_msg[:200]}</i>\n\n"
                "Попробуйте ещё раз или выберите другую модель."
            )
        return cls(detail=err_msg[:150], user_message=user_msg)


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
