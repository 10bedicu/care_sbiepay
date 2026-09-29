import hashlib
import logging

from django.db import transaction

from care_sbiepay import payments
from care_sbiepay.models import SbiEpayPushEvent
from care_sbiepay.settings import plugin_settings as settings
from care_sbiepay.utils import client

logger = logging.getLogger(__name__)

STORED_FORM_FIELDS = ("pushRespData", "merchIdVal", "Bank_Code")


class PushRejectedError(Exception):
    """The push is malformed or unauthenticated; nothing was stored."""


def _reconcile_via_abdm(push: dict) -> bool:
    # Optional: only wired when the ABDM plug is installed.
    try:
        from care_sbiepay.provider import reconcile_abdm_push
    except ImportError:
        logger.info("care_abdm not installed; ignoring unknown SBI ePay order")
        return False
    return reconcile_abdm_push(push)


def payload_hash(push_resp_data: str) -> str:
    return hashlib.sha256(push_resp_data.encode("utf-8")).hexdigest()


def store_push(data) -> tuple[SbiEpayPushEvent, bool]:
    """Authenticate a raw push and persist it. Returns ``(event, created)``.

    A re-delivered push hashes to the same row, so callers can tell a replay
    from a first delivery.
    """
    push_resp_data = data.get("pushRespData")
    if not push_resp_data or not isinstance(push_resp_data, str):
        raise PushRejectedError("pushRespData missing")
    if len(push_resp_data) > settings.SBI_EPAY_PUSH_MAX_BYTES:
        raise PushRejectedError("pushRespData too large")
    push = payments.decode_push(data)
    if push is None:
        raise PushRejectedError("push could not be authenticated")

    confirmation = client.confirmation_from_push(push)
    return SbiEpayPushEvent.objects.get_or_create(
        payload_hash=payload_hash(push_resp_data),
        defaults={
            "merchant_code": confirmation.merchant_code,
            "order_number": confirmation.order_number,
            "gateway_status": confirmation.status,
            "amount": confirmation.amount,
            "currency": confirmation.currency,
            "reference": confirmation.reference,
            "raw_form": {k: data.get(k) for k in STORED_FORM_FIELDS if data.get(k)},
            "decoded": push,
        },
    )


def process_push_event(event: SbiEpayPushEvent) -> SbiEpayPushEvent:
    """Apply a stored push to its order.

    Marks the event processed (order known) or ignored (order unknown). On
    failure the event is marked failed for replay and the error re-raised; the
    savepoint keeps the caller's transaction usable.
    """
    try:
        with transaction.atomic():
            handled = payments.reconcile_push(event.decoded) or _reconcile_via_abdm(
                event.decoded
            )
    except Exception as exc:
        event.mark_failed(f"{type(exc).__name__}: {exc}")
        raise
    event.mark_processed(
        SbiEpayPushEvent.Status.PROCESSED
        if handled
        else SbiEpayPushEvent.Status.IGNORED
    )
    return event


def replay_failed(limit: int = 100) -> int:
    """Retry pushes whose processing failed; returns how many now succeeded."""
    events = SbiEpayPushEvent.objects.filter(
        status=SbiEpayPushEvent.Status.FAILED,
        attempts__lt=settings.SBI_EPAY_PUSH_MAX_ATTEMPTS,
    ).order_by("created_date")[:limit]
    replayed = 0
    for event in events:
        try:
            process_push_event(event)
        except Exception:
            logger.exception("Replay of SBI ePay push %s failed", event.pk)
            continue
        replayed += 1
    return replayed
