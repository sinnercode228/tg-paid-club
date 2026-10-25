"""Member-facing operations the bot calls: start a checkout, pay after a failed renewal,
cancel and resume auto-renew, read the current status.

Errors are :class:`ClubError` subclasses with a ready Russian ``user_message``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paidclub.billing.base import BillingProvider, CheckoutRequest, ProviderSubscriptionRef
from paidclub.container import Core
from paidclub.db import in_transaction
from paidclub.domain import subscriptions as domain
from paidclub.domain.subscriptions import InvalidTransition, SubStatus
from paidclub.models import Subscription
from paidclub.money import Money
from paidclub.repo import (
    latest_subscription,
    live_subscription,
    member_by_telegram_id,
    price_of,
    state_of,
    upsert_member,
    write_state,
)


class ClubError(Exception):
    user_message = "Не получилось выполнить действие. Попробуйте ещё раз."

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail or self.user_message)


class PlanNotFound(ClubError):
    user_message = "Такого тарифа нет."


class ProviderUnavailable(ClubError):
    user_message = "Этот способ оплаты сейчас недоступен для выбранного тарифа."


class AlreadySubscribed(ClubError):
    user_message = "У вас уже есть подписка."

    def __init__(self, subscription_id: int, status: SubStatus) -> None:
        super().__init__(f"subscription {subscription_id} is {status.value}")
        self.subscription_id = subscription_id
        self.status = status


class SubscriptionNotFound(ClubError):
    user_message = "Подписка не найдена."


class ActionNotAllowed(ClubError):
    user_message = "Сейчас это действие для подписки недоступно."


@dataclass(frozen=True, slots=True)
class SubscriptionView:
    id: int
    plan_code: str
    provider: str
    status: SubStatus
    price: Money
    period_end: datetime | None
    grace_until: datetime | None
    next_charge_at: datetime | None
    has_saved_method: bool
    has_access: bool

    @classmethod
    def of(cls, sub: Subscription, now: datetime) -> SubscriptionView:
        return cls(
            id=sub.id,
            plan_code=sub.plan_code,
            provider=sub.provider,
            status=sub.status,
            price=price_of(sub),
            period_end=sub.current_period_end,
            grace_until=sub.grace_until,
            next_charge_at=sub.next_charge_at,
            has_saved_method=sub.payment_method_id is not None,
            has_access=domain.has_access(state_of(sub), now),
        )


@dataclass(frozen=True, slots=True)
class CheckoutLink:
    subscription_id: int
    provider: str
    amount: Money
    url: str


def _provider(core: Core, name: str) -> BillingProvider:
    provider = core.providers.get(name)
    if provider is None:
        raise ProviderUnavailable(f"provider {name!r} is not enabled")
    return provider


async def _owned(session: AsyncSession, telegram_id: int, subscription_id: int) -> Subscription:
    sub = await session.get(Subscription, subscription_id, populate_existing=True)
    if sub is None or sub.member.telegram_id != telegram_id:
        raise SubscriptionNotFound(f"subscription {subscription_id} of {telegram_id}")
    return sub


async def status(core: Core, telegram_id: int) -> SubscriptionView | None:
    async with core.sessions() as session:
        member = await member_by_telegram_id(session, telegram_id)
        if member is None:
            return None
        sub = await latest_subscription(session, member.id)
        if sub is None or sub.status is SubStatus.PENDING:
            return None
        return SubscriptionView.of(sub, core.clock.now())


async def start_checkout(
    core: Core,
    *,
    telegram_id: int,
    plan_code: str,
    provider: str,
    username: str | None = None,
    first_name: str | None = None,
) -> CheckoutLink:
    """Create (or reuse) a pending subscription and a provider checkout for its first period.

    Raises :class:`AlreadySubscribed` if the member has a live subscription; the bot then
    offers "pay" for a past-due one and "resume" for a canceled one instead.
    """
    plan = core.settings.plan(plan_code)
    if plan is None:
        raise PlanNotFound(plan_code)
    billing = _provider(core, provider)
    price = plan.price_for(billing.currency)
    if price is None:
        raise ProviderUnavailable(f"plan {plan_code!r} has no {billing.currency} price")

    async def work(session: AsyncSession) -> int:
        now = core.clock.now()
        member = await upsert_member(
            session, telegram_id=telegram_id, username=username, first_name=first_name, now=now
        )
        live = await live_subscription(session, member.id)
        if live is not None:
            raise AlreadySubscribed(live.id, live.status)
        # Reuse an unpaid checkout for the same plan and price instead of piling up rows.
        pending = (
            await session.scalars(
                select(Subscription)
                .where(
                    Subscription.member_id == member.id,
                    Subscription.status == SubStatus.PENDING,
                    Subscription.plan_code == plan.code,
                    Subscription.provider == billing.name,
                    Subscription.price_minor == price.minor,
                    Subscription.currency == price.currency,
                )
                .order_by(Subscription.id.desc())
                .limit(1)
            )
        ).first()
        if pending is None:
            pending = Subscription(
                member_id=member.id,
                plan_code=plan.code,
                provider=billing.name,
                status=SubStatus.PENDING,
                period_unit=plan.period_unit,
                period_count=plan.period_count,
                price_minor=price.minor,
                currency=price.currency,
                created_at=now,
                updated_at=now,
            )
            session.add(pending)
            await session.flush()
        return pending.id

    sub_id = await in_transaction(core.sessions, work)
    checkout = await billing.start_checkout(
        CheckoutRequest(
            subscription_id=sub_id,
            member_telegram_id=telegram_id,
            plan_code=plan.code,
            plan_title=plan.title,
            amount=price,
            for_period=1,
            description=f"Подписка на клуб: {plan.title}",
            return_url=core.settings.checkout_return_url,
        )
    )
    return CheckoutLink(sub_id, billing.name, price, checkout.url)


async def start_repayment(core: Core, *, telegram_id: int, subscription_id: int) -> CheckoutLink:
    """Checkout for the next unpaid period of an existing subscription: the "pay" button after
    a failed renewal, a renewal without a saved card, or coming back after expiry.

    The period is fixed in the checkout, so if the scheduled retry also succeeds, the second
    payment is recognised as a repeat (``NEEDS_REFUND``) instead of extending twice.
    """

    async def work(session: AsyncSession) -> tuple[str, str, int, Money]:
        sub = await _owned(session, telegram_id, subscription_id)
        if sub.status is SubStatus.CANCELED:
            raise ActionNotAllowed("canceled: resume auto-renew instead")
        if sub.status is SubStatus.EXPIRED:
            other = await live_subscription(session, sub.member_id, exclude_id=sub.id)
            if other is not None:
                raise AlreadySubscribed(other.id, other.status)
        return sub.provider, sub.plan_code, sub.periods_paid + 1, price_of(sub)

    provider_name, plan_code, for_period, price = await in_transaction(core.sessions, work)
    billing = _provider(core, provider_name)
    plan = core.settings.plan(plan_code)
    title = plan.title if plan else plan_code
    checkout = await billing.start_checkout(
        CheckoutRequest(
            subscription_id=subscription_id,
            member_telegram_id=telegram_id,
            plan_code=plan_code,
            plan_title=title,
            amount=price,
            for_period=for_period,
            description=f"Продление подписки на клуб: {title}",
            return_url=core.settings.checkout_return_url,
        )
    )
    return CheckoutLink(subscription_id, billing.name, price, checkout.url)


async def cancel(core: Core, *, telegram_id: int, subscription_id: int) -> SubscriptionView:
    """Turn auto-renew off. The provider is told first (Stars keeps its own subscription), so
    a provider error leaves our state untouched and the member can simply try again."""
    return await _toggle(core, telegram_id, subscription_id, enable=False)


async def resume(core: Core, *, telegram_id: int, subscription_id: int) -> SubscriptionView:
    return await _toggle(core, telegram_id, subscription_id, enable=True)


async def _toggle(
    core: Core, telegram_id: int, subscription_id: int, *, enable: bool
) -> SubscriptionView:
    def transition(sub: Subscription) -> domain.SubState:
        now = core.clock.now()
        state = state_of(sub)
        if enable:
            mode = core.providers.renewal_mode(sub.provider)
            return domain.resume(state, now=now, policy=core.policy, mode=mode)
        return domain.cancel(state, now=now)

    async def check(session: AsyncSession) -> tuple[str, ProviderSubscriptionRef]:
        sub = await _owned(session, telegram_id, subscription_id)
        try:
            transition(sub)
        except InvalidTransition as exc:
            raise ActionNotAllowed(str(exc)) from exc
        return sub.provider, ProviderSubscriptionRef(sub.id, telegram_id, sub.payment_method_id)

    provider_name, ref = await in_transaction(core.sessions, check)
    provider = core.providers.get(provider_name)
    if provider is not None:
        await (provider.resume(ref) if enable else provider.cancel(ref))

    async def apply(session: AsyncSession) -> SubscriptionView:
        sub = await _owned(session, telegram_id, subscription_id)
        try:
            state = transition(sub)
        except InvalidTransition as exc:
            raise ActionNotAllowed(str(exc)) from exc
        write_state(sub, state, now=core.clock.now())
        await session.flush()
        return SubscriptionView.of(sub, core.clock.now())

    return await in_transaction(core.sessions, apply)
