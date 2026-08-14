import json
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from rest_framework.renderers import JSONRenderer
from rest_framework.test import APIClient

from measurements.models import MeasurementSession, SensorReading
from products.models import Bag, ProductModel
from simulation.models import SimulationScenario

import httpx2
from openai import (
    APIConnectionError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    RateLimitError,
)

from .ai import (
    HISTORY_AI_CONTENT_SCHEMA,
    HistoryAIGenerationError,
    HistoryAIGenerationReason,
    HistoryAIContentValidationError,
    HistoryAIResultValidationError,
    build_history_ai_context,
    build_history_ai_fallback,
    generate_history_ai_content,
    get_openai_client,
    get_openai_model,
    validate_history_ai_content,
    validate_history_ai_result,
)
from .ai.client import (
    DEFAULT_OPENAI_MODEL,
    DEFAULT_OPENAI_TIMEOUT_SECONDS,
    OPENAI_MAX_RETRIES,
    get_openai_timeout_seconds,
)
from .ai.errors import raise_history_ai_generation_error
from .ai.generation import (
    HISTORY_AI_MAX_OUTPUT_TOKENS,
    HISTORY_AI_SCHEMA_NAME,
)
from .ai.prompts import HISTORY_AI_DEVELOPER_INSTRUCTION
from .comparisons import (
    ComparisonUnavailableReason,
    build_history_metric_comparison,
    find_previous_history_session,
)
from .constants import RuleCode, Severity
from .metrics import build_history_daily_series, calculate_history_metrics
from .models import AnalysisReport
from .presentation import (
    build_current_history_display_period,
    build_previous_history_display_period,
    project_history_daily_series,
)
from .rules import evaluate_history_rules
from .serializers import AnalysisReportSerializer
from .services import analyze_history_session, analyze_history_session_with_ai


def _source_metrics(response_metrics):
    metrics = deepcopy(response_metrics)
    if isinstance(metrics, dict) and isinstance(metrics.get("daily_series"), list):
        for item in metrics["daily_series"]:
            if isinstance(item, dict):
                item.pop("display_date", None)
    return metrics


def _source_comparison(response_comparison):
    comparison = deepcopy(response_comparison)
    if isinstance(comparison, dict):
        comparison.pop("display_previous_period", None)
    return comparison


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
                measured_at=session.started_at + timedelta(days=sequence),
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

    def build_ai_content(self, marker="generated"):
        return {
            "weekly_summary": f"{marker} weekly summary",
            "care_comment": f"{marker} care comment",
            "pattern_insight": f"{marker} pattern insight",
            "priority_actions": [f"{marker} action"],
        }


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


class HistoryDisplayPresentationTests(TestCase):
    reference_date = date(2026, 8, 14)

    def test_builds_non_overlapping_current_and_previous_seven_day_periods(self):
        current = build_current_history_display_period(self.reference_date)
        previous = build_previous_history_display_period(self.reference_date)

        self.assertEqual(
            current,
            {
                "start_date": "2026-08-07",
                "end_date": "2026-08-13",
                "timezone": "Asia/Seoul",
            },
        )
        self.assertEqual(
            previous,
            {
                "start_date": "2026-07-31",
                "end_date": "2026-08-06",
                "timezone": "Asia/Seoul",
            },
        )

        current_start = date.fromisoformat(current["start_date"])
        current_end = date.fromisoformat(current["end_date"])
        previous_start = date.fromisoformat(previous["start_date"])
        previous_end = date.fromisoformat(previous["end_date"])
        self.assertEqual((current_end - current_start).days + 1, 7)
        self.assertEqual((previous_end - previous_start).days + 1, 7)
        self.assertLess(previous_end, current_start)
        self.assertEqual(current_end + timedelta(days=1), self.reference_date)
        self.assertEqual(previous_end + timedelta(days=1), current_start)

    def test_projects_seven_items_without_mutating_source(self):
        source = [{"date": f"source-{index}"} for index in range(7)]
        original = deepcopy(source)

        projected = project_history_daily_series(source, self.reference_date)

        self.assertEqual(source, original)
        self.assertEqual(
            [item["display_date"] for item in projected],
            [
                "2026-08-07",
                "2026-08-08",
                "2026-08-09",
                "2026-08-10",
                "2026-08-11",
                "2026-08-12",
                "2026-08-13",
            ],
        )
        self.assertEqual(
            [item["date"] for item in projected],
            [item["date"] for item in source],
        )

    def test_does_not_invent_dates_for_malformed_series(self):
        malformed_series = (
            [],
            [{"date": f"source-{index}"} for index in range(6)],
            [{"date": f"source-{index}"} for index in range(8)],
            [{"date": f"source-{index}"} for index in range(6)] + ["invalid"],
        )

        for source in malformed_series:
            with self.subTest(item_count=len(source)):
                original = deepcopy(source)
                projected = project_history_daily_series(
                    source,
                    self.reference_date,
                )

                self.assertEqual(source, original)
                for item in projected:
                    if isinstance(item, dict):
                        self.assertIsNone(item["display_date"])

    def test_preserves_non_list_daily_series(self):
        for source in (None, {}, "legacy"):
            with self.subTest(source=source):
                self.assertEqual(
                    project_history_daily_series(source, self.reference_date),
                    source,
                )


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
        first_report.ai_result = {
            "schema_version": 1,
            "status": "SUCCESS",
            "generated_at": "2026-08-12T20:30:00+09:00",
            "provider": "openai",
            "model": "old-model",
            "fallback_reason": None,
            "content": self.build_ai_content("old"),
        }
        first_report.save(update_fields=["ai_result"])

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
        self.assertEqual(second_report.ai_result, {})

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


class HistoryComparisonOrchestrationTests(HistoryAnalysisTestCase):
    def create_period(
        self,
        started_at,
        *,
        strap_load="4.00",
    ):
        session = MeasurementSession.objects.create(
            bag=self.bag,
            purpose=MeasurementSession.Purpose.HISTORY,
            seed=12345,
            started_at=started_at,
            ended_at=started_at + timedelta(days=7),
            status=MeasurementSession.Status.COMPLETED,
        )
        for sequence in range(7):
            SensorReading.objects.create(
                session=session,
                strap_load=Decimal(strap_load),
                humidity=Decimal("50.00"),
                moisture_detected=False,
                temperature=Decimal("25.00"),
                measured_at=started_at + timedelta(days=sequence),
                load_bias=Decimal("0.1000"),
                body_deformation_ratio=Decimal("0.0100"),
                sequence=sequence,
            )
        return session

    def refresh_for_analysis(self, session):
        return MeasurementSession.objects.select_related(
            "bag__product_model"
        ).get(pk=session.pk)

    def test_saves_available_comparison_without_previous_report(self):
        previous = self.create_period(self.started_at)
        current = self.create_period(self.started_at + timedelta(days=7))

        self.assertFalse(
            AnalysisReport.objects.filter(session=previous).exists()
        )

        report, created = analyze_history_session(current)

        self.assertTrue(created)
        self.assertEqual(
            report.comparison["previous_session_id"],
            previous.pk,
        )
        self.assertTrue(report.comparison["available"])
        self.assertIsNone(report.comparison["reason"])
        self.assertEqual(
            report.comparison["previous_period"],
            {
                "started_at": "2026-07-28T09:00:00+09:00",
                "ended_at": "2026-08-04T09:00:00+09:00",
                "timezone": "Asia/Seoul",
            },
        )
        comparison_metrics = report.comparison["metrics"]
        self.assertEqual(
            set(comparison_metrics),
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
            sum(len(metrics) for metrics in comparison_metrics.values()),
            15,
        )
        self.assertFalse(
            AnalysisReport.objects.filter(session=previous).exists()
        )

    def test_saves_no_previous_period_without_blocking_report(self):
        current = self.create_period(self.started_at)

        report, created = analyze_history_session(current)

        self.assertTrue(created)
        self.assertEqual(
            report.comparison,
            {
                "available": False,
                "reason": ComparisonUnavailableReason.NO_PREVIOUS_PERIOD.value,
                "previous_session_id": None,
                "previous_period": None,
                "metrics": None,
            },
        )

    def test_saves_invalid_period_shape_without_blocking_report(self):
        current = self.create_period(self.started_at)
        last_reading = current.readings.get(sequence=6)
        last_reading.measured_at = current.ended_at
        last_reading.save(update_fields=["measured_at"])

        report, created = analyze_history_session(current)

        self.assertTrue(created)
        self.assertFalse(report.comparison["available"])
        self.assertEqual(
            report.comparison["reason"],
            ComparisonUnavailableReason.INVALID_PERIOD_SHAPE.value,
        )
        self.assertIsNone(report.comparison["metrics"])

    def test_saves_ambiguous_previous_period_without_blocking_report(self):
        self.create_period(self.started_at)
        self.create_period(self.started_at)
        current = self.create_period(self.started_at + timedelta(days=7))

        report, created = analyze_history_session(current)

        self.assertTrue(created)
        self.assertFalse(report.comparison["available"])
        self.assertEqual(
            report.comparison["reason"],
            ComparisonUnavailableReason.AMBIGUOUS_PREVIOUS_PERIOD.value,
        )

    def test_reanalysis_replaces_available_with_unavailable_snapshot(self):
        previous = self.create_period(self.started_at)
        current = self.create_period(self.started_at + timedelta(days=7))
        first_report, first_created = analyze_history_session(current)

        previous.ended_at -= timedelta(hours=1)
        previous.save(update_fields=["ended_at"])
        second_report, second_created = analyze_history_session(current)

        self.assertTrue(first_created)
        self.assertTrue(first_report.comparison["available"])
        self.assertFalse(second_created)
        self.assertEqual(second_report.pk, first_report.pk)
        self.assertEqual(
            second_report.comparison,
            {
                "available": False,
                "reason": ComparisonUnavailableReason.NO_PREVIOUS_PERIOD.value,
                "previous_session_id": None,
                "previous_period": None,
                "metrics": None,
            },
        )

    def test_reanalysis_replaces_unavailable_with_available_snapshot(self):
        current = self.create_period(self.started_at + timedelta(days=7))
        first_report, first_created = analyze_history_session(current)

        previous = self.create_period(self.started_at)
        second_report, second_created = analyze_history_session(current)

        self.assertTrue(first_created)
        self.assertFalse(first_report.comparison["available"])
        self.assertFalse(second_created)
        self.assertEqual(second_report.pk, first_report.pk)
        self.assertTrue(second_report.comparison["available"])
        self.assertEqual(
            second_report.comparison["previous_session_id"],
            previous.pk,
        )

    def test_reanalysis_uses_current_guideline_for_both_periods(self):
        old_guideline = deepcopy(self.product_model.care_guideline)
        old_guideline["max_load_kg"] = 10.0
        ProductModel.objects.filter(pk=self.product_model.pk).update(
            care_guideline=old_guideline
        )
        previous = self.create_period(self.started_at, strap_load="6.00")
        current = self.create_period(
            self.started_at + timedelta(days=7),
            strap_load="6.00",
        )

        previous_report, _created = analyze_history_session(
            self.refresh_for_analysis(previous)
        )
        first_current_report, first_created = analyze_history_session(
            self.refresh_for_analysis(current)
        )
        previous_report_updated_at = previous_report.updated_at

        new_guideline = deepcopy(old_guideline)
        new_guideline["max_load_kg"] = 5.5
        ProductModel.objects.filter(pk=self.product_model.pk).update(
            care_guideline=new_guideline
        )
        second_current_report, second_created = analyze_history_session(
            self.refresh_for_analysis(current)
        )

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(second_current_report.pk, first_current_report.pk)
        self.assertEqual(
            second_current_report.care_guideline_snapshot["max_load_kg"],
            5.5,
        )
        self.assertEqual(
            second_current_report.metrics["load"]["overload_detected_days"],
            7,
        )
        overload_comparison = second_current_report.comparison["metrics"][
            "load"
        ]["overload_detected_days"]
        self.assertEqual(overload_comparison["current"], 7)
        self.assertEqual(overload_comparison["previous"], 7)

        previous_report.refresh_from_db()
        self.assertEqual(previous_report.updated_at, previous_report_updated_at)
        self.assertEqual(
            previous_report.care_guideline_snapshot["max_load_kg"],
            10.0,
        )
        self.assertEqual(
            previous_report.metrics["load"]["overload_detected_days"],
            0,
        )

    def test_stored_comparison_does_not_change_without_reanalysis(self):
        previous = self.create_period(self.started_at)
        current = self.create_period(self.started_at + timedelta(days=7))
        report, _created = analyze_history_session(current)
        stored_comparison = deepcopy(report.comparison)
        stored_updated_at = report.updated_at

        previous_reading = previous.readings.get(sequence=0)
        previous_reading.strap_load = Decimal("9.00")
        previous_reading.save(update_fields=["strap_load"])
        changed_guideline = deepcopy(self.product_model.care_guideline)
        changed_guideline["max_load_kg"] = 1.0
        ProductModel.objects.filter(pk=self.product_model.pk).update(
            care_guideline=changed_guideline
        )

        report.refresh_from_db()

        self.assertEqual(report.comparison, stored_comparison)
        self.assertEqual(report.updated_at, stored_updated_at)


class AnalysisReportComparisonFieldTests(HistoryAnalysisTestCase):
    def test_defaults_comparison_to_empty_dict(self):
        session = self.create_session()
        report = AnalysisReport.objects.create(
            session=session,
            metrics={},
            severity=Severity.NORMAL.value,
            care_guideline_snapshot={},
        )

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


class AnalysisReportAIResultFieldTests(HistoryAnalysisTestCase):
    def build_content(self):
        return {
            "weekly_summary": "주간 요약",
            "care_comment": "관리 설명",
            "pattern_insight": "패턴 설명",
            "priority_actions": ["첫 번째 행동", "두 번째 행동"],
        }

    def build_result(self, *, status):
        if status == "SUCCESS":
            metadata = {
                "provider": "openai",
                "model": "test-model",
                "fallback_reason": None,
            }
        else:
            metadata = {
                "provider": "deterministic",
                "model": None,
                "fallback_reason": "OPENAI_TIMEOUT",
            }
        return {
            "schema_version": 1,
            "status": status,
            "generated_at": "2026-08-12T20:30:00+09:00",
            **metadata,
            "content": self.build_content(),
        }

    def test_defaults_ai_result_to_empty_dict(self):
        session = self.create_session()
        report = AnalysisReport.objects.create(
            session=session,
            metrics={},
            severity=Severity.NORMAL.value,
            care_guideline_snapshot={},
        )

        self.assertEqual(report.ai_result, {})

    def test_analysis_service_uses_ai_result_model_default(self):
        session = self.create_session()
        self.add_uniform_readings(session)

        report, _created = analyze_history_session(session)

        self.assertEqual(report.ai_result, {})

    def test_persists_success_result_json_round_trip(self):
        report = self.create_report()
        expected_result = self.build_result(status="SUCCESS")

        report.ai_result = expected_result
        report.save(update_fields=["ai_result"])
        report.refresh_from_db()

        self.assertEqual(report.ai_result, expected_result)

    def test_persists_fallback_result_json_round_trip(self):
        report = self.create_report()
        expected_result = self.build_result(status="FALLBACK")

        report.ai_result = expected_result
        report.save(update_fields=["ai_result"])
        report.refresh_from_db()

        self.assertEqual(report.ai_result, expected_result)

    def test_preserves_nested_content_and_priority_actions(self):
        report = self.create_report()
        expected_result = self.build_result(status="SUCCESS")
        report.ai_result = expected_result
        report.save(update_fields=["ai_result"])

        report.refresh_from_db()

        self.assertEqual(report.ai_result["content"], expected_result["content"])
        self.assertEqual(
            report.ai_result["content"]["priority_actions"],
            ["첫 번째 행동", "두 번째 행동"],
        )

    def create_report(self):
        session = self.create_session()
        return AnalysisReport.objects.create(
            session=session,
            metrics={},
            severity=Severity.NORMAL.value,
            care_guideline_snapshot={},
            ai_result={},
        )


class AnalysisReportSerializerTests(HistoryAnalysisTestCase):
    @patch(
        "analysis.serializers.get_local_date",
        return_value=date(2026, 8, 14),
    )
    def test_serializes_analysis_report_fields_and_values(self, _localdate):
        scenario = self.create_scenario()
        session = self.create_session()
        session.scenario = scenario
        session.save(update_fields=["scenario"])
        self.add_readings(session)
        report, _created = analyze_history_session(session)
        original_metrics = deepcopy(report.metrics)
        original_comparison = deepcopy(report.comparison)

        data = AnalysisReportSerializer(report).data

        self.assertEqual(
            set(data),
            {
                "id",
                "session_id",
                "scenario_code",
                "period",
                "display_period",
                "metrics",
                "chart_references",
                "comparison",
                "ai_result",
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
        self.assertEqual(
            data["display_period"],
            {
                "start_date": "2026-08-07",
                "end_date": "2026-08-13",
                "timezone": "Asia/Seoul",
            },
        )
        self.assertEqual(_source_metrics(data["metrics"]), report.metrics)
        self.assertEqual(
            [item["display_date"] for item in data["metrics"]["daily_series"]],
            [
                "2026-08-07",
                "2026-08-08",
                "2026-08-09",
                "2026-08-10",
                "2026-08-11",
                "2026-08-12",
                "2026-08-13",
            ],
        )
        self.assertEqual(
            _source_comparison(data["comparison"]),
            report.comparison,
        )
        self.assertEqual(
            data["comparison"]["display_previous_period"],
            {
                "start_date": "2026-07-31",
                "end_date": "2026-08-06",
                "timezone": "Asia/Seoul",
            },
        )
        self.assertEqual(report.metrics, original_metrics)
        self.assertEqual(report.comparison, original_comparison)
        self.assertEqual(data["ai_result"], report.ai_result)
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
        _localdate.assert_called_once_with()

    @patch(
        "analysis.serializers.get_local_date",
        return_value=date(2026, 8, 14),
    )
    def test_handles_legacy_and_malformed_snapshots_without_inventing_dates(
        self,
        _localdate,
    ):
        report = AnalysisReport.objects.create(
            session=self.create_session(),
            metrics={},
            comparison={
                "available": False,
                "previous_period": None,
            },
            severity=Severity.NORMAL.value,
            care_guideline_snapshot={},
        )

        data = AnalysisReportSerializer(report).data

        self.assertEqual(data["metrics"], {})
        self.assertEqual(
            data["comparison"]["display_previous_period"],
            {
                "start_date": "2026-07-31",
                "end_date": "2026-08-06",
                "timezone": "Asia/Seoul",
            },
        )
        self.assertFalse(data["comparison"]["available"])

        for daily_series in (
            [{"date": f"source-{index}"} for index in range(6)],
            [{"date": f"source-{index}"} for index in range(8)],
        ):
            with self.subTest(item_count=len(daily_series)):
                report.metrics = {"daily_series": daily_series}
                original_metrics = deepcopy(report.metrics)
                projected = AnalysisReportSerializer(report).data["metrics"]

                self.assertEqual(report.metrics, original_metrics)
                self.assertEqual(
                    [item["date"] for item in projected["daily_series"]],
                    [item["date"] for item in daily_series],
                )
                self.assertTrue(
                    all(
                        item["display_date"] is None
                        for item in projected["daily_series"]
                    )
                )

        report.metrics = {"daily_series": "legacy"}
        report.comparison = "legacy"
        data = AnalysisReportSerializer(report).data
        self.assertEqual(data["metrics"], {"daily_series": "legacy"})
        self.assertEqual(data["comparison"], "legacy")

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
        report = AnalysisReport.objects.create(
            session=session,
            metrics={},
            severity=Severity.NORMAL.value,
            care_guideline_snapshot={},
        )

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
                "comparison": {"tampered": True},
                "ai_result": {"tampered": True},
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
        self.assertTrue(serializer.fields["comparison"].read_only)
        self.assertTrue(serializer.fields["ai_result"].read_only)


class AnalyzeHistorySessionApiTests(HistoryAnalysisTestCase):
    def setUp(self):
        self.client = APIClient()
        generation_patch = patch(
            "analysis.services.generate_history_ai_content",
            return_value=self.build_ai_content(),
        )
        self.mock_generation = generation_patch.start()
        self.addCleanup(generation_patch.stop)

    def get_url(self, session_id):
        return reverse(
            "analyze-history-session",
            kwargs={"session_id": session_id},
        )

    @patch(
        "analysis.serializers.get_local_date",
        return_value=date(2026, 8, 14),
    )
    def test_analyzes_normal_history_without_request_body(self, _localdate):
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
            response.data["display_period"],
            {
                "start_date": "2026-08-07",
                "end_date": "2026-08-13",
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
                "display_date": "2026-08-07",
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
                "display_period",
                "metrics",
                "chart_references",
                "comparison",
                "ai_result",
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
            _source_comparison(response.data["comparison"]),
            {
                "available": False,
                "reason": ComparisonUnavailableReason.NO_PREVIOUS_PERIOD.value,
                "previous_session_id": None,
                "previous_period": None,
                "metrics": None,
            },
        )
        self.assertEqual(
            _source_comparison(response.data["comparison"]),
            report.comparison,
        )
        self.assertEqual(
            response.data["comparison"]["display_previous_period"],
            {
                "start_date": "2026-07-31",
                "end_date": "2026-08-06",
                "timezone": "Asia/Seoul",
            },
        )
        self.assertEqual(response.data["ai_result"], report.ai_result)
        self.assertEqual(response.data["ai_result"]["status"], "SUCCESS")
        self.assertEqual(response.data["ai_result"]["provider"], "openai")
        self.assertEqual(
            response.data["ai_result"]["content"],
            self.build_ai_content(),
        )
        self.assertEqual(
            _source_metrics(response.data["metrics"])["daily_series"],
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

    def test_returns_fallback_ai_result_for_generation_error(self):
        session = self.create_session()
        self.add_uniform_readings(session, strap_load="6.00")
        self.mock_generation.side_effect = HistoryAIGenerationError(
            "OPENAI_TIMEOUT"
        )

        response = self.client.post(self.get_url(session.id))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["created"])
        self.assertEqual(response.data["ai_result"]["status"], "FALLBACK")
        self.assertEqual(
            response.data["ai_result"]["provider"],
            "deterministic",
        )
        self.assertIsNone(response.data["ai_result"]["model"])
        self.assertEqual(
            response.data["ai_result"]["fallback_reason"],
            "OPENAI_TIMEOUT",
        )
        self.assertEqual(
            validate_history_ai_result(response.data["ai_result"]),
            response.data["ai_result"],
        )

    @override_settings(OPENAI_API_KEY="")
    def test_missing_api_key_returns_fallback_report_without_network(self):
        session = self.create_session()
        self.add_uniform_readings(session)
        self.mock_generation.side_effect = HistoryAIGenerationError(
            "OPENAI_NOT_CONFIGURED"
        )

        response = self.client.post(self.get_url(session.id))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["ai_result"]["status"], "FALLBACK")
        self.assertEqual(
            response.data["ai_result"]["fallback_reason"],
            "OPENAI_NOT_CONFIGURED",
        )

    def test_returns_available_comparison_for_exact_previous_period(self):
        previous = self.create_session()
        self.add_uniform_readings(previous)
        current = self.create_session()
        current.started_at += timedelta(days=7)
        current.ended_at += timedelta(days=7)
        current.save(update_fields=["started_at", "ended_at"])
        self.add_uniform_readings(current)

        response = self.client.post(self.get_url(current.pk))

        self.assertEqual(response.status_code, 200)
        comparison = response.data["comparison"]
        self.assertTrue(comparison["available"])
        self.assertIsNone(comparison["reason"])
        self.assertEqual(comparison["previous_session_id"], previous.pk)
        self.assertEqual(
            comparison["previous_period"],
            {
                "started_at": "2026-07-28T09:00:00+09:00",
                "ended_at": "2026-08-04T09:00:00+09:00",
                "timezone": "Asia/Seoul",
            },
        )
        self.assertEqual(
            set(comparison["metrics"]),
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
            sum(
                len(domain_metrics)
                for domain_metrics in comparison["metrics"].values()
            ),
            15,
        )

    def test_reanalysis_returns_latest_comparison_snapshot(self):
        previous = self.create_session()
        self.add_uniform_readings(previous)
        current = self.create_session()
        current.started_at += timedelta(days=7)
        current.ended_at += timedelta(days=7)
        current.save(update_fields=["started_at", "ended_at"])
        self.add_uniform_readings(current)
        url = self.get_url(current.pk)

        first_response = self.client.post(url)
        previous.ended_at -= timedelta(hours=1)
        previous.save(update_fields=["ended_at"])
        second_response = self.client.post(url)

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertTrue(first_response.data["created"])
        self.assertFalse(second_response.data["created"])
        self.assertEqual(first_response.data["id"], second_response.data["id"])
        self.assertTrue(first_response.data["comparison"]["available"])
        self.assertEqual(
            _source_comparison(second_response.data["comparison"]),
            {
                "available": False,
                "reason": ComparisonUnavailableReason.NO_PREVIOUS_PERIOD.value,
                "previous_session_id": None,
                "previous_period": None,
                "metrics": None,
            },
        )

    def test_reanalysis_returns_same_report(self):
        session = self.create_session()
        self.add_uniform_readings(session)
        url = self.get_url(session.id)
        content_a = self.build_ai_content("A")
        content_b = self.build_ai_content("B")
        self.mock_generation.side_effect = [content_a, content_b]

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
        self.assertEqual(first_response.data["ai_result"]["content"], content_a)
        self.assertEqual(second_response.data["ai_result"]["content"], content_b)
        self.assertEqual(self.mock_generation.call_count, 2)

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
        generation_patch = patch(
            "analysis.services.generate_history_ai_content",
            return_value=self.build_ai_content(),
        )
        self.mock_generation = generation_patch.start()
        self.addCleanup(generation_patch.stop)

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

    @patch(
        "analysis.serializers.get_local_date",
        return_value=date(2026, 8, 14),
    )
    def test_retrieves_stored_report(self, _localdate):
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
                "display_period",
                "metrics",
                "chart_references",
                "comparison",
                "ai_result",
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
        self.assertEqual(
            response.data["display_period"],
            {
                "start_date": "2026-08-07",
                "end_date": "2026-08-13",
                "timezone": "Asia/Seoul",
            },
        )
        self.assertEqual(_source_metrics(response.data["metrics"]), report.metrics)
        self.assertEqual(
            _source_comparison(response.data["comparison"]),
            report.comparison,
        )
        self.assertEqual(response.data["ai_result"], report.ai_result)
        self.mock_generation.assert_not_called()
        self.assertEqual(len(response.data["metrics"]["daily_series"]), 7)
        self.assertEqual(
            _source_metrics(response.data["metrics"])["daily_series"],
            report.metrics["daily_series"],
        )
        self.assertEqual(
            [
                item["display_date"]
                for item in response.data["metrics"]["daily_series"]
            ],
            [
                "2026-08-07",
                "2026-08-08",
                "2026-08-09",
                "2026-08-10",
                "2026-08-11",
                "2026-08-12",
                "2026-08-13",
            ],
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

    @patch(
        "analysis.serializers.get_local_date",
        return_value=date(2026, 8, 14),
    )
    def test_get_matches_post_period_references_and_daily_series(
        self,
        _localdate,
    ):
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
            get_response.data["display_period"],
            post_response.data["display_period"],
        )
        self.assertEqual(
            get_response.data["chart_references"],
            post_response.data["chart_references"],
        )
        self.assertEqual(
            get_response.data["metrics"]["daily_series"],
            post_response.data["metrics"]["daily_series"],
        )
        self.assertEqual(
            get_response.data["comparison"],
            post_response.data["comparison"],
        )

    def test_returns_404_for_missing_report(self):
        response = self.client.get(self.get_url(999999))

        self.assertEqual(response.status_code, 404)
        self.assertIn("detail", response.data)

    def test_get_does_not_reanalyze_or_update_report(self):
        previous = self.create_session()
        self.add_uniform_readings(previous)
        session = self.create_session()
        session.started_at += timedelta(days=7)
        session.ended_at += timedelta(days=7)
        session.save(update_fields=["started_at", "ended_at"])
        self.add_uniform_readings(session)
        report, _created = analyze_history_session(session)
        original_metrics = deepcopy(report.metrics)
        original_comparison = deepcopy(report.comparison)
        original_snapshot = deepcopy(report.care_guideline_snapshot)
        report.ai_result = {
            "schema_version": 1,
            "status": "SUCCESS",
            "generated_at": "2026-08-12T20:30:00+09:00",
            "provider": "openai",
            "model": "stored-model",
            "fallback_reason": None,
            "content": self.build_ai_content("stored"),
        }
        report.save(update_fields=["ai_result", "updated_at"])
        original_ai_result = deepcopy(report.ai_result)
        original_updated_at = report.updated_at
        original_count = AnalysisReport.objects.count()

        self.assertTrue(original_comparison["available"])

        first_reading = session.readings.order_by("sequence").first()
        first_reading.strap_load = Decimal("99.00")
        first_reading.save(update_fields=["strap_load"])
        self.product_model.care_guideline = {"max_load_kg": 999}
        self.product_model.save(update_fields=["care_guideline"])

        response = self.client.get(self.get_url(report.id))
        second_response = self.client.get(self.get_url(report.id))
        report.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(AnalysisReport.objects.count(), original_count)
        self.assertEqual(report.updated_at, original_updated_at)
        self.assertEqual(report.metrics, original_metrics)
        self.assertEqual(report.comparison, original_comparison)
        self.assertEqual(
            _source_comparison(response.data["comparison"]),
            original_comparison,
        )
        self.assertEqual(
            report.metrics["daily_series"],
            original_metrics["daily_series"],
        )
        self.assertEqual(
            _source_metrics(response.data["metrics"])["daily_series"],
            original_metrics["daily_series"],
        )
        self.assertEqual(
            response.data["metrics"]["daily_series"][0]["load_kg"],
            4.0,
        )
        self.assertEqual(report.care_guideline_snapshot, original_snapshot)
        self.assertEqual(report.ai_result, original_ai_result)
        self.assertEqual(response.data["ai_result"], original_ai_result)
        self.assertEqual(second_response.data["ai_result"], original_ai_result)
        self.mock_generation.assert_not_called()
        self.assertEqual(
            response.data["chart_references"]["max_load_kg"],
            original_snapshot["max_load_kg"],
        )
        self.assertEqual(_source_metrics(response.data["metrics"]), original_metrics)
        self.assertEqual(
            response.data["display_period"],
            second_response.data["display_period"],
        )
        self.assertEqual(
            response.data["care_guideline_snapshot"], original_snapshot
        )

    @patch(
        "analysis.serializers.get_local_date",
        return_value=date(2026, 8, 14),
    )
    def test_retrieves_legacy_empty_comparison_without_normalization(
        self,
        _localdate,
    ):
        session = self.create_session()
        report = AnalysisReport.objects.create(
            session=session,
            metrics={},
            severity=Severity.NORMAL.value,
            care_guideline_snapshot={},
        )

        response = self.client.get(self.get_url(report.pk))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data["comparison"],
            {
                "display_previous_period": {
                    "start_date": "2026-07-31",
                    "end_date": "2026-08-06",
                    "timezone": "Asia/Seoul",
                }
            },
        )

    def test_retrieves_report_with_null_scenario(self):
        _session, report = self.create_report(with_scenario=False)

        response = self.client.get(self.get_url(report.id))

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["scenario_code"])


class HistoryAITestCase(HistoryAnalysisTestCase):
    def create_period(
        self,
        started_at,
        *,
        strap_load="4.00",
        moisture_detected=False,
        load_bias="0.1000",
        body_deformation_ratio="0.0100",
    ):
        session = MeasurementSession.objects.create(
            bag=self.bag,
            purpose=MeasurementSession.Purpose.HISTORY,
            seed=12345,
            started_at=started_at,
            ended_at=started_at + timedelta(days=7),
            status=MeasurementSession.Status.COMPLETED,
        )
        self.add_uniform_readings(
            session,
            strap_load=strap_load,
            moisture_detected=moisture_detected,
            load_bias=load_bias,
            body_deformation_ratio=body_deformation_ratio,
        )
        return session

    def create_normal_report(self):
        session = self.create_session()
        self.add_uniform_readings(session)
        report, _created = analyze_history_session(session)
        return report

    def create_warning_report(self):
        session = self.create_session()
        self.add_uniform_readings(session, strap_load="6.00")
        report, _created = analyze_history_session(session)
        return report

    def create_available_comparison_report(self):
        self.create_period(self.started_at, strap_load="4.00")
        current = self.create_period(
            self.started_at + timedelta(days=7),
            strap_load="6.00",
        )
        report, _created = analyze_history_session(current)
        return report

    def set_comparison_metrics(self, report, metrics):
        report.comparison = {
            "available": True,
            "reason": None,
            "previous_session_id": 999,
            "previous_period": {
                "started_at": "2026-07-28T09:00:00+09:00",
                "ended_at": "2026-08-04T09:00:00+09:00",
                "timezone": "Asia/Seoul",
            },
            "metrics": metrics,
        }


class HistoryAIOrchestrationServiceTests(HistoryAITestCase):
    def create_session_with_readings(self, *, strap_load="4.00"):
        session = self.create_session()
        self.add_uniform_readings(session, strap_load=strap_load)
        return session

    @patch("analysis.services.get_openai_model", return_value="resolved-model")
    @patch("analysis.services.generate_history_ai_content")
    def test_saves_valid_success_snapshot(self, generation, model_resolver):
        session = self.create_session_with_readings()
        generated_content = self.build_ai_content("success")
        generation.return_value = generated_content

        report, created = analyze_history_session_with_ai(session)

        self.assertTrue(created)
        model_resolver.assert_called_once_with()
        generation.assert_called_once()
        self.assertEqual(generation.call_args.kwargs, {"model": "resolved-model"})
        self.assertEqual(
            generation.call_args.args[0],
            build_history_ai_context(report),
        )
        self.assertEqual(report.ai_result["schema_version"], 1)
        self.assertEqual(report.ai_result["status"], "SUCCESS")
        self.assertEqual(report.ai_result["provider"], "openai")
        self.assertEqual(report.ai_result["model"], "resolved-model")
        self.assertIsNone(report.ai_result["fallback_reason"])
        self.assertEqual(report.ai_result["content"], generated_content)
        generated_at = datetime.fromisoformat(report.ai_result["generated_at"])
        self.assertIsNotNone(generated_at.utcoffset())
        self.assertEqual(
            validate_history_ai_result(report.ai_result),
            report.ai_result,
        )
        report.refresh_from_db()
        self.assertEqual(report.ai_result["status"], "SUCCESS")

    @patch("analysis.services.get_openai_model", return_value="resolved-model")
    @patch("analysis.services.build_history_ai_fallback")
    @patch("analysis.services.generate_history_ai_content")
    def test_saves_valid_fallback_snapshot(
        self,
        generation,
        fallback_builder,
        _model_resolver,
    ):
        session = self.create_session_with_readings(strap_load="6.00")
        fallback_content = self.build_ai_content("fallback")
        fallback_builder.return_value = fallback_content
        generation.side_effect = HistoryAIGenerationError("OPENAI_TIMEOUT")

        report, created = analyze_history_session_with_ai(session)

        self.assertTrue(created)
        fallback_builder.assert_called_once_with(report)
        self.assertEqual(report.ai_result["schema_version"], 1)
        self.assertEqual(report.ai_result["status"], "FALLBACK")
        self.assertEqual(report.ai_result["provider"], "deterministic")
        self.assertIsNone(report.ai_result["model"])
        self.assertEqual(report.ai_result["fallback_reason"], "OPENAI_TIMEOUT")
        self.assertEqual(report.ai_result["content"], fallback_content)
        self.assertEqual(
            validate_history_ai_result(report.ai_result),
            report.ai_result,
        )

    @patch("analysis.services.build_history_ai_fallback")
    @patch("analysis.services.generate_history_ai_content")
    def test_passes_through_representative_fallback_reasons(
        self,
        generation,
        fallback_builder,
    ):
        fallback_builder.return_value = self.build_ai_content("fallback")

        for reason in ("OPENAI_NOT_CONFIGURED", "INVALID_AI_RESPONSE"):
            with self.subTest(reason=reason):
                session = self.create_session_with_readings()
                generation.side_effect = HistoryAIGenerationError(reason)

                report, _created = analyze_history_session_with_ai(session)

                self.assertEqual(report.ai_result["fallback_reason"], reason)

    @override_settings(OPENAI_API_KEY="")
    def test_missing_api_key_uses_real_not_configured_fallback_boundary(self):
        session = self.create_session_with_readings()

        report, created = analyze_history_session_with_ai(session)

        self.assertTrue(created)
        self.assertEqual(report.ai_result["status"], "FALLBACK")
        self.assertEqual(
            report.ai_result["fallback_reason"],
            "OPENAI_NOT_CONFIGURED",
        )

    @patch("analysis.services.build_history_ai_fallback")
    @patch("analysis.services.generate_history_ai_content")
    def test_programming_error_propagates_and_leaves_empty_ai_result(
        self,
        generation,
        fallback_builder,
    ):
        session = self.create_session_with_readings(strap_load="6.00")
        generation.side_effect = TypeError("programming error")

        with self.assertRaisesMessage(TypeError, "programming error"):
            analyze_history_session_with_ai(session)

        fallback_builder.assert_not_called()
        report = AnalysisReport.objects.get(session=session)
        self.assertEqual(report.metrics["load"]["max_kg"], 6.0)
        self.assertEqual(report.ai_result, {})

    @patch("analysis.services.validate_history_ai_result")
    @patch("analysis.services.generate_history_ai_content")
    def test_result_validation_error_propagates_and_leaves_empty_ai_result(
        self,
        generation,
        result_validator,
    ):
        session = self.create_session_with_readings()
        generation.return_value = self.build_ai_content("success")
        result_validator.side_effect = HistoryAIResultValidationError(
            "orchestration contract error"
        )

        with self.assertRaises(HistoryAIResultValidationError):
            analyze_history_session_with_ai(session)

        report = AnalysisReport.objects.get(session=session)
        self.assertEqual(report.ai_result, {})

    @patch("analysis.services.generate_history_ai_content")
    def test_regeneration_replaces_old_ai_snapshot_and_preserves_created(
        self,
        generation,
    ):
        session = self.create_session_with_readings()
        content_a = self.build_ai_content("A")
        content_b = self.build_ai_content("B")
        generation.side_effect = [content_a, content_b]

        first_report, first_created = analyze_history_session_with_ai(session)
        first_snapshot = deepcopy(first_report.ai_result)
        second_report, second_created = analyze_history_session_with_ai(session)

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first_report.pk, second_report.pk)
        self.assertEqual(generation.call_count, 2)
        self.assertEqual(first_snapshot["content"], content_a)
        self.assertEqual(second_report.ai_result["content"], content_b)
        self.assertNotEqual(second_report.ai_result["content"], content_a)


class HistoryAITransactionBoundaryTests(TransactionTestCase):
    def setUp(self):
        owner = get_user_model().objects.create_user(username="transaction-owner")
        product_model = ProductModel.objects.create(
            brand="Test Brand",
            model_name="Transaction Bag",
            material="Leather",
            care_guideline={
                "avoid_moisture": True,
                "max_load_kg": 5.5,
                "recommended_temp_range_c": [0, 35],
                "max_humidity_percent": 70,
                "max_abs_load_bias": 0.30,
                "max_body_deformation_ratio": 0.03,
                "care_actions": {},
            },
        )
        bag = Bag.objects.create(
            product_model=product_model,
            owner=owner,
            nfc_uid="TRANSACTION-NFC",
        )
        started_at = datetime(2026, 7, 28, 9, tzinfo=ZoneInfo("Asia/Seoul"))
        self.session = MeasurementSession.objects.create(
            bag=bag,
            purpose=MeasurementSession.Purpose.HISTORY,
            seed=12345,
            started_at=started_at,
            ended_at=started_at + timedelta(days=7),
            status=MeasurementSession.Status.COMPLETED,
        )
        for sequence in range(7):
            SensorReading.objects.create(
                session=self.session,
                strap_load=Decimal("4.00"),
                humidity=Decimal("50.00"),
                moisture_detected=False,
                temperature=Decimal("25.00"),
                measured_at=started_at + timedelta(days=sequence),
                load_bias=Decimal("0.1000"),
                body_deformation_ratio=Decimal("0.0100"),
                sequence=sequence,
            )

    def test_generation_runs_after_deterministic_commit_outside_atomic_block(self):
        observed = {}

        def generation(context, *, model):
            stored_report = AnalysisReport.objects.get(session=self.session)
            observed["in_atomic_block"] = connection.in_atomic_block
            observed["stored_ai_result"] = stored_report.ai_result
            observed["stored_metrics"] = stored_report.metrics
            return {
                "weekly_summary": "weekly summary",
                "care_comment": "care comment",
                "pattern_insight": "pattern insight",
                "priority_actions": [],
            }

        with patch(
            "analysis.services.generate_history_ai_content",
            side_effect=generation,
        ):
            report, created = analyze_history_session_with_ai(self.session)

        self.assertTrue(created)
        self.assertFalse(observed["in_atomic_block"])
        self.assertEqual(observed["stored_ai_result"], {})
        self.assertEqual(observed["stored_metrics"], report.metrics)
        self.assertEqual(report.ai_result["status"], "SUCCESS")


class HistoryAIContextTests(HistoryAITestCase):
    def test_builds_normal_history_report_context(self):
        report = self.create_normal_report()

        context = build_history_ai_context(report)

        self.assertEqual(
            set(context),
            {
                "period",
                "metrics",
                "severity",
                "active_rules",
                "unavailable_rules",
                "care_guideline_snapshot",
                "comparison",
            },
        )
        self.assertEqual(context["severity"], Severity.NORMAL.value)
        self.assertEqual(context["active_rules"], [])
        self.assertEqual(context["period"]["timezone"], "Asia/Seoul")
        self.assertEqual(
            context["period"]["started_at"],
            "2026-07-28T09:00:00+09:00",
        )

    def test_builds_warning_history_report_context(self):
        report = self.create_warning_report()

        context = build_history_ai_context(report)

        self.assertEqual(context["severity"], Severity.WARNING.value)
        self.assertEqual(context["active_rules"], [RuleCode.HIGH_LOAD.value])
        self.assertEqual(context["metrics"]["load"]["overload_detected_days"], 7)

    def test_preserves_available_comparison_without_session_id(self):
        report = self.create_available_comparison_report()

        context = build_history_ai_context(report)

        self.assertTrue(context["comparison"]["available"])
        self.assertIsNone(context["comparison"]["reason"])
        self.assertIn("load", context["comparison"]["metrics"])
        self.assertNotIn("previous_session_id", context["comparison"])

    def test_preserves_unavailable_comparison(self):
        report = self.create_normal_report()

        context = build_history_ai_context(report)

        self.assertEqual(
            context["comparison"],
            {
                "available": False,
                "reason": ComparisonUnavailableReason.NO_PREVIOUS_PERIOD.value,
                "previous_period": None,
                "metrics": None,
            },
        )

    def test_excludes_daily_series_from_metrics(self):
        report = self.create_normal_report()

        context = build_history_ai_context(report)

        self.assertEqual(
            set(context["metrics"]),
            {
                "load",
                "temperature",
                "humidity",
                "moisture",
                "load_bias",
                "deformation",
            },
        )
        self.assertNotIn("daily_series", context["metrics"])
        self.assertNotIn("reading_count", context["metrics"])

    def test_excludes_scenario_data(self):
        scenario = self.create_scenario(code="OVERLOAD_HISTORY")
        session = self.create_session()
        session.scenario = scenario
        session.save(update_fields=["scenario"])
        self.add_uniform_readings(session, strap_load="6.00")
        report, _created = analyze_history_session(session)

        context = build_history_ai_context(report)
        serialized = json.dumps(context)

        self.assertNotIn("scenario", serialized.lower())
        self.assertNotIn("OVERLOAD_HISTORY", serialized)

    def test_excludes_public_identifiers_and_database_ids(self):
        report = self.create_normal_report()

        context = build_history_ai_context(report)
        serialized = json.dumps(context)

        self.assertNotIn(str(report.pk), set(context))
        self.assertNotIn("public_token", serialized)
        self.assertNotIn("nfc", serialized.lower())
        self.assertNotIn("owner", serialized.lower())
        self.assertNotIn("session_id", serialized)
        self.assertNotIn("report_id", serialized)
        self.assertNotIn("created_at", serialized)
        self.assertNotIn("updated_at", serialized)

    def test_uses_stored_care_guideline_snapshot(self):
        report = self.create_normal_report()
        stored_snapshot = deepcopy(report.care_guideline_snapshot)
        changed_guideline = deepcopy(stored_snapshot)
        changed_guideline["max_load_kg"] = 999
        self.product_model.care_guideline = changed_guideline
        self.product_model.save(update_fields=["care_guideline"])

        context = build_history_ai_context(report)

        self.assertEqual(
            context["care_guideline_snapshot"]["max_load_kg"],
            stored_snapshot["max_load_kg"],
        )
        self.assertNotEqual(
            context["care_guideline_snapshot"]["max_load_kg"],
            changed_guideline["max_load_kg"],
        )

    def test_context_is_json_serializable(self):
        report = self.create_available_comparison_report()
        report.metrics["load"]["average_kg"] = Decimal("4.25")

        context = build_history_ai_context(report)

        json.dumps(context, allow_nan=False)
        JSONRenderer().render(context)

    def test_context_does_not_mutate_report_snapshots(self):
        report = self.create_available_comparison_report()
        original_metrics = deepcopy(report.metrics)
        original_comparison = deepcopy(report.comparison)
        original_guideline = deepcopy(report.care_guideline_snapshot)
        original_active_rules = deepcopy(report.active_rules)
        original_unavailable_rules = deepcopy(report.unavailable_rules)

        context = build_history_ai_context(report)
        context["metrics"]["load"]["average_kg"] = 999
        context["comparison"]["metrics"]["load"]["average_kg"]["current"] = 999
        context["care_guideline_snapshot"]["max_load_kg"] = 999
        context["active_rules"].append("TAMPERED")

        self.assertEqual(report.metrics, original_metrics)
        self.assertEqual(report.comparison, original_comparison)
        self.assertEqual(report.care_guideline_snapshot, original_guideline)
        self.assertEqual(report.active_rules, original_active_rules)
        self.assertEqual(report.unavailable_rules, original_unavailable_rules)


class HistoryAIContentContractTests(TestCase):
    def valid_payload(self):
        return {
            "weekly_summary": "주간 요약",
            "care_comment": "관리 설명",
            "pattern_insight": "패턴 설명",
            "priority_actions": ["첫 번째 행동", "두 번째 행동"],
        }

    def test_validates_and_normalizes_valid_payload(self):
        payload = self.valid_payload()
        payload["weekly_summary"] = "  주간 요약  "

        result = validate_history_ai_content(payload)

        self.assertEqual(result["weekly_summary"], "주간 요약")
        self.assertIsNot(result, payload)
        self.assertIsNot(result["priority_actions"], payload["priority_actions"])
        self.assertEqual(HISTORY_AI_CONTENT_SCHEMA["required"], list(result))
        self.assertFalse(HISTORY_AI_CONTENT_SCHEMA["additionalProperties"])
        self.assertEqual(
            HISTORY_AI_CONTENT_SCHEMA["properties"]["priority_actions"]["maxItems"],
            2,
        )

    def test_rejects_missing_or_empty_weekly_summary(self):
        missing = self.valid_payload()
        missing.pop("weekly_summary")
        empty = self.valid_payload()
        empty["weekly_summary"] = "   "

        with self.assertRaises(HistoryAIContentValidationError):
            validate_history_ai_content(missing)
        with self.assertRaises(HistoryAIContentValidationError):
            validate_history_ai_content(empty)

    def test_rejects_invalid_care_comment(self):
        payload = self.valid_payload()
        payload["care_comment"] = 123

        with self.assertRaises(HistoryAIContentValidationError):
            validate_history_ai_content(payload)

    def test_rejects_invalid_pattern_insight(self):
        payload = self.valid_payload()
        payload["pattern_insight"] = False

        with self.assertRaises(HistoryAIContentValidationError):
            validate_history_ai_content(payload)

    def test_rejects_non_list_priority_actions(self):
        payload = self.valid_payload()
        payload["priority_actions"] = "행동"

        with self.assertRaises(HistoryAIContentValidationError):
            validate_history_ai_content(payload)

    def test_rejects_invalid_priority_action_item(self):
        for invalid_item in ("", "   ", 1, True, None):
            with self.subTest(invalid_item=invalid_item):
                payload = self.valid_payload()
                payload["priority_actions"] = [invalid_item]
                with self.assertRaises(HistoryAIContentValidationError):
                    validate_history_ai_content(payload)

    def test_rejects_more_than_two_priority_actions(self):
        payload = self.valid_payload()
        payload["priority_actions"] = ["하나", "둘", "셋"]

        with self.assertRaises(HistoryAIContentValidationError):
            validate_history_ai_content(payload)

    def test_rejects_unexpected_field_in_sync_with_schema(self):
        payload = self.valid_payload()
        payload["daily_comments"] = []

        with self.assertRaises(HistoryAIContentValidationError):
            validate_history_ai_content(payload)
        self.assertFalse(HISTORY_AI_CONTENT_SCHEMA["additionalProperties"])


class HistoryAIResultContractTests(TestCase):
    def valid_content(self):
        return {
            "weekly_summary": "주간 요약",
            "care_comment": "관리 설명",
            "pattern_insight": "패턴 설명",
            "priority_actions": ["첫 번째 행동", "두 번째 행동"],
        }

    def valid_success(self):
        return {
            "schema_version": 1,
            "status": "SUCCESS",
            "generated_at": "2026-08-12T20:30:00+09:00",
            "provider": "openai",
            "model": "test-model",
            "fallback_reason": None,
            "content": self.valid_content(),
        }

    def valid_fallback(self):
        return {
            "schema_version": 1,
            "status": "FALLBACK",
            "generated_at": "2026-08-12T20:30:00+09:00",
            "provider": "deterministic",
            "model": None,
            "fallback_reason": "OPENAI_TIMEOUT",
            "content": self.valid_content(),
        }

    def test_validates_success_result(self):
        payload = self.valid_success()

        result = validate_history_ai_result(payload)

        self.assertEqual(result, payload)
        self.assertIsNot(result, payload)
        self.assertIsNot(result["content"], payload["content"])

    def test_validates_fallback_result(self):
        payload = self.valid_fallback()

        result = validate_history_ai_result(payload)

        self.assertEqual(result, payload)

    def test_rejects_non_mapping_and_empty_uninitialized_state(self):
        for payload in (None, [], "result", {}):
            with self.subTest(payload=payload):
                with self.assertRaises(HistoryAIResultValidationError):
                    validate_history_ai_result(payload)

    def test_rejects_missing_field(self):
        payload = self.valid_success()
        payload.pop("content")

        with self.assertRaises(HistoryAIResultValidationError):
            validate_history_ai_result(payload)

    def test_rejects_unexpected_field(self):
        payload = self.valid_success()
        payload["request_id"] = "internal"

        with self.assertRaises(HistoryAIResultValidationError):
            validate_history_ai_result(payload)

    def test_rejects_unsupported_schema_version(self):
        for schema_version in (0, 2, 1.0, "1"):
            with self.subTest(schema_version=schema_version):
                payload = self.valid_success()
                payload["schema_version"] = schema_version
                with self.assertRaises(HistoryAIResultValidationError):
                    validate_history_ai_result(payload)

    def test_rejects_boolean_schema_version(self):
        payload = self.valid_success()
        payload["schema_version"] = True

        with self.assertRaises(HistoryAIResultValidationError):
            validate_history_ai_result(payload)

    def test_rejects_invalid_status(self):
        for status in ("PENDING", None, []):
            with self.subTest(status=status):
                payload = self.valid_success()
                payload["status"] = status
                with self.assertRaises(HistoryAIResultValidationError):
                    validate_history_ai_result(payload)

    def test_rejects_invalid_generated_at(self):
        for generated_at in (None, "", "not-a-datetime", 123):
            with self.subTest(generated_at=generated_at):
                payload = self.valid_success()
                payload["generated_at"] = generated_at
                with self.assertRaises(HistoryAIResultValidationError):
                    validate_history_ai_result(payload)

    def test_rejects_naive_generated_at(self):
        payload = self.valid_success()
        payload["generated_at"] = "2026-08-12T20:30:00"

        with self.assertRaises(HistoryAIResultValidationError):
            validate_history_ai_result(payload)

    def test_rejects_success_provider_mismatch(self):
        payload = self.valid_success()
        payload["provider"] = "deterministic"

        with self.assertRaises(HistoryAIResultValidationError):
            validate_history_ai_result(payload)

    def test_rejects_success_missing_model(self):
        for model in (None, "", "   "):
            with self.subTest(model=model):
                payload = self.valid_success()
                payload["model"] = model
                with self.assertRaises(HistoryAIResultValidationError):
                    validate_history_ai_result(payload)

    def test_rejects_success_fallback_reason(self):
        payload = self.valid_success()
        payload["fallback_reason"] = "OPENAI_TIMEOUT"

        with self.assertRaises(HistoryAIResultValidationError):
            validate_history_ai_result(payload)

    def test_rejects_fallback_provider_mismatch(self):
        payload = self.valid_fallback()
        payload["provider"] = "openai"

        with self.assertRaises(HistoryAIResultValidationError):
            validate_history_ai_result(payload)

    def test_rejects_fallback_model(self):
        payload = self.valid_fallback()
        payload["model"] = "test-model"

        with self.assertRaises(HistoryAIResultValidationError):
            validate_history_ai_result(payload)

    def test_rejects_fallback_missing_or_empty_reason(self):
        for fallback_reason in (None, "", "   "):
            with self.subTest(fallback_reason=fallback_reason):
                payload = self.valid_fallback()
                payload["fallback_reason"] = fallback_reason
                with self.assertRaises(HistoryAIResultValidationError):
                    validate_history_ai_result(payload)

    def test_wraps_invalid_content_as_result_validation_error(self):
        payload = self.valid_success()
        payload["content"]["weekly_summary"] = ""

        with self.assertRaises(HistoryAIResultValidationError) as raised:
            validate_history_ai_result(payload)

        self.assertIsInstance(
            raised.exception.__cause__,
            HistoryAIContentValidationError,
        )

    def test_returns_normalized_content_and_metadata_strings(self):
        payload = self.valid_fallback()
        payload["generated_at"] = "  2026-08-12T20:30:00+09:00  "
        payload["fallback_reason"] = "  OPENAI_TIMEOUT  "
        payload["content"]["weekly_summary"] = "  주간 요약  "

        result = validate_history_ai_result(payload)

        self.assertEqual(result["generated_at"], "2026-08-12T20:30:00+09:00")
        self.assertEqual(result["fallback_reason"], "OPENAI_TIMEOUT")
        self.assertEqual(result["content"]["weekly_summary"], "주간 요약")

    def test_does_not_mutate_input(self):
        payload = self.valid_success()
        original_payload = deepcopy(payload)

        result = validate_history_ai_result(payload)
        result["content"]["priority_actions"].append("변경")

        self.assertEqual(payload, original_payload)

    def test_validated_result_is_json_serializable(self):
        result = validate_history_ai_result(self.valid_success())

        json.dumps(result, allow_nan=False)
        JSONRenderer().render(result)


class HistoryAIFallbackTests(HistoryAITestCase):
    def test_builds_normal_fallback(self):
        report = self.create_normal_report()

        fallback = build_history_ai_fallback(report)

        self.assertEqual(
            fallback["weekly_summary"],
            "최근 7일 동안 확인 가능한 지표에서는 관리 기준을 초과한 기록이 없었어요.",
        )
        self.assertEqual(fallback["priority_actions"], [])

    def test_builds_warning_fallback_from_stored_metric(self):
        report = self.create_warning_report()

        fallback = build_history_ai_fallback(report)

        self.assertIn("하중 기준을 초과한 날이 7일", fallback["weekly_summary"])
        self.assertNotIn("6.000000", fallback["weekly_summary"])

    def test_uses_active_rule_care_action(self):
        report = self.create_warning_report()

        fallback = build_history_ai_fallback(report)

        self.assertEqual(
            fallback["care_comment"],
            "HIGH_LOAD care HIGH_LOAD reason",
        )
        self.assertEqual(fallback["priority_actions"], ["HIGH_LOAD step"])

    def test_limits_priority_actions_to_two(self):
        session = self.create_session()
        self.add_readings(session)
        report, _created = analyze_history_session(session)

        fallback = build_history_ai_fallback(report)

        self.assertEqual(len(fallback["priority_actions"]), 2)
        self.assertEqual(
            fallback["priority_actions"],
            ["HIGH_LOAD step", "HIGH_TEMPERATURE step"],
        )

    def test_deduplicates_priority_actions_in_active_rule_order(self):
        session = self.create_session()
        self.add_readings(session)
        report, _created = analyze_history_session(session)
        report.care_guideline_snapshot["care_actions"]["HIGH_LOAD"]["steps"] = [
            "same step",
            "same step",
        ]
        report.care_guideline_snapshot["care_actions"]["HIGH_TEMPERATURE"][
            "steps"
        ] = ["same step", "second step"]

        fallback = build_history_ai_fallback(report)

        self.assertEqual(
            fallback["priority_actions"],
            ["same step", "second step"],
        )

    def test_builds_available_comparison_pattern(self):
        report = self.create_available_comparison_report()

        fallback = build_history_ai_fallback(report)

        self.assertEqual(
            fallback["pattern_insight"],
            "평균 하중이 이전 7일보다 50.00% 늘었어요. "
            "과부하 발생일이 이전 7일보다 7일 늘었어요.",
        )

    def test_builds_no_previous_period_pattern(self):
        report = self.create_normal_report()

        fallback = build_history_ai_fallback(report)

        self.assertEqual(
            fallback["pattern_insight"],
            "이전 기록이 아직 충분하지 않아 이번 기간의 변화 비교를 제공하지 않았어요.",
        )

    def test_zero_to_positive_change_does_not_invent_one_hundred_percent(self):
        report = self.create_normal_report()
        self.set_comparison_metrics(
            report,
            {
                "load": {
                    "average_kg": {
                        "current": 5,
                        "previous": 0,
                        "change": 5,
                        "change_percent": None,
                    }
                }
            },
        )

        fallback = build_history_ai_fallback(report)

        self.assertIn("5.00kg", fallback["pattern_insight"])
        self.assertNotIn("100%", fallback["pattern_insight"])

    def test_formats_bias_and_deformation_changes_as_percentage_points(self):
        report = self.create_normal_report()
        self.set_comparison_metrics(
            report,
            {
                "load_bias": {
                    "max_absolute_percent": {
                        "current": 15,
                        "previous": 10,
                        "change": 5,
                        "change_percent": 50,
                    }
                },
                "deformation": {
                    "latest_percent": {
                        "current": 4,
                        "previous": 2,
                        "change": 2,
                        "change_percent": 100,
                    }
                },
            },
        )

        fallback = build_history_ai_fallback(report)

        self.assertIn("5.00%p", fallback["pattern_insight"])
        self.assertIn("2.00%p", fallback["pattern_insight"])
        self.assertNotIn("50.00% ", fallback["pattern_insight"])
        self.assertNotIn("100.00%", fallback["pattern_insight"])

    def test_fallback_passes_complete_output_contract_validation(self):
        report = self.create_warning_report()

        fallback = build_history_ai_fallback(report)

        self.assertEqual(validate_history_ai_content(fallback), fallback)

    def test_fallback_is_json_serializable(self):
        report = self.create_available_comparison_report()

        fallback = build_history_ai_fallback(report)

        json.dumps(fallback, allow_nan=False)
        JSONRenderer().render(fallback)

    def test_fallback_does_not_mutate_report_snapshots(self):
        report = self.create_available_comparison_report()
        original_metrics = deepcopy(report.metrics)
        original_comparison = deepcopy(report.comparison)
        original_guideline = deepcopy(report.care_guideline_snapshot)
        original_active_rules = deepcopy(report.active_rules)

        build_history_ai_fallback(report)

        self.assertEqual(report.metrics, original_metrics)
        self.assertEqual(report.comparison, original_comparison)
        self.assertEqual(report.care_guideline_snapshot, original_guideline)
        self.assertEqual(report.active_rules, original_active_rules)


class HistoryAIClientInfrastructureTests(TestCase):
    def build_request(self):
        return httpx2.Request("POST", "https://api.openai.com/v1/responses")

    def assert_mapped_error(self, sdk_error, expected_reason):
        with self.assertRaises(HistoryAIGenerationError) as raised:
            raise_history_ai_generation_error(sdk_error)

        self.assertEqual(raised.exception.reason, expected_reason)
        self.assertIs(raised.exception.__cause__, sdk_error)

    @override_settings(OPENAI_API_KEY="")
    def test_module_import_does_not_construct_client_without_key(self):
        import importlib
        import analysis.ai.client as client_module

        with patch("openai.OpenAI") as openai_constructor:
            importlib.reload(client_module)
            openai_constructor.assert_not_called()
        importlib.reload(client_module)

    @override_settings(OPENAI_API_KEY="")
    def test_missing_key_raises_not_configured_at_client_creation(self):
        with self.assertRaises(HistoryAIGenerationError) as raised:
            get_openai_client()

        self.assertEqual(
            raised.exception.reason,
            HistoryAIGenerationReason.OPENAI_NOT_CONFIGURED.value,
        )

    @override_settings(OPENAI_MODEL=None)
    def test_openai_model_default(self):
        self.assertEqual(DEFAULT_OPENAI_MODEL, "gpt-5.6-terra")
        self.assertEqual(get_openai_model(), "gpt-5.6-terra")

    @override_settings(OPENAI_MODEL="  configured-model  ")
    def test_openai_model_override(self):
        self.assertEqual(get_openai_model(), "configured-model")

    @override_settings(OPENAI_TIMEOUT_SECONDS=None)
    def test_timeout_default(self):
        self.assertEqual(DEFAULT_OPENAI_TIMEOUT_SECONDS, 12.0)
        self.assertEqual(get_openai_timeout_seconds(), 12.0)

    @override_settings(OPENAI_TIMEOUT_SECONDS="4.5")
    def test_timeout_override(self):
        self.assertEqual(get_openai_timeout_seconds(), 4.5)

    def test_client_module_uses_sync_openai_only(self):
        import analysis.ai.client as client_module
        from openai import OpenAI

        self.assertIs(client_module.OpenAI, OpenAI)
        self.assertFalse(hasattr(client_module, "AsyncOpenAI"))

    @override_settings(
        OPENAI_API_KEY="dummy-test-key",
        OPENAI_TIMEOUT_SECONDS=12,
    )
    def test_client_construction_applies_timeout_and_retry(self):
        client_sentinel = object()

        with patch(
            "analysis.ai.client.OpenAI",
            return_value=client_sentinel,
        ) as openai_constructor:
            result = get_openai_client()

        self.assertIs(result, client_sentinel)
        openai_constructor.assert_called_once_with(
            api_key="dummy-test-key",
            timeout=12.0,
            max_retries=OPENAI_MAX_RETRIES,
        )
        self.assertEqual(OPENAI_MAX_RETRIES, 1)

    def test_generation_error_exposes_reason_without_secret(self):
        error = HistoryAIGenerationError(
            HistoryAIGenerationReason.OPENAI_NOT_CONFIGURED
        )

        self.assertEqual(error.reason, "OPENAI_NOT_CONFIGURED")
        self.assertEqual(str(error), "OPENAI_NOT_CONFIGURED")
        self.assertNotIn("key", str(error).lower())

    def test_maps_sdk_timeout(self):
        error = APITimeoutError(request=self.build_request())

        self.assert_mapped_error(error, "OPENAI_TIMEOUT")

    def test_maps_sdk_rate_limit(self):
        request = self.build_request()
        response = httpx2.Response(429, request=request)
        error = RateLimitError("rate limited", response=response, body=None)

        self.assert_mapped_error(error, "OPENAI_RATE_LIMIT")

    def test_maps_sdk_connection_error(self):
        error = APIConnectionError(request=self.build_request())

        self.assert_mapped_error(error, "OPENAI_CONNECTION_ERROR")

    def test_maps_generic_sdk_api_status_error(self):
        request = self.build_request()
        response = httpx2.Response(500, request=request)
        error = APIStatusError("provider error", response=response, body=None)

        self.assert_mapped_error(error, "OPENAI_API_ERROR")

    def test_maps_sdk_response_validation_error(self):
        request = self.build_request()
        response = httpx2.Response(200, request=request)
        error = APIResponseValidationError(
            response=response,
            body={"unexpected": "payload"},
            message="invalid provider response",
        )

        with self.assertRaises(HistoryAIGenerationError) as raised:
            raise_history_ai_generation_error(error)

        self.assertEqual(raised.exception.reason, "OPENAI_API_ERROR")
        self.assertIs(raised.exception.__cause__, error)

    def test_mapping_preserves_original_exception_as_cause(self):
        error = APITimeoutError(request=self.build_request())

        with self.assertRaises(HistoryAIGenerationError) as raised:
            raise_history_ai_generation_error(error)

        self.assertIs(raised.exception.__cause__, error)

    def test_unknown_programming_exception_is_not_mapped(self):
        error = ValueError("programming error")

        with self.assertRaises(ValueError) as raised:
            raise_history_ai_generation_error(error)

        self.assertIs(raised.exception, error)


class HistoryAIGenerationAdapterTests(TestCase):
    def build_request(self):
        return httpx2.Request("POST", "https://api.openai.com/v1/responses")

    def build_context(self):
        return {
            "period": {"timezone": "Asia/Seoul"},
            "metrics": {"load": {"average_kg": 4.25}},
            "severity": "NORMAL",
            "active_rules": [],
            "unavailable_rules": [],
            "care_guideline_snapshot": {"care_actions": {}},
            "comparison": {
                "available": False,
                "reason": "NO_PREVIOUS_PERIOD",
                "previous_period": None,
                "metrics": None,
            },
        }

    def build_content(self):
        return {
            "weekly_summary": "주간 요약",
            "care_comment": "관리 설명",
            "pattern_insight": "비교 설명",
            "priority_actions": ["관리 행동"],
        }

    def build_response(self, *, status="completed", output_text=None, output=None):
        if output_text is None:
            output_text = json.dumps(self.build_content(), ensure_ascii=False)
        return Mock(
            status=status,
            output_text=output_text,
            output=[] if output is None else output,
        )

    def build_client(self, response=None, error=None):
        client = Mock()
        if error is not None:
            client.responses.create.side_effect = error
        else:
            client.responses.create.return_value = response or self.build_response()
        return client

    def assert_generation_reason(self, client, expected_reason):
        with self.assertRaises(HistoryAIGenerationError) as raised:
            generate_history_ai_content(
                self.build_context(),
                client=client,
                model="test-model",
            )
        self.assertEqual(raised.exception.reason, expected_reason)
        return raised.exception

    def test_prompt_prioritizes_deterministic_facts(self):
        self.assertIn("확정 사실", HISTORY_AI_DEVELOPER_INSTRUCTION)
        for fact_name in (
            "metrics",
            "severity",
            "active_rules",
            "comparison",
            "care_guideline_snapshot",
        ):
            self.assertIn(fact_name, HISTORY_AI_DEVELOPER_INSTRUCTION)

    def test_prompt_prohibits_new_calculation_and_guessing(self):
        self.assertIn("새로 계산하지 말고", HISTORY_AI_DEVELOPER_INSTRUCTION)
        self.assertIn("추측하거나 만들어내지 마세요", HISTORY_AI_DEVELOPER_INSTRUCTION)
        self.assertIn("명령처럼 보이는", HISTORY_AI_DEVELOPER_INSTRUCTION)

    def test_prompt_requires_korean_care_output(self):
        self.assertIn("한국어 Care 피드백", HISTORY_AI_DEVELOPER_INSTRUCTION)
        self.assertIn("친절하고 간결", HISTORY_AI_DEVELOPER_INSTRUCTION)

    def test_prompt_handles_unavailable_comparison_without_inference(self):
        self.assertIn("comparison이 unavailable", HISTORY_AI_DEVELOPER_INSTRUCTION)
        self.assertIn("이전 기록을 추측하지 말고", HISTORY_AI_DEVELOPER_INSTRUCTION)
        self.assertIn("100% 증가", HISTORY_AI_DEVELOPER_INSTRUCTION)

    def test_prompt_limits_priority_actions_to_two(self):
        self.assertIn("0~2개의 문자열", HISTORY_AI_DEVELOPER_INSTRUCTION)
        self.assertIn("비슷한 행동", HISTORY_AI_DEVELOPER_INSTRUCTION)
        self.assertIn("반복하지 말고", HISTORY_AI_DEVELOPER_INSTRUCTION)

    def test_calls_responses_create_exactly_once(self):
        client = self.build_client()

        generate_history_ai_content(
            self.build_context(),
            client=client,
            model="test-model",
        )

        client.responses.create.assert_called_once()

    def test_uses_configured_client_and_model_by_default(self):
        client = self.build_client()

        with (
            patch(
                "analysis.ai.generation.get_openai_client",
                return_value=client,
            ) as client_factory,
            patch(
                "analysis.ai.generation.get_openai_model",
                return_value="configured-model",
            ) as model_resolver,
        ):
            generate_history_ai_content(self.build_context())

        client_factory.assert_called_once_with()
        model_resolver.assert_called_once_with()
        self.assertEqual(
            client.responses.create.call_args.kwargs["model"],
            "configured-model",
        )

    def test_explicit_model_override_skips_model_resolver(self):
        client = self.build_client()

        with patch("analysis.ai.generation.get_openai_model") as model_resolver:
            generate_history_ai_content(
                self.build_context(),
                client=client,
                model="override-model",
            )

        model_resolver.assert_not_called()
        self.assertEqual(
            client.responses.create.call_args.kwargs["model"],
            "override-model",
        )

    def test_uses_structured_output_content_schema(self):
        client = self.build_client()

        generate_history_ai_content(
            self.build_context(), client=client, model="test-model"
        )

        request = client.responses.create.call_args.kwargs
        response_format = request["text"]["format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertEqual(response_format["name"], HISTORY_AI_SCHEMA_NAME)
        self.assertTrue(response_format["strict"])
        self.assertIs(response_format["schema"], HISTORY_AI_CONTENT_SCHEMA)

    def test_disables_provider_storage_and_streaming_without_tools(self):
        client = self.build_client()

        generate_history_ai_content(
            self.build_context(), client=client, model="test-model"
        )

        request = client.responses.create.call_args.kwargs
        self.assertIs(request["store"], False)
        self.assertIs(request["stream"], False)
        self.assertNotIn("tools", request)
        self.assertNotIn("background", request)
        self.assertNotIn("previous_response_id", request)
        self.assertNotIn("conversation", request)
        client.chat.completions.create.assert_not_called()

    def test_sets_safe_output_limit_without_unverified_reasoning_option(self):
        client = self.build_client()

        generate_history_ai_content(
            self.build_context(), client=client, model="test-model"
        )

        request = client.responses.create.call_args.kwargs
        self.assertEqual(
            request["max_output_tokens"],
            HISTORY_AI_MAX_OUTPUT_TOKENS,
        )
        self.assertEqual(HISTORY_AI_MAX_OUTPUT_TOKENS, 1200)
        self.assertNotIn("reasoning", request)

    def test_serializes_context_as_readable_strict_json(self):
        context = self.build_context()
        context["care_guideline_snapshot"]["material_note"] = "가죽 관리"
        client = self.build_client()

        generate_history_ai_content(context, client=client, model="test-model")

        request_input = client.responses.create.call_args.kwargs["input"]
        self.assertIsInstance(request_input, str)
        self.assertIn("가죽 관리", request_input)
        self.assertEqual(json.loads(request_input), context)

    def test_does_not_print_or_log_context(self):
        client = self.build_client()

        with (
            patch("builtins.print") as print_mock,
            patch("logging.Logger._log") as log_mock,
        ):
            generate_history_ai_content(
                self.build_context(), client=client, model="test-model"
            )

        print_mock.assert_not_called()
        log_mock.assert_not_called()

    def test_returns_normalized_valid_content(self):
        content = self.build_content()
        content["weekly_summary"] = "  주간 요약  "
        response = self.build_response(
            output_text=json.dumps(content, ensure_ascii=False)
        )

        result = generate_history_ai_content(
            self.build_context(),
            client=self.build_client(response),
            model="test-model",
        )

        self.assertEqual(
            result,
            {
                "weekly_summary": "주간 요약",
                "care_comment": "관리 설명",
                "pattern_insight": "비교 설명",
                "priority_actions": ["관리 행동"],
            },
        )

    def test_success_preserves_all_four_content_fields(self):
        result = generate_history_ai_content(
            self.build_context(),
            client=self.build_client(),
            model="test-model",
        )

        self.assertEqual(
            set(result),
            {
                "weekly_summary",
                "care_comment",
                "pattern_insight",
                "priority_actions",
            },
        )

    def test_success_does_not_mutate_input_context(self):
        context = self.build_context()
        original_context = deepcopy(context)

        generate_history_ai_content(
            context,
            client=self.build_client(),
            model="test-model",
        )

        self.assertEqual(context, original_context)

    def test_success_result_is_json_serializable(self):
        result = generate_history_ai_content(
            self.build_context(),
            client=self.build_client(),
            model="test-model",
        )

        json.dumps(result, ensure_ascii=False, allow_nan=False)
        JSONRenderer().render(result)

    def test_detects_refusal_content(self):
        refusal = Mock(type="refusal", refusal="not returned")
        message = Mock(type="message", content=[refusal])
        response = self.build_response(output=[message])

        self.assert_generation_reason(
            self.build_client(response),
            "OPENAI_REFUSAL",
        )

    def test_detects_incomplete_response(self):
        response = self.build_response(status="incomplete")

        self.assert_generation_reason(
            self.build_client(response),
            "OPENAI_INCOMPLETE",
        )

    def test_rejects_non_completed_response_states(self):
        for status in ("failed", "cancelled", "queued", "in_progress", None):
            with self.subTest(status=status):
                response = self.build_response(status=status)
                self.assert_generation_reason(
                    self.build_client(response),
                    "OPENAI_API_ERROR",
                )

    def test_rejects_none_output_text(self):
        response = self.build_response()
        response.output_text = None

        self.assert_generation_reason(
            self.build_client(response),
            "EMPTY_AI_RESPONSE",
        )

    def test_rejects_blank_output_text(self):
        response = self.build_response(output_text="   ")

        self.assert_generation_reason(
            self.build_client(response),
            "EMPTY_AI_RESPONSE",
        )

    def test_rejects_invalid_json_with_cause(self):
        response = self.build_response(output_text="{invalid")

        error = self.assert_generation_reason(
            self.build_client(response),
            "INVALID_AI_RESPONSE",
        )

        self.assertIsInstance(error.__cause__, json.JSONDecodeError)

    def test_rejects_json_with_missing_content_field(self):
        content = self.build_content()
        content.pop("care_comment")
        response = self.build_response(output_text=json.dumps(content))

        self.assert_generation_reason(
            self.build_client(response),
            "INVALID_AI_RESPONSE",
        )

    def test_rejects_more_than_two_priority_actions(self):
        content = self.build_content()
        content["priority_actions"] = ["하나", "둘", "셋"]
        response = self.build_response(
            output_text=json.dumps(content, ensure_ascii=False)
        )

        self.assert_generation_reason(
            self.build_client(response),
            "INVALID_AI_RESPONSE",
        )

    def test_preserves_content_validator_error_as_cause(self):
        content = self.build_content()
        content["weekly_summary"] = ""
        response = self.build_response(output_text=json.dumps(content))

        error = self.assert_generation_reason(
            self.build_client(response),
            "INVALID_AI_RESPONSE",
        )

        self.assertIsInstance(error.__cause__, HistoryAIContentValidationError)

    def test_maps_responses_timeout_with_existing_helper(self):
        sdk_error = APITimeoutError(request=self.build_request())

        error = self.assert_generation_reason(
            self.build_client(error=sdk_error),
            "OPENAI_TIMEOUT",
        )

        self.assertIs(error.__cause__, sdk_error)

    def test_maps_responses_rate_limit_with_existing_helper(self):
        request = self.build_request()
        response = httpx2.Response(429, request=request)
        sdk_error = RateLimitError("rate limited", response=response, body=None)

        error = self.assert_generation_reason(
            self.build_client(error=sdk_error),
            "OPENAI_RATE_LIMIT",
        )

        self.assertIs(error.__cause__, sdk_error)

    def test_maps_responses_connection_error_with_existing_helper(self):
        sdk_error = APIConnectionError(request=self.build_request())

        error = self.assert_generation_reason(
            self.build_client(error=sdk_error),
            "OPENAI_CONNECTION_ERROR",
        )

        self.assertIs(error.__cause__, sdk_error)

    def test_maps_responses_api_status_error_with_existing_helper(self):
        request = self.build_request()
        response = httpx2.Response(500, request=request)
        sdk_error = APIStatusError("provider error", response=response, body=None)

        error = self.assert_generation_reason(
            self.build_client(error=sdk_error),
            "OPENAI_API_ERROR",
        )

        self.assertIs(error.__cause__, sdk_error)

    def test_maps_responses_validation_error_with_existing_helper(self):
        request = self.build_request()
        response = httpx2.Response(200, request=request)
        sdk_error = APIResponseValidationError(
            response=response,
            body={"unexpected": "payload"},
            message="invalid provider response",
        )

        error = self.assert_generation_reason(
            self.build_client(error=sdk_error),
            "OPENAI_API_ERROR",
        )

        self.assertIs(error.__cause__, sdk_error)

    def test_unknown_programming_error_is_not_mapped(self):
        client = self.build_client(error=TypeError("programming error"))

        with self.assertRaises(TypeError) as raised:
            generate_history_ai_content(
                self.build_context(),
                client=client,
                model="test-model",
            )

        self.assertEqual(str(raised.exception), "programming error")
