from rest_framework import serializers

from .models import MeasurementSession


class MeasurementSessionSerializer(serializers.ModelSerializer):
    class Meta:
        model = MeasurementSession
        fields = ["id", "purpose", "seed", "started_at", "ended_at", "status"]
