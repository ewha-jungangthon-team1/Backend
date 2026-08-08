from rest_framework import serializers

from .models import MeasurementSession
from .services import POLLING_INTERVAL_SECONDS, calculate_scheduled_end_at


class LiveSessionSerializer(serializers.ModelSerializer):
    """
    API 2(라이브 세션 확보) 응답 형식.
    created / polling_interval_seconds / scheduled_end_at는 DB 컬럼이 아니라
    그때그때 계산해서 채워 넣는 값이다.
    """

    session_id = serializers.IntegerField(source="id", read_only=True)
    created = serializers.SerializerMethodField()
    polling_interval_seconds = serializers.SerializerMethodField()
    scheduled_end_at = serializers.SerializerMethodField()

    class Meta:
        model = MeasurementSession
        fields = [
            "session_id",
            "status",
            "created",
            "polling_interval_seconds",
            "started_at",
            "scheduled_end_at",
        ]

    def get_created(self, obj):
        return self.context.get("created", False)

    def get_polling_interval_seconds(self, obj):
        return POLLING_INTERVAL_SECONDS

    def get_scheduled_end_at(self, obj):
        return calculate_scheduled_end_at(obj)
