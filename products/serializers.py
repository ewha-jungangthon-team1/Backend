from rest_framework import serializers
from .models import *


class ProductModelSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductModel
        fields = ["model_image", "model_name", "material", "care_guideline"]


class BagDetailSerializer(serializers.ModelSerializer):
    product_model = ProductModelSerializer(read_only=True)
    class Meta:
        model = Bag
        fields = ["public_token", "created_at", "product_model"]

class LifecycleRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = LifecycleRecord
        fields = ["id", "record_type", "description", "recorded_at"]