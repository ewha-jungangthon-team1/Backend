import json
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase

from measurements.models import MeasurementSession, SensorReading
from products.models import Bag, ProductModel

from .constants import RuleCode, Severity
from .metrics import calculate_history_metrics
from .rules import evaluate_history_rules


class HistoryAnalysisTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        owner = get_user_model().objects.create_user(
            username="analysis-owner",
            password="test-password",
        )
        cls.product_model = ProductModel.objects.create(
            brand="Test Brand",
            model_name="Test Bag",
            material="Leather",
            care_guideline={
                "avoid_moisture": True,
                "max_load_kg": 5.5,
                "recommended_temp_range_c": [0, 35],
                "max_humidity_percent": 70,
                "max_abs_load_bias": 0.30,
                "max_body_deformation_ratio": 0.03,
                "care_actions": {
                    rule_code.value: {
                        "title": f"{rule_code.value} care",
                        "reason": f"{rule_code.value} reason",
                        "steps": [f"{rule_code.value} step"],
                    }
                    for rule_code in RuleCode
                },
            },
        )
        cls.bag = Bag.objects.create(
            product_model=cls.product_model,
            owner=owner,
            nfc_uid="ANALYSIS-TEST-NFC",
        )
        cls.started_at = datetime(2026, 7, 28, 9, tzinfo=ZoneInfo("Asia/Seoul"))

    def create_session(self, *, purpose="history", status="COMPLETED"):
        return MeasurementSession.objects.create(
            bag=self.bag,
            purpose=purpose,
            seed=12345,
            started_at=self.started_at,
            ended_at=self.started_at + timedelta(days=7),
            status=status,
        )

    def add_readings(self, session):
        loads = ["4.00", "5.00", "6.00", "7.00", "8.00", "9.00", "10.00"]
        temperatures = ["20.00", "25.00", "30.00", "35.00", "36.00", "40.00", "34.00"]
        humidities = ["40.00", "50.00", "60.00", "70.00", "80.00", "90.00", "100.00"]
        load_biases = ["-0.1000", "0.2000", "-0.9000", "0.4000", "0.5000", "-0.7000", "0.3000"]
        deformations = ["0.0000", "0.0100", "0.0200", "0.0300", "0.0400", "0.0500", "0.0600"]

        for sequence in range(7):
            SensorReading.objects.create(
                session=session,
                strap_load=Decimal(loads[sequence]),
                humidity=Decimal(humidities[sequence]),
                moisture_detected=sequence in {2, 4},
                temperature=Decimal(temperatures[sequence]),
                measured_at=self.started_at + timedelta(days=sequence),
                load_bias=Decimal(load_biases[sequence]),
                body_deformation_ratio=Decimal(deformations[sequence]),
                sequence=sequence,
            )


class HistoryMetricsTests(HistoryAnalysisTestCase):
    def test_calculates_history_metrics(self):
        session = self.create_session()
        self.add_readings(session)

        metrics = calculate_history_metrics(session)

        self.assertEqual(metrics["reading_count"], 7)
        self.assertEqual(metrics["load"]["average_kg"], 7.0)
        self.assertEqual(metrics["load"]["max_kg"], 10.0)
        self.assertEqual(metrics["load"]["overload_detected_days"], 5)
        self.assertAlmostEqual(metrics["temperature"]["average_c"], 220 / 7)
        self.assertEqual(metrics["temperature"]["max_c"], 40.0)
        self.assertEqual(
            metrics["temperature"]["high_temperature_detected_days"], 2
        )
        self.assertEqual(metrics["humidity"]["average_percent"], 70.0)
        self.assertEqual(metrics["humidity"]["max_percent"], 100.0)
        self.assertEqual(metrics["humidity"]["high_humidity_detected_days"], 3)
        self.assertEqual(metrics["moisture"]["detected_days"], 2)
        self.assertTrue(metrics["moisture"]["detected_any"])
        self.assertEqual(metrics["load_bias"]["max_absolute"], 0.9)
        self.assertEqual(metrics["load_bias"]["latest"], 0.3)
        self.assertEqual(metrics["load_bias"]["biased_days"], 4)
        self.assertEqual(metrics["deformation"]["max_ratio"], 0.06)
        self.assertEqual(metrics["deformation"]["latest_ratio"], 0.06)
        self.assertEqual(metrics["deformation"]["deformation_detected_days"], 3)

    def test_marks_missing_threshold_metrics_unavailable(self):
        self.product_model.care_guideline = {
            "avoid_moisture": True,
            "max_load_kg": 5.5,
            "recommended_temp_range_c": [0, 35],
        }
        self.product_model.save(update_fields=["care_guideline"])
        self.bag.refresh_from_db()
        session = self.create_session()
        self.add_readings(session)

        metrics = calculate_history_metrics(session)

        self.assertIsNone(metrics["humidity"]["high_humidity_detected_days"])
        self.assertIsNone(metrics["load_bias"]["biased_days"])
        self.assertIsNone(metrics["deformation"]["deformation_detected_days"])

    def test_uses_optional_thresholds_when_they_are_configured(self):
        session = self.create_session()
        self.add_readings(session)

        metrics = calculate_history_metrics(session)

        self.assertEqual(metrics["humidity"]["high_humidity_detected_days"], 3)
        self.assertEqual(metrics["load_bias"]["biased_days"], 4)
        self.assertEqual(metrics["deformation"]["deformation_detected_days"], 3)

    def test_result_is_json_serializable(self):
        session = self.create_session()
        self.add_readings(session)

        metrics = calculate_history_metrics(session)

        json.dumps(metrics)

    def test_rejects_missing_session(self):
        with self.assertRaisesMessage(ValueError, "session is required"):
            calculate_history_metrics(None)

    def test_rejects_missing_readings(self):
        session = self.create_session()

        with self.assertRaisesMessage(ValueError, "At least one SensorReading"):
            calculate_history_metrics(session)

    def test_rejects_non_history_session(self):
        session = self.create_session(purpose=MeasurementSession.Purpose.LIVE)
        self.add_readings(session)

        with self.assertRaisesMessage(ValueError, "Only HISTORY sessions"):
            calculate_history_metrics(session)

    def test_rejects_running_session(self):
        session = self.create_session(status=MeasurementSession.Status.RUNNING)
        self.add_readings(session)

        with self.assertRaisesMessage(ValueError, "Only COMPLETED sessions"):
            calculate_history_metrics(session)


class HistoryRuleEngineTests(HistoryAnalysisTestCase):
    def build_metrics(
        self,
        *,
        overload_days=0,
        high_temperature_days=0,
        high_humidity_days=None,
        moisture=False,
        biased_days=None,
        deformation_days=None,
    ):
        return {
            "load": {"overload_detected_days": overload_days},
            "temperature": {
                "high_temperature_detected_days": high_temperature_days
            },
            "humidity": {"high_humidity_detected_days": high_humidity_days},
            "moisture": {"detected_any": moisture},
            "load_bias": {"biased_days": biased_days},
            "deformation": {"deformation_detected_days": deformation_days},
        }

    def test_returns_normal_when_no_available_rule_is_active(self):
        result = evaluate_history_rules(
            self.build_metrics(), self.product_model.care_guideline
        )

        self.assertEqual(result["severity"], Severity.NORMAL.value)
        self.assertEqual(result["active_rules"], [])
        self.assertEqual(
            result["unavailable_rules"],
            [
                RuleCode.HIGH_HUMIDITY.value,
                RuleCode.LOAD_BIAS.value,
                RuleCode.DEFORMATION.value,
            ],
        )

    def test_high_load_returns_warning(self):
        result = evaluate_history_rules(
            self.build_metrics(overload_days=1),
            self.product_model.care_guideline,
        )

        self.assertEqual(result["severity"], Severity.WARNING.value)
        self.assertIn(RuleCode.HIGH_LOAD.value, result["active_rules"])

    def test_high_temperature_returns_warning(self):
        result = evaluate_history_rules(
            self.build_metrics(high_temperature_days=1),
            self.product_model.care_guideline,
        )

        self.assertEqual(result["severity"], Severity.WARNING.value)
        self.assertIn(RuleCode.HIGH_TEMPERATURE.value, result["active_rules"])

    def test_moisture_returns_warning_when_policy_avoids_moisture(self):
        result = evaluate_history_rules(
            self.build_metrics(moisture=True),
            self.product_model.care_guideline,
        )

        self.assertEqual(result["severity"], Severity.WARNING.value)
        self.assertIn(RuleCode.MOISTURE.value, result["active_rules"])

    def test_unavailable_rules_are_not_marked_active(self):
        result = evaluate_history_rules(
            self.build_metrics(
                high_humidity_days=None,
                biased_days=None,
                deformation_days=None,
            ),
            self.product_model.care_guideline,
        )

        self.assertNotIn(RuleCode.HIGH_HUMIDITY.value, result["active_rules"])
        self.assertNotIn(RuleCode.LOAD_BIAS.value, result["active_rules"])
        self.assertNotIn(RuleCode.DEFORMATION.value, result["active_rules"])
