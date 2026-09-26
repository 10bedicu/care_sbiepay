from pydantic import UUID4, BaseModel, field_validator

from care.emr.models.invoice import Invoice


class CreatePaymentLinkRequest(BaseModel):
    invoice_id: UUID4

    @field_validator("invoice_id")
    @classmethod
    def validate_invoice_id(cls, value):
        if value and not Invoice.objects.filter(external_id=value).exists():
            raise ValueError("Invoice not found")
        return value


class PaymentLink(BaseModel):
    order_number: str
    payment_url: str
    status: str
