from collections.abc import Mapping

from measurements.models import MeasurementSession

from .constants import RuleCode
from .metrics import _as_float, _get_thresholds


def evaluate_live_rules(
    current_reading,
    care_guideline,
    moisture_seen_in_session,
):
    """Evaluate current LIVE raw values without performing database queries."""
    if not isinstance(current_reading, Mapping):
        current_reading = {}
    if not isinstance(care_guideline, dict):
        care_guideline = {}

    thresholds = _get_thresholds(care_guideline)
    candidates = [
        (
            RuleCode.HIGH_LOAD,
            _as_float(current_reading.get("strap_load")),
            thresholds["max_load_kg"],
        ),
        (
            RuleCode.HIGH_TEMPERATURE,
            _as_float(current_reading.get("temperature")),
            thresholds["max_temperature_c"],
        ),
        (
            RuleCode.HIGH_HUMIDITY,
            _as_float(current_reading.get("humidity")),
            thresholds["max_humidity_percent"],
        ),
        (
            RuleCode.LOAD_BIAS,
            _as_float(current_reading.get("load_bias")),
            thresholds["max_abs_load_bias"],
        ),
        (
            RuleCode.DEFORMATION,
            _as_float(current_reading.get("body_deformation_ratio")),
            thresholds["max_body_deformation_ratio"],
        ),
    ]

    active_rules = []
    unavailable_rules = []
    for rule_code, current_value, threshold in candidates:
        if current_value is None or threshold is None:
            unavailable_rules.append(rule_code.value)
            continue

        comparison_value = (
            abs(current_value)
            if rule_code is RuleCode.LOAD_BIAS
            else current_value
        )
        if comparison_value > threshold:
            active_rules.append(rule_code.value)

    avoid_moisture = care_guideline.get("avoid_moisture")
    if not isinstance(avoid_moisture, bool):
        unavailable_rules.append(RuleCode.MOISTURE.value)
    elif avoid_moisture:
        if not isinstance(moisture_seen_in_session, bool):
            unavailable_rules.append(RuleCode.MOISTURE.value)
        elif moisture_seen_in_session:
            active_rules.append(RuleCode.MOISTURE.value)

    return {
        "active_rules": active_rules,
        "unavailable_rules": unavailable_rules,
    }


def evaluate_live_session_rules(session, current_reading):
    """Resolve session moisture history, then delegate to the pure evaluator."""
    if session is None:
        raise ValueError("session is required.")
    if session.purpose != MeasurementSession.Purpose.LIVE:
        raise ValueError("Only LIVE sessions can be evaluated.")

    care_guideline = session.bag.product_model.care_guideline
    avoid_moisture = (
        care_guideline.get("avoid_moisture")
        if isinstance(care_guideline, dict)
        else None
    )
    moisture_seen_in_session = False
    if avoid_moisture is True:
        moisture_seen_in_session = session.readings.filter(
            moisture_detected=True
        ).exists()

    return evaluate_live_rules(
        current_reading,
        care_guideline,
        moisture_seen_in_session,
    )
