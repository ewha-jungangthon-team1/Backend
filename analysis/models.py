from django.db import models

from measurements.models import MeasurementSession

from .constants import Severity


class AnalysisReport(models.Model):
    session = models.OneToOneField(
        MeasurementSession,
        on_delete=models.CASCADE,
        related_name="analysis_report",
    )
    metrics = models.JSONField()
    severity = models.CharField(
        max_length=20,
        choices=[(severity.value, severity.value) for severity in Severity],
    )
    active_rules = models.JSONField(default=list)
    unavailable_rules = models.JSONField(default=list)
    care_guideline_snapshot = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"AnalysisReport(session={self.session_id}, severity={self.severity})"
