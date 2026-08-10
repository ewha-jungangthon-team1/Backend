from django.shortcuts import get_object_or_404
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.exceptions import NotFound
from products.models import Bag

from .models import MeasurementSession
from .serializers import MeasurementSessionHomeSerializer, MeasurementSessionSerializer
from simulation.services import get_latest_session_for_bag

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

class HomeView(RetrieveAPIView):
 
    serializer_class = MeasurementSessionHomeSerializer
 
    def get_object(self):
        try:
            bag = Bag.objects.select_related("product_model").get(
                public_token=self.kwargs["public_token"]
            )
        except Bag.DoesNotExist:
            raise NotFound("존재하지 않는 가방입니다.")
 
        session = get_latest_session_for_bag(bag)
        if session is None:
            raise NotFound("아직 측정 기록이 없습니다.")
 
        return session