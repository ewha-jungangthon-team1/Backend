import random
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from analysis.models import AnalysisReport
from analysis.services import analyze_history_session
from measurements.models import MeasurementSession
from products.models import Bag, ProductModel
from simulation.models import SimulationScenario
from simulation.services import (
    FIELD_MAX_CLAMP,
    FIELD_MIN_CLAMP,
    FIELD_NAMES,
    FIELD_PRECISION,
    OPTIONAL_FIELD_NAMES,
    calculate_total_reading_count,
    compute_field_value,
    create_simulation_session,
    generate_single_reading,
    is_moisture_trigger,
)


PRODUCT_A_IDENTITY = ("MCM", "Vela Visetos Sling Bag")
PRODUCT_A_NFC_UID = "NFC-DEMO-0001"
PRODUCT_B_IDENTITY = ("MCM", "Visetos Original Boston Bag")
PRODUCT_B_NFC_UID = "NFC-DEMO-0002"

PREVIOUS_STARTED_AT = datetime(2026, 8, 2, 0, 0)
CURRENT_STARTED_AT = datetime(2026, 8, 9, 0, 0)
PERIOD_DURATION = timedelta(days=7)

LEGACY_PRODUCT_A_SCENARIO_CODES = {
    "OVERLOAD_HISTORY",
    "HIGH_TEMPERATURE_HISTORY",
    "HIGH_HUMIDITY_HISTORY",
    "COMPOSITE_RISK_HISTORY",
}

MANAGED_HISTORY_SPECS = (
    {
        "product_key": "A",
        "period_key": "previous",
        "code": "MCM_VELA_HISTORY_PREVIOUS",
        "name": "MCM Vela History Previous",
        "seed": 42001,
        "started_at": PREVIOUS_STARTED_AT,
        "config": {
            "strap_load": {"start": 2.6, "end": 3.2},
            "temperature": {"start": 21, "end": 25},
            "humidity": {"start": 42, "end": 48},
            "load_bias": {"start": -0.04, "end": 0.08},
            "body_deformation_ratio": {"start": 0.003, "end": 0.007},
            "moisture_event": {"enabled": False},
        },
    },
    {
        "product_key": "A",
        "period_key": "current",
        "code": "MCM_VELA_HISTORY_CURRENT",
        "name": "MCM Vela History Current",
        "seed": 42002,
        "started_at": CURRENT_STARTED_AT,
        "config": {
            "strap_load": {"start": 2.8, "end": 3.6},
            "temperature": {"start": 22, "end": 27},
            "humidity": {"start": 44, "end": 52},
            "load_bias": {"start": 0.02, "end": 0.14},
            "body_deformation_ratio": {"start": 0.004, "end": 0.011},
            "moisture_event": {"enabled": False},
        },
    },
    {
        "product_key": "B",
        "period_key": "previous",
        "code": "MCM_BOSTON_HISTORY_PREVIOUS",
        "name": "MCM Boston History Previous",
        "seed": 43001,
        "started_at": PREVIOUS_STARTED_AT,
        "config": {
            "strap_load": {"start": 2.2, "end": 2.8},
            "temperature": {"start": 21, "end": 25},
            "humidity": {"start": 42, "end": 47},
            "load_bias": {"start": -0.03, "end": 0.06},
            "body_deformation_ratio": {"start": 0.003, "end": 0.008},
            "material_moisture_percent": {"start": 16, "end": 18},
            "moisture_event": {"enabled": False},
        },
    },
    {
        "product_key": "B",
        "period_key": "current",
        "code": "MCM_BOSTON_HISTORY_CURRENT",
        "name": "MCM Boston History Current",
        "seed": 43002,
        "started_at": CURRENT_STARTED_AT,
        "config": {
            "strap_load": {"start": 2.4, "end": 3.1},
            "temperature": {"start": 22, "end": 26},
            "humidity": {"start": 44, "end": 54},
            "load_bias": {"start": 0.01, "end": 0.10},
            "body_deformation_ratio": {"start": 0.004, "end": 0.010},
            "material_moisture_percent": {"start": 20, "end": 17},
            "moisture_event": {"enabled": False},
        },
    },
)


def _scenario_defaults(spec):
    return {
        "name": spec["name"],
        "scenario_type": SimulationScenario.ScenarioType.NORMAL,
        "mode": SimulationScenario.Mode.HISTORY,
        "logical_duration_seconds": 604800,
        "sample_interval_seconds": 86400,
        "config": deepcopy(spec["config"]),
        "version": 1,
        "is_active": True,
    }


class Command(BaseCommand):
    help = "Seed deterministic Product A/B final HISTORY demo data."

    @transaction.atomic
    def handle(self, *args, **options):
        products = {
            "A": self._resolve_product(PRODUCT_A_IDENTITY, "Product A"),
            "B": self._resolve_product(PRODUCT_B_IDENTITY, "Product B"),
        }
        bags = {
            "A": self._resolve_bag(PRODUCT_A_NFC_UID, products["A"], "Product A"),
            "B": self._resolve_bag(PRODUCT_B_NFC_UID, products["B"], "Product B"),
        }

        scenarios = {}
        for spec in MANAGED_HISTORY_SPECS:
            scenario, _created = SimulationScenario.objects.update_or_create(
                code=spec["code"],
                defaults=_scenario_defaults(spec),
            )
            scenarios[spec["code"]] = scenario

        self._exclude_product_a_legacy_history(bags["A"])

        managed = {}
        for product_key in ("A", "B"):
            for period_key in ("previous", "current"):
                spec = next(
                    item
                    for item in MANAGED_HISTORY_SPECS
                    if item["product_key"] == product_key
                    and item["period_key"] == period_key
                )
                scenario = scenarios[spec["code"]]
                started_at = timezone.make_aware(
                    spec["started_at"],
                    timezone.get_default_timezone(),
                )
                session = self._ensure_session(
                    bag=bags[product_key],
                    scenario=scenario,
                    seed=spec["seed"],
                    started_at=started_at,
                )
                self._ensure_readings(session)
                report = self._ensure_report(session)
                self._validate_managed_report(report)
                managed[(product_key, period_key)] = (session, report)

            self._validate_current_report(
                current=managed[(product_key, "current")],
                previous_session=managed[(product_key, "previous")][0],
            )

        self.stdout.write(
            self.style.SUCCESS(
                "Demo HISTORY data seeded (4 scenarios, 4 sessions, 28 readings, "
                "4 reports)."
            )
        )

    def _resolve_product(self, identity, label):
        matches = list(
            ProductModel.objects.select_for_update().filter(
                brand=identity[0],
                model_name=identity[1],
            )
        )
        if len(matches) != 1:
            raise CommandError(
                f"Expected exactly one final {label} ProductModel; found {len(matches)}."
            )
        return matches[0]

    def _resolve_bag(self, nfc_uid, product, label):
        matches = list(
            Bag.objects.select_for_update().filter(nfc_uid=nfc_uid)
        )
        if len(matches) != 1:
            raise CommandError(
                f"Expected exactly one {label} demo Bag {nfc_uid}; found {len(matches)}."
            )
        bag = matches[0]
        if bag.product_model_id != product.pk:
            raise CommandError(f"{nfc_uid} is linked to a different ProductModel.")
        return bag

    def _exclude_product_a_legacy_history(self, bag):
        (
            MeasurementSession.objects.select_for_update()
            .filter(
                bag=bag,
                purpose=MeasurementSession.Purpose.HISTORY,
                scenario__code__in=LEGACY_PRODUCT_A_SCENARIO_CODES,
                include_in_report=True,
            )
            .update(include_in_report=False)
        )

    def _ensure_session(self, *, bag, scenario, seed, started_at):
        matches = list(
            MeasurementSession.objects.select_for_update().filter(
                bag=bag,
                scenario=scenario,
                started_at=started_at,
            )
        )
        if len(matches) > 1:
            raise CommandError(
                f"Multiple managed sessions match {scenario.code} at {started_at.isoformat()}."
            )
        if not matches:
            session, _total_count = create_simulation_session(
                bag,
                scenario.code,
                random_seed=seed,
                started_at=started_at,
            )
            session.include_in_report = True
            session.save(update_fields=["include_in_report"])
            return session

        session = matches[0]
        expected = {
            "bag_id": bag.pk,
            "scenario_id": scenario.pk,
            "purpose": MeasurementSession.Purpose.HISTORY,
            "status": MeasurementSession.Status.COMPLETED,
            "started_at": started_at,
            "ended_at": started_at + PERIOD_DURATION,
            "seed": seed,
        }
        conflicts = [
            field
            for field, expected_value in expected.items()
            if getattr(session, field) != expected_value
        ]
        if conflicts:
            raise CommandError(
                f"Managed session {session.pk} conflicts on: {', '.join(conflicts)}."
            )
        if not session.include_in_report:
            session.include_in_report = True
            session.save(update_fields=["include_in_report"])
        return session

    def _expected_reading_values(self, session, sequence, total_count):
        rng = random.Random(session.seed + sequence)
        progress = sequence / max(total_count - 1, 1)
        values = {}
        for field in FIELD_NAMES:
            field_config = session.scenario.config.get(field)
            if field in OPTIONAL_FIELD_NAMES and field_config is None:
                values[field] = None
                continue
            value = compute_field_value(field_config, progress, rng)
            if field in FIELD_MIN_CLAMP:
                value = max(value, FIELD_MIN_CLAMP[field])
            if field in FIELD_MAX_CLAMP:
                value = min(value, FIELD_MAX_CLAMP[field])
            values[field] = Decimal(str(round(value, FIELD_PRECISION[field])))
        return values

    def _validate_reading(self, reading, session, total_count):
        expected_values = self._expected_reading_values(
            session,
            reading.sequence,
            total_count,
        )
        expected_measured_at = session.started_at + timedelta(
            seconds=reading.sequence * session.scenario.sample_interval_seconds
        )
        conflicts = []
        if reading.measured_at != expected_measured_at:
            conflicts.append("measured_at")
        if reading.moisture_detected != is_moisture_trigger(
            session.scenario.config,
            reading.sequence,
            total_count,
        ):
            conflicts.append("moisture_detected")
        for field, expected_value in expected_values.items():
            if getattr(reading, field) != expected_value:
                conflicts.append(field)
        if conflicts:
            raise CommandError(
                f"Managed reading session={session.pk} sequence={reading.sequence} "
                f"conflicts on: {', '.join(conflicts)}."
            )

    def _ensure_readings(self, session):
        total_count = calculate_total_reading_count(session.scenario)
        if total_count != 7:
            raise CommandError(f"Managed scenario {session.scenario.code} must produce 7 readings.")

        existing = list(session.readings.order_by("sequence"))
        if any(reading.sequence not in range(total_count) for reading in existing):
            raise CommandError(f"Managed session {session.pk} has an unexpected sequence.")
        for reading in existing:
            self._validate_reading(reading, session, total_count)

        existing_sequences = {reading.sequence for reading in existing}
        for sequence in range(total_count):
            if sequence not in existing_sequences:
                generate_single_reading(session, sequence, total_count)

        readings = list(session.readings.order_by("sequence"))
        if [reading.sequence for reading in readings] != list(range(7)):
            raise CommandError(f"Managed session {session.pk} must contain sequences 0..6.")
        for reading in readings:
            self._validate_reading(reading, session, total_count)

    def _ensure_report(self, session):
        try:
            return AnalysisReport.objects.select_for_update().get(session=session)
        except AnalysisReport.DoesNotExist:
            report, _created = analyze_history_session(session)
            return report

    def _validate_managed_report(self, report):
        conflicts = []
        if report.severity != "NORMAL":
            conflicts.append("severity")
        if report.active_rules != []:
            conflicts.append("active_rules")
        if report.unavailable_rules != []:
            conflicts.append("unavailable_rules")
        if len(report.metrics.get("daily_series", [])) != 7:
            conflicts.append("metrics.daily_series")
        if conflicts:
            raise CommandError(
                f"Managed report {report.pk} conflicts on: {', '.join(conflicts)}."
            )

    def _validate_current_report(self, *, current, previous_session):
        _current_session, report = current
        conflicts = []
        if not report.comparison.get("available"):
            conflicts.append("comparison.available")
        if report.comparison.get("previous_session_id") != previous_session.pk:
            conflicts.append("comparison.previous_session_id")
        if conflicts:
            raise CommandError(
                f"Managed current report {report.pk} conflicts on: {', '.join(conflicts)}."
            )
