from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
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
            include_in_report=True,
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


def _as_decimal(value, metric_path):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{metric_path} must be numeric or None.")

    decimal_value = Decimal(str(value))
    if not decimal_value.is_finite():
        raise ValueError(f"{metric_path} must be a finite number or None.")
    return decimal_value


def _get_required_metric(metrics, domain, metric_name, input_name):
    if domain not in metrics or not isinstance(metrics[domain], Mapping):
        raise ValueError(f"{input_name}.{domain} must be a mapping.")
    if metric_name not in metrics[domain]:
        raise ValueError(
            f"Missing required metric: {input_name}.{domain}.{metric_name}."
        )

    value = metrics[domain][metric_name]
    if value is not None:
        _as_decimal(value, f"{input_name}.{domain}.{metric_name}")
    return value


def _round_decimal(value):
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _serialize_comparison_value(value, *, count_value):
    if value is None:
        return None

    decimal_value = Decimal(str(value))
    if count_value:
        if decimal_value != decimal_value.to_integral_value():
            raise ValueError("Count metrics must contain integer values.")
        return int(decimal_value)
    return _round_decimal(decimal_value)


def _build_numeric_comparison(
    current,
    previous,
    *,
    allow_relative_change=True,
    count_value=False,
):
    serialized_current = _serialize_comparison_value(
        current,
        count_value=count_value,
    )
    serialized_previous = _serialize_comparison_value(
        previous,
        count_value=count_value,
    )
    if current is None or previous is None:
        return {
            "current": serialized_current,
            "previous": serialized_previous,
            "change": None,
            "change_percent": None,
        }

    current_decimal = Decimal(str(current))
    previous_decimal = Decimal(str(previous))
    change = current_decimal - previous_decimal

    if not allow_relative_change:
        change_percent = None
    elif previous_decimal == 0:
        change_percent = Decimal("0") if current_decimal == 0 else None
    else:
        change_percent = (change / abs(previous_decimal)) * 100

    return {
        "current": serialized_current,
        "previous": serialized_previous,
        "change": _serialize_comparison_value(
            change,
            count_value=count_value,
        ),
        "change_percent": (
            _round_decimal(change_percent)
            if change_percent is not None
            else None
        ),
    }


def _get_comparison(
    current_metrics,
    previous_metrics,
    domain,
    metric_name,
    *,
    output_name=None,
    allow_relative_change=True,
    count_value=False,
    convert_ratio_to_percent=False,
):
    current = _get_required_metric(
        current_metrics,
        domain,
        metric_name,
        "current_metrics",
    )
    previous = _get_required_metric(
        previous_metrics,
        domain,
        metric_name,
        "previous_metrics",
    )
    if convert_ratio_to_percent:
        current = Decimal(str(current)) * 100 if current is not None else None
        previous = Decimal(str(previous)) * 100 if previous is not None else None

    return (
        output_name or metric_name,
        _build_numeric_comparison(
            current,
            previous,
            allow_relative_change=allow_relative_change,
            count_value=count_value,
        ),
    )


def build_history_metric_comparison(current_metrics, previous_metrics):
    if not isinstance(current_metrics, Mapping):
        raise ValueError("current_metrics must be a mapping.")
    if not isinstance(previous_metrics, Mapping):
        raise ValueError("previous_metrics must be a mapping.")

    return {
        "load": dict(
            [
                _get_comparison(
                    current_metrics,
                    previous_metrics,
                    "load",
                    "average_kg",
                ),
                _get_comparison(
                    current_metrics,
                    previous_metrics,
                    "load",
                    "max_kg",
                ),
                _get_comparison(
                    current_metrics,
                    previous_metrics,
                    "load",
                    "overload_detected_days",
                    count_value=True,
                ),
            ]
        ),
        "temperature": dict(
            [
                _get_comparison(
                    current_metrics,
                    previous_metrics,
                    "temperature",
                    "average_c",
                    allow_relative_change=False,
                ),
                _get_comparison(
                    current_metrics,
                    previous_metrics,
                    "temperature",
                    "max_c",
                    allow_relative_change=False,
                ),
                _get_comparison(
                    current_metrics,
                    previous_metrics,
                    "temperature",
                    "high_temperature_detected_days",
                    count_value=True,
                ),
            ]
        ),
        "humidity": dict(
            [
                _get_comparison(
                    current_metrics,
                    previous_metrics,
                    "humidity",
                    "average_percent",
                ),
                _get_comparison(
                    current_metrics,
                    previous_metrics,
                    "humidity",
                    "max_percent",
                ),
                _get_comparison(
                    current_metrics,
                    previous_metrics,
                    "humidity",
                    "high_humidity_detected_days",
                    count_value=True,
                ),
            ]
        ),
        "moisture": dict(
            [
                _get_comparison(
                    current_metrics,
                    previous_metrics,
                    "moisture",
                    "detected_days",
                    count_value=True,
                )
            ]
        ),
        "load_bias": dict(
            [
                _get_comparison(
                    current_metrics,
                    previous_metrics,
                    "load_bias",
                    "max_absolute",
                    output_name="max_absolute_percent",
                    convert_ratio_to_percent=True,
                ),
                _get_comparison(
                    current_metrics,
                    previous_metrics,
                    "load_bias",
                    "biased_days",
                    count_value=True,
                ),
            ]
        ),
        "deformation": dict(
            [
                _get_comparison(
                    current_metrics,
                    previous_metrics,
                    "deformation",
                    "latest_ratio",
                    output_name="latest_percent",
                    convert_ratio_to_percent=True,
                ),
                _get_comparison(
                    current_metrics,
                    previous_metrics,
                    "deformation",
                    "max_ratio",
                    output_name="max_percent",
                    convert_ratio_to_percent=True,
                ),
                _get_comparison(
                    current_metrics,
                    previous_metrics,
                    "deformation",
                    "deformation_detected_days",
                    count_value=True,
                ),
            ]
        ),
    }
