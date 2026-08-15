import json
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from enum import Enum

from django.utils import timezone

from measurements.home import build_sensor_presentation_values
from measurements.models import MeasurementSession
from simulation.services import get_latest_reading

from .live_rules import evaluate_live_session_rules
from .live_state import build_live_state


RAW_SENSOR_FIELDS = (
    "strap_load",
    "load_bias",
    "body_deformation_ratio",
    "temperature",
    "humidity",
    "material_moisture_percent",
    "moisture_detected",
)

GUIDELINE_THRESHOLD_FIELDS = (
    "max_load_kg",
    "recommended_temp_range_c",
    "max_humidity_percent",
    "max_abs_load_bias",
    "max_body_deformation_ratio",
    "avoid_moisture",
)


def _as_project_timezone_iso(value):
    if value is None:
        return None
    project_timezone = timezone.get_default_timezone()
    if timezone.is_naive(value):
        value = timezone.make_aware(value, project_timezone)
    return timezone.localtime(value, project_timezone).isoformat()


def _json_compatible(value):
    if isinstance(value, dict):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_compatible(item) for item in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return _as_project_timezone_iso(value)
    if isinstance(value, Enum):
        return _json_compatible(value.value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return deepcopy(value)
    raise ValueError(
        f"Unsupported LIVE care context value type: {type(value).__name__}."
    )


def _project_relevant_care_actions(
    care_guideline,
    active_rules,
    primary_rule,
):
    configured_actions = care_guideline.get("care_actions")
    if not isinstance(configured_actions, dict):
        return {}

    ordered_rules = []
    if isinstance(primary_rule, str) and primary_rule in active_rules:
        ordered_rules.append(primary_rule)
    ordered_rules.extend(
        rule_code for rule_code in active_rules if rule_code not in ordered_rules
    )

    return {
        rule_code: _json_compatible(configured_actions[rule_code])
        for rule_code in ordered_rules
        if isinstance(configured_actions.get(rule_code), dict)
    }


def build_live_care_context(session):
    if session is None or not isinstance(session, MeasurementSession):
        raise ValueError("session is required.")
    if session.pk is None:
        raise ValueError("session must be saved.")
    if session.purpose != MeasurementSession.Purpose.LIVE:
        raise ValueError("Only LIVE sessions can build detailed care context.")
    if session.scenario_id is None:
        raise ValueError("A scenario is required to build detailed LIVE care context.")

    current_reading = get_latest_reading(session)
    if current_reading is None:
        raise ValueError("At least one current LIVE reading is required.")

    product = session.bag.product_model
    care_guideline = (
        product.care_guideline
        if isinstance(product.care_guideline, dict)
        else {}
    )
    rule_result = evaluate_live_session_rules(session, current_reading)
    state = build_live_state(rule_result, care_guideline)
    presentation = build_sensor_presentation_values(
        strap_load=current_reading["strap_load"],
        load_bias=current_reading["load_bias"],
        body_deformation_ratio=current_reading["body_deformation_ratio"],
        temperature=current_reading["temperature"],
        humidity=current_reading["humidity"],
        material_moisture_percent=current_reading.get(
            "material_moisture_percent"
        ),
    )

    active_rules = list(rule_result["active_rules"])
    unavailable_rules = list(rule_result["unavailable_rules"])
    primary_rule = state.get("primary_rule")
    context = {
        "product": {
            "brand": product.brand,
            "model_name": product.model_name,
            "material": product.material,
        },
        "observation": {
            "session_id": session.pk,
            "sequence": current_reading.get("sequence"),
            "observed_at": _as_project_timezone_iso(
                current_reading.get("observed_at")
            ),
        },
        "sensor_facts": {
            "raw": {
                field_name: _json_compatible(current_reading.get(field_name))
                for field_name in RAW_SENSOR_FIELDS
            },
            "presentation": _json_compatible(presentation),
        },
        "interpretation": {
            "active_rules": active_rules,
            "unavailable_rules": unavailable_rules,
            "state_code": state.get("code"),
            "primary_rule": primary_rule,
            "quick_care": state.get("quick_care"),
        },
        "guideline": {
            "thresholds": {
                field_name: _json_compatible(care_guideline.get(field_name))
                for field_name in GUIDELINE_THRESHOLD_FIELDS
            },
            "relevant_care_actions": _project_relevant_care_actions(
                care_guideline,
                active_rules,
                primary_rule,
            ),
        },
    }

    serialized = json.dumps(context, ensure_ascii=False, allow_nan=False)
    return json.loads(serialized)
