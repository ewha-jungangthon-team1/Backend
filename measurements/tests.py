from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from products.models import Bag, ProductModel

from .home import (
    build_sensor_presentation_values,
    calculate_bias_magnitude_percent,
    calculate_deformation_percentage,
    calculate_internal_humidity_percent,
    calculate_left_load_percent,
    calculate_load_bias_percentage,
    calculate_load_distribution_percentages,
    calculate_material_moisture_percent,
    calculate_right_load_percent,
    calculate_shape_deviation_percent,
    calculate_temperature_c,
    calculate_total_load_kg,
)
from .models import MeasurementSession, SensorReading


class SensorPresentationHelperTests(SimpleTestCase):
    def test_load_distribution_boundaries_and_example(self):
        cases = (
            (Decimal("-1"), 100, 0),
            (Decimal("0"), 50, 50),
            (Decimal("0.36"), 32, 68),
            (Decimal("1"), 0, 100),
        )

        for load_bias, expected_left, expected_right in cases:
            with self.subTest(load_bias=load_bias):
                self.assertEqual(
                    calculate_left_load_percent(load_bias),
                    expected_left,
                )
                self.assertEqual(
                    calculate_right_load_percent(load_bias),
                    expected_right,
                )

    def test_bias_magnitude_is_separate_from_distribution(self):
        for load_bias in (Decimal("-0.68"), 0.68):
            with self.subTest(load_bias=load_bias):
                self.assertEqual(
                    calculate_bias_magnitude_percent(load_bias),
                    68,
                )

        distribution = calculate_load_distribution_percentages(Decimal("0.68"))
        self.assertEqual(distribution["left_load_percent"], 16)
        self.assertEqual(distribution["right_load_percent"], 84)

    def test_load_distribution_always_sums_to_one_hundred(self):
        for load_bias in (-1, Decimal("-0.68"), 0, 0.36, Decimal("0.3333"), 1):
            with self.subTest(load_bias=load_bias):
                distribution = calculate_load_distribution_percentages(load_bias)
                self.assertEqual(
                    distribution["left_load_percent"]
                    + distribution["right_load_percent"],
                    100,
                )

    def test_shape_deviation_preserves_two_decimal_precision(self):
        self.assertEqual(
            calculate_shape_deviation_percent(Decimal("0.0250")),
            2.5,
        )
        self.assertEqual(calculate_shape_deviation_percent(0.0684), 6.84)

    def test_direct_sensor_aliases_do_not_change_scale(self):
        self.assertEqual(calculate_total_load_kg(Decimal("3.25")), 3.25)
        self.assertEqual(calculate_internal_humidity_percent(58), 58)
        self.assertEqual(calculate_temperature_c(33.5), 33.5)

    def test_optional_material_moisture_preserves_null_and_zero(self):
        self.assertIsNone(calculate_material_moisture_percent(None))
        self.assertEqual(calculate_material_moisture_percent(0), 0)
        self.assertEqual(
            calculate_material_moisture_percent(Decimal("42.50")),
            42.5,
        )

    def test_builder_returns_only_common_numeric_presentation_values(self):
        result = build_sensor_presentation_values(
            strap_load=Decimal("3.25"),
            load_bias=0.36,
            body_deformation_ratio=Decimal("0.0684"),
            temperature=33.5,
            humidity=Decimal("58.00"),
            material_moisture_percent=Decimal("42.50"),
        )

        self.assertEqual(
            result,
            {
                "total_load_kg": 3.25,
                "bias_magnitude_percent": 36.0,
                "left_load_percent": 32.0,
                "right_load_percent": 68.0,
                "shape_deviation_percent": 6.84,
                "temperature_c": 33.5,
                "internal_humidity_percent": 58.0,
                "material_moisture_percent": 42.5,
            },
        )

    def test_legacy_home_percentage_helpers_keep_existing_integer_behavior(self):
        self.assertEqual(calculate_load_bias_percentage(Decimal("0.684")), 68)
        self.assertEqual(calculate_deformation_percentage(Decimal("0.0684")), 7)


class SensorReadingMaterialMoistureTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        owner = get_user_model().objects.create_user(
            username="material-moisture-owner",
            password="test-password",
        )
        product_model = ProductModel.objects.create(
            brand="Test Brand",
            model_name="Material Moisture Bag",
            material="Leather",
            care_guideline={},
        )
        bag = Bag.objects.create(
            product_model=product_model,
            owner=owner,
            nfc_uid="MATERIAL-MOISTURE-NFC",
        )
        cls.session = MeasurementSession.objects.create(
            bag=bag,
            purpose=MeasurementSession.Purpose.HISTORY,
            seed=12345,
            started_at=timezone.now(),
            ended_at=timezone.now(),
            status=MeasurementSession.Status.COMPLETED,
        )

    def build_reading(self, material_moisture_percent):
        return SensorReading(
            session=self.session,
            strap_load=Decimal("4.00"),
            humidity=Decimal("50.00"),
            material_moisture_percent=material_moisture_percent,
            moisture_detected=False,
            temperature=Decimal("25.00"),
            measured_at=timezone.now(),
            load_bias=Decimal("0.0000"),
            body_deformation_ratio=Decimal("0.0100"),
            sequence=0,
        )

    def test_measurement_session_include_in_report_defaults_true(self):
        self.session.refresh_from_db()

        self.assertIs(self.session.include_in_report, True)

    def test_stores_numeric_material_moisture_percentage(self):
        reading = self.build_reading(Decimal("42.50"))
        reading.full_clean()
        reading.save()

        reading.refresh_from_db()
        self.assertEqual(reading.material_moisture_percent, Decimal("42.50"))

    def test_zero_is_a_valid_measured_value(self):
        reading = self.build_reading(Decimal("0.00"))
        reading.full_clean()
        reading.save()

        reading.refresh_from_db()
        self.assertEqual(reading.material_moisture_percent, Decimal("0.00"))

    def test_null_is_allowed(self):
        reading = self.build_reading(None)
        reading.full_clean()
        reading.save()

        reading.refresh_from_db()
        self.assertIsNone(reading.material_moisture_percent)

    def test_rejects_value_below_zero_during_validation(self):
        reading = self.build_reading(Decimal("-0.01"))

        with self.assertRaises(ValidationError):
            reading.full_clean()

    def test_rejects_value_above_one_hundred_during_validation(self):
        reading = self.build_reading(Decimal("100.01"))

        with self.assertRaises(ValidationError):
            reading.full_clean()
