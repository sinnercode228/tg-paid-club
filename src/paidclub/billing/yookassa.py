"""YooKassa (ЮKassa) card payments, with a saved payment method for renewals.

How the API is used (https://yookassa.ru/developers):

* **Auth.** HTTP Basic with the shop id as the user name and the secret key as the password.
* **Idempotence.** Every POST carries an ``Idempotence-Key``. YooKassa answers a repeated key
  with the original result for 24 hours after the first request.
* **First payment.** ``capture: true`` (one-stage), ``save_payment_method: true`` and a
  ``redirect`` confirmation: the member pays on YooKassa's page, and the result arrives as a
  webhook. The method can be charged later only if the payment object says
  ``payment_method.saved: true``.
* **Renewals.** A POST with ``payment_method_id`` and no confirmation. The idempotence key is
  the scheduler's ``paidclub-{subscription}-{period}-{attempt}``, so a retry after a timeout
  or a crash repeats the same request instead of charging again. A ``pending`` answer is
  re-checked with ``GET /payments/{id}``, because a repeated POST returns the cached first
  answer.
* **Webhooks are not signed.** The body is only trusted for the payment id. The payment is
  then fetched with the shop credentials, and the status of that fetched object is what gets
  applied. A forged notification can at most make us look up a real payment.

Payments carry our ``metadata`` (subscription, period, attempt, purpose) and a ``paidclub``
marker. Payments without the marker were not created by this app (the shop may sell other
things) and are ignored.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Self

import httpx

from paidclub.billing.base import (
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
    WebhookRejected,
)
from paidclub.clock import Clock, SystemClock, ensure_utc
from paidclub.config import Settings
from paidclub.money import Money

log = logging.getLogger(__name__)

API_URL = "https://api.yookassa.ru/v3"
#: Metadata key that marks payments created by this app.
MARKER = "paidclub"
#: YooKassa limit for ``description``.
DESCRIPTION_MAX_LENGTH = 128
#: YooKassa limit for the ``Idempotence-Key`` header.
IDEMPOTENCE_KEY_MAX_LENGTH = 64
#: YooKassa answers within 30 seconds (or with a 500), so wait a little longer than that.
DEFAULT_TIMEOUT = httpx.Timeout(40.0, connect=10.0)

#: Payment ids are UUID-like; anything else is not looked up (it would go into a URL path).
_PAYMENT_ID = re.compile(r"^[0-9A-Za-z-]{1,64}$")
_UNDECIDED = frozenset({"pending", "waiting_for_capture"})


class YooKassaError(ProviderError):
    """A YooKassa request that did not return a payment: transport error, timeout or an HTTP
    error. For a renewal charge the outcome is unknown, so the scheduler retries later with
    the same idempotence key, as YooKassa recommends for 500 answers."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        parameter: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.parameter = parameter


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    return ensure_utc(datetime.fromisoformat(value))


def payment_event(payment: Mapping[str, Any]) -> BillingEvent | None:
    """Turn a YooKassa payment object into the event the app applies.

    Returns ``None`` while the payment is undecided (``pending``, ``waiting_for_capture``) and
    for payments this app did not create. Raises ``KeyError``/``ValueError``/``TypeError`` on
    a malformed object.
    """
    metadata = payment.get("metadata") or {}
    if not isinstance(metadata, Mapping) or metadata.get(MARKER) != "1":
        return None
    status = payment["status"]
    if status in _UNDECIDED:
        return None
    amount = payment["amount"]
    created_at = _parse_time(payment.get("created_at"))
    if created_at is None:
        raise ValueError("payment has no created_at")
    common: dict[str, Any] = {
        "provider": YooKassaProvider.name,
        "provider_payment_id": str(payment["id"]),
        "subscription_id": _optional_int(metadata.get("subscription_id")),
        "amount": Money.from_decimal(str(amount["value"]), str(amount["currency"])),
        "purpose": PaymentPurpose(metadata.get("purpose") or PaymentPurpose.CHECKOUT.value),
        "for_period": _optional_int(metadata.get("for_period")),
        "attempt": _optional_int(metadata.get("attempt")),
    }
    if status == "succeeded":
        method = payment.get("payment_method") or {}
        saved = isinstance(method, Mapping) and method.get("saved") is True and method.get("id")
        return PaymentSucceeded(
            **common,
            occurred_at=_parse_time(payment.get("captured_at")) or created_at,
            payment_method_id=str(method["id"]) if saved else None,
        )
    if status == "canceled":
        details = payment.get("cancellation_details") or {}
        reason = details.get("reason") if isinstance(details, Mapping) else None
        return PaymentFailed(**common, occurred_at=created_at, reason=str(reason or "canceled"))
    raise ValueError(f"unknown payment status {status!r}")


class YooKassaProvider:
    name = "yookassa"
    currency = "RUB"
    supports_merchant_renewal = True

    def __init__(
        self,
        *,
        shop_id: str,
        secret_key: str,
        return_url: str,
        api_url: str = API_URL,
        client: httpx.AsyncClient | None = None,
        clock: Clock | None = None,
    ) -> None:
        if not shop_id or not secret_key:
            raise ValueError("YooKassa needs a shop id and a secret key")
        self._auth = httpx.BasicAuth(shop_id, secret_key)
        self._clock = clock or SystemClock()
        self._return_url = return_url
        self._api_url = api_url.rstrip("/")
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
        if not settings.yookassa_shop_id or settings.yookassa_secret_key is None:
            raise ValueError("set PAIDCLUB_YOOKASSA_SHOP_ID and PAIDCLUB_YOOKASSA_SECRET_KEY")
        return cls(
            shop_id=settings.yookassa_shop_id,
            secret_key=settings.yookassa_secret_key.get_secret_value(),
            return_url=settings.checkout_return_url or settings.public_base_url,
            api_url=settings.yookassa_api_url,
            client=client,
            clock=clock,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # --- HTTP ------------------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        idempotence_key: str | None = None,
    ) -> dict[str, Any]:
        headers = {}
        if idempotence_key is not None:
            if len(idempotence_key) > IDEMPOTENCE_KEY_MAX_LENGTH:
                raise ValueError(f"idempotence key longer than {IDEMPOTENCE_KEY_MAX_LENGTH}")
            headers["Idempotence-Key"] = idempotence_key
        where = f"YooKassa {method} {path}"
        try:
            response = await self._client.request(
                method, f"{self._api_url}{path}", json=body, headers=headers, auth=self._auth
            )
        except httpx.TimeoutException as exc:
            raise YooKassaError(f"{where}: timed out") from exc
        except httpx.HTTPError as exc:
            raise YooKassaError(f"{where}: {type(exc).__name__}: {exc}") from exc
        try:
            data = response.json()
        except ValueError:
            data = None
        if not response.is_success:
            error = data if isinstance(data, dict) else {}
            code, parameter = error.get("code"), error.get("parameter")
            parts = [f"HTTP {response.status_code}", code, error.get("description")]
            if parameter:
                parts.append(f"(parameter {parameter})")
            raise YooKassaError(
                f"{where}: " + " ".join(str(part) for part in parts if part),
                status=response.status_code,
                code=code,
                parameter=parameter,
            )
        if not isinstance(data, dict) or "id" not in data or "status" not in data:
            raise YooKassaError(f"{where}: unexpected response {response.text[:200]!r}")
        return data

    async def get_payment(self, payment_id: str) -> dict[str, Any]:
        if not _PAYMENT_ID.match(payment_id):
            raise ValueError(f"not a YooKassa payment id: {payment_id!r}")
        return await self._request("GET", f"/payments/{payment_id}")

    @staticmethod
    def _amount(amount: Money) -> dict[str, str]:
        if amount.currency != "RUB":
            raise ValueError(f"YooKassa charges RUB here, got {amount.currency}")
        return {"value": amount.to_decimal_str(), "currency": amount.currency}

    @staticmethod
    def _metadata(
        *, subscription_id: int, telegram_id: int, for_period: int, purpose: PaymentPurpose
    ) -> dict[str, str]:
        return {
            MARKER: "1",
            "subscription_id": str(subscription_id),
            "telegram_id": str(telegram_id),
            "for_period": str(for_period),
            "purpose": purpose.value,
        }

    # --- BillingProvider -------------------------------------------------------------------

    async def start_checkout(self, request: CheckoutRequest) -> Checkout:
        body = {
            "amount": self._amount(request.amount),
            "capture": True,
            "save_payment_method": True,
            "confirmation": {
                "type": "redirect",
                "return_url": request.return_url or self._return_url,
            },
            "description": request.description[:DESCRIPTION_MAX_LENGTH],
            "metadata": self._metadata(
                subscription_id=request.subscription_id,
                telegram_id=request.member_telegram_id,
                for_period=request.for_period,
                purpose=PaymentPurpose.CHECKOUT,
            ),
        }
        # A fresh key per checkout: tapping "pay" twice creates two payment pages, and core
        # recognises a second payment for the same period if both get paid.
        key = str(uuid.uuid4())
        try:
            payment = await self._request("POST", "/payments", body=body, idempotence_key=key)
        except YooKassaError as exc:
            if exc.status is not None and exc.status < 500:
                raise
            # Outcome unknown: the same key returns the payment if the first call created it.
            log.warning("retrying checkout for subscription %s: %s", request.subscription_id, exc)
            payment = await self._request("POST", "/payments", body=body, idempotence_key=key)
        confirmation = payment.get("confirmation") or {}
        url = confirmation.get("confirmation_url") if isinstance(confirmation, dict) else None
        if not url:
            raise YooKassaError(f"YooKassa payment {payment['id']} has no confirmation_url")
        return Checkout(url=str(url), provider_payment_id=str(payment["id"]))

    async def charge_renewal(self, request: RenewalRequest) -> ChargeResult:
        if request.payment_method_id is None:
            return PaymentFailed(
                provider=self.name,
                provider_payment_id=f"no_method:{request.idempotence_key}",
                subscription_id=request.subscription_id,
                amount=request.amount,
                occurred_at=self._clock.now(),
                purpose=PaymentPurpose.RENEWAL,
                reason="no_saved_payment_method",
                for_period=request.for_period,
                attempt=request.attempt,
            )
        metadata = self._metadata(
            subscription_id=request.subscription_id,
            telegram_id=request.member_telegram_id,
            for_period=request.for_period,
            purpose=PaymentPurpose.RENEWAL,
        )
        metadata["attempt"] = str(request.attempt)
        body = {
            "amount": self._amount(request.amount),
            "capture": True,
            "payment_method_id": request.payment_method_id,
            "description": request.description[:DESCRIPTION_MAX_LENGTH],
            "metadata": metadata,
        }
        payment = await self._request(
            "POST", "/payments", body=body, idempotence_key=request.idempotence_key
        )
        if payment["status"] in _UNDECIDED:
            # A repeated key returns the first answer as it was; ask for the current state.
            payment = await self.get_payment(str(payment["id"]))
        try:
            event = payment_event(payment)
        except (KeyError, TypeError, ValueError) as exc:
            raise YooKassaError(f"malformed YooKassa payment {payment.get('id')}: {exc}") from exc
        if event is None:
            if payment["status"] not in _UNDECIDED:
                raise YooKassaError(f"YooKassa payment {payment['id']} lacks our metadata")
            if payment["status"] == "waiting_for_capture":
                log.warning(
                    "YooKassa payment %s is waiting for capture although capture=true was sent; "
                    "check that the shop is not set up for two-stage payments",
                    payment["id"],
                )
            return PaymentPending(provider=self.name, provider_payment_id=str(payment["id"]))
        return event

    async def parse_webhook(
        self, headers: Mapping[str, str], body: bytes
    ) -> Sequence[PaymentSucceeded | PaymentFailed]:
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise WebhookRejected("YooKassa notification is not JSON") from exc
        if not isinstance(data, dict) or data.get("type") != "notification":
            raise WebhookRejected("not a YooKassa notification")
        event_name, obj = data.get("event"), data.get("object")
        if not isinstance(event_name, str) or not isinstance(obj, dict):
            raise WebhookRejected("YooKassa notification without event or object")
        if not event_name.startswith("payment."):
            log.info("ignoring YooKassa %s notification", event_name)
            return []
        payment_id = obj.get("id")
        if not isinstance(payment_id, str) or not _PAYMENT_ID.match(payment_id):
            raise WebhookRejected("YooKassa notification without a valid payment id")
        try:
            payment = await self.get_payment(payment_id)
        except YooKassaError as exc:
            if exc.status == 404:
                raise WebhookRejected(f"YooKassa has no payment {payment_id}") from exc
            raise
        try:
            event = payment_event(payment)
        except (KeyError, TypeError, ValueError) as exc:
            raise YooKassaError(f"malformed YooKassa payment {payment_id}: {exc}") from exc
        if event is None:
            log.info(
                "YooKassa payment %s (%s): nothing to apply (status %s)",
                payment_id,
                event_name,
                payment.get("status"),
            )
            return []
        return [event]

    async def cancel(self, ref: ProviderSubscriptionRef) -> None:
        """Nothing to do: YooKassa has no subscription, renewals are our own charges."""

    async def resume(self, ref: ProviderSubscriptionRef) -> None:
        """Nothing to do, see :meth:`cancel`."""
