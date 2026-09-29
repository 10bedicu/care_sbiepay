from django.db import models

from care.utils.models.base import BaseModel


class SbiEpayPayment(BaseModel):
    """Tracks a standalone SBI ePay payment so pushes/polling can reconcile it.

    Independent of ABDM; scan-and-pay orders are tracked by ABDM's PaymentOrder.
    """

    class Status(models.TextChoices):
        CREATED = "created"
        PAID = "paid"
        FAILED = "failed"
        EXPIRED = "expired"
        CANCELLED = "cancelled"

    order_number = models.CharField(max_length=15, unique=True)
    invoice = models.ForeignKey("emr.Invoice", on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.CREATED
    )
    reference = models.CharField(max_length=255, blank=True, default="")
    payment_url = models.TextField(blank=True, default="")
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
