from django.db import models
from django.utils import timezone

from care.utils.models.base import BaseModel


class SbiEpayPushEvent(BaseModel):
    class Status(models.TextChoices):
        RECEIVED = "received"
        PROCESSED = "processed"
        IGNORED = "ignored"
        FAILED = "failed"

    payload_hash = models.CharField(max_length=64, unique=True)
    merchant_code = models.CharField(max_length=255, blank=True, default="")
    order_number = models.CharField(
        max_length=100, blank=True, default="", db_index=True
    )
    gateway_status = models.CharField(max_length=64, blank=True, default="")
    amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=16, blank=True, default="")
    reference = models.CharField(max_length=255, blank=True, default="")
    raw_form = models.JSONField(default=dict, blank=True)
    decoded = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.RECEIVED, db_index=True
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    error_message = models.TextField(blank=True, default="")
    processed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.order_number or self.payload_hash[:12]}:{self.status}"

    def mark_processed(self, status: str = Status.PROCESSED) -> None:
        self.status = status
        self.processed_at = timezone.now()
        self.error_message = ""
        self.save(
            update_fields=["status", "processed_at", "error_message", "modified_date"]
        )

    def mark_failed(self, error_message: str) -> None:
        self.status = self.Status.FAILED
        self.attempts = (self.attempts or 0) + 1
        self.error_message = (error_message or "")[:8000]
        self.save(
            update_fields=["status", "attempts", "error_message", "modified_date"]
        )
