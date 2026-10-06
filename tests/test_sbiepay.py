from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

import requests
from abdm.models import AbhaNumber, HealthFacility, PaymentOrder
from abdm.models.payment_order import PaymentOrderStatus
from abdm.service.helper import uuid
from abdm.utils import user as abdm_user
from care_sbiepay.locks import SbiEpayPaymentLock
from care_sbiepay.models import SbiEpayMerchant, SbiEpayPayment, SbiEpayPushEvent
from care_sbiepay.settings import plugin_settings as settings
from care_sbiepay.utils import client, crypto
from django.core.cache import cache
from django.test import SimpleTestCase
from model_bakery import baker

from care.emr.models.invoice import Invoice
from care.emr.models.payment_reconciliation import PaymentReconciliation
from care.emr.resources.invoice.spec import InvoiceStatusOptions
from care.emr.resources.payment_reconciliation.spec import (
    PaymentReconciliationOutcomeOptions,
    PaymentReconciliationStatusOptions,
    PaymentReconciliationTypeOptions,
)
from care.security.permissions.payment_reconciliation import (
    PaymentReconciliationPermissions,
)
from care.utils.lock import Lock, ObjectLocked
from care.utils.tests.base import CareAPITestBase
from care.utils.time_util import care_now
from care_sbiepay import payments, push_events, tasks
from care_sbiepay import provider as sbiepay_provider

MERCHANT_KEY = "/IIvvWMcy5ls/V2hCNZ5/Q=="

# Produced with OpenSSL (aes-128-cbc, key = iv = first 16 bytes of MERCHANT_KEY),
# independent of pycryptodome, matching SBI's Java AES256Bit reference.
KAT_PLAIN = "hello world"
KAT_CIPHER = "z3b4grYIETCxgwGKTleddw=="
KAT_PUSH_BODY = "order123|ATRN1|SUCCESS|100.00|INR|"
KAT_PUSH = (
    "vqraD2JfYECmkSXxUmw5jGhFAfc/qy/4j5txB2m0F3y6c1EOj19RUWWiI2TLR7VNskPQgKl8fZ0gWSTV"
    "X3TtoJWHpUQdiQ680cKWZrHFN+IIPv4yhgT2zvssGz5U6vTZJjRmfk9m0D4RejawuWYqv69YzMv0e7p5"
    "uY4efgoxLmsKqWDjeIK76/UMJt4oo9VRaaKYCsh1k3W1j3/7U67U8ddsNHR8t8+R5fiBGdD8QhQ="
)


class TestSbiEpayCrypto(SimpleTestCase):
    def test_encrypt_decrypt_roundtrip(self):
        enc = crypto.encrypt(MERCHANT_KEY, "hello world")
        self.assertEqual(crypto.decrypt(MERCHANT_KEY, enc), "hello world")

    def test_matches_reference_vector(self):
        self.assertEqual(crypto.encrypt(MERCHANT_KEY, KAT_PLAIN), KAT_CIPHER)
        self.assertEqual(crypto.decrypt(MERCHANT_KEY, KAT_CIPHER), KAT_PLAIN)

    def test_parses_reference_push(self):
        push = client.parse_push_response(MERCHANT_KEY, KAT_PUSH)

        self.assertEqual(push["merch_order_no"], "order123")
        self.assertEqual(push["atrn"], "ATRN1")
        self.assertEqual(push["status"], "SUCCESS")
        self.assertEqual(push["amount"], "100.00")
        self.assertEqual(push["currency"], "INR")

    def test_rejects_tampered_push(self):
        body = KAT_PUSH_BODY.replace("100.00", "1.00")
        tampered = crypto.encrypt(MERCHANT_KEY, body + crypto.checksum(KAT_PUSH_BODY))

        with self.assertRaises(client.SbiEpayError):
            client.parse_push_response(MERCHANT_KEY, tampered)


class TestSbiEpayClient(SimpleTestCase):
    def test_order_validity_uses_max_age(self):
        with patch.object(client.settings, "SBI_EPAY_PAYMENT_MAX_AGE", 60):
            validity = client.order_validity()

        expected = datetime.now(client.IST) + timedelta(seconds=60)
        self.assertAlmostEqual(validity, expected, delta=timedelta(seconds=5))

    def test_order_validity_is_capped_at_end_of_ist_day(self):
        with patch.object(client.settings, "SBI_EPAY_PAYMENT_MAX_AGE", 10 * 86400):
            validity = client.order_validity()

        now = datetime.now(client.IST)
        self.assertEqual(validity.date(), now.date())
        self.assertEqual(
            (validity.hour, validity.minute, validity.second), (23, 59, 59)
        )

    @patch("care_sbiepay.utils.client._post_encrypted")
    def test_create_payment_link_sends_validity_in_ist(self, mock_post):
        validity = datetime(2030, 1, 15, 10, 30, tzinfo=client.IST)

        client.create_payment_link(
            object(),
            merch_order_no="abc123",
            amount=Decimal(100),
            other_details="CARE",
            validity=validity,
        )

        req = mock_post.call_args.args[2]
        self.assertEqual(req["merchOrderNoValidity"], "15/01/2030 10:30:00")
        self.assertEqual(req["amount"], "100.00")

    def _merchant(self):
        return MagicMock(merchant_key=MERCHANT_KEY, merchant_code="1000755")

    def _gateway_response(self, plain: str, cs: str | None):
        response = MagicMock(status_code=200)
        body = {"encData": crypto.encrypt(MERCHANT_KEY, plain)}
        if cs is not None:
            body["cs"] = cs
        response.json.return_value = body
        return response

    @patch("care_sbiepay.utils.client.get_token", return_value="Bearer t")
    @patch("care_sbiepay.utils.client.requests.post")
    def test_response_checksum_is_verified(self, mock_post, _token):
        plain = '{"res":{"Response Status":"SUCCESS"}}'
        mock_post.return_value = self._gateway_response(plain, crypto.checksum(plain))

        result = client.status_query(
            self._merchant(), merch_order_no="abc", amount=Decimal(1)
        )

        self.assertEqual(result["Response Status"], "SUCCESS")

    @patch("care_sbiepay.utils.client.get_token", return_value="Bearer t")
    @patch("care_sbiepay.utils.client.requests.post")
    def test_response_checksum_mismatch_is_rejected(self, mock_post, _token):
        plain = '{"res":{"Response Status":"SUCCESS"}}'
        mock_post.return_value = self._gateway_response(plain, crypto.checksum("x"))

        with self.assertRaises(client.SbiEpayError):
            client.status_query(
                self._merchant(), merch_order_no="abc", amount=Decimal(1)
            )

    def test_confirmation_from_push_reads_amount_and_merchant(self):
        confirmation = client.confirmation_from_push(
            {
                "merch_order_no": "o1",
                "status": "success",
                "atrn": "",
                "bank_ref_number": "BR1",
                "amount": "100.00",
                "currency": "inr",
                "merchant_id": "1000755",
            }
        )

        self.assertEqual(confirmation.status, "SUCCESS")
        self.assertEqual(confirmation.reference, "BR1")
        self.assertEqual(confirmation.amount, Decimal("100.00"))
        self.assertEqual(confirmation.currency, "INR")
        self.assertEqual(confirmation.merchant_code, "1000755")

    def test_confirmation_tolerates_missing_or_bad_amount(self):
        self.assertIsNone(client.confirmation_from_push({"amount": "abc"}).amount)
        self.assertIsNone(
            client.confirmation_from_status({"Response Status": "PENDING"}, "o1").amount
        )
        self.assertEqual(client.confirmation_from_status({}, "o1").order_number, "o1")


class SbiEpayTestBase(CareAPITestBase):
    def setUp(self):
        # cached across tests but each test rolls back; reset to avoid stale FK.
        abdm_user.ABDM_USER = None
        self.user = self.create_super_user()
        self.facility = self.create_facility(self.user)
        self.health_facility = baker.make(
            HealthFacility, facility=self.facility, hf_id="TEST_HIP"
        )
        self.patient = self.create_patient(phone_number="+919999999999")
        self.abha_number = baker.make(
            AbhaNumber,
            patient=self.patient,
            health_id="testpatient@sbx",
            abha_number="91123456789012",
        )
        self.account = baker.make(
            "emr.Account", facility=self.facility, patient=self.patient
        )
        self.merchant = SbiEpayMerchant.objects.create(
            facility=self.facility,
            merchant_code="1000755",
            merchant_key=MERCHANT_KEY,
        )

    def make_invoice(self, number="INV-SBI"):
        return baker.make(
            Invoice,
            facility=self.facility,
            patient=self.patient,
            account=self.account,
            status=InvoiceStatusOptions.issued.value,
            total_gross=Decimal(100),
            number=number,
        )

    def make_order(self):
        invoice = self.make_invoice("INV-SBI-REC")
        return PaymentOrder.objects.create(
            open_order_request_id=uuid(),
            abha_number=self.abha_number,
            health_facility=self.health_facility,
            invoice=invoice,
            order_number="sbiorder123",
            status=PaymentOrderStatus.PAYMENT_INITIATED,
            amount=invoice.total_gross,
        )

    def make_payment(self, order_number, number="INV-PAY", **fields):
        invoice = self.make_invoice(number)
        return SbiEpayPayment.objects.create(
            order_number=order_number,
            invoice=invoice,
            amount=invoice.total_gross,
            **fields,
        )

    def success_push(self, order_number, reference="ATRN1", **overrides):
        push = {
            "merch_order_no": order_number,
            "status": "SUCCESS",
            "atrn": reference,
            "amount": "100.00",
            "currency": "INR",
            "merchant_id": "1000755",
        }
        push.update(overrides)
        return push


class TestSbiEpayProvider(SbiEpayTestBase):
    @patch("care_sbiepay.provider.client.create_payment_link")
    def test_create_payment_link(self, mock_create):
        mock_create.return_value = {
            "merchOrderNo": "abc123",
            "amount": 100,
            "paymentUrl": "https://epay.sbiuat.bank.in/secure/epayPayment.jsp?enctoken=xyz",
        }

        result = sbiepay_provider.SbiEpayProvider().create_payment_link(
            self.make_invoice()
        )

        self.assertEqual(
            result["payment_url"],
            "https://epay.sbiuat.bank.in/secure/epayPayment.jsp?enctoken=xyz",
        )
        self.assertEqual(result["payment_link_id"], result["payment_url"])
        self.assertLessEqual(len(result["order_number"]), 15)
        self.assertTrue(result["order_number"].isalnum())
        self.assertEqual(result["amount"], Decimal(100))
        # order ref is generated by us, not passed straight through
        self.assertEqual(
            mock_create.call_args.kwargs["merch_order_no"], result["order_number"]
        )
        self.assertEqual(mock_create.call_args.args[0], self.merchant)

    @patch("care_sbiepay.provider.client.create_payment_link")
    def test_create_payment_link_expires_when_abdm_gives_up(self, mock_create):
        mock_create.return_value = {"paymentUrl": "https://pay/abdm"}

        with patch.object(
            sbiepay_provider.abdm_settings, "ABDM_SCAN_AND_PAY_ORDER_MAX_AGE", 1800
        ):
            sbiepay_provider.SbiEpayProvider().create_payment_link(self.make_invoice())

        validity = mock_create.call_args.kwargs["validity"]
        expected = datetime.now(client.IST) + timedelta(seconds=1800)
        self.assertAlmostEqual(validity, expected, delta=timedelta(seconds=5))

    def test_create_payment_link_requires_merchant(self):
        self.merchant.delete()

        with self.assertRaises(sbiepay_provider.client.SbiEpayError):
            sbiepay_provider.SbiEpayProvider().create_payment_link(self.make_invoice())


class TestSbiEpayReconcile(SbiEpayTestBase):
    @patch("abdm.tasks.scan_pay.scan_pay_notify.delay")
    @patch("care_sbiepay.provider.client.status_query")
    def test_reconcile_marks_paid_and_notifies(self, mock_status, mock_task):
        mock_status.return_value = {
            "Response Status": "SUCCESS",
            "SBIePayRefID/ATRN": "ATRN123",
        }
        order = self.make_order()

        with self.captureOnCommitCallbacks(execute=True):
            sbiepay_provider.SbiEpayProvider().reconcile_order(order)

        order.refresh_from_db()
        self.assertEqual(order.status, PaymentOrderStatus.SUCCESS)
        self.assertEqual(order.transaction_id, "ATRN123")
        reconciliation = PaymentReconciliation.objects.get(target_invoice=order.invoice)
        self.assertEqual(reconciliation.amount, Decimal(100))
        mock_task.assert_called_once()
        self.assertEqual(mock_status.call_args.args[0], self.merchant)

    @patch("abdm.tasks.scan_pay.scan_pay_notify.delay")
    @patch("care_sbiepay.provider.client.status_query")
    def test_reconcile_uses_order_amount_not_invoice_total(self, mock_status, _task):
        mock_status.return_value = {
            "Response Status": "SUCCESS",
            "SBIePayRefID/ATRN": "ATRN123",
        }
        order = self.make_order()
        # invoice edited after the link was generated
        Invoice.objects.filter(pk=order.invoice_id).update(total_gross=Decimal(150))
        order.refresh_from_db()

        sbiepay_provider.SbiEpayProvider().reconcile_order(order)

        self.assertEqual(mock_status.call_args.kwargs["amount"], Decimal(100))
        reconciliation = PaymentReconciliation.objects.get(target_invoice=order.invoice)
        self.assertEqual(reconciliation.amount, Decimal(100))

    @patch("abdm.tasks.scan_pay.scan_pay_notify.delay")
    def test_push_records_confirmed_amount_and_notes_mismatch(self, _task):
        order = self.make_order()

        sbiepay_provider.reconcile_abdm_push(
            self.success_push("sbiorder123", amount="90.00")
        )

        reconciliation = PaymentReconciliation.objects.get(target_invoice=order.invoice)
        self.assertEqual(reconciliation.amount, Decimal("90.00"))
        self.assertIn("amount mismatch", reconciliation.note)
        order.refresh_from_db()
        # short-paid: ABDM keeps the order pending rather than claiming success
        self.assertEqual(order.status, PaymentOrderStatus.PENDING)

    @patch("abdm.tasks.scan_pay.scan_pay_notify.delay")
    @patch("care_sbiepay.provider.client.status_query")
    def test_reconcile_is_idempotent(self, mock_status, mock_task):
        mock_status.return_value = {
            "Response Status": "SUCCESS",
            "SBIePayRefID/ATRN": "ATRN123",
        }
        order = self.make_order()

        sbiepay_provider.SbiEpayProvider().reconcile_order(order)
        sbiepay_provider.SbiEpayProvider().reconcile_order(order)
        # a push for the same order arriving after polling settled it
        sbiepay_provider.reconcile_abdm_push(
            self.success_push("sbiorder123", reference="ATRN123")
        )

        self.assertEqual(
            PaymentReconciliation.objects.filter(target_invoice=order.invoice).count(),
            1,
        )

    @patch("abdm.tasks.scan_pay.scan_pay_notify.delay")
    @patch("care_sbiepay.provider.client.status_query")
    def test_reconcile_ignores_unpaid(self, mock_status, mock_task):
        mock_status.return_value = {"Response Status": "NA"}
        order = self.make_order()

        sbiepay_provider.SbiEpayProvider().reconcile_order(order)

        order.refresh_from_db()
        self.assertEqual(order.status, PaymentOrderStatus.PAYMENT_INITIATED)
        self.assertFalse(
            PaymentReconciliation.objects.filter(target_invoice=order.invoice).exists()
        )
        mock_task.assert_not_called()

    @patch("abdm.tasks.scan_pay.scan_pay_notify.delay")
    @patch("care_sbiepay.provider.client.status_query")
    def test_reconcile_fails_failed_order(self, mock_status, mock_task):
        mock_status.return_value = {"Response Status": "FAILURE"}
        order = self.make_order()

        with self.captureOnCommitCallbacks(execute=True):
            sbiepay_provider.SbiEpayProvider().reconcile_order(order)

        order.refresh_from_db()
        self.assertEqual(order.status, PaymentOrderStatus.FAIL)
        self.assertEqual(
            order.invoice.status, InvoiceStatusOptions.entered_in_error.value
        )
        self.assertFalse(
            PaymentReconciliation.objects.filter(target_invoice=order.invoice).exists()
        )
        # the PHR is told the payment failed
        payload = mock_task.call_args.args[0]
        self.assertEqual(payload["acknowledgement"]["status"], "FAIL")

    @patch("care_sbiepay.provider.client.status_query")
    def test_reconcile_fails_expired_order(self, mock_status):
        mock_status.return_value = {"Response Status": "EXPIRED"}
        order = self.make_order()

        sbiepay_provider.SbiEpayProvider().reconcile_order(order)

        order.refresh_from_db()
        self.assertEqual(order.status, PaymentOrderStatus.FAIL)

    @patch("care_sbiepay.provider.client.status_query")
    def test_reconcile_cancels_cancelled_order(self, mock_status):
        mock_status.return_value = {"Response Status": "CANCELLED"}
        order = self.make_order()

        sbiepay_provider.SbiEpayProvider().reconcile_order(order)

        order.refresh_from_db()
        self.assertEqual(order.status, PaymentOrderStatus.CANCELED)

    @patch("abdm.tasks.scan_pay.scan_pay_notify.delay")
    def test_push_marks_paid_and_notifies(self, mock_task):
        order = self.make_order()

        with self.captureOnCommitCallbacks(execute=True):
            handled = sbiepay_provider.reconcile_abdm_push(
                {
                    "merch_order_no": "sbiorder123",
                    "status": "SUCCESS",
                    "atrn": "ATRN999",
                    "bank_ref_number": "BR123",
                }
            )

        self.assertTrue(handled)
        order.refresh_from_db()
        self.assertEqual(order.status, PaymentOrderStatus.SUCCESS)
        self.assertEqual(order.transaction_id, "ATRN999")
        self.assertTrue(
            PaymentReconciliation.objects.filter(target_invoice=order.invoice).exists()
        )
        mock_task.assert_called_once()

    @patch("abdm.tasks.scan_pay.scan_pay_notify.delay")
    def test_push_ignores_unpaid(self, mock_task):
        order = self.make_order()

        handled = sbiepay_provider.reconcile_abdm_push(
            {"merch_order_no": "sbiorder123", "status": "FAILED"}
        )

        self.assertTrue(handled)
        order.refresh_from_db()
        self.assertEqual(order.status, PaymentOrderStatus.PAYMENT_INITIATED)
        mock_task.assert_not_called()

    def test_push_for_unknown_order_is_not_handled(self):
        self.assertFalse(
            sbiepay_provider.reconcile_abdm_push(self.success_push("nosuchorder"))
        )

    @patch("abdm.tasks.scan_pay.scan_pay_notify.delay")
    @patch("care_sbiepay.provider.client.status_query")
    def test_late_success_after_order_failed_is_still_recorded(
        self, mock_status, mock_task
    ):
        mock_status.return_value = {"Response Status": "FAILURE"}
        order = self.make_order()
        sbiepay_provider.SbiEpayProvider().reconcile_order(order)
        order.refresh_from_db()
        self.assertEqual(order.status, PaymentOrderStatus.FAIL)

        with self.captureOnCommitCallbacks(execute=True):
            sbiepay_provider.reconcile_abdm_push(self.success_push("sbiorder123"))

        order.refresh_from_db()
        self.assertEqual(order.status, PaymentOrderStatus.SUCCESS)
        reconciliation = PaymentReconciliation.objects.get(target_invoice=order.invoice)
        self.assertIn("voided", reconciliation.note)
        mock_task.assert_called_once()


class TestSbiEpayStandalonePayment(SbiEpayTestBase):
    def record_payment(self, invoice, amount, is_credit_note=False, reference="x"):
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
            is_credit_note=is_credit_note,
            reference_number=reference,
        )

    @patch("care_sbiepay.payments.client.create_payment_link")
    def test_create_payment_persists_record(self, mock_create):
        mock_create.return_value = {
            "paymentUrl": "https://epay.sbiuat.bank.in/secure/epayPayment.jsp?enctoken=xyz",
        }

        payment = payments.create_payment(self.make_invoice())

        self.assertEqual(payment.status, SbiEpayPayment.Status.CREATED)
        self.assertEqual(payment.amount, Decimal(100))
        self.assertEqual(
            payment.payment_url,
            "https://epay.sbiuat.bank.in/secure/epayPayment.jsp?enctoken=xyz",
        )
        self.assertLessEqual(len(payment.order_number), 15)
        self.assertEqual(
            mock_create.call_args.kwargs["merch_order_no"], payment.order_number
        )
        self.assertEqual(mock_create.call_args.args[0], self.merchant)
        # the validity sent to SBI is what polling stops at
        self.assertEqual(payment.expires_at, mock_create.call_args.kwargs["validity"])
        expected = care_now() + timedelta(seconds=settings.SBI_EPAY_PAYMENT_MAX_AGE)
        self.assertLessEqual(payment.expires_at, expected + timedelta(seconds=5))
        self.assertGreater(payment.expires_at, care_now())

    @patch("care_sbiepay.payments.client.create_payment_link")
    def test_create_payment_charges_outstanding_balance(self, mock_create):
        mock_create.return_value = {"paymentUrl": "https://pay"}
        invoice = self.make_invoice()
        self.record_payment(invoice, 30, reference="cash1")
        self.record_payment(invoice, 10, is_credit_note=True, reference="cn1")

        payment = payments.create_payment(invoice)

        self.assertEqual(payment.amount, Decimal("80.00"))
        self.assertEqual(mock_create.call_args.kwargs["amount"], Decimal("80.00"))

    @patch("care_sbiepay.payments.client.create_payment_link")
    def test_create_payment_rejects_settled_invoice(self, mock_create):
        invoice = self.make_invoice()
        self.record_payment(invoice, 100)

        with self.assertRaises(payments.client.SbiEpayError):
            payments.create_payment(invoice)
        mock_create.assert_not_called()

    def test_create_payment_requires_merchant(self):
        self.merchant.delete()

        with self.assertRaises(payments.client.SbiEpayError):
            payments.create_payment(self.make_invoice())

    def test_create_payment_rejects_disabled_merchant(self):
        self.merchant.is_enabled = False
        self.merchant.save()

        with self.assertRaises(payments.client.SbiEpayError):
            payments.create_payment(self.make_invoice())

    @patch("care_sbiepay.payments.rebalance_account_task.delay")
    def test_push_reconciles_standalone_payment(self, mock_rebalance):
        invoice = self.make_invoice("INV-STANDALONE")
        payment = SbiEpayPayment.objects.create(
            order_number="standalone123",
            invoice=invoice,
            amount=invoice.total_gross,
        )

        with self.captureOnCommitCallbacks(execute=True):
            payments.reconcile_push(self.success_push("standalone123", "ATRN555"))

        payment.refresh_from_db()
        self.assertEqual(payment.status, SbiEpayPayment.Status.PAID)
        self.assertEqual(payment.reference, "ATRN555")
        self.assertEqual(payment.paid_amount, Decimal("100.00"))
        self.assertFalse(payment.needs_review)
        reconciliation = PaymentReconciliation.objects.get(target_invoice=invoice)
        self.assertEqual(payment.reconciliation, reconciliation)
        self.assertEqual(reconciliation.amount, Decimal("100.00"))
        self.assertEqual(reconciliation.reference_number, "ATRN555")
        mock_rebalance.assert_called_once_with(self.account.id)

    @patch("care_sbiepay.payments.rebalance_account_task.delay")
    def test_rebalance_waits_for_commit(self, mock_rebalance):
        self.make_payment("commit1234", "INV-COMMIT")

        with self.captureOnCommitCallbacks() as callbacks:
            payments.reconcile_push(self.success_push("commit1234"))

        mock_rebalance.assert_not_called()
        self.assertEqual(len(callbacks), 1)

    def test_replayed_confirmation_credits_once(self):
        payment = self.make_payment("replay12345", "INV-REPLAY")
        push = self.success_push("replay12345", "ATRN-R")

        payments.reconcile_push(push)
        payments.reconcile_push(push)
        payments.reconcile_push(dict(push, atrn=""))

        self.assertEqual(
            PaymentReconciliation.objects.filter(
                target_invoice=payment.invoice
            ).count(),
            1,
        )

    def test_stale_snapshots_cannot_double_credit(self):
        payment = self.make_payment("stale1234567", "INV-STALE")
        # two workers each loaded the row while it was still CREATED
        snapshot_a = SbiEpayPayment.objects.get(pk=payment.pk)
        snapshot_b = SbiEpayPayment.objects.get(pk=payment.pk)
        confirmation = client.confirmation_from_push(
            self.success_push("stale1234567", "ATRN-S")
        )

        self.assertTrue(payments.reconcile_payment(snapshot_a, confirmation))
        self.assertEqual(snapshot_b.status, SbiEpayPayment.Status.CREATED)
        self.assertFalse(payments.reconcile_payment(snapshot_b, confirmation))

        self.assertEqual(
            PaymentReconciliation.objects.filter(
                target_invoice=payment.invoice
            ).count(),
            1,
        )

    def test_settlement_in_progress_elsewhere_raises(self):
        payment = self.make_payment("locked1234567", "INV-LOCK")
        confirmation = client.confirmation_from_push(self.success_push("locked1234567"))

        with SbiEpayPaymentLock(payment), self.assertRaises(ObjectLocked):
            payments.reconcile_payment(payment, confirmation)

        payment.refresh_from_db()
        self.assertEqual(payment.status, SbiEpayPayment.Status.CREATED)
        self.assertFalse(
            PaymentReconciliation.objects.filter(
                target_invoice=payment.invoice
            ).exists()
        )

    def test_confirmed_amount_is_recorded_and_mismatch_flagged(self):
        payment = self.make_payment("amount1234567", "INV-AMT")
        # invoice edited after the link went out
        Invoice.objects.filter(pk=payment.invoice_id).update(total_gross=Decimal(150))

        payments.reconcile_push(self.success_push("amount1234567", amount="90.00"))

        payment.refresh_from_db()
        self.assertEqual(payment.status, SbiEpayPayment.Status.PAID)
        self.assertEqual(payment.paid_amount, Decimal("90.00"))
        self.assertTrue(payment.needs_review)
        self.assertIn("amount mismatch", payment.review_reason)
        reconciliation = PaymentReconciliation.objects.get(
            target_invoice=payment.invoice
        )
        self.assertEqual(reconciliation.amount, Decimal("90.00"))
        self.assertIn("Needs review", reconciliation.note)

    def test_confirmation_without_amount_records_link_amount(self):
        payment = self.make_payment("noamount12345", "INV-NOAMT")
        Invoice.objects.filter(pk=payment.invoice_id).update(total_gross=Decimal(150))

        payments.reconcile_push(
            {"merch_order_no": "noamount12345", "status": "SUCCESS", "atrn": "A"}
        )

        reconciliation = PaymentReconciliation.objects.get(
            target_invoice=payment.invoice
        )
        self.assertEqual(reconciliation.amount, Decimal(100))
        payment.refresh_from_db()
        self.assertFalse(payment.needs_review)

    def test_merchant_and_currency_mismatch_are_flagged(self):
        payment = self.make_payment("merchant12345", "INV-MERCH")

        payments.reconcile_push(
            self.success_push("merchant12345", merchant_id="9999999", currency="USD")
        )

        payment.refresh_from_db()
        self.assertEqual(payment.status, SbiEpayPayment.Status.PAID)
        self.assertTrue(payment.needs_review)
        self.assertIn("merchant mismatch", payment.review_reason)
        self.assertIn("currency mismatch", payment.review_reason)

    def test_confirmation_for_another_order_is_refused(self):
        payment = self.make_payment("orderA1234567", "INV-A")
        confirmation = client.confirmation_from_push(self.success_push("orderB1234567"))

        with self.assertRaises(client.SbiEpayError):
            payments.reconcile_payment(payment, confirmation)
        self.assertFalse(
            PaymentReconciliation.objects.filter(
                target_invoice=payment.invoice
            ).exists()
        )

    @patch("care_sbiepay.payments.rebalance_account_task.delay")
    def test_push_keeps_pending_payment_created(self, mock_rebalance):
        invoice = self.make_invoice("INV-STANDALONE-2")
        payment = SbiEpayPayment.objects.create(
            order_number="standalone456",
            invoice=invoice,
            amount=invoice.total_gross,
        )

        payments.reconcile_push(
            {"merch_order_no": "standalone456", "status": "PENDING"}
        )

        payment.refresh_from_db()
        self.assertEqual(payment.status, SbiEpayPayment.Status.CREATED)
        self.assertFalse(
            PaymentReconciliation.objects.filter(target_invoice=invoice).exists()
        )
        mock_rebalance.assert_not_called()

    def test_push_marks_failed_standalone_payment(self):
        invoice = self.make_invoice("INV-STANDALONE-3")
        payment = SbiEpayPayment.objects.create(
            order_number="standalone789",
            invoice=invoice,
            amount=invoice.total_gross,
        )

        payments.reconcile_push(
            {"merch_order_no": "standalone789", "status": "FAILURE"}
        )

        payment.refresh_from_db()
        self.assertEqual(payment.status, SbiEpayPayment.Status.FAILED)
        self.assertFalse(
            PaymentReconciliation.objects.filter(target_invoice=invoice).exists()
        )

    def test_push_returns_false_for_unknown_order(self):
        self.assertFalse(
            payments.reconcile_push({"merch_order_no": "unknown", "status": "SUCCESS"})
        )

    @patch("care_sbiepay.payments.rebalance_account_task.delay")
    def test_push_settles_locally_expired_payment(self, mock_rebalance):
        payment = self.make_payment(
            "lateorder123", "INV-LATE", status=SbiEpayPayment.Status.EXPIRED
        )

        with self.captureOnCommitCallbacks(execute=True):
            payments.reconcile_push(self.success_push("lateorder123", "ATRN9"))

        payment.refresh_from_db()
        self.assertEqual(payment.status, SbiEpayPayment.Status.PAID)
        self.assertEqual(payment.reference, "ATRN9")
        self.assertTrue(payment.needs_review)
        self.assertIn("settled from status expired", payment.review_reason)
        self.assertTrue(
            PaymentReconciliation.objects.filter(
                target_invoice=payment.invoice
            ).exists()
        )
        mock_rebalance.assert_called_once()

    def test_push_settles_failed_and_superseded_payments(self):
        for status in (SbiEpayPayment.Status.FAILED, SbiEpayPayment.Status.SUPERSEDED):
            order = f"late{status[:8]}"
            payment = self.make_payment(order, f"INV-{status}", status=status)

            payments.reconcile_push(self.success_push(order, f"ATRN-{status}"))

            payment.refresh_from_db()
            self.assertEqual(payment.status, SbiEpayPayment.Status.PAID)
            self.assertIn(f"settled from status {status}", payment.review_reason)

    def test_terminal_marks_never_overwrite_each_other(self):
        payment = self.make_payment(
            "superseded123", "INV-SUP", status=SbiEpayPayment.Status.SUPERSEDED
        )

        payments.reconcile_push(
            {"merch_order_no": "superseded123", "status": "FAILURE"}
        )

        payment.refresh_from_db()
        self.assertEqual(payment.status, SbiEpayPayment.Status.SUPERSEDED)

    def test_push_marks_expired_standalone_payment(self):
        payment = self.make_payment("expiredorder1", "INV-GW-EXPIRED")

        payments.reconcile_push(
            {"merch_order_no": "expiredorder1", "status": "EXPIRED"}
        )

        payment.refresh_from_db()
        self.assertEqual(payment.status, SbiEpayPayment.Status.EXPIRED)

    @patch("care_sbiepay.payments.rebalance_account_task.delay")
    @patch("care_sbiepay.payments.client.status_query")
    def test_poll_reconciles_paid_pending_payment(self, mock_status, mock_rebalance):
        mock_status.return_value = {
            "Response Status": "SUCCESS",
            "SBIePayRefID/ATRN": "ATRN777",
        }
        invoice = self.make_invoice("INV-POLL")
        payment = SbiEpayPayment.objects.create(
            order_number="pollorder123",
            invoice=invoice,
            amount=invoice.total_gross,
        )

        payments.poll_pending_payments()

        payment.refresh_from_db()
        self.assertEqual(payment.status, SbiEpayPayment.Status.PAID)
        self.assertEqual(payment.reference, "ATRN777")
        self.assertTrue(
            PaymentReconciliation.objects.filter(target_invoice=invoice).exists()
        )

    def past_deadline(self, extra_seconds=60):
        return care_now() - timedelta(
            seconds=settings.SBI_EPAY_PAYMENT_MAX_AGE
            + settings.SBI_EPAY_EXPIRY_GRACE_SECONDS
            + extra_seconds
        )

    @patch("care_sbiepay.payments.client.status_query")
    def test_poll_expires_stale_payment_after_final_check(self, mock_status):
        mock_status.return_value = {"Response Status": "PENDING"}
        payment = self.make_payment("staleorder123", "INV-EXPIRE")
        SbiEpayPayment.objects.filter(pk=payment.pk).update(
            created_date=self.past_deadline()
        )

        payments.poll_pending_payments()
        payments.poll_pending_payments()

        payment.refresh_from_db()
        self.assertEqual(payment.status, SbiEpayPayment.Status.EXPIRED)
        self.assertFalse(payment.needs_review)
        # one last status query, then never polled again
        mock_status.assert_called_once()
        self.assertEqual(
            mock_status.call_args.kwargs["merch_order_no"], "staleorder123"
        )

    @patch("care_sbiepay.payments.client.status_query")
    def test_poll_keeps_polling_through_grace_period(self, mock_status):
        mock_status.return_value = {"Response Status": "PENDING"}
        # link just expired at the gateway; SBI may still report a last-second payment
        payment = self.make_payment(
            "graceorder123", "INV-GRACE", expires_at=care_now() - timedelta(seconds=1)
        )

        payments.poll_pending_payments()

        payment.refresh_from_db()
        self.assertEqual(payment.status, SbiEpayPayment.Status.CREATED)

    @patch("care_sbiepay.payments.client.status_query")
    def test_poll_does_not_expire_while_gateway_is_unreachable(self, mock_status):
        mock_status.side_effect = client.SbiEpayError("gateway down")
        payment = self.make_payment("downorder123", "INV-DOWN")
        SbiEpayPayment.objects.filter(pk=payment.pk).update(
            created_date=self.past_deadline()
        )

        payments.poll_pending_payments()
        payments.poll_pending_payments()

        payment.refresh_from_db()
        self.assertEqual(payment.status, SbiEpayPayment.Status.CREATED)
        # an unanswered payment is left alone for the backoff period
        self.assertEqual(mock_status.call_count, 1)
        self.assertTrue(cache.get(payments.backoff_key(payment)))

        cache.delete(payments.backoff_key(payment))
        payments.poll_pending_payments()

        self.assertEqual(mock_status.call_count, 2)

    @patch("care_sbiepay.payments.client.status_query")
    def test_poll_backs_off_only_when_gateway_did_not_answer(self, mock_status):
        mock_status.return_value = {"Response Status": "NA"}
        payment = self.make_payment("answered12345", "INV-ANSWERED")

        payments.poll_pending_payments()
        payments.poll_pending_payments()

        self.assertEqual(mock_status.call_count, 2)
        self.assertIsNone(cache.get(payments.backoff_key(payment)))

    @patch("care_sbiepay.payments.client.status_query")
    def test_poll_gives_up_after_hard_cap_and_flags_for_review(self, mock_status):
        mock_status.side_effect = client.SbiEpayError("gateway down")
        payment = self.make_payment("capped1234567", "INV-CAP")
        SbiEpayPayment.objects.filter(pk=payment.pk).update(
            created_date=self.past_deadline(
                settings.SBI_EPAY_EXPIRY_HARD_CAP_SECONDS + 60
            )
        )

        payments.poll_pending_payments()

        payment.refresh_from_db()
        self.assertEqual(payment.status, SbiEpayPayment.Status.EXPIRED)
        self.assertTrue(payment.needs_review)
        self.assertIn("unreachable", payment.review_reason)

    @patch("care_sbiepay.payments.client.status_query")
    def test_poll_skips_payment_being_settled_elsewhere(self, mock_status):
        payment = self.make_payment("busyorder1234", "INV-BUSY")
        mock_status.return_value = {
            "Response Status": "SUCCESS",
            "SBIePayRefID/ATRN": "A",
        }

        with SbiEpayPaymentLock(payment):
            payments.poll_pending_payments()

        payment.refresh_from_db()
        self.assertEqual(payment.status, SbiEpayPayment.Status.CREATED)

    @patch("care_sbiepay.payments.client.status_query")
    def test_poll_logs_unreachable_gateway_without_a_traceback(self, mock_status):
        mock_status.side_effect = requests.Timeout("read timed out")
        payment = self.make_payment("slowgateway12", "INV-SLOW")

        with self.assertLogs("care_sbiepay.payments", level="WARNING") as logs:
            payments.poll_pending_payments()

        payment.refresh_from_db()
        self.assertEqual(payment.status, SbiEpayPayment.Status.CREATED)
        self.assertEqual([record.levelname for record in logs.records], ["WARNING"])
        self.assertIsNone(logs.records[0].exc_info)

    @patch("care_sbiepay.payments.client.status_query")
    def test_poll_task_skips_tick_while_previous_run_is_going(self, mock_status):
        self.make_payment("lockedorder12", "INV-LOCKED")

        with Lock(tasks.POLL_LOCK_KEY):
            tasks.poll_pending_payments()

        mock_status.assert_not_called()

    @patch("care_sbiepay.tasks.Lock")
    def test_poll_task_does_nothing_when_nothing_is_pending(self, mock_lock):
        self.make_payment(
            "settled123456", "INV-SETTLED", status=SbiEpayPayment.Status.PAID
        )

        tasks.poll_pending_payments()

        mock_lock.assert_not_called()

    @patch("care_sbiepay.payments.client.status_query")
    def test_poll_stops_at_order_validity(self, mock_status):
        mock_status.return_value = {"Response Status": "PENDING"}
        # fresh row whose order validity (plus grace) already passed at the gateway
        payment = self.make_payment(
            "shortorder123",
            "INV-SHORT",
            expires_at=care_now()
            - timedelta(seconds=settings.SBI_EPAY_EXPIRY_GRACE_SECONDS + 1),
        )

        payments.poll_pending_payments()
        payments.poll_pending_payments()

        payment.refresh_from_db()
        self.assertEqual(payment.status, SbiEpayPayment.Status.EXPIRED)
        mock_status.assert_called_once()

    @patch("care_sbiepay.payments.client.status_query")
    def test_poll_keeps_polling_until_order_validity(self, mock_status):
        mock_status.return_value = {"Response Status": "PENDING"}
        # old row whose order is still valid at the gateway
        payment = self.make_payment(
            "longorder1234", "INV-LONG", expires_at=care_now() + timedelta(hours=3)
        )
        SbiEpayPayment.objects.filter(pk=payment.pk).update(
            created_date=self.past_deadline()
        )

        payments.poll_pending_payments()

        payment.refresh_from_db()
        self.assertEqual(payment.status, SbiEpayPayment.Status.CREATED)
        mock_status.assert_called_once()

    @patch("care_sbiepay.payments.client.status_query")
    def test_poll_marks_gateway_expired_payment(self, mock_status):
        mock_status.return_value = {"Response Status": "EXPIRED"}
        payment = self.make_payment("gwexpired1234", "INV-GW-EXP")

        payments.poll_pending_payments()

        payment.refresh_from_db()
        self.assertEqual(payment.status, SbiEpayPayment.Status.EXPIRED)

    def test_pending_payment_and_supersede(self):
        invoice = self.make_invoice("INV-PENDING")
        SbiEpayPayment.objects.create(
            order_number="olddead123456",
            invoice=invoice,
            amount=Decimal(100),
            expires_at=care_now() - timedelta(hours=1),
        )
        live = SbiEpayPayment.objects.create(
            order_number="live123456789",
            invoice=invoice,
            amount=Decimal(100),
            expires_at=care_now() + timedelta(hours=1),
        )

        self.assertEqual(payments.pending_payment(invoice), live)
        self.assertTrue(payments.supersede(live))
        live.refresh_from_db()
        self.assertEqual(live.status, SbiEpayPayment.Status.SUPERSEDED)
        self.assertIsNone(payments.pending_payment(invoice))


def make_push(fields, key=MERCHANT_KEY) -> str:
    # SBI push: pipe-delimited body, trailing SHA-512 of everything up to the last pipe.
    body = "|".join(fields) + "|"
    return crypto.encrypt(key, body + crypto.checksum(body))


class TestSbiEpayDecodePush(SbiEpayTestBase):
    fields = ["order123", "ATRN1", "SUCCESS", "100.00", "INR", "UPI", "CARE"]

    def test_decodes_with_merchant_from_merch_id_val(self):
        push = payments.decode_push(
            {"pushRespData": make_push(self.fields), "merchIdVal": "1000755"}
        )

        self.assertEqual(push["merch_order_no"], "order123")
        self.assertEqual(push["status"], "SUCCESS")
        self.assertEqual(push["atrn"], "ATRN1")
        # the key that authenticated the push identifies the merchant
        self.assertEqual(push["merchant_id"], "1000755")

    def test_falls_back_to_trying_all_merchants(self):
        other_facility = self.create_facility(self.user)
        SbiEpayMerchant.objects.create(
            facility=other_facility,
            merchant_code="2000000",
            merchant_key="AAAAAAAAAAAAAAAAAAAAAA==",
        )

        push = payments.decode_push({"pushRespData": make_push(self.fields)})

        self.assertEqual(push["merch_order_no"], "order123")

    def test_returns_none_for_unknown_merchant(self):
        self.assertIsNone(
            payments.decode_push(
                {"pushRespData": make_push(self.fields), "merchIdVal": "unknown"}
            )
        )

    def test_returns_none_without_payload(self):
        self.assertIsNone(payments.decode_push({"merchIdVal": "1000755"}))


WEBHOOK_URL = "/api/care_sbiepay/webhook/"
PAYMENT_LINK_URL = "/api/care_sbiepay/payment_link/"


class TestSbiEpayWebhook(SbiEpayTestBase):
    def push_fields(self, order_number, reference="ATRN-W", status="SUCCESS"):
        return [order_number, reference, status, "100.00", "INR", "UPI", "CARE"]

    def post_push(self, fields, **extra):
        data = {"pushRespData": make_push(fields), "merchIdVal": "1000755", **extra}
        return self.client.post(
            WEBHOOK_URL,
            urlencode(data),
            content_type="application/x-www-form-urlencoded",
        )

    def test_settles_payment_and_stores_event(self):
        payment = self.make_payment("webhook123456", "INV-WH")

        response = self.post_push(self.push_fields("webhook123456"))

        self.assertEqual(response.status_code, 200)
        payment.refresh_from_db()
        self.assertEqual(payment.status, SbiEpayPayment.Status.PAID)
        event = SbiEpayPushEvent.objects.get()
        self.assertEqual(event.status, SbiEpayPushEvent.Status.PROCESSED)
        self.assertEqual(event.order_number, "webhook123456")
        self.assertEqual(event.reference, "ATRN-W")
        self.assertEqual(event.amount, Decimal("100.00"))
        self.assertEqual(event.merchant_code, "1000755")
        self.assertIn("pushRespData", event.raw_form)
        self.assertIsNotNone(event.processed_at)

    def test_redelivery_is_idempotent(self):
        payment = self.make_payment("redeliver1234", "INV-RD")
        fields = self.push_fields("redeliver1234")

        first = self.post_push(fields)
        second = self.post_push(fields)

        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(SbiEpayPushEvent.objects.count(), 1)
        self.assertEqual(
            PaymentReconciliation.objects.filter(
                target_invoice=payment.invoice
            ).count(),
            1,
        )

    def test_rejects_unauthenticated_push(self):
        fields = self.push_fields("forged1234567")

        response = self.post_push(fields, merchIdVal="unknown")
        garbage = self.client.post(
            WEBHOOK_URL,
            urlencode(
                {"pushRespData": "bm90IGVuY3J5cHRlZA==", "merchIdVal": "1000755"}
            ),
            content_type="application/x-www-form-urlencoded",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(garbage.status_code, 400)
        self.assertFalse(SbiEpayPushEvent.objects.exists())

    def test_rejects_missing_or_oversized_payload(self):
        missing = self.client.post(
            WEBHOOK_URL,
            urlencode({"merchIdVal": "1000755"}),
            content_type="application/x-www-form-urlencoded",
        )
        with patch.object(push_events.settings, "SBI_EPAY_PUSH_MAX_BYTES", 10):
            oversized = self.post_push(self.push_fields("big1234567890"))

        self.assertEqual(missing.status_code, 400)
        self.assertEqual(oversized.status_code, 400)
        self.assertFalse(SbiEpayPushEvent.objects.exists())

    def test_unknown_order_is_kept_as_ignored(self):
        response = self.post_push(self.push_fields("nobody1234567"))

        self.assertEqual(response.status_code, 200)
        event = SbiEpayPushEvent.objects.get()
        self.assertEqual(event.status, SbiEpayPushEvent.Status.IGNORED)

    @patch("care_sbiepay.push_events.payments.reconcile_push")
    def test_processing_failure_returns_500_and_keeps_event(self, mock_reconcile):
        mock_reconcile.side_effect = RuntimeError("db hiccup")
        payment = self.make_payment("failing123456", "INV-FAIL")

        response = self.post_push(self.push_fields("failing123456"))

        self.assertEqual(response.status_code, 500)
        event = SbiEpayPushEvent.objects.get()
        self.assertEqual(event.status, SbiEpayPushEvent.Status.FAILED)
        self.assertEqual(event.attempts, 1)
        self.assertIn("db hiccup", event.error_message)
        payment.refresh_from_db()
        self.assertEqual(payment.status, SbiEpayPayment.Status.CREATED)

    def test_redelivery_retries_failed_event(self):
        payment = self.make_payment("retry12345678", "INV-RETRY")
        fields = self.push_fields("retry12345678")
        with patch(
            "care_sbiepay.push_events.payments.reconcile_push",
            side_effect=RuntimeError("down"),
        ):
            self.assertEqual(self.post_push(fields).status_code, 500)

        response = self.post_push(fields)

        self.assertEqual(response.status_code, 200)
        payment.refresh_from_db()
        self.assertEqual(payment.status, SbiEpayPayment.Status.PAID)
        event = SbiEpayPushEvent.objects.get()
        self.assertEqual(event.status, SbiEpayPushEvent.Status.PROCESSED)
        self.assertEqual(event.error_message, "")

    def test_locked_payment_fails_delivery_then_replays(self):
        payment = self.make_payment("busywebhook12", "INV-BUSY-WH")

        with SbiEpayPaymentLock(payment):
            response = self.post_push(self.push_fields("busywebhook12"))
        self.assertEqual(response.status_code, 500)
        event = SbiEpayPushEvent.objects.get()
        self.assertEqual(event.status, SbiEpayPushEvent.Status.FAILED)
        self.assertIn("ObjectLocked", event.error_message)

        self.assertEqual(push_events.replay_failed(), 1)

        payment.refresh_from_db()
        self.assertEqual(payment.status, SbiEpayPayment.Status.PAID)
        event.refresh_from_db()
        self.assertEqual(event.status, SbiEpayPushEvent.Status.PROCESSED)

    def test_replay_gives_up_after_max_attempts(self):
        self.make_payment("giveup1234567", "INV-GIVEUP")
        with patch(
            "care_sbiepay.push_events.payments.reconcile_push",
            side_effect=RuntimeError("down"),
        ):
            self.post_push(self.push_fields("giveup1234567"))
        SbiEpayPushEvent.objects.update(attempts=settings.SBI_EPAY_PUSH_MAX_ATTEMPTS)

        self.assertEqual(push_events.replay_failed(), 0)
        self.assertEqual(
            SbiEpayPushEvent.objects.get().status, SbiEpayPushEvent.Status.FAILED
        )


class TestSbiEpayPaymentLinkAPI(SbiEpayTestBase):
    def setUp(self):
        super().setUp()
        self.staff = self.create_user()
        role = self.create_role_with_permissions(
            [
                PaymentReconciliationPermissions.can_write_payment_reconciliation.name,
                PaymentReconciliationPermissions.can_read_payment_reconciliation.name,
            ]
        )
        organization = self.create_facility_organization(self.facility)
        self.attach_role_facility_organization_user(organization, self.staff, role)
        self.client.force_authenticate(user=self.staff)

    def record_payment(self, invoice, amount, reference="cash"):
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
            reference_number=reference,
        )

    def post_link(self, invoice):
        return self.client.post(
            PAYMENT_LINK_URL, {"invoice_id": str(invoice.external_id)}, format="json"
        )

    @patch("care_sbiepay.payments.client.create_payment_link")
    def test_creates_link_for_outstanding_balance(self, mock_create):
        mock_create.return_value = {"paymentUrl": "https://pay/1"}
        invoice = self.make_invoice("INV-API")
        self.record_payment(invoice, 30)

        response = self.post_link(invoice)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["amount"], "70.00")
        self.assertEqual(response.data["status"], "created")
        self.assertEqual(response.data["payment_url"], "https://pay/1")
        self.assertIsNotNone(response.data["expires_at"])
        self.assertEqual(mock_create.call_args.kwargs["amount"], Decimal("70.00"))

    @patch("care_sbiepay.payments.client.create_payment_link")
    def test_requires_facility_permission(self, mock_create):
        outsider = self.create_user()
        self.client.force_authenticate(user=outsider)

        response = self.post_link(self.make_invoice("INV-403"))

        self.assertEqual(response.status_code, 403)
        mock_create.assert_not_called()
        self.assertFalse(SbiEpayPayment.objects.exists())

    def test_requires_authentication(self):
        self.client.force_authenticate(user=None)

        response = self.post_link(self.make_invoice("INV-401"))

        self.assertIn(response.status_code, (401, 403))
        self.assertFalse(SbiEpayPayment.objects.exists())

    def test_unknown_invoice_is_404(self):
        response = self.client.post(
            PAYMENT_LINK_URL, {"invoice_id": str(uuid())}, format="json"
        )

        self.assertEqual(response.status_code, 404)

    @patch("care_sbiepay.payments.client.create_payment_link")
    def test_rejects_invoices_that_are_not_issued(self, mock_create):
        for invoice_status in (
            InvoiceStatusOptions.draft,
            InvoiceStatusOptions.balanced,
            InvoiceStatusOptions.cancelled,
            InvoiceStatusOptions.entered_in_error,
        ):
            invoice = self.make_invoice(f"INV-{invoice_status.value}")
            Invoice.objects.filter(pk=invoice.pk).update(status=invoice_status.value)

            response = self.post_link(invoice)

            self.assertEqual(response.status_code, 400, invoice_status)
        mock_create.assert_not_called()

    @patch("care_sbiepay.payments.client.create_payment_link")
    def test_rejects_invoice_without_outstanding_balance(self, mock_create):
        invoice = self.make_invoice("INV-SETTLED")
        self.record_payment(invoice, 100)

        response = self.post_link(invoice)

        self.assertEqual(response.status_code, 400)
        self.assertIn("outstanding", str(response.data))
        mock_create.assert_not_called()

    @patch("care_sbiepay.payments.client.create_payment_link")
    def test_reuses_live_link(self, mock_create):
        mock_create.return_value = {"paymentUrl": "https://pay/1"}
        invoice = self.make_invoice("INV-REUSE")

        first = self.post_link(invoice)
        second = self.post_link(invoice)

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.data["order_number"], first.data["order_number"])
        mock_create.assert_called_once()
        self.assertEqual(SbiEpayPayment.objects.filter(invoice=invoice).count(), 1)

    @patch("care_sbiepay.payments.client.create_payment_link")
    def test_supersedes_link_when_balance_changes(self, mock_create):
        mock_create.side_effect = [
            {"paymentUrl": "https://pay/1"},
            {"paymentUrl": "https://pay/2"},
        ]
        invoice = self.make_invoice("INV-SUPERSEDE")
        first = self.post_link(invoice)
        self.record_payment(invoice, 30)

        second = self.post_link(invoice)

        self.assertEqual(second.status_code, 201)
        self.assertNotEqual(second.data["order_number"], first.data["order_number"])
        self.assertEqual(second.data["amount"], "70.00")
        old = SbiEpayPayment.objects.get(order_number=first.data["order_number"])
        self.assertEqual(old.status, SbiEpayPayment.Status.SUPERSEDED)

    @patch("care_sbiepay.payments.client.create_payment_link")
    def test_gateway_failure_keeps_existing_link(self, mock_create):
        mock_create.side_effect = [
            {"paymentUrl": "https://pay/1"},
            client.SbiEpayError("SBI down"),
        ]
        invoice = self.make_invoice("INV-GW-FAIL")
        first = self.post_link(invoice)
        self.record_payment(invoice, 30)

        second = self.post_link(invoice)

        self.assertEqual(second.status_code, 400)
        old = SbiEpayPayment.objects.get(order_number=first.data["order_number"])
        self.assertEqual(old.status, SbiEpayPayment.Status.CREATED)

    @patch("care_sbiepay.payments.client.status_query")
    def test_refresh_settles_paid_link(self, mock_status):
        mock_status.return_value = {
            "Response Status": "SUCCESS",
            "SBIePayRefID/ATRN": "ATRN-REFRESH",
        }
        payment = self.make_payment(
            "refresh123456", "INV-REFRESH", status=SbiEpayPayment.Status.EXPIRED
        )

        response = self.client.post(f"{PAYMENT_LINK_URL}refresh123456/refresh/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "paid")
        payment.refresh_from_db()
        self.assertEqual(payment.reference, "ATRN-REFRESH")

    @patch("care_sbiepay.payments.client.status_query")
    def test_refresh_requires_facility_permission(self, mock_status):
        self.make_payment("refresh403123", "INV-REFRESH-403")
        self.client.force_authenticate(user=self.create_user())

        response = self.client.post(f"{PAYMENT_LINK_URL}refresh403123/refresh/")

        self.assertEqual(response.status_code, 403)
        mock_status.assert_not_called()

    def test_refresh_unknown_payment_is_404(self):
        response = self.client.post(f"{PAYMENT_LINK_URL}nosuchorder123/refresh/")

        self.assertEqual(response.status_code, 404)

    @patch("care_sbiepay.payments.client.status_query")
    def test_refresh_reports_whether_gateway_answered(self, mock_status):
        self.make_payment("refreshgw1234", "INV-REFRESH-GW")
        mock_status.side_effect = requests.Timeout("read timed out")

        response = self.client.post(f"{PAYMENT_LINK_URL}refreshgw1234/refresh/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["gateway_checked"])
        self.assertEqual(response.data["status"], "created")

        mock_status.side_effect = None
        mock_status.return_value = {"Response Status": "PENDING"}

        response = self.client.post(f"{PAYMENT_LINK_URL}refreshgw1234/refresh/")

        self.assertTrue(response.data["gateway_checked"])
        self.assertEqual(response.data["status"], "created")

    def test_lists_links_for_invoice_newest_first(self):
        invoice = self.make_invoice("INV-LIST")
        SbiEpayPayment.objects.create(
            order_number="list000000001",
            invoice=invoice,
            amount=Decimal(100),
            status=SbiEpayPayment.Status.SUPERSEDED,
        )
        SbiEpayPayment.objects.create(
            order_number="list000000002", invoice=invoice, amount=Decimal(70)
        )
        self.make_payment("otherinvoice1", "INV-OTHER")

        response = self.client.get(
            PAYMENT_LINK_URL, {"invoice": str(invoice.external_id)}
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(
            [row["order_number"] for row in response.data["results"]],
            ["list000000002", "list000000001"],
        )
        row = response.data["results"][0]
        self.assertEqual(row["invoice_id"], str(invoice.external_id))
        self.assertEqual(row["amount"], "70.00")
        self.assertFalse(row["needs_review"])

        response = self.client.get(
            PAYMENT_LINK_URL,
            {"invoice": str(invoice.external_id), "status": "created"},
        )

        self.assertEqual(
            [row["order_number"] for row in response.data["results"]],
            ["list000000002"],
        )

    def test_list_requires_invoice(self):
        response = self.client.get(PAYMENT_LINK_URL)

        self.assertEqual(response.status_code, 400)

    def test_list_unknown_invoice_is_404(self):
        response = self.client.get(PAYMENT_LINK_URL, {"invoice": str(uuid())})

        self.assertEqual(response.status_code, 404)

    def test_list_requires_facility_permission(self):
        invoice = self.make_invoice("INV-LIST-403")
        self.client.force_authenticate(user=self.create_user())

        response = self.client.get(
            PAYMENT_LINK_URL, {"invoice": str(invoice.external_id)}
        )

        self.assertEqual(response.status_code, 403)


class TestSbiEpayMerchantAPI(CareAPITestBase):
    def setUp(self):
        self.superuser = self.create_super_user()
        self.facility = self.create_facility(self.superuser)
        self.url = "/api/care_sbiepay/merchant/"

    def detail_url(self, facility):
        return f"{self.url}{facility.external_id}/"

    def test_superuser_can_create_merchant(self):
        self.client.force_authenticate(user=self.superuser)

        response = self.client.post(
            self.url,
            {
                "facility_id": str(self.facility.external_id),
                "merchant_code": "1000755",
                "merchant_key": MERCHANT_KEY,
                "is_enabled": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["merchant_code"], "1000755")
        self.assertEqual(response.data["facility_id"], self.facility.external_id)
        self.assertNotIn("merchant_key", response.data)
        self.assertTrue(response.data["merchant_key_masked"].endswith("/Q=="))
        self.assertTrue(response.data["merchant_key_masked"].startswith("****"))
        merchant = SbiEpayMerchant.objects.get(facility=self.facility)
        self.assertEqual(merchant.merchant_key, MERCHANT_KEY)

    def test_non_superuser_cannot_write_but_can_read(self):
        user = self.create_user()
        org = self.create_facility_organization(self.facility)
        baker.make("emr.FacilityOrganizationUser", organization=org, user=user)
        SbiEpayMerchant.objects.create(
            facility=self.facility, merchant_code="1000755", merchant_key=MERCHANT_KEY
        )
        self.client.force_authenticate(user=user)

        response = self.client.post(
            self.url,
            {
                "facility_id": str(self.facility.external_id),
                "merchant_code": "x",
                "merchant_key": "y",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 403)

        response = self.client.get(self.detail_url(self.facility))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["merchant_code"], "1000755")

    def test_user_without_facility_access_gets_404(self):
        user = self.create_user()
        SbiEpayMerchant.objects.create(
            facility=self.facility, merchant_code="1000755", merchant_key=MERCHANT_KEY
        )
        self.client.force_authenticate(user=user)

        response = self.client.get(self.detail_url(self.facility))

        self.assertEqual(response.status_code, 404)

    def test_patch_updates_key_and_toggle(self):
        merchant = SbiEpayMerchant.objects.create(
            facility=self.facility, merchant_code="1000755", merchant_key=MERCHANT_KEY
        )
        self.client.force_authenticate(user=self.superuser)

        response = self.client.patch(
            self.detail_url(self.facility),
            {"merchant_key": "newkey1234567890", "is_enabled": False},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        merchant.refresh_from_db()
        self.assertEqual(merchant.merchant_key, "newkey1234567890")
        self.assertFalse(merchant.is_enabled)
        self.assertEqual(response.data["merchant_key_masked"], "************7890")

    def test_patch_cannot_move_merchant_to_another_facility(self):
        SbiEpayMerchant.objects.create(
            facility=self.facility, merchant_code="1000755", merchant_key=MERCHANT_KEY
        )
        other = self.create_facility(self.superuser)
        self.client.force_authenticate(user=self.superuser)

        response = self.client.patch(
            self.detail_url(self.facility),
            {"facility_id": str(other.external_id)},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
