import json
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.renderers import JSONRenderer
from rest_framework.test import APIClient

from measurements.models import MeasurementSession, SensorReading
from products.models import Bag, ProductModel
from simulation.models import SimulationScenario

from .comparisons import (
    ComparisonUnavailableReason,
    build_history_metric_comparison,
    find_previous_history_session,
)
from .constants import RuleCode, Severity
from .metrics import build_history_daily_series, calculate_history_metrics
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

    def test_explicit_guideline_overrides_live_thresholds_without_mutation(self):
        session = self.create_session()
        self.add_readings(session)
        self.product_model.care_guideline = {
            "max_load_kg": 10.0,
            "recommended_temp_range_c": [0, 100],
            "max_humidity_percent": 100,
            "max_abs_load_bias": 1.0,
            "max_body_deformation_ratio": 1.0,
        }
        self.product_model.save(update_fields=["care_guideline"])
        explicit_guideline = {
            "max_load_kg": 5.5,
            "recommended_temp_range_c": [0, 35],
            "max_humidity_percent": 70,
            "max_abs_load_bias": 0.30,
            "max_body_deformation_ratio": 0.03,
        }
        original_explicit_guideline = deepcopy(explicit_guideline)

        live_metrics = calculate_history_metrics(session)
        explicit_metrics = calculate_history_metrics(
            session,
            care_guideline=explicit_guideline,
        )

        self.assertEqual(live_metrics["load"]["overload_detected_days"], 0)
        self.assertEqual(explicit_metrics["load"]["overload_detected_days"], 5)
        self.assertEqual(
            explicit_metrics["temperature"]["high_temperature_detected_days"],
            2,
        )
        self.assertEqual(
            explicit_metrics["humidity"]["high_humidity_detected_days"],
            3,
        )
        self.assertEqual(explicit_metrics["load_bias"]["biased_days"], 4)
        self.assertEqual(
            explicit_metrics["deformation"]["deformation_detected_days"],
            3,
        )
        self.assertEqual(
            explicit_metrics["load"]["average_kg"],
            live_metrics["load"]["average_kg"],
        )
        self.assertEqual(
            explicit_metrics["load"]["max_kg"],
            live_metrics["load"]["max_kg"],
        )
        self.assertEqual(
            explicit_metrics["daily_series"],
            live_metrics["daily_series"],
        )
        self.assertEqual(explicit_guideline, original_explicit_guideline)

    def test_explicit_empty_guideline_does_not_fall_back_to_live_guideline(self):
        session = self.create_session()
        self.add_readings(session)

        metrics = calculate_history_metrics(session, care_guideline={})

        self.assertIsNone(metrics["load"]["overload_detected_days"])
        self.assertIsNone(
            metrics["temperature"]["high_temperature_detected_days"]
        )
        self.assertIsNone(metrics["humidity"]["high_humidity_detected_days"])
        self.assertIsNone(metrics["load_bias"]["biased_days"])
        self.assertIsNone(
            metrics["deformation"]["deformation_detected_days"]
        )

    def test_result_is_json_serializable(self):
        session = self.create_session()
        self.add_readings(session)

        metrics = calculate_history_metrics(session)

        json.dumps(metrics)

    def test_includes_daily_series_built_by_shared_builder(self):
        session = self.create_session()
        self.add_readings(session)

        metrics = calculate_history_metrics(session)

        self.assertEqual(len(metrics["daily_series"]), 7)
        self.assertEqual(
            metrics["daily_series"],
            build_history_daily_series(session),
        )

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


class HistoryDailySeriesTests(HistoryAnalysisTestCase):
    def test_builds_seven_daily_items_in_sequence_order(self):
        session = self.create_session()
        self.add_readings(session)

        daily_series = build_history_daily_series(session)

        self.assertEqual(len(daily_series), 7)
        self.assertEqual(
            [item["load_kg"] for item in daily_series],
            [4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0],
        )
        self.assertEqual(
            [item["date"] for item in daily_series],
            [
                "2026-07-28",
                "2026-07-29",
                "2026-07-30",
                "2026-07-31",
                "2026-08-01",
                "2026-08-02",
                "2026-08-03",
            ],
        )
        self.assertEqual(
            [item["moisture_detected"] for item in daily_series],
            [False, False, True, False, True, False, False],
        )

    def test_converts_utc_timestamp_to_project_timezone_date(self):
        session = self.create_session()
        SensorReading.objects.create(
            session=session,
            strap_load=Decimal("3.02"),
            humidity=Decimal("50.00"),
            moisture_detected=False,
            temperature=Decimal("25.00"),
            measured_at=datetime(2026, 8, 3, 15, 30, tzinfo=ZoneInfo("UTC")),
            load_bias=Decimal("0.0000"),
            body_deformation_ratio=Decimal("0.0250"),
            sequence=0,
        )

        daily_series = build_history_daily_series(session)

        self.assertEqual(daily_series[0]["date"], "2026-08-04")

    def test_rounds_numeric_fields_and_preserves_boolean(self):
        reading = Mock(
            strap_load=Decimal("3.026"),
            body_deformation_ratio=Decimal("0.02504"),
            moisture_detected=True,
            measured_at=datetime(2026, 8, 4, 9, tzinfo=ZoneInfo("Asia/Seoul")),
        )
        session = Mock()
        session.readings.order_by.return_value = [reading]

        daily_series = build_history_daily_series(session)

        self.assertEqual(
            daily_series[0],
            {
                "date": "2026-08-04",
                "load_kg": 3.03,
                "deformation_ratio": 0.025,
                "deformation_percent": 2.5,
                "moisture_detected": True,
            },
        )
        self.assertIsInstance(daily_series[0]["load_kg"], float)
        self.assertIsInstance(daily_series[0]["deformation_ratio"], float)
        self.assertIsInstance(daily_series[0]["deformation_percent"], float)
        self.assertIs(daily_series[0]["moisture_detected"], True)
        session.readings.order_by.assert_called_once_with("sequence")

    def test_result_is_json_renderable(self):
        session = self.create_session()
        self.add_readings(session)

        rendered = JSONRenderer().render(build_history_daily_series(session))

        self.assertEqual(len(json.loads(rendered)), 7)


class PreviousHistorySessionSelectorTests(HistoryAnalysisTestCase):
    def create_period(
        self,
        *,
        started_at,
        bag=None,
        purpose=MeasurementSession.Purpose.HISTORY,
        status=MeasurementSession.Status.COMPLETED,
        duration_days=7,
        reading_count=7,
        date_offsets=None,
        measured_at_values=None,
        sequences=None,
        scenario=None,
    ):
        session = MeasurementSession.objects.create(
            bag=bag or self.bag,
            scenario=scenario,
            purpose=purpose,
            seed=12345,
            started_at=started_at,
            ended_at=started_at + timedelta(days=duration_days),
            status=status,
        )
        if date_offsets is None:
            date_offsets = list(range(reading_count))
        if sequences is None:
            sequences = list(range(reading_count))

        for index in range(reading_count):
            SensorReading.objects.create(
                session=session,
                strap_load=Decimal("4.00"),
                humidity=Decimal("50.00"),
                moisture_detected=False,
                temperature=Decimal("25.00"),
                measured_at=(
                    measured_at_values[index]
                    if measured_at_values is not None
                    else started_at + timedelta(days=date_offsets[index])
                ),
                load_bias=Decimal("0.1000"),
                body_deformation_ratio=Decimal("0.0100"),
                sequence=sequences[index],
            )
        return session

    def create_other_bag(self, nfc_uid):
        return Bag.objects.create(
            product_model=self.product_model,
            owner=self.bag.owner,
            nfc_uid=nfc_uid,
        )

    def test_selects_valid_contiguous_previous_period(self):
        previous = self.create_period(started_at=self.started_at)
        current = self.create_period(
            started_at=self.started_at + timedelta(days=7)
        )

        result = find_previous_history_session(current)

        self.assertTrue(result.is_available)
        self.assertEqual(result.previous_session, previous)
        self.assertIsNone(result.reason)

    def test_allows_different_scenarios_when_periods_are_contiguous(self):
        previous_scenario = self.create_scenario("NORMAL_HISTORY")
        current_scenario = self.create_scenario("OVERLOAD_HISTORY")
        previous = self.create_period(
            started_at=self.started_at,
            scenario=previous_scenario,
        )
        current = self.create_period(
            started_at=self.started_at + timedelta(days=7),
            scenario=current_scenario,
        )

        result = find_previous_history_session(current)

        self.assertEqual(result.previous_session, previous)
        self.assertNotEqual(previous.scenario_id, current.scenario_id)

    def test_excludes_contiguous_session_from_different_bag(self):
        other_bag = self.create_other_bag("OTHER-BAG-CONTIGUOUS")
        self.create_period(started_at=self.started_at, bag=other_bag)
        current = self.create_period(
            started_at=self.started_at + timedelta(days=7)
        )

        result = find_previous_history_session(current)

        self.assertIsNone(result.previous_session)
        self.assertEqual(
            result.reason,
            ComparisonUnavailableReason.NO_PREVIOUS_PERIOD,
        )

    def test_excludes_live_candidate(self):
        self.create_period(
            started_at=self.started_at,
            purpose=MeasurementSession.Purpose.LIVE,
        )
        current = self.create_period(
            started_at=self.started_at + timedelta(days=7)
        )

        result = find_previous_history_session(current)

        self.assertEqual(
            result.reason,
            ComparisonUnavailableReason.NO_PREVIOUS_PERIOD,
        )

    def test_excludes_running_history_candidate(self):
        self.create_period(
            started_at=self.started_at,
            status=MeasurementSession.Status.RUNNING,
        )
        current = self.create_period(
            started_at=self.started_at + timedelta(days=7)
        )

        result = find_previous_history_session(current)

        self.assertEqual(
            result.reason,
            ComparisonUnavailableReason.NO_PREVIOUS_PERIOD,
        )

    def test_returns_no_previous_period_without_exact_candidate(self):
        current = self.create_period(started_at=self.started_at)

        result = find_previous_history_session(current)

        self.assertFalse(result.is_available)
        self.assertIsNone(result.previous_session)
        self.assertEqual(
            result.reason,
            ComparisonUnavailableReason.NO_PREVIOUS_PERIOD,
        )

    def test_selects_exact_previous_despite_other_overlapping_history(self):
        previous = self.create_period(started_at=self.started_at)
        current = self.create_period(
            started_at=self.started_at + timedelta(days=7)
        )
        self.create_period(
            started_at=current.started_at + timedelta(days=1)
        )

        result = find_previous_history_session(current)

        self.assertTrue(result.is_available)
        self.assertEqual(result.previous_session, previous)
        self.assertIsNone(result.reason)

    def test_returns_no_previous_when_only_overlapping_history_exists(self):
        current = self.create_period(started_at=self.started_at)
        self.create_period(
            started_at=current.started_at + timedelta(days=1)
        )

        result = find_previous_history_session(current)

        self.assertFalse(result.is_available)
        self.assertIsNone(result.previous_session)
        self.assertEqual(
            result.reason,
            ComparisonUnavailableReason.NO_PREVIOUS_PERIOD,
        )

    def test_boundary_touching_previous_is_not_overlap(self):
        previous = self.create_period(started_at=self.started_at)
        current = self.create_period(
            started_at=self.started_at + timedelta(days=7)
        )

        result = find_previous_history_session(current)

        self.assertEqual(result.previous_session, previous)
        self.assertIsNone(result.reason)

    def test_returns_ambiguous_when_two_previous_candidates_match(self):
        self.create_period(started_at=self.started_at)
        self.create_period(started_at=self.started_at)
        current = self.create_period(
            started_at=self.started_at + timedelta(days=7)
        )

        result = find_previous_history_session(current)

        self.assertIsNone(result.previous_session)
        self.assertEqual(
            result.reason,
            ComparisonUnavailableReason.AMBIGUOUS_PREVIOUS_PERIOD,
        )

    def test_returns_invalid_shape_for_non_seven_day_current(self):
        current = self.create_period(
            started_at=self.started_at,
            duration_days=6,
        )

        result = find_previous_history_session(current)

        self.assertEqual(
            result.reason,
            ComparisonUnavailableReason.INVALID_PERIOD_SHAPE,
        )

    def test_returns_invalid_shape_for_current_with_wrong_reading_count(self):
        current = self.create_period(
            started_at=self.started_at,
            reading_count=6,
        )

        result = find_previous_history_session(current)

        self.assertEqual(
            result.reason,
            ComparisonUnavailableReason.INVALID_PERIOD_SHAPE,
        )

    def test_returns_invalid_shape_for_duplicate_local_dates(self):
        current = self.create_period(
            started_at=self.started_at,
            date_offsets=[0, 1, 2, 3, 4, 5, 5],
        )

        result = find_previous_history_session(current)

        self.assertEqual(
            result.reason,
            ComparisonUnavailableReason.INVALID_PERIOD_SHAPE,
        )

    def test_returns_invalid_shape_when_dates_are_shifted_by_one_day(self):
        measured_at_values = [
            self.started_at + timedelta(days=offset)
            for offset in range(1, 7)
        ]
        measured_at_values.append(
            self.started_at + timedelta(days=7, hours=-1)
        )
        current = self.create_period(
            started_at=self.started_at,
            measured_at_values=measured_at_values,
        )

        result = find_previous_history_session(current)

        self.assertEqual(
            result.reason,
            ComparisonUnavailableReason.INVALID_PERIOD_SHAPE,
        )

    def test_returns_invalid_shape_when_reading_precedes_period_start(self):
        measured_at_values = [
            self.started_at + timedelta(days=offset)
            for offset in range(7)
        ]
        measured_at_values[0] = self.started_at - timedelta(hours=1)
        current = self.create_period(
            started_at=self.started_at,
            measured_at_values=measured_at_values,
        )

        result = find_previous_history_session(current)

        self.assertEqual(
            result.reason,
            ComparisonUnavailableReason.INVALID_PERIOD_SHAPE,
        )

    def test_returns_invalid_shape_when_reading_is_at_exclusive_period_end(self):
        measured_at_values = [
            self.started_at + timedelta(days=offset)
            for offset in range(7)
        ]
        measured_at_values[-1] = self.started_at + timedelta(days=7)
        current = self.create_period(
            started_at=self.started_at,
            measured_at_values=measured_at_values,
        )

        result = find_previous_history_session(current)

        self.assertEqual(
            result.reason,
            ComparisonUnavailableReason.INVALID_PERIOD_SHAPE,
        )

    def test_returns_invalid_shape_for_nonstandard_sequences(self):
        current = self.create_period(
            started_at=self.started_at,
            sequences=[1, 2, 3, 4, 5, 6, 7],
        )

        result = find_previous_history_session(current)

        self.assertEqual(
            result.reason,
            ComparisonUnavailableReason.INVALID_PERIOD_SHAPE,
        )

    def test_returns_invalid_shape_when_previous_shape_is_invalid(self):
        self.create_period(
            started_at=self.started_at,
            reading_count=6,
        )
        current = self.create_period(
            started_at=self.started_at + timedelta(days=7)
        )

        result = find_previous_history_session(current)

        self.assertIsNone(result.previous_session)
        self.assertEqual(
            result.reason,
            ComparisonUnavailableReason.INVALID_PERIOD_SHAPE,
        )

    def test_ignores_created_at_and_pk_order(self):
        current = self.create_period(
            started_at=self.started_at + timedelta(days=7)
        )
        previous = self.create_period(started_at=self.started_at)

        result = find_previous_history_session(current)

        self.assertGreater(previous.pk, current.pk)
        self.assertGreater(previous.created_at, current.created_at)
        self.assertEqual(result.previous_session, previous)

    def test_rejects_invalid_current_session_inputs(self):
        with self.assertRaisesMessage(ValueError, "current_session is required"):
            find_previous_history_session(None)

        live = self.create_period(
            started_at=self.started_at,
            purpose=MeasurementSession.Purpose.LIVE,
        )
        with self.assertRaisesMessage(ValueError, "Only HISTORY sessions"):
            find_previous_history_session(live)

        running = self.create_period(
            started_at=self.started_at,
            status=MeasurementSession.Status.RUNNING,
        )
        with self.assertRaisesMessage(ValueError, "Only COMPLETED sessions"):
            find_previous_history_session(running)

        missing_end = self.create_period(
            started_at=self.started_at,
        )
        missing_end.ended_at = None
        missing_end.save(update_fields=["ended_at"])
        with self.assertRaisesMessage(ValueError, "started_at and ended_at"):
            find_previous_history_session(missing_end)


class HistoryMetricComparisonTests(TestCase):
    def build_metrics_pair(self):
        current = {
            "reading_count": 7,
            "load": {
                "average_kg": 5.0,
                "max_kg": 7.0,
                "overload_detected_days": 3,
            },
            "temperature": {
                "average_c": 30.0,
                "max_c": 35.0,
                "high_temperature_detected_days": 2,
            },
            "humidity": {
                "average_percent": 60.0,
                "max_percent": 75.0,
                "high_humidity_detected_days": 2,
            },
            "moisture": {
                "detected_days": 1,
                "detected_any": True,
            },
            "load_bias": {
                "max_absolute": 0.30,
                "latest": -0.10,
                "biased_days": 2,
            },
            "deformation": {
                "latest_ratio": 0.025,
                "max_ratio": 0.04,
                "deformation_detected_days": 2,
            },
            "daily_series": [{"date": "2026-08-04"}],
        }
        previous = {
            "reading_count": 7,
            "load": {
                "average_kg": 4.0,
                "max_kg": 6.0,
                "overload_detected_days": 0,
            },
            "temperature": {
                "average_c": 25.0,
                "max_c": 32.0,
                "high_temperature_detected_days": 1,
            },
            "humidity": {
                "average_percent": 50.0,
                "max_percent": 70.0,
                "high_humidity_detected_days": 1,
            },
            "moisture": {
                "detected_days": 0,
                "detected_any": False,
            },
            "load_bias": {
                "max_absolute": 0.20,
                "latest": 0.10,
                "biased_days": 1,
            },
            "deformation": {
                "latest_ratio": 0.01,
                "max_ratio": 0.03,
                "deformation_detected_days": 1,
            },
            "daily_series": [{"date": "2026-07-28"}],
        }
        return current, previous

    def test_builds_exact_six_domains_and_fifteen_metrics(self):
        current, previous = self.build_metrics_pair()

        comparison = build_history_metric_comparison(current, previous)

        self.assertEqual(
            set(comparison),
            {
                "load",
                "temperature",
                "humidity",
                "moisture",
                "load_bias",
                "deformation",
            },
        )
        self.assertEqual(
            {domain: set(metrics) for domain, metrics in comparison.items()},
            {
                "load": {"average_kg", "max_kg", "overload_detected_days"},
                "temperature": {
                    "average_c",
                    "max_c",
                    "high_temperature_detected_days",
                },
                "humidity": {
                    "average_percent",
                    "max_percent",
                    "high_humidity_detected_days",
                },
                "moisture": {"detected_days"},
                "load_bias": {"max_absolute_percent", "biased_days"},
                "deformation": {
                    "latest_percent",
                    "max_percent",
                    "deformation_detected_days",
                },
            },
        )

    def test_compares_average_load(self):
        current, previous = self.build_metrics_pair()

        comparison = build_history_metric_comparison(current, previous)

        self.assertEqual(
            comparison["load"]["average_kg"],
            {
                "current": 5.0,
                "previous": 4.0,
                "change": 1.0,
                "change_percent": 25.0,
            },
        )

    def test_zero_to_positive_has_null_relative_change(self):
        current, previous = self.build_metrics_pair()

        comparison = build_history_metric_comparison(current, previous)

        self.assertEqual(
            comparison["load"]["overload_detected_days"],
            {
                "current": 3,
                "previous": 0,
                "change": 3,
                "change_percent": None,
            },
        )

    def test_zero_to_zero_has_zero_relative_change(self):
        current, previous = self.build_metrics_pair()
        current["moisture"]["detected_days"] = 0

        comparison = build_history_metric_comparison(current, previous)

        self.assertEqual(
            comparison["moisture"]["detected_days"],
            {
                "current": 0,
                "previous": 0,
                "change": 0,
                "change_percent": 0.0,
            },
        )

    def test_none_value_preserves_inputs_and_nulls_changes(self):
        current, previous = self.build_metrics_pair()
        previous["humidity"]["high_humidity_detected_days"] = None

        comparison = build_history_metric_comparison(current, previous)

        self.assertEqual(
            comparison["humidity"]["high_humidity_detected_days"],
            {
                "current": 2,
                "previous": None,
                "change": None,
                "change_percent": None,
            },
        )

    def test_temperature_uses_absolute_change_only(self):
        current, previous = self.build_metrics_pair()

        comparison = build_history_metric_comparison(current, previous)

        self.assertEqual(
            comparison["temperature"]["average_c"],
            {
                "current": 30.0,
                "previous": 25.0,
                "change": 5.0,
                "change_percent": None,
            },
        )
        self.assertEqual(
            comparison["temperature"]["max_c"]["change_percent"],
            None,
        )
        self.assertEqual(
            comparison["temperature"]["high_temperature_detected_days"][
                "change_percent"
            ],
            100.0,
        )

    def test_humidity_uses_percentage_points_and_relative_change(self):
        current, previous = self.build_metrics_pair()

        comparison = build_history_metric_comparison(current, previous)

        self.assertEqual(
            comparison["humidity"]["average_percent"],
            {
                "current": 60.0,
                "previous": 50.0,
                "change": 10.0,
                "change_percent": 20.0,
            },
        )

    def test_converts_load_bias_ratio_to_percent_values(self):
        current, previous = self.build_metrics_pair()

        comparison = build_history_metric_comparison(current, previous)

        self.assertEqual(
            comparison["load_bias"]["max_absolute_percent"],
            {
                "current": 30.0,
                "previous": 20.0,
                "change": 10.0,
                "change_percent": 50.0,
            },
        )

    def test_converts_deformation_ratios_to_percent_values(self):
        current, previous = self.build_metrics_pair()

        comparison = build_history_metric_comparison(current, previous)

        self.assertEqual(
            comparison["deformation"]["latest_percent"],
            {
                "current": 2.5,
                "previous": 1.0,
                "change": 1.5,
                "change_percent": 150.0,
            },
        )
        self.assertEqual(
            comparison["deformation"]["max_percent"],
            {
                "current": 4.0,
                "previous": 3.0,
                "change": 1.0,
                "change_percent": 33.33,
            },
        )

    def test_preserves_integer_types_for_count_values(self):
        current, previous = self.build_metrics_pair()

        comparison = build_history_metric_comparison(current, previous)

        count_paths = [
            ("load", "overload_detected_days"),
            ("temperature", "high_temperature_detected_days"),
            ("humidity", "high_humidity_detected_days"),
            ("moisture", "detected_days"),
            ("load_bias", "biased_days"),
            ("deformation", "deformation_detected_days"),
        ]
        for domain, metric_name in count_paths:
            with self.subTest(domain=domain, metric_name=metric_name):
                values = comparison[domain][metric_name]
                self.assertIsInstance(values["current"], int)
                self.assertIsInstance(values["previous"], int)
                self.assertIsInstance(values["change"], int)

    def test_calculates_with_raw_values_before_rounding_payload(self):
        current, previous = self.build_metrics_pair()
        current["load"]["average_kg"] = 1.005
        previous["load"]["average_kg"] = 1.004

        comparison = build_history_metric_comparison(current, previous)

        self.assertEqual(
            comparison["load"]["average_kg"],
            {
                "current": 1.01,
                "previous": 1.0,
                "change": 0.0,
                "change_percent": 0.1,
            },
        )

    def test_rejects_missing_required_metric_and_invalid_input_contract(self):
        current, previous = self.build_metrics_pair()
        del current["load"]["average_kg"]

        with self.assertRaisesMessage(
            ValueError,
            "current_metrics.load.average_kg",
        ):
            build_history_metric_comparison(current, previous)

        with self.assertRaisesMessage(ValueError, "current_metrics must be a mapping"):
            build_history_metric_comparison([], previous)

    def test_does_not_mutate_input_metrics(self):
        current, previous = self.build_metrics_pair()
        original_current = deepcopy(current)
        original_previous = deepcopy(previous)

        build_history_metric_comparison(current, previous)

        self.assertEqual(current, original_current)
        self.assertEqual(previous, original_previous)

    def test_comparison_payload_is_json_serializable(self):
        current, previous = self.build_metrics_pair()

        comparison = build_history_metric_comparison(current, previous)

        json.dumps(comparison, allow_nan=False)
        JSONRenderer().render(comparison)


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
    def test_creates_normal_history_report(self):
        session = self.create_session()
        self.add_uniform_readings(session)

        report, created = analyze_history_session(session)

        self.assertTrue(created)
        self.assertEqual(report.session, session)
        self.assertEqual(report.severity, Severity.NORMAL.value)
        self.assertEqual(report.active_rules, [])
        self.assertEqual(
            report.metrics["daily_series"],
            build_history_daily_series(session),
        )

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
            second_report.metrics["daily_series"][0]["load_kg"],
            6.0,
        )
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


class AnalysisReportComparisonFieldTests(HistoryAnalysisTestCase):
    def test_defaults_comparison_to_empty_dict(self):
        session = self.create_session()
        self.add_uniform_readings(session)

        report, _created = analyze_history_session(session)

        self.assertEqual(report.comparison, {})

    def test_persists_comparison_json_round_trip(self):
        session = self.create_session()
        self.add_uniform_readings(session)
        report, _created = analyze_history_session(session)
        comparison = {
            "available": True,
            "metrics": {
                "load": {
                    "average_kg": {
                        "current": 5.2,
                        "previous": 3.4,
                        "change": 1.8,
                        "change_percent": 52.9,
                    }
                }
            },
        }

        report.comparison = comparison
        report.save(update_fields=["comparison"])
        report.refresh_from_db()

        self.assertEqual(report.comparison, comparison)


class AnalysisReportSerializerTests(HistoryAnalysisTestCase):
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
                "period",
                "metrics",
                "chart_references",
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
        self.assertEqual(
            data["period"],
            {
                "started_at": "2026-07-28T09:00:00+09:00",
                "ended_at": "2026-08-04T09:00:00+09:00",
                "timezone": "Asia/Seoul",
            },
        )
        self.assertEqual(data["metrics"], report.metrics)
        self.assertEqual(
            data["chart_references"],
            {
                "max_load_kg": 5.5,
                "max_body_deformation_ratio": 0.03,
                "max_body_deformation_percent": 3.0,
                "avoid_moisture": True,
            },
        )
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

    def test_serializes_null_ended_at(self):
        session = self.create_session()
        session.ended_at = None
        session.save(update_fields=["ended_at"])
        self.add_readings(session)
        report, _created = analyze_history_session(session)

        data = AnalysisReportSerializer(report).data

        self.assertIsNone(data["period"]["ended_at"])

    def test_serializes_missing_chart_references_as_null(self):
        session = self.create_session()
        self.add_readings(session)
        report, _created = analyze_history_session(session)
        report.care_guideline_snapshot = {}

        data = AnalysisReportSerializer(report).data

        self.assertEqual(
            data["chart_references"],
            {
                "max_load_kg": None,
                "max_body_deformation_ratio": None,
                "max_body_deformation_percent": None,
                "avoid_moisture": None,
            },
        )

    def test_all_fields_are_read_only(self):
        serializer = AnalysisReportSerializer(
            data={
                "id": 999,
                "session_id": 999,
                "scenario_code": "OVERLOAD_HISTORY",
                "period": {"timezone": "UTC"},
                "metrics": {"tampered": True},
                "chart_references": {"max_load_kg": 999},
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


class AnalyzeHistorySessionApiTests(HistoryAnalysisTestCase):
    def setUp(self):
        self.client = APIClient()

    def get_url(self, session_id):
        return reverse(
            "analyze-history-session",
            kwargs={"session_id": session_id},
        )

    def test_analyzes_normal_history_without_request_body(self):
        session = self.create_session()
        self.add_uniform_readings(session)

        response = self.client.post(self.get_url(session.id))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["created"])
        self.assertEqual(response.data["session_id"], session.id)
        self.assertEqual(response.data["severity"], Severity.NORMAL.value)
        self.assertEqual(response.data["active_rules"], [])
        self.assertEqual(
            response.data["period"],
            {
                "started_at": "2026-07-28T09:00:00+09:00",
                "ended_at": "2026-08-04T09:00:00+09:00",
                "timezone": "Asia/Seoul",
            },
        )
        self.assertEqual(
            response.data["chart_references"],
            {
                "max_load_kg": 5.5,
                "max_body_deformation_ratio": 0.03,
                "max_body_deformation_percent": 3.0,
                "avoid_moisture": True,
            },
        )
        self.assertEqual(len(response.data["metrics"]["daily_series"]), 7)
        self.assertEqual(
            response.data["metrics"]["daily_series"][0],
            {
                "date": "2026-07-28",
                "load_kg": 4.0,
                "deformation_ratio": 0.01,
                "deformation_percent": 1.0,
                "moisture_detected": False,
            },
        )
        self.assertEqual(
            set(response.data),
            {
                "created",
                "id",
                "session_id",
                "scenario_code",
                "period",
                "metrics",
                "chart_references",
                "severity",
                "active_rules",
                "unavailable_rules",
                "care_guideline_snapshot",
                "created_at",
                "updated_at",
            },
        )
        self.assertTrue(
            AnalysisReport.objects.filter(session=session).exists()
        )
        report = AnalysisReport.objects.get(session=session)
        self.assertEqual(
            response.data["metrics"]["daily_series"],
            report.metrics["daily_series"],
        )

    def test_analyzes_risk_history_and_ignores_request_values(self):
        session = self.create_session()
        self.add_uniform_readings(session, strap_load="6.00")

        response = self.client.post(
            self.get_url(session.id),
            {
                "severity": Severity.NORMAL.value,
                "active_rules": [],
                "metrics": {"tampered": True},
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["severity"], Severity.WARNING.value)
        self.assertEqual(
            response.data["active_rules"], [RuleCode.HIGH_LOAD.value]
        )
        self.assertNotEqual(response.data["metrics"], {"tampered": True})

    def test_reanalysis_returns_same_report(self):
        session = self.create_session()
        self.add_uniform_readings(session)
        url = self.get_url(session.id)

        first_response = self.client.post(url, {}, format="json")
        first_reading = session.readings.order_by("sequence").first()
        first_reading.strap_load = Decimal("6.00")
        first_reading.save(update_fields=["strap_load"])
        updated_guideline = self.product_model.care_guideline.copy()
        updated_guideline["max_load_kg"] = 8.0
        self.product_model.care_guideline = updated_guideline
        self.product_model.save(update_fields=["care_guideline"])
        second_response = self.client.post(url, {}, format="json")

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertTrue(first_response.data["created"])
        self.assertFalse(second_response.data["created"])
        self.assertEqual(first_response.data["id"], second_response.data["id"])
        self.assertEqual(
            AnalysisReport.objects.filter(session=session).count(), 1
        )
        self.assertEqual(
            first_response.data["metrics"]["daily_series"][0]["load_kg"],
            4.0,
        )
        self.assertEqual(
            second_response.data["metrics"]["daily_series"][0]["load_kg"],
            6.0,
        )
        self.assertEqual(second_response.data["metrics"]["load"]["max_kg"], 6.0)
        self.assertEqual(first_response.data["chart_references"]["max_load_kg"], 5.5)
        self.assertEqual(second_response.data["chart_references"]["max_load_kg"], 8.0)
        self.assertEqual(
            second_response.data["care_guideline_snapshot"]["max_load_kg"],
            8.0,
        )

    def test_returns_404_for_missing_session(self):
        response = self.client.post(self.get_url(999999))

        self.assertEqual(response.status_code, 404)
        self.assertIn("detail", response.data)
        self.assertFalse(AnalysisReport.objects.exists())

    def test_returns_400_for_live_session(self):
        session = self.create_session(purpose=MeasurementSession.Purpose.LIVE)
        self.add_uniform_readings(session)

        response = self.client.post(self.get_url(session.id))

        self.assertEqual(response.status_code, 400)
        self.assertIn("detail", response.data)
        self.assertFalse(AnalysisReport.objects.filter(session=session).exists())

    def test_returns_400_for_running_history(self):
        session = self.create_session(status=MeasurementSession.Status.RUNNING)
        self.add_uniform_readings(session)

        response = self.client.post(self.get_url(session.id))

        self.assertEqual(response.status_code, 400)
        self.assertIn("detail", response.data)
        self.assertFalse(AnalysisReport.objects.filter(session=session).exists())

    def test_returns_400_for_history_without_readings(self):
        session = self.create_session()

        response = self.client.post(self.get_url(session.id), {}, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("detail", response.data)
        self.assertFalse(AnalysisReport.objects.filter(session=session).exists())


class AnalysisReportDetailApiTests(HistoryAnalysisTestCase):
    def setUp(self):
        self.client = APIClient()

    def get_url(self, report_id):
        return reverse(
            "analysis-report-detail",
            kwargs={"report_id": report_id},
        )

    def create_report(self, *, with_scenario=True):
        session = self.create_session()
        if with_scenario:
            session.scenario = self.create_scenario()
            session.save(update_fields=["scenario"])
        self.add_readings(session)
        report, _created = analyze_history_session(session)
        return session, report

    def test_retrieves_stored_report(self):
        session, report = self.create_report()

        response = self.client.get(self.get_url(report.id))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(response.data),
            {
                "id",
                "session_id",
                "scenario_code",
                "period",
                "metrics",
                "chart_references",
                "severity",
                "active_rules",
                "unavailable_rules",
                "care_guideline_snapshot",
                "created_at",
                "updated_at",
            },
        )
        self.assertNotIn("created", response.data)
        self.assertEqual(response.data["id"], report.id)
        self.assertEqual(response.data["session_id"], session.id)
        self.assertEqual(
            response.data["scenario_code"], session.scenario.code
        )
        self.assertEqual(
            response.data["period"],
            {
                "started_at": "2026-07-28T09:00:00+09:00",
                "ended_at": "2026-08-04T09:00:00+09:00",
                "timezone": "Asia/Seoul",
            },
        )
        self.assertEqual(response.data["metrics"], report.metrics)
        self.assertEqual(len(response.data["metrics"]["daily_series"]), 7)
        self.assertEqual(
            response.data["metrics"]["daily_series"],
            report.metrics["daily_series"],
        )
        self.assertEqual(response.data["severity"], report.severity)
        self.assertEqual(response.data["active_rules"], report.active_rules)
        self.assertEqual(
            response.data["unavailable_rules"], report.unavailable_rules
        )
        self.assertEqual(
            response.data["care_guideline_snapshot"],
            report.care_guideline_snapshot,
        )
        self.assertEqual(
            response.data["chart_references"],
            {
                "max_load_kg": 5.5,
                "max_body_deformation_ratio": 0.03,
                "max_body_deformation_percent": 3.0,
                "avoid_moisture": True,
            },
        )

    def test_get_matches_post_period_references_and_daily_series(self):
        session = self.create_session()
        session.scenario = self.create_scenario()
        session.save(update_fields=["scenario"])
        self.add_readings(session)
        post_response = self.client.post(
            reverse(
                "analyze-history-session",
                kwargs={"session_id": session.id},
            )
        )

        get_response = self.client.get(self.get_url(post_response.data["id"]))

        self.assertEqual(post_response.status_code, 200)
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(get_response.data["period"], post_response.data["period"])
        self.assertEqual(
            get_response.data["chart_references"],
            post_response.data["chart_references"],
        )
        self.assertEqual(
            get_response.data["metrics"]["daily_series"],
            post_response.data["metrics"]["daily_series"],
        )

    def test_returns_404_for_missing_report(self):
        response = self.client.get(self.get_url(999999))

        self.assertEqual(response.status_code, 404)
        self.assertIn("detail", response.data)

    def test_get_does_not_reanalyze_or_update_report(self):
        session, report = self.create_report()
        original_metrics = deepcopy(report.metrics)
        original_snapshot = deepcopy(report.care_guideline_snapshot)
        original_updated_at = report.updated_at
        original_count = AnalysisReport.objects.count()

        first_reading = session.readings.order_by("sequence").first()
        first_reading.strap_load = Decimal("99.00")
        first_reading.save(update_fields=["strap_load"])
        self.product_model.care_guideline = {"max_load_kg": 999}
        self.product_model.save(update_fields=["care_guideline"])

        response = self.client.get(self.get_url(report.id))
        report.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(AnalysisReport.objects.count(), original_count)
        self.assertEqual(report.updated_at, original_updated_at)
        self.assertEqual(report.metrics, original_metrics)
        self.assertEqual(
            report.metrics["daily_series"],
            original_metrics["daily_series"],
        )
        self.assertEqual(
            response.data["metrics"]["daily_series"],
            original_metrics["daily_series"],
        )
        self.assertEqual(
            response.data["metrics"]["daily_series"][0]["load_kg"],
            4.0,
        )
        self.assertEqual(report.care_guideline_snapshot, original_snapshot)
        self.assertEqual(
            response.data["chart_references"]["max_load_kg"],
            original_snapshot["max_load_kg"],
        )
        self.assertEqual(response.data["metrics"], original_metrics)
        self.assertEqual(
            response.data["care_guideline_snapshot"], original_snapshot
        )

    def test_retrieves_report_with_null_scenario(self):
        _session, report = self.create_report(with_scenario=False)

        response = self.client.get(self.get_url(report.id))

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["scenario_code"])
