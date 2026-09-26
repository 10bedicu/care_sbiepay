from rest_framework.routers import DefaultRouter

from care_sbiepay.api.viewsets.payment_link import PaymentLinkViewSet
from care_sbiepay.api.viewsets.webhook import WebhookViewSet

router = DefaultRouter()

router.register("payment_link", PaymentLinkViewSet, basename="sbiepay__payment_link")
router.register("webhook", WebhookViewSet, basename="sbiepay__webhook")

urlpatterns = router.urls
