from django.urls import path

from .views import *

urlpatterns = [
    # home 화면 API1
    path("bags/<uuid:public_token>/home/",HomeView.as_view(),name="bag-home"),
]
