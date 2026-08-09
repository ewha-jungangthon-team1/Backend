from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from products.serializers import *

class PassportDetailView(APIView):
    """
    NFC/DPP 공개 진입점 API
    - public_token으로 가방을 조회
    - 소유자 개인정보(email 등)를 완벽히 배제하고 제품 및 기본 정보만 반환
    """
    def get(self, request, public_token):
        # DB 쿼리 최적화: select_related로 ProductModel을 함께 JOIN 조회
        bag = get_object_or_404(
            Bag.objects.select_related('product_model'), 
            public_token=public_token
        )

        # 기존 시리얼라이저 데이터 활용
        bag_data = BagDetailSerializer(bag).data

        # 요구사항에 맞춘 JSON 구조 조합
        response_payload = {
            "bag": bag_data
        }

        return Response(response_payload, status=status.HTTP_200_OK)

