from copy import deepcopy
from datetime import timedelta

from django.utils import timezone


HISTORY_DISPLAY_DAYS = 7


def _build_display_period(reference_date, *, start_offset, end_offset):
    return {
        "start_date": (reference_date - timedelta(days=start_offset)).isoformat(),
        "end_date": (reference_date - timedelta(days=end_offset)).isoformat(),
        "timezone": timezone.get_default_timezone_name(),
    }


def build_current_history_display_period(reference_date):
    return _build_display_period(
        reference_date,
        start_offset=HISTORY_DISPLAY_DAYS,
        end_offset=1,
    )


def build_previous_history_display_period(reference_date):
    return _build_display_period(
        reference_date,
        start_offset=HISTORY_DISPLAY_DAYS * 2,
        end_offset=HISTORY_DISPLAY_DAYS + 1,
    )


def project_history_daily_series(daily_series, reference_date):
    projected_series = deepcopy(daily_series)
    if not isinstance(projected_series, list):
        return projected_series

    is_valid_series = (
        len(projected_series) == HISTORY_DISPLAY_DAYS
        and all(isinstance(item, dict) for item in projected_series)
    )
    if not is_valid_series:
        for item in projected_series:
            if isinstance(item, dict):
                item["display_date"] = None
        return projected_series

    start_date = reference_date - timedelta(days=HISTORY_DISPLAY_DAYS)
    for index, item in enumerate(projected_series):
        item["display_date"] = (start_date + timedelta(days=index)).isoformat()
    return projected_series
