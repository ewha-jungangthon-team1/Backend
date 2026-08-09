from django.contrib import admin
from .models import *

admin.site.register(ProductModel)
admin.site.register(Bag)
admin.site.register(LifecycleRecord)