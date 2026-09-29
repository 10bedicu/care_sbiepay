# care_sbiepay

SBI ePay payment plug for CARE. It works standalone (generate a payment link for
any invoice) and, when `care_abdm` is installed, also registers itself as an
ABDM scan-and-pay provider.

## Overview

The plug has two independent roles:

1. **Standalone payments** — expose an API to generate an SBI ePay payment link
   for an invoice. Payments are reconciled against the invoice via the webhook
   or the Celery polling task. This works with or without ABDM.
2. **ABDM scan-and-pay** — registers an `sbi_epay` provider with `care_abdm`.
   When ABDM is configured with `ABDM_SCAN_AND_PAY_PROVIDER=sbi_epay`,
   scan-and-pay payment links are generated through SBI ePay.

If `care_abdm` is not installed, role (1) keeps working and role (2) is simply
skipped — the plug never brings down the deployment.

## Installation

Add the plug to `plug_config.py`:

```python
care_sbiepay_plug = Plug(
    name="care_sbiepay",
    package_name="/app/care_sbiepay",
    version="",
    configs={},
)
```

## Configuration

Settings are read from `PLUGIN_CONFIGS["care_sbiepay"]` or the environment:

| Setting | Description | Default |
| --- | --- | --- |
| `SBI_EPAY_BASE_URL` | SBI ePay API base URL | `https://epay.sbiuat.bank.in` |
| `SBI_EPAY_API_KEY_ID` | Merchant API key id | |
| `SBI_EPAY_API_SECRET_KEY` | Merchant API secret key | |
| `SBI_EPAY_SOURCE_URL` | Merchant's SBI-registered source URL | |
| `SBI_EPAY_REQUEST_TIMEOUT` | HTTP request timeout in seconds | `30` |
| `SBI_EPAY_PAID_RESPONSE_STATUSES` | Gateway statuses treated as paid | `SUCCESS` |
| `SBI_EPAY_FAILED_RESPONSE_STATUSES` | Gateway statuses treated as failed | `FAILURE,FAILED,ABORTED,INVALID` |
| `SBI_EPAY_CANCELLED_RESPONSE_STATUSES` | Gateway statuses treated as cancelled | `CANCELLED,CANCELED` |
| `SBI_EPAY_EXPIRED_RESPONSE_STATUSES` | Gateway statuses treated as expired | `EXPIRED` |
| `SBI_EPAY_POLLING_ENABLED` | Enable the Celery status-polling task | `True` |
| `SBI_EPAY_POLLING_INTERVAL` | Polling interval in seconds | `10` |
| `SBI_EPAY_PAYMENT_MAX_AGE` | Seconds a new order stays payable (`merchOrderNoValidity`), capped at end of the IST day | `3600` |

For ABDM scan-and-pay, also set `ABDM_SCAN_AND_PAY_PROVIDER=sbi_epay` in the ABDM
plug config.

### Per-facility merchant

The merchant code and merchant (AES) key are configured per facility, not via
environment. A facility without an enabled merchant cannot generate payment
links. Reads are open to authenticated users; writes require a superuser. The
key is write-only and returned masked as `merchant_key_masked`.

```
POST  /api/care_sbiepay/merchant/
{ "facility_id": "<facility external_id>", "merchant_code": "...", "merchant_key": "...", "is_enabled": true }

GET   /api/care_sbiepay/merchant/<facility external_id>/
PATCH /api/care_sbiepay/merchant/<facility external_id>/
```

The [`care_sbiepay_fe`](../../care_sbiepay_fe) frontend plug adds a
"Configure SBI ePay merchant" action to the facility home page for this.

## Payment link API

Generate a payment link for an existing invoice (authenticated):

```
POST /api/care_sbiepay/payment_link/
{ "invoice_id": "<invoice external_id>" }
```

Returns the `order_number`, `payment_url`, and `status`.

## Reconciliation

A standalone payment moves through these states:

`created → paid | failed | cancelled | expired`

- **Webhook** — SBI ePay push notifications are posted to
  `POST /api/care_sbiepay/webhook/`. The push is decrypted with the key of the
  merchant identified by `merchIdVal`. Standalone orders settle their invoice;
  unknown orders fall through to ABDM scan-and-pay (when installed).
- **Polling** — when `SBI_EPAY_POLLING_ENABLED` is on, a Celery task queries the
  gateway every `SBI_EPAY_POLLING_INTERVAL` seconds for each pending payment and
  settles paid ones. Each payment stores the order validity sent to SBI as
  `expires_at`; once that passes, the payment gets one final status query and
  is then marked `expired`. Terminal payments are never polled again.
- **Late payments** — a push reporting a paid order still settles a payment
  that was marked `expired` locally, so no captured money is dropped.
