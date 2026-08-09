from django.contrib import admin

from .models import MeasurementSession, SensorReading

admin.site.register(MeasurementSession)
@admin.register(SensorReading)
class SensorReadingAdmin(admin.ModelAdmin):
    ordering = ("session_id", "measured_at")
