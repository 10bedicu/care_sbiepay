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
| `SBI_EPAY_CURRENCY` | Currency a confirmation must report; anything else is flagged for review | `INR` |
| `SBI_EPAY_EXPIRY_GRACE_SECONDS` | Keep polling this long after the order validity for a last-second payment | `900` |
| `SBI_EPAY_EXPIRY_HARD_CAP_SECONDS` | If the gateway stays unreachable, expire (flagged for review) this long after the deadline | `86400` |
| `SBI_EPAY_UNREACHABLE_BACKOFF` | Leave a payment alone this long after the gateway failed to answer about it | `120` |
| `SBI_EPAY_PUSH_REPLAY_INTERVAL` | Seconds between retries of pushes that failed to process | `60` |
| `SBI_EPAY_PUSH_MAX_ATTEMPTS` | Give up replaying a push after this many failures | `5` |
| `SBI_EPAY_PUSH_MAX_BYTES` | Reject `pushRespData` larger than this | `16384` |

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

Generate a payment link for an invoice. The caller needs the
`can_write_payment_reconciliation` permission in the invoice's facility (the
same privilege as recording a payment).

```
POST /api/care_sbiepay/payment_link/
{ "invoice_id": "<invoice external_id>" }
```

- The invoice must be `issued` (not draft, balanced, cancelled or a refund).
- The link is for the invoice's **outstanding balance** (total less active
  payments, plus credit notes), never the full total again. `400` when nothing
  is outstanding.
- If a live link for that balance already exists it is returned with `200`
  instead of creating another. If the balance changed, the old link is marked
  `superseded` and a new one is created (`201`).

Returns `order_number`, `payment_url`, `status`, `amount` and `expires_at`.

```
POST /api/care_sbiepay/payment_link/<order_number>/refresh/
```

Asks the gateway for the order's current status and settles it if paid — also
for a link that already expired locally. Use it when a patient reports having
paid but the payment has not shown up.

## Reconciliation

A standalone payment moves through these states:

`created → paid | failed | cancelled | expired | superseded`

- **Webhook** — SBI ePay push notifications are posted to
  `POST /api/care_sbiepay/webhook/`. The push is decrypted with the key of the
  merchant identified by `merchIdVal` and its checksum verified; anything that
  does not authenticate gets `400` and is not stored. Authentic pushes are
  stored (`SbiEpayPushEvent`) **before** they are applied: a re-delivered push
  is a no-op, a push that fails to process returns `500` and is retried by the
  `replay_failed_push_events` task, and a push for an order CARE does not know
  is kept as `ignored`. Standalone orders settle their invoice; unknown orders
  fall through to ABDM scan-and-pay (when installed).
- **Polling** — when `SBI_EPAY_POLLING_ENABLED` is on, a Celery task queries the
  gateway every `SBI_EPAY_POLLING_INTERVAL` seconds for each pending payment and
  settles paid ones. Each payment stores the order validity sent to SBI as
  `expires_at`. Polling continues for `SBI_EPAY_EXPIRY_GRACE_SECONDS` past that,
  then the payment is marked `expired` — but only after the gateway actually
  answered; an unreachable gateway never expires a payment until
  `SBI_EPAY_EXPIRY_HARD_CAP_SECONDS`, and then it is flagged for review.
- **Exactly once** — settlement runs under a per-payment lock with a fresh copy
  of the row, so concurrent webhook and polling workers cannot credit an
  invoice twice; a paid ATRN is also unique at the database level. The account
  rebalance is queued only after the transaction commits.
- **Amounts** — the reconciliation records the amount the gateway confirmed
  (falling back to the amount the link was created for), never the invoice's
  current total. A confirmation whose amount, currency or merchant differs
  from what was expected is still recorded (the money moved) but sets
  `needs_review` with the reason on the payment and in the reconciliation note.
- **Late payments** — a verified success for a payment already marked
  `expired`, `failed`, `cancelled` or `superseded` still settles it; such
  settlements are flagged for review because a replacement link may exist.

### Known gaps

- **Refunds** are not handled. A refund made in the SBI merchant portal is not
  reflected here; record it in CARE as a credit note against the invoice.
- A superseded link stays payable at the gateway (SBI has no cancel-order API);
  if the patient pays it anyway the payment settles and is flagged for review.
