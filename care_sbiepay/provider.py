import logging

from abdm.models.payment_order import (
    PAYMENT_ORDER_PAID_STATUSES,
    PaymentOrder,
    PaymentOrderStatus,
)
from abdm.service.v3.payment_providers import (
    PaymentProvider,
    close_payment_order,
    create_payment_reconciliation,
    reconcile_payment_order,
    register_provider,
)

from care_sbiepay.payments import (
    cancelled_statuses,
    expired_statuses,
    failed_statuses,
    get_merchant,
    merchant_for,
    other_details,
    paid_statuses,
)
from care_sbiepay.utils import client

logger = logging.getLogger(__name__)


def reconcile_abdm_push(push: dict) -> bool:
    """Reconcile an ABDM scan-and-pay order from a decoded push payload.

    Only a paid status changes anything; a failed attempt does not close the
    order because the link stays payable. Returns True when the order is known.
    """
    confirmation = client.confirmation_from_push(push)
    if confirmation.status in paid_statuses():
        order = reconcile_payment_order(
            confirmation.order_number,
            confirmation.reference,
            amount=confirmation.amount,
        )
        return order is not None
    return PaymentOrder.objects.filter(order_number=confirmation.order_number).exists()


@register_provider
class SbiEpayProvider(PaymentProvider):
    name = "sbi_epay"

    def create_payment_link(self, invoice) -> dict:
        merchant = merchant_for(invoice.facility)
        merch_order_no = client.merch_order_number()
        result = client.create_payment_link(
            merchant,
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
            "amount": invoice.total_gross,
        }

    def reconcile_order(self, order) -> None:
        if not order.invoice_id or order.status in PAYMENT_ORDER_PAID_STATUSES:
            return
        merchant = get_merchant(order.invoice.facility)
        if not merchant:
            logger.warning(
                "No SBI ePay merchant for facility of order %s", order.order_number
            )
            return
        # the status query must quote the amount the order was created with
        amount = order.amount if order.amount is not None else order.invoice.total_gross
        result = client.status_query(
            merchant,
            merch_order_no=order.order_number,
            amount=amount,
        )
        confirmation = client.confirmation_from_status(result, order.order_number)
        status = confirmation.status
        if status in paid_statuses():
            create_payment_reconciliation(
                order, confirmation.reference, amount=confirmation.amount
            )
        elif status in cancelled_statuses():
            close_payment_order(order, PaymentOrderStatus.CANCELED)
        elif status in failed_statuses() or status in expired_statuses():
            close_payment_order(order, PaymentOrderStatus.FAIL)
        else:
            logger.info(
                "SBI ePay order %s not paid, status %s", order.order_number, status
            )
