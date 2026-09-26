from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from care.emr.api.viewsets.base import emr_exception_handler
from care.emr.models.invoice import Invoice
from care_sbiepay import payments
from care_sbiepay.api.serializers.payment_link import (
    CreatePaymentLinkRequest,
    PaymentLink,
)
from care_sbiepay.utils import client


@extend_schema(tags=["SBI ePay"])
class PaymentLinkViewSet(GenericViewSet):
    permission_classes = (IsAuthenticated,)

    def get_exception_handler(self):
        return emr_exception_handler

    @extend_schema(
        description="Create an SBI ePay payment link",
        request=CreatePaymentLinkRequest,
        responses={201: PaymentLink},
    )
    def create(self, request):
        data = CreatePaymentLinkRequest.model_validate(request.data)
        invoice = Invoice.objects.get(external_id=data.invoice_id)

        try:
            payment = payments.create_payment(invoice)
        except client.SbiEpayError as e:
            return Response(
                {"detail": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            PaymentLink(
                order_number=payment.order_number,
                payment_url=payment.payment_url,
                status=payment.status,
            ).model_dump(),
            status=status.HTTP_201_CREATED,
        )
