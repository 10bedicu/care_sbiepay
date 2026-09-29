from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from care.emr.api.viewsets.base import emr_exception_handler
from care.emr.locks.billing import InvoiceLock
from care.emr.models.invoice import Invoice
from care.emr.resources.invoice.spec import InvoiceStatusOptions
from care.security.authorization.base import AuthorizationController
from care_sbiepay import payments
from care_sbiepay.api.serializers.payment_link import (
    CreatePaymentLinkRequest,
    PaymentLink,
)
from care_sbiepay.models import SbiEpayPayment
from care_sbiepay.utils import client

# collecting a payment is the same privilege as recording one
WRITE_PERMISSION = "can_write_payment_reconciliation_in_facility"


def _serialize(payment: SbiEpayPayment) -> dict:
    return PaymentLink.from_payment(payment).model_dump(mode="json")


@extend_schema(tags=["SBI ePay"])
class PaymentLinkViewSet(GenericViewSet):
    permission_classes = (IsAuthenticated,)
    lookup_field = "order_number"

    def get_exception_handler(self):
        return emr_exception_handler

    def _authorize(self, facility) -> None:
        if not AuthorizationController.call(
            WRITE_PERMISSION, self.request.user, facility
        ):
            raise PermissionDenied("Cannot collect payments for this facility")

    @extend_schema(
        description=(
            "Create an SBI ePay payment link for an issued invoice's outstanding "
            "balance. Returns the existing live link (200) when one already "
            "covers that balance."
        ),
        request=CreatePaymentLinkRequest,
        responses={200: PaymentLink, 201: PaymentLink},
    )
    def create(self, request):
        data = CreatePaymentLinkRequest.model_validate(request.data)
        invoice = (
            Invoice.objects.select_related("facility", "account", "patient")
            .filter(external_id=data.invoice_id)
            .first()
        )
        if not invoice:
            raise NotFound("Invoice not found")
        self._authorize(invoice.facility)
        if invoice.status != InvoiceStatusOptions.issued.value:
            msg = (
                f"Invoice must be issued to collect payment (status: {invoice.status})"
            )
            raise ValidationError(msg)
        if invoice.is_refund:
            raise ValidationError("Cannot collect payment for a refund invoice")

        with InvoiceLock(invoice):
            outstanding = payments.invoice_outstanding(invoice)
            if outstanding <= 0:
                raise ValidationError("Invoice has no outstanding balance")
            existing = payments.pending_payment(invoice)
            if existing and existing.amount == outstanding:
                return Response(_serialize(existing), status=status.HTTP_200_OK)
            try:
                payment = payments.create_payment(invoice, outstanding)
            except client.SbiEpayError as e:
                return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
            # retire the stale link only once its replacement exists
            if existing:
                payments.supersede(existing)

        return Response(_serialize(payment), status=status.HTTP_201_CREATED)

    @extend_schema(
        description=(
            "Ask the gateway for the payment's current status and settle it if "
            "paid, even if the link has expired locally."
        ),
        request=None,
        responses={200: PaymentLink},
    )
    @action(detail=True, methods=["POST"])
    def refresh(self, request, order_number=None):
        payment = (
            SbiEpayPayment.objects.select_related(
                "invoice__account", "invoice__facility"
            )
            .filter(order_number=order_number)
            .first()
        )
        if not payment:
            raise NotFound("Payment not found")
        self._authorize(payment.invoice.facility)
        if payment.status != SbiEpayPayment.Status.PAID:
            payments.check_gateway(payment)
            payment.refresh_from_db()
        return Response(_serialize(payment), status=status.HTTP_200_OK)
