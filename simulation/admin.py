from django.contrib import admin

from .models import SimulationScenario


@admin.register(SimulationScenario)
class SimulationScenarioAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "code",
        "name",
        "scenario_type",
        "mode",
        "version",
        "is_active",
    )
    list_filter = ("scenario_type", "mode", "is_active")
    search_fields = ("code", "name")
    readonly_fields = ("created_at",)
