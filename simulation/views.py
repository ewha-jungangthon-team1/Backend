from django.utils.dateparse import parse_datetime
from rest_framework import status as http_status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response

from measurements.home import build_sensor_presentation_values
from measurements.models import MeasurementSession
from products.models import Bag

from .serializers import LiveSessionSerializer
from .services import (
    close_session,
    create_simulation_session,
    ensure_live_session,
    get_latest_reading,
)


@api_view(["POST"])
def ensure_live_session_view(request, public_token):
    try:
        bag = Bag.objects.get(public_token=public_token)
    except Bag.DoesNotExist:
        return Response({"detail": "존재하지 않는 가방입니다."}, status=http_status.HTTP_404_NOT_FOUND)

    try:
        session, created = ensure_live_session(bag)
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=http_status.HTTP_400_BAD_REQUEST)
    serializer = LiveSessionSerializer(session, context={"created": created})
    return Response(serializer.data)


@api_view(["GET"])
def latest_reading_view(request, session_id):
    try:
        session = MeasurementSession.objects.select_related("scenario").get(id=session_id)
    except MeasurementSession.DoesNotExist:
        return Response({"detail": "존재하지 않는 세션입니다."}, status=http_status.HTTP_404_NOT_FOUND)

    reading = get_latest_reading(session)
    if reading is None:
        return Response({"detail": "아직 생성된 데이터가 없습니다."}, status=http_status.HTTP_404_NOT_FOUND)

    presentation_values = build_sensor_presentation_values(
        strap_load=reading["strap_load"],
        load_bias=reading["load_bias"],
        body_deformation_ratio=reading["body_deformation_ratio"],
        temperature=reading["temperature"],
        humidity=reading["humidity"],
        material_moisture_percent=reading["material_moisture_percent"],
    )
    return Response(
        {
            **reading,
            "presentation": {"values": presentation_values},
        }
    )


# ------------------------------------------------------------
# 아래부터는 시연자·관리자 전용 API (일반 사용자에게 노출 안 함)
# ------------------------------------------------------------

@api_view(["POST"])
@permission_classes([IsAdminUser])
def create_simulation_view(request, bag_id):
    """관리자가 시나리오/시드를 직접 지정해서 세션+더미데이터를 생성한다."""
    try:
        bag = Bag.objects.get(id=bag_id)
    except Bag.DoesNotExist:
        return Response({"detail": "존재하지 않는 가방입니다."}, status=http_status.HTTP_404_NOT_FOUND)

    scenario_code = request.data.get("scenario_code")
    if not scenario_code:
        return Response({"detail": "scenario_code는 필수입니다."}, status=http_status.HTTP_400_BAD_REQUEST)

    random_seed = request.data.get("random_seed")
    started_at_raw = request.data.get("started_at")
    started_at = parse_datetime(started_at_raw) if started_at_raw else None

    try:
        session, reading_count = create_simulation_session(
            bag, scenario_code, random_seed=random_seed, started_at=started_at
        )
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=http_status.HTTP_400_BAD_REQUEST)

    return Response(
        {
            "session_id": session.id,
            "scenario_code": session.scenario.code,
            "status": session.status,
            "reading_count": reading_count,
            "random_seed": session.seed,
        }
    )


@api_view(["PATCH"])
@permission_classes([IsAdminUser])
def close_session_view(request, session_id):
    """세션 상태만 COMPLETED로 바꾼다. 분석은 별도 API(B 담당)에서 처리."""
    try:
        session = MeasurementSession.objects.get(id=session_id)
    except MeasurementSession.DoesNotExist:
        return Response({"detail": "존재하지 않는 세션입니다."}, status=http_status.HTTP_404_NOT_FOUND)

    ended_at_raw = request.data.get("ended_at")
    ended_at = parse_datetime(ended_at_raw) if ended_at_raw else None

    session = close_session(session, ended_at=ended_at)
    return Response({"session_id": session.id, "status": session.status})
