import logging

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from care_sbiepay import payments
from care_sbiepay.utils import client

logger = logging.getLogger(__name__)


def _reconcile_via_abdm(push: dict) -> None:
    # Optional: only wired when the ABDM plug is installed.
    try:
        from care_sbiepay.provider import reconcile_abdm_push
    except ImportError:
        logger.info("care_abdm not installed; ignoring unknown SBI ePay order")
        return
    reconcile_abdm_push(push)


@extend_schema(tags=["SBI ePay"])
class WebhookViewSet(GenericViewSet):
    permission_classes = (AllowAny,)
    authentication_classes = []

    def create(self, request):
        logger.info("SBI ePay webhook received")

        push_resp_data = request.data.get("pushRespData")
        if push_resp_data:
            try:
                push = client.parse_push_response(push_resp_data)
                if not payments.reconcile_push(push):
                    _reconcile_via_abdm(push)
            except Exception:
                logger.exception("Failed to process SBI ePay push response")

        return Response(status=status.HTTP_200_OK)
