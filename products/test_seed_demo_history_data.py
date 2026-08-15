from copy import deepcopy
from datetime import datetime, timedelta
from io import StringIO
from unittest.mock import patch
from uuid import UUID

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from analysis.models import AnalysisReport
from measurements.models import MeasurementSession, SensorReading
from simulation.models import SimulationScenario
from simulation.services import create_simulation_session

from .management.commands.seed_demo_history_data import (
    CURRENT_STARTED_AT,
    LEGACY_PRODUCT_A_SCENARIO_CODES,
    MANAGED_HISTORY_SPECS,
    PREVIOUS_STARTED_AT,
    PRODUCT_A_IDENTITY,
    PRODUCT_A_NFC_UID,
    PRODUCT_B_IDENTITY,
    PRODUCT_B_NFC_UID,
    _scenario_defaults,
)
from .models import Bag, ProductModel


class SeedDemoHistoryDataTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(
            username="demo-history-owner",
            password="test-password",
        )
        guideline_common = {
            "avoid_moisture": True,
            "max_load_kg": 5.5,
            "recommended_temp_range_c": [0, 35],
            "max_abs_load_bias": 0.30,
            "max_body_deformation_ratio": 0.03,
        }
        cls.product_a = ProductModel.objects.create(
            brand=PRODUCT_A_IDENTITY[0],
            model_name=PRODUCT_A_IDENTITY[1],
            material="Leather",
            care_guideline={**guideline_common, "max_humidity_percent": 70},
        )
        cls.product_b = ProductModel.objects.create(
            brand=PRODUCT_B_IDENTITY[0],
            model_name=PRODUCT_B_IDENTITY[1],
            material="Suede",
            care_guideline={**guideline_common, "max_humidity_percent": 60},
        )
        cls.bag_a = Bag.objects.create(
            product_model=cls.product_a,
            owner=cls.owner,
            nfc_uid=PRODUCT_A_NFC_UID,
            public_token=UUID("11111111-1111-1111-1111-111111111111"),
        )
        cls.bag_b = Bag.objects.create(
            product_model=cls.product_b,
            owner=cls.owner,
            nfc_uid=PRODUCT_B_NFC_UID,
            public_token=UUID("22222222-2222-2222-2222-222222222222"),
        )

    def setUp(self):
        self.client = APIClient()

    def run_seed(self):
        call_command("seed_demo_history_data", stdout=StringIO())

    def aware(self, value):
        return timezone.make_aware(value, timezone.get_default_timezone())

    def get_managed(self, code):
        return MeasurementSession.objects.get(
            scenario__code=code,
            started_at=self.aware(
                next(
                    spec["started_at"]
                    for spec in MANAGED_HISTORY_SPECS
                    if spec["code"] == code
                )
            ),
        )

    def create_legacy_session(self, *, bag, code, started_at):
        scenario, _created = SimulationScenario.objects.get_or_create(
            code=code,
            defaults={
                "name": code,
                "scenario_type": SimulationScenario.ScenarioType.NORMAL,
                "mode": SimulationScenario.Mode.HISTORY,
                "logical_duration_seconds": 604800,
                "sample_interval_seconds": 86400,
                "config": {
                    "strap_load": {"start": 2.0, "end": 2.5},
                    "temperature": {"start": 20, "end": 24},
                    "humidity": {"start": 40, "end": 45},
                    "load_bias": {"start": 0.01, "end": 0.02},
                    "body_deformation_ratio": {"start": 0.001, "end": 0.002},
                    "moisture_event": {"enabled": False},
                },
                "version": 1,
                "is_active": True,
            },
        )
        session, _count = create_simulation_session(
            bag,
            scenario.code,
            random_seed=991,
            started_at=started_at,
        )
        report = AnalysisReport.objects.create(
            session=session,
            metrics={"protected": True},
            severity="NORMAL",
            active_rules=[],
            unavailable_rules=[],
            care_guideline_snapshot={"protected": True},
            comparison={"protected": True},
            ai_result={"protected": True},
        )
        return session, report

    @patch("analysis.services.generate_history_ai_content")
    @patch("analysis.services.analyze_history_session_with_ai")
    def test_seeds_final_history_idempotently_and_preserves_unrelated_data(
        self,
        analyze_with_ai,
        generate_ai,
    ):
        legacy_a = []
        for index, code in enumerate(sorted(LEGACY_PRODUCT_A_SCENARIO_CODES)):
            started_at = (
                self.aware(PREVIOUS_STARTED_AT)
                if index == 0
                else self.aware(datetime(2026, 8, 16) + timedelta(days=index))
            )
            legacy_a.append(
                self.create_legacy_session(
                    bag=self.bag_a,
                    code=code,
                    started_at=started_at,
                )
            )
        unrelated_session, unrelated_report = self.create_legacy_session(
            bag=self.bag_b,
            code="OVERLOAD_HISTORY",
            started_at=self.aware(datetime(2026, 8, 25)),
        )
        unrelated_readings = list(
            unrelated_session.readings.order_by("sequence").values()
        )
        unrelated_snapshot = {
            "metrics": deepcopy(unrelated_report.metrics),
            "care_guideline_snapshot": deepcopy(
                unrelated_report.care_guideline_snapshot
            ),
            "comparison": deepcopy(unrelated_report.comparison),
            "ai_result": deepcopy(unrelated_report.ai_result),
            "updated_at": unrelated_report.updated_at,
        }
        legacy_reading_snapshots = {
            session.pk: list(session.readings.order_by("sequence").values())
            for session, _report in legacy_a
        }
        product_snapshot = {
            "a": (
                self.product_a.brand,
                self.product_a.model_name,
                self.product_a.material,
                deepcopy(self.product_a.care_guideline),
            ),
            "b": (
                self.product_b.brand,
                self.product_b.model_name,
                self.product_b.material,
                deepcopy(self.product_b.care_guideline),
            ),
            "bag_a": (
                self.bag_a.product_model_id,
                self.bag_a.owner_id,
                self.bag_a.public_token,
            ),
            "bag_b": (
                self.bag_b.product_model_id,
                self.bag_b.owner_id,
                self.bag_b.public_token,
            ),
        }

        self.run_seed()
        managed_sessions = {
            spec["code"]: self.get_managed(spec["code"])
            for spec in MANAGED_HISTORY_SPECS
        }
        first_snapshot = {
            code: {
                "session_pk": session.pk,
                "seed": session.seed,
                "report_pk": session.analysis_report.pk,
                "metrics": deepcopy(session.analysis_report.metrics),
                "report_updated_at": session.analysis_report.updated_at,
            }
            for code, session in managed_sessions.items()
        }
        counts = {
            "scenarios": SimulationScenario.objects.count(),
            "sessions": MeasurementSession.objects.count(),
            "readings": SensorReading.objects.count(),
            "reports": AnalysisReport.objects.count(),
        }

        self.run_seed()

        analyze_with_ai.assert_not_called()
        generate_ai.assert_not_called()
        self.assertEqual(SimulationScenario.objects.count(), counts["scenarios"])
        self.assertEqual(MeasurementSession.objects.count(), counts["sessions"])
        self.assertEqual(SensorReading.objects.count(), counts["readings"])
        self.assertEqual(AnalysisReport.objects.count(), counts["reports"])

        for spec in MANAGED_HISTORY_SPECS:
            scenario = SimulationScenario.objects.get(code=spec["code"])
            for field, value in _scenario_defaults(spec).items():
                self.assertEqual(getattr(scenario, field), value)

            session = self.get_managed(spec["code"])
            report = session.analysis_report
            self.assertEqual(session.pk, first_snapshot[spec["code"]]["session_pk"])
            self.assertEqual(session.seed, spec["seed"])
            self.assertEqual(session.seed, first_snapshot[spec["code"]]["seed"])
            self.assertEqual(session.purpose, MeasurementSession.Purpose.HISTORY)
            self.assertEqual(session.status, MeasurementSession.Status.COMPLETED)
            self.assertEqual(session.ended_at - session.started_at, timedelta(days=7))
            self.assertIs(session.include_in_report, True)
            self.assertEqual(
                list(session.readings.values_list("sequence", flat=True)),
                list(range(7)),
            )
            self.assertEqual(
                list(session.readings.values_list("measured_at", flat=True)),
                [session.started_at + timedelta(days=index) for index in range(7)],
            )
            self.assertEqual(report.pk, first_snapshot[spec["code"]]["report_pk"])
            self.assertEqual(report.metrics, first_snapshot[spec["code"]]["metrics"])
            self.assertEqual(
                report.updated_at,
                first_snapshot[spec["code"]]["report_updated_at"],
            )
            self.assertEqual(report.severity, "NORMAL")
            self.assertEqual(report.active_rules, [])
            self.assertEqual(report.unavailable_rules, [])
            self.assertEqual(len(report.metrics["daily_series"]), 7)
            self.assertEqual(report.ai_result, {})

        for legacy_session, legacy_report in legacy_a:
            legacy_session.refresh_from_db()
            legacy_report.refresh_from_db()
            self.assertIs(legacy_session.include_in_report, False)
            self.assertEqual(legacy_report.metrics, {"protected": True})
            self.assertEqual(
                legacy_report.care_guideline_snapshot,
                {"protected": True},
            )
            self.assertEqual(legacy_report.comparison, {"protected": True})
            self.assertEqual(legacy_report.ai_result, {"protected": True})
            self.assertEqual(
                list(legacy_session.readings.order_by("sequence").values()),
                legacy_reading_snapshots[legacy_session.pk],
            )

        unrelated_session.refresh_from_db()
        unrelated_report.refresh_from_db()
        self.assertIs(unrelated_session.include_in_report, True)
        self.assertEqual(
            list(unrelated_session.readings.order_by("sequence").values()),
            unrelated_readings,
        )
        self.assertEqual(unrelated_report.metrics, unrelated_snapshot["metrics"])
        self.assertEqual(
            unrelated_report.care_guideline_snapshot,
            unrelated_snapshot["care_guideline_snapshot"],
        )
        self.assertEqual(
            unrelated_report.comparison,
            unrelated_snapshot["comparison"],
        )
        self.assertEqual(unrelated_report.ai_result, unrelated_snapshot["ai_result"])
        self.assertEqual(unrelated_report.updated_at, unrelated_snapshot["updated_at"])

        self.product_a.refresh_from_db()
        self.product_b.refresh_from_db()
        self.bag_a.refresh_from_db()
        self.bag_b.refresh_from_db()
        self.assertEqual(
            (
                self.product_a.brand,
                self.product_a.model_name,
                self.product_a.material,
                self.product_a.care_guideline,
            ),
            product_snapshot["a"],
        )
        self.assertEqual(
            (
                self.product_b.brand,
                self.product_b.model_name,
                self.product_b.material,
                self.product_b.care_guideline,
            ),
            product_snapshot["b"],
        )
        self.assertEqual(
            (
                self.bag_a.product_model_id,
                self.bag_a.owner_id,
                self.bag_a.public_token,
            ),
            product_snapshot["bag_a"],
        )
        self.assertEqual(
            (
                self.bag_b.product_model_id,
                self.bag_b.owner_id,
                self.bag_b.public_token,
            ),
            product_snapshot["bag_b"],
        )

        self._assert_product_result("A", self.bag_a, managed_sessions)
        self._assert_product_result("B", self.bag_b, managed_sessions)

    def _assert_product_result(self, product_key, bag, managed_sessions):
        previous_code = next(
            spec["code"]
            for spec in MANAGED_HISTORY_SPECS
            if spec["product_key"] == product_key
            and spec["period_key"] == "previous"
        )
        current_code = next(
            spec["code"]
            for spec in MANAGED_HISTORY_SPECS
            if spec["product_key"] == product_key
            and spec["period_key"] == "current"
        )
        previous = managed_sessions[previous_code]
        current = managed_sessions[current_code]
        self.assertEqual(previous.started_at, self.aware(PREVIOUS_STARTED_AT))
        self.assertEqual(previous.ended_at, self.aware(CURRENT_STARTED_AT))
        self.assertEqual(current.started_at, previous.ended_at)
        self.assertEqual(
            current.ended_at,
            self.aware(CURRENT_STARTED_AT) + timedelta(days=7),
        )
        self.assertTrue(current.analysis_report.comparison["available"])
        self.assertEqual(
            current.analysis_report.comparison["previous_session_id"],
            previous.pk,
        )

        with patch(
            "analysis.views.get_current_time",
            return_value=self.aware(datetime(2026, 8, 30)),
        ):
            response = self.client.get(
                reverse(
                    "bag-latest-analysis-report",
                    kwargs={"public_token": bag.public_token},
                )
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["id"], current.analysis_report.pk)
        self.assertEqual(response.data["severity"], "NORMAL")
        self.assertEqual(response.data["active_rules"], [])
        self.assertEqual(response.data["unavailable_rules"], [])
        self.assertEqual(len(response.data["metrics"]["daily_series"]), 7)
        self.assertTrue(
            all(
                "presentation" in item
                for item in response.data["metrics"]["daily_series"]
            )
        )
        self.assertEqual(len(response.data["charts"]["load"]), 7)
        self.assertEqual(len(response.data["charts"]["shape"]), 7)
        self.assertEqual(len(response.data["charts"]["environment"]), 7)
        self.assertEqual(
            set(response.data["charts"]["load"][0]),
            {
                "date",
                "display_date",
                "total_load_kg",
                "left_load_percent",
                "right_load_percent",
            },
        )
        self.assertEqual(
            set(response.data["charts"]["shape"][0]),
            {"date", "display_date", "shape_deviation_percent"},
        )
        self.assertEqual(
            set(response.data["charts"]["environment"][0]),
            {
                "date",
                "display_date",
                "temperature_c",
                "internal_humidity_percent",
                "material_moisture_percent",
            },
        )
        self.assertEqual(
            set(response.data["comparison"]["metrics"]),
            {
                "load",
                "temperature",
                "humidity",
                "moisture",
                "load_bias",
                "deformation",
            },
        )
        moisture_values = [
            item["material_moisture_percent"]
            for item in response.data["charts"]["environment"]
        ]
        if product_key == "A":
            self.assertEqual(moisture_values, [None] * 7)
        else:
            self.assertTrue(all(value is not None for value in moisture_values))
            self.assertFalse(current.readings.filter(moisture_detected=True).exists())

    def test_repairs_missing_reading_and_restores_managed_eligibility(self):
        self.run_seed()
        session = self.get_managed("MCM_VELA_HISTORY_CURRENT")
        report = session.analysis_report
        original_metrics = deepcopy(report.metrics)
        original_updated_at = report.updated_at
        session.readings.get(sequence=3).delete()
        session.include_in_report = False
        session.save(update_fields=["include_in_report"])

        self.run_seed()

        session.refresh_from_db()
        report.refresh_from_db()
        self.assertIs(session.include_in_report, True)
        self.assertEqual(
            list(session.readings.values_list("sequence", flat=True)),
            list(range(7)),
        )
        self.assertEqual(report.metrics, original_metrics)
        self.assertEqual(report.updated_at, original_updated_at)

    def test_conflicting_managed_reading_fails_without_overwrite(self):
        self.run_seed()
        session = self.get_managed("MCM_BOSTON_HISTORY_CURRENT")
        reading = session.readings.get(sequence=2)
        reading.strap_load = 99
        reading.save(update_fields=["strap_load"])

        with self.assertRaises(CommandError):
            self.run_seed()

        reading.refresh_from_db()
        self.assertEqual(reading.strap_load, 99)

    def test_late_conflict_rolls_back_all_command_changes(self):
        current_b_spec = next(
            spec
            for spec in MANAGED_HISTORY_SPECS
            if spec["code"] == "MCM_BOSTON_HISTORY_CURRENT"
        )
        scenario = SimulationScenario.objects.create(
            code=current_b_spec["code"],
            name="Preexisting conflicting scenario",
            scenario_type=SimulationScenario.ScenarioType.NORMAL,
            mode=SimulationScenario.Mode.HISTORY,
            logical_duration_seconds=604800,
            sample_interval_seconds=86400,
            config=deepcopy(current_b_spec["config"]),
            version=9,
            is_active=True,
        )
        conflicting_session = MeasurementSession.objects.create(
            bag=self.bag_b,
            scenario=scenario,
            purpose=MeasurementSession.Purpose.HISTORY,
            seed=999999,
            started_at=self.aware(CURRENT_STARTED_AT),
            ended_at=self.aware(CURRENT_STARTED_AT) + timedelta(days=7),
            status=MeasurementSession.Status.COMPLETED,
        )
        legacy_session, _report = self.create_legacy_session(
            bag=self.bag_a,
            code="OVERLOAD_HISTORY",
            started_at=self.aware(datetime(2026, 8, 20)),
        )

        with self.assertRaises(CommandError):
            self.run_seed()

        scenario.refresh_from_db()
        legacy_session.refresh_from_db()
        conflicting_session.refresh_from_db()
        self.assertEqual(scenario.name, "Preexisting conflicting scenario")
        self.assertEqual(scenario.version, 9)
        self.assertIs(legacy_session.include_in_report, True)
        self.assertEqual(conflicting_session.seed, 999999)
        self.assertFalse(
            SimulationScenario.objects.filter(
                code="MCM_VELA_HISTORY_PREVIOUS"
            ).exists()
        )
        self.assertFalse(
            MeasurementSession.objects.filter(
                scenario__code="MCM_VELA_HISTORY_PREVIOUS"
            ).exists()
        )

    def test_missing_final_product_fails_without_creating_identity(self):
        self.product_b.delete()
        product_count = ProductModel.objects.count()
        bag_count = Bag.objects.count()

        with self.assertRaises(CommandError):
            self.run_seed()

        self.assertEqual(ProductModel.objects.count(), product_count)
        self.assertEqual(Bag.objects.count(), bag_count)
        self.assertFalse(
            SimulationScenario.objects.filter(
                code__in=[spec["code"] for spec in MANAGED_HISTORY_SPECS]
            ).exists()
        )
