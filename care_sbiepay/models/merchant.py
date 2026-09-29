from django.db import models

from care.utils.models.base import BaseModel


class SbiEpayMerchant(BaseModel):
    """Per-facility SBI ePay merchant credentials.

    The merchant key is the AES key used to encrypt requests and decrypt
    responses/pushes for this merchant code.
    """

    facility = models.OneToOneField(
        "facility.Facility",
        on_delete=models.PROTECT,
        to_field="external_id",
    )
    merchant_code = models.CharField(max_length=255)
    merchant_key = models.CharField(max_length=255)
    is_enabled = models.BooleanField(default=True)
