from enum import Enum

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    RateLimitError,
)


class HistoryAIGenerationReason(str, Enum):
    OPENAI_NOT_CONFIGURED = "OPENAI_NOT_CONFIGURED"
    OPENAI_TIMEOUT = "OPENAI_TIMEOUT"
    OPENAI_RATE_LIMIT = "OPENAI_RATE_LIMIT"
    OPENAI_CONNECTION_ERROR = "OPENAI_CONNECTION_ERROR"
    OPENAI_API_ERROR = "OPENAI_API_ERROR"


class HistoryAIGenerationError(Exception):
    def __init__(self, reason):
        if not isinstance(reason, HistoryAIGenerationReason):
            reason = HistoryAIGenerationReason(reason)
        self.reason = reason.value
        super().__init__(self.reason)


def raise_history_ai_generation_error(error):
    if isinstance(error, APITimeoutError):
        reason = HistoryAIGenerationReason.OPENAI_TIMEOUT
    elif isinstance(error, RateLimitError):
        reason = HistoryAIGenerationReason.OPENAI_RATE_LIMIT
    elif isinstance(error, APIConnectionError):
        reason = HistoryAIGenerationReason.OPENAI_CONNECTION_ERROR
    elif isinstance(error, APIStatusError):
        reason = HistoryAIGenerationReason.OPENAI_API_ERROR
    else:
        raise error

    raise HistoryAIGenerationError(reason) from error
