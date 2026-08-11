from django.urls import path

from . import views


urlpatterns = [
    path(
        "sessions/<int:session_id>/analyze/",
        views.analyze_history_session_view,
        name="analyze-history-session",
    ),
    path(
        "reports/<int:report_id>/",
        views.analysis_report_detail_view,
        name="analysis-report-detail",
    ),
]
