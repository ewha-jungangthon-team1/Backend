from rest_framework import status as http_status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from measurements.models import MeasurementSession

from .serializers import AnalysisReportSerializer
from .services import analyze_history_session


@api_view(["POST"])
def analyze_history_session_view(request, session_id):
    try:
        session = MeasurementSession.objects.select_related(
            "bag__product_model",
            "scenario",
        ).get(id=session_id)
    except MeasurementSession.DoesNotExist:
        return Response(
            {"detail": "존재하지 않는 세션입니다."},
            status=http_status.HTTP_404_NOT_FOUND,
        )

    try:
        report, created = analyze_history_session(session)
    except ValueError as exc:
        return Response(
            {"detail": str(exc)},
            status=http_status.HTTP_400_BAD_REQUEST,
        )

    serializer = AnalysisReportSerializer(report)
    response_data = {"created": created, **serializer.data}
    return Response(response_data, status=http_status.HTTP_200_OK)
