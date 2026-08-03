from rest_framework import serializers

from .models import LifecycleRecord


class LifecycleRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = LifecycleRecord
        fields = ["id", "record_type", "description", "recorded_at"]
