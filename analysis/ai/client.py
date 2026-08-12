from django.conf import settings
from openai import OpenAI

from .errors import HistoryAIGenerationError, HistoryAIGenerationReason


DEFAULT_OPENAI_MODEL = "gpt-5.6-terra"
DEFAULT_OPENAI_TIMEOUT_SECONDS = 12.0
OPENAI_MAX_RETRIES = 1


def get_openai_model():
    configured_model = getattr(settings, "OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    if not isinstance(configured_model, str) or not configured_model.strip():
        return DEFAULT_OPENAI_MODEL
    return configured_model.strip()


def get_openai_timeout_seconds():
    configured_timeout = getattr(
        settings,
        "OPENAI_TIMEOUT_SECONDS",
        DEFAULT_OPENAI_TIMEOUT_SECONDS,
    )
    if configured_timeout is None or configured_timeout == "":
        return DEFAULT_OPENAI_TIMEOUT_SECONDS
    return float(configured_timeout)


def get_openai_client():
    api_key = getattr(settings, "OPENAI_API_KEY", "")
    if not isinstance(api_key, str) or not api_key.strip():
        raise HistoryAIGenerationError(
            HistoryAIGenerationReason.OPENAI_NOT_CONFIGURED
        )

    return OpenAI(
        api_key=api_key.strip(),
        timeout=get_openai_timeout_seconds(),
        max_retries=OPENAI_MAX_RETRIES,
    )
