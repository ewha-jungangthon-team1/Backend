from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from products.models import Bag, ProductModel

from .models import MeasurementSession, SensorReading


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
