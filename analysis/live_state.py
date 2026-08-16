from collections.abc import Mapping


STATE_COPY_FIELDS = (
    "code",
    "headline",
    "description",
    "quick_care",
    "theme_key",
)


def _empty_state(active_rules, unavailable_rules):
    return {
        "code": None,
        "primary_rule": None,
        "active_rules": active_rules,
        "unavailable_rules": unavailable_rules,
        "headline": None,
        "description": None,
        "quick_care": None,
        "theme_key": None,
    }


def _build_configured_state(
    configured_state,
    active_rules,
    unavailable_rules,
    *,
    primary_rule=None,
):
    if not isinstance(configured_state, Mapping):
        return None
    if any(
        not isinstance(configured_state.get(field), str)
        or not configured_state[field].strip()
        for field in STATE_COPY_FIELDS
    ):
        return None

    return {
        "code": configured_state["code"],
        "primary_rule": primary_rule,
        "active_rules": active_rules,
        "unavailable_rules": unavailable_rules,
        "headline": configured_state["headline"],
        "description": configured_state["description"],
        "quick_care": configured_state["quick_care"],
        "theme_key": configured_state["theme_key"],
    }


def build_live_state(rule_result, care_guideline):
    """Select Product-configured LIVE copy without database access."""
    if not isinstance(rule_result, Mapping):
        rule_result = {}
    active_rules = rule_result.get("active_rules")
    unavailable_rules = rule_result.get("unavailable_rules")
    active_rules = list(active_rules) if isinstance(active_rules, list) else []
    unavailable_rules = (
        list(unavailable_rules) if isinstance(unavailable_rules, list) else []
    )
    empty_state = _empty_state(active_rules, unavailable_rules)

    if not isinstance(care_guideline, Mapping):
        return empty_state
    live_states = care_guideline.get("live_states")
    if not isinstance(live_states, Mapping):
        return empty_state

    if not active_rules:
        stable = _build_configured_state(
            live_states.get("stable"),
            active_rules,
            unavailable_rules,
        )
        return stable or empty_state

    configured_states = live_states.get("states")
    if isinstance(configured_states, list):
        for configured_state in configured_states:
            if not isinstance(configured_state, Mapping):
                continue
            required_rules = configured_state.get("required_rules")
            primary_rule = configured_state.get("primary_rule")
            if (
                not isinstance(required_rules, list)
                or not required_rules
                or any(
                    not isinstance(required_rule, str) or not required_rule
                    for required_rule in required_rules
                )
                or not isinstance(primary_rule, str)
                or primary_rule not in required_rules
            ):
                continue
            if not all(
                required_rule in active_rules for required_rule in required_rules
            ):
                continue

            selected_state = _build_configured_state(
                configured_state,
                active_rules,
                unavailable_rules,
                primary_rule=primary_rule,
            )
            if selected_state is not None:
                return selected_state

    fallback = _build_configured_state(
        live_states.get("fallback_active"),
        active_rules,
        unavailable_rules,
    )
    return fallback or empty_state
