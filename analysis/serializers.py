from decimal import Decimal

from django.utils import timezone
from rest_framework import serializers

from .models import AnalysisReport


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
    chart_references = serializers.SerializerMethodField()

    class Meta:
        model = AnalysisReport
        fields = [
            "id",
            "session_id",
            "scenario_code",
            "period",
            "metrics",
            "chart_references",
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
