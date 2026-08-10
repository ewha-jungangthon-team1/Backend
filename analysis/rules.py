from .constants import RuleCode, Severity


def evaluate_history_rules(metrics, care_guideline):
    if not isinstance(metrics, dict):
        raise ValueError("metrics must be a dictionary.")
    if not isinstance(care_guideline, dict):
        care_guideline = {}

    active_rules = []
    unavailable_rules = []

    candidates = [
        (
            RuleCode.HIGH_LOAD,
            metrics["load"]["overload_detected_days"],
        ),
        (
            RuleCode.HIGH_TEMPERATURE,
            metrics["temperature"]["high_temperature_detected_days"],
        ),
        (
            RuleCode.HIGH_HUMIDITY,
            metrics["humidity"]["high_humidity_detected_days"],
        ),
        (
            RuleCode.LOAD_BIAS,
            metrics["load_bias"]["biased_days"],
        ),
        (
            RuleCode.DEFORMATION,
            metrics["deformation"]["deformation_detected_days"],
        ),
    ]

    for rule_code, detected_days in candidates:
        if detected_days is None:
            unavailable_rules.append(rule_code.value)
        elif detected_days > 0:
            active_rules.append(rule_code.value)

    avoid_moisture = care_guideline.get("avoid_moisture")
    if not isinstance(avoid_moisture, bool):
        unavailable_rules.append(RuleCode.MOISTURE.value)
    elif avoid_moisture and metrics["moisture"]["detected_any"]:
        active_rules.append(RuleCode.MOISTURE.value)

    return {
        "severity": (
            Severity.WARNING.value if active_rules else Severity.NORMAL.value
        ),
        "active_rules": active_rules,
        "unavailable_rules": unavailable_rules,
    }
