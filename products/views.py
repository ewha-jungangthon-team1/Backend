from rest_framework.generics import RetrieveAPIView

from .models import Bag
from .serializers import BagDetailSerializer


class BagDetailView(RetrieveAPIView):
    queryset = Bag.objects.select_related("product_model")
    serializer_class = BagDetailSerializer
    lookup_field = "public_token"
    lookup_url_kwarg = "public_token"
