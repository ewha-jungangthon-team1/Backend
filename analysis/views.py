from django.utils.timezone import now as get_current_time
from rest_framework import status as http_status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from measurements.models import MeasurementSession

from .ai import HistoryAIContentValidationError, HistoryAIResultValidationError
from .models import AnalysisReport
from .serializers import AnalysisReportSerializer
from .services import analyze_history_session_with_ai


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
        report, created = analyze_history_session_with_ai(session)
    except (HistoryAIContentValidationError, HistoryAIResultValidationError):
        raise
    except ValueError as exc:
        return Response(
            {"detail": str(exc)},
            status=http_status.HTTP_400_BAD_REQUEST,
        )

    serializer = AnalysisReportSerializer(report)
    response_data = {"created": created, **serializer.data}
    return Response(response_data, status=http_status.HTTP_200_OK)


@api_view(["GET"])
def analysis_report_detail_view(request, report_id):
    try:
        report = AnalysisReport.objects.select_related(
            "session__scenario"
        ).get(id=report_id)
    except AnalysisReport.DoesNotExist:
        return Response(
            {"detail": "존재하지 않는 분석 리포트입니다."},
            status=http_status.HTTP_404_NOT_FOUND,
        )

    serializer = AnalysisReportSerializer(report)
    return Response(serializer.data, status=http_status.HTTP_200_OK)


@api_view(["GET"])
def latest_analysis_report_for_bag_view(request, public_token):
    report = (
        AnalysisReport.objects.select_related("session__scenario")
        .filter(
            session__bag__public_token=public_token,
            session__purpose=MeasurementSession.Purpose.HISTORY,
            session__status=MeasurementSession.Status.COMPLETED,
            session__ended_at__isnull=False,
            session__ended_at__lte=get_current_time(),
        )
        .order_by(
            "-session__ended_at",
            "-session__started_at",
            "-id",
        )
        .first()
    )
    if report is None:
        return Response(
            {"detail": "No eligible analysis report was found."},
            status=http_status.HTTP_404_NOT_FOUND,
        )

    serializer = AnalysisReportSerializer(report)
    return Response(serializer.data, status=http_status.HTTP_200_OK)
