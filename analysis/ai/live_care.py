import json
import re
from collections.abc import Mapping
from copy import deepcopy
from enum import Enum

from openai import (
    APIConnectionError,
    APIError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    RateLimitError,
)

from .client import get_openai_client, get_openai_model
from .errors import HistoryAIGenerationError


LIVE_CARE_TITLE_MAX_LENGTH = 40
LIVE_CARE_DESCRIPTION_MAX_LENGTH = 120
LIVE_CARE_SCHEMA_NAME = "live_care_content"
LIVE_CARE_MAX_OUTPUT_TOKENS = 700

LIVE_CARE_CONTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "step": {"type": "integer", "enum": [1, 2, 3]},
                    "title": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": LIVE_CARE_TITLE_MAX_LENGTH,
                    },
                    "description": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": LIVE_CARE_DESCRIPTION_MAX_LENGTH,
                    },
                },
                "required": ["step", "title", "description"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["steps"],
    "additionalProperties": False,
}

_HANGUL_PATTERN = re.compile(r"[가-힣]")
_STEP_TITLES = ("우선 관리", "상태 정리", "마무리 확인")
_NEUTRAL_ACTIONS = (
    "현재 상태를 계속 확인해 주세요.",
    "관리 기준에 맞는 환경에서 보관해 주세요.",
    "다음 사용 전에 가방 상태를 다시 확인해 주세요.",
)

LIVE_CARE_DEVELOPER_INSTRUCTION = """\
당신은 백엔드가 이미 확정한 현재 LIVE 상태를 짧은 케어 행동 문구로 바꾸는 작성기입니다.
제공된 JSON context만 근거로 사용하고, 정확히 3단계의 짧은 한국어 존댓말을 작성하세요.

근거 우선순위는 primary_rule, active_rules, relevant_care_actions, quick_care,
sensor_facts, product.material 순서입니다.

다음 제한을 반드시 지키세요.
- active_rules를 다시 판정하거나 state_code 또는 primary_rule을 다시 결정하지 마세요.
- context에 없는 사건, 다른 rule action, 다른 제품·세션·HISTORY 정보를 만들지 마세요.
- 제공되지 않은 센서 수치나 새로운 숫자를 생성하거나 추정하지 마세요.
- 센서 상태를 근거로 손상, 변색, 변형 등을 확정 진단하지 마세요.
- 제공된 material을 그대로 사용하고 다른 소재를 추정하지 마세요.
- context에 없는 care action을 전문 지식처럼 추가하지 마세요.
- 근거 없는 세척제, 화학제품, 크림, 오일, 발수제를 추천하지 마세요.
- 드라이어, 난방기, 히터, 직접 열원, 직사광선 건조를 권하지 마세요.
- 근거 없이 전문 수선업체 방문을 추가하지 마세요.
- active_rules가 비어 있으면 존재하지 않는 위험이나 손상을 만들지 마세요.
- diagnosis, summary, warning, checklist 또는 네 번째 단계를 추가하지 마세요.
- 출력은 제공된 JSON schema의 정확히 3단계만 포함하세요.
"""


class LiveCareContentValidationError(ValueError):
    pass


class LiveCareGenerationReason(str, Enum):
    OPENAI_NOT_CONFIGURED = "OPENAI_NOT_CONFIGURED"
    OPENAI_TIMEOUT = "OPENAI_TIMEOUT"
    OPENAI_RATE_LIMIT = "OPENAI_RATE_LIMIT"
    OPENAI_CONNECTION_ERROR = "OPENAI_CONNECTION_ERROR"
    OPENAI_API_ERROR = "OPENAI_API_ERROR"
    OPENAI_REFUSAL = "OPENAI_REFUSAL"
    OPENAI_INCOMPLETE = "OPENAI_INCOMPLETE"
    EMPTY_AI_RESPONSE = "EMPTY_AI_RESPONSE"
    INVALID_AI_RESPONSE = "INVALID_AI_RESPONSE"


class LiveCareGenerationError(Exception):
    def __init__(self, reason):
        if not isinstance(reason, LiveCareGenerationReason):
            reason = LiveCareGenerationReason(reason)
        self.reason = reason.value
        super().__init__(self.reason)


def _normalize_text(value, field_name, max_length):
    if not isinstance(value, str) or not value.strip():
        raise LiveCareContentValidationError(
            f"{field_name} must be a non-empty string."
        )
    normalized = value.strip()
    if len(normalized) > max_length:
        raise LiveCareContentValidationError(
            f"{field_name} must contain at most {max_length} characters."
        )
    if _HANGUL_PATTERN.search(normalized) is None:
        raise LiveCareContentValidationError(
            f"{field_name} must contain Korean text."
        )
    return normalized


def validate_live_care_content(payload):
    if not isinstance(payload, dict):
        raise LiveCareContentValidationError("payload must be a dictionary.")
    if set(payload) != {"steps"}:
        raise LiveCareContentValidationError(
            "payload must contain exactly the steps field."
        )

    steps = payload["steps"]
    if not isinstance(steps, list):
        raise LiveCareContentValidationError("steps must be a list.")
    if len(steps) != 3:
        raise LiveCareContentValidationError(
            "steps must contain exactly 3 items."
        )

    normalized_steps = []
    for index, item in enumerate(steps):
        if not isinstance(item, dict):
            raise LiveCareContentValidationError(
                f"steps[{index}] must be a dictionary."
            )
        if set(item) != {"step", "title", "description"}:
            raise LiveCareContentValidationError(
                f"steps[{index}] has invalid fields."
            )
        step_number = item["step"]
        if isinstance(step_number, bool) or not isinstance(step_number, int):
            raise LiveCareContentValidationError(
                f"steps[{index}].step must be an integer."
            )
        if step_number != index + 1:
            raise LiveCareContentValidationError(
                "step numbers must be exactly [1, 2, 3]."
            )

        normalized_steps.append(
            {
                "step": step_number,
                "title": _normalize_text(
                    item["title"],
                    f"steps[{index}].title",
                    LIVE_CARE_TITLE_MAX_LENGTH,
                ),
                "description": _normalize_text(
                    item["description"],
                    f"steps[{index}].description",
                    LIVE_CARE_DESCRIPTION_MAX_LENGTH,
                ),
            }
        )

    return {"steps": normalized_steps}


def _get_response_value(value, field_name, default=None):
    if isinstance(value, Mapping):
        return value.get(field_name, default)
    return getattr(value, field_name, default)


def _response_has_refusal(response):
    for output_item in _get_response_value(response, "output", []) or []:
        if _get_response_value(output_item, "type") != "message":
            continue
        for content_item in _get_response_value(output_item, "content", []) or []:
            if _get_response_value(content_item, "type") == "refusal":
                return True
    return False


def _raise_live_generation_error(reason, error=None):
    generation_error = LiveCareGenerationError(reason)
    if error is None:
        raise generation_error
    raise generation_error from error


def _raise_mapped_openai_error(error):
    if isinstance(error, APITimeoutError):
        reason = LiveCareGenerationReason.OPENAI_TIMEOUT
    elif isinstance(error, RateLimitError):
        reason = LiveCareGenerationReason.OPENAI_RATE_LIMIT
    elif isinstance(error, APIConnectionError):
        reason = LiveCareGenerationReason.OPENAI_CONNECTION_ERROR
    elif isinstance(error, (APIResponseValidationError, APIStatusError, APIError)):
        reason = LiveCareGenerationReason.OPENAI_API_ERROR
    else:
        raise error
    _raise_live_generation_error(reason, error)


def generate_live_care_content(context, *, client=None, model=None):
    if not isinstance(context, Mapping):
        raise TypeError("context must be a mapping.")

    serialized_context = json.dumps(
        context,
        ensure_ascii=False,
        allow_nan=False,
    )
    if client is None:
        try:
            resolved_client = get_openai_client()
        except HistoryAIGenerationError as error:
            _raise_live_generation_error(error.reason, error)
    else:
        resolved_client = client
    resolved_model = model if model is not None else get_openai_model()

    try:
        response = resolved_client.responses.create(
            model=resolved_model,
            instructions=LIVE_CARE_DEVELOPER_INSTRUCTION,
            input=serialized_context,
            text={
                "format": {
                    "type": "json_schema",
                    "name": LIVE_CARE_SCHEMA_NAME,
                    "schema": LIVE_CARE_CONTENT_SCHEMA,
                    "strict": True,
                }
            },
            max_output_tokens=LIVE_CARE_MAX_OUTPUT_TOKENS,
            store=False,
            stream=False,
        )
    except APIError as error:
        _raise_mapped_openai_error(error)

    status = _get_response_value(response, "status")
    if status == "incomplete":
        _raise_live_generation_error(
            LiveCareGenerationReason.OPENAI_INCOMPLETE
        )
    if status != "completed":
        _raise_live_generation_error(
            LiveCareGenerationReason.OPENAI_API_ERROR
        )
    if _response_has_refusal(response):
        _raise_live_generation_error(
            LiveCareGenerationReason.OPENAI_REFUSAL
        )

    output_text = _get_response_value(response, "output_text")
    if not isinstance(output_text, str) or not output_text.strip():
        _raise_live_generation_error(
            LiveCareGenerationReason.EMPTY_AI_RESPONSE
        )

    try:
        parsed_content = json.loads(output_text)
    except json.JSONDecodeError as error:
        _raise_live_generation_error(
            LiveCareGenerationReason.INVALID_AI_RESPONSE,
            error,
        )

    try:
        return validate_live_care_content(parsed_content)
    except LiveCareContentValidationError as error:
        _raise_live_generation_error(
            LiveCareGenerationReason.INVALID_AI_RESPONSE,
            error,
        )


def _normalize_fallback_action(value):
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    if (
        not normalized
        or len(normalized) > LIVE_CARE_DESCRIPTION_MAX_LENGTH
        or _HANGUL_PATTERN.search(normalized) is None
    ):
        return None
    return normalized


def _ordered_active_rules(interpretation):
    active_rules = interpretation.get("active_rules")
    active_rules = active_rules if isinstance(active_rules, list) else []
    active_rules = [rule for rule in active_rules if isinstance(rule, str)]
    primary_rule = interpretation.get("primary_rule")
    ordered = []
    if isinstance(primary_rule, str) and primary_rule in active_rules:
        ordered.append(primary_rule)
    ordered.extend(rule for rule in active_rules if rule not in ordered)
    return ordered


def build_live_care_fallback(context):
    if not isinstance(context, dict):
        raise ValueError("context must be a dictionary.")

    interpretation = context.get("interpretation")
    interpretation = interpretation if isinstance(interpretation, dict) else {}
    guideline = context.get("guideline")
    guideline = guideline if isinstance(guideline, dict) else {}
    relevant_actions = guideline.get("relevant_care_actions")
    relevant_actions = (
        relevant_actions if isinstance(relevant_actions, dict) else {}
    )

    selected_actions = []
    seen_actions = set()

    def add_action(value):
        normalized = _normalize_fallback_action(value)
        if normalized is None or normalized in seen_actions:
            return
        seen_actions.add(normalized)
        selected_actions.append(normalized)

    for rule_code in _ordered_active_rules(interpretation):
        action = relevant_actions.get(rule_code)
        if not isinstance(action, dict):
            continue
        configured_steps = action.get("steps")
        if not isinstance(configured_steps, list):
            continue
        for configured_step in configured_steps:
            add_action(configured_step)
            if len(selected_actions) == 3:
                break
        if len(selected_actions) == 3:
            break

    if len(selected_actions) < 3:
        add_action(interpretation.get("quick_care"))
    for neutral_action in _NEUTRAL_ACTIONS:
        if len(selected_actions) == 3:
            break
        add_action(neutral_action)

    if len(selected_actions) != 3:
        raise ValueError("Unable to build exactly 3 fallback care actions.")

    fallback = {
        "steps": [
            {
                "step": index,
                "title": _STEP_TITLES[index - 1],
                "description": description,
            }
            for index, description in enumerate(selected_actions, start=1)
        ]
    }
    return validate_live_care_content(deepcopy(fallback))
