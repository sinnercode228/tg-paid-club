"""Crypto Pay (@CryptoBot): invoices priced in dollars and paid in USDT, TON or another coin.

How the API is used (https://help.send.tg/en/articles/10279948-crypto-pay-api):

* **Requests.** ``POST {api_url}/{method}`` with a JSON body and the app's token in the
  ``Crypto-Pay-API-Token`` header. The mainnet is ``https://pay.crypt.bot/api``; the testnet,
  with test coins and its own app made in @CryptoTestnetBot, is
  ``https://testnet-pay.crypt.bot/api``. Every answer is ``{"ok": true, "result": ...}`` or
  ``{"ok": false, "error": {"code": ..., "name": ...}}``. Numbers are read as decimals, never
  as floats.
* **Checkout.** ``createInvoice`` with ``currency_type: "fiat"``, ``fiat: "USD"`` and the plan's
  ``price_usd``, so a plan has one price whichever coin pays it; ``accepted_assets`` lists the
  coins (``PAIDCLUB_CRYPTOPAY_ASSETS``), and Crypto Bot converts at its own rate when the
  member pays. The ``payload`` is ``paidclub:<subscription>:<period>``. ``expires_in`` ends an
  unpaid invoice long before :mod:`paidclub.checkouts` stops looking it up. The member gets
  ``bot_invoice_url``, which opens the invoice in @CryptoBot.
* **No renewals.** Nobody can be charged without paying by hand: there is no saved method.
  :meth:`CryptoPayProvider.charge_renewal` reports ``no_saved_payment_method`` without a
  request, so a period end goes past due like any renewal with nothing to charge, and the
  member pays the next period with a new invoice from the "pay" button, early or during grace.
* **Webhooks are signed.** ``crypto-pay-api-signature`` is the hex HMAC-SHA-256 of the raw
  body, keyed with the SHA-256 of the API token. A request without a matching signature is
  rejected before anything else is done. The body is still not trusted for money: the invoice
  is fetched with ``getInvoices`` and its status there is what gets applied, as with YooKassa.
  ``invoice_paid`` is the only update Crypto Pay sends.
* **Lookups.** The same ``getInvoices(invoice_ids=...)`` answers
  :meth:`CryptoPayProvider.check_payment`, so a webhook that never arrives is caught by the
  checkout lookups. ``active`` is undecided, ``paid`` a payment, ``expired`` a failed checkout,
  which is recorded and leaves the subscription alone.
* **Refunds** are not made through the API: when core flags a payment that applied to nothing,
  the owner sends the coins back from @CryptoBot.

Invoices whose payload does not start with ``paidclub:`` were not created by this app (the same
Crypto Pay app may sell other things) and are ignored.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Self

import httpx

from paidclub.billing.base import (
    NO_SAVED_METHOD,
    BillingEvent,
    ChargeResult,
    Checkout,
    CheckoutRequest,
    PaymentFailed,
    PaymentPending,
    PaymentPurpose,
    PaymentSucceeded,
    ProviderError,
    ProviderSubscriptionRef,
    RenewalRequest,
    WebhookEvent,
    WebhookRejected,
)
from paidclub.clock import Clock, SystemClock, ensure_utc
from paidclub.config import Settings
from paidclub.money import Money

log = logging.getLogger(__name__)

API_URL = "https://pay.crypt.bot/api"
TESTNET_API_URL = "https://testnet-pay.crypt.bot/api"
AUTH_HEADER = "Crypto-Pay-API-Token"
SIGNATURE_HEADER = "crypto-pay-api-signature"
#: Invoices are priced in this currency; plans carry it as ``price_usd``.
FIAT = "USD"
#: Every invoice payload we create starts with this.
PAYLOAD_PREFIX = "paidclub:"
#: Crypto Pay limit for ``description``.
DESCRIPTION_MAX_LENGTH = 1024
#: Crypto Pay accepts ``expires_in`` up to 31 days; checkouts are looked up for 48 hours.
MAX_INVOICE_LIFETIME = timedelta(days=1)
DEFAULT_INVOICE_LIFETIME = timedelta(hours=1)
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

#: Invoice ids are integers; anything else is not looked up.
_INVOICE_ID = re.compile(r"^[0-9]{1,20}$")
_STATUSES = frozenset({"active", "paid", "expired"})


class CryptoPayError(ProviderError):
    """A Crypto Pay request that did not return a result: transport error, timeout, an HTTP
    error or ``"ok": false``. ``name`` is Crypto Pay's error name, such as ``UNAUTHORIZED``."""

    def __init__(self, message: str, *, status: int | None = None, name: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.name = name


def signature(api_token: str, body: bytes) -> str:
    """What Crypto Pay sends in ``crypto-pay-api-signature`` for ``body``."""
    key = hashlib.sha256(api_token.encode()).digest()
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def encode_payload(subscription_id: int, for_period: int) -> str:
    return f"{PAYLOAD_PREFIX}{subscription_id}:{for_period}"


def decode_payload(payload: Any) -> tuple[int, int] | None:
    """``(subscription, period)`` from one of our payloads, ``None`` for anything else."""
    if not isinstance(payload, str) or not payload.startswith(PAYLOAD_PREFIX):
        return None
    sub, _, period = payload.removeprefix(PAYLOAD_PREFIX).partition(":")
    if not (sub.isdigit() and period.isdigit()):
        return None
    return int(sub), int(period)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    return ensure_utc(datetime.fromisoformat(value))


def invoice_event(invoice: Mapping[str, Any]) -> BillingEvent | None:
    """Turn a Crypto Pay invoice into the event the app applies.

    Returns ``None`` while the invoice is ``active`` and for invoices this app did not create.
    Raises ``KeyError``/``ValueError``/``TypeError`` on a malformed one. The amount is the
    dollar price of the invoice, whichever coin paid it.
    """
    ours = decode_payload(invoice.get("payload"))
    if ours is None:
        return None
    status = invoice["status"]
    if status not in _STATUSES:
        raise ValueError(f"unknown invoice status {status!r}")
    if status == "active":
        return None
    if invoice.get("currency_type") != "fiat":
        raise ValueError(f"invoice priced in {invoice.get('currency_type')!r}, not in a fiat")
    created_at = _parse_time(invoice.get("created_at"))
    if created_at is None:
        raise ValueError("invoice has no created_at")
    subscription_id, for_period = ours
    common: dict[str, Any] = {
        "provider": CryptoPayProvider.name,
        "provider_payment_id": str(invoice["invoice_id"]),
        "subscription_id": subscription_id,
        "amount": Money.from_decimal(invoice["amount"], str(invoice["fiat"])),
        "purpose": PaymentPurpose.CHECKOUT,
        "for_period": for_period,
    }
    if status == "paid":
        return PaymentSucceeded(
            **common, occurred_at=_parse_time(invoice.get("paid_at")) or created_at
        )
    return PaymentFailed(
        **common,
        occurred_at=_parse_time(invoice.get("expiration_date")) or created_at,
        reason="expired",
    )


class CryptoPayProvider:
    name = "cryptopay"
    currency = FIAT
    #: Renewals are ours to start, but there is never anything to charge: see the module.
    supports_merchant_renewal = True

    def __init__(
        self,
        *,
        api_token: str,
        assets: Sequence[str],
        return_url: str | None = None,
        invoice_lifetime: timedelta = DEFAULT_INVOICE_LIFETIME,
        api_url: str = API_URL,
        client: httpx.AsyncClient | None = None,
        clock: Clock | None = None,
    ) -> None:
        if not api_token:
            raise ValueError("Crypto Pay needs an API token")
        if not assets:
            raise ValueError("Crypto Pay needs at least one coin to accept")
        if not timedelta(seconds=1) <= invoice_lifetime <= MAX_INVOICE_LIFETIME:
            raise ValueError(
                f"an invoice lifetime must be between 1 second and {MAX_INVOICE_LIFETIME}"
            )
        self._token = api_token
        self._assets = ",".join(assets)
        self._return_url = return_url
        self._expires_in = int(invoice_lifetime.total_seconds())
        self._api_url = api_url.rstrip("/")
        self._clock = clock or SystemClock()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Clock | None = None,
    ) -> Self:
        if settings.cryptopay_api_token is None:
            raise ValueError("set PAIDCLUB_CRYPTOPAY_API_TOKEN")
        return cls(
            api_token=settings.cryptopay_api_token.get_secret_value(),
            assets=settings.cryptopay_assets,
            return_url=settings.after_payment_url,
            invoice_lifetime=timedelta(minutes=settings.cryptopay_invoice_lifetime_minutes),
            api_url=settings.cryptopay_api_url,
            client=client,
            clock=clock,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # --- HTTP ------------------------------------------------------------------------------

    async def _call(self, method: str, params: Mapping[str, Any]) -> Any:
        """One API method; returns its ``result`` or raises :class:`CryptoPayError`."""
        where = f"Crypto Pay {method}"
        try:
            response = await self._client.post(
                f"{self._api_url}/{method}", json=dict(params), headers={AUTH_HEADER: self._token}
            )
        except httpx.TimeoutException as exc:
            raise CryptoPayError(f"{where}: timed out") from exc
        except httpx.HTTPError as exc:
            raise CryptoPayError(f"{where}: {type(exc).__name__}: {exc}") from exc
        try:
            data = json.loads(response.content, parse_float=Decimal)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise CryptoPayError(
                f"{where}: HTTP {response.status_code}, unexpected response "
                f"{response.text[:200]!r}",
                status=response.status_code,
            )
        if data.get("ok") is not True:
            error = data.get("error")
            name = error.get("name") if isinstance(error, dict) else None
            raise CryptoPayError(
                f"{where}: HTTP {response.status_code} {name or 'error'}",
                status=response.status_code,
                name=str(name) if name else None,
            )
        return data.get("result")

    async def get_invoice(self, invoice_id: str) -> dict[str, Any] | None:
        """The invoice with this id, or ``None`` if Crypto Pay has none."""
        if not _INVOICE_ID.match(invoice_id):
            raise ValueError(f"not a Crypto Pay invoice id: {invoice_id!r}")
        result = await self._call("getInvoices", {"invoice_ids": invoice_id})
        items = result.get("items") if isinstance(result, dict) else result
        if not isinstance(items, list):
            raise CryptoPayError("Crypto Pay getInvoices: the answer is not a list of invoices")
        # Matched by id, not taken as the first item: the filter is not ours to trust either.
        for item in items:
            if isinstance(item, dict) and str(item.get("invoice_id")) == invoice_id:
                return dict(item)
        return None

    def _result(self, invoice: Mapping[str, Any]) -> ChargeResult | None:
        """The state of a fetched invoice: an event, pending while active, ``None`` if foreign."""
        invoice_id = invoice.get("invoice_id")
        try:
            event = invoice_event(invoice)
        except (KeyError, TypeError, ValueError) as exc:
            raise CryptoPayError(f"malformed Crypto Pay invoice {invoice_id}: {exc}") from exc
        if event is None and invoice.get("status") == "active":
            return PaymentPending(provider=self.name, provider_payment_id=str(invoice_id))
        if isinstance(event, PaymentSucceeded):
            log.info(
                "Crypto Pay invoice %s paid: %s %s for %s",
                invoice_id,
                invoice.get("paid_amount"),
                invoice.get("paid_asset"),
                event.amount.human(),
            )
        return event

    # --- BillingProvider -------------------------------------------------------------------

    async def start_checkout(self, request: CheckoutRequest) -> Checkout:
        if request.amount.currency != FIAT:
            raise ValueError(
                f"Crypto Pay invoices are priced in {FIAT}, got {request.amount.currency}"
            )
        params: dict[str, Any] = {
            "currency_type": "fiat",
            "fiat": FIAT,
            "amount": request.amount.to_decimal_str(),
            "accepted_assets": self._assets,
            "description": request.description[:DESCRIPTION_MAX_LENGTH],
            "payload": encode_payload(request.subscription_id, request.for_period),
            "expires_in": self._expires_in,
        }
        return_url = request.return_url or self._return_url
        if return_url and return_url.startswith("https://"):
            # A "Return" button in @CryptoBot after paying; only a public address makes sense.
            params["paid_btn_name"] = "callback"
            params["paid_btn_url"] = return_url
        # No retry: createInvoice has no idempotence key, and a repeat would only make a second
        # invoice. The member taps "pay" again instead.
        invoice = await self._call("createInvoice", params)
        if not isinstance(invoice, dict) or invoice.get("invoice_id") is None:
            raise CryptoPayError("Crypto Pay createInvoice: the answer is not an invoice")
        url = invoice.get("bot_invoice_url") or invoice.get("mini_app_invoice_url")
        if not url:
            raise CryptoPayError(f"Crypto Pay invoice {invoice['invoice_id']} has no link to pay")
        return Checkout(url=str(url), provider_payment_id=str(invoice["invoice_id"]))

    async def charge_renewal(self, request: RenewalRequest) -> ChargeResult:
        """There is nothing to charge: the period end becomes a renewal the member pays."""
        return PaymentFailed(
            provider=self.name,
            provider_payment_id=f"no_method:{request.idempotence_key}",
            subscription_id=request.subscription_id,
            amount=request.amount,
            occurred_at=self._clock.now(),
            purpose=PaymentPurpose.RENEWAL,
            reason=NO_SAVED_METHOD,
            for_period=request.for_period,
            attempt=request.attempt,
        )

    async def check_payment(self, provider_payment_id: str) -> ChargeResult | None:
        if not _INVOICE_ID.match(provider_payment_id):
            return None  # one of our synthetic ids (no_method:...)
        invoice = await self.get_invoice(provider_payment_id)
        if invoice is None:
            return None
        result = self._result(invoice)
        if result is None:
            raise CryptoPayError(f"Crypto Pay invoice {provider_payment_id} lacks our payload")
        return result

    async def parse_webhook(
        self, headers: Mapping[str, str], body: bytes
    ) -> Sequence[WebhookEvent]:
        lowered = {key.lower(): value for key, value in headers.items()}
        given = lowered.get(SIGNATURE_HEADER, "").strip().lower()
        if not given or not hmac.compare_digest(
            given.encode(), signature(self._token, body).encode()
        ):
            raise WebhookRejected("bad or missing Crypto Pay signature")
        try:
            data = json.loads(body, parse_float=Decimal)
        except ValueError as exc:
            raise WebhookRejected("Crypto Pay update is not JSON") from exc
        if not isinstance(data, dict) or not isinstance(data.get("update_type"), str):
            raise WebhookRejected("not a Crypto Pay update")
        if data["update_type"] != "invoice_paid":
            log.info("ignoring Crypto Pay %s update", data["update_type"])
            return []
        sent = data.get("payload")
        invoice_id = str(sent.get("invoice_id")) if isinstance(sent, dict) else ""
        if not _INVOICE_ID.match(invoice_id):
            raise WebhookRejected("Crypto Pay update without a valid invoice id")
        invoice = await self.get_invoice(invoice_id)
        if invoice is None:
            raise WebhookRejected(f"Crypto Pay has no invoice {invoice_id}")
        result = self._result(invoice)
        if result is None or isinstance(result, PaymentPending):
            log.info(
                "Crypto Pay invoice %s: nothing to apply (status %s, payload %r)",
                invoice_id,
                invoice.get("status"),
                invoice.get("payload"),
            )
            return []
        return [result]

    async def cancel(self, ref: ProviderSubscriptionRef) -> None:
        """Nothing to do: Crypto Pay has no subscription, every period is a new invoice."""

    async def resume(self, ref: ProviderSubscriptionRef) -> None:
        """Nothing to do, see :meth:`cancel`."""
