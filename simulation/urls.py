from django.urls import path

from . import views

urlpatterns = [
    path(
        "bags/<uuid:public_token>/live-sessions/ensure/",
        views.ensure_live_session_view,
        name="ensure-live-session",
    ),
    path(
        "sessions/<int:session_id>/latest-reading/",
        views.latest_reading_view,
        name="latest-reading",
    ),
    # 관리자 전용
    path(
        "internal/bags/<int:bag_id>/simulations/",
        views.create_simulation_view,
        name="create-simulation",
    ),
    path(
        "internal/sessions/<int:session_id>/close/",
        views.close_session_view,
        name="close-session",
    ),
]
