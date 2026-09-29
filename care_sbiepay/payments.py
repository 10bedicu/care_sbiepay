import logging
import re
from datetime import timedelta

from care.emr.models.payment_reconciliation import PaymentReconciliation
from care.emr.resources.account.sync_items import rebalance_account_task
from care.emr.resources.payment_reconciliation.spec import (
    PaymentReconciliationIssuerTypeOptions,
    PaymentReconciliationKindOptions,
    PaymentReconciliationOutcomeOptions,
    PaymentReconciliationPaymentMethodOptions,
    PaymentReconciliationStatusOptions,
    PaymentReconciliationTypeOptions,
)
from care.utils.time_util import care_now
from care_sbiepay.models import SbiEpayMerchant, SbiEpayPayment
from care_sbiepay.settings import plugin_settings as settings
from care_sbiepay.utils import client

logger = logging.getLogger(__name__)


def _status_set(raw: str) -> set[str]:
    return {value.strip().upper() for value in raw.split(",") if value.strip()}


def paid_statuses() -> set[str]:
    return _status_set(settings.SBI_EPAY_PAID_RESPONSE_STATUSES)


def failed_statuses() -> set[str]:
    return _status_set(settings.SBI_EPAY_FAILED_RESPONSE_STATUSES)


def cancelled_statuses() -> set[str]:
    return _status_set(settings.SBI_EPAY_CANCELLED_RESPONSE_STATUSES)


def other_details(invoice) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", invoice.patient.name or "")[:100] or "CARE"


def get_merchant(facility) -> SbiEpayMerchant | None:
    """Merchant configured for a facility, regardless of whether it is enabled.

    Used to reconcile payments that were already created.
    """
    return SbiEpayMerchant.objects.filter(facility=facility).first()


def merchant_for(facility) -> SbiEpayMerchant:
    """Merchant to use for new payments; must be configured and enabled."""
    merchant = get_merchant(facility)
    if not merchant:
        raise client.SbiEpayError("SBI ePay merchant not configured for facility")
    if not merchant.is_enabled:
        raise client.SbiEpayError("SBI ePay payments are disabled for facility")
    return merchant


def create_payment(invoice) -> SbiEpayPayment:
    """Generate an SBI ePay payment link for an invoice and persist it."""
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
    return SbiEpayPayment.objects.create(
        order_number=merch_order_no,
        invoice=invoice,
        amount=invoice.total_gross,
        payment_url=payment_url,
    )


def _record_reconciliation(payment: SbiEpayPayment, reference) -> None:
    invoice = payment.invoice
    payment_reconciliation = PaymentReconciliation.objects.create(
        target_invoice=invoice,
        facility=invoice.facility,
        account=invoice.account,
        reconciliation_type=PaymentReconciliationTypeOptions.payment.value,
        status=PaymentReconciliationStatusOptions.active.value,
        kind=PaymentReconciliationKindOptions.online.value,
        issuer_type=PaymentReconciliationIssuerTypeOptions.patient.value,
        outcome=PaymentReconciliationOutcomeOptions.complete.value,
        method=PaymentReconciliationPaymentMethodOptions.debc.value,
        payment_datetime=care_now(),
        amount=invoice.total_gross,
        tendered_amount=invoice.total_gross,
        returned_amount=0,
        is_credit_note=False,
        authorization="",
        disposition="",
        note=f"Payment via SBI ePay (order {payment.order_number}).",
        reference_number=reference,
    )
    rebalance_account_task.delay(payment_reconciliation.account.id)


def reconcile_payment(payment: SbiEpayPayment, reference) -> None:
    if payment.status != SbiEpayPayment.Status.CREATED:
        return
    _record_reconciliation(payment, reference)
    payment.status = SbiEpayPayment.Status.PAID
    payment.reference = reference or ""
    payment.save(update_fields=["status", "reference", "modified_date"])


def _mark(payment: SbiEpayPayment, status: str) -> None:
    if payment.status != SbiEpayPayment.Status.CREATED:
        return
    payment.status = status
    payment.save(update_fields=["status", "modified_date"])


def apply_gateway_status(
    payment: SbiEpayPayment, gateway_status: str, reference
) -> None:
    """Move a pending payment to a terminal state based on a gateway status."""
    status = (gateway_status or "").upper()
    if status in paid_statuses():
        reconcile_payment(payment, reference)
    elif status in cancelled_statuses():
        _mark(payment, SbiEpayPayment.Status.CANCELLED)
    elif status in failed_statuses():
        _mark(payment, SbiEpayPayment.Status.FAILED)
    else:
        logger.info(
            "SBI ePay order %s still pending, status %s",
            payment.order_number,
            gateway_status,
        )


def decode_push(data) -> dict | None:
    """Decrypt a raw webhook payload using the merchant it was pushed for.

    SBI sends ``merchIdVal`` alongside ``pushRespData``; when it is missing every
    configured merchant key is tried until the checksum validates.
    """
    push_resp_data = data.get("pushRespData")
    if not push_resp_data:
        return None

    merchants = SbiEpayMerchant.objects.all()
    merchant_code = data.get("merchIdVal")
    if merchant_code:
        merchants = merchants.filter(merchant_code=merchant_code)

    for merchant in merchants:
        try:
            return client.parse_push_response(merchant.merchant_key, push_resp_data)
        except Exception:
            logger.debug(
                "SBI ePay push did not decode with merchant %s",
                merchant.merchant_code,
            )
    logger.warning(
        "No SBI ePay merchant could decode push (merchIdVal=%s)", merchant_code
    )
    return None


def reconcile_push(push: dict) -> bool:
    """Reconcile a standalone payment from a decoded push payload.

    Returns True when the order belongs to a standalone payment (handled here),
    False when it is unknown (e.g. an ABDM scan-and-pay order).
    """
    payment = SbiEpayPayment.objects.filter(
        order_number=push.get("merch_order_no")
    ).first()
    if not payment:
        return False
    apply_gateway_status(
        payment,
        push.get("status"),
        push.get("atrn") or push.get("bank_ref_number"),
    )
    return True


def poll_pending_payments() -> None:
    """Poll SBI ePay for each pending payment; expire stale ones."""
    cutoff = care_now() - timedelta(seconds=settings.SBI_EPAY_PAYMENT_MAX_AGE)
    pending = SbiEpayPayment.objects.filter(
        status=SbiEpayPayment.Status.CREATED
    ).select_related("invoice__account", "invoice__facility")
    for payment in pending:
        if payment.created_date and payment.created_date < cutoff:
            _mark(payment, SbiEpayPayment.Status.EXPIRED)
            continue
        merchant = get_merchant(payment.invoice.facility)
        if not merchant:
            logger.warning(
                "No SBI ePay merchant for facility of order %s; skipping poll",
                payment.order_number,
            )
            continue
        try:
            result = client.status_query(
                merchant,
                merch_order_no=payment.order_number,
                amount=payment.amount,
            )
        except Exception:
            logger.exception(
                "Failed to poll SBI ePay status for order %s", payment.order_number
            )
            continue
        reference = result.get("SBIePayRefID/ATRN") or result.get(
            "Bank Reference Number"
        )
        apply_gateway_status(payment, result.get("Response Status"), reference)
