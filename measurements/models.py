from django.db import models
from products.models import Bag
from simulation.models import SimulationScenario


class MeasurementSession(models.Model):
    class Purpose(models.TextChoices):
        LIVE = "live", "Live"
        HISTORY = "history", "History"
        DEMO = "demo", "Demo"
        TEST = "test", "Test"

    class Status(models.TextChoices):
        RUNNING = "RUNNING", "Running"
        COMPLETED = "COMPLETED", "Completed"

    bag = models.ForeignKey(Bag, on_delete=models.CASCADE, related_name="sessions")
    scenario = models.ForeignKey(
        SimulationScenario,
        on_delete=models.SET_NULL,
        related_name="sessions",
        null=True,
        blank=True,
    )
    purpose = models.CharField(max_length=20, choices=Purpose.choices)
    seed = models.BigIntegerField()
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices)

    def __str__(self):
        return f"Session({self.id}, {self.purpose}, {self.status})"


class SensorReading(models.Model):
    session = models.ForeignKey(MeasurementSession, on_delete=models.CASCADE, related_name="readings")
    strap_load = models.DecimalField(max_digits=6, decimal_places=2)
    strap_strain = models.DecimalField(max_digits=6, decimal_places=4)
    humidity = models.DecimalField(max_digits=5, decimal_places=2)
    moisture_detected = models.BooleanField(default=False)
    temperature = models.DecimalField(max_digits=5, decimal_places=2)
    measured_at = models.DateTimeField()

    def __str__(self):
        return f"Reading(session={self.session_id}, at={self.measured_at})"
