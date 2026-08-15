from copy import deepcopy
from datetime import timedelta
from io import StringIO
from unittest.mock import patch
from uuid import UUID

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from analysis.models import AnalysisReport
from measurements.models import MeasurementSession, SensorReading
from simulation.models import SimulationScenario
from simulation.services import calculate_total_reading_count, generate_single_reading

from .management.commands.seed_demo_live_data import (
    PRODUCT_A_DISPLAY_METRICS,
    PRODUCT_A_LIVE_STATES,
    PRODUCT_A_SCENARIO_DEFAULTS,
    PRODUCT_B_CARE_ACTIONS,
    PRODUCT_B_DISPLAY_METRICS,
    PRODUCT_B_LIVE_STATES,
    PRODUCT_B_PUBLIC_TOKEN,
    PRODUCT_B_SCENARIO_DEFAULTS,
)
from .models import Bag, ProductModel


class SeedDemoLiveDataTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(
            username="demo-live-owner",
            password="test-password",
        )
        self.product_a = ProductModel.objects.create(
            brand="Central Bag Co.",
            model_name="Voyager Tote",
            material="Full-grain Leather",
            model_image="ProductModel/image/existing-a.jpg",
            demo_live_scenario_code="HOT_CAR_LIVE",
            care_guideline={
                "avoid_moisture": True,
                "max_load_kg": 5.5,
                "recommended_temp_range_c": [0, 35],
                "max_humidity_percent": 70,
                "max_abs_load_bias": 0.30,
                "max_body_deformation_ratio": 0.03,
                "care_actions": {
                    "HIGH_LOAD": {
                        "title": "기존 하중 관리",
                        "reason": "기존 이유",
                        "steps": ["기존 단계"],
                    }
                },
                "note": "기존 Product A note",
                "existing_extension": {"preserved": True},
                "live_presentation": {"existing_live_extension": True},
                "live_states": {"existing_state_extension": True},
            },
        )
        self.bag_a = Bag.objects.create(
            product_model=self.product_a,
            owner=self.owner,
            nfc_uid="NFC-DEMO-0001",
            public_token=UUID("11111111-1111-1111-1111-111111111111"),
        )
        self.hot_car_scenario = SimulationScenario.objects.create(
            code="HOT_CAR_LIVE",
            name="Outdated name",
            scenario_type=SimulationScenario.ScenarioType.NORMAL,
            mode=SimulationScenario.Mode.LIVE,
            logical_duration_seconds=86400,
            sample_interval_seconds=3600,
            config={},
            version=9,
            is_active=False,
        )
        history_scenario = SimulationScenario.objects.create(
            code="SEED_PROTECTION_HISTORY",
            name="Seed Protection History",
            scenario_type=SimulationScenario.ScenarioType.NORMAL,
            mode=SimulationScenario.Mode.HISTORY,
            logical_duration_seconds=604800,
            sample_interval_seconds=86400,
            config={},
        )
        started_at = timezone.now() - timedelta(days=7)
        self.history_session = MeasurementSession.objects.create(
            bag=self.bag_a,
            scenario=history_scenario,
            purpose=MeasurementSession.Purpose.HISTORY,
            seed=123,
            started_at=started_at,
            ended_at=started_at + timedelta(days=7),
            status=MeasurementSession.Status.COMPLETED,
        )
        self.history_reading = SensorReading.objects.create(
            session=self.history_session,
            sequence=0,
            measured_at=started_at,
            strap_load=3,
            humidity=50,
            temperature=25,
            load_bias=0.1,
            body_deformation_ratio=0.01,
            moisture_detected=False,
        )
        self.report = AnalysisReport.objects.create(
            session=self.history_session,
            metrics={"protected": True},
            severity="NORMAL",
            active_rules=[],
            unavailable_rules=[],
            care_guideline_snapshot={"snapshot": "protected"},
            comparison={"protected": True},
            ai_result={"protected": True},
        )

    def run_seed(self):
        call_command("seed_demo_live_data", stdout=StringIO())

    def test_seed_creates_final_data_idempotently_and_protects_history(self):
        protected = {
            "product_a_pk": self.product_a.pk,
            "bag_a_pk": self.bag_a.pk,
            "bag_a_token": self.bag_a.public_token,
            "hot_car_pk": self.hot_car_scenario.pk,
            "session_count": MeasurementSession.objects.count(),
            "reading_count": SensorReading.objects.count(),
            "report_count": AnalysisReport.objects.count(),
            "report": deepcopy(self.report.ai_result),
            "report_updated_at": self.report.updated_at,
        }

        self.run_seed()
        self.run_seed()

        self.product_a.refresh_from_db()
        self.bag_a.refresh_from_db()
        self.report.refresh_from_db()
        self.assertEqual(self.product_a.pk, protected["product_a_pk"])
        self.assertEqual(
            (self.product_a.brand, self.product_a.model_name, self.product_a.material),
            ("MCM", "Vela Visetos Sling Bag", "Leather"),
        )
        self.assertEqual(
            self.product_a.model_image.name,
            "ProductModel/image/existing-a.jpg",
        )
        self.assertEqual(self.bag_a.pk, protected["bag_a_pk"])
        self.assertEqual(self.bag_a.product_model_id, self.product_a.pk)
        self.assertEqual(self.bag_a.public_token, protected["bag_a_token"])
        self.assertEqual(self.bag_a.nfc_uid, "NFC-DEMO-0001")
        self.assertEqual(
            self.product_a.care_guideline["existing_extension"],
            {"preserved": True},
        )
        self.assertEqual(
            self.product_a.care_guideline["care_actions"]["HIGH_LOAD"]["title"],
            "기존 하중 관리",
        )
        self.assertEqual(
            self.product_a.care_guideline["note"],
            "기존 Product A note",
        )
        self.assertEqual(self.product_a.care_guideline["max_load_kg"], 5.5)
        self.assertEqual(
            self.product_a.care_guideline["live_presentation"]["display_metrics"],
            PRODUCT_A_DISPLAY_METRICS,
        )
        self.assertIs(
            self.product_a.care_guideline["live_presentation"][
                "existing_live_extension"
            ],
            True,
        )
        self.assertEqual(
            {
                key: self.product_a.care_guideline["live_states"][key]
                for key in ("stable", "states", "fallback_active")
            },
            PRODUCT_A_LIVE_STATES,
        )
        self.assertEqual(
            [
                state["code"]
                for state in self.product_a.care_guideline["live_states"][
                    "states"
                ]
            ],
            ["SHAPE_RISK", "HEAT_EXPOSURE"],
        )
        self.assertIs(
            self.product_a.care_guideline["live_states"][
                "existing_state_extension"
            ],
            True,
        )

        hot_car = SimulationScenario.objects.get(code="HOT_CAR_LIVE")
        self.assertEqual(hot_car.pk, protected["hot_car_pk"])
        for field, value in PRODUCT_A_SCENARIO_DEFAULTS.items():
            self.assertEqual(getattr(hot_car, field), value)

        product_b = ProductModel.objects.get(
            brand="MCM",
            model_name="Visetos Original Boston Bag",
        )
        self.assertEqual(product_b.material, "Suede")
        self.assertEqual(product_b.demo_live_scenario_code, "RAIN_MOISTURE_LIVE")
        self.assertEqual(ProductModel.objects.count(), 2)
        self.assertEqual(
            product_b.care_guideline["live_presentation"]["display_metrics"],
            PRODUCT_B_DISPLAY_METRICS,
        )
        self.assertEqual(
            product_b.care_guideline["care_actions"], PRODUCT_B_CARE_ACTIONS
        )
        self.assertEqual(
            {
                key: product_b.care_guideline["live_states"][key]
                for key in ("stable", "states", "fallback_active")
            },
            PRODUCT_B_LIVE_STATES,
        )
        self.assertEqual(
            [
                state["code"]
                for state in product_b.care_guideline["live_states"]["states"]
            ],
            ["HUMIDITY_RETENTION", "MOISTURE_CONTACT"],
        )
        self.assertEqual(product_b.care_guideline["max_humidity_percent"], 60)
        self.assertEqual(product_b.care_guideline["max_load_kg"], 5.5)
        self.assertEqual(product_b.care_guideline["max_abs_load_bias"], 0.30)
        self.assertEqual(product_b.care_guideline["max_body_deformation_ratio"], 0.03)
        self.assertEqual(product_b.care_guideline["recommended_temp_range_c"], [0, 35])
        self.assertIs(product_b.care_guideline["avoid_moisture"], True)

        bag_b = Bag.objects.get(nfc_uid="NFC-DEMO-0002")
        self.assertEqual(Bag.objects.count(), 2)
        self.assertEqual(bag_b.product_model_id, product_b.pk)
        self.assertEqual(bag_b.owner_id, self.bag_a.owner_id)
        self.assertEqual(bag_b.public_token, PRODUCT_B_PUBLIC_TOKEN)
        self.assertIsNone(bag_b.serial_number)

        rain_scenario = SimulationScenario.objects.get(code="RAIN_MOISTURE_LIVE")
        for field, value in PRODUCT_B_SCENARIO_DEFAULTS.items():
            self.assertEqual(getattr(rain_scenario, field), value)
        self.assertEqual(
            SimulationScenario.objects.filter(code="RAIN_MOISTURE_LIVE").count(),
            1,
        )

        self.assertEqual(MeasurementSession.objects.count(), protected["session_count"])
        self.assertEqual(SensorReading.objects.count(), protected["reading_count"])
        self.assertEqual(AnalysisReport.objects.count(), protected["report_count"])
        self.assertEqual(self.report.ai_result, protected["report"])
        self.assertEqual(self.report.metrics, {"protected": True})
        self.assertEqual(self.report.updated_at, protected["report_updated_at"])
        self.assertEqual(
            self.report.care_guideline_snapshot, {"snapshot": "protected"}
        )
        self.history_session.refresh_from_db()
        self.history_reading.refresh_from_db()
        self.assertEqual(self.history_session.bag_id, self.bag_a.pk)
        self.assertEqual(self.history_reading.strap_load, 3)

    def test_product_b_trajectory_has_numeric_moisture_and_initial_event(self):
        self.run_seed()
        bag_b = Bag.objects.get(nfc_uid="NFC-DEMO-0002")
        scenario = SimulationScenario.objects.get(code="RAIN_MOISTURE_LIVE")
        session = MeasurementSession.objects.create(
            bag=bag_b,
            scenario=scenario,
            purpose=MeasurementSession.Purpose.LIVE,
            seed=54321,
            started_at=timezone.now(),
            status=MeasurementSession.Status.RUNNING,
        )

        reading = generate_single_reading(
            session,
            sequence=0,
            total_count=calculate_total_reading_count(scenario),
        )

        self.assertIsNotNone(reading.material_moisture_percent)
        self.assertIs(reading.moisture_detected, True)

    def test_nfc_collision_fails_and_rolls_back_all_changes(self):
        other_product = ProductModel.objects.create(
            brand="Other",
            model_name="Other Bag",
            material="Canvas",
            care_guideline={},
        )
        Bag.objects.create(
            product_model=other_product,
            owner=self.owner,
            nfc_uid="NFC-DEMO-0002",
        )

        with self.assertRaisesMessage(CommandError, "belongs to another product"):
            self.run_seed()

        self.product_a.refresh_from_db()
        self.assertEqual(self.product_a.brand, "Central Bag Co.")
        self.assertFalse(
            SimulationScenario.objects.filter(code="RAIN_MOISTURE_LIVE").exists()
        )
        self.assertFalse(
            ProductModel.objects.filter(
                brand="MCM", model_name="Visetos Original Boston Bag"
            ).exists()
        )

    def test_public_token_collision_fails_without_overwriting_bag(self):
        other_product = ProductModel.objects.create(
            brand="Other",
            model_name="Token Owner Bag",
            material="Canvas",
            care_guideline={},
        )
        other_bag = Bag.objects.create(
            product_model=other_product,
            owner=self.owner,
            nfc_uid="OTHER-NFC",
            public_token=PRODUCT_B_PUBLIC_TOKEN,
        )

        with self.assertRaisesMessage(CommandError, "belongs to another product"):
            self.run_seed()

        other_bag.refresh_from_db()
        self.assertEqual(other_bag.nfc_uid, "OTHER-NFC")
        self.assertEqual(other_bag.product_model_id, other_product.pk)

    def test_duplicate_product_b_identity_fails_fast(self):
        for material in ("First", "Second"):
            ProductModel.objects.create(
                brand="MCM",
                model_name="Visetos Original Boston Bag",
                material=material,
                care_guideline={},
            )

        with self.assertRaisesMessage(CommandError, "Multiple Product B rows"):
            self.run_seed()

    def test_latest_reading_keeps_product_a_and_b_presentations_isolated(self):
        self.run_seed()
        bag_a = Bag.objects.get(nfc_uid="NFC-DEMO-0001")
        bag_b = Bag.objects.get(nfc_uid="NFC-DEMO-0002")
        current_time = timezone.now()

        with (
            patch("simulation.services.timezone.now", return_value=current_time),
            patch(
                "simulation.services.get_current_time",
                return_value=current_time,
            ),
        ):
            ensure_a = self.client.post(
                reverse(
                    "ensure-live-session",
                    kwargs={"public_token": bag_a.public_token},
                )
            )
            ensure_b = self.client.post(
                reverse(
                    "ensure-live-session",
                    kwargs={"public_token": bag_b.public_token},
                )
            )
            latest_a = self.client.get(
                reverse(
                    "latest-reading",
                    kwargs={"session_id": ensure_a.data["session_id"]},
                )
            )
            latest_b = self.client.get(
                reverse(
                    "latest-reading",
                    kwargs={"session_id": ensure_b.data["session_id"]},
                )
            )

        self.assertEqual(ensure_a.status_code, 200)
        self.assertEqual(ensure_b.status_code, 200)
        self.assertEqual(latest_a.status_code, 200)
        self.assertEqual(latest_b.status_code, 200)
        self.assertEqual(
            [
                metric["key"]
                for metric in latest_a.data["presentation"]["display_metrics"]
            ],
            [
                "right_load_percent",
                "shape_deviation_percent",
                "temperature_c",
            ],
        )
        self.assertIsNone(latest_a.data["material_moisture_percent"])
        self.assertEqual(
            [
                metric["key"]
                for metric in latest_b.data["presentation"]["display_metrics"]
            ],
            [
                "shape_deviation_percent",
                "material_moisture_percent",
                "internal_humidity_percent",
            ],
        )
        self.assertIsNotNone(latest_b.data["material_moisture_percent"])
        self.assertIs(latest_b.data["moisture_detected"], True)

    def test_reuses_existing_exact_product_b_without_replacing_image(self):
        product_b = ProductModel.objects.create(
            brand="MCM",
            model_name="Visetos Original Boston Bag",
            material="Old material",
            model_image="ProductModel/image/existing-b.jpg",
            care_guideline={
                "existing_extension": True,
                "live_presentation": {"existing_live_extension": True},
                "live_states": {"existing_state_extension": True},
            },
        )

        self.run_seed()

        product_b.refresh_from_db()
        self.assertEqual(
            ProductModel.objects.filter(
                brand="MCM", model_name="Visetos Original Boston Bag"
            ).count(),
            1,
        )
        self.assertEqual(product_b.material, "Suede")
        self.assertEqual(
            product_b.model_image.name,
            "ProductModel/image/existing-b.jpg",
        )
        self.assertIs(product_b.care_guideline["existing_extension"], True)
        self.assertIs(
            product_b.care_guideline["live_presentation"][
                "existing_live_extension"
            ],
            True,
        )
        self.assertIs(
            product_b.care_guideline["live_states"][
                "existing_state_extension"
            ],
            True,
        )
