import logging

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from care_sbiepay import push_events
from care_sbiepay.models import SbiEpayPushEvent

logger = logging.getLogger(__name__)

HANDLED = (SbiEpayPushEvent.Status.PROCESSED, SbiEpayPushEvent.Status.IGNORED)


@extend_schema(tags=["SBI ePay"])
class WebhookViewSet(GenericViewSet):
    permission_classes = (AllowAny,)
    authentication_classes = []

    def create(self, request):
        """Store an authenticated push, then apply it.

        400: unauthenticated/malformed (not stored). 500: stored but processing
        failed; the replay task (or a re-delivery) retries it. 200 otherwise.
        """
        logger.info("SBI ePay webhook received")

        try:
            event, created = push_events.store_push(request.data)
        except push_events.PushRejectedError as exc:
            logger.warning("Rejected SBI ePay push: %s", exc)
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        if not created and event.status in HANDLED:
            return Response(status=status.HTTP_200_OK)

        try:
            push_events.process_push_event(event)
        except Exception:
            logger.exception("Failed to process SBI ePay push %s", event.pk)
            return Response(
                {"detail": "Push stored but could not be processed"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(status=status.HTTP_200_OK)
