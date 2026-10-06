from datetime import datetime
from decimal import Decimal

from pydantic import UUID4, BaseModel


class CreatePaymentLinkRequest(BaseModel):
    invoice_id: UUID4


class PaymentLink(BaseModel):
    id: UUID4
    invoice_id: UUID4
    order_number: str
    payment_url: str
    status: str
    amount: Decimal
    paid_amount: Decimal | None = None
    reference: str = ""
    expires_at: datetime | None = None
    needs_review: bool = False
    review_reason: str = ""
    created_date: datetime
    modified_date: datetime

    @classmethod
    def from_payment(cls, payment) -> "PaymentLink":
        return cls(
            id=payment.external_id,
            invoice_id=payment.invoice.external_id,
            order_number=payment.order_number,
            payment_url=payment.payment_url,
            status=payment.status,
            amount=payment.amount,
            paid_amount=payment.paid_amount,
            reference=payment.reference,
            expires_at=payment.expires_at,
            needs_review=payment.needs_review,
            review_reason=payment.review_reason,
            created_date=payment.created_date,
            modified_date=payment.modified_date,
        )
