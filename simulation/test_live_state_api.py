from copy import deepcopy
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from measurements.models import MeasurementSession, SensorReading
from products.management.commands.seed_demo_live_data import (
    PRODUCT_A_DISPLAY_METRICS,
    PRODUCT_A_LIVE_STATES,
    PRODUCT_B_DISPLAY_METRICS,
    PRODUCT_B_LIVE_STATES,
)
from products.models import Bag, ProductModel

from .models import SimulationScenario


def build_guideline(display_metrics, live_states, max_humidity):
    return {
        "avoid_moisture": True,
        "max_load_kg": 5.5,
        "recommended_temp_range_c": [0, 35],
        "max_humidity_percent": max_humidity,
        "max_abs_load_bias": 0.30,
        "max_body_deformation_ratio": 0.03,
        "live_presentation": {"display_metrics": deepcopy(display_metrics)},
        "live_states": deepcopy(live_states),
    }


@override_settings(USE_TZ=True)
class LatestReadingStateIntegrationTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        owner = get_user_model().objects.create_user(
            username="live-state-api-owner",
            password="test-password",
        )
        cls.product_a = ProductModel.objects.create(
            brand="Arbitrary Brand A",
            model_name="Arbitrary Model A",
            material="Leather",
            care_guideline=build_guideline(
                PRODUCT_A_DISPLAY_METRICS,
                PRODUCT_A_LIVE_STATES,
                70,
            ),
        )
        cls.product_b = ProductModel.objects.create(
            brand="Arbitrary Brand B",
            model_name="Arbitrary Model B",
            material="Suede",
            care_guideline=build_guideline(
                PRODUCT_B_DISPLAY_METRICS,
                PRODUCT_B_LIVE_STATES,
                60,
            ),
        )
        bag_a = Bag.objects.create(
            product_model=cls.product_a,
            owner=owner,
            nfc_uid="LIVE-STATE-API-A",
        )
        bag_b = Bag.objects.create(
            product_model=cls.product_b,
            owner=owner,
            nfc_uid="LIVE-STATE-API-B",
        )
        scenario_a = SimulationScenario.objects.create(
            code="LIVE_STATE_API_A",
            name="Live State API A",
            scenario_type=SimulationScenario.ScenarioType.HIGH_TEMPERATURE,
            mode=SimulationScenario.Mode.LIVE,
            logical_duration_seconds=86400,
            sample_interval_seconds=3600,
            config={},
        )
        scenario_b = SimulationScenario.objects.create(
            code="LIVE_STATE_API_B",
            name="Live State API B",
            scenario_type=SimulationScenario.ScenarioType.HIGH_HUMIDITY,
            mode=SimulationScenario.Mode.LIVE,
            logical_duration_seconds=86400,
            sample_interval_seconds=3600,
            config={},
        )
        cls.started_at = timezone.now()
        cls.session_a = MeasurementSession.objects.create(
            bag=bag_a,
            scenario=scenario_a,
            purpose=MeasurementSession.Purpose.LIVE,
            seed=100,
            started_at=cls.started_at,
            status=MeasurementSession.Status.RUNNING,
        )
        cls.session_b = MeasurementSession.objects.create(
            bag=bag_b,
            scenario=scenario_b,
            purpose=MeasurementSession.Purpose.LIVE,
            seed=200,
            started_at=cls.started_at,
            status=MeasurementSession.Status.RUNNING,
        )
        SensorReading.objects.create(
            session=cls.session_b,
            sequence=0,
            measured_at=cls.started_at,
            strap_load=3,
            humidity=50,
            temperature=25,
            load_bias=0.1,
            body_deformation_ratio=0.01,
            material_moisture_percent=58,
            moisture_detected=True,
        )

    def current_reading(self, session, **overrides):
        reading = {
            "session_id": session.pk,
            "sequence": 1,
            "measured_at": self.started_at,
            "observed_at": self.started_at,
            "scenario_type": session.scenario.scenario_type,
            "progress_ratio": 0.5,
            "is_finished": False,
            "moisture_detected": False,
            "strap_load": 3.0,
            "humidity": 50.0,
            "temperature": 25.0,
            "load_bias": 0.1,
            "body_deformation_ratio": 0.01,
            "material_moisture_percent": None,
        }
        reading.update(overrides)
        return reading

    def get_latest(self, session, reading):
        with patch("simulation.views.get_latest_reading", return_value=reading):
            return self.client.get(
                reverse("latest-reading", kwargs={"session_id": session.pk})
            )

    def test_product_configs_drive_distinct_state_and_preserve_existing_contract(self):
        response_a = self.get_latest(
            self.session_a,
            self.current_reading(
                self.session_a,
                temperature=36.0,
                load_bias=0.31,
                body_deformation_ratio=0.031,
            ),
        )
        response_b = self.get_latest(
            self.session_b,
            self.current_reading(
                self.session_b,
                humidity=60.1,
                material_moisture_percent=50.0,
            ),
        )

        self.assertEqual(response_a.status_code, 200)
        self.assertEqual(response_b.status_code, 200)
        self.assertEqual(response_a.data["temperature"], 36.0)
        self.assertEqual(response_a.data["load_bias"], 0.31)
        self.assertEqual(
            response_a.data["presentation"]["values"]["right_load_percent"],
            65.5,
        )
        self.assertEqual(
            [
                metric["key"]
                for metric in response_a.data["presentation"]["display_metrics"]
            ],
            [
                "right_load_percent",
                "shape_deviation_percent",
                "temperature_c",
            ],
        )
        self.assertEqual(
            response_a.data["presentation"]["state"]["code"],
            "SHAPE_RISK",
        )
        self.assertEqual(
            response_a.data["presentation"]["state"]["primary_rule"],
            "DEFORMATION",
        )

        self.assertEqual(
            [
                metric["key"]
                for metric in response_b.data["presentation"]["display_metrics"]
            ],
            [
                "shape_deviation_percent",
                "material_moisture_percent",
                "internal_humidity_percent",
            ],
        )
        self.assertEqual(
            response_b.data["presentation"]["state"]["code"],
            "HUMIDITY_RETENTION",
        )
        self.assertEqual(
            response_b.data["presentation"]["state"]["headline"],
            PRODUCT_B_LIVE_STATES["states"][0]["headline"],
        )

    def test_api_state_keeps_prior_moisture_latched_when_current_boolean_is_false(self):
        current = self.current_reading(
            self.session_b,
            humidity=55.0,
            moisture_detected=False,
            material_moisture_percent=52.0,
        )

        response = self.get_latest(self.session_b, current)

        self.assertEqual(response.status_code, 200)
        state = response.data["presentation"]["state"]
        self.assertEqual(state["code"], "MOISTURE_CONTACT")
        self.assertEqual(state["active_rules"], ["MOISTURE"])
        self.assertIs(response.data["moisture_detected"], False)

    def test_missing_and_invalid_state_config_return_null_copy_without_500(self):
        current = self.current_reading(self.session_a, temperature=36.0)
        guideline = deepcopy(self.product_a.care_guideline)
        guideline.pop("live_states")
        self.product_a.care_guideline = guideline
        self.product_a.save(update_fields=["care_guideline"])

        missing_response = self.get_latest(self.session_a, current)

        self.assertEqual(missing_response.status_code, 200)
        self.assertIsNone(missing_response.data["presentation"]["state"]["code"])
        self.assertEqual(
            missing_response.data["presentation"]["state"]["active_rules"],
            ["HIGH_TEMPERATURE"],
        )

        guideline["live_states"] = {
            "states": ["invalid"],
            "fallback_active": "invalid",
        }
        self.product_a.care_guideline = guideline
        self.product_a.save(update_fields=["care_guideline"])

        invalid_response = self.get_latest(self.session_a, current)

        self.assertEqual(invalid_response.status_code, 200)
        self.assertIsNone(invalid_response.data["presentation"]["state"]["code"])

    def test_state_integration_uses_joined_product_and_one_moisture_query(self):
        current = self.current_reading(self.session_b, humidity=60.1)

        with patch("simulation.views.get_latest_reading", return_value=current):
            with self.assertNumQueries(2):
                response = self.client.get(
                    reverse(
                        "latest-reading",
                        kwargs={"session_id": self.session_b.pk},
                    )
                )

        self.assertEqual(response.status_code, 200)
