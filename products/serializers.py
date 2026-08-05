from rest_framework import serializers
from .models import Bag, ProductModel


class ProductModelSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductModel
        fields = ["model_image", "model_name", "material", "care_guideline"]


class BagDetailSerializer(serializers.ModelSerializer):
    product_model = ProductModelSerializer(read_only=True)
    class Meta:
        model = Bag
        fields = ["public_token", "created_at", "product_model"]
