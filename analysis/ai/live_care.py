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
LIVE_CARE_DETAIL_CONTENT_MAX_LENGTH = 60
LIVE_CARE_SCHEMA_NAME = "live_care_content"
LIVE_CARE_MAX_OUTPUT_TOKENS = 700

LIVE_CARE_FIRST_DETAIL_LABEL = "어떻게"
LIVE_CARE_SECOND_DETAIL_LABELS = ("언제까지", "피해주세요", "보관할 때")
LIVE_CARE_DETAIL_LABELS = (LIVE_CARE_FIRST_DETAIL_LABEL,) + LIVE_CARE_SECOND_DETAIL_LABELS


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
                    "details": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 2,
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {
                                    "type": "string",
                                    "enum": list(LIVE_CARE_DETAIL_LABELS),
                                },
                                "content": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": LIVE_CARE_DETAIL_CONTENT_MAX_LENGTH,
                                },
                            },
                            "required": ["label", "content"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["step", "title", "description", "details"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["steps"],
    "additionalProperties": False,
}

_HANGUL_PATTERN = re.compile(r"[가-힣]")

# 룰 코드별 details 두번째 라벨 매핑 (기획 확정: 온도/수분=언제까지,
# 하중 계열=피해주세요, 형태=보관할 때)
RULE_SECOND_DETAIL_LABEL = {
    "HIGH_TEMPERATURE": "언제까지",
    "MOISTURE": "언제까지",
    "HIGH_HUMIDITY": "피해주세요",
    "HIGH_LOAD": "피해주세요",
    "LOAD_BIAS": "피해주세요",
    "DEFORMATION": "보관할 때",
}

# 활성 룰이 3개를 초과할 때 어떤 3개를 보여줄지 정하는 심각도 우선순위.
# 되돌리기 어려운 손상(DEFORMATION) > 수분/곰팡이(MOISTURE) > 소재 손상(TEMP)
# > 나머지 순서.
_RULE_SEVERITY_ORDER = (
    "DEFORMATION",
    "MOISTURE",
    "HIGH_TEMPERATURE",
    "HIGH_LOAD",
    "LOAD_BIAS",
    "HIGH_HUMIDITY",
)

# 활성 룰이 3개 미만이거나, 룰별 care_actions 데이터가 details를 만들기에
# 부족할 때 채워 넣는 중립 필러 스텝 (AI 실패 시 안전망용).
_NEUTRAL_FILLER_STEPS = (
    {
        "title": "가방 상태를 계속 확인해 주세요",
        "description": "특별한 위험 신호는 없지만 꾸준한 확인이 도움이 돼요.",
        "details": [
            {"label": "어떻게", "content": "겉면과 내부를 가볍게 살펴봐 주세요."},
            {"label": "보관할 때", "content": "통풍이 잘 되는 곳에서 보관해 주세요."},
        ],
    },
    {
        "title": "무게가 고르게 분산되도록 정리해 주세요",
        "description": "한쪽으로 무게가 쏠리지 않도록 짐을 정리해 주세요.",
        "details": [
            {"label": "어떻게", "content": "무거운 물건은 가방 중앙 쪽에 넣어 주세요."},
            {"label": "피해주세요", "content": "한쪽 손잡이에만 무게가 쏠리지 않도록 해주세요."},
        ],
    },
    {
        "title": "다음 사용 전에 가방 상태를 다시 확인해 주세요",
        "description": "사용 전에 소재와 형태를 가볍게 점검해 주세요.",
        "details": [
            {"label": "어떻게", "content": "지퍼와 손잡이 부분을 가볍게 눌러 확인해 주세요."},
            {"label": "언제까지", "content": "다음 사용 전까지 통풍이 되는 곳에 두어 주세요."},
        ],
    },
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

각 단계(step)마다 details를 정확히 2개 작성하세요.
- details[0].label은 항상 "어떻게"이고, 그 단계의 구체적인 행동 방법을 적으세요.
- details[1].label은 해당 단계가 다루는 룰(active_rules)의 성격에 따라
  다음 중 하나를 고르세요.
  - HIGH_TEMPERATURE, MOISTURE: "언제까지" (조치를 얼마나 유지해야 하는지)
  - HIGH_HUMIDITY, HIGH_LOAD, LOAD_BIAS: "피해주세요" (하지 말아야 할 행동)
  - DEFORMATION: "보관할 때" (형태를 유지하는 보관 방법)
- 한 단계가 여러 룰과 관련되면 primary_rule 또는 가장 핵심적인 룰 하나를 기준으로
  라벨을 고르세요.
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
        if set(item) != {"step", "title", "description", "details"}:
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
                "details": _normalize_details(item["details"], index),
            }
        )

    return {"steps": normalized_steps}

def _normalize_details(details, step_index):
    field_name = f"steps[{step_index}].details"
    if not isinstance(details, list) or len(details) != 2:
        raise LiveCareContentValidationError(
            f"{field_name} must contain exactly 2 items."
        )

    normalized_details = []
    for detail_index, detail in enumerate(details):
        detail_field_name = f"{field_name}[{detail_index}]"
        if not isinstance(detail, dict) or set(detail) != {"label", "content"}:
            raise LiveCareContentValidationError(
                f"{detail_field_name} has invalid fields."
            )
        label = detail["label"]
        if not isinstance(label, str) or label not in LIVE_CARE_DETAIL_LABELS:
            raise LiveCareContentValidationError(
                f"{detail_field_name}.label must be one of {LIVE_CARE_DETAIL_LABELS}."
            )
        normalized_details.append(
            {
                "label": label,
                "content": _normalize_text(
                    detail["content"],
                    f"{detail_field_name}.content",
                    LIVE_CARE_DETAIL_CONTENT_MAX_LENGTH,
                ),
            }
        )

    if normalized_details[0]["label"] != LIVE_CARE_FIRST_DETAIL_LABEL:
        raise LiveCareContentValidationError(
            f"{field_name}[0].label must be '{LIVE_CARE_FIRST_DETAIL_LABEL}'."
        )
    if normalized_details[1]["label"] not in LIVE_CARE_SECOND_DETAIL_LABELS:
        raise LiveCareContentValidationError(
            f"{field_name}[1].label must be one of {LIVE_CARE_SECOND_DETAIL_LABELS}."
        )

    return normalized_details

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

def _priority_ordered_active_rules(interpretation):
    active_rules = interpretation.get("active_rules")
    active_rules = active_rules if isinstance(active_rules, list) else []
    active_rules = [rule for rule in active_rules if isinstance(rule, str)]

    if len(active_rules) > 3:
        # 활성 룰이 3개를 넘으면 심각도 우선순위로 재정렬합니다.
        return sorted(
            active_rules,
            key=lambda code: (
                _RULE_SEVERITY_ORDER.index(code)
                if code in _RULE_SEVERITY_ORDER
                else len(_RULE_SEVERITY_ORDER)
            ),
        )

    primary_rule = interpretation.get("primary_rule")
    ordered = []
    if isinstance(primary_rule, str) and primary_rule in active_rules:
        ordered.append(primary_rule)
    ordered.extend(rule for rule in active_rules if rule not in ordered)
    return ordered


def _valid_fallback_text(value, max_length):
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    if (
        not normalized
        or len(normalized) > max_length
        or _HANGUL_PATTERN.search(normalized) is None
    ):
        return None
    return normalized


def _build_rule_step(rule_code, action):
    if not isinstance(action, dict):
        return None
    second_label = RULE_SECOND_DETAIL_LABEL.get(rule_code)
    if second_label is None:
        return None

    title = _valid_fallback_text(action.get("title"), LIVE_CARE_TITLE_MAX_LENGTH)
    reason = _valid_fallback_text(
        action.get("reason"), LIVE_CARE_DESCRIPTION_MAX_LENGTH
    )
    configured_steps = action.get("steps")
    if (
        title is None
        or reason is None
        or not isinstance(configured_steps, list)
        or len(configured_steps) < 2
    ):
        return None

    how_step = _valid_fallback_text(
        configured_steps[0], LIVE_CARE_DETAIL_CONTENT_MAX_LENGTH
    )
    other_step = _valid_fallback_text(
        configured_steps[1], LIVE_CARE_DETAIL_CONTENT_MAX_LENGTH
    )
    if how_step is None or other_step is None:
        return None

    return {
        "title": title,
        "description": reason,
        "details": [
            {"label": LIVE_CARE_FIRST_DETAIL_LABEL, "content": how_step},
            {"label": second_label, "content": other_step},
        ],
    }

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

    steps = []
    seen_titles = set()

    for rule_code in _priority_ordered_active_rules(interpretation):
        built_step = _build_rule_step(rule_code, relevant_actions.get(rule_code))
        if built_step is None or built_step["title"] in seen_titles:
            continue
        steps.append(built_step)
        seen_titles.add(built_step["title"])
        if len(steps) == 3:
            break

    filler_index = 0
    while len(steps) < 3 and filler_index < len(_NEUTRAL_FILLER_STEPS):
        candidate = _NEUTRAL_FILLER_STEPS[filler_index]
        filler_index += 1
        if candidate["title"] in seen_titles:
            continue
        steps.append(deepcopy(candidate))
        seen_titles.add(candidate["title"])

    if len(steps) != 3:
        raise ValueError("Unable to build exactly 3 fallback care steps.")

    fallback = {
        "steps": [
            {"step": index, **step}
            for index, step in enumerate(steps, start=1)
        ]
    }
    return validate_live_care_content(deepcopy(fallback))
 