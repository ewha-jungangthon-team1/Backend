from django.shortcuts import get_object_or_404
from rest_framework.generics import ListAPIView, RetrieveAPIView

from products.models import Bag

from .models import MeasurementSession
from .serializers import MeasurementSessionSerializer


class SessionListView(ListAPIView):
    serializer_class = MeasurementSessionSerializer

    def get_queryset(self):
        bag = get_object_or_404(Bag, public_token=self.kwargs["public_token"])
        return (
            MeasurementSession.objects.filter(bag=bag)
            .exclude(purpose=MeasurementSession.Purpose.LIVE)
            .order_by("-started_at")
        )


class SessionDetailView(RetrieveAPIView):
    serializer_class = MeasurementSessionSerializer
    lookup_url_kwarg = "session_id"

    def get_queryset(self):
        bag = get_object_or_404(Bag, public_token=self.kwargs["public_token"])
        return MeasurementSession.objects.filter(bag=bag).exclude(
            purpose=MeasurementSession.Purpose.LIVE
        )
