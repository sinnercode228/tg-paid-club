from __future__ import annotations

from datetime import UTC, datetime, timedelta

from paidclub import club
from paidclub.access import decide_join_request
from paidclub.billing.base import PaymentFailed, PaymentPurpose
from paidclub.billing.fake import FakeProvider
from paidclub.clock import FakeClock
from paidclub.container import Core
from paidclub.models import AccessEventKind
from paidclub.money import Money
from paidclub.payments import apply_event
from paidclub.repo import member_by_telegram_id
from tests.helpers import CHAT_ID, OWNER_ID, access_events, subscribe

FEB_28 = datetime(2026, 2, 28, 9, 0, tzinfo=UTC)
MEMBER = 801


async def decide(core: Core, telegram_id: int = MEMBER, chat_id: int = CHAT_ID) -> str:
    return (await decide_join_request(core, chat_id=chat_id, telegram_id=telegram_id)).action


async def test_a_stranger_is_declined_and_remembered(core: Core) -> None:
    decision = await decide_join_request(
        core, chat_id=CHAT_ID, telegram_id=MEMBER, username="guest", first_name="Гость"
    )
    assert decision.action == "decline"
    async with core.sessions() as session:
        member = await member_by_telegram_id(session, MEMBER)
        assert member is not None
        assert member.username == "guest"
    [event] = await access_events(core)
    assert event.kind is AccessEventKind.JOIN_DECLINED
    assert event.chat_id == CHAT_ID


async def test_an_active_member_is_approved(core: Core, fake: FakeProvider) -> None:
    sub_id = await subscribe(core, fake, MEMBER)
    decision = await decide_join_request(core, chat_id=CHAT_ID, telegram_id=MEMBER)
    assert (decision.action, decision.subscription_id) == ("approve", sub_id)
    assert (await access_events(core))[-1].kind is AccessEventKind.JOIN_APPROVED


async def test_requests_to_other_chats_are_ignored(core: Core, fake: FakeProvider) -> None:
    await subscribe(core, fake, MEMBER)
    assert await decide(core, chat_id=-100999) == "ignore"
    assert await access_events(core) == []


async def test_a_pending_checkout_does_not_grant_access(core: Core) -> None:
    await club.start_checkout(core, telegram_id=MEMBER, plan_code="month", provider="fake")
    assert await decide(core) == "decline"


async def test_a_canceled_member_gets_in_until_the_paid_period_ends(
    core: Core, fake: FakeProvider, clock: FakeClock
) -> None:
    sub_id = await subscribe(core, fake, MEMBER)
    await club.cancel(core, telegram_id=MEMBER, subscription_id=sub_id)
    clock.set(FEB_28 - timedelta(seconds=1))
    assert await decide(core) == "approve"
    clock.set(FEB_28)
    assert await decide(core) == "decline"


async def test_a_past_due_member_gets_in_only_during_grace(
    core: Core, fake: FakeProvider, clock: FakeClock
) -> None:
    sub_id = await subscribe(core, fake, MEMBER)
    clock.set(FEB_28)
    failure = PaymentFailed(
        provider="fake",
        provider_payment_id="r1",
        subscription_id=sub_id,
        amount=Money(99000, "RUB"),
        occurred_at=FEB_28,
        purpose=PaymentPurpose.RENEWAL,
        reason="card_declined",
        for_period=2,
        attempt=0,
    )
    await apply_event(core, failure)
    clock.set(FEB_28 + timedelta(days=3) - timedelta(seconds=1))
    assert await decide(core) == "approve"
    # Grace is over even if the scheduler has not expired the subscription yet.
    clock.set(FEB_28 + timedelta(days=3))
    assert await decide(core) == "decline"


async def test_the_owner_is_always_approved(core: Core) -> None:
    assert await decide(core, telegram_id=OWNER_ID) == "approve"
