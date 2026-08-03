from django.urls import path

from .views import BagDetailView

urlpatterns = [
    path("passports/<uuid:public_token>/", BagDetailView.as_view(), name="bag-detail"),
]
