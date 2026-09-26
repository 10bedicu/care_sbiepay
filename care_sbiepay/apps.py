import logging

from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _

logger = logging.getLogger(__name__)

PLUGIN_NAME = "care_sbiepay"


class CareSbiEpayConfig(AppConfig):
    name = PLUGIN_NAME
    verbose_name = _("Care SBI ePay")

    def ready(self):
        # Register the provider only if the ABDM plug is installed.
        try:
            import care_sbiepay.provider  # noqa: F401
        except ImportError:
            logger.info(
                "care_abdm not installed; skipping SBI ePay provider registration"
            )
