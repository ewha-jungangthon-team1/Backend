import json
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.urls import reverse
from rest_framework.test import APITestCase

from measurements.models import MeasurementSession
from products.models import Bag, ProductModel

from .models import SimulationScenario
from .services import (
    DEMO_REAL_SECONDS,
    POLLING_INTERVAL_SECONDS,
    generate_single_reading,
    get_latest_reading,
)


class LiveResponseContractTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        owner = get_user_model().objects.create_user(
            username="live-response-owner",
            password="test-password",
        )
        product_model = ProductModel.objects.create(
            brand="Test Brand",
            model_name="Test Live Bag",
            material="Leather",
            care_guideline={},
            demo_live_scenario_code="NORMAL_LIVE",
        )
        cls.bag = Bag.objects.create(
            product_model=product_model,
            owner=owner,
            nfc_uid="LIVE-RESPONSE-NFC",
        )
        SimulationScenario.objects.create(
            code="NORMAL_LIVE",
            name="Normal Live",
            scenario_type=SimulationScenario.ScenarioType.NORMAL,
            mode=SimulationScenario.Mode.LIVE,
            logical_duration_seconds=86400,
            sample_interval_seconds=3600,
            config={
                "strap_load": {"min": 3.25, "max": 3.25},
                "humidity": {"min": 58, "max": 58},
                "temperature": {"min": 33.5, "max": 33.5},
                "load_bias": {"min": 0.36, "max": 0.36},
                "body_deformation_ratio": {"min": 0.025, "max": 0.025},
                "moisture_event": {"enabled": False},
            },
        )
        cls.started_at = datetime(
            2026,
            8,
            14,
            12,
            tzinfo=ZoneInfo("Asia/Seoul"),
        )

    def setUp(self):
        self.current_time = self.started_at
        progress_clock_patch = patch(
            "simulation.services.timezone.now",
            side_effect=lambda: self.current_time,
        )
        observation_clock_patch = patch(
            "simulation.services.get_current_time",
            side_effect=lambda: self.current_time,
        )
        self.mock_progress_clock = progress_clock_patch.start()
        self.mock_observation_clock = observation_clock_patch.start()
        self.addCleanup(progress_clock_patch.stop)
        self.addCleanup(observation_clock_patch.stop)

    def ensure_live_session(self):
        response = self.client.post(
            reverse(
                "ensure-live-session",
                kwargs={"public_token": self.bag.public_token},
            )
        )
        self.assertEqual(response.status_code, 200)
        return response

    def get_latest_reading(self, session_id):
        return self.client.get(
            reverse(
                "latest-reading",
                kwargs={"session_id": session_id},
            )
        )

    def json_payload(self, response):
        return json.loads(response.content.decode("utf-8"))

    def test_ensure_and_latest_reading_return_complete_json_contract(self):
        ensure_response = self.ensure_live_session()

        self.assertTrue(ensure_response.data["created"])
        self.assertEqual(
            ensure_response.data["polling_interval_seconds"],
            POLLING_INTERVAL_SECONDS,
        )

        response = self.get_latest_reading(ensure_response.data["session_id"])
        payload = self.json_payload(response)

        self.assertEqual(response.status_code, 200)
        raw_fields = {
            "session_id",
            "sequence",
            "measured_at",
            "observed_at",
            "scenario_type",
            "progress_ratio",
            "is_finished",
            "moisture_detected",
            "strap_load",
            "humidity",
            "temperature",
            "load_bias",
            "body_deformation_ratio",
            "material_moisture_percent",
        }
        self.assertTrue(raw_fields.issubset(payload))
        self.assertEqual(set(payload), raw_fields | {"presentation"})
        self.assertNotIn("polling_interval_seconds", payload)
        self.assertNotIn("strap_strain", payload)
        for field_name in (
            "strap_load",
            "humidity",
            "temperature",
            "load_bias",
            "body_deformation_ratio",
            "progress_ratio",
        ):
            self.assertIsInstance(payload[field_name], (int, float))
            self.assertNotIsInstance(payload[field_name], bool)
        self.assertIsInstance(payload["moisture_detected"], bool)
        self.assertIsNone(payload["material_moisture_percent"])
        self.assertIsInstance(payload["is_finished"], bool)

        self.assertEqual(set(payload["presentation"]), {"values"})
        presentation_values = payload["presentation"]["values"]
        self.assertEqual(
            set(presentation_values),
            {
                "total_load_kg",
                "bias_magnitude_percent",
                "left_load_percent",
                "right_load_percent",
                "shape_deviation_percent",
                "temperature_c",
                "internal_humidity_percent",
                "material_moisture_percent",
            },
        )
        self.assertEqual(payload["load_bias"], 0.36)
        self.assertEqual(presentation_values["bias_magnitude_percent"], 36)
        self.assertEqual(presentation_values["left_load_percent"], 32)
        self.assertEqual(presentation_values["right_load_percent"], 68)
        self.assertEqual(presentation_values["shape_deviation_percent"], 2.5)
        self.assertEqual(presentation_values["internal_humidity_percent"], 58)
        self.assertEqual(presentation_values["temperature_c"], 33.5)
        self.assertIsNone(presentation_values["material_moisture_percent"])

        reading = MeasurementSession.objects.get(
            pk=payload["session_id"]
        ).readings.get(sequence=payload["sequence"])
        measured_at = parse_datetime(payload["measured_at"])
        observed_at = parse_datetime(payload["observed_at"])
        self.assertTrue(timezone.is_aware(measured_at))
        self.assertTrue(timezone.is_aware(observed_at))
        self.assertEqual(measured_at, reading.measured_at)
        self.assertEqual(observed_at, self.current_time)
        self.mock_observation_clock.assert_called_once_with()

    def test_same_sequence_keeps_measurement_time_without_duplicate_reading(self):
        session_id = self.ensure_live_session().data["session_id"]

        first = self.json_payload(self.get_latest_reading(session_id))
        first_count = MeasurementSession.objects.get(pk=session_id).readings.count()
        self.current_time = self.started_at + timedelta(seconds=2)
        second = self.json_payload(self.get_latest_reading(session_id))
        second_count = MeasurementSession.objects.get(pk=session_id).readings.count()

        self.assertEqual(first["sequence"], 0)
        self.assertEqual(second["sequence"], first["sequence"])
        self.assertEqual(second["measured_at"], first["measured_at"])
        self.assertNotEqual(second["observed_at"], first["observed_at"])
        self.assertGreater(second["progress_ratio"], first["progress_ratio"])
        self.assertEqual(first_count, 1)
        self.assertEqual(second_count, first_count)

    def test_new_sequence_returns_its_stored_measurement_time(self):
        session_id = self.ensure_live_session().data["session_id"]
        first = self.json_payload(self.get_latest_reading(session_id))

        self.current_time = self.started_at + timedelta(seconds=8)
        second = self.json_payload(self.get_latest_reading(session_id))

        self.assertEqual(first["sequence"], 0)
        self.assertEqual(second["sequence"], 1)
        self.assertEqual(
            MeasurementSession.objects.get(pk=session_id).readings.count(),
            2,
        )
        second_reading = MeasurementSession.objects.get(
            pk=session_id
        ).readings.get(sequence=1)
        self.assertEqual(
            parse_datetime(second["measured_at"]),
            second_reading.measured_at,
        )
        self.assertNotEqual(second["measured_at"], first["measured_at"])

    def test_progress_and_completion_contract_remain_unchanged(self):
        session_id = self.ensure_live_session().data["session_id"]

        initial = self.json_payload(self.get_latest_reading(session_id))
        self.current_time = self.started_at + timedelta(
            seconds=DEMO_REAL_SECONDS / 2
        )
        middle = self.json_payload(self.get_latest_reading(session_id))
        self.current_time = self.started_at + timedelta(
            seconds=DEMO_REAL_SECONDS
        )
        finished = self.json_payload(self.get_latest_reading(session_id))

        self.assertEqual(initial["progress_ratio"], 0.0)
        self.assertEqual(middle["progress_ratio"], 0.5)
        self.assertGreaterEqual(middle["progress_ratio"], 0)
        self.assertLessEqual(middle["progress_ratio"], 1)
        self.assertEqual(finished["sequence"], 23)
        self.assertEqual(finished["progress_ratio"], 1.0)
        self.assertIs(finished["is_finished"], True)
        self.assertIsNotNone(finished["measured_at"])
        self.assertIsNotNone(finished["observed_at"])

        session = MeasurementSession.objects.get(pk=session_id)
        self.assertEqual(session.status, MeasurementSession.Status.COMPLETED)
        self.assertIsNotNone(session.ended_at)
        self.assertEqual(session.readings.count(), 24)
        final_measured_at = finished["measured_at"]
        final_ended_at = session.ended_at

        self.current_time += timedelta(seconds=2)
        repeated = self.json_payload(self.get_latest_reading(session_id))
        session.refresh_from_db()

        self.assertEqual(repeated["sequence"], 23)
        self.assertEqual(repeated["progress_ratio"], 1.0)
        self.assertIs(repeated["is_finished"], True)
        self.assertEqual(repeated["measured_at"], final_measured_at)
        self.assertNotEqual(repeated["observed_at"], finished["observed_at"])
        self.assertEqual(session.status, MeasurementSession.Status.COMPLETED)
        self.assertEqual(session.ended_at, final_ended_at)
        self.assertEqual(session.readings.count(), 24)

    def test_latest_reading_returns_404_for_missing_session(self):
        response = self.get_latest_reading(999999)

        self.assertEqual(response.status_code, 404)
        self.assertIn("detail", response.data)


class ProductSpecificLiveScenarioTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(
            username="product-live-owner",
            password="test-password",
        )
        cls.hot_car_scenario = cls.create_scenario("HOT_CAR_LIVE")
        cls.alternate_scenario = cls.create_scenario("ALTERNATE_PRODUCT_LIVE")
        cls.inactive_scenario = cls.create_scenario(
            "INACTIVE_PRODUCT_LIVE",
            is_active=False,
        )
        cls.history_scenario = cls.create_scenario(
            "PRODUCT_HISTORY",
            mode=SimulationScenario.Mode.HISTORY,
        )
        cls.product_a, cls.bag_a = cls.create_product_and_bag(
            "A",
            cls.hot_car_scenario.code,
        )
        cls.product_b, cls.bag_b = cls.create_product_and_bag(
            "B",
            cls.alternate_scenario.code,
        )
        cls.started_at = datetime(
            2026,
            8,
            14,
            12,
            tzinfo=ZoneInfo("Asia/Seoul"),
        )

    @classmethod
    def create_scenario(cls, code, *, mode=SimulationScenario.Mode.LIVE, is_active=True):
        return SimulationScenario.objects.create(
            code=code,
            name=code,
            scenario_type=SimulationScenario.ScenarioType.HIGH_TEMPERATURE,
            mode=mode,
            logical_duration_seconds=86400,
            sample_interval_seconds=3600,
            config={
                "strap_load": {"min": 2.5, "max": 4.0},
                "humidity": {"min": 40, "max": 55},
                "temperature": {"start": 28, "end": 42},
                "load_bias": {"start": 0.1, "end": 0.6},
                "body_deformation_ratio": {"start": 0.005, "end": 0.06},
                "moisture_event": {"enabled": False},
            },
            is_active=is_active,
        )

    @classmethod
    def create_product_and_bag(cls, suffix, scenario_code):
        product = ProductModel.objects.create(
            brand=f"Synthetic Brand {suffix}",
            model_name=f"Synthetic Model {suffix}",
            material="Leather",
            care_guideline={},
            demo_live_scenario_code=scenario_code,
        )
        bag = Bag.objects.create(
            product_model=product,
            owner=cls.owner,
            nfc_uid=f"PRODUCT-LIVE-NFC-{suffix}",
        )
        return product, bag

    def setUp(self):
        clock_patch = patch(
            "simulation.services.timezone.now",
            return_value=self.started_at,
        )
        self.mock_clock = clock_patch.start()
        self.addCleanup(clock_patch.stop)

    def ensure(self, bag):
        return self.client.post(
            reverse(
                "ensure-live-session",
                kwargs={"public_token": bag.public_token},
            )
        )

    def assert_bad_mapping(self, bag):
        response = self.ensure(bag)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.data), {"detail"})
        self.assertIsInstance(response.data["detail"], str)
        self.assertTrue(response.data["detail"])

    def test_configured_hot_car_scenario_is_created_with_existing_response_schema(self):
        response = self.ensure(self.bag_a)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(response.data),
            {
                "session_id",
                "status",
                "created",
                "polling_interval_seconds",
                "started_at",
                "scheduled_end_at",
            },
        )
        self.assertIs(response.data["created"], True)
        session = MeasurementSession.objects.get(pk=response.data["session_id"])
        self.assertEqual(session.scenario, self.hot_car_scenario)
        self.assertEqual(session.bag, self.bag_a)

    def test_two_synthetic_products_keep_independent_running_scenarios(self):
        response_a = self.ensure(self.bag_a)
        response_b = self.ensure(self.bag_b)

        self.assertEqual(response_a.status_code, 200)
        self.assertEqual(response_b.status_code, 200)
        session_a = MeasurementSession.objects.get(pk=response_a.data["session_id"])
        session_b = MeasurementSession.objects.get(pk=response_b.data["session_id"])
        self.assertNotEqual(session_a.pk, session_b.pk)
        self.assertEqual(session_a.scenario, self.hot_car_scenario)
        self.assertEqual(session_b.scenario, self.alternate_scenario)
        self.assertEqual(session_a.status, MeasurementSession.Status.RUNNING)
        self.assertEqual(session_b.status, MeasurementSession.Status.RUNNING)

    def test_existing_running_session_is_reused_before_mapping_resolution(self):
        first = self.ensure(self.bag_a)
        self.product_a.demo_live_scenario_code = "UNKNOWN_AFTER_SESSION_STARTED"
        self.product_a.save(update_fields=["demo_live_scenario_code"])

        second = self.ensure(self.bag_a)

        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.data["session_id"], first.data["session_id"])
        self.assertIs(second.data["created"], False)
        self.assertEqual(
            MeasurementSession.objects.filter(bag=self.bag_a).count(),
            1,
        )

    def test_none_and_blank_mappings_return_controlled_400(self):
        for suffix, mapping in (("NONE", None), ("BLANK", "   ")):
            with self.subTest(mapping=mapping):
                _product, bag = self.create_product_and_bag(suffix, mapping)
                self.assert_bad_mapping(bag)

    def test_unknown_mapping_returns_controlled_400(self):
        _product, bag = self.create_product_and_bag("UNKNOWN", "UNKNOWN_LIVE")
        self.assert_bad_mapping(bag)

    def test_inactive_live_mapping_returns_controlled_400(self):
        _product, bag = self.create_product_and_bag(
            "INACTIVE",
            self.inactive_scenario.code,
        )
        self.assert_bad_mapping(bag)

    def test_history_mapping_returns_controlled_400(self):
        _product, bag = self.create_product_and_bag(
            "HISTORY",
            self.history_scenario.code,
        )
        self.assert_bad_mapping(bag)

    def test_selection_does_not_depend_on_product_identity_or_bag_token(self):
        self.product_a.brand = "Renamed Brand"
        self.product_a.model_name = "Renamed Model"
        self.product_a.save(update_fields=["brand", "model_name"])

        response = self.ensure(self.bag_a)

        self.assertEqual(response.status_code, 200)
        session = MeasurementSession.objects.get(pk=response.data["session_id"])
        self.assertEqual(session.scenario.code, "HOT_CAR_LIVE")


class MaterialMoistureGenerationTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        owner = get_user_model().objects.create_user(
            username="material-moisture-generation-owner",
            password="test-password",
        )
        product_model = ProductModel.objects.create(
            brand="Test Brand",
            model_name="Material Moisture Generation Bag",
            material="Leather",
            care_guideline={},
        )
        cls.bag = Bag.objects.create(
            product_model=product_model,
            owner=owner,
            nfc_uid="MATERIAL-MOISTURE-GENERATION-NFC",
        )
        cls.started_at = datetime(
            2026,
            8,
            14,
            12,
            tzinfo=ZoneInfo("Asia/Seoul"),
        )

    def create_session(self, suffix, *, material_moisture_config=None):
        config = {
            "strap_load": {"min": 2.5, "max": 4.0},
            "humidity": {"min": 40, "max": 55},
            "temperature": {"min": 22, "max": 26},
            "load_bias": {"min": -0.1, "max": 0.1},
            "body_deformation_ratio": {"min": 0.005, "max": 0.012},
            "moisture_event": {"enabled": True, "trigger_at_ratio": 0.5},
        }
        if material_moisture_config is not None:
            config["material_moisture_percent"] = material_moisture_config

        scenario = SimulationScenario.objects.create(
            code=f"MATERIAL_MOISTURE_{suffix}",
            name=f"Material Moisture {suffix}",
            scenario_type=SimulationScenario.ScenarioType.HIGH_HUMIDITY,
            mode=SimulationScenario.Mode.LIVE,
            logical_duration_seconds=20,
            sample_interval_seconds=10,
            config=config,
        )
        return MeasurementSession.objects.create(
            bag=self.bag,
            scenario=scenario,
            purpose=MeasurementSession.Purpose.LIVE,
            seed=12345,
            started_at=self.started_at,
            status=MeasurementSession.Status.RUNNING,
        )

    def get_interpolated_reading(self, session, latest, local_ratio=0.5):
        with (
            patch(
                "simulation.services.calculate_progress",
                return_value=(latest.sequence, 2, local_ratio),
            ),
            patch(
                "simulation.services.ensure_readings_up_to_now",
                return_value=latest,
            ),
            patch(
                "simulation.services.calculate_overall_progress_ratio",
                return_value=0.5,
            ),
        ):
            return get_latest_reading(session)

    def test_generates_numeric_material_moisture_from_existing_config_format(self):
        session = self.create_session(
            "CONFIGURED",
            material_moisture_config={"min": 42, "max": 42},
        )

        reading = generate_single_reading(session, sequence=0, total_count=2)
        reading.refresh_from_db()

        self.assertEqual(
            reading.material_moisture_percent,
            Decimal("42.00"),
        )

    def test_missing_config_stores_null_and_preserves_moisture_event(self):
        session = self.create_session("UNSUPPORTED")

        reading = generate_single_reading(session, sequence=1, total_count=2)
        reading.refresh_from_db()

        self.assertIsNone(reading.material_moisture_percent)
        self.assertIs(reading.moisture_detected, True)

    def test_interpolates_numeric_material_moisture(self):
        session = self.create_session(
            "INTERPOLATED",
            material_moisture_config={"start": 20, "end": 60},
        )
        previous = generate_single_reading(session, sequence=0, total_count=2)
        latest = generate_single_reading(session, sequence=1, total_count=2)

        result = self.get_interpolated_reading(session, latest)

        expected = round(
            (
                float(previous.material_moisture_percent)
                + float(latest.material_moisture_percent)
            )
            / 2,
            2,
        )
        self.assertEqual(result["material_moisture_percent"], expected)

    def test_unsupported_interpolation_keeps_null(self):
        session = self.create_session("NULL_INTERPOLATION")
        generate_single_reading(session, sequence=0, total_count=2)
        latest = generate_single_reading(session, sequence=1, total_count=2)

        result = self.get_interpolated_reading(session, latest)

        self.assertIsNone(result["material_moisture_percent"])

    def test_latest_reading_api_presents_numeric_material_moisture(self):
        session = self.create_session(
            "API_NUMERIC",
            material_moisture_config={"min": 42.5, "max": 42.5},
        )

        with (
            patch(
                "simulation.services.timezone.now",
                return_value=self.started_at,
            ),
            patch(
                "simulation.services.get_current_time",
                return_value=self.started_at,
            ),
        ):
            response = self.client.get(
                reverse(
                    "latest-reading",
                    kwargs={"session_id": session.id},
                )
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["material_moisture_percent"], 42.5)
        self.assertEqual(
            response.data["presentation"]["values"][
                "material_moisture_percent"
            ],
            42.5,
        )
