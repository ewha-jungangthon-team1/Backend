from django.conf import settings
from django.db import models

class SimulationScenario(models.Model):
    class ScenarioType(models.TextChoices):
        NORMAL = "NORMAL", "Normal"
        OVERLOAD = "OVERLOAD", "Overload"
        HIGH_TEMPERATURE = "HIGH_TEMPERATURE", "High Temperature"
        HIGH_HUMIDITY = "HIGH_HUMIDITY", "High Humidity"
        COMPOSITE_RISK = "COMPOSITE_RISK", "Composite Risk"

    class Mode(models.TextChoices):
        LIVE = "LIVE", "Live"
        HISTORY = "HISTORY", "History"

    code = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=100)
    scenario_type = models.CharField(max_length=30, choices=ScenarioType.choices)
    mode = models.CharField(max_length=20, choices=Mode.choices)
    logical_duration_seconds = models.IntegerField()
    sample_interval_seconds = models.IntegerField()
    config = models.JSONField()
    version = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.code} (v{self.version})"
