from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from paidclub import club
from paidclub.billing.base import (
    PaymentFailed,
    PaymentPurpose,
    PaymentSucceeded,
    WebhookRejected,
)
from paidclub.billing.fake import FakeProvider
from paidclub.clock import FakeClock
from paidclub.container import Core
from paidclub.domain.subscriptions import SubStatus
from paidclub.models import PaymentOutcome, PaymentStatus
from paidclub.money import Money
from paidclub.payments import apply_event
from tests.conftest import START
from tests.helpers import (
    OWNER_ID,
    StubStarsProvider,
    add_invite_link,
    checkout_event,
    load_sub,
    notifications,
    payments,
    subscribe,
)

FEB_28 = datetime(2026, 2, 28, 9, 0, tzinfo=UTC)
MAR_31 = datetime(2026, 3, 31, 9, 0, tzinfo=UTC)
MEMBER = 501


def renewal_failure(
    sub_id: int, *, payment_id: str, for_period: int, attempt: int
) -> PaymentFailed:
    return PaymentFailed(
        provider="fake",
        provider_payment_id=payment_id,
        subscription_id=sub_id,
        amount=Money(99000, "RUB"),
        occurred_at=START,
        purpose=PaymentPurpose.RENEWAL,
        reason="card_declined",
        for_period=for_period,
        attempt=attempt,
    )


async def test_first_payment_activates_and_sends_the_join_link(
    core: Core, fake: FakeProvider
) -> None:
    await add_invite_link(core)
    sub_id = await subscribe(core, fake, MEMBER)

    sub = await load_sub(core, sub_id)
    assert sub.status is SubStatus.ACTIVE
    assert sub.periods_paid == 1
    assert sub.current_period_start == START
    assert sub.current_period_end == FEB_28
    assert sub.next_charge_at == FEB_28
    assert sub.payment_method_id is not None
    assert sub.payment_method_id.startswith("fake_pm_ok_")

    [payment] = await payments(core)
    assert payment.outcome is PaymentOutcome.APPLIED
    assert payment.for_period == 1
    assert payment.period_end == FEB_28
    assert payment.plan_code == "month"

    [message] = await notifications(core, "activated")
    assert message.chat_id == MEMBER
    assert "активна до 28.02.2026" in message.text
    assert message.buttons == [{"text": "Вступить в клуб", "url": "https://t.me/+club"}]


async def test_the_same_webhook_twice_extends_nothing(core: Core, fake: FakeProvider) -> None:
    event = await checkout_event(core, fake, MEMBER)
    first = await apply_event(core, event)
    second = await apply_event(core, event)

    assert first.outcome is PaymentOutcome.APPLIED
    assert second.outcome is PaymentOutcome.DUPLICATE
    sub = await load_sub(core, event.subscription_id or 0)
    assert sub.periods_paid == 1
    assert sub.current_period_end == FEB_28
    assert len(await payments(core)) == 1
    assert len(await notifications(core, "activated")) == 1


async def test_the_same_webhook_delivered_concurrently_is_applied_once(
    core: Core, fake: FakeProvider
) -> None:
    event = await checkout_event(core, fake, MEMBER)
    results = await asyncio.gather(*(apply_event(core, event) for _ in range(3)))

    assert sorted(result.outcome.value for result in results) == [
        "applied",
        "duplicate",
        "duplicate",
    ]
    sub = await load_sub(core, event.subscription_id or 0)
    assert sub.periods_paid == 1
    assert len(await payments(core)) == 1


async def test_a_second_payment_for_the_same_period_is_flagged_for_refund(
    core: Core, fake: FakeProvider
) -> None:
    # The member opened the checkout twice and paid both times.
    first = await checkout_event(core, fake, MEMBER)
    second = await checkout_event(core, fake, MEMBER)
    assert first.subscription_id == second.subscription_id
    assert first.provider_payment_id != second.provider_payment_id

    assert (await apply_event(core, first)).outcome is PaymentOutcome.APPLIED
    assert (await apply_event(core, second)).outcome is PaymentOutcome.NEEDS_REFUND

    sub = await load_sub(core, first.subscription_id or 0)
    assert sub.periods_paid == 1
    assert sub.current_period_end == FEB_28
    [alert] = await notifications(core, "owner_alert")
    assert alert.chat_id == OWNER_ID
    assert second.provider_payment_id in alert.text
    assert "уже оплачен" in alert.text


async def test_paying_the_next_period_early_extends_from_the_period_end(
    core: Core, fake: FakeProvider, clock: FakeClock
) -> None:
    sub_id = await subscribe(core, fake, MEMBER)
    clock.advance(timedelta(days=10))
    link = await club.start_repayment(core, telegram_id=MEMBER, subscription_id=sub_id)
    session = fake.read_checkout(link.url.rsplit("/", 1)[1])
    assert session.for_period == 2
    headers, body = fake.checkout_webhook(session, succeeded=True, card="ok")
    [event] = await fake.parse_webhook(headers, body)

    assert (await apply_event(core, event)).outcome is PaymentOutcome.APPLIED
    assert (await apply_event(core, event)).outcome is PaymentOutcome.DUPLICATE
    sub = await load_sub(core, sub_id)
    assert sub.periods_paid == 2
    assert sub.current_period_start == FEB_28
    assert sub.current_period_end == MAR_31
    assert sub.next_charge_at == MAR_31
    [renewed] = await notifications(core, "renewed")
    assert "продлена до 31.03.2026" in renewed.text


async def test_a_wrong_amount_is_not_applied(core: Core, fake: FakeProvider) -> None:
    event = await checkout_event(core, fake, MEMBER)
    cheap = replace(event, amount=Money(100, "RUB"))

    result = await apply_event(core, cheap)
    assert result.outcome is PaymentOutcome.AMOUNT_MISMATCH
    sub = await load_sub(core, event.subscription_id or 0)
    assert sub.status is SubStatus.PENDING
    [alert] = await notifications(core, "owner_alert")
    assert "ожидалось 990 ₽" in alert.text


async def test_a_payment_for_an_unknown_subscription_is_kept_and_reported(
    core: Core, fake: FakeProvider
) -> None:
    event = await checkout_event(core, fake, MEMBER)
    stray = replace(event, subscription_id=999)

    result = await apply_event(core, stray)
    assert result.outcome is PaymentOutcome.UNKNOWN_SUBSCRIPTION
    [payment] = await payments(core)
    assert payment.subscription_id is None
    assert payment.status is PaymentStatus.SUCCEEDED
    assert len(await notifications(core, "owner_alert")) == 1


async def test_a_tampered_webhook_is_rejected(core: Core, fake: FakeProvider) -> None:
    link = await club.start_checkout(core, telegram_id=MEMBER, plan_code="month", provider="fake")
    session = fake.read_checkout(link.url.rsplit("/", 1)[1])
    headers, body = fake.checkout_webhook(session, succeeded=True)

    with pytest.raises(WebhookRejected):
        await fake.parse_webhook(headers, body.replace(b"990.00", b"1.00"))
    with pytest.raises(WebhookRejected):
        await fake.parse_webhook({}, body)
    other = FakeProvider(secret="another-secret", base_url="http://x", clock=core.clock)
    with pytest.raises(WebhookRejected):
        await other.parse_webhook(headers, body)
    # Header names are case-insensitive, as they are on the wire.
    lowered = {key.lower(): value for key, value in headers.items()}
    assert len(await fake.parse_webhook(lowered, body)) == 1


async def test_a_tampered_checkout_token_is_rejected(core: Core, fake: FakeProvider) -> None:
    link = await club.start_checkout(core, telegram_id=MEMBER, plan_code="month", provider="fake")
    token = link.url.rsplit("/", 1)[1]
    payload, signature = token.split(".")
    with pytest.raises(WebhookRejected):
        fake.read_checkout(f"{payload}x.{signature}")
    with pytest.raises(WebhookRejected):
        fake.read_checkout("garbage")


async def test_a_failed_checkout_changes_nothing(core: Core, fake: FakeProvider) -> None:
    link = await club.start_checkout(core, telegram_id=MEMBER, plan_code="month", provider="fake")
    session = fake.read_checkout(link.url.rsplit("/", 1)[1])
    headers, body = fake.checkout_webhook(session, succeeded=False)
    [event] = await fake.parse_webhook(headers, body)

    assert (await apply_event(core, event)).outcome is PaymentOutcome.RECORDED
    sub = await load_sub(core, link.subscription_id)
    assert sub.status is SubStatus.PENDING
    assert await notifications(core) == []


async def test_a_payment_reported_failed_and_then_succeeded_is_applied(
    core: Core, fake: FakeProvider
) -> None:
    link = await club.start_checkout(core, telegram_id=MEMBER, plan_code="month", provider="fake")
    session = fake.read_checkout(link.url.rsplit("/", 1)[1])
    [failed] = await fake.parse_webhook(*fake.checkout_webhook(session, succeeded=False))
    [succeeded] = await fake.parse_webhook(*fake.checkout_webhook(session, succeeded=True))

    assert (await apply_event(core, failed)).outcome is PaymentOutcome.RECORDED
    assert (await apply_event(core, succeeded)).outcome is PaymentOutcome.APPLIED
    assert (await apply_event(core, failed)).outcome is PaymentOutcome.DUPLICATE
    [payment] = await payments(core)
    assert payment.status is PaymentStatus.SUCCEEDED
    assert (await load_sub(core, link.subscription_id)).status is SubStatus.ACTIVE


async def test_only_the_current_renewal_attempt_moves_dunning(
    core: Core, fake: FakeProvider
) -> None:
    sub_id = await subscribe(core, fake, MEMBER)

    first = renewal_failure(sub_id, payment_id="r1", for_period=2, attempt=0)
    assert (await apply_event(core, first)).outcome is PaymentOutcome.APPLIED
    sub = await load_sub(core, sub_id)
    assert sub.status is SubStatus.PAST_DUE
    assert sub.dunning_attempt == 1

    # Another report about attempt 0, under a different payment id: already counted.
    again = renewal_failure(sub_id, payment_id="r1-bis", for_period=2, attempt=0)
    assert (await apply_event(core, again)).outcome is PaymentOutcome.STALE
    # A failure for a period that is already paid.
    old = renewal_failure(sub_id, payment_id="r0", for_period=1, attempt=0)
    assert (await apply_event(core, old)).outcome is PaymentOutcome.STALE

    sub = await load_sub(core, sub_id)
    assert sub.dunning_attempt == 1
    assert len(await notifications(core, "payment_failed")) == 1


async def test_a_failed_attempt_on_the_pay_page_does_not_burn_a_retry(
    core: Core, fake: FakeProvider
) -> None:
    sub_id = await subscribe(core, fake, MEMBER)
    await apply_event(core, renewal_failure(sub_id, payment_id="r1", for_period=2, attempt=0))
    link = await club.start_repayment(core, telegram_id=MEMBER, subscription_id=sub_id)
    session = fake.read_checkout(link.url.rsplit("/", 1)[1])
    [declined] = await fake.parse_webhook(*fake.checkout_webhook(session, succeeded=False))

    assert (await apply_event(core, declined)).outcome is PaymentOutcome.RECORDED
    assert (await load_sub(core, sub_id)).dunning_attempt == 1


async def test_stars_payments_take_the_period_end_from_telegram(
    core: Core, clock: FakeClock
) -> None:
    stars = StubStarsProvider()
    core.providers.register(stars)
    link = await club.start_checkout(core, telegram_id=MEMBER, plan_code="month", provider="stars")
    assert link.amount == Money(500, "XTR")
    first_end = START + timedelta(days=30, minutes=1)

    def stars_payment(charge_id: str, end: datetime, purpose: PaymentPurpose) -> PaymentSucceeded:
        return PaymentSucceeded(
            provider="stars",
            provider_payment_id=charge_id,
            subscription_id=link.subscription_id,
            amount=Money(500, "XTR"),
            occurred_at=clock.now(),
            purpose=purpose,
            period_end=end,
            payment_method_id=charge_id if purpose is PaymentPurpose.CHECKOUT else None,
        )

    first = stars_payment("ch_1", first_end, PaymentPurpose.CHECKOUT)
    assert (await apply_event(core, first)).outcome is PaymentOutcome.APPLIED
    sub = await load_sub(core, link.subscription_id)
    assert sub.current_period_end == first_end
    assert sub.next_charge_at == first_end + timedelta(minutes=60)
    assert sub.payment_method_id == "ch_1"

    clock.set(first_end)
    second_end = first_end + timedelta(days=30)
    renewal = stars_payment("ch_2", second_end, PaymentPurpose.RENEWAL)
    assert (await apply_event(core, renewal)).outcome is PaymentOutcome.APPLIED
    assert (await apply_event(core, renewal)).outcome is PaymentOutcome.DUPLICATE
    repeat = stars_payment("ch_2b", second_end, PaymentPurpose.RENEWAL)
    assert (await apply_event(core, repeat)).outcome is PaymentOutcome.NEEDS_REFUND

    sub = await load_sub(core, link.subscription_id)
    assert sub.periods_paid == 2
    assert sub.current_period_start == first_end
    assert sub.current_period_end == second_end
    assert sub.payment_method_id == "ch_1"
