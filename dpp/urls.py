from django.urls import path

from .views import LifecycleListView

urlpatterns = [
    path("<uuid:public_token>/lifecycle/", LifecycleListView.as_view(), name="lifecycle-list"),
]
