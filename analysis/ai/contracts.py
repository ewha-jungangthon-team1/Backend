from collections.abc import Mapping


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
