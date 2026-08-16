from rest_framework.generics import ListAPIView

from .models import Bag
from .serializers import BagListSerializer


class BagListView(ListAPIView):
    serializer_class = BagListSerializer
    queryset = Bag.objects.select_related("product_model").order_by("id")
