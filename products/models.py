import uuid
from django.conf import settings
from django.db import models

class ProductModel(models.Model):
    brand = models.CharField(max_length=100)
    model_image = models.ImageField(upload_to="ProductModel/image", blank=True, null=True)
    model_name = models.CharField(max_length=100)
    material = models.CharField(max_length=100)
    care_guideline = models.JSONField()

    def __str__(self):
        return f"{self.brand} {self.model_name}"


class Bag(models.Model):
    product_model = models.ForeignKey(ProductModel, on_delete=models.CASCADE, related_name="bags")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="bags")
    serial_number = models.CharField(max_length=100, unique=True, blank=True, null=True)
    nfc_uid = models.CharField(max_length=100,unique=True)
    public_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    manufactured_at = models.DateField(blank=True, null=True)
    purchased_at = models.DateField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Bag({self.public_token})"
