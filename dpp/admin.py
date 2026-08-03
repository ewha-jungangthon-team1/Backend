from django.contrib import admin
from .models import LifecycleRecord


@admin.register(LifecycleRecord)
class LifecycleRecordAdmin(admin.ModelAdmin):
    list_display = ("id", "bag", "record_type", "recorded_at")
    list_filter = ("record_type",)
    search_fields = ("bag__public_token", "description")
