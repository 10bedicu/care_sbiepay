from typing import Any

import environ
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.dispatch import receiver
from rest_framework.settings import perform_import

from care_sbiepay.apps import PLUGIN_NAME

env = environ.Env()


class PluginSettings:  # pragma: no cover
    """
    A settings object that allows plugin settings to be accessed as
    properties. For example:

        from plugin.settings import plugin_settings
        print(plugin_settings.API_KEY)

    Any setting with string import paths will be automatically resolved
    and return the class, rather than the string literal.

    """

    def __init__(
        self,
        plugin_name: str | None = None,
        defaults: dict | None = None,
        import_strings: set | None = None,
        required_settings: set | None = None,
    ) -> None:
        if not plugin_name:
            raise ValueError("Plugin name must be provided")
        self.plugin_name = plugin_name
        self.defaults = defaults or {}
        self.import_strings = import_strings or set()
        self.required_settings = required_settings or set()
        self._cached_attrs = set()
        self.validate()

    def __getattr__(self, attr) -> Any:
        if attr not in self.defaults:
            msg = f"Invalid setting: '{attr}'"
            raise AttributeError(msg)

        # Try to find the setting from user settings, then from environment variables
        val = self.defaults[attr]
        try:
            val = self.user_settings[attr]
        except KeyError:
            try:
                val = env(attr, cast=type(val))
            except environ.ImproperlyConfigured:
                # Fall back to defaults
                pass

        # Coerce import strings into classes
        if attr in self.import_strings:
            val = perform_import(val, attr)

        self._cached_attrs.add(attr)
        setattr(self, attr, val)
        return val

    @property
    def user_settings(self) -> dict:
        if not hasattr(self, "_user_settings"):
            self._user_settings = getattr(settings, "PLUGIN_CONFIGS", {}).get(
                self.plugin_name, {}
            )
        return self._user_settings

    def validate(self) -> None:
        """
        This method handles the validation of the plugin settings.
        It could be overridden to provide custom validation logic.

        the base implementation checks if all the required settings are truthy.
        """
        for setting in self.required_settings:
            if not getattr(self, setting):
                msg = (
                    f'The "{setting}" setting is required. '
                    f'Please set the "{setting}" in the environment or the {PLUGIN_NAME} plugin config.'
                )
                raise ImproperlyConfigured(msg)

    def reload(self) -> None:
        """
        Deletes the cached attributes so they will be recomputed next time they are accessed.
        """
        for attr in self._cached_attrs:
            delattr(self, attr)
        self._cached_attrs.clear()
        if hasattr(self, "_user_settings"):
            delattr(self, "_user_settings")


REQUIRED_SETTINGS = set()

DEFAULTS = {
    "SBI_EPAY_BASE_URL": "https://epay.sbiuat.bank.in",
    "SBI_EPAY_API_KEY_ID": "",
    "SBI_EPAY_API_SECRET_KEY": "",
    "SBI_EPAY_SOURCE_URL": "",
    "SBI_EPAY_REQUEST_TIMEOUT": 30,
    "SBI_EPAY_PAID_RESPONSE_STATUSES": "SUCCESS",
    "SBI_EPAY_FAILED_RESPONSE_STATUSES": "FAILURE,FAILED,ABORTED,INVALID",
    "SBI_EPAY_CANCELLED_RESPONSE_STATUSES": "CANCELLED,CANCELED",
    "SBI_EPAY_EXPIRED_RESPONSE_STATUSES": "EXPIRED",
    "SBI_EPAY_POLLING_ENABLED": True,
    "SBI_EPAY_POLLING_INTERVAL": 10,
    "SBI_EPAY_PAYMENT_MAX_AGE": 3600,
    "SBI_EPAY_CURRENCY": "INR",
    "SBI_EPAY_EXPIRY_GRACE_SECONDS": 900,
    "SBI_EPAY_EXPIRY_HARD_CAP_SECONDS": 86400,
    "SBI_EPAY_PUSH_REPLAY_INTERVAL": 60,
    "SBI_EPAY_PUSH_MAX_ATTEMPTS": 5,
    "SBI_EPAY_PUSH_MAX_BYTES": 16384,
}

plugin_settings = PluginSettings(
    PLUGIN_NAME, defaults=DEFAULTS, required_settings=REQUIRED_SETTINGS
)


@receiver(setting_changed)
def reload_plugin_settings(*args, **kwargs) -> None:
    setting = kwargs["setting"]
    if setting == "PLUGIN_CONFIGS":
        plugin_settings.reload()
