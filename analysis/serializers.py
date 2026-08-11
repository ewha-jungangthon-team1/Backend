from rest_framework import serializers

from .models import AnalysisReport


class AnalysisReportSerializer(serializers.ModelSerializer):
    session_id = serializers.IntegerField(read_only=True)
    scenario_code = serializers.CharField(
        source="session.scenario.code",
        read_only=True,
        allow_null=True,
    )

    class Meta:
        model = AnalysisReport
        fields = [
            "id",
            "session_id",
            "scenario_code",
            "metrics",
            "severity",
            "active_rules",
            "unavailable_rules",
            "care_guideline_snapshot",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields
