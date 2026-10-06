import logging

from celery import current_app, shared_task

from care.utils.lock import Lock, ObjectLocked
from care_sbiepay import payments, push_events
from care_sbiepay.models import SbiEpayPayment
from care_sbiepay.settings import plugin_settings as settings

logger = logging.getLogger(__name__)

POLL_LOCK_KEY = "sbiepay:poll_pending_payments"
POLL_LOCK_TIMEOUT = 300


@shared_task(ignore_result=True)
def poll_pending_payments() -> None:
    if not settings.SBI_EPAY_POLLING_ENABLED:
        return
    if not SbiEpayPayment.objects.filter(status=SbiEpayPayment.Status.CREATED).exists():
        return
    try:
        with Lock(POLL_LOCK_KEY, POLL_LOCK_TIMEOUT):
            payments.poll_pending_payments()
    except ObjectLocked:
        logger.info("Previous SBI ePay poll is still running; skipping this tick")


@shared_task(ignore_result=True)
def replay_failed_push_events() -> None:
    replayed = push_events.replay_failed()
    if replayed:
        logger.info("Replayed %s failed SBI ePay push(es)", replayed)


@current_app.on_after_finalize.connect
def setup_periodic_tasks(sender, **kwargs) -> None:
    sender.add_periodic_task(
        settings.SBI_EPAY_PUSH_REPLAY_INTERVAL,
        replay_failed_push_events.s(),
        name="sbiepay_replay_failed_push_events",
    )
    if not settings.SBI_EPAY_POLLING_ENABLED:
        return
    interval = settings.SBI_EPAY_POLLING_INTERVAL
    # expiring stale runs stops a backlog of polls executing back-to-back
    sender.add_periodic_task(
        interval,
        poll_pending_payments.s().set(expires=interval),
        name="sbiepay_poll_pending_payments",
    )
