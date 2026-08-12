from collections.abc import Mapping
from datetime import datetime


HISTORY_AI_CONTENT_FIELDS = (
    "weekly_summary",
    "care_comment",
    "pattern_insight",
    "priority_actions",
)

HISTORY_AI_CONTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "weekly_summary": {"type": "string", "minLength": 1},
        "care_comment": {"type": "string", "minLength": 1},
        "pattern_insight": {"type": "string", "minLength": 1},
        "priority_actions": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "maxItems": 2,
        },
    },
    "required": list(HISTORY_AI_CONTENT_FIELDS),
    "additionalProperties": False,
}


class HistoryAIContentValidationError(ValueError):
    pass


class HistoryAIResultValidationError(ValueError):
    pass


def _normalize_required_string(payload, field_name):
    value = payload[field_name]
    if not isinstance(value, str) or not value.strip():
        raise HistoryAIContentValidationError(
            f"{field_name} must be a non-empty string."
        )
    return value.strip()


def validate_history_ai_content(payload):
    if not isinstance(payload, Mapping):
        raise HistoryAIContentValidationError("payload must be a mapping.")

    expected_fields = set(HISTORY_AI_CONTENT_FIELDS)
    actual_fields = set(payload)
    missing_fields = expected_fields - actual_fields
    if missing_fields:
        raise HistoryAIContentValidationError(
            "Missing required fields: " + ", ".join(sorted(missing_fields)) + "."
        )

    unexpected_fields = actual_fields - expected_fields
    if unexpected_fields:
        raise HistoryAIContentValidationError(
            "Unexpected fields: " + ", ".join(sorted(unexpected_fields)) + "."
        )

    priority_actions = payload["priority_actions"]
    if not isinstance(priority_actions, list):
        raise HistoryAIContentValidationError("priority_actions must be a list.")
    if len(priority_actions) > 2:
        raise HistoryAIContentValidationError(
            "priority_actions must contain at most 2 items."
        )

    normalized_actions = []
    for index, action in enumerate(priority_actions):
        if not isinstance(action, str) or not action.strip():
            raise HistoryAIContentValidationError(
                f"priority_actions[{index}] must be a non-empty string."
            )
        normalized_actions.append(action.strip())

    return {
        "weekly_summary": _normalize_required_string(payload, "weekly_summary"),
        "care_comment": _normalize_required_string(payload, "care_comment"),
        "pattern_insight": _normalize_required_string(payload, "pattern_insight"),
        "priority_actions": normalized_actions,
    }


def _validate_generated_at(value):
    if not isinstance(value, str) or not value.strip():
        raise HistoryAIResultValidationError(
            "generated_at must be a non-empty ISO-8601 string."
        )

    normalized_value = value.strip()
    try:
        parsed_value = datetime.fromisoformat(normalized_value)
    except ValueError as error:
        raise HistoryAIResultValidationError(
            "generated_at must be a valid ISO-8601 datetime."
        ) from error

    if parsed_value.tzinfo is None or parsed_value.utcoffset() is None:
        raise HistoryAIResultValidationError(
            "generated_at must be timezone-aware."
        )
    return normalized_value


def _validate_non_empty_string(value, field_name):
    if not isinstance(value, str) or not value.strip():
        raise HistoryAIResultValidationError(
            f"{field_name} must be a non-empty string."
        )
    return value.strip()


def validate_history_ai_result(payload):
    if not isinstance(payload, Mapping):
        raise HistoryAIResultValidationError("payload must be a mapping.")

    expected_fields = {
        "schema_version",
        "status",
        "generated_at",
        "provider",
        "model",
        "fallback_reason",
        "content",
    }
    actual_fields = set(payload)
    missing_fields = expected_fields - actual_fields
    if missing_fields:
        raise HistoryAIResultValidationError(
            "Missing required fields: " + ", ".join(sorted(missing_fields)) + "."
        )

    unexpected_fields = actual_fields - expected_fields
    if unexpected_fields:
        raise HistoryAIResultValidationError(
            "Unexpected fields: " + ", ".join(sorted(unexpected_fields)) + "."
        )

    schema_version = payload["schema_version"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != 1
    ):
        raise HistoryAIResultValidationError("schema_version must be integer 1.")

    status = payload["status"]
    if not isinstance(status, str) or status not in {"SUCCESS", "FALLBACK"}:
        raise HistoryAIResultValidationError(
            "status must be SUCCESS or FALLBACK."
        )

    generated_at = _validate_generated_at(payload["generated_at"])
    provider = payload["provider"]
    model = payload["model"]
    fallback_reason = payload["fallback_reason"]

    if status == "SUCCESS":
        if provider != "openai":
            raise HistoryAIResultValidationError(
                "SUCCESS provider must be openai."
            )
        model = _validate_non_empty_string(model, "SUCCESS model")
        if fallback_reason is not None:
            raise HistoryAIResultValidationError(
                "SUCCESS fallback_reason must be null."
            )
    else:
        if provider != "deterministic":
            raise HistoryAIResultValidationError(
                "FALLBACK provider must be deterministic."
            )
        if model is not None:
            raise HistoryAIResultValidationError("FALLBACK model must be null.")
        fallback_reason = _validate_non_empty_string(
            fallback_reason,
            "FALLBACK fallback_reason",
        )

    try:
        content = validate_history_ai_content(payload["content"])
    except HistoryAIContentValidationError as error:
        raise HistoryAIResultValidationError(
            "content must satisfy the HISTORY AI content contract."
        ) from error

    return {
        "schema_version": schema_version,
        "status": status,
        "generated_at": generated_at,
        "provider": provider,
        "model": model,
        "fallback_reason": fallback_reason,
        "content": content,
    }
