from django.contrib import admin
from .models import Bag, ProductModel

admin.site.register(ProductModel)
admin.site.register(Bag)