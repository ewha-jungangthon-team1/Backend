from django.contrib import admin

from .models import MeasurementSession, SensorReading


@admin.register(MeasurementSession)
class MeasurementSessionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "bag",
        "scenario",
        "purpose",
        "status",
        "started_at",
        "ended_at",
    )
    list_filter = ("purpose", "status")
    search_fields = ("bag__public_token",)


@admin.register(SensorReading)
class SensorReadingAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "session",
        "strap_load",
        "strap_strain",
        "humidity",
        "temperature",
        "moisture_detected",
        "measured_at",
    )
    list_filter = ("moisture_detected",)
    search_fields = ("session__id",)
