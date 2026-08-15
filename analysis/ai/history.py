from collections.abc import Mapping
from copy import deepcopy
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum

from django.utils import timezone


_HISTORY_METRIC_DOMAINS = (
    "load",
    "temperature",
    "humidity",
    "moisture",
    "load_bias",
    "deformation",
)

_CARE_GUIDELINE_FIELDS = (
    "max_load_kg",
    "recommended_temp_range_c",
    "max_humidity_percent",
    "max_abs_load_bias",
    "max_body_deformation_ratio",
    "avoid_moisture",
    "care_actions",
)

_COMPARISON_FIELDS = (
    "available",
    "reason",
    "previous_period",
    "metrics",
)


def _as_project_timezone_iso(value):
    if value is None:
        return None

    project_timezone = timezone.get_default_timezone()
    if timezone.is_naive(value):
        value = timezone.make_aware(value, project_timezone)
    return timezone.localtime(value, project_timezone).isoformat()


def _json_safe_copy(value):
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe_copy(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe_copy(item) for item in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Enum):
        return _json_safe_copy(value.value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return deepcopy(value)
    raise ValueError(f"Unsupported AI context value type: {type(value).__name__}.")


def _select_mapping_fields(snapshot, field_names):
    if not isinstance(snapshot, Mapping):
        return {}
    return {
        field_name: _json_safe_copy(snapshot[field_name])
        for field_name in field_names
        if field_name in snapshot
    }


def _as_finite_decimal(value):
    if isinstance(value, bool):
        return None
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return decimal_value if decimal_value.is_finite() else None


def _build_material_moisture_summary(metrics):
    if not isinstance(metrics, Mapping):
        return None
    daily_series = metrics.get("daily_series")
    if not isinstance(daily_series, list):
        return None

    values = []
    for item in daily_series:
        if not isinstance(item, Mapping):
            continue
        presentation = item.get("presentation")
        if not isinstance(presentation, Mapping):
            continue
        value = _as_finite_decimal(
            presentation.get("material_moisture_percent")
        )
        if value is not None:
            values.append(value)

    if not values:
        return None

    first = values[0]
    latest = values[-1]
    change = (latest - first).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    if change > 0:
        trend = "INCREASED"
    elif change < 0:
        trend = "DECREASED"
    else:
        trend = "STABLE"

    return {
        "first_percent": float(first),
        "latest_percent": float(latest),
        "change_percentage_points": float(change),
        "trend": trend,
    }


def build_history_ai_context(report):
    if report is None:
        raise ValueError("report is required.")

    session = report.session
    return {
        "period": {
            "started_at": _as_project_timezone_iso(session.started_at),
            "ended_at": _as_project_timezone_iso(session.ended_at),
            "timezone": timezone.get_default_timezone_name(),
        },
        "metrics": _select_mapping_fields(
            report.metrics,
            _HISTORY_METRIC_DOMAINS,
        ),
        "material_moisture_summary": _build_material_moisture_summary(
            report.metrics
        ),
        "severity": _json_safe_copy(report.severity),
        "active_rules": _json_safe_copy(report.active_rules),
        "unavailable_rules": _json_safe_copy(report.unavailable_rules),
        "care_guideline_snapshot": _select_mapping_fields(
            report.care_guideline_snapshot,
            _CARE_GUIDELINE_FIELDS,
        ),
        "comparison": _select_mapping_fields(
            report.comparison,
            _COMPARISON_FIELDS,
        ),
    }
