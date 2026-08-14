from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from .models import Bag, ProductModel


class BagListAPITests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        owner = get_user_model().objects.create_user(
            username="bag-list-owner",
            password="test-password",
        )
        cls.product_a = ProductModel.objects.create(
            brand="Brand A",
            model_name="Model A",
            material="Leather",
            care_guideline={"marker": "A"},
        )
        cls.product_b = ProductModel.objects.create(
            brand="Brand B",
            model_name="Model B",
            material="Canvas",
            care_guideline={"marker": "B"},
        )
        cls.bag_a = Bag.objects.create(
            product_model=cls.product_a,
            owner=owner,
            serial_number="SERIAL-A",
            nfc_uid="NFC-A",
        )
        cls.bag_b = Bag.objects.create(
            product_model=cls.product_b,
            owner=owner,
            serial_number="SERIAL-B",
            nfc_uid="NFC-B",
        )
        cls.url = reverse("bag-list")

    def test_get_bag_list_returns_200(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_returns_all_bags_in_id_order(self):
        response = self.client.get(self.url)

        self.assertEqual(len(response.data), 2)
        self.assertEqual(
            [item["public_token"] for item in response.data],
            [str(self.bag_a.public_token), str(self.bag_b.public_token)],
        )

    def test_maps_each_bag_to_its_product_summary(self):
        response = self.client.get(self.url)

        self.assertEqual(
            response.data[0]["product"],
            {
                "brand": "Brand A",
                "model_name": "Model A",
                "material": "Leather",
                "image_url": None,
            },
        )
        self.assertEqual(
            response.data[1]["product"],
            {
                "brand": "Brand B",
                "model_name": "Model B",
                "material": "Canvas",
                "image_url": None,
            },
        )

    def test_returns_distinct_public_tokens(self):
        response = self.client.get(self.url)

        tokens = [item["public_token"] for item in response.data]
        self.assertEqual(len(tokens), len(set(tokens)))

    def test_excludes_private_and_unnecessary_fields(self):
        response = self.client.get(self.url)

        for item in response.data:
            self.assertEqual(set(item), {"public_token", "product"})
            self.assertEqual(
                set(item["product"]),
                {"brand", "model_name", "material", "image_url"},
            )

    def test_returns_null_image_url_when_model_image_is_missing(self):
        response = self.client.get(self.url)

        self.assertIsNone(response.data[0]["product"]["image_url"])
        self.assertIsNone(response.data[1]["product"]["image_url"])

    def test_returns_image_url_when_model_image_exists(self):
        ProductModel.objects.filter(pk=self.product_a.pk).update(
            model_image="ProductModel/image/model-a.jpg"
        )

        response = self.client.get(self.url)

        self.assertTrue(
            response.data[0]["product"]["image_url"].endswith(
                "/media/ProductModel/image/model-a.jpg"
            )
        )

    def test_post_is_not_allowed(self):
        response = self.client.post(self.url, data={})

        self.assertEqual(
            response.status_code,
            status.HTTP_405_METHOD_NOT_ALLOWED,
        )


class EmptyBagListAPITests(APITestCase):
    def test_returns_empty_list_when_no_bags_exist(self):
        response = self.client.get(reverse("bag-list"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])
