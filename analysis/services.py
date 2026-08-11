from copy import deepcopy

from django.db import transaction

from measurements.models import MeasurementSession

from .metrics import calculate_history_metrics
from .models import AnalysisReport
from .rules import evaluate_history_rules


@transaction.atomic
def analyze_history_session(
    session: MeasurementSession,
) -> tuple[AnalysisReport, bool]:
    care_guideline_snapshot = deepcopy(
        session.bag.product_model.care_guideline
    )
    metrics = calculate_history_metrics(session)
    rule_result = evaluate_history_rules(metrics, care_guideline_snapshot)

    report, created = AnalysisReport.objects.update_or_create(
        session=session,
        defaults={
            "metrics": metrics,
            "severity": rule_result["severity"],
            "active_rules": rule_result["active_rules"],
            "unavailable_rules": rule_result["unavailable_rules"],
            "care_guideline_snapshot": care_guideline_snapshot,
        },
    )
    return report, created
