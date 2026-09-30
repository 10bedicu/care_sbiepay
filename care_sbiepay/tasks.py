import logging

from celery import current_app, shared_task

from care.utils.lock import Lock, ObjectLocked
from care_sbiepay import payments, push_events
from care_sbiepay.settings import plugin_settings as settings

logger = logging.getLogger(__name__)

POLL_LOCK_KEY = "sbiepay:poll_pending_payments"
POLL_LOCK_TIMEOUT = 300


@shared_task
def poll_pending_payments() -> None:
    if not settings.SBI_EPAY_POLLING_ENABLED:
        return
    try:
        with Lock(POLL_LOCK_KEY, POLL_LOCK_TIMEOUT):
            payments.poll_pending_payments()
    except ObjectLocked:
        logger.info("Previous SBI ePay poll is still running; skipping this tick")


@shared_task
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
    sender.add_periodic_task(
        settings.SBI_EPAY_POLLING_INTERVAL,
        poll_pending_payments.s(),
        name="sbiepay_poll_pending_payments",
    )
