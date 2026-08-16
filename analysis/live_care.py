from copy import deepcopy
from dataclasses import dataclass
from enum import Enum

from django.db import IntegrityError, transaction
from django.utils import timezone

from measurements.models import MeasurementSession

from .ai.live_care import (
    LiveCareGenerationError,
    build_live_care_fallback,
    generate_live_care_content,
    validate_live_care_content,
)
from .live_care_context import build_live_care_context
from .models import LiveCareResult


class LiveCareClaimState(str, Enum):
    CLAIMED = "CLAIMED"
    GENERATING = "GENERATING"
    READY = "READY"


@dataclass(frozen=True)
class LiveCareClaim:
    state: LiveCareClaimState
    result: LiveCareResult


@dataclass(frozen=True)
class LiveCareGenerationOutcome:
    state: LiveCareClaimState
    result: LiveCareResult
    created: bool


def _validate_persisted_live_session(session):
    if session is None or not isinstance(session, MeasurementSession):
        raise ValueError("session is required.")
    if session.pk is None:
        raise ValueError("session must be saved.")
    if session.purpose != MeasurementSession.Purpose.LIVE:
        raise ValueError("Only LIVE sessions can have detailed care results.")


def _claim_from_result(result, *, claimed=False):
    if result.status == LiveCareResult.Status.READY:
        state = LiveCareClaimState.READY
    elif claimed:
        state = LiveCareClaimState.CLAIMED
    else:
        state = LiveCareClaimState.GENERATING
    return LiveCareClaim(state=state, result=result)


def get_ready_live_care_result(session):
    _validate_persisted_live_session(session)
    return LiveCareResult.objects.filter(
        session_id=session.pk,
        status=LiveCareResult.Status.READY,
    ).first()


def claim_live_care_generation(session):
    _validate_persisted_live_session(session)

    existing = LiveCareResult.objects.filter(session_id=session.pk).first()
    if existing is not None:
        return _claim_from_result(existing)

    if session.scenario_id is None:
        raise ValueError("A scenario is required to generate detailed LIVE care.")

    try:
        with transaction.atomic():
            result = LiveCareResult.objects.create(
                session=session,
                status=LiveCareResult.Status.GENERATING,
            )
    except IntegrityError as error:
        try:
            result = LiveCareResult.objects.get(session_id=session.pk)
        except LiveCareResult.DoesNotExist:
            raise error
        return _claim_from_result(result)

    return _claim_from_result(result, claimed=True)


def finalize_live_care_result(
    result,
    *,
    context_snapshot,
    care_result,
    fallback_used=False,
    fallback_reason=None,
    generated_at=None,
):
    if not isinstance(result, LiveCareResult) or result.pk is None:
        raise ValueError("A saved LiveCareResult is required.")

    with transaction.atomic():
        stored = LiveCareResult.objects.select_for_update().get(pk=result.pk)
        if stored.status == LiveCareResult.Status.READY:
            return stored, False

        if not isinstance(context_snapshot, dict):
            raise ValueError("context_snapshot must be a dictionary.")
        if not isinstance(care_result, dict) or not care_result:
            raise ValueError("care_result must be a non-empty dictionary.")
        if not isinstance(fallback_used, bool):
            raise ValueError("fallback_used must be a boolean.")
        if fallback_reason is not None and not isinstance(fallback_reason, str):
            raise ValueError("fallback_reason must be a string or None.")

        stored.context_snapshot = deepcopy(context_snapshot)
        stored.care_result = deepcopy(care_result)
        stored.generated_at = generated_at or timezone.now()
        stored.fallback_used = fallback_used
        stored.fallback_reason = fallback_reason
        stored.status = LiveCareResult.Status.READY
        stored.full_clean()
        stored.save(
            update_fields=[
                "context_snapshot",
                "care_result",
                "generated_at",
                "fallback_used",
                "fallback_reason",
                "status",
                "updated_at",
            ]
        )
        return stored, True


def release_live_care_generation(result):
    if not isinstance(result, LiveCareResult) or result.pk is None:
        raise ValueError("A saved LiveCareResult is required.")

    deleted_count, _deleted_by_model = LiveCareResult.objects.filter(
        pk=result.pk,
        status=LiveCareResult.Status.GENERATING,
    ).delete()
    return deleted_count == 1


def generate_or_get_live_care(session):
    claim = claim_live_care_generation(session)
    if claim.state != LiveCareClaimState.CLAIMED:
        return LiveCareGenerationOutcome(
            state=claim.state,
            result=claim.result,
            created=False,
        )

    try:
        context = build_live_care_context(session)
        try:
            care_result = generate_live_care_content(context)
        except LiveCareGenerationError as error:
            care_result = validate_live_care_content(
                build_live_care_fallback(context)
            )
            fallback_used = True
            fallback_reason = error.reason
        else:
            fallback_used = False
            fallback_reason = None

        ready, finalized = finalize_live_care_result(
            claim.result,
            context_snapshot=context,
            care_result=care_result,
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
        )
    except Exception:
        release_live_care_generation(claim.result)
        raise

    return LiveCareGenerationOutcome(
        state=LiveCareClaimState.READY,
        result=ready,
        created=finalized,
    )
