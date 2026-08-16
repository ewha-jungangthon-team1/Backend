from django.core.exceptions import ValidationError
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
    comparison = models.JSONField(default=dict)
    ai_result = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"AnalysisReport(session={self.session_id}, severity={self.severity})"


class LiveCareResult(models.Model):
    class Status(models.TextChoices):
        GENERATING = "GENERATING", "Generating"
        READY = "READY", "Ready"

    session = models.OneToOneField(
        MeasurementSession,
        on_delete=models.CASCADE,
        related_name="live_care_result",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.GENERATING,
    )
    context_snapshot = models.JSONField(default=dict)
    care_result = models.JSONField(default=dict)
    generated_at = models.DateTimeField(null=True, blank=True)
    fallback_used = models.BooleanField(default=False)
    fallback_reason = models.CharField(
        max_length=100,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        super().clean()
        errors = {}

        if (
            self.session_id is not None
            and self.session.purpose != MeasurementSession.Purpose.LIVE
        ):
            errors["session"] = "LiveCareResult requires a LIVE session."
        if not isinstance(self.context_snapshot, dict):
            errors["context_snapshot"] = "context_snapshot must be a dictionary."
        if not isinstance(self.care_result, dict):
            errors["care_result"] = "care_result must be a dictionary."
        elif self.status == self.Status.READY and not self.care_result:
            errors["care_result"] = "READY care_result must not be empty."
        if self.status == self.Status.READY and self.generated_at is None:
            errors["generated_at"] = "READY generated_at is required."

        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"LiveCareResult(session={self.session_id}, status={self.status})"
