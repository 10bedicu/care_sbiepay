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
    register_provider,
)
from abdm.settings import plugin_settings as abdm_settings

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


def _scan_pay_order(order_number: str):
    return (
        PaymentOrder.objects.filter(order_number=order_number)
        .select_related(
            "invoice__account", "invoice__facility", "health_facility__facility"
        )
        .first()
    )


def apply_scan_pay_confirmation(order, confirmation) -> None:
    """Apply a gateway confirmation to a scan-and-pay order.

    A paid status records a reconciliation, and that signal notifies the PHR.
    Cancelled, failed, and expired statuses close the order, which notifies
    too. Any other status leaves the order payable.
    """
    status = confirmation.status
    if status in paid_statuses():
        create_payment_reconciliation(
            order, confirmation.reference, amount=confirmation.amount
        )
    elif status in cancelled_statuses():
        close_payment_order(order, PaymentOrderStatus.CANCELED)
    elif status in expired_statuses():
        close_payment_order(order, PaymentOrderStatus.EXPIRED)
    elif status in failed_statuses():
        close_payment_order(order, PaymentOrderStatus.FAIL)
    else:
        logger.info(
            "SBI ePay order %s not paid, status %s", order.order_number, status
        )


def reconcile_abdm_push(push: dict) -> bool:
    """Reconcile an ABDM scan-and-pay order from a decoded push payload.

    Paid, failed, cancelled, and expired statuses update the order and notify
    the PHR. Returns True when the order is known.
    """
    confirmation = client.confirmation_from_push(push)
    order = _scan_pay_order(confirmation.order_number)
    if order is None:
        return False
    apply_scan_pay_confirmation(order, confirmation)
    return True


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
            # the link should stop being payable when ABDM gives up on the order
            validity=client.order_validity(
                abdm_settings.ABDM_SCAN_AND_PAY_ORDER_MAX_AGE
            ),
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

    def reconcile_order(self, order) -> bool:
        if not order.invoice_id or order.status in PAYMENT_ORDER_PAID_STATUSES:
            return False
        merchant = get_merchant(order.invoice.facility)
        if not merchant:
            logger.warning(
                "No SBI ePay merchant for facility of order %s", order.order_number
            )
            return False
        # the status query must quote the amount the order was created with
        amount = order.amount if order.amount is not None else order.invoice.total_gross
        result = client.status_query(
            merchant,
            merch_order_no=order.order_number,
            amount=amount,
        )
        confirmation = client.confirmation_from_status(result, order.order_number)
        apply_scan_pay_confirmation(order, confirmation)
        return True
