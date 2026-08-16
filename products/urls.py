from django.urls import path

from .views import BagListView

urlpatterns = [
    path("bags/", BagListView.as_view(), name="bag-list"),
]
