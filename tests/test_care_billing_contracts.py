from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

from care_sbiepay.locks import SbiEpayPaymentLock
from care_sbiepay.models import SbiEpayPayment
from care_sbiepay.utils import client
from model_bakery import baker

from care.emr.locks.billing import InvoiceLock
from care.emr.models.charge_item import ChargeItem
from care.emr.models.payment_reconciliation import PaymentReconciliation
from care.emr.resources.charge_item.spec import ChargeItemStatusOptions
from care.emr.resources.invoice.spec import InvoiceStatusOptions
from care.emr.resources.payment_reconciliation.spec import (
    PaymentReconciliationOutcomeOptions,
    PaymentReconciliationStatusOptions,
    PaymentReconciliationTypeOptions,
)
from care.security.permissions.payment_reconciliation import (
    PaymentReconciliationPermissions,
)
from care.utils.lock import ObjectLocked
from care.utils.rounding.rounding import care_round
from care_sbiepay import payments
from tests.test_sbiepay import PAYMENT_LINK_URL, SbiEpayTestBase

MERCHANT_URL = "/api/care_sbiepay/merchant/"


class CareBillingContractTests(SbiEpayTestBase):
    def setUp(self):
        super().setUp()
        self.staff = self.create_user()
        role = self.create_role_with_permissions(
            [
                PaymentReconciliationPermissions.can_write_payment_reconciliation.name,
                PaymentReconciliationPermissions.can_read_payment_reconciliation.name,
            ]
        )
        self.organization = self.create_facility_organization(self.facility)
        self.attach_role_facility_organization_user(
            self.organization, self.staff, role
        )
        self.client.force_authenticate(user=self.staff)

    def record_payment(self, invoice, amount, reference="paid"):
        return baker.make(
            PaymentReconciliation,
            facility=self.facility,
            account=self.account,
            target_invoice=invoice,
            reconciliation_type=PaymentReconciliationTypeOptions.payment.value,
            status=PaymentReconciliationStatusOptions.active.value,
            outcome=PaymentReconciliationOutcomeOptions.complete.value,
            amount=Decimal(amount),
            tendered_amount=Decimal(amount),
            returned_amount=Decimal(0),
            is_credit_note=False,
            reference_number=reference,
        )

    @patch("care_sbiepay.payments.client.create_payment_link")
    def test_payment_link_uses_the_invoice_lock_and_outstanding_balance(
        self, mock_create
    ):
        invoice = self.make_invoice("INV-CONTRACT")
        self.record_payment(invoice, Decimal("30"))
        outstanding = care_round(invoice.total_gross - Decimal("30"))
        mock_create.return_value = {"paymentUrl": "https://epay.example/pay"}

        response = self.client.post(
            PAYMENT_LINK_URL,
            {"invoice_id": str(invoice.external_id)},
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(mock_create.call_args.kwargs["amount"], outstanding)
        payment = SbiEpayPayment.objects.get(invoice=invoice)
        self.assertEqual(payment.amount, outstanding)

        draft = self.make_invoice("INV-DRAFT")
        draft.status = InvoiceStatusOptions.draft.value
        draft.save(update_fields=["status"])
        rejected = self.client.post(
            PAYMENT_LINK_URL,
            {"invoice_id": str(draft.external_id)},
            format="json",
        )
        self.assertEqual(rejected.status_code, 400)

    def test_locked_invoice_is_a_client_error(self):
        invoice = self.make_invoice("INV-LOCKED")
        with InvoiceLock(invoice):
            response = self.client.post(
                PAYMENT_LINK_URL,
                {"invoice_id": str(invoice.external_id)},
                format="json",
            )
        self.assertEqual(response.status_code, 423)

    def test_settlement_rebalances_the_account_under_the_payment_lock(self):
        payment = self.make_payment("sbiordercontr", "INV-SETTLE")
        baker.make(
            ChargeItem,
            facility=self.facility,
            patient=self.patient,
            account=self.account,
            status=ChargeItemStatusOptions.billable.value,
            title="Still open",
            quantity=1,
            total_price=Decimal(25),
        )

        with self.captureOnCommitCallbacks(execute=True):
            recorded = payments.reconcile_payment(
                payment,
                client.Confirmation(
                    order_number=payment.order_number,
                    status="SUCCESS",
                    reference="ATRN-CONTRACT",
                    amount=payment.amount,
                    currency="INR",
                    merchant_code=self.merchant.merchant_code,
                ),
            )

        self.assertTrue(recorded)
        payment.refresh_from_db()
        self.account.refresh_from_db()
        self.assertEqual(payment.status, SbiEpayPayment.Status.PAID)
        self.assertEqual(self.account.total_gross, payment.invoice.total_gross)
        self.assertEqual(self.account.total_paid, payment.amount)
        self.assertEqual(self.account.total_billable_charge_items, Decimal(25))

        with SbiEpayPaymentLock(payment), self.assertRaises(ObjectLocked):
            payments.reconcile_payment(
                payment,
                client.Confirmation(
                    order_number=payment.order_number,
                    status="SUCCESS",
                    reference="ATRN-AGAIN",
                    amount=payment.amount,
                    currency="INR",
                    merchant_code=self.merchant.merchant_code,
                ),
            )

    def test_collection_permission_and_merchant_membership(self):
        self.client.force_authenticate(user=self.create_user())
        denied = self.client.post(
            PAYMENT_LINK_URL,
            {"invoice_id": str(self.make_invoice("INV-403").external_id)},
            format="json",
        )
        self.assertEqual(denied.status_code, 403)

        self.client.force_authenticate(user=self.staff)
        opened = self.client.get(f"{MERCHANT_URL}{self.facility.external_id}/")
        self.assertEqual(opened.status_code, 200, getattr(opened, "data", opened.content))

        self.client.force_authenticate(user=self.create_user())
        hidden = self.client.get(f"{MERCHANT_URL}{self.facility.external_id}/")
        self.assertEqual(hidden.status_code, 404)

    def test_invalid_payment_link_body_is_a_client_error(self):
        response = self.client.post(PAYMENT_LINK_URL, {}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("errors", response.data)
