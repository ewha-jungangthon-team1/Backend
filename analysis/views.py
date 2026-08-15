from django.utils import timezone
from django.utils.timezone import now as get_current_time
from rest_framework import status as http_status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from measurements.models import MeasurementSession

from .ai import HistoryAIContentValidationError, HistoryAIResultValidationError
from .models import AnalysisReport
from .live_care import (
    LiveCareClaimState,
    generate_or_get_live_care,
)
from .serializers import AnalysisReportSerializer
from .services import analyze_history_session_with_ai


def _as_project_timezone_iso(value):
    if value is None:
        return None
    project_timezone = timezone.get_default_timezone()
    if timezone.is_naive(value):
        value = timezone.make_aware(value, project_timezone)
    return timezone.localtime(value, project_timezone).isoformat()


def _build_live_care_response(outcome):
    result = outcome.result
    if outcome.state == LiveCareClaimState.GENERATING:
        return {
            "session_id": result.session_id,
            "created": False,
            "status": LiveCareClaimState.GENERATING.value,
        }
    return {
        "session_id": result.session_id,
        "created": outcome.created,
        "status": LiveCareClaimState.READY.value,
        "care": result.care_result,
        "generated_at": _as_project_timezone_iso(result.generated_at),
        "fallback_used": result.fallback_used,
    }


@api_view(["POST"])
def live_care_view(request, session_id):
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

    if session.purpose != MeasurementSession.Purpose.LIVE:
        return Response(
            {"detail": "Only LIVE sessions can have detailed care results."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )

    try:
        outcome = generate_or_get_live_care(session)
    except ValueError as error:
        if str(error) != "A scenario is required to generate detailed LIVE care.":
            raise
        return Response(
            {"detail": str(error)},
            status=http_status.HTTP_400_BAD_REQUEST,
        )

    response_status = (
        http_status.HTTP_202_ACCEPTED
        if outcome.state == LiveCareClaimState.GENERATING
        else http_status.HTTP_200_OK
    )
    return Response(
        _build_live_care_response(outcome),
        status=response_status,
    )


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
            session__include_in_report=True,
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
