from django.urls import path

from .views import *

urlpatterns = [
    path("bags/<uuid:public_token>/home/",HomeView.as_view(),name="bag-home"),
    path("<uuid:public_token>/sessions/", SessionListView.as_view(), name="session-list"),
    path("<uuid:public_token>/sessions/<int:session_id>/", SessionDetailView.as_view(), name="session-detail"),
]
