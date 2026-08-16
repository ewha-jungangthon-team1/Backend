import json
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from measurements.home import build_sensor_presentation_values
from measurements.models import MeasurementSession, SensorReading
from products.models import Bag, ProductModel
from simulation.models import SimulationScenario

from .ai.live_care import (
    LIVE_CARE_DESCRIPTION_MAX_LENGTH,
    LIVE_CARE_TITLE_MAX_LENGTH,
    LiveCareContentValidationError,
    build_live_care_fallback,
    validate_live_care_content,
)
from .live_care_context import build_live_care_context


def build_live_states(*, code, required_rules, primary_rule, quick_care):
    return {
        "stable": {
            "code": "STABLE",
            "headline": "현재 상태를 확인하고 있어요",
            "description": "현재 센서 상태를 확인하고 있어요.",
            "quick_care": "현재 상태를 계속 확인해 주세요.",
            "theme_key": "stable",
        },
        "states": [
            {
                "code": code,
                "required_rules": required_rules,
                "primary_rule": primary_rule,
                "headline": "관리가 필요한 상태가 감지됐어요",
                "description": "현재 센서 기준으로 관리가 필요한 상태예요.",
                "quick_care": quick_care,
                "theme_key": "warning",
            }
        ],
        "fallback_active": {
            "code": "ATTENTION",
            "headline": "현재 상태를 확인해 주세요",
            "description": "현재 센서 상태를 자세히 확인해 주세요.",
            "quick_care": "가방 상태를 확인해 주세요.",
            "theme_key": "attention",
        },
    }


class LiveCareContextTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        owner = get_user_model().objects.create_user(
            username="live-care-context-owner",
            password="test-password",
        )
        cls.scenario = SimulationScenario.objects.create(
            code="LIVE_CARE_CONTEXT",
            name="Context Calculation Only",
            scenario_type=SimulationScenario.ScenarioType.NORMAL,
            mode=SimulationScenario.Mode.LIVE,
            logical_duration_seconds=86400,
            sample_interval_seconds=3600,
            config={},
            is_active=True,
        )
        common_thresholds = {
            "max_load_kg": 5.5,
            "recommended_temp_range_c": [0, 35],
            "max_humidity_percent": 60,
            "max_abs_load_bias": 0.30,
            "max_body_deformation_ratio": 0.03,
            "avoid_moisture": True,
        }
        product_a_actions = {
            rule: {
                "title": f"A {rule} 관리",
                "steps": [f"A {rule} 행동을 확인해 주세요."],
            }
            for rule in (
                "HIGH_LOAD",
                "HIGH_TEMPERATURE",
                "HIGH_HUMIDITY",
                "MOISTURE",
                "LOAD_BIAS",
                "DEFORMATION",
            )
        }
        product_b_actions = {
            rule: {
                "title": f"B {rule} 관리",
                "steps": [f"B {rule} 행동을 확인해 주세요."],
            }
            for rule in ("MOISTURE", "HIGH_HUMIDITY", "HIGH_LOAD")
        }
        cls.product_a = ProductModel.objects.create(
            brand="Context Brand A",
            model_name="Context Leather Bag",
            material="Leather",
            care_guideline={
                **common_thresholds,
                "care_actions": product_a_actions,
                "live_states": build_live_states(
                    code="SHAPE_RISK",
                    required_rules=[
                        "HIGH_TEMPERATURE",
                        "LOAD_BIAS",
                        "DEFORMATION",
                    ],
                    primary_rule="DEFORMATION",
                    quick_care="내용물을 정리하고 가방을 세워 주세요.",
                ),
                "live_presentation": {"must_not_leak": "A presentation config"},
            },
        )
        cls.product_b = ProductModel.objects.create(
            brand="Context Brand B",
            model_name="Context Suede Bag",
            material="Suede",
            care_guideline={
                **common_thresholds,
                "care_actions": product_b_actions,
                "live_states": build_live_states(
                    code="HUMIDITY_RETENTION",
                    required_rules=["MOISTURE", "HIGH_HUMIDITY"],
                    primary_rule="HIGH_HUMIDITY",
                    quick_care="가방을 열어 통풍되는 곳에 두어 주세요.",
                ),
                "live_presentation": {"must_not_leak": "B presentation config"},
            },
        )
        cls.bag_a = Bag.objects.create(
            product_model=cls.product_a,
            owner=owner,
            nfc_uid="LIVE-CARE-CONTEXT-A",
        )
        cls.bag_b = Bag.objects.create(
            product_model=cls.product_b,
            owner=owner,
            nfc_uid="LIVE-CARE-CONTEXT-B",
        )
        now = timezone.now()
        cls.session_a = MeasurementSession.objects.create(
            bag=cls.bag_a,
            scenario=cls.scenario,
            purpose=MeasurementSession.Purpose.LIVE,
            seed=1001,
            started_at=now,
            status=MeasurementSession.Status.RUNNING,
        )
        cls.session_b = MeasurementSession.objects.create(
            bag=cls.bag_b,
            scenario=cls.scenario,
            purpose=MeasurementSession.Purpose.LIVE,
            seed=1002,
            started_at=now,
            status=MeasurementSession.Status.RUNNING,
        )
        SensorReading.objects.create(
            session=cls.session_b,
            sequence=0,
            measured_at=now,
            strap_load="3.00",
            humidity="70.00",
            material_moisture_percent="42.50",
            moisture_detected=True,
            temperature="25.00",
            load_bias="0.1000",
            body_deformation_ratio="0.0100",
        )

    def current_a(self):
        observed_at = timezone.now()
        return {
            "session_id": self.session_a.pk,
            "sequence": 8,
            "measured_at": observed_at - timedelta(seconds=1),
            "observed_at": observed_at,
            "scenario_type": "NORMAL",
            "progress_ratio": 0.5,
            "is_finished": False,
            "strap_load": 3.2,
            "load_bias": 0.4,
            "body_deformation_ratio": 0.04,
            "temperature": 40.0,
            "humidity": 50.0,
            "material_moisture_percent": None,
            "moisture_detected": False,
        }

    def current_b(self):
        observed_at = timezone.now()
        return {
            "session_id": self.session_b.pk,
            "sequence": 9,
            "measured_at": observed_at - timedelta(seconds=1),
            "observed_at": observed_at,
            "scenario_type": "NORMAL",
            "progress_ratio": 0.6,
            "is_finished": False,
            "strap_load": 3.0,
            "load_bias": 0.1,
            "body_deformation_ratio": 0.01,
            "temperature": 25.0,
            "humidity": 70.0,
            "material_moisture_percent": 42.5,
            "moisture_detected": True,
        }

    def test_product_a_context_reuses_current_f2_f3_facts_and_projects_actions(self):
        current = self.current_a()
        guideline_before = deepcopy(self.product_a.care_guideline)

        with patch(
            "analysis.live_care_context.get_latest_reading",
            return_value=current,
        ) as latest:
            context = build_live_care_context(self.session_a)

        latest.assert_called_once_with(self.session_a)
        self.assertEqual(
            context["product"],
            {
                "brand": "Context Brand A",
                "model_name": "Context Leather Bag",
                "material": "Leather",
            },
        )
        self.assertEqual(
            context["sensor_facts"]["raw"],
            {
                "strap_load": 3.2,
                "load_bias": 0.4,
                "body_deformation_ratio": 0.04,
                "temperature": 40.0,
                "humidity": 50.0,
                "material_moisture_percent": None,
                "moisture_detected": False,
            },
        )
        self.assertEqual(
            context["sensor_facts"]["presentation"],
            build_sensor_presentation_values(
                strap_load=3.2,
                load_bias=0.4,
                body_deformation_ratio=0.04,
                temperature=40,
                humidity=50,
                material_moisture_percent=None,
            ),
        )
        self.assertEqual(
            context["interpretation"],
            {
                "active_rules": [
                    "HIGH_TEMPERATURE",
                    "LOAD_BIAS",
                    "DEFORMATION",
                ],
                "unavailable_rules": [],
                "state_code": "SHAPE_RISK",
                "primary_rule": "DEFORMATION",
                "quick_care": "내용물을 정리하고 가방을 세워 주세요.",
            },
        )
        self.assertEqual(
            list(context["guideline"]["relevant_care_actions"]),
            ["DEFORMATION", "HIGH_TEMPERATURE", "LOAD_BIAS"],
        )
        self.assertNotIn(
            "MOISTURE", context["guideline"]["relevant_care_actions"]
        )
        self.assertNotIn("live_states", context["guideline"])
        self.assertNotIn("live_presentation", context["guideline"])
        self.assertEqual(self.product_a.care_guideline, guideline_before)
        json.dumps(context, ensure_ascii=False, allow_nan=False)

    def test_product_b_context_keeps_session_moisture_and_numeric_material_fact(self):
        current = self.current_b()

        with patch(
            "analysis.live_care_context.get_latest_reading",
            return_value=current,
        ):
            context = build_live_care_context(self.session_b)

        self.assertEqual(context["product"]["material"], "Suede")
        self.assertEqual(
            context["sensor_facts"]["raw"]["material_moisture_percent"],
            42.5,
        )
        self.assertIs(
            context["sensor_facts"]["raw"]["moisture_detected"], True
        )
        self.assertEqual(
            context["interpretation"]["active_rules"],
            ["HIGH_HUMIDITY", "MOISTURE"],
        )
        self.assertEqual(
            list(context["guideline"]["relevant_care_actions"]),
            ["HIGH_HUMIDITY", "MOISTURE"],
        )
        self.assertNotIn(
            "HIGH_LOAD", context["guideline"]["relevant_care_actions"]
        )

    def test_product_contexts_are_isolated(self):
        with patch(
            "analysis.live_care_context.get_latest_reading",
            side_effect=[self.current_a(), self.current_b()],
        ):
            context_a = build_live_care_context(self.session_a)
            context_b = build_live_care_context(self.session_b)

        serialized_a = json.dumps(context_a, ensure_ascii=False)
        serialized_b = json.dumps(context_b, ensure_ascii=False)
        self.assertNotIn("Context Brand B", serialized_a)
        self.assertNotIn("B HIGH_HUMIDITY", serialized_a)
        self.assertNotIn("Context Brand A", serialized_b)
        self.assertNotIn("A DEFORMATION", serialized_b)

    def test_context_rejects_non_live_missing_scenario_and_missing_reading(self):
        history = MeasurementSession.objects.create(
            bag=self.bag_a,
            scenario=self.scenario,
            purpose=MeasurementSession.Purpose.HISTORY,
            seed=2001,
            started_at=timezone.now(),
            ended_at=timezone.now(),
            status=MeasurementSession.Status.COMPLETED,
        )
        with self.assertRaisesMessage(ValueError, "Only LIVE sessions"):
            build_live_care_context(history)

        self.session_a.scenario = None
        self.session_a.save(update_fields=["scenario"])
        with self.assertRaisesMessage(ValueError, "scenario is required"):
            build_live_care_context(self.session_a)

        self.session_a.scenario = self.scenario
        self.session_a.save(update_fields=["scenario"])
        with (
            patch(
                "analysis.live_care_context.get_latest_reading",
                return_value=None,
            ),
            self.assertRaisesMessage(ValueError, "current LIVE reading"),
        ):
            build_live_care_context(self.session_a)


class LiveCareContentContractTests(TestCase):
    def valid_content(self):
        return {
            "steps": [
                {
                    "step": index,
                    "title": f"관리 {index}단계",
                    "description": f"가방 상태를 {index}번째로 확인해 주세요.",
                }
                for index in range(1, 4)
            ]
        }

    def assert_invalid(self, payload):
        with self.assertRaises(LiveCareContentValidationError):
            validate_live_care_content(payload)

    def test_validates_and_normalizes_exact_three_step_contract(self):
        payload = self.valid_content()
        payload["steps"][0]["title"] = "  우선 관리  "
        payload["steps"][0]["description"] = "  현재 상태를 확인해 주세요.  "

        result = validate_live_care_content(payload)

        self.assertEqual(set(result), {"steps"})
        self.assertEqual([item["step"] for item in result["steps"]], [1, 2, 3])
        self.assertEqual(result["steps"][0]["title"], "우선 관리")
        self.assertEqual(
            result["steps"][0]["description"], "현재 상태를 확인해 주세요."
        )

    def test_rejects_wrong_count_order_duplicates_and_bool_step(self):
        for mutate in (
            lambda value: value["steps"].pop(),
            lambda value: value["steps"].append(deepcopy(value["steps"][-1])),
            lambda value: value["steps"].__setitem__(
                slice(None),
                [value["steps"][0], value["steps"][2], value["steps"][1]],
            ),
            lambda value: value["steps"][1].__setitem__("step", 1),
            lambda value: value["steps"][0].__setitem__("step", True),
        ):
            with self.subTest(mutate=mutate):
                payload = self.valid_content()
                mutate(payload)
                self.assert_invalid(payload)

    def test_rejects_extra_fields_empty_non_string_and_non_korean_text(self):
        invalid_payloads = []
        top_extra = self.valid_content()
        top_extra["summary"] = "추가"
        invalid_payloads.append(top_extra)
        item_extra = self.valid_content()
        item_extra["steps"][0]["extra"] = "추가"
        invalid_payloads.append(item_extra)
        for field, value in (
            ("title", ""),
            ("description", "   "),
            ("title", 123),
            ("description", ["문장"]),
            ("title", "English only"),
            ("description", "English only"),
        ):
            payload = self.valid_content()
            payload["steps"][0][field] = value
            invalid_payloads.append(payload)

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                self.assert_invalid(payload)

    def test_rejects_title_and_description_over_length_limits(self):
        title = self.valid_content()
        title["steps"][0]["title"] = "가" * (LIVE_CARE_TITLE_MAX_LENGTH + 1)
        description = self.valid_content()
        description["steps"][0]["description"] = "가" * (
            LIVE_CARE_DESCRIPTION_MAX_LENGTH + 1
        )

        self.assert_invalid(title)
        self.assert_invalid(description)


class LiveCareFallbackTests(TestCase):
    def context(
        self,
        *,
        active_rules=None,
        primary_rule=None,
        quick_care="현재 상태를 계속 확인해 주세요.",
        actions=None,
    ):
        return {
            "product": {"material": "Synthetic Test Material"},
            "interpretation": {
                "active_rules": active_rules or [],
                "unavailable_rules": [],
                "state_code": "TEST_STATE",
                "primary_rule": primary_rule,
                "quick_care": quick_care,
            },
            "guideline": {
                "thresholds": {},
                "relevant_care_actions": actions or {},
            },
        }

    def descriptions(self, result):
        return [item["description"] for item in result["steps"]]

    def test_primary_action_is_collected_before_other_active_rules(self):
        context = self.context(
            active_rules=["RULE_A", "RULE_B"],
            primary_rule="RULE_B",
            actions={
                "RULE_A": {"steps": ["보조 행동을 확인해 주세요."]},
                "RULE_B": {
                    "steps": [
                        "우선 행동을 실행해 주세요.",
                        "두 번째 우선 행동을 실행해 주세요.",
                    ]
                },
            },
        )

        result = build_live_care_fallback(context)

        self.assertEqual(
            self.descriptions(result),
            [
                "우선 행동을 실행해 주세요.",
                "두 번째 우선 행동을 실행해 주세요.",
                "보조 행동을 확인해 주세요.",
            ],
        )

    def test_merges_deduplicates_and_truncates_actions_deterministically(self):
        context = self.context(
            active_rules=["RULE_A", "RULE_B"],
            primary_rule="RULE_A",
            actions={
                "RULE_A": {
                    "steps": [
                        "첫 행동을 실행해 주세요.",
                        "  첫   행동을 실행해 주세요.  ",
                        "두 번째 행동을 실행해 주세요.",
                    ]
                },
                "RULE_B": {
                    "steps": [
                        "세 번째 행동을 실행해 주세요.",
                        "네 번째 행동을 실행해 주세요.",
                    ]
                },
            },
        )

        result = build_live_care_fallback(context)

        self.assertEqual(
            self.descriptions(result),
            [
                "첫 행동을 실행해 주세요.",
                "두 번째 행동을 실행해 주세요.",
                "세 번째 행동을 실행해 주세요.",
            ],
        )

    def test_fills_missing_actions_with_quick_care_and_neutral_actions(self):
        context = self.context(
            active_rules=["RULE_A"],
            primary_rule="RULE_A",
            quick_care="빠른 관리를 실행해 주세요.",
            actions={"RULE_A": {"steps": ["근거 행동을 실행해 주세요."]}},
        )

        result = build_live_care_fallback(context)

        self.assertEqual(
            self.descriptions(result),
            [
                "근거 행동을 실행해 주세요.",
                "빠른 관리를 실행해 주세요.",
                "현재 상태를 계속 확인해 주세요.",
            ],
        )

    def test_stable_fallback_has_exact_three_steps_without_risk_claims(self):
        result = build_live_care_fallback(self.context())

        self.assertEqual(len(result["steps"]), 3)
        self.assertEqual(
            self.descriptions(result),
            [
                "현재 상태를 계속 확인해 주세요.",
                "관리 기준에 맞는 환경에서 보관해 주세요.",
                "다음 사용 전에 가방 상태를 다시 확인해 주세요.",
            ],
        )
        serialized = json.dumps(result, ensure_ascii=False)
        for unsupported_claim in ("손상되었습니다", "수분이 남아", "변형이 진행"):
            self.assertNotIn(unsupported_claim, serialized)

    def test_fallback_uses_shared_validator_and_contains_no_product_hardcoding(self):
        context = self.context(
            active_rules=["RULE_A"],
            primary_rule="RULE_A",
            actions={"RULE_A": {"steps": ["가방을 안전하게 확인해 주세요."]}},
        )

        result = build_live_care_fallback(context)

        self.assertEqual(validate_live_care_content(result), result)
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("Synthetic Test Material", serialized)
        self.assertNotIn("Product A", serialized)
        self.assertNotIn("Product B", serialized)

    def test_skips_invalid_grounded_actions_before_neutral_fill(self):
        context = self.context(
            active_rules=["RULE_A"],
            primary_rule="RULE_A",
            actions={
                "RULE_A": {
                    "steps": [
                        "English only",
                        "가" * (LIVE_CARE_DESCRIPTION_MAX_LENGTH + 1),
                    ]
                }
            },
        )

        result = build_live_care_fallback(context)

        self.assertEqual(len(result["steps"]), 3)
        self.assertEqual(validate_live_care_content(result), result)
