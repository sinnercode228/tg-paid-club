"""The contract every billing provider implements, and the events it reports.

A provider only talks to the payment system. It never touches the database: whatever it learns
(a webhook, the result of a renewal charge, a Telegram Stars payment) becomes a
:class:`PaymentSucceeded` or :class:`PaymentFailed`, and every one of those goes through
:func:`paidclub.payments.apply_payment_event`, the single place where money changes state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from paidclub.money import Money


class PaymentPurpose(StrEnum):
    #: The member paid through a checkout page or invoice: first payment, or the "pay" button
    #: after a failed renewal. Failures here do not count as dunning attempts.
    CHECKOUT = "checkout"
    #: A renewal: merchant-initiated charge of a saved method, or a Stars auto-renewal.
    RENEWAL = "renewal"


@dataclass(frozen=True, slots=True)
class CheckoutRequest:
    subscription_id: int
    member_telegram_id: int
    plan_code: str
    plan_title: str
    amount: Money
    #: Which billing period this payment covers (1 for a new subscription).
    for_period: int
    #: Shown on the provider's page / receipt.
    description: str
    #: Where the provider sends the member after paying (YooKassa ``confirmation.return_url``).
    return_url: str | None = None


@dataclass(frozen=True, slots=True)
class Checkout:
    url: str
    #: Some providers create the payment up front (YooKassa, the fake provider).
    provider_payment_id: str | None = None


@dataclass(frozen=True, slots=True)
class RenewalRequest:
    subscription_id: int
    member_telegram_id: int
    plan_code: str
    amount: Money
    for_period: int
    attempt: int
    #: Saved method to charge; ``None`` means the member never allowed saving one.
    payment_method_id: str | None
    #: Derived from (subscription, period, attempt); the same request after a crash reuses it.
    idempotence_key: str
    description: str
    #: When a request with this key was first sent, if this is a repeat of one whose answer
    #: never came. A provider that forgets keys after a while (YooKassa: 24 hours) must look
    #: the payment up instead of sending the request again once the key may be forgotten.
    first_sent_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ProviderSubscriptionRef:
    subscription_id: int
    member_telegram_id: int
    payment_method_id: str | None


@dataclass(frozen=True, slots=True)
class PaymentSucceeded:
    provider: str
    provider_payment_id: str
    #: Our subscription id, read back from the provider's metadata / invoice payload.
    subscription_id: int | None
    amount: Money
    occurred_at: datetime
    purpose: PaymentPurpose
    #: The period this pays for, when the provider carries it (our metadata). ``None`` = next.
    for_period: int | None = None
    attempt: int | None = None
    #: Set when the provider decides the period end itself (Stars ``subscription_expiration_date``).
    period_end: datetime | None = None
    #: A payment method saved for renewals (YooKassa), or the Stars subscription charge id.
    payment_method_id: str | None = None


@dataclass(frozen=True, slots=True)
class PaymentFailed:
    provider: str
    provider_payment_id: str
    subscription_id: int | None
    amount: Money
    occurred_at: datetime
    purpose: PaymentPurpose
    reason: str
    for_period: int | None = None
    attempt: int | None = None
    #: Set when the provider refused for a reason on the shop's side (credentials, a feature
    #: not enabled for the shop) that only the owner can fix; core alerts the owner.
    owner_note: str | None = None


#: Failure reasons of renewals that were due but never sent to a provider: nothing was charged.
NO_SAVED_METHOD = "no_saved_payment_method"
PROVIDER_NOT_CONFIGURED = "provider_not_configured"
NOT_CHARGED_REASONS = frozenset({NO_SAVED_METHOD, PROVIDER_NOT_CONFIGURED})


@dataclass(frozen=True, slots=True)
class PaymentPending:
    """The provider accepted a renewal charge but has not decided; a webhook will follow."""

    provider: str
    provider_payment_id: str


@dataclass(frozen=True, slots=True)
class PaymentRefunded:
    """Money of a payment went back to the member, all of it or a part (YooKassa
    ``refund.succeeded``, made by the owner in the shop's dashboard)."""

    provider: str
    #: The payment that was refunded.
    provider_payment_id: str
    refund_id: str
    amount: Money
    occurred_at: datetime


BillingEvent = PaymentSucceeded | PaymentFailed
ChargeResult = PaymentSucceeded | PaymentFailed | PaymentPending
#: What a notification can report.
WebhookEvent = PaymentSucceeded | PaymentFailed | PaymentRefunded


class WebhookRejected(Exception):
    """The request is not a genuine, well-formed provider notification (answer 4xx)."""


class ProviderError(Exception):
    """The provider could not be reached or answered with a server error.

    For a renewal charge this means "outcome unknown": the attempt is retried later with the
    same idempotence key, which the provider deduplicates.
    """


@runtime_checkable
class BillingProvider(Protocol):
    #: Stored on subscriptions and payments: ``"fake"``, ``"yookassa"``, ``"stars"``.
    name: str
    #: ``"RUB"`` or ``"XTR"``; picks the plan price.
    currency: str
    #: ``True`` when we charge renewals ourselves (``charge_renewal``), ``False`` when the
    #: provider renews on its own and reports the payment (Telegram Stars).
    supports_merchant_renewal: bool

    async def start_checkout(self, request: CheckoutRequest) -> Checkout:
        """Create a payment page / invoice link for the member to pay."""
        ...

    async def charge_renewal(self, request: RenewalRequest) -> ChargeResult:
        """Charge the saved method. Must be idempotent per ``request.idempotence_key``.

        Return :class:`PaymentFailed` when the provider refused and nothing was charged
        (a declined card, but also a request the provider rejects for good). Raise
        :class:`ProviderError` only when the outcome is unknown (timeouts, 429, 5xx).
        """
        ...

    async def check_payment(self, provider_payment_id: str) -> ChargeResult | None:
        """The current state of a payment this app created: an event once the provider has
        decided, :class:`PaymentPending` while it has not.

        Used for renewal charges reported as pending and for checkouts whose notification
        may never arrive. ``None`` when the provider cannot look this payment up. Raise
        :class:`ProviderError` when it cannot be reached.
        """
        ...

    async def parse_webhook(
        self, headers: Mapping[str, str], body: bytes
    ) -> Sequence[WebhookEvent]:
        """Verify and parse a notification. Return ``[]`` for events we ignore.

        Raise :class:`WebhookRejected` when it cannot be verified.
        """
        ...

    async def cancel(self, ref: ProviderSubscriptionRef) -> None:
        """Stop provider-side auto-renewal, if the provider has any (Stars)."""
        ...

    async def resume(self, ref: ProviderSubscriptionRef) -> None:
        """Undo :meth:`cancel` before the period ends."""
        ...
