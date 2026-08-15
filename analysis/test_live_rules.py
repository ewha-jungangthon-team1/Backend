from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from measurements.models import MeasurementSession, SensorReading
from products.models import Bag, ProductModel
from simulation.models import SimulationScenario
from simulation.services import get_latest_reading

from .constants import RuleCode
from .live_rules import evaluate_live_rules, evaluate_live_session_rules


BASE_GUIDELINE = {
    "avoid_moisture": True,
    "max_load_kg": 5.5,
    "recommended_temp_range_c": [0, 35],
    "max_humidity_percent": 60,
    "max_abs_load_bias": 0.30,
    "max_body_deformation_ratio": 0.03,
}

BASE_READING = {
    "strap_load": 3.0,
    "temperature": 25.0,
    "humidity": 50.0,
    "load_bias": 0.10,
    "body_deformation_ratio": 0.01,
    "material_moisture_percent": None,
    "moisture_detected": False,
}


class LiveRuleEvaluatorTests(SimpleTestCase):
    def evaluate(self, reading=None, guideline=None, moisture_seen=False):
        return evaluate_live_rules(
            BASE_READING | (reading or {}),
            BASE_GUIDELINE | (guideline or {}),
            moisture_seen,
        )

    def test_numeric_rule_boundaries_use_strict_greater_than(self):
        cases = [
            (
                RuleCode.HIGH_LOAD.value,
                "strap_load",
                (5.49, 5.5, 5.51),
            ),
            (
                RuleCode.HIGH_TEMPERATURE.value,
                "temperature",
                (34.9, 35, 35.1),
            ),
            (
                RuleCode.HIGH_HUMIDITY.value,
                "humidity",
                (59.9, 60, 60.1),
            ),
            (
                RuleCode.LOAD_BIAS.value,
                "load_bias",
                (0.29, 0.30, 0.31),
            ),
            (
                RuleCode.DEFORMATION.value,
                "body_deformation_ratio",
                (0.029, 0.03, 0.031),
            ),
        ]

        for rule_code, reading_key, (below, equal, above) in cases:
            with self.subTest(rule=rule_code, position="below"):
                result = self.evaluate({reading_key: below})
                self.assertNotIn(rule_code, result["active_rules"])
            with self.subTest(rule=rule_code, position="equal"):
                result = self.evaluate({reading_key: equal})
                self.assertNotIn(rule_code, result["active_rules"])
            with self.subTest(rule=rule_code, position="above"):
                result = self.evaluate({reading_key: above})
                self.assertIn(rule_code, result["active_rules"])

    def test_negative_load_bias_uses_absolute_value(self):
        result = self.evaluate({"load_bias": -0.31})

        self.assertEqual(result["active_rules"], [RuleCode.LOAD_BIAS.value])

    def test_moisture_requires_product_policy_and_session_history(self):
        cases = [
            (False, True, []),
            (True, False, []),
            (True, True, [RuleCode.MOISTURE.value]),
        ]

        for avoid_moisture, moisture_seen, expected in cases:
            with self.subTest(
                avoid_moisture=avoid_moisture,
                moisture_seen=moisture_seen,
            ):
                result = self.evaluate(
                    guideline={"avoid_moisture": avoid_moisture},
                    moisture_seen=moisture_seen,
                )
                self.assertEqual(result["active_rules"], expected)

    def test_missing_and_invalid_guideline_values_are_unavailable(self):
        result = evaluate_live_rules(BASE_READING, {}, False)

        self.assertEqual(result["active_rules"], [])
        self.assertEqual(
            result["unavailable_rules"],
            [
                RuleCode.HIGH_LOAD.value,
                RuleCode.HIGH_TEMPERATURE.value,
                RuleCode.HIGH_HUMIDITY.value,
                RuleCode.LOAD_BIAS.value,
                RuleCode.DEFORMATION.value,
                RuleCode.MOISTURE.value,
            ],
        )

        malformed = self.evaluate(
            guideline={"recommended_temp_range_c": [0]},
        )
        self.assertEqual(
            malformed["unavailable_rules"],
            [RuleCode.HIGH_TEMPERATURE.value],
        )

    def test_missing_current_numeric_value_is_unavailable(self):
        current_reading = BASE_READING.copy()
        current_reading.pop("temperature")

        result = evaluate_live_rules(current_reading, BASE_GUIDELINE, False)

        self.assertEqual(
            result["unavailable_rules"],
            [RuleCode.HIGH_TEMPERATURE.value],
        )

    def test_product_a_intended_progression(self):
        product_a_guideline = BASE_GUIDELINE | {"max_humidity_percent": 70}
        stages = [
            ({}, []),
            (
                {"temperature": 35.1},
                [RuleCode.HIGH_TEMPERATURE.value],
            ),
            (
                {"temperature": 35.1, "load_bias": 0.31},
                [
                    RuleCode.HIGH_TEMPERATURE.value,
                    RuleCode.LOAD_BIAS.value,
                ],
            ),
            (
                {
                    "temperature": 35.1,
                    "load_bias": 0.31,
                    "body_deformation_ratio": 0.031,
                },
                [
                    RuleCode.HIGH_TEMPERATURE.value,
                    RuleCode.LOAD_BIAS.value,
                    RuleCode.DEFORMATION.value,
                ],
            ),
        ]

        for reading, expected in stages:
            with self.subTest(reading=reading):
                result = evaluate_live_rules(
                    BASE_READING | reading,
                    product_a_guideline,
                    False,
                )
                self.assertEqual(result["active_rules"], expected)
                self.assertNotIn(RuleCode.HIGH_LOAD.value, result["active_rules"])
                self.assertNotIn(
                    RuleCode.HIGH_HUMIDITY.value,
                    result["active_rules"],
                )
                self.assertNotIn(RuleCode.MOISTURE.value, result["active_rules"])

    def test_product_b_intended_progression(self):
        event_only = self.evaluate(moisture_seen=True)
        event_and_humidity = self.evaluate(
            {"humidity": 60.1},
            moisture_seen=True,
        )

        self.assertEqual(
            event_only["active_rules"],
            [RuleCode.MOISTURE.value],
        )
        self.assertEqual(
            event_and_humidity["active_rules"],
            [
                RuleCode.HIGH_HUMIDITY.value,
                RuleCode.MOISTURE.value,
            ],
        )
        for inactive_rule in (
            RuleCode.HIGH_LOAD.value,
            RuleCode.HIGH_TEMPERATURE.value,
            RuleCode.LOAD_BIAS.value,
            RuleCode.DEFORMATION.value,
        ):
            self.assertNotIn(inactive_rule, event_and_humidity["active_rules"])

    def test_high_material_moisture_alone_does_not_activate_a_rule(self):
        result = self.evaluate(
            {"material_moisture_percent": 90},
            moisture_seen=False,
        )

        self.assertEqual(result["active_rules"], [])

    def test_active_rule_order_matches_history_rule_order(self):
        result = self.evaluate(
            {
                "strap_load": 5.51,
                "temperature": 35.1,
                "humidity": 60.1,
                "load_bias": -0.31,
                "body_deformation_ratio": 0.031,
            },
            moisture_seen=True,
        )

        self.assertEqual(
            result["active_rules"],
            [
                RuleCode.HIGH_LOAD.value,
                RuleCode.HIGH_TEMPERATURE.value,
                RuleCode.HIGH_HUMIDITY.value,
                RuleCode.LOAD_BIAS.value,
                RuleCode.DEFORMATION.value,
                RuleCode.MOISTURE.value,
            ],
        )


class LiveSessionRuleEvaluatorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        owner = get_user_model().objects.create_user(
            username="live-rule-owner",
            password="test-password",
        )
        product = ProductModel.objects.create(
            brand="Test Brand",
            model_name="Live Rule Bag",
            material="Leather",
            care_guideline=BASE_GUIDELINE,
        )
        bag = Bag.objects.create(
            product_model=product,
            owner=owner,
            nfc_uid="LIVE-RULE-NFC",
        )
        scenario = SimulationScenario.objects.create(
            code="LIVE_RULE_SCENARIO",
            name="Live Rule Scenario",
            scenario_type=SimulationScenario.ScenarioType.HIGH_TEMPERATURE,
            mode=SimulationScenario.Mode.LIVE,
            logical_duration_seconds=86400,
            sample_interval_seconds=3600,
            config={},
        )
        cls.session = MeasurementSession.objects.create(
            bag=bag,
            scenario=scenario,
            purpose=MeasurementSession.Purpose.LIVE,
            seed=12345,
            started_at=timezone.now(),
            status=MeasurementSession.Status.RUNNING,
        )
        cls.first_reading = SensorReading.objects.create(
            session=cls.session,
            sequence=0,
            measured_at=cls.session.started_at,
            strap_load=3,
            humidity=50,
            temperature=34,
            load_bias=0.1,
            body_deformation_ratio=0.01,
            moisture_detected=True,
        )
        cls.latest_reading = SensorReading.objects.create(
            session=cls.session,
            sequence=1,
            measured_at=cls.session.started_at + timedelta(hours=1),
            strap_load=3,
            humidity=50,
            temperature=36,
            load_bias=0.1,
            body_deformation_ratio=0.01,
            moisture_detected=False,
        )

    def current_reading(self, **overrides):
        return BASE_READING | {"moisture_detected": False} | overrides

    def test_wrapper_latches_prior_moisture_with_one_exists_query(self):
        session = MeasurementSession.objects.select_related(
            "bag__product_model"
        ).get(pk=self.session.pk)

        with self.assertNumQueries(1):
            result = evaluate_live_session_rules(
                session,
                self.current_reading(),
            )

        self.assertEqual(result["active_rules"], [RuleCode.MOISTURE.value])

    def test_numeric_rules_use_interpolated_latest_reading_values(self):
        session = MeasurementSession.objects.select_related(
            "bag__product_model", "scenario"
        ).get(pk=self.session.pk)

        with (
            patch(
                "simulation.services.calculate_progress",
                return_value=(1, 24, 0.25),
            ),
            patch(
                "simulation.services.ensure_readings_up_to_now",
                return_value=self.latest_reading,
            ),
            patch(
                "simulation.services.calculate_overall_progress_ratio",
                return_value=0.5,
            ),
        ):
            below_threshold = get_latest_reading(session)

        with (
            patch(
                "simulation.services.calculate_progress",
                return_value=(1, 24, 0.75),
            ),
            patch(
                "simulation.services.ensure_readings_up_to_now",
                return_value=self.latest_reading,
            ),
            patch(
                "simulation.services.calculate_overall_progress_ratio",
                return_value=0.5,
            ),
        ):
            above_threshold = get_latest_reading(session)

        self.assertEqual(below_threshold["temperature"], 34.5)
        self.assertEqual(above_threshold["temperature"], 35.5)
        self.assertNotIn(
            RuleCode.HIGH_TEMPERATURE.value,
            evaluate_live_session_rules(session, below_threshold)["active_rules"],
        )
        self.assertIn(
            RuleCode.HIGH_TEMPERATURE.value,
            evaluate_live_session_rules(session, above_threshold)["active_rules"],
        )
