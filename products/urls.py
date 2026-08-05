from django.urls import path

from .views import PassportDetailView

urlpatterns = [
    path("passports/<uuid:public_token>/", PassportDetailView.as_view(), name="passport_detail"),
]
