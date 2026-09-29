from django.db import models
from django.db.models import Q

from care.utils.models.base import BaseModel


class SbiEpayPayment(BaseModel):
    class Status(models.TextChoices):
        CREATED = "created"
        PAID = "paid"
        FAILED = "failed"
        EXPIRED = "expired"
        CANCELLED = "cancelled"
        SUPERSEDED = "superseded"

    order_number = models.CharField(max_length=15, unique=True)
    invoice = models.ForeignKey("emr.Invoice", on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.CREATED
    )
    reference = models.CharField(max_length=255, blank=True, default="")
    payment_url = models.TextField(blank=True, default="")
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    paid_amount = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    reconciliation = models.OneToOneField(
        "emr.PaymentReconciliation",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="sbiepay_payment",
    )
    needs_review = models.BooleanField(default=False, db_index=True)
    review_reason = models.TextField(blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["reference"],
                condition=Q(status="paid") & ~Q(reference=""),
                name="sbiepay_unique_paid_reference",
            ),
        ]

    def flag_for_review(self, reason: str) -> None:
        self.needs_review = True
        self.review_reason = f"{self.review_reason}\n{reason}".strip()
