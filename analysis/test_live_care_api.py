import json
from copy import deepcopy
from unittest.mock import Mock, patch

import httpx2
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    RateLimitError,
)
from rest_framework.test import APITestCase

from measurements.models import MeasurementSession
from products.models import Bag, ProductModel
from simulation.models import SimulationScenario

from .ai.live_care import (
    LIVE_CARE_CONTENT_SCHEMA,
    LIVE_CARE_DEVELOPER_INSTRUCTION,
    LiveCareGenerationError,
    generate_live_care_content,
    validate_live_care_content,
)
from .live_care import (
    LiveCareClaimState,
    claim_live_care_generation,
    finalize_live_care_result,
    generate_or_get_live_care,
    release_live_care_generation,
)
from .models import LiveCareResult


def valid_care_content():
    return {
        "steps": [
            {
                "step": 1,
                "title": "우선 관리",
                "description": "현재 상태에 맞는 관리 행동을 확인해 주세요.",
            },
            {
                "step": 2,
                "title": "상태 정리",
                "description": "관리 기준에 맞는 환경에서 보관해 주세요.",
            },
            {
                "step": 3,
                "title": "마무리 확인",
                "description": "다음 사용 전에 가방 상태를 다시 확인해 주세요.",
            },
        ]
    }


def live_context(*, material="Leather", rule="DEFORMATION", marker="A"):
    return {
        "product": {
            "brand": f"Context Brand {marker}",
            "model_name": f"Context Model {marker}",
            "material": material,
        },
        "observation": {
            "session_id": 1,
            "sequence": 10,
            "observed_at": "2026-08-16T12:00:00+09:00",
        },
        "sensor_facts": {
            "raw": {
                "temperature": 40.0,
                "humidity": 50.0,
                "material_moisture_percent": None,
            },
            "presentation": {
                "temperature_c": 40.0,
                "internal_humidity_percent": 50.0,
                "material_moisture_percent": None,
            },
        },
        "interpretation": {
            "active_rules": [rule],
            "unavailable_rules": [],
            "state_code": "TEST_STATE",
            "primary_rule": rule,
            "quick_care": "현재 상태를 확인해 주세요.",
        },
        "guideline": {
            "thresholds": {"max_humidity_percent": 60},
            "relevant_care_actions": {
                rule: {
                    "steps": [f"{marker} 상태에 맞는 행동을 확인해 주세요."]
                }
            },
        },
    }


class LiveCareProviderTests(TestCase):
    def build_response(self, *, status="completed", content=None, output=None):
        return Mock(
            status=status,
            output_text=json.dumps(
                valid_care_content() if content is None else content,
                ensure_ascii=False,
            ),
            output=[] if output is None else output,
        )

    def build_client(self, *, response=None, error=None):
        client = Mock()
        if error is not None:
            client.responses.create.side_effect = error
        else:
            client.responses.create.return_value = response or self.build_response()
        return client

    def assert_reason(self, client, reason):
        with self.assertRaises(LiveCareGenerationError) as raised:
            generate_live_care_content(
                live_context(),
                client=client,
                model="test-model",
            )
        self.assertEqual(raised.exception.reason, reason)
        return raised.exception

    def test_success_uses_shared_strict_schema_and_application_validation(self):
        client = self.build_client()

        result = generate_live_care_content(
            live_context(), client=client, model="test-model"
        )

        self.assertEqual(result, valid_care_content())
        request = client.responses.create.call_args.kwargs
        self.assertEqual(request["model"], "test-model")
        self.assertIs(
            request["text"]["format"]["schema"],
            LIVE_CARE_CONTENT_SCHEMA,
        )
        self.assertIs(request["text"]["format"]["strict"], True)
        self.assertIs(request["store"], False)
        self.assertIs(request["stream"], False)
        validate_live_care_content(result)

    def test_uses_existing_client_and_model_settings(self):
        client = self.build_client()
        with (
            patch(
                "analysis.ai.live_care.get_openai_client",
                return_value=client,
            ) as factory,
            patch(
                "analysis.ai.live_care.get_openai_model",
                return_value="configured-model",
            ) as model,
        ):
            generate_live_care_content(live_context())

        factory.assert_called_once_with()
        model.assert_called_once_with()
        self.assertEqual(
            client.responses.create.call_args.kwargs["model"],
            "configured-model",
        )

    def test_prompt_protects_grounding_and_safety_boundaries(self):
        required_phrases = (
            "제공된 JSON context만 근거",
            "active_rules를 다시 판정",
            "context에 없는 사건",
            "새로운 숫자를 생성하거나 추정",
            "확정 진단하지 마세요",
            "다른 소재를 추정하지 마세요",
            "세척제, 화학제품, 크림, 오일, 발수제",
            "직접 열원, 직사광선 건조",
            "정확히 3단계",
            "한국어 존댓말",
        )
        for phrase in required_phrases:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, LIVE_CARE_DEVELOPER_INSTRUCTION)

    def test_provider_input_is_exact_context_without_cross_product_expansion(self):
        contexts = (
            live_context(material="Leather", rule="DEFORMATION", marker="A"),
            live_context(material="Suede", rule="MOISTURE", marker="B"),
        )
        for context in contexts:
            with self.subTest(material=context["product"]["material"]):
                client = self.build_client()
                generate_live_care_content(
                    context, client=client, model="test-model"
                )
                sent = json.loads(client.responses.create.call_args.kwargs["input"])
                self.assertEqual(sent, context)

        self.assertNotIn("Context Brand B", json.dumps(contexts[0]))
        self.assertNotIn("Context Brand A", json.dumps(contexts[1]))

    @override_settings(OPENAI_API_KEY="")
    def test_missing_api_key_is_live_generation_error(self):
        self.assert_reason_from_call("OPENAI_NOT_CONFIGURED")

    def assert_reason_from_call(self, reason):
        with self.assertRaises(LiveCareGenerationError) as raised:
            generate_live_care_content(live_context())
        self.assertEqual(raised.exception.reason, reason)

    def test_maps_timeout_rate_limit_connection_and_server_errors(self):
        request = httpx2.Request("POST", "https://api.openai.com/v1/responses")
        cases = (
            (APITimeoutError(request=request), "OPENAI_TIMEOUT"),
            (
                RateLimitError(
                    "rate limited",
                    response=httpx2.Response(429, request=request),
                    body=None,
                ),
                "OPENAI_RATE_LIMIT",
            ),
            (APIConnectionError(request=request), "OPENAI_CONNECTION_ERROR"),
            (
                APIStatusError(
                    "server error",
                    response=httpx2.Response(500, request=request),
                    body=None,
                ),
                "OPENAI_API_ERROR",
            ),
        )
        for error, reason in cases:
            with self.subTest(reason=reason):
                mapped = self.assert_reason(
                    self.build_client(error=error), reason
                )
                self.assertIs(mapped.__cause__, error)

    def test_maps_refusal_incomplete_empty_and_malformed_output(self):
        refusal = Mock(type="refusal")
        cases = (
            (
                self.build_response(
                    output=[Mock(type="message", content=[refusal])]
                ),
                "OPENAI_REFUSAL",
            ),
            (self.build_response(status="incomplete"), "OPENAI_INCOMPLETE"),
            (Mock(status="completed", output_text="", output=[]), "EMPTY_AI_RESPONSE"),
            (Mock(status="completed", output_text="{bad", output=[]), "INVALID_AI_RESPONSE"),
        )
        for response, reason in cases:
            with self.subTest(reason=reason):
                self.assert_reason(self.build_client(response=response), reason)

    def test_application_contract_violations_are_invalid_content(self):
        invalid_payloads = []
        four_steps = valid_care_content()
        four_steps["steps"].append(
            {"step": 4, "title": "추가", "description": "추가 행동입니다."}
        )
        invalid_payloads.append(four_steps)
        wrong_order = valid_care_content()
        wrong_order["steps"][1]["step"] = 3
        invalid_payloads.append(wrong_order)
        empty = valid_care_content()
        empty["steps"][0]["title"] = ""
        invalid_payloads.append(empty)
        extra = valid_care_content()
        extra["steps"][0]["extra"] = "금지"
        invalid_payloads.append(extra)
        too_long = valid_care_content()
        too_long["steps"][0]["description"] = "가" * 121
        invalid_payloads.append(too_long)

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                self.assert_reason(
                    self.build_client(response=self.build_response(content=payload)),
                    "INVALID_AI_RESPONSE",
                )

    def test_unknown_programming_error_is_not_hidden(self):
        client = self.build_client(error=TypeError("programming error"))
        with self.assertRaisesMessage(TypeError, "programming error"):
            generate_live_care_content(
                live_context(), client=client, model="test-model"
            )

    def test_provider_does_not_log_context_or_response(self):
        with (
            patch("builtins.print") as print_mock,
            patch("logging.Logger._log") as log_mock,
        ):
            generate_live_care_content(
                live_context(), client=self.build_client(), model="test-model"
            )
        print_mock.assert_not_called()
        log_mock.assert_not_called()


class LiveCareAPITests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        owner = get_user_model().objects.create_user(
            username="live-care-api-owner",
            password="test-password",
        )
        cls.product = ProductModel.objects.create(
            brand="API Context Brand",
            model_name="API Context Bag",
            material="Leather",
            care_guideline={
                "max_load_kg": 5.5,
                "recommended_temp_range_c": [5, 35],
                "max_humidity_percent": 60,
                "max_abs_load_bias": 0.3,
                "max_body_deformation_ratio": 0.03,
                "avoid_moisture": True,
                "care_actions": {},
            },
        )
        cls.bag = Bag.objects.create(
            product_model=cls.product,
            owner=owner,
            nfc_uid="LIVE-CARE-API-NFC",
        )
        cls.scenario = SimulationScenario.objects.create(
            code="LIVE_CARE_API_SCENARIO",
            name="LIVE Care API Scenario",
            scenario_type=SimulationScenario.ScenarioType.NORMAL,
            mode=SimulationScenario.Mode.LIVE,
            logical_duration_seconds=86400,
            sample_interval_seconds=3600,
            config={
                "strap_load": {"start": 2, "end": 3},
                "humidity": {"start": 45, "end": 50},
                "temperature": {"start": 24, "end": 28},
                "load_bias": {"start": 0, "end": 0.1},
                "body_deformation_ratio": {"start": 0.005, "end": 0.01},
                "material_moisture_percent": {"start": 20, "end": 20},
                "moisture_event": {"enabled": False},
            },
        )

    def create_session(
        self,
        *,
        purpose=MeasurementSession.Purpose.LIVE,
        status=MeasurementSession.Status.RUNNING,
        scenario=True,
    ):
        now = timezone.now()
        return MeasurementSession.objects.create(
            bag=self.bag,
            scenario=self.scenario if scenario else None,
            purpose=purpose,
            seed=MeasurementSession.objects.count() + 100,
            started_at=now,
            ended_at=now if status == MeasurementSession.Status.COMPLETED else None,
            status=status,
        )

    def post_care(self, session_id):
        return self.client.post(
            reverse("live-care", kwargs={"session_id": session_id}),
            format="json",
        )

    def test_first_success_and_second_post_reuse_immutable_cache(self):
        session = self.create_session()
        context_value = live_context()
        context_value["observation"]["session_id"] = session.pk
        content = valid_care_content()

        with (
            patch(
                "analysis.live_care.build_live_care_context",
                return_value=context_value,
            ) as context_builder,
            patch(
                "analysis.live_care.generate_live_care_content",
                return_value=content,
            ) as provider,
            patch("analysis.live_care_context.get_latest_reading") as latest,
        ):
            first = self.post_care(session.pk)
            stored = LiveCareResult.objects.get(session=session)
            original = {
                "context": deepcopy(stored.context_snapshot),
                "care": deepcopy(stored.care_result),
                "generated_at": stored.generated_at,
                "updated_at": stored.updated_at,
            }
            second = self.post_care(session.pk)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data["created"], True)
        self.assertEqual(first.data["status"], "READY")
        self.assertEqual(first.data["care"], content)
        self.assertEqual(first.data["fallback_used"], False)
        self.assertEqual(
            set(first.data),
            {
                "session_id",
                "created",
                "status",
                "care",
                "generated_at",
                "fallback_used",
            },
        )
        self.assertNotIn("context_snapshot", first.data)
        self.assertNotIn("fallback_reason", first.data)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.data["created"], False)
        self.assertEqual(second.data["care"], first.data["care"])
        self.assertEqual(second.data["generated_at"], first.data["generated_at"])
        context_builder.assert_called_once_with(session)
        provider.assert_called_once_with(context_value)
        latest.assert_not_called()
        stored.refresh_from_db()
        self.assertEqual(stored.context_snapshot, original["context"])
        self.assertEqual(stored.care_result, original["care"])
        self.assertEqual(stored.generated_at, original["generated_at"])
        self.assertEqual(stored.updated_at, original["updated_at"])

    def test_state_change_after_ready_does_not_regenerate(self):
        session = self.create_session()
        with (
            patch(
                "analysis.live_care.build_live_care_context",
                return_value=live_context(),
            ) as context_builder,
            patch(
                "analysis.live_care.generate_live_care_content",
                return_value=valid_care_content(),
            ) as provider,
        ):
            first = self.post_care(session.pk)
            stored = LiveCareResult.objects.get(session=session)
            original = (
                deepcopy(stored.context_snapshot),
                deepcopy(stored.care_result),
                stored.generated_at,
                stored.updated_at,
            )
            session.status = MeasurementSession.Status.COMPLETED
            session.ended_at = timezone.now()
            session.save(update_fields=["status", "ended_at"])
            second = self.post_care(session.pk)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.data["created"], False)
        context_builder.assert_called_once()
        provider.assert_called_once()
        stored.refresh_from_db()
        self.assertEqual(
            (
                stored.context_snapshot,
                stored.care_result,
                stored.generated_at,
                stored.updated_at,
            ),
            original,
        )

    def test_existing_generating_returns_minimal_202_without_work(self):
        session = self.create_session()
        row = LiveCareResult.objects.create(session=session)
        original_updated_at = row.updated_at
        with (
            patch("analysis.live_care.build_live_care_context") as context_builder,
            patch("analysis.live_care.generate_live_care_content") as provider,
            patch("analysis.live_care_context.get_latest_reading") as latest,
        ):
            response = self.post_care(session.pk)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            response.data,
            {"session_id": session.pk, "created": False, "status": "GENERATING"},
        )
        context_builder.assert_not_called()
        provider.assert_not_called()
        latest.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(row.updated_at, original_updated_at)

    def test_all_typed_provider_failures_finalize_valid_cached_fallback(self):
        reasons = (
            "OPENAI_NOT_CONFIGURED",
            "OPENAI_TIMEOUT",
            "OPENAI_RATE_LIMIT",
            "OPENAI_CONNECTION_ERROR",
            "OPENAI_API_ERROR",
            "OPENAI_REFUSAL",
            "OPENAI_INCOMPLETE",
            "EMPTY_AI_RESPONSE",
            "INVALID_AI_RESPONSE",
        )
        for reason in reasons:
            with self.subTest(reason=reason):
                session = self.create_session()
                with (
                    patch(
                        "analysis.live_care.build_live_care_context",
                        return_value=live_context(),
                    ),
                    patch(
                        "analysis.live_care.generate_live_care_content",
                        side_effect=LiveCareGenerationError(reason),
                    ) as provider,
                ):
                    first = self.post_care(session.pk)
                    second = self.post_care(session.pk)

                self.assertEqual(first.status_code, 200)
                self.assertEqual(first.data["created"], True)
                self.assertEqual(first.data["fallback_used"], True)
                validate_live_care_content(first.data["care"])
                self.assertEqual(second.data["created"], False)
                self.assertEqual(second.data["care"], first.data["care"])
                provider.assert_called_once()
                stored = LiveCareResult.objects.get(session=session)
                self.assertEqual(stored.status, LiveCareResult.Status.READY)
                self.assertEqual(stored.fallback_reason, reason)

    def test_invalid_provider_content_becomes_ready_fallback(self):
        session = self.create_session()
        invalid = valid_care_content()
        invalid["steps"].append(
            {"step": 4, "title": "추가", "description": "추가 행동입니다."}
        )
        client = Mock()
        client.responses.create.return_value = Mock(
            status="completed",
            output_text=json.dumps(invalid, ensure_ascii=False),
            output=[],
        )
        with (
            patch("analysis.live_care.build_live_care_context", return_value=live_context()),
            patch("analysis.ai.live_care.get_openai_client", return_value=client),
            patch("analysis.ai.live_care.get_openai_model", return_value="test-model"),
        ):
            response = self.post_care(session.pk)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["fallback_used"], True)
        validate_live_care_content(response.data["care"])
        self.assertEqual(
            LiveCareResult.objects.get(session=session).fallback_reason,
            "INVALID_AI_RESPONSE",
        )

    def test_context_and_unexpected_provider_failures_release_claim_for_retry(self):
        for target in ("context", "provider"):
            with self.subTest(target=target):
                session = self.create_session()
                context_patch = (
                    patch(
                        "analysis.live_care.build_live_care_context",
                        side_effect=RuntimeError("context failure"),
                    )
                    if target == "context"
                    else patch(
                        "analysis.live_care.build_live_care_context",
                        return_value=live_context(),
                    )
                )
                provider_patch = (
                    patch(
                        "analysis.live_care.generate_live_care_content",
                        side_effect=TypeError("programming error"),
                    )
                    if target == "provider"
                    else patch(
                        "analysis.live_care.generate_live_care_content",
                        return_value=valid_care_content(),
                    )
                )
                with (
                    context_patch,
                    provider_patch,
                    self.assertRaises((RuntimeError, TypeError)),
                ):
                    self.post_care(session.pk)

                self.assertFalse(LiveCareResult.objects.filter(session=session).exists())
                retry = claim_live_care_generation(session)
                self.assertEqual(retry.state, LiveCareClaimState.CLAIMED)

    def test_release_never_deletes_or_mutates_ready_result(self):
        session = self.create_session()
        claim = claim_live_care_generation(session)
        ready, _created = finalize_live_care_result(
            claim.result,
            context_snapshot=live_context(),
            care_result=valid_care_content(),
        )
        original = (
            deepcopy(ready.context_snapshot),
            deepcopy(ready.care_result),
            ready.updated_at,
        )

        released = release_live_care_generation(ready)

        self.assertIs(released, False)
        ready.refresh_from_db()
        self.assertEqual(
            (ready.context_snapshot, ready.care_result, ready.updated_at),
            original,
        )

    def test_session_resolution_validation_and_ready_before_scenario_validation(self):
        missing = self.post_care(999999)
        self.assertEqual(missing.status_code, 404)

        history = self.create_session(
            purpose=MeasurementSession.Purpose.HISTORY,
            status=MeasurementSession.Status.COMPLETED,
        )
        self.assertEqual(self.post_care(history.pk).status_code, 400)

        no_scenario = self.create_session(scenario=False)
        self.assertEqual(self.post_care(no_scenario.pk).status_code, 400)
        self.assertFalse(LiveCareResult.objects.filter(session=no_scenario).exists())

        session = self.create_session()
        claim = claim_live_care_generation(session)
        ready, _created = finalize_live_care_result(
            claim.result,
            context_snapshot=live_context(),
            care_result=valid_care_content(),
        )
        session.scenario = None
        session.save(update_fields=["scenario"])
        with (
            patch("analysis.live_care.build_live_care_context") as context_builder,
            patch("analysis.live_care.generate_live_care_content") as provider,
        ):
            cached = self.post_care(session.pk)
        self.assertEqual(cached.status_code, 200)
        self.assertEqual(cached.data["created"], False)
        self.assertEqual(cached.data["care"], ready.care_result)
        context_builder.assert_not_called()
        provider.assert_not_called()

    def test_running_and_completed_sessions_allow_first_generation(self):
        for status in (
            MeasurementSession.Status.RUNNING,
            MeasurementSession.Status.COMPLETED,
        ):
            with self.subTest(status=status):
                session = self.create_session(status=status)
                with (
                    patch(
                        "analysis.live_care.build_live_care_context",
                        return_value=live_context(),
                    ) as context_builder,
                    patch(
                        "analysis.live_care.generate_live_care_content",
                        return_value=valid_care_content(),
                    ),
                ):
                    response = self.post_care(session.pk)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data["created"], True)
                context_builder.assert_called_once()
                self.assertEqual(context_builder.call_args.args[0].status, status)

    def test_completed_first_generation_uses_actual_latest_live_context(self):
        session = self.create_session(status=MeasurementSession.Status.COMPLETED)
        with patch(
            "analysis.live_care.generate_live_care_content",
            return_value=valid_care_content(),
        ) as provider:
            response = self.post_care(session.pk)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["created"], True)
        stored = LiveCareResult.objects.get(session=session)
        self.assertEqual(
            stored.context_snapshot["observation"]["session_id"],
            session.pk,
        )
        self.assertIn("raw", stored.context_snapshot["sensor_facts"])
        self.assertEqual(session.readings.count(), 1)
        provider.assert_called_once_with(stored.context_snapshot)

    def test_latest_reading_polling_never_invokes_live_care_provider(self):
        session = self.create_session()
        with patch("analysis.live_care.generate_live_care_content") as provider:
            for _index in range(3):
                response = self.client.get(
                    reverse("latest-reading", kwargs={"session_id": session.pk})
                )
                self.assertEqual(response.status_code, 200)
        provider.assert_not_called()
        self.assertFalse(LiveCareResult.objects.filter(session=session).exists())


class LiveCareTransactionBoundaryTests(TransactionTestCase):
    def setUp(self):
        owner = get_user_model().objects.create_user(
            username="live-care-transaction-owner",
            password="test-password",
        )
        product = ProductModel.objects.create(
            brand="Transaction Brand",
            model_name="Transaction Bag",
            material="Leather",
            care_guideline={},
        )
        bag = Bag.objects.create(
            product_model=product,
            owner=owner,
            nfc_uid="LIVE-CARE-TRANSACTION-NFC",
        )
        scenario = SimulationScenario.objects.create(
            code="LIVE_CARE_TRANSACTION_SCENARIO",
            name="LIVE Care Transaction Scenario",
            scenario_type=SimulationScenario.ScenarioType.NORMAL,
            mode=SimulationScenario.Mode.LIVE,
            logical_duration_seconds=86400,
            sample_interval_seconds=3600,
            config={},
        )
        self.session = MeasurementSession.objects.create(
            bag=bag,
            scenario=scenario,
            purpose=MeasurementSession.Purpose.LIVE,
            seed=9191,
            started_at=timezone.now(),
            status=MeasurementSession.Status.RUNNING,
        )

    def test_context_and_provider_run_outside_database_transaction(self):
        transaction_states = []

        def build_context(_session):
            transaction_states.append(("context", connection.in_atomic_block))
            return live_context()

        def generate_content(_context):
            transaction_states.append(("provider", connection.in_atomic_block))
            return valid_care_content()

        with (
            patch(
                "analysis.live_care.build_live_care_context",
                side_effect=build_context,
            ),
            patch(
                "analysis.live_care.generate_live_care_content",
                side_effect=generate_content,
            ),
        ):
            outcome = generate_or_get_live_care(self.session)

        self.assertEqual(outcome.state, LiveCareClaimState.READY)
        self.assertEqual(
            transaction_states,
            [("context", False), ("provider", False)],
        )
