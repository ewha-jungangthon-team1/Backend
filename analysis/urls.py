from django.urls import path

from . import views


urlpatterns = [
    path(
        "sessions/<int:session_id>/analyze/",
        views.analyze_history_session_view,
        name="analyze-history-session",
    ),
]
