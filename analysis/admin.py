from django.contrib import admin

from .models import AnalysisReport


@admin.register(AnalysisReport)
class AnalysisReportAdmin(admin.ModelAdmin):
    list_display = ["id", "session", "severity", "created_at", "updated_at"]
