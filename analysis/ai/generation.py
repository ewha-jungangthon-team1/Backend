import json
from collections.abc import Mapping

from openai import APIError

from .client import get_openai_client, get_openai_model
from .contracts import (
    HISTORY_AI_CONTENT_SCHEMA,
    HistoryAIContentValidationError,
    validate_history_ai_content,
)
from .errors import (
    HistoryAIGenerationError,
    HistoryAIGenerationReason,
    raise_history_ai_generation_error,
)
from .prompts import HISTORY_AI_DEVELOPER_INSTRUCTION


HISTORY_AI_SCHEMA_NAME = "history_ai_content"
HISTORY_AI_MAX_OUTPUT_TOKENS = 1200


def _get_value(value, field_name, default=None):
    if isinstance(value, Mapping):
        return value.get(field_name, default)
    return getattr(value, field_name, default)


def _has_refusal(response):
    for output_item in _get_value(response, "output", []) or []:
        if _get_value(output_item, "type") != "message":
            continue
        for content_item in _get_value(output_item, "content", []) or []:
            if _get_value(content_item, "type") == "refusal":
                return True
    return False


def _raise_generation_error(reason, error=None):
    generation_error = HistoryAIGenerationError(reason)
    if error is None:
        raise generation_error
    raise generation_error from error


def generate_history_ai_content(context, *, client=None, model=None):
    if not isinstance(context, Mapping):
        raise TypeError("context must be a mapping.")

    serialized_context = json.dumps(
        context,
        ensure_ascii=False,
        allow_nan=False,
    )
    resolved_client = client if client is not None else get_openai_client()
    resolved_model = model if model is not None else get_openai_model()

    try:
        response = resolved_client.responses.create(
            model=resolved_model,
            instructions=HISTORY_AI_DEVELOPER_INSTRUCTION,
            input=serialized_context,
            text={
                "format": {
                    "type": "json_schema",
                    "name": HISTORY_AI_SCHEMA_NAME,
                    "schema": HISTORY_AI_CONTENT_SCHEMA,
                    "strict": True,
                }
            },
            max_output_tokens=HISTORY_AI_MAX_OUTPUT_TOKENS,
            store=False,
            stream=False,
        )
    except APIError as error:
        raise_history_ai_generation_error(error)

    status = _get_value(response, "status")
    if status == "incomplete":
        _raise_generation_error(HistoryAIGenerationReason.OPENAI_INCOMPLETE)
    if status != "completed":
        _raise_generation_error(HistoryAIGenerationReason.OPENAI_API_ERROR)
    if _has_refusal(response):
        _raise_generation_error(HistoryAIGenerationReason.OPENAI_REFUSAL)

    output_text = _get_value(response, "output_text")
    if not isinstance(output_text, str) or not output_text.strip():
        _raise_generation_error(HistoryAIGenerationReason.EMPTY_AI_RESPONSE)

    try:
        parsed_content = json.loads(output_text)
    except json.JSONDecodeError as error:
        _raise_generation_error(
            HistoryAIGenerationReason.INVALID_AI_RESPONSE,
            error,
        )

    try:
        return validate_history_ai_content(parsed_content)
    except HistoryAIContentValidationError as error:
        _raise_generation_error(
            HistoryAIGenerationReason.INVALID_AI_RESPONSE,
            error,
        )
