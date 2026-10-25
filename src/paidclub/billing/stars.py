"""Telegram Stars subscriptions: the invoice link, the payment updates and cancellation.

What the Bot API offers (checked against aiogram's ``CreateInvoiceLink``,
``SuccessfulPayment`` and ``EditUserStarSubscription``):

* ``createInvoiceLink`` with ``currency="XTR"``, exactly one price and
  ``subscription_period=2592000`` creates a link to a subscription. The period must be 30 days
  and the price at most 10000 Stars, so Stars sells only monthly plans here.
* Telegram charges renewals itself. Payments arrive as messages with ``successful_payment``:
  ``invoice_payload`` is the payload of the invoice, ``telegram_payment_charge_id`` identifies
  the payment, ``is_recurring`` marks a recurring payment for a subscription,
  ``is_first_recurring`` the first one, and ``subscription_expiration_date`` says when the
  paid period ends.
* ``editUserStarSubscription(user_id, telegram_payment_charge_id, is_canceled)`` turns
  auto-renewal off, or lets the member turn it back on after the bot turned it off.
  Telegram's documentation of bot subscriptions names the charge id of the first payment for
  this, which is why that one is stored as the payment method.
* The Bot API has no update for a renewal that failed. Core treats a renewal that has not
  arrived by the period end (plus ``stars_renewal_wait_minutes``) as failed.
* Before a member pays, Telegram sends a ``pre_checkout_query`` that must be answered within
  10 seconds. Answering it does not move money; only ``successful_payment`` does.

Refunds (``refundStarPayment``) are out of scope: when a payment cannot be applied, core alerts
the owner, who can refund it.

The provider never touches the database. :func:`create_stars_router` holds the handlers that
turn Telegram updates into payments: each ``successful_payment`` is stored as it arrived
(``stars_payment_updates``) before it is applied, because Telegram does not deliver it again,
and the scheduler applies stored ones that failed (:func:`retry_stored_payments`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import LabeledPrice, Message, PreCheckoutQuery, SuccessfulPayment
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from paidclub.billing.base import (
    ChargeResult,
    Checkout,
    CheckoutRequest,
    PaymentPurpose,
    PaymentSucceeded,
    ProviderError,
    ProviderSubscriptionRef,
    RenewalRequest,
    WebhookEvent,
    WebhookRejected,
)
from paidclub.config import STARS_SUBSCRIPTION_PERIOD
from paidclub.container import Core
from paidclub.db import in_transaction, insert_ignore
from paidclub.domain.subscriptions import SubStatus
from paidclub.models import PaymentOutcome, StarsPaymentStatus, StarsPaymentUpdate, Subscription
from paidclub.money import Money
from paidclub.payments import ApplyResult, alert_owner, apply_payment_event
from paidclub.repo import live_subscription

log = logging.getLogger(__name__)

PROVIDER_NAME = "stars"
CURRENCY = "XTR"
#: The only period the Bot API accepts for ``subscription_period`` (30 days).
SUBSCRIPTION_PERIOD_SECONDS = int(STARS_SUBSCRIPTION_PERIOD.total_seconds())
#: Bot API limit for the price of one subscription period.
MAX_SUBSCRIPTION_PRICE = 10_000
#: Every invoice payload we create starts with this, so other invoices of the same bot are
#: left to other handlers.
PAYLOAD_PREFIX = "paidclub:"
TITLE_MAX_LENGTH = 32
DESCRIPTION_MAX_LENGTH = 255
#: Telegram cancels the payment if the pre-checkout query is not answered within 10 seconds.
PRE_CHECKOUT_TIMEOUT = 5.0

# Shown by Telegram in the payment window when a pre-checkout query is declined.
ERROR_INVALID = "Этот счёт недействителен. Откройте /start и выберите тариф заново."
ERROR_AMOUNT = "Цена подписки изменилась. Откройте /start и запросите новый счёт."
ERROR_ALREADY_PAID = "Этот период уже оплачен, повторная оплата не нужна."
ERROR_STALE = "Этот счёт устарел. Откройте /start и оплатите подписку по новой кнопке."
ERROR_HAS_SUBSCRIPTION = "У вас уже есть действующая подписка. Проверьте её в /start."
ERROR_UNAVAILABLE = "Не удалось проверить счёт. Попробуйте ещё раз через минуту."
#: Sent to a member whose payment arrived but paid for nothing (the owner is alerted by core).
NOT_APPLIED_NOTICE = (
    "Платёж получен, но этот период уже оплачен. Владелец клуба получил уведомление "
    "и разберётся с возвратом Stars."
)


@dataclass(frozen=True, slots=True)
class InvoicePayload:
    subscription_id: int
    #: The period this invoice was created for. Renewals carry the payload of the first
    #: invoice, so this is only meaningful for the first payment.
    for_period: int

    def encode(self) -> str:
        return f"{PAYLOAD_PREFIX}{self.subscription_id}:{self.for_period}"

    @classmethod
    def decode(cls, payload: str) -> InvoicePayload | None:
        if not payload.startswith(PAYLOAD_PREFIX):
            return None
        sub, _, period = payload.removeprefix(PAYLOAD_PREFIX).partition(":")
        if not (sub.isdigit() and period.isdigit()):
            return None
        return cls(int(sub), int(period))


def payment_event(payment: SuccessfulPayment, *, occurred_at: datetime) -> PaymentSucceeded | None:
    """The event for a ``successful_payment``, or ``None`` if it is not one of our invoices.

    The first payment of a Telegram subscription is a checkout: it carries its charge id as the
    payment method (``cancel`` needs it) and the period its invoice was created for, so paying
    the same invoice twice is caught as a repeat. Every later payment is a renewal of that
    Telegram subscription and names no period. The period end comes from
    ``subscription_expiration_date``, because Telegram decides when the period ends.
    """
    payload = InvoicePayload.decode(payment.invoice_payload)
    if payload is None or payment.currency != CURRENCY:
        return None
    first = bool(payment.is_first_recurring) or not payment.is_recurring
    expires = payment.subscription_expiration_date
    subscription = bool(payment.is_recurring or payment.is_first_recurring or expires)
    return PaymentSucceeded(
        provider=PROVIDER_NAME,
        provider_payment_id=payment.telegram_payment_charge_id,
        subscription_id=payload.subscription_id,
        amount=Money(payment.total_amount, CURRENCY),
        occurred_at=occurred_at,
        purpose=PaymentPurpose.CHECKOUT if first else PaymentPurpose.RENEWAL,
        for_period=payload.for_period if first else None,
        period_end=datetime.fromtimestamp(expires, UTC) if expires else None,
        payment_method_id=payment.telegram_payment_charge_id if first and subscription else None,
    )


class StarsProvider:
    """Telegram Stars as a :class:`~paidclub.billing.base.BillingProvider`.

    Telegram renews on its own (``supports_merchant_renewal = False``), so ``charge_renewal``
    is never called and there are no webhooks: payments arrive as bot updates, handled by
    :func:`create_stars_router`.
    """

    name = PROVIDER_NAME
    currency = CURRENCY
    supports_merchant_renewal = False

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def start_checkout(self, request: CheckoutRequest) -> Checkout:
        if request.amount.currency != CURRENCY:
            raise ValueError(f"Stars invoices are in XTR, got {request.amount.currency}")
        if not 0 < request.amount.minor <= MAX_SUBSCRIPTION_PRICE:
            raise ValueError(
                f"a Stars subscription costs 1 to {MAX_SUBSCRIPTION_PRICE} Stars per period, "
                f"plan {request.plan_code!r} asks for {request.amount.minor}"
            )
        payload = InvoicePayload(request.subscription_id, request.for_period).encode()
        try:
            link = await self.bot.create_invoice_link(
                title=request.plan_title[:TITLE_MAX_LENGTH],
                description=request.description[:DESCRIPTION_MAX_LENGTH],
                payload=payload,
                currency=CURRENCY,
                prices=[LabeledPrice(label=request.plan_title, amount=request.amount.minor)],
                subscription_period=SUBSCRIPTION_PERIOD_SECONDS,
            )
        except TelegramAPIError as exc:
            raise ProviderError(f"createInvoiceLink failed: {exc}") from exc
        return Checkout(url=link)

    async def charge_renewal(self, request: RenewalRequest) -> ChargeResult:
        raise ProviderError("Telegram charges Stars renewals itself; there is nothing to charge")

    async def check_payment(self, provider_payment_id: str) -> ChargeResult | None:
        """The Bot API cannot look payments up; they arrive as updates."""
        return None

    async def parse_webhook(
        self, headers: Mapping[str, str], body: bytes
    ) -> Sequence[WebhookEvent]:
        raise WebhookRejected("Stars payments arrive as bot updates, not webhooks")

    async def cancel(self, ref: ProviderSubscriptionRef) -> None:
        """Turn Telegram's auto-renewal off.

        A ``Bad Request`` means Telegram has no active subscription to stop (it lapsed, or the
        member cancelled it in Telegram), so nothing will be charged and cancelling on our side
        goes ahead. Network and server errors raise :class:`ProviderError`.
        """
        if ref.payment_method_id is None:
            log.warning("subscription %s has no Stars charge id to cancel", ref.subscription_id)
            return
        try:
            await self.bot.edit_user_star_subscription(
                user_id=ref.member_telegram_id,
                telegram_payment_charge_id=ref.payment_method_id,
                is_canceled=True,
            )
        except TelegramBadRequest as exc:
            log.warning(
                "Telegram refused to cancel Stars subscription %s of %s: %s",
                ref.payment_method_id,
                ref.member_telegram_id,
                exc.message,
            )
        except TelegramAPIError as exc:
            raise ProviderError(f"editUserStarSubscription failed: {exc}") from exc

    async def resume(self, ref: ProviderSubscriptionRef) -> None:
        """Allow renewal again after :meth:`cancel`.

        Unlike cancelling, any refusal raises: telling the member that auto-renewal is back on
        when Telegram will not renew would be wrong. Per the Bot API, ``is_canceled=False``
        lets the member re-enable a subscription the bot cancelled; if no renewal arrives,
        core treats it as a missed renewal and offers to pay.
        """
        if ref.payment_method_id is None:
            raise ProviderError(f"subscription {ref.subscription_id} has no Stars charge id")
        try:
            await self.bot.edit_user_star_subscription(
                user_id=ref.member_telegram_id,
                telegram_payment_charge_id=ref.payment_method_id,
                is_canceled=False,
            )
        except TelegramAPIError as exc:
            raise ProviderError(f"editUserStarSubscription failed: {exc}") from exc


# --- update handling ---------------------------------------------------------------------------


async def check_pre_checkout(
    core: Core, *, user_id: int, currency: str, total_amount: int, payload: str
) -> str | None:
    """Decide a pre-checkout query: ``None`` to accept, or the error to show the member.

    Telegram allows several concurrent subscriptions per user, so this is where a second
    payment for an already paid period is stopped: an old invoice link opened again, or a new
    invoice while another subscription is live. Two payments racing past this check are still
    caught when applied, because a first payment names the period of its invoice.
    """
    parsed = InvoicePayload.decode(payload)
    if parsed is None:
        return ERROR_INVALID
    async with core.sessions() as session:
        sub = await session.get(Subscription, parsed.subscription_id)
        if sub is None or sub.member.telegram_id != user_id or sub.provider != PROVIDER_NAME:
            return ERROR_INVALID
        if currency != CURRENCY or sub.currency != CURRENCY or total_amount != sub.price_minor:
            return ERROR_AMOUNT
        if sub.status in (SubStatus.ACTIVE, SubStatus.CANCELED):
            return ERROR_ALREADY_PAID
        if parsed.for_period != sub.periods_paid + 1:
            # An invoice for a period that was settled meanwhile (an old link after expiry).
            return ERROR_STALE
        if await live_subscription(session, sub.member_id, exclude_id=sub.id) is not None:
            return ERROR_HAS_SUBSCRIPTION
    return None


async def answer_pre_checkout(
    core: Core, query: PreCheckoutQuery, *, within: float = PRE_CHECKOUT_TIMEOUT
) -> str | None:
    """Check and answer a pre-checkout query in time; returns the error shown, if any.

    If the check cannot finish (database down or slow), the payment is declined: declining
    moves no money and the member can simply try again.
    """
    error: str | None
    try:
        async with asyncio.timeout(within):
            error = await check_pre_checkout(
                core,
                user_id=query.from_user.id,
                currency=query.currency,
                total_amount=query.total_amount,
                payload=query.invoice_payload,
            )
    except Exception:
        log.exception("pre-checkout check for %r failed", query.invoice_payload)
        error = ERROR_UNAVAILABLE
    await query.answer(ok=error is None, error_message=error)
    return error


class PaymentNotStored(Exception):
    """A Stars payment could not even be stored; the owner has to apply it by hand."""

    def __init__(self, payment: SuccessfulPayment, user_id: int) -> None:
        super().__init__(f"Stars payment {payment.telegram_payment_charge_id} not stored")
        self.payment = payment
        self.user_id = user_id

    def owner_text(self) -> str:
        payment = self.payment
        return (
            "Требует внимания. Платёж Telegram Stars не удалось сохранить (база данных "
            f"недоступна): {payment.total_amount} ⭐ от пользователя {self.user_id}, "
            f"charge id {payment.telegram_payment_charge_id}, счёт {payment.invoice_payload!r}. "
            "Подписка не продлена: продлите её или верните Stars."
        )


async def handle_successful_payment(
    core: Core,
    payment: SuccessfulPayment,
    *,
    user_id: int,
    occurred_at: datetime,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> ApplyResult | None:
    """Store a Stars payment, then apply it and keep Telegram's subscriptions in line.

    Telegram delivers ``successful_payment`` once, and the update is acknowledged before it
    is handled. So the update is stored first, in its own short transaction (retried a few
    times); if applying it fails afterwards, the scheduler applies the stored row later
    (:func:`retry_stored_payments`). Raises :class:`PaymentNotStored` if even storing fails:
    the caller then tells the owner directly, outside the database.

    Returns ``None`` for payments that are not ours, and while a stored payment waits for a
    retry.
    """
    if payment_event(payment, occurred_at=occurred_at) is None:
        log.warning("ignoring successful_payment with payload %r", payment.invoice_payload)
        return None
    row_id = await _store(core, payment, user_id=user_id, paid_at=occurred_at, sleep=sleep)
    try:
        return await apply_stored_payment(core, row_id)
    except Exception as exc:
        log.exception(
            "Stars payment %s stored but not applied yet, the scheduler retries it",
            payment.telegram_payment_charge_id,
        )
        await _record_failure(core, row_id, exc)
        return None


#: Storing a payment update is retried this many times before the owner is alerted.
STORE_ATTEMPTS = 4
#: Retries of a stored payment, with a delay doubling from one minute to an hour.
APPLY_ATTEMPTS = 12
_FIRST_RETRY = timedelta(minutes=1)
_LONGEST_RETRY = timedelta(hours=1)


async def _store(
    core: Core,
    payment: SuccessfulPayment,
    *,
    user_id: int,
    paid_at: datetime,
    sleep: Callable[[float], Awaitable[None]],
) -> int:
    async def work(session: AsyncSession) -> int:
        now = core.clock.now()
        await insert_ignore(
            session,
            StarsPaymentUpdate.__table__,  # type: ignore[arg-type]
            {
                "telegram_payment_charge_id": payment.telegram_payment_charge_id,
                "user_id": user_id,
                "payment": payment.model_dump(mode="json", exclude_none=True),
                "paid_at": paid_at,
                "status": StarsPaymentStatus.PENDING.value,
                "attempts": 0,
                # The handler applies it right away; the scheduler only picks up leftovers.
                "next_attempt_at": now + _FIRST_RETRY,
                "received_at": now,
            },
            ["telegram_payment_charge_id"],
        )
        row_id = await session.scalar(
            select(StarsPaymentUpdate.id).where(
                StarsPaymentUpdate.telegram_payment_charge_id == payment.telegram_payment_charge_id
            )
        )
        assert row_id is not None
        return row_id

    for attempt in range(1, STORE_ATTEMPTS + 1):
        try:
            return await in_transaction(core.sessions, work)
        except Exception:
            if attempt == STORE_ATTEMPTS:
                log.critical(
                    "Stars payment NOT stored: charge %s, payload %r, %s XTR, user %s, expires %s",
                    payment.telegram_payment_charge_id,
                    payment.invoice_payload,
                    payment.total_amount,
                    user_id,
                    payment.subscription_expiration_date,
                    exc_info=True,
                )
                raise PaymentNotStored(payment, user_id) from None
            log.warning("storing Stars payment failed (%s/%s), retrying", attempt, STORE_ATTEMPTS)
            await sleep(0.5 * 2 ** (attempt - 1))
    raise AssertionError("unreachable")  # pragma: no cover


async def apply_stored_payment(core: Core, row_id: int) -> ApplyResult | None:
    """Apply a stored payment update (once: an applied row is a duplicate).

    * A first payment that pays for a period replaces the Telegram subscription stored on the
      member's subscription (paying again after a missed renewal starts a new one); the old
      one is cancelled so it cannot renew later and charge twice.
    * A first payment that cannot be applied (the period was paid meanwhile) is recorded by
      core with an owner alert; its Telegram subscription is cancelled so it does not keep
      charging every month.
    """
    async with core.sessions() as session:
        row = await session.get(StarsPaymentUpdate, row_id)
        assert row is not None
        payment = SuccessfulPayment.model_validate(row.payment)
        user_id, paid_at = row.user_id, row.paid_at
    event = payment_event(payment, occurred_at=paid_at)
    assert event is not None  # checked when it was stored
    assert event.subscription_id is not None
    sub_id = event.subscription_id
    first = event.purpose is PaymentPurpose.CHECKOUT
    previous_charge: str | None = None
    if first:
        async with core.sessions() as session:
            sub = await session.get(Subscription, sub_id)
            if sub is not None and sub.provider == PROVIDER_NAME:
                previous_charge = sub.payment_method_id

    async def work(session: AsyncSession) -> ApplyResult:
        result = await apply_payment_event(session, event, core)
        stored = await session.get(StarsPaymentUpdate, row_id, populate_existing=True)
        assert stored is not None
        stored.status = StarsPaymentStatus.APPLIED
        stored.applied_at = core.clock.now()
        return result

    result = await in_transaction(core.sessions, work)
    log.info(
        "Stars payment %s -> %s (subscription %s)",
        event.provider_payment_id,
        result.outcome.value,
        result.subscription_id,
    )
    if not first or event.payment_method_id is None:
        return result

    stale: str | None = None
    if result.outcome is PaymentOutcome.APPLIED:
        if previous_charge is not None and previous_charge != event.payment_method_id:
            stale = previous_charge
    elif result.outcome is not PaymentOutcome.DUPLICATE:
        stale = event.payment_method_id
    provider = core.providers.get(PROVIDER_NAME)
    if stale is not None:
        if provider is None:
            log.error("cannot cancel Stars subscription %s: provider not registered", stale)
        else:
            try:
                await provider.cancel(ProviderSubscriptionRef(sub_id, user_id, stale))
            except ProviderError:
                log.exception("could not cancel Stars subscription %s of %s", stale, user_id)
    return result


async def _record_failure(core: Core, row_id: int, exc: BaseException) -> None:
    """Note why a stored payment did not apply; give up after :data:`APPLY_ATTEMPTS`."""
    try:
        async with core.sessions() as session, session.begin():
            row = await session.get(StarsPaymentUpdate, row_id, populate_existing=True)
            if row is None or row.status is not StarsPaymentStatus.PENDING:
                return
            row.last_error = f"{type(exc).__name__}: {exc}"[:2000]
            if row.attempts >= APPLY_ATTEMPTS:
                row.status = StarsPaymentStatus.FAILED
                await alert_owner(
                    session,
                    core,
                    key=f"owner:stars_not_applied:{row.telegram_payment_charge_id}",
                    text=f"Платёж Telegram Stars {row.telegram_payment_charge_id} от "
                    f"пользователя {row.user_id} так и не удалось применить "
                    f"({row.last_error[:200]}). Продлите подписку вручную или верните Stars.",
                )
    except Exception:
        # The database is still down; the row stays pending and is retried.
        log.exception("could not record the failure of Stars payment row %s", row_id)


async def retry_stored_payments(core: Core, *, batch_size: int = 50) -> int:
    """Apply stored Stars payments that the handler could not; returns how many were applied."""
    now = core.clock.now()
    async with core.sessions() as session:
        due = (
            await session.execute(
                select(StarsPaymentUpdate.id, StarsPaymentUpdate.attempts)
                .where(
                    StarsPaymentUpdate.status == StarsPaymentStatus.PENDING,
                    StarsPaymentUpdate.next_attempt_at <= now,
                )
                .order_by(StarsPaymentUpdate.next_attempt_at)
                .limit(batch_size)
            )
        ).all()
    applied = 0
    for row_id, attempts in due:
        delay = min(_FIRST_RETRY * 2**attempts, _LONGEST_RETRY)
        async with core.sessions() as session, session.begin():
            # Moving next_attempt_at is the claim: a second worker's update matches nothing.
            claimed = await session.execute(
                update(StarsPaymentUpdate)
                .where(
                    StarsPaymentUpdate.id == row_id,
                    StarsPaymentUpdate.status == StarsPaymentStatus.PENDING,
                    StarsPaymentUpdate.next_attempt_at <= now,
                )
                .values(next_attempt_at=now + delay, attempts=StarsPaymentUpdate.attempts + 1)
            )
        if claimed.rowcount != 1:  # type: ignore[attr-defined]
            continue
        try:
            await apply_stored_payment(core, row_id)
        except Exception as exc:
            log.warning("stored Stars payment row %s still not applied: %s", row_id, exc)
            await _record_failure(core, row_id, exc)
        else:
            applied += 1
    return applied


def create_stars_router(
    core: Core,
    *,
    pre_checkout_timeout: float = PRE_CHECKOUT_TIMEOUT,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Router:
    """Handlers for Stars invoices created by :class:`StarsProvider`.

    Include it in the dispatcher (``dp.include_router(create_stars_router(core))``) and
    register the provider (``core.providers.register(StarsProvider(bot))``). Both handlers only
    react to our invoice payloads.
    """
    router = Router(name="paidclub-stars")

    @router.pre_checkout_query(F.invoice_payload.startswith(PAYLOAD_PREFIX))
    async def on_pre_checkout(query: PreCheckoutQuery) -> None:
        await answer_pre_checkout(core, query, within=pre_checkout_timeout)

    @router.message(F.successful_payment.invoice_payload.startswith(PAYLOAD_PREFIX))
    async def on_successful_payment(message: Message, bot: Bot) -> None:
        payment = message.successful_payment
        if payment is None or message.from_user is None:
            return
        try:
            result = await handle_successful_payment(
                core,
                payment,
                user_id=message.from_user.id,
                occurred_at=message.date.astimezone(UTC),
                sleep=sleep,
            )
        except PaymentNotStored as exc:
            # The database is unreachable, so the outbox is too: tell the owner directly.
            if core.settings.owner_id is not None:
                try:
                    await bot.send_message(chat_id=core.settings.owner_id, text=exc.owner_text())
                except TelegramAPIError:
                    log.exception("could not tell the owner about an unstored Stars payment")
            raise
        if result is not None and result.outcome is PaymentOutcome.NEEDS_REFUND:
            try:
                await message.answer(NOT_APPLIED_NOTICE)
            except TelegramAPIError:
                log.warning("could not tell %s about the unapplied payment", message.from_user.id)

    return router
