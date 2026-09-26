import logging

from abdm.models.payment_order import PAYMENT_ORDER_PAID_STATUSES
from abdm.service.v3.payment_providers import (
    PaymentProvider,
    create_payment_reconciliation,
    reconcile_payment_order,
    register_provider,
)

from care_sbiepay.payments import other_details, paid_statuses
from care_sbiepay.utils import client

logger = logging.getLogger(__name__)


def reconcile_abdm_push(push: dict) -> None:
    """Reconcile an ABDM scan-and-pay order from a decoded push payload."""
    if (push.get("status") or "").upper() not in paid_statuses():
        return
    reconcile_payment_order(
        push.get("merch_order_no"),
        push.get("atrn") or push.get("bank_ref_number"),
    )


@register_provider
class SbiEpayProvider(PaymentProvider):
    name = "sbi_epay"

    def create_payment_link(self, invoice) -> dict:
        merch_order_no = client.merch_order_number()
        result = client.create_payment_link(
            merch_order_no=merch_order_no,
            amount=invoice.total_gross,
            other_details=other_details(invoice),
        )
        payment_url = result.get("paymentUrl")
        if not payment_url:
            raise client.SbiEpayError("SBI ePay did not return a payment URL")
        return {
            "order_number": merch_order_no,
            "payment_link_id": payment_url,
            "payment_url": payment_url,
        }

    def reconcile_order(self, order) -> None:
        if not order.invoice_id or order.status in PAYMENT_ORDER_PAID_STATUSES:
            return
        result = client.status_query(
            merch_order_no=order.order_number,
            amount=order.invoice.total_gross,
        )
        status = (result.get("Response Status") or "").upper()
        if status not in paid_statuses():
            logger.info(
                "SBI ePay order %s not paid, status %s", order.order_number, status
            )
            return
        reference = result.get("SBIePayRefID/ATRN") or result.get(
            "Bank Reference Number"
        )
        create_payment_reconciliation(order, reference)
