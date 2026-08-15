from copy import deepcopy
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from measurements.models import MeasurementSession
from products.models import Bag, ProductModel
from simulation.models import SimulationScenario

from .live_care import (
    LiveCareClaimState,
    claim_live_care_generation,
    finalize_live_care_result,
    get_ready_live_care_result,
)
from .models import LiveCareResult


class LiveCarePersistenceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        owner = get_user_model().objects.create_user(
            username="live-care-owner",
            password="test-password",
        )
        product = ProductModel.objects.create(
            brand="Test Brand",
            model_name="Live Care Bag",
            material="Leather",
            care_guideline={},
        )
        cls.bag = Bag.objects.create(
            product_model=product,
            owner=owner,
            nfc_uid="LIVE-CARE-NFC",
        )
        cls.scenario = SimulationScenario.objects.create(
            code="LIVE_CARE_SCENARIO",
            name="Live Care Scenario",
            scenario_type=SimulationScenario.ScenarioType.NORMAL,
            mode=SimulationScenario.Mode.LIVE,
            logical_duration_seconds=86400,
            sample_interval_seconds=3600,
            config={},
            version=1,
            is_active=True,
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
            seed=12345,
            started_at=now,
            ended_at=(
                now if status == MeasurementSession.Status.COMPLETED else None
            ),
            status=status,
        )

    def finalize(self, result, *, marker="first", fallback=False):
        return finalize_live_care_result(
            result,
            context_snapshot={"marker": marker, "active_rules": []},
            care_result={"steps": [{"marker": marker}]},
            fallback_used=fallback,
            fallback_reason="OPENAI_TIMEOUT" if fallback else None,
        )

    def test_live_session_claim_creates_one_generating_row(self):
        session = self.create_session()

        claim = claim_live_care_generation(session)

        self.assertEqual(claim.state, LiveCareClaimState.CLAIMED)
        self.assertEqual(claim.result.status, LiveCareResult.Status.GENERATING)
        self.assertEqual(claim.result.context_snapshot, {})
        self.assertEqual(claim.result.care_result, {})
        self.assertIsNone(claim.result.generated_at)
        self.assertEqual(LiveCareResult.objects.filter(session=session).count(), 1)

    def test_reclaim_returns_existing_generating_row_without_duplicate(self):
        session = self.create_session()
        first = claim_live_care_generation(session)

        second = claim_live_care_generation(session)

        self.assertEqual(second.state, LiveCareClaimState.GENERATING)
        self.assertEqual(second.result.pk, first.result.pk)
        self.assertEqual(LiveCareResult.objects.filter(session=session).count(), 1)

    def test_ready_reclaim_reuses_immutable_result(self):
        session = self.create_session()
        claim = claim_live_care_generation(session)
        ready, finalized = self.finalize(claim.result)
        original = {
            "context": deepcopy(ready.context_snapshot),
            "care": deepcopy(ready.care_result),
            "generated_at": ready.generated_at,
            "fallback_used": ready.fallback_used,
            "fallback_reason": ready.fallback_reason,
            "updated_at": ready.updated_at,
        }

        reclaimed = claim_live_care_generation(session)
        ready_lookup = get_ready_live_care_result(session)

        self.assertTrue(finalized)
        self.assertEqual(reclaimed.state, LiveCareClaimState.READY)
        self.assertEqual(reclaimed.result.pk, ready.pk)
        self.assertEqual(ready_lookup.pk, ready.pk)
        self.assertEqual(reclaimed.result.context_snapshot, original["context"])
        self.assertEqual(reclaimed.result.care_result, original["care"])
        self.assertEqual(reclaimed.result.updated_at, original["updated_at"])

    def test_one_to_one_constraint_rejects_second_row_for_session(self):
        session = self.create_session()
        LiveCareResult.objects.create(session=session)

        with self.assertRaises(IntegrityError), transaction.atomic():
            LiveCareResult.objects.create(session=session)

        self.assertEqual(LiveCareResult.objects.filter(session=session).count(), 1)

    def test_history_session_claim_is_rejected(self):
        session = self.create_session(
            purpose=MeasurementSession.Purpose.HISTORY,
            status=MeasurementSession.Status.COMPLETED,
        )

        with self.assertRaisesMessage(ValueError, "Only LIVE sessions"):
            claim_live_care_generation(session)

        self.assertFalse(LiveCareResult.objects.filter(session=session).exists())

    def test_running_and_completed_live_sessions_are_allowed(self):
        for status in (
            MeasurementSession.Status.RUNNING,
            MeasurementSession.Status.COMPLETED,
        ):
            with self.subTest(status=status):
                session = self.create_session(status=status)

                claim = claim_live_care_generation(session)

                self.assertEqual(claim.state, LiveCareClaimState.CLAIMED)
                self.assertEqual(claim.result.session_id, session.pk)

    def test_finalize_stores_ready_snapshot_and_fallback_metadata(self):
        session = self.create_session()
        claim = claim_live_care_generation(session)

        ready, finalized = self.finalize(
            claim.result,
            marker="fallback",
            fallback=True,
        )

        self.assertTrue(finalized)
        self.assertEqual(ready.status, LiveCareResult.Status.READY)
        self.assertEqual(
            ready.context_snapshot,
            {"marker": "fallback", "active_rules": []},
        )
        self.assertEqual(
            ready.care_result,
            {"steps": [{"marker": "fallback"}]},
        )
        self.assertIsNotNone(ready.generated_at)
        self.assertIs(timezone.is_aware(ready.generated_at), True)
        self.assertIs(ready.fallback_used, True)
        self.assertEqual(ready.fallback_reason, "OPENAI_TIMEOUT")

        ready.refresh_from_db()
        self.assertEqual(ready.status, LiveCareResult.Status.READY)
        self.assertEqual(ready.fallback_reason, "OPENAI_TIMEOUT")

    def test_second_finalize_does_not_overwrite_ready_result(self):
        session = self.create_session()
        claim = claim_live_care_generation(session)
        ready, _finalized = self.finalize(claim.result, marker="original")
        original_updated_at = ready.updated_at

        same_ready, finalized = finalize_live_care_result(
            claim.result,
            context_snapshot={"marker": "replacement"},
            care_result={"steps": [{"marker": "replacement"}]},
            fallback_used=True,
            fallback_reason="REPLACEMENT",
        )

        self.assertFalse(finalized)
        self.assertEqual(same_ready.context_snapshot["marker"], "original")
        self.assertEqual(same_ready.care_result["steps"][0]["marker"], "original")
        self.assertIs(same_ready.fallback_used, False)
        self.assertIsNone(same_ready.fallback_reason)
        self.assertEqual(same_ready.updated_at, original_updated_at)

    def test_integrity_error_loser_reloads_single_competing_row(self):
        session = self.create_session()
        competing = LiveCareResult.objects.create(session=session)
        empty_lookup = Mock()
        empty_lookup.first.return_value = None

        with (
            patch(
                "analysis.live_care.LiveCareResult.objects.filter",
                return_value=empty_lookup,
            ),
            patch(
                "analysis.live_care.LiveCareResult.objects.create",
                side_effect=IntegrityError("simulated unique race"),
            ),
        ):
            claim = claim_live_care_generation(session)

        self.assertEqual(claim.state, LiveCareClaimState.GENERATING)
        self.assertEqual(claim.result.pk, competing.pk)
        self.assertEqual(LiveCareResult.objects.filter(session=session).count(), 1)

    def test_new_claim_requires_scenario_but_ready_cache_survives_scenario_removal(self):
        no_scenario = self.create_session(scenario=False)
        with self.assertRaisesMessage(ValueError, "scenario is required"):
            claim_live_care_generation(no_scenario)

        session = self.create_session()
        claim = claim_live_care_generation(session)
        ready, _finalized = self.finalize(claim.result)
        session.scenario = None
        session.status = MeasurementSession.Status.COMPLETED
        session.save(update_fields=["scenario", "status"])

        reclaimed = claim_live_care_generation(session)

        self.assertEqual(reclaimed.state, LiveCareClaimState.READY)
        self.assertEqual(reclaimed.result.pk, ready.pk)

    def test_model_validation_enforces_live_and_ready_persistence_invariants(self):
        history = self.create_session(
            purpose=MeasurementSession.Purpose.HISTORY,
            status=MeasurementSession.Status.COMPLETED,
        )
        with self.assertRaises(ValidationError):
            LiveCareResult(session=history).full_clean()

        live = self.create_session()
        invalid_ready = LiveCareResult(
            session=live,
            status=LiveCareResult.Status.READY,
            context_snapshot=[],
            care_result={},
        )
        with self.assertRaises(ValidationError) as raised:
            invalid_ready.full_clean()

        self.assertEqual(
            set(raised.exception.message_dict),
            {"context_snapshot", "care_result", "generated_at"},
        )
