from dataclasses import dataclass
from datetime import timedelta
from enum import Enum

from django.utils import timezone

from measurements.models import MeasurementSession


class ComparisonUnavailableReason(str, Enum):
    NO_PREVIOUS_PERIOD = "NO_PREVIOUS_PERIOD"
    AMBIGUOUS_PREVIOUS_PERIOD = "AMBIGUOUS_PREVIOUS_PERIOD"
    INVALID_PERIOD_SHAPE = "INVALID_PERIOD_SHAPE"


@dataclass(frozen=True)
class PreviousHistorySelection:
    previous_session: MeasurementSession | None
    reason: ComparisonUnavailableReason | None

    @property
    def is_available(self):
        return self.previous_session is not None and self.reason is None


def _has_valid_period_shape(session):
    if session.started_at is None or session.ended_at is None:
        return False
    if session.ended_at - session.started_at != timedelta(days=7):
        return False

    readings = list(
        session.readings.order_by("sequence").values_list(
            "sequence",
            "measured_at",
        )
    )
    if len(readings) != 7:
        return False
    if [sequence for sequence, _measured_at in readings] != list(range(7)):
        return False
    if any(
        measured_at < session.started_at or measured_at >= session.ended_at
        for _sequence, measured_at in readings
    ):
        return False

    project_timezone = timezone.get_default_timezone()
    actual_dates = {
        timezone.localdate(measured_at, project_timezone)
        for _sequence, measured_at in readings
    }
    period_start_date = timezone.localdate(
        session.started_at,
        project_timezone,
    )
    expected_dates = {
        period_start_date + timedelta(days=offset)
        for offset in range(7)
    }
    return actual_dates == expected_dates


def find_previous_history_session(current_session):
    if current_session is None:
        raise ValueError("current_session is required.")
    if current_session.purpose != MeasurementSession.Purpose.HISTORY:
        raise ValueError("Only HISTORY sessions can be compared.")
    if current_session.status != MeasurementSession.Status.COMPLETED:
        raise ValueError("Only COMPLETED sessions can be compared.")
    if current_session.started_at is None or current_session.ended_at is None:
        raise ValueError("Comparison sessions require started_at and ended_at.")

    if not _has_valid_period_shape(current_session):
        return PreviousHistorySelection(
            previous_session=None,
            reason=ComparisonUnavailableReason.INVALID_PERIOD_SHAPE,
        )

    eligible_sessions = (
        MeasurementSession.objects.filter(
            bag_id=current_session.bag_id,
            purpose=MeasurementSession.Purpose.HISTORY,
            status=MeasurementSession.Status.COMPLETED,
            ended_at__isnull=False,
        )
        .exclude(pk=current_session.pk)
    )

    candidates = list(
        eligible_sessions.filter(ended_at=current_session.started_at)[:2]
    )
    if not candidates:
        return PreviousHistorySelection(
            previous_session=None,
            reason=ComparisonUnavailableReason.NO_PREVIOUS_PERIOD,
        )
    if len(candidates) > 1:
        return PreviousHistorySelection(
            previous_session=None,
            reason=ComparisonUnavailableReason.AMBIGUOUS_PREVIOUS_PERIOD,
        )

    previous_session = candidates[0]
    if not _has_valid_period_shape(previous_session):
        return PreviousHistorySelection(
            previous_session=None,
            reason=ComparisonUnavailableReason.INVALID_PERIOD_SHAPE,
        )

    return PreviousHistorySelection(
        previous_session=previous_session,
        reason=None,
    )
