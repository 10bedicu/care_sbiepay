from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from abdm.models import AbhaNumber, HealthFacility, PaymentOrder
from abdm.models.payment_order import PaymentOrderStatus
from abdm.service.helper import uuid
from abdm.utils import user as abdm_user
from care_sbiepay.utils import client, crypto
from django.test import SimpleTestCase
from model_bakery import baker

from care.emr.models.invoice import Invoice
from care.emr.models.payment_reconciliation import PaymentReconciliation
from care.emr.resources.invoice.spec import InvoiceStatusOptions
from care.utils.tests.base import CareAPITestBase
from care.utils.time_util import care_now
from care_sbiepay import payments
from care_sbiepay import provider as sbiepay_provider
from care_sbiepay.models import SbiEpayMerchant, SbiEpayPayment
from care_sbiepay.settings import plugin_settings as settings

MERCHANT_KEY = "/IIvvWMcy5ls/V2hCNZ5/Q=="


class TestSbiEpayCrypto(SimpleTestCase):
    def test_encrypt_decrypt_roundtrip(self):
        enc = crypto.encrypt(MERCHANT_KEY, "hello world")
        self.assertEqual(crypto.decrypt(MERCHANT_KEY, enc), "hello world")


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
        self.assertEqual((validity.hour, validity.minute, validity.second), (23, 59, 59))

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
        return PaymentOrder.objects.create(
            open_order_request_id=uuid(),
            abha_number=self.abha_number,
            health_facility=self.health_facility,
            invoice=self.make_invoice("INV-SBI-REC"),
            order_number="sbiorder123",
            status=PaymentOrderStatus.PAYMENT_INITIATED,
        )

    def make_payment(self, order_number, number="INV-PAY", **fields):
        invoice = self.make_invoice(number)
        return SbiEpayPayment.objects.create(
            order_number=order_number,
            invoice=invoice,
            amount=invoice.total_gross,
            **fields,
        )


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
        # order ref is generated by us, not passed straight through
        self.assertEqual(
            mock_create.call_args.kwargs["merch_order_no"], result["order_number"]
        )
        self.assertEqual(mock_create.call_args.args[0], self.merchant)

    def test_create_payment_link_requires_merchant(self):
        self.merchant.delete()

        with self.assertRaises(sbiepay_provider.client.SbiEpayError):
            sbiepay_provider.SbiEpayProvider().create_payment_link(self.make_invoice())


class TestSbiEpayReconcile(SbiEpayTestBase):
    @patch("abdm.signals.scan_pay.scan_pay_notify.delay")
    @patch("care_sbiepay.provider.client.status_query")
    def test_reconcile_marks_paid_and_notifies(self, mock_status, mock_task):
        mock_status.return_value = {
            "Response Status": "SUCCESS",
            "SBIePayRefID/ATRN": "ATRN123",
        }
        order = self.make_order()

        sbiepay_provider.SbiEpayProvider().reconcile_order(order)

        order.refresh_from_db()
        self.assertEqual(order.status, PaymentOrderStatus.SUCCESS)
        self.assertEqual(order.transaction_id, "ATRN123")
        self.assertTrue(
            PaymentReconciliation.objects.filter(
                target_invoice=order.invoice
            ).exists()
        )
        mock_task.assert_called_once()
        self.assertEqual(mock_status.call_args.args[0], self.merchant)

    @patch("abdm.signals.scan_pay.scan_pay_notify.delay")
    @patch("care_sbiepay.provider.client.status_query")
    def test_reconcile_ignores_unpaid(self, mock_status, mock_task):
        mock_status.return_value = {"Response Status": "NA"}
        order = self.make_order()

        sbiepay_provider.SbiEpayProvider().reconcile_order(order)

        order.refresh_from_db()
        self.assertEqual(order.status, PaymentOrderStatus.PAYMENT_INITIATED)
        self.assertFalse(
            PaymentReconciliation.objects.filter(
                target_invoice=order.invoice
            ).exists()
        )
        mock_task.assert_not_called()

    @patch("abdm.signals.scan_pay.scan_pay_notify.delay")
    @patch("care_sbiepay.provider.client.status_query")
    def test_reconcile_fails_failed_order(self, mock_status, mock_task):
        mock_status.return_value = {"Response Status": "FAILURE"}
        order = self.make_order()

        sbiepay_provider.SbiEpayProvider().reconcile_order(order)

        order.refresh_from_db()
        self.assertEqual(order.status, PaymentOrderStatus.FAIL)
        self.assertFalse(
            PaymentReconciliation.objects.filter(
                target_invoice=order.invoice
            ).exists()
        )
        mock_task.assert_not_called()

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

    @patch("abdm.signals.scan_pay.scan_pay_notify.delay")
    def test_push_marks_paid_and_notifies(self, mock_task):
        order = self.make_order()

        sbiepay_provider.reconcile_abdm_push(
            {
                "merch_order_no": "sbiorder123",
                "status": "SUCCESS",
                "atrn": "ATRN999",
                "bank_ref_number": "BR123",
            }
        )

        order.refresh_from_db()
        self.assertEqual(order.status, PaymentOrderStatus.SUCCESS)
        self.assertEqual(order.transaction_id, "ATRN999")
        self.assertTrue(
            PaymentReconciliation.objects.filter(
                target_invoice=order.invoice
            ).exists()
        )
        mock_task.assert_called_once()

    @patch("abdm.signals.scan_pay.scan_pay_notify.delay")
    def test_push_ignores_unpaid(self, mock_task):
        order = self.make_order()

        sbiepay_provider.reconcile_abdm_push(
            {"merch_order_no": "sbiorder123", "status": "FAILED"}
        )

        order.refresh_from_db()
        self.assertEqual(order.status, PaymentOrderStatus.PAYMENT_INITIATED)
        mock_task.assert_not_called()


class TestSbiEpayStandalonePayment(SbiEpayTestBase):
    @patch("care_sbiepay.payments.client.create_payment_link")
    def test_create_payment_persists_record(self, mock_create):
        mock_create.return_value = {
            "paymentUrl": "https://epay.sbiuat.bank.in/secure/epayPayment.jsp?enctoken=xyz",
        }

        payment = payments.create_payment(self.make_invoice())

        self.assertEqual(payment.status, SbiEpayPayment.Status.CREATED)
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

        payments.reconcile_push(
            {
                "merch_order_no": "standalone123",
                "status": "SUCCESS",
                "atrn": "ATRN555",
            }
        )

        payment.refresh_from_db()
        self.assertEqual(payment.status, SbiEpayPayment.Status.PAID)
        self.assertEqual(payment.reference, "ATRN555")
        self.assertTrue(
            PaymentReconciliation.objects.filter(target_invoice=invoice).exists()
        )
        mock_rebalance.assert_called_once()

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

        payments.reconcile_push(
            {"merch_order_no": "lateorder123", "status": "SUCCESS", "atrn": "ATRN9"}
        )

        payment.refresh_from_db()
        self.assertEqual(payment.status, SbiEpayPayment.Status.PAID)
        self.assertEqual(payment.reference, "ATRN9")
        self.assertTrue(
            PaymentReconciliation.objects.filter(target_invoice=payment.invoice).exists()
        )
        mock_rebalance.assert_called_once()

    def test_push_marks_expired_standalone_payment(self):
        payment = self.make_payment("expiredorder1", "INV-GW-EXPIRED")

        payments.reconcile_push({"merch_order_no": "expiredorder1", "status": "EXPIRED"})

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

    @patch("care_sbiepay.payments.client.status_query")
    def test_poll_expires_stale_payment_after_final_check(self, mock_status):
        mock_status.return_value = {"Response Status": "PENDING"}
        payment = self.make_payment("staleorder123", "INV-EXPIRE")
        stale = care_now() - timedelta(seconds=settings.SBI_EPAY_PAYMENT_MAX_AGE + 60)
        SbiEpayPayment.objects.filter(pk=payment.pk).update(created_date=stale)

        payments.poll_pending_payments()
        payments.poll_pending_payments()

        payment.refresh_from_db()
        self.assertEqual(payment.status, SbiEpayPayment.Status.EXPIRED)
        # one last status query, then never polled again
        mock_status.assert_called_once()
        self.assertEqual(mock_status.call_args.kwargs["merch_order_no"], "staleorder123")

    @patch("care_sbiepay.payments.client.status_query")
    def test_poll_expires_stale_payment_even_if_gateway_fails(self, mock_status):
        mock_status.side_effect = client.SbiEpayError("gateway down")
        payment = self.make_payment("downorder123", "INV-DOWN")
        stale = care_now() - timedelta(seconds=settings.SBI_EPAY_PAYMENT_MAX_AGE + 60)
        SbiEpayPayment.objects.filter(pk=payment.pk).update(created_date=stale)

        payments.poll_pending_payments()

        payment.refresh_from_db()
        self.assertEqual(payment.status, SbiEpayPayment.Status.EXPIRED)

    @patch("care_sbiepay.payments.client.status_query")
    def test_poll_stops_at_order_validity(self, mock_status):
        mock_status.return_value = {"Response Status": "PENDING"}
        # fresh row whose order validity already passed at the gateway
        payment = self.make_payment(
            "shortorder123", "INV-SHORT", expires_at=care_now() - timedelta(seconds=1)
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
        stale = care_now() - timedelta(seconds=settings.SBI_EPAY_PAYMENT_MAX_AGE + 60)
        SbiEpayPayment.objects.filter(pk=payment.pk).update(created_date=stale)

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
