from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from ..constants import RuleCode, Severity

from .contracts import validate_history_ai_content


_RULE_FACTS = {
    RuleCode.HIGH_LOAD.value: (
        ("load", "overload_detected_days"),
        "하중 기준을 초과한 날이 {days}일",
    ),
    RuleCode.HIGH_TEMPERATURE.value: (
        ("temperature", "high_temperature_detected_days"),
        "온도 기준을 초과한 날이 {days}일",
    ),
    RuleCode.HIGH_HUMIDITY.value: (
        ("humidity", "high_humidity_detected_days"),
        "내부 습도 기준을 초과한 날이 {days}일",
    ),
    RuleCode.MOISTURE.value: (
        ("moisture", "detected_days"),
        "수분 접촉이 감지된 날이 {days}일",
    ),
    RuleCode.LOAD_BIAS.value: (
        ("load_bias", "biased_days"),
        "하중 편중 기준을 초과한 날이 {days}일",
    ),
    RuleCode.DEFORMATION.value: (
        ("deformation", "deformation_detected_days"),
        "형태 편차 기준을 초과한 날이 {days}일",
    ),
}


def _mapping(value):
    return value if isinstance(value, Mapping) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _nested_value(mapping, path):
    value = mapping
    for key in path:
        value = _mapping(value).get(key)
    return value


def _as_decimal(value):
    if isinstance(value, bool):
        return None
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return decimal_value if decimal_value.is_finite() else None


def _format_number(value, decimal_places=2):
    decimal_value = _as_decimal(value)
    if decimal_value is None:
        return None
    quantum = Decimal("1").scaleb(-decimal_places)
    return format(
        decimal_value.quantize(quantum, rounding=ROUND_HALF_UP),
        f".{decimal_places}f",
    )


def _build_weekly_summary(report):
    active_rules = _list(report.active_rules)
    metrics = _mapping(report.metrics)

    for rule_code in active_rules:
        fact_definition = _RULE_FACTS.get(rule_code)
        if fact_definition is None:
            continue
        metric_path, template = fact_definition
        detected_days = _nested_value(metrics, metric_path)
        detected_days_decimal = _as_decimal(detected_days)
        if detected_days_decimal is not None:
            fact = template.format(days=int(detected_days_decimal))
            return f"최근 7일은 {fact} 확인됐어요. 관련 관리가 필요해요."

    if report.severity == Severity.WARNING.value or active_rules:
        return (
            "최근 7일은 관리 기준을 벗어난 항목이 확인됐어요. "
            "현재 상태에 맞는 관리가 필요해요."
        )
    return (
        "최근 7일은 확인 가능한 지표에서 관리 기준을 넘은 기록 없이 "
        "전반적으로 안정적이었어요."
    )


def _care_actions(report):
    guideline = _mapping(report.care_guideline_snapshot)
    return _mapping(guideline.get("care_actions"))


def _build_care_comment(report):
    care_actions = _care_actions(report)
    for rule_code in _list(report.active_rules):
        action = _mapping(care_actions.get(rule_code))
        title = action.get("title")
        reason = action.get("reason")
        title = title.strip() if isinstance(title, str) else ""
        reason = reason.strip() if isinstance(reason, str) else ""
        if title and reason:
            return f"{title} {reason}"
        if title or reason:
            return title or reason

    if report.active_rules:
        return "관리 기준을 벗어난 항목을 확인하고 현재 상태를 점검해 주세요."
    return "현재 특별히 주의가 필요한 상태는 아니에요."


def _build_priority_actions(report):
    care_actions = _care_actions(report)
    selected_actions = []
    seen_actions = set()

    for rule_code in _list(report.active_rules):
        action = _mapping(care_actions.get(rule_code))
        for step in _list(action.get("steps")):
            if not isinstance(step, str):
                continue
            normalized_step = step.strip()
            if not normalized_step or normalized_step in seen_actions:
                continue
            seen_actions.add(normalized_step)
            selected_actions.append(normalized_step)
            if len(selected_actions) == 2:
                return selected_actions

    return selected_actions


def _metric_change(comparison_metrics, domain, metric_name):
    value = _nested_value(comparison_metrics, (domain, metric_name))
    value = _mapping(value)
    current = _as_decimal(value.get("current"))
    previous = _as_decimal(value.get("previous"))
    change = _as_decimal(value.get("change"))
    change_percent = _as_decimal(value.get("change_percent"))
    if current is None or previous is None or change is None:
        return None
    return {
        "current": current,
        "previous": previous,
        "change": change,
        "change_percent": change_percent,
    }


def _direction_phrase(change, increase_word="늘었어요.", decrease_word="줄었어요."):
    if change > 0:
        return increase_word
    if change < 0:
        return decrease_word
    return "같았어요."


def _average_load_pattern(comparison_metrics):
    metric = _metric_change(comparison_metrics, "load", "average_kg")
    if metric is None or metric["change"] == 0:
        return None

    if metric["change_percent"] is not None:
        formatted_change = _format_number(abs(metric["change_percent"]))
        unit = "%"
    else:
        formatted_change = _format_number(abs(metric["change"]))
        unit = "kg"
    return (
        f"평균 하중이 이전 7일보다 {formatted_change}{unit} "
        f"{_direction_phrase(metric['change'], '늘었어요.', '줄었어요.')}"
    )


def _day_count_pattern(comparison_metrics, domain, metric_name, label):
    metric = _metric_change(comparison_metrics, domain, metric_name)
    if metric is None or metric["change"] == 0:
        return None
    change = int(abs(metric["change"]))
    return (
        f"{label}이 이전 7일보다 {change}일 "
        f"{_direction_phrase(metric['change'], '늘었어요.', '줄었어요.')}"
    )


def _percentage_point_pattern(comparison_metrics, domain, metric_name, label):
    metric = _metric_change(comparison_metrics, domain, metric_name)
    if metric is None or metric["change"] == 0:
        return None
    formatted_change = _format_number(abs(metric["change"]))
    return (
        f"{label}이 이전 7일보다 {formatted_change}%p "
        f"{_direction_phrase(metric['change'], '높아졌어요.', '낮아졌어요.')}"
    )


def _build_available_pattern(comparison_metrics):
    candidates = (
        _average_load_pattern(comparison_metrics),
        _day_count_pattern(
            comparison_metrics,
            "load",
            "overload_detected_days",
            "과부하 발생일",
        ),
        _percentage_point_pattern(
            comparison_metrics,
            "load_bias",
            "max_absolute_percent",
            "최대 하중 편중",
        ),
        _percentage_point_pattern(
            comparison_metrics,
            "deformation",
            "latest_percent",
            "최근 형태 편차",
        ),
        _day_count_pattern(
            comparison_metrics,
            "moisture",
            "detected_days",
            "수분 접촉일",
        ),
    )
    selected = [candidate for candidate in candidates if candidate][:2]
    if selected:
        return " ".join(selected)
    return "이전 7일과 비교해 선택된 주요 관리 지표의 변화가 없었어요."


def _build_pattern_insight(report):
    comparison = _mapping(report.comparison)
    if comparison.get("available") is True:
        return _build_available_pattern(_mapping(comparison.get("metrics")))

    if comparison.get("reason") == "NO_PREVIOUS_PERIOD":
        return "이전 기록이 아직 충분하지 않아 이번에는 기간별 변화를 비교하기 어려워요."
    return "이전 기간과 안전하게 비교하기 어려워 이번에는 현재 상태만 확인해 주세요."


def build_history_ai_fallback(report):
    if report is None:
        raise ValueError("report is required.")

    return validate_history_ai_content(
        {
            "weekly_summary": _build_weekly_summary(report),
            "care_comment": _build_care_comment(report),
            "pattern_insight": _build_pattern_insight(report),
            "priority_actions": _build_priority_actions(report),
        }
    )
