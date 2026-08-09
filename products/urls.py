from django.urls import path

from .views import *

urlpatterns = [
    path("passports/<uuid:public_token>/", PassportDetailView.as_view(), name="passport_detail"),
    path("<uuid:public_token>/lifecycle/", LifecycleListView.as_view(), name="lifecycle-list"),
]
