"""A member turns auto-renew off while a renewal charge is still on its way.

The charge was sent before the cancel (or was still pending), so it may succeed afterwards. It
pays the period after the last paid one, but auto-renew stays off: no further charge is ever
scheduled, also when the subscription expired in between.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from paidclub import club
from paidclub.billing import ProviderRegistry
from paidclub.billing.base import ChargeResult, RenewalRequest
from paidclub.billing.fake import Decision, FakeProvider
from paidclub.clock import FakeClock
from paidclub.config import Settings
from paidclub.container import Core, build_core
from paidclub.domain.subscriptions import ExpireReason, SubStatus, renewal_idempotence_key
from paidclub.models import PaymentOutcome, SubscriptionEvent
from paidclub.money import Money
from paidclub.payments import apply_event
from paidclub.scheduler import Scheduler, TickReport
from paidclub.stats import club_report
from tests.helpers import FakeRevoker, load_sub, notifications, payments, subscribe

FEB_28 = datetime(2026, 2, 28, 9, 0, tzinfo=UTC)
MAR_31 = datetime(2026, 3, 31, 9, 0, tzinfo=UTC)
DAY = timedelta(days=1)
MEMBER = 901

CoreFactory = Callable[[FakeProvider], Awaitable[Core]]


@pytest.fixture
async def make_core(settings: Settings, clock: FakeClock) -> AsyncIterator[CoreFactory]:
    made: list[Core] = []

    async def make(provider: FakeProvider) -> Core:
        instance = await build_core(settings, clock=clock, providers=ProviderRegistry([provider]))
        made.append(instance)
        return instance

    yield make
    for instance in made:
        await instance.aclose()


async def tick(core: Core) -> TickReport:
    return await Scheduler(core, FakeRevoker()).tick()


def renewal(sub_id: int, *, for_period: int, attempt: int, method: str | None) -> RenewalRequest:
    return RenewalRequest(
        subscription_id=sub_id,
        member_telegram_id=MEMBER,
        plan_code="month",
        amount=Money(99000, "RUB"),
        for_period=for_period,
        attempt=attempt,
        payment_method_id=method,
        idempotence_key=renewal_idempotence_key(sub_id, for_period, attempt),
        description="",
    )


async def approve_late(
    core: Core, provider: FakeProvider, sub_id: int, *, for_period: int, attempt: int
) -> PaymentOutcome:
    method = (await load_sub(core, sub_id)).payment_method_id
    webhook = provider.renewal_webhook(
        renewal(sub_id, for_period=for_period, attempt=attempt, method=method), succeeded=True
    )
    [event] = await provider.parse_webhook(*webhook)
    return (await apply_event(core, event)).outcome


async def test_a_retry_approved_after_cancel_pays_the_period_and_charges_nothing_more(
    make_core: CoreFactory, clock: FakeClock
) -> None:
    """Declined at the period end; the retry a day later comes back pending; the member
    cancels (past_due -> expired); then the bank approves the retry."""

    def decide(request: RenewalRequest) -> Decision:
        if request.for_period == 2:
            return "failed" if request.attempt == 0 else "pending"
        return "succeeded"

    provider = FakeProvider(
        secret="test-secret", base_url="http://localhost:8000", clock=clock, decide=decide
    )
    core = await make_core(provider)
    sub_id = await subscribe(core, provider, MEMBER)

    clock.set(FEB_28)
    assert (await tick(core)).charge_failed == 1
    clock.set(FEB_28 + DAY)
    assert (await tick(core)).pending == 1
    await club.cancel(core, telegram_id=MEMBER, subscription_id=sub_id)
    sub = await load_sub(core, sub_id)
    assert (sub.status, sub.expire_reason) == (SubStatus.EXPIRED, ExpireReason.CANCELED)

    assert await approve_late(core, provider, sub_id, for_period=2, attempt=1) is (
        PaymentOutcome.APPLIED
    )

    sub = await load_sub(core, sub_id)
    assert (sub.status, sub.next_charge_at) == (SubStatus.CANCELED, None)
    assert (sub.current_period_start, sub.current_period_end) == (FEB_28, MAR_31)
    assert sub.access_revoked_at is None  # removed again after the next expiry
    [message] = await notifications(core, "renewed")
    assert "Автопродление остаётся выключенным" in message.text

    clock.set(MAR_31)
    assert (await tick(core)).expired == 1
    assert renewal_idempotence_key(sub_id, 3, 0) not in provider.renewal_calls
    assert (await load_sub(core, sub_id)).status is SubStatus.EXPIRED


class CancelDuringCharge(FakeProvider):
    """The member presses "cancel" while our renewal request is still in flight."""

    def __init__(self, *, telegram_id: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.core: Core | None = None
        self.telegram_id = telegram_id

    async def charge_renewal(self, request: RenewalRequest) -> ChargeResult:
        if request.for_period == 2 and request.attempt == 1:
            assert self.core is not None
            await club.cancel(
                self.core, telegram_id=self.telegram_id, subscription_id=request.subscription_id
            )
        return await super().charge_renewal(request)


async def test_a_retry_that_succeeds_while_the_member_cancels_keeps_auto_renew_off(
    make_core: CoreFactory, clock: FakeClock
) -> None:
    provider = CancelDuringCharge(
        telegram_id=MEMBER,
        secret="test-secret",
        base_url="http://localhost:8000",
        clock=clock,
        decide=lambda r: "failed" if (r.for_period, r.attempt) == (2, 0) else "succeeded",
    )
    core = await make_core(provider)
    provider.core = core
    sub_id = await subscribe(core, provider, MEMBER)

    clock.set(FEB_28)
    await tick(core)
    clock.set(FEB_28 + DAY)
    assert (await tick(core)).renewed == 1

    async with core.sessions() as session:
        log = [
            (event.from_status, event.to_status, event.reason)
            for event in await session.scalars(
                select(SubscriptionEvent)
                .where(SubscriptionEvent.subscription_id == sub_id)
                .order_by(SubscriptionEvent.id)
            )
        ]
    assert log[-2:] == [
        (SubStatus.PAST_DUE, SubStatus.EXPIRED, "canceled"),
        (SubStatus.EXPIRED, SubStatus.CANCELED, None),
    ]
    sub = await load_sub(core, sub_id)
    assert (sub.status, sub.next_charge_at, sub.current_period_end) == (
        SubStatus.CANCELED,
        None,
        MAR_31,
    )


async def test_a_pending_regular_renewal_approved_after_cancel_and_expiry_keeps_the_schedule(
    make_core: CoreFactory, clock: FakeClock
) -> None:
    """The renewal at the period end is pending, the member cancels, the next tick expires
    the canceled subscription, then the bank approves."""
    provider = FakeProvider(
        secret="test-secret",
        base_url="http://localhost:8000",
        clock=clock,
        decide=lambda request: "pending" if request.for_period == 2 else "succeeded",
    )
    core = await make_core(provider)
    sub_id = await subscribe(core, provider, MEMBER)

    clock.set(FEB_28)
    assert (await tick(core)).pending == 1
    clock.advance(timedelta(minutes=5))
    await club.cancel(core, telegram_id=MEMBER, subscription_id=sub_id)
    clock.advance(timedelta(minutes=1))
    assert (await tick(core)).expired == 1

    assert await approve_late(core, provider, sub_id, for_period=2, attempt=0) is (
        PaymentOutcome.APPLIED
    )

    sub = await load_sub(core, sub_id)
    assert (sub.status, sub.next_charge_at) == (SubStatus.CANCELED, None)
    # The period follows the billing anchor, not the moment of the approval.
    assert (sub.current_period_start, sub.current_period_end) == (FEB_28, MAR_31)

    report = await club_report(core, days=60)
    assert (report.totals.cancellations, report.totals.reactivated) == (1, 1)
    assert report.totals.renewals == 1


async def test_an_approval_that_arrives_after_the_paid_period_is_over_needs_a_refund(
    make_core: CoreFactory, clock: FakeClock
) -> None:
    provider = FakeProvider(
        secret="test-secret",
        base_url="http://localhost:8000",
        clock=clock,
        decide=lambda request: "pending" if request.for_period == 2 else "succeeded",
    )
    core = await make_core(provider)
    sub_id = await subscribe(core, provider, MEMBER)
    clock.set(FEB_28)
    await tick(core)
    await club.cancel(core, telegram_id=MEMBER, subscription_id=sub_id)
    clock.advance(timedelta(minutes=1))
    await tick(core)

    clock.set(MAR_31 + DAY)
    outcome = await approve_late(core, provider, sub_id, for_period=2, attempt=0)

    assert outcome is PaymentOutcome.NEEDS_REFUND
    assert (await load_sub(core, sub_id)).status is SubStatus.EXPIRED
    assert [p.applied for p in await payments(core)] == [True, False]
    [alert] = await notifications(core, "owner_alert")
    assert "после отмены автопродления" in alert.text
