from django.test import SimpleTestCase

from products.management.commands.seed_demo_live_data import (
    PRODUCT_A_LIVE_STATES,
    PRODUCT_B_LIVE_STATES,
)

from .constants import RuleCode
from .live_state import build_live_state


def guideline(live_states):
    return {"live_states": live_states}


class LiveStateBuilderTests(SimpleTestCase):
    def build(self, active_rules, live_states, unavailable_rules=None):
        return build_live_state(
            {
                "active_rules": active_rules,
                "unavailable_rules": unavailable_rules or [],
            },
            guideline(live_states),
        )

    def test_product_a_state_transitions_and_fallbacks(self):
        cases = [
            ([], "STABLE", None),
            (
                [RuleCode.HIGH_TEMPERATURE.value],
                "HEAT_EXPOSURE",
                RuleCode.HIGH_TEMPERATURE.value,
            ),
            (
                [
                    RuleCode.HIGH_TEMPERATURE.value,
                    RuleCode.LOAD_BIAS.value,
                ],
                "HEAT_EXPOSURE",
                RuleCode.HIGH_TEMPERATURE.value,
            ),
            (
                [
                    RuleCode.HIGH_TEMPERATURE.value,
                    RuleCode.LOAD_BIAS.value,
                    RuleCode.DEFORMATION.value,
                ],
                "SHAPE_RISK",
                RuleCode.DEFORMATION.value,
            ),
            ([RuleCode.LOAD_BIAS.value], "ATTENTION", None),
            ([RuleCode.DEFORMATION.value], "ATTENTION", None),
        ]

        for active_rules, expected_code, expected_primary in cases:
            with self.subTest(active_rules=active_rules):
                result = self.build(active_rules, PRODUCT_A_LIVE_STATES)
                self.assertEqual(result["code"], expected_code)
                self.assertEqual(result["primary_rule"], expected_primary)
                self.assertEqual(result["active_rules"], active_rules)

    def test_product_b_state_transitions_and_fallbacks(self):
        cases = [
            ([], "STABLE", None),
            (
                [RuleCode.MOISTURE.value],
                "MOISTURE_CONTACT",
                RuleCode.MOISTURE.value,
            ),
            (
                [
                    RuleCode.MOISTURE.value,
                    RuleCode.HIGH_HUMIDITY.value,
                ],
                "HUMIDITY_RETENTION",
                RuleCode.HIGH_HUMIDITY.value,
            ),
            ([RuleCode.HIGH_HUMIDITY.value], "ATTENTION", None),
            ([RuleCode.DEFORMATION.value], "ATTENTION", None),
        ]

        for active_rules, expected_code, expected_primary in cases:
            with self.subTest(active_rules=active_rules):
                result = self.build(active_rules, PRODUCT_B_LIVE_STATES)
                self.assertEqual(result["code"], expected_code)
                self.assertEqual(result["primary_rule"], expected_primary)

    def test_matching_is_order_independent_but_output_order_is_preserved(self):
        first_order = [
            RuleCode.HIGH_HUMIDITY.value,
            RuleCode.MOISTURE.value,
        ]
        second_order = list(reversed(first_order))

        first = self.build(first_order, PRODUCT_B_LIVE_STATES)
        second = self.build(second_order, PRODUCT_B_LIVE_STATES)

        self.assertEqual(first["code"], "HUMIDITY_RETENTION")
        self.assertEqual(second["code"], "HUMIDITY_RETENTION")
        self.assertEqual(first["active_rules"], first_order)
        self.assertEqual(second["active_rules"], second_order)

    def test_first_matching_state_wins_and_allows_additional_rules(self):
        active_rules = [
            RuleCode.HIGH_LOAD.value,
            RuleCode.HIGH_TEMPERATURE.value,
            RuleCode.LOAD_BIAS.value,
            RuleCode.DEFORMATION.value,
        ]

        result = self.build(active_rules, PRODUCT_A_LIVE_STATES)

        self.assertEqual(result["code"], "SHAPE_RISK")
        self.assertEqual(result["active_rules"], active_rules)

    def test_unavailable_rules_are_preserved_without_blocking_a_match(self):
        result = self.build(
            [RuleCode.MOISTURE.value],
            PRODUCT_B_LIVE_STATES,
            unavailable_rules=[RuleCode.HIGH_LOAD.value],
        )

        self.assertEqual(result["code"], "MOISTURE_CONTACT")
        self.assertEqual(
            result["unavailable_rules"],
            [RuleCode.HIGH_LOAD.value],
        )

    def test_seed_copy_is_returned_exactly_for_selected_state(self):
        result = self.build(
            [
                RuleCode.HIGH_TEMPERATURE.value,
                RuleCode.LOAD_BIAS.value,
                RuleCode.DEFORMATION.value,
            ],
            PRODUCT_A_LIVE_STATES,
        )
        configured = PRODUCT_A_LIVE_STATES["states"][0]

        for field in ("code", "headline", "description", "quick_care", "theme_key"):
            self.assertEqual(result[field], configured[field])

    def test_missing_config_returns_null_copy_and_preserves_rules(self):
        rule_result = {
            "active_rules": [RuleCode.HIGH_LOAD.value],
            "unavailable_rules": [RuleCode.MOISTURE.value],
        }

        result = build_live_state(rule_result, {})

        self.assertEqual(
            result,
            {
                "code": None,
                "primary_rule": None,
                "active_rules": rule_result["active_rules"],
                "unavailable_rules": rule_result["unavailable_rules"],
                "headline": None,
                "description": None,
                "quick_care": None,
                "theme_key": None,
            },
        )

    def test_invalid_state_items_are_skipped_and_fallback_is_used(self):
        invalid_states = {
            "stable": PRODUCT_A_LIVE_STATES["stable"],
            "states": [
                "not-a-mapping",
                {
                    "code": "INVALID",
                    "required_rules": [RuleCode.HIGH_LOAD.value],
                    "primary_rule": RuleCode.DEFORMATION.value,
                    "headline": "invalid",
                    "description": "invalid",
                    "quick_care": "invalid",
                    "theme_key": "invalid",
                },
            ],
            "fallback_active": PRODUCT_A_LIVE_STATES["fallback_active"],
        }

        result = self.build([RuleCode.HIGH_LOAD.value], invalid_states)

        self.assertEqual(result["code"], "ATTENTION")

    def test_invalid_stable_config_returns_null_copy_without_crashing(self):
        result = self.build([], {"stable": {"code": "STABLE"}})

        self.assertIsNone(result["code"])
        self.assertIsNone(result["headline"])
