import logging
import re
from datetime import timedelta
from decimal import Decimal

import requests
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q, Sum

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
from care.utils.lock import ObjectLocked
from care.utils.rounding.rounding import care_round
from care.utils.time_util import care_now
from care_sbiepay.locks import SbiEpayPaymentLock
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


def expired_statuses() -> set[str]:
    return _status_set(settings.SBI_EPAY_EXPIRED_RESPONSE_STATUSES)


def payment_deadline(payment: SbiEpayPayment):
    """When the link stops being payable at the gateway."""
    if payment.expires_at:
        return payment.expires_at
    if payment.created_date:
        return payment.created_date + timedelta(
            seconds=settings.SBI_EPAY_PAYMENT_MAX_AGE
        )
    return None


def polling_deadline(payment: SbiEpayPayment):
    """Deadline plus a grace period for the gateway to report a last-second payment."""
    deadline = payment_deadline(payment)
    if not deadline:
        return None
    return deadline + timedelta(seconds=settings.SBI_EPAY_EXPIRY_GRACE_SECONDS)


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


def invoice_outstanding(invoice) -> Decimal:
    """Invoice total less settled payments, plus credit notes (as the account does)."""
    totals = PaymentReconciliation.objects.filter(
        target_invoice=invoice,
        status=PaymentReconciliationStatusOptions.active.value,
        outcome=PaymentReconciliationOutcomeOptions.complete.value,
    ).aggregate(
        paid=Sum("amount", filter=Q(is_credit_note=False)),
        credited=Sum("amount", filter=Q(is_credit_note=True)),
    )
    paid = totals["paid"] or Decimal(0)
    credited = totals["credited"] or Decimal(0)
    return care_round(Decimal(invoice.total_gross) - paid + credited)


def create_payment(invoice, amount: Decimal | None = None) -> SbiEpayPayment:
    """Generate an SBI ePay payment link for an invoice and persist it.

    Defaults to the invoice's outstanding balance so a partially settled
    invoice is never charged in full again.
    """
    merchant = merchant_for(invoice.facility)
    amount = care_round(amount if amount is not None else invoice_outstanding(invoice))
    if amount <= 0:
        raise client.SbiEpayError("Invoice has no outstanding balance")
    merch_order_no = client.merch_order_number()
    validity = client.order_validity()
    result = client.create_payment_link(
        merchant,
        merch_order_no=merch_order_no,
        amount=amount,
        other_details=other_details(invoice),
        validity=validity,
    )
    payment_url = result.get("paymentUrl")
    if not payment_url:
        raise client.SbiEpayError("SBI ePay did not return a payment URL")
    return SbiEpayPayment.objects.create(
        order_number=merch_order_no,
        invoice=invoice,
        amount=amount,
        payment_url=payment_url,
        expires_at=validity,
    )


def pending_payment(invoice) -> SbiEpayPayment | None:
    """The invoice's newest link that is still payable at the gateway, if any."""
    now = care_now()
    for payment in SbiEpayPayment.objects.filter(
        invoice=invoice, status=SbiEpayPayment.Status.CREATED
    ).order_by("-created_date"):
        deadline = payment_deadline(payment)
        if not deadline or deadline > now:
            return payment
    return None


def supersede(payment: SbiEpayPayment) -> bool:
    """Retire a live link that no longer matches what the invoice needs.

    The gateway keeps honouring the old link; if it is paid anyway the payment
    still settles and is flagged for review.
    """
    return _mark(payment, SbiEpayPayment.Status.SUPERSEDED)


def _fresh(payment: SbiEpayPayment) -> SbiEpayPayment:
    return SbiEpayPayment.objects.select_related(
        "invoice__account", "invoice__facility"
    ).get(pk=payment.pk)


def _already_recorded(payment: SbiEpayPayment, reference: str) -> bool:
    if payment.status == SbiEpayPayment.Status.PAID or payment.reconciliation_id:
        return True
    if not reference:
        return False
    return PaymentReconciliation.objects.filter(
        target_invoice_id=payment.invoice_id,
        reference_number=reference,
        status=PaymentReconciliationStatusOptions.active.value,
    ).exists()


def _validate(
    payment: SbiEpayPayment, confirmation: client.Confirmation
) -> tuple[Decimal, list[str]]:
    """Amount to record, and the reasons (if any) the settlement needs review."""
    if confirmation.order_number and confirmation.order_number != payment.order_number:
        msg = (
            f"Confirmation for order {confirmation.order_number} applied to "
            f"order {payment.order_number}"
        )
        raise client.SbiEpayError(msg)
    reasons = []
    merchant = get_merchant(payment.invoice.facility)
    if (
        confirmation.merchant_code
        and merchant
        and confirmation.merchant_code != merchant.merchant_code
    ):
        reasons.append(
            f"merchant mismatch: expected {merchant.merchant_code}, "
            f"got {confirmation.merchant_code}"
        )
    currency = settings.SBI_EPAY_CURRENCY.upper()
    if confirmation.currency and confirmation.currency != currency:
        reasons.append(
            f"currency mismatch: expected {currency}, got {confirmation.currency}"
        )
    amount = confirmation.amount if confirmation.amount is not None else payment.amount
    if amount != payment.amount:
        reasons.append(f"amount mismatch: expected {payment.amount}, got {amount}")
    if payment.status != SbiEpayPayment.Status.CREATED:
        reasons.append(f"settled from status {payment.status}")
    return amount, reasons


def _record_reconciliation(
    payment: SbiEpayPayment, reference: str, amount: Decimal, note: str
) -> PaymentReconciliation:
    invoice = payment.invoice
    return PaymentReconciliation.objects.create(
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
        amount=amount,
        tendered_amount=amount,
        returned_amount=0,
        is_credit_note=False,
        authorization="",
        disposition="",
        note=note,
        reference_number=reference,
    )


def reconcile_payment(
    payment: SbiEpayPayment, confirmation: client.Confirmation
) -> bool:
    """Record a gateway-confirmed payment exactly once.

    Runs under the payment lock with a fresh copy of the row, so concurrent
    webhook/polling workers cannot both credit the invoice. The confirmed
    amount is what gets recorded; any deviation from what we asked for is
    flagged for review rather than silently adjusted. Returns True when a
    reconciliation was recorded by this call.
    """
    with SbiEpayPaymentLock(payment), transaction.atomic():
        payment = _fresh(payment)
        reference = confirmation.reference
        if _already_recorded(payment, reference):
            return False
        amount, reasons = _validate(payment, confirmation)
        note = f"Payment via SBI ePay (order {payment.order_number})."
        if reasons:
            note += " Needs review: " + "; ".join(reasons) + "."
        payment.reconciliation = _record_reconciliation(
            payment, reference, amount, note
        )
        payment.paid_amount = amount
        payment.reference = reference
        payment.status = SbiEpayPayment.Status.PAID
        for reason in reasons:
            payment.flag_for_review(reason)
        payment.save(
            update_fields=[
                "reconciliation",
                "paid_amount",
                "reference",
                "status",
                "needs_review",
                "review_reason",
                "modified_date",
            ]
        )
        account_id = payment.invoice.account_id
        transaction.on_commit(lambda: rebalance_account_task.delay(account_id))
    if reasons:
        logger.warning(
            "SBI ePay order %s settled but needs review: %s",
            payment.order_number,
            "; ".join(reasons),
        )
    return True


def _mark(payment: SbiEpayPayment, status: str, review: str = "") -> bool:
    """Move a still-pending payment to a terminal unpaid state."""
    with SbiEpayPaymentLock(payment), transaction.atomic():
        payment = _fresh(payment)
        if payment.status != SbiEpayPayment.Status.CREATED:
            return False
        payment.status = status
        if review:
            payment.flag_for_review(review)
        payment.save(
            update_fields=["status", "needs_review", "review_reason", "modified_date"]
        )
    return True


def apply_gateway_status(
    payment: SbiEpayPayment, confirmation: client.Confirmation
) -> None:
    """Move a pending payment to a terminal state based on a gateway status."""
    status = confirmation.status
    if status in paid_statuses():
        reconcile_payment(payment, confirmation)
    elif status in cancelled_statuses():
        _mark(payment, SbiEpayPayment.Status.CANCELLED)
    elif status in failed_statuses():
        _mark(payment, SbiEpayPayment.Status.FAILED)
    elif status in expired_statuses():
        _mark(payment, SbiEpayPayment.Status.EXPIRED)
    else:
        logger.info(
            "SBI ePay order %s still pending, status %s",
            payment.order_number,
            confirmation.status,
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
            push = client.parse_push_response(merchant.merchant_key, push_resp_data)
        except Exception:
            logger.debug(
                "SBI ePay push did not decode with merchant %s",
                merchant.merchant_code,
            )
            continue
        # the key that authenticated the push identifies the merchant
        if not push.get("merchant_id"):
            push["merchant_id"] = merchant.merchant_code
        return push
    logger.warning(
        "No SBI ePay merchant could decode push (merchIdVal=%s)", merchant_code
    )
    return None


def reconcile_push(push: dict) -> bool:
    """Reconcile a standalone payment from a decoded push payload.

    Returns True when the order belongs to a standalone payment (handled here),
    False when it is unknown (e.g. an ABDM scan-and-pay order).
    """
    payment = (
        SbiEpayPayment.objects.filter(order_number=push.get("merch_order_no"))
        .select_related("invoice__account", "invoice__facility")
        .first()
    )
    if not payment:
        return False
    apply_gateway_status(payment, client.confirmation_from_push(push))
    return True


def check_gateway(payment: SbiEpayPayment) -> bool:
    """Query the gateway for the order once; returns whether it answered."""
    merchant = get_merchant(payment.invoice.facility)
    if not merchant:
        logger.warning(
            "No SBI ePay merchant for facility of order %s; skipping poll",
            payment.order_number,
        )
        return False
    try:
        result = client.status_query(
            merchant,
            merch_order_no=payment.order_number,
            amount=payment.amount,
        )
    except (requests.Timeout, requests.ConnectionError) as exc:
        logger.warning(
            "SBI ePay unreachable while polling order %s: %s",
            payment.order_number,
            exc,
        )
        return False
    except Exception:
        logger.exception(
            "Failed to poll SBI ePay status for order %s", payment.order_number
        )
        return False
    apply_gateway_status(
        payment, client.confirmation_from_status(result, payment.order_number)
    )
    return True


def backoff_key(payment: SbiEpayPayment) -> str:
    return f"sbiepay:backoff:{payment.external_id}"


def _poll(payment: SbiEpayPayment, now) -> None:
    if cache.get(backoff_key(payment)):
        return
    answered = check_gateway(payment)
    if not answered:
        cache.set(
            backoff_key(payment),
            1,
            timeout=settings.SBI_EPAY_UNREACHABLE_BACKOFF,
        )
    deadline = polling_deadline(payment)
    if not deadline or deadline > now:
        return
    if answered:
        # the gateway had its final say; a later push can still settle the row
        _mark(payment, SbiEpayPayment.Status.EXPIRED)
        return
    hard_cap = deadline + timedelta(seconds=settings.SBI_EPAY_EXPIRY_HARD_CAP_SECONDS)
    if hard_cap <= now:
        _mark(
            payment,
            SbiEpayPayment.Status.EXPIRED,
            review="gateway unreachable at expiry; final status unconfirmed",
        )


def poll_pending_payments() -> None:
    """Poll the gateway for each pending payment; expire those past their deadline.

    A payment is only expired after the gateway answered a query past the
    deadline (plus grace), so an unreachable gateway cannot make us drop a
    payment that actually succeeded. Rows being settled by another worker are
    skipped this cycle.
    """
    now = care_now()
    pending = SbiEpayPayment.objects.filter(
        status=SbiEpayPayment.Status.CREATED
    ).select_related("invoice__account", "invoice__facility")
    for payment in pending:
        try:
            _poll(payment, now)
        except ObjectLocked:
            logger.info(
                "SBI ePay order %s is being settled elsewhere; skipping",
                payment.order_number,
            )
