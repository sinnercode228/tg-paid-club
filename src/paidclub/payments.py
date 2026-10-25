"""Applying payment events: the one place where money changes a subscription.

Every source of payment facts calls :func:`apply_event` (or :func:`apply_payment_event` inside
a transaction it already owns): provider webhooks, the scheduler's renewal charges, the
Telegram Stars ``successful_payment`` handler. The rules, in order:

1. **Once per provider payment.** ``(provider, provider_payment_id)`` is unique. A repeated
   webhook or update is answered with ``DUPLICATE`` and changes nothing. The single exception
   is a payment recorded as failed that later reports success: money moved, success wins.
2. **Once per period.** A success pays for exactly one period, ``periods_paid + 1`` (or the
   period the provider metadata names). A second payment for a period that is already paid is
   recorded as ``NEEDS_REFUND`` and the owner is alerted; the period is not extended twice.
3. **The amount must match** the price snapshotted on the subscription.
4. **Failures only count when they are the current dunning attempt** of a merchant-initiated
   renewal. A member's failed attempt on a checkout page never burns a retry.

State changes, the payment row and the resulting messages are written in one transaction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from paidclub import texts
from paidclub.billing.base import BillingEvent, PaymentFailed, PaymentPurpose, PaymentSucceeded
from paidclub.container import Core
from paidclub.db import in_transaction
from paidclub.domain import subscriptions as domain
from paidclub.domain.subscriptions import RenewalMode, SubStatus
from paidclub.models import (
    ChargeAttempt,
    ChargeAttemptStatus,
    Payment,
    PaymentOutcome,
    PaymentStatus,
    Subscription,
)
from paidclub.notifications import enqueue
from paidclub.repo import invite_links, live_subscription, price_of, state_of, write_state

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ApplyResult:
    outcome: PaymentOutcome
    subscription_id: int | None
    status: SubStatus | None = None
    period_end: datetime | None = None
    payment_id: int | None = None


async def apply_event(core: Core, event: BillingEvent) -> ApplyResult:
    """Apply one event in its own transaction, retrying if a concurrent writer won a race.

    Raises :class:`paidclub.db.ConcurrentUpdateError` if it keeps losing; a webhook route
    should then answer 5xx so the provider delivers the notification again.
    """

    async def work(session: AsyncSession) -> ApplyResult:
        return await apply_payment_event(session, event, core)

    result = await in_transaction(core.sessions, work)
    log.info(
        "payment %s/%s -> %s (subscription %s, %s)",
        event.provider,
        event.provider_payment_id,
        result.outcome.value,
        result.subscription_id,
        result.status.value if result.status else "-",
    )
    return result


async def apply_payment_event(
    session: AsyncSession, event: BillingEvent, core: Core
) -> ApplyResult:
    """Apply ``event`` inside the caller's transaction. See the module docstring for the rules."""
    now = core.clock.now()
    succeeded = isinstance(event, PaymentSucceeded)

    existing = (
        await session.scalars(
            select(Payment).where(
                Payment.provider == event.provider,
                Payment.provider_payment_id == event.provider_payment_id,
            )
        )
    ).one_or_none()
    if existing is not None and (existing.status is PaymentStatus.SUCCEEDED or not succeeded):
        return ApplyResult(
            PaymentOutcome.DUPLICATE, existing.subscription_id, payment_id=existing.id
        )

    if existing is not None:
        payment = existing
        payment.status = PaymentStatus.SUCCEEDED
        payment.failure_reason = None
        payment.applied = False
    else:
        payment = Payment(
            provider=event.provider,
            provider_payment_id=event.provider_payment_id,
            purpose=event.purpose,
            status=PaymentStatus.SUCCEEDED if succeeded else PaymentStatus.FAILED,
            outcome=PaymentOutcome.RECORDED,
            applied=False,
            amount_minor=event.amount.minor,
            currency=event.amount.currency,
            for_period=event.for_period,
            attempt=event.attempt,
            failure_reason=event.reason if isinstance(event, PaymentFailed) else None,
            occurred_at=event.occurred_at,
            created_at=now,
        )
        session.add(payment)

    if isinstance(event, PaymentFailed) and event.owner_note:
        # The shop's own problem (credentials, permissions): once a day is enough.
        await alert_owner(
            session,
            core,
            key=f"owner:provider:{event.provider}:{event.reason}:{now:%Y-%m-%d}",
            text=f"Провайдер {event.provider} отклонил списание ({event.reason}). "
            f"{event.owner_note} Пока это не исправлено, продления не проходят.",
        )

    sub = None
    if event.subscription_id is not None:
        sub = await session.get(Subscription, event.subscription_id, populate_existing=True)
    if sub is None:
        payment.outcome = PaymentOutcome.UNKNOWN_SUBSCRIPTION
        await session.flush()
        if succeeded:
            await alert_owner(
                session,
                core,
                key=f"owner:unknown_sub:{event.provider}:{event.provider_payment_id}",
                text=f"Платёж {event.provider} {event.provider_payment_id} на "
                f"{event.amount.human()} ссылается на неизвестную подписку "
                f"({event.subscription_id}).",
            )
        return ApplyResult(PaymentOutcome.UNKNOWN_SUBSCRIPTION, None, payment_id=payment.id)

    payment.subscription_id = sub.id
    payment.member_id = sub.member_id
    payment.plan_code = sub.plan_code

    if isinstance(event, PaymentSucceeded):
        outcome = await _apply_success(session, core, sub, event, payment, now)
    else:
        outcome = await _apply_failure(session, core, sub, event, payment, now)

    payment.outcome = outcome
    payment.applied = outcome is PaymentOutcome.APPLIED
    await session.flush()
    await session.execute(
        update(ChargeAttempt)
        .where(
            ChargeAttempt.subscription_id == sub.id,
            ChargeAttempt.provider_payment_id == event.provider_payment_id,
        )
        .values(
            status=ChargeAttemptStatus.SUCCEEDED if succeeded else ChargeAttemptStatus.FAILED,
            updated_at=now,
        )
    )
    return ApplyResult(
        outcome, sub.id, status=sub.status, period_end=sub.current_period_end, payment_id=payment.id
    )


async def _apply_success(
    session: AsyncSession,
    core: Core,
    sub: Subscription,
    event: PaymentSucceeded,
    payment: Payment,
    now: datetime,
) -> PaymentOutcome:
    expected = price_of(sub)
    ref = f"{event.provider} {event.provider_payment_id}"
    if event.amount != expected:
        await alert_owner(
            session,
            core,
            key=f"owner:amount:{event.provider}:{event.provider_payment_id}",
            text=f"Платёж {ref}: пришло {event.amount.human()}, ожидалось {expected.human()}. "
            f"Подписка #{sub.id} не продлена.",
        )
        return PaymentOutcome.AMOUNT_MISMATCH

    next_period = sub.periods_paid + 1
    problem: str | None = None
    if event.for_period is not None and event.for_period != next_period:
        # Checked first, also for provider-authoritative periods: a Stars first payment names
        # the period its invoice was created for, so a second Telegram subscription bought
        # from the same invoice is a repeat even though its end is a little later.
        problem = (
            "этот период уже оплачен"
            if event.for_period < next_period
            else f"платёж за период {event.for_period}, а оплачено {sub.periods_paid}"
        )
    elif (
        # Provider-authoritative period (Stars): an end we already have is a repeat.
        event.period_end is not None
        and sub.current_period_end is not None
        and sub.status is not SubStatus.EXPIRED
        and event.period_end <= sub.current_period_end
    ):
        problem = "этот период уже оплачен"
    if problem is None and sub.status in (SubStatus.PENDING, SubStatus.EXPIRED):
        other = await live_subscription(session, sub.member_id, exclude_id=sub.id)
        if other is not None:
            problem = f"у участника уже есть действующая подписка #{other.id}"
    if problem is not None:
        payment.for_period = event.for_period
        await alert_owner(
            session,
            core,
            key=f"owner:refund:{event.provider}:{event.provider_payment_id}",
            text=f"Платёж {ref} на {event.amount.human()} не к чему применить: {problem}. "
            f"Подписка #{sub.id}, участник {sub.member.telegram_id}. "
            "Проверьте и при необходимости верните деньги.",
        )
        return PaymentOutcome.NEEDS_REFUND

    before = sub.status
    mode = core.providers.renewal_mode(sub.provider)
    state = domain.payment_succeeded(
        state_of(sub),
        now=now,
        policy=core.policy,
        mode=mode,
        period_end=event.period_end,
        renewal=event.purpose is PaymentPurpose.RENEWAL,
    )
    assert state.current_period_end is not None
    if before is SubStatus.EXPIRED and state.current_period_end <= now:
        # A renewal charge from before a cancel that settled only after the period it pays
        # for was over: there is no time left to give the member.
        payment.for_period = state.periods_paid
        await alert_owner(
            session,
            core,
            key=f"owner:refund:{event.provider}:{event.provider_payment_id}",
            text=f"Платёж {ref} на {event.amount.human()} пришёл после отмены автопродления "
            f"и после конца периода, который он оплачивает. Подписка #{sub.id}, участник "
            f"{sub.member.telegram_id}. Проверьте и при необходимости верните деньги.",
        )
        return PaymentOutcome.NEEDS_REFUND
    write_state(sub, state, now=now)
    if before is SubStatus.EXPIRED:
        # Removal after the next expiry must run again.
        sub.access_revoked_at = None
    if event.payment_method_id:
        sub.payment_method_id = event.payment_method_id
    payment.for_period = state.periods_paid
    payment.period_start = state.current_period_start
    payment.period_end = state.current_period_end

    plan = core.settings.plan(sub.plan_code)
    plan_title = plan.title if plan else sub.plan_code
    if before is SubStatus.EXPIRED and state.status is SubStatus.CANCELED:
        message = texts.renewed_after_cancel(
            plan_title=plan_title,
            period_end=state.current_period_end,
            invite_links=await invite_links(session),
            tz=core.tz,
        )
        kind, key = "renewed", f"renewed:{sub.id}:{state.periods_paid}"
    elif before in (SubStatus.PENDING, SubStatus.EXPIRED):
        message = texts.activated(
            plan_title=plan_title,
            period_end=state.current_period_end,
            invite_links=await invite_links(session),
            tz=core.tz,
            reactivated=before is SubStatus.EXPIRED,
        )
        kind, key = "activated", f"activated:{sub.id}:{state.periods_paid}"
    else:
        message = texts.renewed(
            plan_title=plan_title,
            amount=event.amount,
            period_end=state.current_period_end,
            tz=core.tz,
        )
        kind, key = "renewed", f"renewed:{sub.id}:{state.periods_paid}"
    await enqueue(
        session,
        key=key,
        kind=kind,
        chat_id=sub.member.telegram_id,
        member_id=sub.member_id,
        message=message,
        now=now,
    )
    return PaymentOutcome.APPLIED


async def _apply_failure(
    session: AsyncSession,
    core: Core,
    sub: Subscription,
    event: PaymentFailed,
    payment: Payment,
    now: datetime,
) -> PaymentOutcome:
    if event.purpose is PaymentPurpose.CHECKOUT:
        return PaymentOutcome.RECORDED
    for_period = event.for_period if event.for_period is not None else sub.periods_paid + 1
    payment.for_period = for_period
    current = (
        sub.status in (SubStatus.ACTIVE, SubStatus.PAST_DUE)
        and for_period == sub.periods_paid + 1
        and (event.attempt is None or event.attempt == sub.dunning_attempt)
    )
    if not current:
        return PaymentOutcome.STALE

    attempt = sub.dunning_attempt
    mode = core.providers.renewal_mode(sub.provider)
    state = domain.charge_failed(
        state_of(sub), now=now, policy=core.policy, retries=mode is RenewalMode.MERCHANT
    )
    write_state(sub, state, now=now)
    await notify_renewal_failed(session, core, sub, attempt=attempt, now=now)
    return PaymentOutcome.APPLIED


async def notify_renewal_failed(
    session: AsyncSession, core: Core, sub: Subscription, *, attempt: int, now: datetime
) -> None:
    """Enqueue the "renewal failed" message for a subscription that just went past due."""
    assert sub.grace_until is not None
    if core.providers.renewal_mode(sub.provider) is RenewalMode.PROVIDER:
        message = texts.stars_renewal_missing(
            subscription_id=sub.id, amount=price_of(sub), grace_until=sub.grace_until, tz=core.tz
        )
    else:
        message = texts.payment_failed(
            subscription_id=sub.id,
            amount=price_of(sub),
            grace_until=sub.grace_until,
            next_retry_at=sub.next_charge_at,
            tz=core.tz,
        )
    await enqueue(
        session,
        key=f"payment_failed:{sub.id}:{sub.periods_paid + 1}:{attempt}",
        kind="payment_failed",
        chat_id=sub.member.telegram_id,
        member_id=sub.member_id,
        message=message,
        now=now,
        not_after=sub.grace_until,
    )


async def alert_owner(session: AsyncSession, core: Core, *, key: str, text: str) -> None:
    """Queue a message to the owner in the caller's transaction (logged in any case)."""
    log.warning("owner alert: %s", text)
    owner_id = core.settings.owner_id
    if owner_id is None:
        return
    await enqueue(
        session,
        key=key,
        kind="owner_alert",
        chat_id=owner_id,
        message=texts.owner_alert(text),
        now=core.clock.now(),
    )
