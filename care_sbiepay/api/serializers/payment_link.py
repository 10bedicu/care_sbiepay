from datetime import datetime
from decimal import Decimal

from pydantic import UUID4, BaseModel


class CreatePaymentLinkRequest(BaseModel):
    invoice_id: UUID4


class PaymentLink(BaseModel):
    order_number: str
    payment_url: str
    status: str
    amount: Decimal
    expires_at: datetime | None = None

    @classmethod
    def from_payment(cls, payment) -> "PaymentLink":
        return cls(
            order_number=payment.order_number,
            payment_url=payment.payment_url,
            status=payment.status,
            amount=payment.amount,
            expires_at=payment.expires_at,
        )
