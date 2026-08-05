from django.db import models
from products.models import Bag
from simulation.models import SimulationScenario
from django.core.validators import MinValueValidator, MaxValueValidator


class MeasurementSession(models.Model):
    class Purpose(models.TextChoices):
        LIVE = "live", "Live"
        HISTORY = "history", "History"

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
    # -1.0000: 왼쪽에 완전히 집중
    #  0.0000: 좌우 균형
    #  1.0000: 오른쪽에 완전히 집중
    load_bias = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        default=0,
        validators=[
            MinValueValidator(-1),
            MaxValueValidator(1),
        ],
    )

    # 가방 본체의 기준 상태 대비 변형률
    # 예: 0.0000 = 변형 없음, 0.0250 = 2.5% 변형
    body_deformation_ratio = models.DecimalField(
        max_digits=7,
        decimal_places=4,
        default=0,
        validators=[
            MinValueValidator(0),
        ],
    )

    sequence = models.PositiveIntegerField()
    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["session", "sequence"], name="unique_session_sequence"),
        ]
        ordering = ["measured_at"]
    def __str__(self):
        return f"Reading(session={self.session_id}, at={self.measured_at})"
