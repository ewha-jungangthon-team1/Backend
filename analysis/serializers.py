from copy import deepcopy
from decimal import Decimal

from django.utils import timezone
from django.utils.timezone import localdate as get_local_date
from rest_framework import serializers

from .models import AnalysisReport
from .presentation import (
    build_current_history_display_period,
    build_previous_history_display_period,
    project_history_daily_series,
)


def _as_json_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    return float(value)


def _as_project_timezone_iso(value):
    if value is None:
        return None

    project_timezone = timezone.get_default_timezone()
    if timezone.is_naive(value):
        value = timezone.make_aware(value, project_timezone)
    return timezone.localtime(value, project_timezone).isoformat()


class AnalysisReportSerializer(serializers.ModelSerializer):
    session_id = serializers.IntegerField(read_only=True)
    scenario_code = serializers.CharField(
        source="session.scenario.code",
        read_only=True,
        allow_null=True,
    )
    period = serializers.SerializerMethodField()
    display_period = serializers.SerializerMethodField()
    metrics = serializers.SerializerMethodField()
    chart_references = serializers.SerializerMethodField()
    comparison = serializers.SerializerMethodField()

    class Meta:
        model = AnalysisReport
        fields = [
            "id",
            "session_id",
            "scenario_code",
            "period",
            "display_period",
            "metrics",
            "chart_references",
            "comparison",
            "ai_result",
            "severity",
            "active_rules",
            "unavailable_rules",
            "care_guideline_snapshot",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_period(self, obj):
        return {
            "started_at": _as_project_timezone_iso(obj.session.started_at),
            "ended_at": _as_project_timezone_iso(obj.session.ended_at),
            "timezone": timezone.get_default_timezone_name(),
        }

    def _get_display_reference_date(self):
        if not hasattr(self, "_display_reference_date"):
            self._display_reference_date = get_local_date()
        return self._display_reference_date

    def get_display_period(self, obj):
        return build_current_history_display_period(
            self._get_display_reference_date()
        )

    def get_metrics(self, obj):
        metrics = deepcopy(obj.metrics)
        if not isinstance(metrics, dict) or "daily_series" not in metrics:
            return metrics

        metrics["daily_series"] = project_history_daily_series(
            metrics["daily_series"],
            self._get_display_reference_date(),
        )
        return metrics

    def get_comparison(self, obj):
        comparison = deepcopy(obj.comparison)
        if not isinstance(comparison, dict):
            return comparison

        comparison["display_previous_period"] = (
            build_previous_history_display_period(
                self._get_display_reference_date()
            )
        )
        return comparison

    def get_chart_references(self, obj):
        snapshot = obj.care_guideline_snapshot
        if not isinstance(snapshot, dict):
            snapshot = {}

        max_load_kg = _as_json_number(snapshot.get("max_load_kg"))
        max_deformation_ratio = _as_json_number(
            snapshot.get("max_body_deformation_ratio")
        )
        avoid_moisture = snapshot.get("avoid_moisture")
        if not isinstance(avoid_moisture, bool):
            avoid_moisture = None

        return {
            "max_load_kg": max_load_kg,
            "max_body_deformation_ratio": max_deformation_ratio,
            "max_body_deformation_percent": (
                float(Decimal(str(max_deformation_ratio)) * 100)
                if max_deformation_ratio is not None
                else None
            ),
            "avoid_moisture": avoid_moisture,
        }
