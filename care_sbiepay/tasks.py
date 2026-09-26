import logging

from celery import current_app, shared_task

from care_sbiepay import payments
from care_sbiepay.settings import plugin_settings as settings

logger = logging.getLogger(__name__)


@shared_task
def poll_pending_payments() -> None:
    if not settings.SBI_EPAY_POLLING_ENABLED:
        return
    payments.poll_pending_payments()


@current_app.on_after_finalize.connect
def setup_periodic_tasks(sender, **kwargs) -> None:
    if not settings.SBI_EPAY_POLLING_ENABLED:
        return
    sender.add_periodic_task(
        settings.SBI_EPAY_POLLING_INTERVAL,
        poll_pending_payments.s(),
        name="sbiepay_poll_pending_payments",
    )
