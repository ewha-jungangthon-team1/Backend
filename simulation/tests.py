import json
from datetime import datetime, timedelta
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
from .services import DEMO_REAL_SECONDS, POLLING_INTERVAL_SECONDS


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
                "strap_load": {"min": 2.5, "max": 4.0},
                "humidity": {"min": 40, "max": 55},
                "temperature": {"min": 22, "max": 26},
                "load_bias": {"min": -0.1, "max": 0.1},
                "body_deformation_ratio": {"min": 0.005, "max": 0.012},
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
        self.assertEqual(
            set(payload),
            {
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
            },
        )
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
        self.assertIsInstance(payload["is_finished"], bool)

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
