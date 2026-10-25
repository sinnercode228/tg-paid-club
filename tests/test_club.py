from __future__ import annotations

from datetime import timedelta

import pytest

from paidclub import club
from paidclub.billing.base import PaymentPurpose, PaymentSucceeded
from paidclub.billing.fake import FakeProvider
from paidclub.clock import FakeClock
from paidclub.container import Core
from paidclub.domain.subscriptions import SubStatus
from paidclub.money import Money
from paidclub.payments import apply_event
from tests.helpers import StubStarsProvider, load_sub, subscribe

MEMBER = 601


async def test_an_abandoned_checkout_is_reused_for_the_same_plan(core: Core) -> None:
    first = await club.start_checkout(core, telegram_id=MEMBER, plan_code="month", provider="fake")
    again = await club.start_checkout(core, telegram_id=MEMBER, plan_code="month", provider="fake")
    other = await club.start_checkout(core, telegram_id=MEMBER, plan_code="year", provider="fake")
    assert again.subscription_id == first.subscription_id
    assert other.subscription_id != first.subscription_id
    assert first.amount == Money(99000, "RUB")
    assert other.amount == Money(890000, "RUB")
    assert first.url.startswith("http://localhost:8000/fake-checkout/")


async def test_the_price_is_snapshotted_on_the_subscription(core: Core) -> None:
    link = await club.start_checkout(
        core, telegram_id=MEMBER, plan_code="quarter", provider="fake", username="ann"
    )
    sub = await load_sub(core, link.subscription_id)
    assert (sub.period_unit, sub.period_count) == ("month", 3)
    assert (sub.price_minor, sub.currency) == (249000, "RUB")
    assert sub.member.username == "ann"


async def test_a_member_with_a_live_subscription_cannot_start_another(
    core: Core, fake: FakeProvider
) -> None:
    sub_id = await subscribe(core, fake, MEMBER)
    with pytest.raises(club.AlreadySubscribed) as info:
        await club.start_checkout(core, telegram_id=MEMBER, plan_code="year", provider="fake")
    assert info.value.subscription_id == sub_id
    assert info.value.status is SubStatus.ACTIVE


async def test_unknown_plans_and_unavailable_providers_are_refused(core: Core) -> None:
    with pytest.raises(club.PlanNotFound):
        await club.start_checkout(core, telegram_id=MEMBER, plan_code="lifetime", provider="fake")
    with pytest.raises(club.ProviderUnavailable):
        await club.start_checkout(core, telegram_id=MEMBER, plan_code="month", provider="stars")
    core.providers.register(StubStarsProvider())
    # Stars only sell 30-day periods; the quarter plan has no Stars price.
    with pytest.raises(club.ProviderUnavailable):
        await club.start_checkout(core, telegram_id=MEMBER, plan_code="quarter", provider="stars")


async def test_cancel_and_resume_auto_renew(
    core: Core, fake: FakeProvider, clock: FakeClock
) -> None:
    sub_id = await subscribe(core, fake, MEMBER)
    clock.advance(timedelta(days=3))

    view = await club.cancel(core, telegram_id=MEMBER, subscription_id=sub_id)
    assert view.status is SubStatus.CANCELED
    assert view.next_charge_at is None
    assert view.has_access
    assert fake.canceled == [sub_id]

    view = await club.resume(core, telegram_id=MEMBER, subscription_id=sub_id)
    assert view.status is SubStatus.ACTIVE
    assert view.next_charge_at == view.period_end
    assert fake.resumed == [sub_id]


async def test_stars_cancellation_is_sent_to_the_provider_first(
    core: Core, clock: FakeClock
) -> None:
    stars = StubStarsProvider()
    core.providers.register(stars)
    link = await club.start_checkout(core, telegram_id=MEMBER, plan_code="month", provider="stars")
    await apply_event(
        core,
        PaymentSucceeded(
            provider="stars",
            provider_payment_id="ch_1",
            subscription_id=link.subscription_id,
            amount=Money(500, "XTR"),
            occurred_at=clock.now(),
            purpose=PaymentPurpose.CHECKOUT,
            period_end=clock.now() + timedelta(days=30),
            payment_method_id="ch_1",
        ),
    )
    await club.cancel(core, telegram_id=MEMBER, subscription_id=link.subscription_id)
    [ref] = stars.canceled
    assert ref.payment_method_id == "ch_1"
    assert ref.member_telegram_id == MEMBER


async def test_members_can_only_touch_their_own_subscription(
    core: Core, fake: FakeProvider
) -> None:
    sub_id = await subscribe(core, fake, MEMBER)
    with pytest.raises(club.SubscriptionNotFound):
        await club.cancel(core, telegram_id=MEMBER + 1, subscription_id=sub_id)
    with pytest.raises(club.SubscriptionNotFound):
        await club.start_repayment(core, telegram_id=MEMBER + 1, subscription_id=sub_id)
    assert fake.canceled == []


async def test_actions_that_do_not_fit_the_state_are_refused(
    core: Core, fake: FakeProvider, clock: FakeClock
) -> None:
    sub_id = await subscribe(core, fake, MEMBER)
    await club.cancel(core, telegram_id=MEMBER, subscription_id=sub_id)
    await club.cancel(core, telegram_id=MEMBER, subscription_id=sub_id)  # idempotent
    with pytest.raises(club.ActionNotAllowed):
        await club.start_repayment(core, telegram_id=MEMBER, subscription_id=sub_id)
    clock.advance(timedelta(days=40))  # past the paid period
    with pytest.raises(club.ActionNotAllowed):
        await club.resume(core, telegram_id=MEMBER, subscription_id=sub_id)


async def test_status_describes_the_current_subscription(core: Core, fake: FakeProvider) -> None:
    assert await club.status(core, MEMBER) is None
    await club.start_checkout(core, telegram_id=MEMBER, plan_code="month", provider="fake")
    assert await club.status(core, MEMBER) is None
    sub_id = await subscribe(core, fake, MEMBER)
    view = await club.status(core, MEMBER)
    assert view is not None
    assert view.id == sub_id
    assert view.status is SubStatus.ACTIVE
    assert view.has_saved_method
    assert view.has_access
