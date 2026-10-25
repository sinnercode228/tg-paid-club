"""Fake billing provider for the demo, the ``simulate`` command and tests.

It behaves like a real card provider where it matters:

* checkout produces a URL to a local payment page (served by ``paidclub.web.fake_checkout``);
  the page's buttons produce a signed webhook that goes through the same verify-and-apply path
  as YooKassa's;
* webhooks are HMAC-signed and :meth:`FakeProvider.parse_webhook` rejects anything else;
* renewals charge a "saved card" and are idempotent per idempotence key: the outcome is a pure
  function of the key, so a retry after a crash gets the same payment id and the same answer.

Saved cards encode how renewals behave: ``fake_pm_ok_*`` always pays, ``fake_pm_decline_*``
is always declined, which lets the demo show dunning without waiting for a random failure.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from paidclub.billing.base import (
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
from paidclub.clock import Clock
from paidclub.money import Money

SIGNATURE_HEADER = "X-Fake-Signature"
CardKind = Literal["ok", "decline"]
#: What a renewal decider may answer; ``"error"`` simulates a timeout (outcome unknown).
Decision = Literal["succeeded", "failed", "pending", "error"]


@dataclass(frozen=True, slots=True)
class FakeCheckoutSession:
    """Everything the fake payment page needs, carried in a signed token (no server state)."""

    payment_id: str
    subscription_id: int
    member_telegram_id: int
    plan_title: str
    amount: Money
    for_period: int
    description: str
    return_url: str | None


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class FakeProvider:
    name = "fake"
    currency = "RUB"
    supports_merchant_renewal = True

    def __init__(
        self,
        *,
        secret: str,
        base_url: str,
        clock: Clock,
        failure_rate: float = 0.0,
        decide: Callable[[RenewalRequest], Decision] | None = None,
    ) -> None:
        if not secret:
            raise ValueError("the fake provider needs a signing secret")
        if not 0.0 <= failure_rate <= 1.0:
            raise ValueError("failure_rate must be between 0 and 1")
        self._secret = secret.encode()
        self._base_url = base_url.rstrip("/")
        self._clock = clock
        self._failure_rate = failure_rate
        self._decide = decide
        #: Idempotence keys of every renewal call, in order (tests assert on retries).
        self.renewal_calls: list[str] = []
        self.canceled: list[int] = []
        self.resumed: list[int] = []

    # --- signing ---------------------------------------------------------------------------

    def _sign(self, data: bytes) -> str:
        return hmac.new(self._secret, data, hashlib.sha256).hexdigest()

    def checkout_token(self, session: FakeCheckoutSession) -> str:
        payload = {
            "pid": session.payment_id,
            "sub": session.subscription_id,
            "tg": session.member_telegram_id,
            "title": session.plan_title,
            "amount": session.amount.minor,
            "cur": session.amount.currency,
            "period": session.for_period,
            "desc": session.description,
            "ret": session.return_url,
        }
        raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        return f"{_b64(raw)}.{self._sign(raw)}"

    def read_checkout(self, token: str) -> FakeCheckoutSession:
        """Decode a token from a checkout URL; raises :class:`WebhookRejected` if forged."""
        try:
            encoded, signature = token.split(".", 1)
            raw = _unb64(encoded)
        except (ValueError, TypeError) as exc:
            raise WebhookRejected("malformed checkout token") from exc
        if not hmac.compare_digest(self._sign(raw), signature):
            raise WebhookRejected("bad checkout token signature")
        data = json.loads(raw)
        return FakeCheckoutSession(
            payment_id=data["pid"],
            subscription_id=data["sub"],
            member_telegram_id=data["tg"],
            plan_title=data["title"],
            amount=Money(data["amount"], data["cur"]),
            for_period=data["period"],
            description=data["desc"],
            return_url=data["ret"],
        )

    # --- checkout --------------------------------------------------------------------------

    async def start_checkout(self, request: CheckoutRequest) -> Checkout:
        session = FakeCheckoutSession(
            payment_id=f"fake_{secrets.token_hex(8)}",
            subscription_id=request.subscription_id,
            member_telegram_id=request.member_telegram_id,
            plan_title=request.plan_title,
            amount=request.amount,
            for_period=request.for_period,
            description=request.description,
            return_url=request.return_url,
        )
        url = f"{self._base_url}/fake-checkout/{self.checkout_token(session)}"
        return Checkout(url=url, provider_payment_id=session.payment_id)

    def checkout_webhook(
        self,
        session: FakeCheckoutSession,
        *,
        succeeded: bool,
        card: CardKind | None = "ok",
        reason: str = "card_declined",
    ) -> tuple[dict[str, str], bytes]:
        """The notification the fake payment page sends when the member picks an outcome.

        ``card=None`` models a member who paid without letting us save the card: the period is
        paid, but renewals will fail with ``no_saved_payment_method``.
        """
        method = None
        if succeeded and card is not None:
            method = f"fake_pm_{card}_{secrets.token_hex(6)}"
        return self._webhook(
            event="payment.succeeded" if succeeded else "payment.failed",
            payment_id=session.payment_id,
            subscription_id=session.subscription_id,
            amount=session.amount,
            purpose=PaymentPurpose.CHECKOUT,
            for_period=session.for_period,
            attempt=None,
            payment_method_id=method,
            reason=None if succeeded else reason,
        )

    def renewal_webhook(
        self, request: RenewalRequest, *, succeeded: bool, reason: str = "card_declined"
    ) -> tuple[dict[str, str], bytes]:
        """The notification that settles a renewal the provider first reported as pending."""
        return self._webhook(
            event="payment.succeeded" if succeeded else "payment.failed",
            payment_id=self.renewal_payment_id(request.idempotence_key),
            subscription_id=request.subscription_id,
            amount=request.amount,
            purpose=PaymentPurpose.RENEWAL,
            for_period=request.for_period,
            attempt=request.attempt,
            payment_method_id=None,
            reason=None if succeeded else reason,
        )

    def _webhook(
        self,
        *,
        event: str,
        payment_id: str,
        subscription_id: int,
        amount: Money,
        purpose: PaymentPurpose,
        for_period: int,
        attempt: int | None,
        payment_method_id: str | None,
        reason: str | None,
    ) -> tuple[dict[str, str], bytes]:
        body = json.dumps(
            {
                "event": event,
                "payment": {
                    "id": payment_id,
                    "subscription_id": subscription_id,
                    "amount": amount.to_decimal_str(),
                    "currency": amount.currency,
                    "purpose": purpose.value,
                    "for_period": for_period,
                    "attempt": attempt,
                    "payment_method_id": payment_method_id,
                    "reason": reason,
                    "occurred_at": self._clock.now().isoformat(),
                },
            },
            separators=(",", ":"),
        ).encode()
        headers = {
            "Content-Type": "application/json",
            SIGNATURE_HEADER: f"sha256={self._sign(body)}",
        }
        return headers, body

    async def parse_webhook(
        self, headers: Mapping[str, str], body: bytes
    ) -> Sequence[PaymentSucceeded | PaymentFailed]:
        lowered = {key.lower(): value for key, value in headers.items()}
        signature = lowered.get(SIGNATURE_HEADER.lower(), "")
        if not signature.startswith("sha256=") or not hmac.compare_digest(
            signature.removeprefix("sha256="), self._sign(body)
        ):
            raise WebhookRejected("bad or missing fake webhook signature")
        try:
            data: dict[str, Any] = json.loads(body)
            payment = data["payment"]
            event = data["event"]
            common: dict[str, Any] = {
                "provider": self.name,
                "provider_payment_id": str(payment["id"]),
                "subscription_id": int(payment["subscription_id"]),
                "amount": Money.from_decimal(payment["amount"], payment["currency"]),
                "occurred_at": datetime.fromisoformat(payment["occurred_at"]),
                "purpose": PaymentPurpose(payment["purpose"]),
                "for_period": payment.get("for_period"),
                "attempt": payment.get("attempt"),
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise WebhookRejected(f"malformed fake webhook: {exc}") from exc
        if event == "payment.succeeded":
            return [PaymentSucceeded(**common, payment_method_id=payment.get("payment_method_id"))]
        if event == "payment.failed":
            return [PaymentFailed(**common, reason=str(payment.get("reason") or "declined"))]
        return []

    # --- renewals --------------------------------------------------------------------------

    @staticmethod
    def renewal_payment_id(idempotence_key: str) -> str:
        return "fake_r_" + hashlib.sha256(idempotence_key.encode()).hexdigest()[:20]

    def _default_decision(self, request: RenewalRequest) -> Decision:
        method = request.payment_method_id or ""
        if method.startswith("fake_pm_decline"):
            return "failed"
        if self._failure_rate:
            digest = hashlib.sha256(f"rate:{request.idempotence_key}".encode()).digest()
            if int.from_bytes(digest[:4], "big") / 2**32 < self._failure_rate:
                return "failed"
        return "succeeded"

    async def charge_renewal(self, request: RenewalRequest) -> ChargeResult:
        self.renewal_calls.append(request.idempotence_key)
        payment_id = self.renewal_payment_id(request.idempotence_key)
        common: dict[str, Any] = {
            "provider": self.name,
            "provider_payment_id": payment_id,
            "subscription_id": request.subscription_id,
            "amount": request.amount,
            "occurred_at": self._clock.now(),
            "purpose": PaymentPurpose.RENEWAL,
            "for_period": request.for_period,
            "attempt": request.attempt,
        }
        if request.payment_method_id is None:
            return PaymentFailed(**common, reason="no_saved_payment_method")
        decision = self._decide(request) if self._decide else self._default_decision(request)
        if decision == "error":
            raise ProviderError("fake provider timeout")
        if decision == "pending":
            return PaymentPending(provider=self.name, provider_payment_id=payment_id)
        if decision == "failed":
            return PaymentFailed(**common, reason="card_declined")
        return PaymentSucceeded(**common)

    async def cancel(self, ref: ProviderSubscriptionRef) -> None:
        self.canceled.append(ref.subscription_id)

    async def resume(self, ref: ProviderSubscriptionRef) -> None:
        self.resumed.append(ref.subscription_id)
