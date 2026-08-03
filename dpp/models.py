from django.db import models

from products.models import Bag


class LifecycleRecord(models.Model):
    bag = models.ForeignKey(Bag, on_delete=models.CASCADE, related_name="lifecycle_records")
    record_type = models.CharField(max_length=50)
    description = models.TextField()
    metadata = models.JSONField(blank=True, default=dict)
    recorded_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.record_type} - Bag({self.bag_id})"
