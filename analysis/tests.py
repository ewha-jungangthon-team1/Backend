import json
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.renderers import JSONRenderer

from measurements.models import MeasurementSession, SensorReading
from products.models import Bag, ProductModel
from simulation.models import SimulationScenario

from .constants import RuleCode, Severity
from .metrics import calculate_history_metrics
from .models import AnalysisReport
from .rules import evaluate_history_rules
from .serializers import AnalysisReportSerializer
from .services import analyze_history_session


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


class HistoryAnalysisServiceTests(HistoryAnalysisTestCase):
    def add_uniform_readings(
        self,
        session,
        *,
        strap_load="4.00",
        temperature="25.00",
        humidity="50.00",
        moisture_detected=False,
        load_bias="0.1000",
        body_deformation_ratio="0.0100",
    ):
        for sequence in range(7):
            SensorReading.objects.create(
                session=session,
                strap_load=Decimal(strap_load),
                humidity=Decimal(humidity),
                moisture_detected=moisture_detected,
                temperature=Decimal(temperature),
                measured_at=self.started_at + timedelta(days=sequence),
                load_bias=Decimal(load_bias),
                body_deformation_ratio=Decimal(body_deformation_ratio),
                sequence=sequence,
            )

    def test_creates_normal_history_report(self):
        session = self.create_session()
        self.add_uniform_readings(session)

        report, created = analyze_history_session(session)

        self.assertTrue(created)
        self.assertEqual(report.session, session)
        self.assertEqual(report.severity, Severity.NORMAL.value)
        self.assertEqual(report.active_rules, [])

    def test_creates_single_risk_history_report(self):
        session = self.create_session()
        self.add_uniform_readings(session, strap_load="6.00")

        report, created = analyze_history_session(session)

        self.assertTrue(created)
        self.assertEqual(report.severity, Severity.WARNING.value)
        self.assertEqual(report.active_rules, [RuleCode.HIGH_LOAD.value])

    def test_creates_composite_risk_history_report(self):
        session = self.create_session()
        self.add_readings(session)

        report, created = analyze_history_session(session)

        self.assertTrue(created)
        self.assertEqual(report.severity, Severity.WARNING.value)
        self.assertEqual(
            set(report.active_rules),
            {rule_code.value for rule_code in RuleCode},
        )

    def test_reanalysis_updates_existing_report(self):
        session = self.create_session()
        self.add_uniform_readings(session)

        first_report, first_created = analyze_history_session(session)

        first_reading = session.readings.order_by("sequence").first()
        first_reading.strap_load = Decimal("6.00")
        first_reading.save(update_fields=["strap_load"])
        updated_guideline = self.product_model.care_guideline.copy()
        updated_guideline["note"] = "updated guideline"
        self.product_model.care_guideline = updated_guideline
        self.product_model.save(update_fields=["care_guideline"])
        session = MeasurementSession.objects.select_related(
            "bag__product_model"
        ).get(pk=session.pk)

        second_report, second_created = analyze_history_session(session)

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first_report.pk, second_report.pk)
        self.assertEqual(
            AnalysisReport.objects.filter(session=session).count(), 1
        )
        self.assertEqual(second_report.metrics["load"]["max_kg"], 6.0)
        self.assertEqual(
            second_report.active_rules, [RuleCode.HIGH_LOAD.value]
        )
        self.assertEqual(
            second_report.care_guideline_snapshot["note"],
            "updated guideline",
        )

    def test_care_guideline_snapshot_is_independent(self):
        session = self.create_session()
        self.add_uniform_readings(session)
        source_guideline = session.bag.product_model.care_guideline

        report, _created = analyze_history_session(session)
        source_guideline["max_load_kg"] = 999

        self.assertEqual(report.care_guideline_snapshot["max_load_kg"], 5.5)

        self.product_model.care_guideline = {"max_load_kg": 999}
        self.product_model.save(update_fields=["care_guideline"])
        report.refresh_from_db()

        self.assertEqual(report.care_guideline_snapshot["max_load_kg"], 5.5)

    def test_rejects_live_session_without_creating_report(self):
        session = self.create_session(purpose=MeasurementSession.Purpose.LIVE)
        self.add_uniform_readings(session)

        with self.assertRaisesMessage(ValueError, "Only HISTORY sessions"):
            analyze_history_session(session)

        self.assertFalse(AnalysisReport.objects.filter(session=session).exists())

    def test_rejects_running_history_without_creating_report(self):
        session = self.create_session(status=MeasurementSession.Status.RUNNING)
        self.add_uniform_readings(session)

        with self.assertRaisesMessage(ValueError, "Only COMPLETED sessions"):
            analyze_history_session(session)

        self.assertFalse(AnalysisReport.objects.filter(session=session).exists())

    def test_rejects_history_without_readings_or_report(self):
        session = self.create_session()

        with self.assertRaisesMessage(ValueError, "At least one SensorReading"):
            analyze_history_session(session)

        self.assertFalse(AnalysisReport.objects.filter(session=session).exists())


class AnalysisReportSerializerTests(HistoryAnalysisTestCase):
    def create_scenario(self, code="NORMAL_HISTORY"):
        return SimulationScenario.objects.create(
            code=code,
            name="Normal History",
            scenario_type=SimulationScenario.ScenarioType.NORMAL,
            mode=SimulationScenario.Mode.HISTORY,
            logical_duration_seconds=604800,
            sample_interval_seconds=86400,
            config={},
        )

    def test_serializes_analysis_report_fields_and_values(self):
        scenario = self.create_scenario()
        session = self.create_session()
        session.scenario = scenario
        session.save(update_fields=["scenario"])
        self.add_readings(session)
        report, _created = analyze_history_session(session)

        data = AnalysisReportSerializer(report).data

        self.assertEqual(
            set(data),
            {
                "id",
                "session_id",
                "scenario_code",
                "metrics",
                "severity",
                "active_rules",
                "unavailable_rules",
                "care_guideline_snapshot",
                "created_at",
                "updated_at",
            },
        )
        self.assertEqual(data["id"], report.id)
        self.assertEqual(data["session_id"], report.session_id)
        self.assertEqual(data["scenario_code"], scenario.code)
        self.assertEqual(data["metrics"], report.metrics)
        self.assertEqual(data["severity"], report.severity)
        self.assertEqual(data["active_rules"], report.active_rules)
        self.assertEqual(data["unavailable_rules"], report.unavailable_rules)
        self.assertEqual(
            data["care_guideline_snapshot"],
            report.care_guideline_snapshot,
        )
        JSONRenderer().render(data)

    def test_serializes_null_scenario_code(self):
        session = self.create_session()
        self.add_readings(session)
        report, _created = analyze_history_session(session)

        data = AnalysisReportSerializer(report).data

        self.assertIsNone(data["scenario_code"])

    def test_all_fields_are_read_only(self):
        serializer = AnalysisReportSerializer(
            data={
                "id": 999,
                "session_id": 999,
                "scenario_code": "OVERLOAD_HISTORY",
                "metrics": {"tampered": True},
                "severity": Severity.DANGER.value,
                "active_rules": [RuleCode.HIGH_LOAD.value],
                "unavailable_rules": [],
                "care_guideline_snapshot": {"max_load_kg": 999},
                "created_at": "2026-08-12T00:00:00Z",
                "updated_at": "2026-08-12T00:00:00Z",
            }
        )

        self.assertTrue(serializer.is_valid())
        self.assertEqual(serializer.validated_data, {})
