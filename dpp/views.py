from django.shortcuts import get_object_or_404
from rest_framework.generics import ListAPIView

from products.models import Bag

from .models import LifecycleRecord
from .serializers import LifecycleRecordSerializer


class LifecycleListView(ListAPIView):
    serializer_class = LifecycleRecordSerializer

    def get_queryset(self):
        bag = get_object_or_404(Bag, public_token=self.kwargs["public_token"])
        return LifecycleRecord.objects.filter(bag=bag).order_by("recorded_at")
