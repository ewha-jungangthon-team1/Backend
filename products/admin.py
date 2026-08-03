from django.contrib import admin

from .models import Bag, ProductModel


@admin.register(ProductModel)
class ProductModelAdmin(admin.ModelAdmin):
    list_display = ("id", "brand", "model_name", "material")
    search_fields = ("brand", "model_name")


@admin.register(Bag)
class BagAdmin(admin.ModelAdmin):
    list_display = ("id", "product_model", "owner", "nfc_uid", "public_token", "created_at")
    search_fields = ("nfc_uid", "public_token")
    readonly_fields = ("public_token", "created_at")
