from copy import deepcopy
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from products.models import Bag, ProductModel
from simulation.models import SimulationScenario


PRODUCT_A_LEGACY_IDENTITY = ("Central Bag Co.", "Voyager Tote")
PRODUCT_A_FINAL_IDENTITY = ("MCM", "Vela Visetos Sling Bag")
PRODUCT_A_DEMO_NFC_UID = "NFC-DEMO-0001"
PRODUCT_A_SCENARIO_CODE = "HOT_CAR_LIVE"

PRODUCT_B_IDENTITY = ("MCM", "Visetos Original Boston Bag")
PRODUCT_B_DEMO_NFC_UID = "NFC-DEMO-0002"
PRODUCT_B_PUBLIC_TOKEN = UUID("22222222-2222-2222-2222-222222222222")
PRODUCT_B_SCENARIO_CODE = "RAIN_MOISTURE_LIVE"

DEMO_GUIDELINE_NOTE = "브랜드 공식 자료가 아닌 시연용 가정 데이터"

PRODUCT_A_DISPLAY_METRICS = [
    {"key": "right_load_percent", "label": "우측 하중", "unit": "%"},
    {"key": "shape_deviation_percent", "label": "형태 편차", "unit": "%"},
    {"key": "temperature_c", "label": "현재 온도", "unit": "°C"},
]

PRODUCT_B_DISPLAY_METRICS = [
    {"key": "shape_deviation_percent", "label": "형태 편차", "unit": "%"},
    {
        "key": "material_moisture_percent",
        "label": "소재 수분도",
        "unit": "%",
    },
    {
        "key": "internal_humidity_percent",
        "label": "내부 습도",
        "unit": "%",
    },
]

PRODUCT_B_CARE_ACTIONS = {
    "MOISTURE": {
        "title": "내용물을 모두 꺼내 주세요.",
        "reason": (
            "스웨이드 소재에 수분이 남아 있어 내부 공기가 통하도록 "
            "먼저 비워 주는 것이 좋아요."
        ),
        "steps": [
            "내용물을 꺼내 내부에 남은 물기가 없는지 확인해 주세요.",
            "가방 입구를 열어 내부에 공기가 통하게 해 주세요.",
        ],
    },
    "HIGH_HUMIDITY": {
        "title": "가방을 열어 통풍이 잘되는 곳에 두어 주세요.",
        "reason": (
            "내부 습도가 높게 유지되고 있어 자연스럽게 습기를 "
            "빼주는 것이 좋아요."
        ),
        "steps": [
            "가방 입구와 포켓을 열어 공기가 통하게 해 주세요.",
            "직사광선과 강한 열을 피해 통풍이 되는 곳에서 자연 건조해 주세요.",
        ],
    },
}

LIVE_STATE_FALLBACK = {
    "code": "ATTENTION",
    "headline": "가방 상태에 변화가 감지됐어요",
    "description": "일부 센서 값이 관리 기준을 벗어나 현재 상태를 확인하고 있어요.",
    "quick_care": "가방 상태를 확인해 주세요.",
    "theme_key": "attention",
}

PRODUCT_A_LIVE_STATES = {
    "stable": {
        "code": "STABLE",
        "headline": "현재 가방 상태를 확인하고 있어요",
        "description": "온도와 하중, 형태 편차를 실시간으로 확인하고 있어요.",
        "quick_care": "현재 상태를 계속 확인해 주세요.",
        "theme_key": "stable",
    },
    "states": [
        {
            "code": "SHAPE_RISK",
            "required_rules": [
                "HIGH_TEMPERATURE",
                "LOAD_BIAS",
                "DEFORMATION",
            ],
            "primary_rule": "DEFORMATION",
            "headline": "가방의 형태 변화가 감지되고 있어요",
            "description": (
                "높은 온도에 노출된 상태에서 하중 편중과 형태 편차가 "
                "함께 감지되고 있어요."
            ),
            "quick_care": "내용물을 비우고 가방을 세워 주세요.",
            "theme_key": "shape_warning",
        },
        {
            "code": "HEAT_EXPOSURE",
            "required_rules": ["HIGH_TEMPERATURE"],
            "primary_rule": "HIGH_TEMPERATURE",
            "headline": "가방이 높은 온도에 노출되고 있어요",
            "description": (
                "현재 온도가 관리 기준을 넘어 높은 온도에 노출되고 있어요."
            ),
            "quick_care": "가방을 서늘한 곳으로 옮겨 주세요.",
            "theme_key": "heat_warning",
        },
    ],
    "fallback_active": LIVE_STATE_FALLBACK,
}

PRODUCT_B_LIVE_STATES = {
    "stable": {
        "code": "STABLE",
        "headline": "현재 가방 상태를 확인하고 있어요",
        "description": "소재 수분과 내부 습도를 실시간으로 확인하고 있어요.",
        "quick_care": "현재 상태를 계속 확인해 주세요.",
        "theme_key": "stable",
    },
    "states": [
        {
            "code": "HUMIDITY_RETENTION",
            "required_rules": ["MOISTURE", "HIGH_HUMIDITY"],
            "primary_rule": "HIGH_HUMIDITY",
            "headline": "가방 내부 습도가 높게 감지되고 있어요",
            "description": (
                "수분 접촉 이후 내부 습도가 관리 기준을 넘은 상태예요."
            ),
            "quick_care": "가방을 열어 통풍이 잘되는 곳에 두어 주세요.",
            "theme_key": "humidity_warning",
        },
        {
            "code": "MOISTURE_CONTACT",
            "required_rules": ["MOISTURE"],
            "primary_rule": "MOISTURE",
            "headline": "스웨이드 소재에 수분 접촉이 감지됐어요",
            "description": (
                "수분 접촉이 감지되어 스웨이드 소재의 상태를 계속 확인하고 있어요."
            ),
            "quick_care": "내용물을 꺼내고 가방을 열어 주세요.",
            "theme_key": "moisture_warning",
        },
    ],
    "fallback_active": LIVE_STATE_FALLBACK,
}

PRODUCT_A_SCENARIO_DEFAULTS = {
    "name": "Hot Car (Live)",
    "scenario_type": SimulationScenario.ScenarioType.HIGH_TEMPERATURE,
    "mode": SimulationScenario.Mode.LIVE,
    "logical_duration_seconds": 86400,
    "sample_interval_seconds": 3600,
    "config": {
        "strap_load": {"min": 2.5, "max": 4.0},
        "humidity": {"min": 40, "max": 55},
        "temperature": {"start": 33, "end": 47},
        "load_bias": {"start": 0.10, "end": 0.60},
        "body_deformation_ratio": {"start": 0.005, "end": 0.060},
        "moisture_event": {"enabled": False},
    },
    "version": 1,
    "is_active": True,
}

PRODUCT_B_SCENARIO_DEFAULTS = {
    "name": "Rain Moisture (Live)",
    "scenario_type": SimulationScenario.ScenarioType.HIGH_HUMIDITY,
    "mode": SimulationScenario.Mode.LIVE,
    "logical_duration_seconds": 86400,
    "sample_interval_seconds": 3600,
    "config": {
        "strap_load": {"min": 2.0, "max": 3.5},
        "temperature": {"min": 23, "max": 27},
        "load_bias": {"start": 0.00, "end": 0.08},
        "body_deformation_ratio": {"start": 0.005, "end": 0.020},
        "humidity": {"start": 48, "end": 78},
        "material_moisture_percent": {"start": 58, "end": 42},
        "moisture_event": {"enabled": True, "trigger_at_ratio": 0},
    },
    "version": 1,
    "is_active": True,
}


def _require_mapping(value, description):
    if not isinstance(value, dict):
        raise CommandError(f"{description} must be a JSON object.")
    return deepcopy(value)


def _merge_live_presentation(guideline, display_metrics, description):
    live_presentation = _require_mapping(
        guideline.get("live_presentation", {}),
        f"{description}.live_presentation",
    )
    live_presentation["display_metrics"] = deepcopy(display_metrics)
    guideline["live_presentation"] = live_presentation


def _merge_live_states(guideline, configured_live_states, description):
    live_states = _require_mapping(
        guideline.get("live_states", {}),
        f"{description}.live_states",
    )
    live_states.update(deepcopy(configured_live_states))
    guideline["live_states"] = live_states


class Command(BaseCommand):
    help = "Seed final Product A/B LIVE demo data without touching session history."

    @transaction.atomic
    def handle(self, *args, **options):
        product_a = self._resolve_product_a()
        product_a_bag = self._resolve_product_a_bag(product_a)

        SimulationScenario.objects.update_or_create(
            code=PRODUCT_A_SCENARIO_CODE,
            defaults=deepcopy(PRODUCT_A_SCENARIO_DEFAULTS),
        )
        self._update_product_a(product_a)

        product_b, product_b_created = self._resolve_product_b()
        SimulationScenario.objects.update_or_create(
            code=PRODUCT_B_SCENARIO_CODE,
            defaults=deepcopy(PRODUCT_B_SCENARIO_DEFAULTS),
        )
        self._update_product_b(product_b)
        product_b_bag, product_b_bag_created = self._resolve_product_b_bag(
            product_b,
            product_a_bag.owner,
        )

        self.stdout.write(
            self.style.SUCCESS(
                "Demo LIVE data seeded "
                f"(Product B: {'created' if product_b_created else 'reused'}, "
                f"Bag B: {'created' if product_b_bag_created else 'reused'}, "
                f"Bag B id: {product_b_bag.pk})."
            )
        )

    def _resolve_product_a(self):
        legacy_brand, legacy_model_name = PRODUCT_A_LEGACY_IDENTITY
        final_brand, final_model_name = PRODUCT_A_FINAL_IDENTITY
        matches = list(
            ProductModel.objects.select_for_update()
            .filter(
                Q(brand=legacy_brand, model_name=legacy_model_name)
                | Q(brand=final_brand, model_name=final_model_name)
            )
            .order_by("pk")
        )
        if len(matches) != 1:
            raise CommandError(
                "Expected exactly one legacy or final Product A row; "
                f"found {len(matches)}."
            )
        return matches[0]

    def _resolve_product_a_bag(self, product_a):
        try:
            bag = Bag.objects.select_for_update().get(
                nfc_uid=PRODUCT_A_DEMO_NFC_UID
            )
        except Bag.DoesNotExist as exc:
            raise CommandError(
                f"Product A demo Bag {PRODUCT_A_DEMO_NFC_UID} was not found."
            ) from exc

        if bag.product_model_id != product_a.pk:
            raise CommandError(
                f"{PRODUCT_A_DEMO_NFC_UID} is linked to a different ProductModel."
            )
        return bag

    def _update_product_a(self, product_a):
        guideline = _require_mapping(
            product_a.care_guideline,
            "Product A care_guideline",
        )
        _merge_live_presentation(
            guideline,
            PRODUCT_A_DISPLAY_METRICS,
            "Product A care_guideline",
        )
        _merge_live_states(
            guideline,
            PRODUCT_A_LIVE_STATES,
            "Product A care_guideline",
        )

        product_a.brand, product_a.model_name = PRODUCT_A_FINAL_IDENTITY
        product_a.material = "Leather"
        product_a.demo_live_scenario_code = PRODUCT_A_SCENARIO_CODE
        product_a.care_guideline = guideline
        product_a.save(
            update_fields=[
                "brand",
                "model_name",
                "material",
                "demo_live_scenario_code",
                "care_guideline",
            ]
        )

    def _resolve_product_b(self):
        brand, model_name = PRODUCT_B_IDENTITY
        matches = list(
            ProductModel.objects.select_for_update()
            .filter(brand=brand, model_name=model_name)
            .order_by("pk")
        )
        if len(matches) > 1:
            raise CommandError(
                "Multiple Product B rows match the exact brand/model_name identity."
            )
        if matches:
            return matches[0], False

        return (
            ProductModel.objects.create(
                brand=brand,
                model_name=model_name,
                material="Suede",
                care_guideline={},
            ),
            True,
        )

    def _update_product_b(self, product_b):
        guideline = _require_mapping(
            product_b.care_guideline,
            "Product B care_guideline",
        )
        existing_actions = guideline.get("care_actions", {})
        existing_actions = _require_mapping(
            existing_actions,
            "Product B care_guideline.care_actions",
        )
        existing_actions.update(deepcopy(PRODUCT_B_CARE_ACTIONS))
        _merge_live_presentation(
            guideline,
            PRODUCT_B_DISPLAY_METRICS,
            "Product B care_guideline",
        )
        _merge_live_states(
            guideline,
            PRODUCT_B_LIVE_STATES,
            "Product B care_guideline",
        )
        guideline.update(
            {
                "avoid_moisture": True,
                "max_load_kg": 5.5,
                "recommended_temp_range_c": [0, 35],
                "max_humidity_percent": 60,
                "max_abs_load_bias": 0.30,
                "max_body_deformation_ratio": 0.03,
                "care_actions": existing_actions,
                "note": DEMO_GUIDELINE_NOTE,
            }
        )

        product_b.material = "Suede"
        product_b.demo_live_scenario_code = PRODUCT_B_SCENARIO_CODE
        product_b.care_guideline = guideline
        product_b.save(
            update_fields=[
                "material",
                "demo_live_scenario_code",
                "care_guideline",
            ]
        )

    def _resolve_product_b_bag(self, product_b, owner):
        nfc_bag = (
            Bag.objects.select_for_update()
            .filter(nfc_uid=PRODUCT_B_DEMO_NFC_UID)
            .first()
        )
        token_bag = (
            Bag.objects.select_for_update()
            .filter(public_token=PRODUCT_B_PUBLIC_TOKEN)
            .first()
        )

        for identifier, bag in (
            (PRODUCT_B_DEMO_NFC_UID, nfc_bag),
            (str(PRODUCT_B_PUBLIC_TOKEN), token_bag),
        ):
            if bag is not None and bag.product_model_id != product_b.pk:
                raise CommandError(
                    f"Demo Bag identifier {identifier} belongs to another product."
                )

        matched_bags = {
            bag.pk: bag for bag in (nfc_bag, token_bag) if bag is not None
        }
        if len(matched_bags) > 1:
            raise CommandError(
                "Product B NFC UID and public token resolve to different Bags."
            )

        if matched_bags:
            bag = next(iter(matched_bags.values()))
            bag.owner = owner
            bag.nfc_uid = PRODUCT_B_DEMO_NFC_UID
            bag.public_token = PRODUCT_B_PUBLIC_TOKEN
            bag.save(update_fields=["owner", "nfc_uid", "public_token"])
            return bag, False

        return (
            Bag.objects.create(
                product_model=product_b,
                owner=owner,
                serial_number=None,
                nfc_uid=PRODUCT_B_DEMO_NFC_UID,
                public_token=PRODUCT_B_PUBLIC_TOKEN,
            ),
            True,
        )
