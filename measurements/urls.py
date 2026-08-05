from django.urls import path

from .views import SessionDetailView, SessionListView

urlpatterns = [
    path("<uuid:public_token>/sessions/", SessionListView.as_view(), name="session-list"),
    path("<uuid:public_token>/sessions/<int:session_id>/", SessionDetailView.as_view(), name="session-detail"),
]
