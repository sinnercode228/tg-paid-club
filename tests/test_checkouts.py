"""Checkout payments are looked up when their notification does not arrive."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from paidclub import club
from paidclub.billing.fake import FakeProvider
from paidclub.billing.yookassa import YooKassaProvider
from paidclub.checkouts import GIVE_UP_AFTER
from paidclub.clock import FakeClock
from paidclub.container import Core
from paidclub.domain.subscriptions import SubStatus
from paidclub.models import Checkout, PaymentOutcome
from paidclub.payments import apply_event
from paidclub.scheduler import Scheduler
from tests.helpers import FakeRevoker, load_sub, notifications
from tests.test_billing_yookassa import SECRET, SHOP_ID, FakeYooKassa

MEMBER = 1201


@pytest.fixture
def api(clock: FakeClock) -> FakeYooKassa:
    return FakeYooKassa(clock)


@pytest.fixture
async def yookassa(
    core: Core, api: FakeYooKassa, clock: FakeClock
) -> AsyncIterator[YooKassaProvider]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(api.handler))
    provider = YooKassaProvider(
        shop_id=SHOP_ID,
        secret_key=SECRET,
        return_url=core.settings.public_base_url,
        client=client,
        clock=clock,
    )
    core.providers.register(provider)
    yield provider
    await client.aclose()


async def checkouts(core: Core) -> list[Checkout]:
    async with core.sessions() as session:
        return list((await session.scalars(select(Checkout).order_by(Checkout.id))).all())


def lookups(api: FakeYooKassa, payment_id: str) -> int:
    return sum(
        1
        for request in api.requests
        if request.method == "GET" and request.url.path == f"/v3/payments/{payment_id}"
    )


async def start(core: Core) -> tuple[int, str]:
    link = await club.start_checkout(
        core, telegram_id=MEMBER, plan_code="month", provider="yookassa"
    )
    return link.subscription_id, link.url.rsplit("=", 1)[1]


async def test_a_paid_checkout_whose_notification_never_arrives_is_applied_by_a_lookup(
    core: Core, api: FakeYooKassa, yookassa: YooKassaProvider, clock: FakeClock
) -> None:
    sub_id, payment_id = await start(core)
    [row] = await checkouts(core)
    assert (row.provider, row.provider_payment_id, row.for_period) == ("yookassa", payment_id, 1)
    api.pay(payment_id)

    scheduler = Scheduler(core, FakeRevoker())
    clock.advance(timedelta(minutes=4))
    assert (await scheduler.tick()).checkouts_checked == 0
    clock.advance(timedelta(minutes=1))
    report = await scheduler.tick()

    assert (report.checkouts_checked, report.checkouts_recovered) == (1, 1)
    sub = await load_sub(core, sub_id)
    assert sub.status is SubStatus.ACTIVE
    assert sub.payment_method_id == f"pm-{payment_id}"
    assert len(await notifications(core, "activated")) == 1
    [alert] = await notifications(core, "owner_alert")
    assert "http://localhost:8000/webhooks/yookassa" in alert.text
    [row] = await checkouts(core)
    assert (row.next_check_at, row.result) == (None, "applied")

    # A notification that turns up later is a duplicate.
    [event] = await yookassa.parse_webhook({}, api.notification(payment_id))
    assert (await apply_event(core, event)).outcome is PaymentOutcome.DUPLICATE


async def test_a_checkout_settled_by_its_notification_is_not_looked_up(
    core: Core, api: FakeYooKassa, yookassa: YooKassaProvider, clock: FakeClock
) -> None:
    _, payment_id = await start(core)
    api.pay(payment_id)
    [event] = await yookassa.parse_webhook({}, api.notification(payment_id))
    await apply_event(core, event)
    before = lookups(api, payment_id)

    clock.advance(timedelta(hours=3))
    await Scheduler(core, FakeRevoker()).tick()

    assert lookups(api, payment_id) == before
    [row] = await checkouts(core)
    assert (row.next_check_at, row.result, row.checks) == (None, "applied", 0)
    assert await notifications(core, "owner_alert") == []


async def test_an_unpaid_checkout_is_looked_up_on_a_schedule_until_yookassa_cancels_it(
    core: Core, api: FakeYooKassa, yookassa: YooKassaProvider, clock: FakeClock
) -> None:
    sub_id, payment_id = await start(core)
    scheduler = Scheduler(core, FakeRevoker())
    created = clock.now()
    for minutes in (5, 20, 35):
        clock.set(created + timedelta(minutes=minutes))
        assert (await scheduler.tick()).checkouts_checked == 1
    assert lookups(api, payment_id) == 3

    api.decline(payment_id, "expired_on_confirmation")
    clock.set(created + timedelta(minutes=50))
    await scheduler.tick()

    [row] = await checkouts(core)
    assert (row.next_check_at, row.result) == (None, "recorded")
    assert (await load_sub(core, sub_id)).status is SubStatus.PENDING
    assert await notifications(core, "owner_alert") == []


async def test_lookups_stop_after_two_days_with_one_last_look(
    core: Core, api: FakeYooKassa, yookassa: YooKassaProvider, clock: FakeClock
) -> None:
    _, payment_id = await start(core)
    scheduler = Scheduler(core, FakeRevoker())
    clock.advance(GIVE_UP_AFTER)  # the scheduler was not running meanwhile
    await scheduler.tick()
    clock.advance(timedelta(days=1))
    await scheduler.tick()
    [row] = await checkouts(core)
    assert (row.next_check_at, row.result) == (None, "abandoned")
    assert lookups(api, payment_id) == 1


async def test_fake_checkouts_are_left_to_the_payment_page(
    core: Core, fake: FakeProvider, clock: FakeClock
) -> None:
    await club.start_checkout(core, telegram_id=MEMBER, plan_code="month", provider="fake")
    clock.advance(timedelta(minutes=5))
    await Scheduler(core, FakeRevoker()).tick()
    [row] = await checkouts(core)
    assert (row.next_check_at, row.result) == (None, "untracked")
    assert fake.checks == [row.provider_payment_id]
