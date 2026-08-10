from rest_framework import serializers
from products.serializers import HomeSerializer
from simulation.services import POLLING_INTERVAL_SECONDS, calculate_scheduled_end_at, get_latest_reading
from .models import MeasurementSession, SensorReading
from .home import (
    build_smart_material_points,
    get_ai_summary_placeholder,
    get_priority_care_placeholder,
)

class MeasurementSessionSerializer(serializers.ModelSerializer):
    class Meta:
        model = MeasurementSession
        fields = ["id", "purpose", "seed", "started_at", "ended_at", "status"]

class SensorReadingSerializer(serializers.ModelSerializer):
    """SensorReading 원본 필드를 그대로 노출하는 범용 serializer (여러 API에서 재사용 가능)."""

    class Meta:
        model = SensorReading
        fields = [
            "sequence",
            "measured_at",
            "strap_load",
            "humidity",
            "temperature",
            "moisture_detected",
            "load_bias",
            "body_deformation_ratio",
        ]


class LiveSessionSerializer(serializers.ModelSerializer):
    """
    API 1(라이브 세션 확보) 응답 형식.
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

class SmartMaterialPointSerializer(serializers.Serializer):
    """스마트소재 감지 포인트 1개 (형태편차/좌우하중/고온노출 등)."""
 
    position = serializers.CharField()
    label = serializers.CharField()
    value = serializers.CharField()
 
 
# TODO(B): 아래 두 serializer는 임시로 여기 있는 것.
# B가 ai_analysis 앱을 만들면 그쪽 serializers.py로 옮기고,
# 여기서는 `from ai_analysis.serializers import AISummarySerializer, PriorityCareSerializer`로
# 가져다 쓰면 됨. 필드 이름(status_text, reasoning, summary, link)은 유지할 것.
class AISummarySerializer(serializers.Serializer):
    status_text = serializers.CharField(allow_null=True)
    reasoning = serializers.CharField(allow_null=True)
 
 
class PriorityCareSerializer(serializers.Serializer):
    summary = serializers.CharField(allow_null=True)
    link = serializers.CharField(allow_null=True)
 
 
class MeasurementSessionHomeSerializer(serializers.ModelSerializer):
    """
    홈 화면 응답을 조립하는 최상위 serializer.
    본체(MeasurementSession)가 이 앱(measurements)에 있으므로 조립도 여기서 담당한다.
      - product           -> products 앱의 HomeSerializer
      - smart_material_points -> .home의 계산 함수 (생성기가 만든 데이터를 가공)
      - ai_summary / priority_care -> (지금은 placeholder, 나중에 B의 앱으로 교체)
    """
 
    checked_at = serializers.SerializerMethodField()
    product = serializers.SerializerMethodField()
    smart_material_points = serializers.SerializerMethodField()
    ai_summary = serializers.SerializerMethodField()
    priority_care = serializers.SerializerMethodField()
 
    class Meta:
        model = MeasurementSession
        fields = ["checked_at", "product", "smart_material_points", "ai_summary", "priority_care"]
 
    def get_checked_at(self, obj):
        latest_raw = obj.readings.order_by("-sequence").first()
        return latest_raw.measured_at if latest_raw else obj.started_at
 
    def get_product(self, obj):
        return HomeSerializer(obj.bag.product_model, context=self.context).data
 
    def get_smart_material_points(self, obj):
        latest_reading = get_latest_reading(obj)
        if latest_reading is None:
            return []
        return build_smart_material_points(obj, latest_reading)
 
    def get_ai_summary(self, obj):
        return get_ai_summary_placeholder(obj)
 
    def get_priority_care(self, obj):
        return get_priority_care_placeholder(obj)
 