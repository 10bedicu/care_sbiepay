import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import requests
from django.core.cache import cache

from care_sbiepay.settings import plugin_settings as settings
from care_sbiepay.utils import crypto

logger = logging.getLogger(__name__)

AUTH_PATH = "/authenticationAPI/authentication/v1/getmerchantapiauthentication"
GENERATE_PAYMENT_URL_PATH = "/SBIePayPayment/payagg/generatePaymentURL/payURL"
STATUS_QUERY_PATH = "/MerchantDVAPI/getStatusQueryAPI"

TOKEN_CACHE_KEY = "care_sbiepay_token"
TOKEN_CACHE_TTL = 23 * 60 * 60  # SBI token valid ~24h; refresh a little early.

IST = ZoneInfo("Asia/Kolkata")


class SbiEpayError(Exception):
    pass


@dataclass(frozen=True)
class Confirmation:
    """What the gateway asserts about an order, from a push or a status query."""

    order_number: str
    status: str
    reference: str = ""
    amount: Decimal | None = None
    currency: str = ""
    merchant_code: str = ""
    raw: dict = field(default_factory=dict)


def _decimal_or_none(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


def _first(data: dict, *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def confirmation_from_push(push: dict) -> Confirmation:
    return Confirmation(
        order_number=_first(push, "merch_order_no"),
        status=_first(push, "status").upper(),
        reference=_first(push, "atrn", "bank_ref_number"),
        amount=_decimal_or_none(push.get("amount")),
        currency=_first(push, "currency").upper(),
        merchant_code=_first(push, "merchant_id"),
        raw=push,
    )


def confirmation_from_status(result: dict, order_number: str) -> Confirmation:
    return Confirmation(
        order_number=_first(result, "merchOrderNo", "Merchant Order No")
        or order_number,
        status=_first(result, "Response Status").upper(),
        reference=_first(result, "SBIePayRefID/ATRN", "Bank Reference Number"),
        amount=_decimal_or_none(_first(result, "Amount", "amount") or None),
        currency=_first(result, "Currency", "currency").upper(),
        merchant_code=_first(result, "Merchant ID", "merchantId"),
        raw=result,
    )


def checksum(plain: str) -> str:
    return crypto.checksum(plain)


def format_amount(value) -> str:
    return f"{Decimal(str(value)):.2f}"


def merch_order_number() -> str:
    # SBI merchOrderNo is VARCHAR(15); use a short alphanumeric unique reference.
    return uuid.uuid4().hex[:15]


def _source_url() -> str:
    return settings.SBI_EPAY_SOURCE_URL


def order_validity() -> datetime:
    now = datetime.now(IST)
    end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0)
    return min(now + timedelta(seconds=settings.SBI_EPAY_PAYMENT_MAX_AGE), end_of_day)


def get_token(force_refresh: bool = False) -> str:
    if not force_refresh:
        token = cache.get(TOKEN_CACHE_KEY)
        if token:
            return token

    response = requests.post(
        settings.SBI_EPAY_BASE_URL + AUTH_PATH,
        headers={
            "api_key_id": settings.SBI_EPAY_API_KEY_ID,
            "api_secret_key": settings.SBI_EPAY_API_SECRET_KEY,
        },
        timeout=settings.SBI_EPAY_REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    token = (response.json().get("data") or {}).get("token")
    if not token:
        raise SbiEpayError("Failed to obtain SBI ePay token")

    cache.set(TOKEN_CACHE_KEY, token, timeout=TOKEN_CACHE_TTL)
    return token


def _post_encrypted(merchant, path: str, req: dict) -> dict:
    plain = json.dumps({"req": req}, separators=(",", ":"))
    body = {
        "encData": crypto.encrypt(merchant.merchant_key, plain),
        "cs": checksum(plain),
        "merchantCode": merchant.merchant_code,
    }

    def _call(token):
        return requests.post(
            settings.SBI_EPAY_BASE_URL + path,
            headers={
                "Authorization": token,  # token already carries the "Bearer " prefix
                "api_name": path,
                "Content-Type": "application/json",
            },
            json=body,
            timeout=settings.SBI_EPAY_REQUEST_TIMEOUT,
        )

    response = _call(get_token())
    if response.status_code in (401, 403):
        response = _call(get_token(force_refresh=True))
    response.raise_for_status()

    payload = response.json()
    enc = payload.get("encData")
    if not enc or enc == "ERROR":
        raise SbiEpayError(payload.get("cs") or "SBI ePay request failed")

    plain = crypto.decrypt(merchant.merchant_key, enc)
    cs = payload.get("cs")
    if cs and cs.strip().lower() != checksum(plain).lower():
        raise SbiEpayError("SBI ePay response checksum mismatch")
    decrypted = json.loads(plain)
    return decrypted.get("res") or decrypted


def create_payment_link(
    merchant,
    *,
    merch_order_no: str,
    amount,
    other_details: str,
    validity: datetime | None = None,
) -> dict:
    now = datetime.now(IST)
    validity = validity or order_validity()
    return _post_encrypted(
        merchant,
        GENERATE_PAYMENT_URL_PATH,
        {
            "merchOrderNo": merch_order_no,
            "amount": format_amount(amount),
            "transactionDate": now.strftime("%d/%m/%Y %H:%M:%S"),
            "merchOrderNoValidity": validity.astimezone(IST).strftime(
                "%d/%m/%Y %H:%M:%S"
            ),
            "sourceUrl": _source_url(),
            "otherDetails": other_details,
        },
    )


def status_query(merchant, *, merch_order_no: str, amount, atrn: str = "") -> dict:
    return _post_encrypted(
        merchant,
        STATUS_QUERY_PATH,
        {
            "atrn": atrn,
            "merchantId": merchant.merchant_code,
            "merchOrderNo": merch_order_no,
            "amount": format_amount(amount),
            "sourceUrl": _source_url(),
        },
    )


# Push response is a pipe-delimited string with a trailing SHA-512 checksum.
PUSH_FIELDS = (
    "merch_order_no",
    "atrn",
    "status",
    "amount",
    "currency",
    "pay_mode",
    "other_details",
    "response_status",
    "bank_code",
    "bank_ref_number",
    "transaction_date",
    "country",
    "cin",
    "merchant_id",
)


def parse_push_response(merchant_key: str, push_resp_data: str) -> dict:
    plain = crypto.decrypt(merchant_key, push_resp_data)
    idx = plain.rfind("|")
    if idx == -1:
        raise SbiEpayError("Malformed SBI ePay push payload")
    # checksum covers everything up to and including the pipe before it.
    if checksum(plain[: idx + 1]) != plain[idx + 1 :]:
        raise SbiEpayError("SBI ePay push checksum mismatch")

    fields = plain.split("|")
    return {name: fields[i] for i, name in enumerate(PUSH_FIELDS) if i < len(fields)}
