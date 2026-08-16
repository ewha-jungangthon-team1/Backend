from rest_framework import serializers
from .models import *

class HomeSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductModel
        fields = ["model_image", "model_name"]


class ProductModelSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductModel
        fields = ["model_image", "model_name", "material", "care_guideline"]


class ProductSummarySerializer(serializers.ModelSerializer):
    image_url = serializers.ImageField(
        source="model_image",
        read_only=True,
        allow_null=True,
    )

    class Meta:
        model = ProductModel
        fields = ["brand", "model_name", "material", "image_url"]


class BagListSerializer(serializers.ModelSerializer):
    product = ProductSummarySerializer(
        source="product_model",
        read_only=True,
    )

    class Meta:
        model = Bag
        fields = ["public_token", "product"]


class BagDetailSerializer(serializers.ModelSerializer):
    product_model = ProductModelSerializer(read_only=True)
    class Meta:
        model = Bag
        fields = ["public_token", "created_at", "product_model"]

class LifecycleRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = LifecycleRecord
        fields = ["id", "record_type", "description", "recorded_at"]
