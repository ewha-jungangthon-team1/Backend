from copy import deepcopy

from django.db import transaction
from django.utils import timezone

from measurements.models import MeasurementSession

from .comparisons import (
    build_history_metric_comparison,
    find_previous_history_session,
)
from .metrics import calculate_history_metrics
from .models import AnalysisReport
from .rules import evaluate_history_rules


def _as_project_timezone_iso(value):
    project_timezone = timezone.get_default_timezone()
    if timezone.is_naive(value):
        value = timezone.make_aware(value, project_timezone)
    return timezone.localtime(value, project_timezone).isoformat()


def _build_period_snapshot(session):
    return {
        "started_at": _as_project_timezone_iso(session.started_at),
        "ended_at": _as_project_timezone_iso(session.ended_at),
        "timezone": timezone.get_default_timezone_name(),
    }


@transaction.atomic
def analyze_history_session(
    session: MeasurementSession,
) -> tuple[AnalysisReport, bool]:
    care_guideline_snapshot = deepcopy(
        session.bag.product_model.care_guideline
    )
    current_metrics = calculate_history_metrics(
        session,
        care_guideline=care_guideline_snapshot,
    )
    rule_result = evaluate_history_rules(
        current_metrics,
        care_guideline_snapshot,
    )

    selection = find_previous_history_session(session)
    if selection.is_available:
        previous_session = selection.previous_session
        previous_metrics = calculate_history_metrics(
            previous_session,
            care_guideline=care_guideline_snapshot,
        )
        comparison_snapshot = {
            "available": True,
            "reason": None,
            "previous_session_id": previous_session.pk,
            "previous_period": _build_period_snapshot(previous_session),
            "metrics": build_history_metric_comparison(
                current_metrics,
                previous_metrics,
            ),
        }
    else:
        comparison_snapshot = {
            "available": False,
            "reason": selection.reason.value,
            "previous_session_id": None,
            "previous_period": None,
            "metrics": None,
        }

    report, created = AnalysisReport.objects.update_or_create(
        session=session,
        defaults={
            "metrics": current_metrics,
            "severity": rule_result["severity"],
            "active_rules": rule_result["active_rules"],
            "unavailable_rules": rule_result["unavailable_rules"],
            "care_guideline_snapshot": care_guideline_snapshot,
            "comparison": comparison_snapshot,
        },
    )
    return report, created
