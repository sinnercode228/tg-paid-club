from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from paidclub import club
from paidclub.access import RevokeOutcome, decide_join_request
from paidclub.billing import ProviderRegistry
from paidclub.billing.base import ChargeResult, PaymentPurpose, PaymentSucceeded, RenewalRequest
from paidclub.billing.fake import Decision, FakeProvider
from paidclub.clock import FakeClock
from paidclub.config import Settings
from paidclub.container import Core, build_core
from paidclub.domain.subscriptions import ExpireReason, SubStatus, renewal_idempotence_key
from paidclub.models import (
    AccessEventKind,
    ChargeAttempt,
    ChargeAttemptStatus,
    PaymentOutcome,
    PaymentStatus,
)
from paidclub.money import Money
from paidclub.payments import apply_event
from paidclub.scheduler import Scheduler, TickReport
from tests.helpers import (
    ADMIN_ID,
    CHAT_ID,
    OWNER_ID,
    FakeRevoker,
    StubStarsProvider,
    access_events,
    add_invite_link,
    load_sub,
    notifications,
    payments,
    subscribe,
)

FEB_28 = datetime(2026, 2, 28, 9, 0, tzinfo=UTC)
MAR_31 = datetime(2026, 3, 31, 9, 0, tzinfo=UTC)
DAY = timedelta(days=1)
MEMBER = 701

CoreFactory = Callable[[Callable[[RenewalRequest], Decision]], Awaitable[Core]]


@pytest.fixture
async def core_with_decider(settings: Settings, clock: FakeClock) -> AsyncIterator[CoreFactory]:
    """A core whose fake provider answers renewals with a scripted decision."""
    made: list[Core] = []

    async def make(decide: Callable[[RenewalRequest], Decision]) -> Core:
        provider = FakeProvider(
            secret="test-secret", base_url="http://localhost:8000", clock=clock, decide=decide
        )
        instance = await build_core(settings, clock=clock, providers=ProviderRegistry([provider]))
        made.append(instance)
        return instance

    yield make
    for instance in made:
        await instance.aclose()


async def tick(core: Core, revoker: FakeRevoker) -> TickReport:
    report = await Scheduler(core, revoker).tick()
    assert report.errors == 0
    return report


async def attempts(core: Core) -> list[ChargeAttempt]:
    async with core.sessions() as session:
        return list((await session.scalars(select(ChargeAttempt).order_by(ChargeAttempt.id))).all())


def fake_of(core: Core) -> FakeProvider:
    provider = core.providers.require("fake")
    assert isinstance(provider, FakeProvider)
    return provider


# --- the happy path ----------------------------------------------------------------------------


async def test_a_member_is_reminded_and_renewed_at_the_period_end(
    core: Core, fake: FakeProvider, clock: FakeClock, revoker: FakeRevoker
) -> None:
    sub_id = await subscribe(core, fake, MEMBER)

    clock.set(FEB_28 - 3 * DAY - timedelta(minutes=1))
    assert (await tick(core, revoker)).reminders == 0
    clock.set(FEB_28 - 3 * DAY)
    assert (await tick(core, revoker)).reminders == 1
    assert (await tick(core, revoker)).reminders == 0
    clock.set(FEB_28 - DAY)
    assert (await tick(core, revoker)).reminders == 1

    reminders = await notifications(core, "reminder")
    assert [n.key for n in reminders] == [
        f"reminder:{sub_id}:20260228T090000Z:3d",
        f"reminder:{sub_id}:20260228T090000Z:1d",
    ]
    assert reminders[0].text.startswith("Через 3 дня, 28.02.2026, подписка продлится")
    assert "спишем 990 ₽ с сохранённой карты" in reminders[0].text
    assert reminders[0].buttons == [
        {"text": "Отключить автопродление", "callback_data": f"cancel:{sub_id}"}
    ]
    assert reminders[1].text.startswith("Завтра, 28.02.2026")
    assert reminders[0].not_after == FEB_28

    clock.set(FEB_28)
    report = await tick(core, revoker)
    assert (report.charged, report.renewed) == (1, 1)
    sub = await load_sub(core, sub_id)
    assert sub.status is SubStatus.ACTIVE
    assert sub.periods_paid == 2
    assert sub.current_period_start == FEB_28
    assert sub.current_period_end == MAR_31
    assert sub.next_charge_at == MAR_31
    assert sub.claim_token is None

    renewal = (await payments(core))[-1]
    assert renewal.purpose is PaymentPurpose.RENEWAL
    assert renewal.outcome is PaymentOutcome.APPLIED
    [attempt] = await attempts(core)
    assert attempt.idempotence_key == renewal_idempotence_key(sub_id, 2, 0)
    assert attempt.status is ChargeAttemptStatus.SUCCEEDED
    assert attempt.provider_payment_id == renewal.provider_payment_id
    assert len(await notifications(core, "renewed")) == 1

    assert (await tick(core, revoker)).charged == 0
    assert fake.renewal_calls == [renewal_idempotence_key(sub_id, 2, 0)]


# --- dunning -----------------------------------------------------------------------------------


async def test_a_declined_card_goes_through_dunning_and_then_loses_access(
    core: Core, fake: FakeProvider, clock: FakeClock, revoker: FakeRevoker
) -> None:
    sub_id = await subscribe(core, fake, MEMBER, card="decline")

    clock.set(FEB_28)
    assert (await tick(core, revoker)).charge_failed == 1
    sub = await load_sub(core, sub_id)
    assert sub.status is SubStatus.PAST_DUE
    assert sub.grace_until == FEB_28 + 3 * DAY
    assert sub.next_charge_at == FEB_28 + DAY
    decision = await decide_join_request(core, chat_id=CHAT_ID, telegram_id=MEMBER)
    assert decision.action == "approve"

    clock.set(FEB_28 + DAY - timedelta(seconds=1))
    assert (await tick(core, revoker)).charged == 0
    clock.set(FEB_28 + DAY)
    assert (await tick(core, revoker)).charge_failed == 1
    assert (await load_sub(core, sub_id)).next_charge_at == FEB_28 + 3 * DAY

    # The last retry falls exactly on the end of grace: it runs, fails, and only then expires.
    clock.set(FEB_28 + 3 * DAY)
    report = await tick(core, revoker)
    assert (report.charge_failed, report.expired, report.removed) == (1, 1, 1)
    sub = await load_sub(core, sub_id)
    assert sub.status is SubStatus.EXPIRED
    assert sub.expire_reason is ExpireReason.PAYMENT_FAILED
    assert sub.access_revoked_at == FEB_28 + 3 * DAY
    assert revoker.calls == [(CHAT_ID, MEMBER)]
    assert [e.kind for e in await access_events(core)][-1] is AccessEventKind.REMOVED

    assert fake.renewal_calls == [renewal_idempotence_key(sub_id, 2, n) for n in range(3)]
    failed = await notifications(core, "payment_failed")
    assert len(failed) == 3
    assert "Повторим попытку 01.03.2026" in failed[0].text
    assert "Автоматических попыток больше не будет" in failed[2].text
    [ended] = await notifications(core, "access_ended")
    assert "Оплата за продление так и не прошла" in ended.text

    clock.advance(DAY)
    report = await tick(core, revoker)
    assert (report.charged, report.expired, report.removed) == (0, 0, 0)
    decision = await decide_join_request(core, chat_id=CHAT_ID, telegram_id=MEMBER)
    assert decision.action == "decline"


async def test_paying_during_grace_restores_the_subscription_and_stops_retries(
    core: Core, fake: FakeProvider, clock: FakeClock, revoker: FakeRevoker
) -> None:
    sub_id = await subscribe(core, fake, MEMBER, card="decline")
    clock.set(FEB_28)
    await tick(core, revoker)

    clock.set(FEB_28 + timedelta(hours=5))
    link = await club.start_repayment(core, telegram_id=MEMBER, subscription_id=sub_id)
    session = fake.read_checkout(link.url.rsplit("/", 1)[1])
    [event] = await fake.parse_webhook(*fake.checkout_webhook(session, succeeded=True, card="ok"))
    assert (await apply_event(core, event)).outcome is PaymentOutcome.APPLIED

    sub = await load_sub(core, sub_id)
    assert sub.status is SubStatus.ACTIVE
    assert sub.current_period_start == FEB_28
    assert sub.current_period_end == MAR_31
    assert sub.payment_method_id is not None
    assert sub.payment_method_id.startswith("fake_pm_ok_")
    clock.set(FEB_28 + 3 * DAY)
    report = await tick(core, revoker)
    assert (report.charged, report.expired) == (0, 0)
    assert len(fake.renewal_calls) == 1


async def test_without_a_saved_card_the_renewal_fails_and_asks_to_pay(
    core: Core, fake: FakeProvider, clock: FakeClock, revoker: FakeRevoker
) -> None:
    sub_id = await subscribe(core, fake, MEMBER, card=None)
    clock.set(FEB_28 - 3 * DAY)
    await tick(core, revoker)
    [reminder] = await notifications(core, "reminder")
    assert "Автосписание не настроено" in reminder.text
    assert reminder.buttons == [{"text": "Оплатить 990 ₽", "callback_data": f"pay:{sub_id}"}]

    clock.set(FEB_28)
    await tick(core, revoker)
    assert (await load_sub(core, sub_id)).status is SubStatus.PAST_DUE
    failure = (await payments(core))[-1]
    assert failure.status is PaymentStatus.FAILED
    assert failure.failure_reason == "no_saved_payment_method"


async def test_a_disabled_provider_sends_its_subscriptions_through_dunning(
    core: Core, fake: FakeProvider, settings: Settings, clock: FakeClock, revoker: FakeRevoker
) -> None:
    sub_id = await subscribe(core, fake, MEMBER)
    without = await build_core(settings, clock=clock, providers=ProviderRegistry())
    try:
        clock.set(FEB_28)
        assert (await tick(without, revoker)).charge_failed == 1
    finally:
        await without.aclose()
    assert (await load_sub(core, sub_id)).status is SubStatus.PAST_DUE
    assert (await payments(core))[-1].failure_reason == "provider_not_configured"


# --- crashes, pending charges and concurrency --------------------------------------------------


async def test_a_provider_timeout_is_retried_later_with_the_same_key(
    core_with_decider: CoreFactory, clock: FakeClock, revoker: FakeRevoker
) -> None:
    answers: list[Decision] = ["error", "succeeded"]
    core = await core_with_decider(lambda request: answers.pop(0))
    fake = fake_of(core)
    sub_id = await subscribe(core, fake, MEMBER)

    clock.set(FEB_28)
    assert (await tick(core, revoker)).provider_errors == 1
    [attempt] = await attempts(core)
    assert attempt.status is ChargeAttemptStatus.STARTED
    assert attempt.last_error == "ProviderError: fake provider timeout"
    assert (await load_sub(core, sub_id)).status is SubStatus.ACTIVE

    clock.advance(timedelta(seconds=299))
    assert (await tick(core, revoker)).charged == 0
    clock.advance(timedelta(seconds=1))
    assert (await tick(core, revoker)).renewed == 1

    key = renewal_idempotence_key(sub_id, 2, 0)
    assert fake.renewal_calls == [key, key]
    [attempt] = await attempts(core)
    assert (attempt.calls, attempt.status) == (2, ChargeAttemptStatus.SUCCEEDED)
    assert len(await payments(core)) == 2
    assert (await load_sub(core, sub_id)).current_period_end == MAR_31


async def test_a_worker_that_dies_mid_charge_is_replaced_without_a_double_charge(
    core_with_decider: CoreFactory, clock: FakeClock, revoker: FakeRevoker
) -> None:
    calls = 0

    def decide(request: RenewalRequest) -> Decision:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SystemExit("worker killed while waiting for the provider")
        return "succeeded"

    core = await core_with_decider(decide)
    fake = fake_of(core)
    sub_id = await subscribe(core, fake, MEMBER)

    clock.set(FEB_28)
    with pytest.raises(SystemExit):
        await Scheduler(core, revoker).tick()
    [attempt] = await attempts(core)
    assert attempt.status is ChargeAttemptStatus.STARTED
    sub = await load_sub(core, sub_id)
    assert sub.claim_token is not None
    assert sub.claimed_until == FEB_28 + timedelta(seconds=300)

    # Another worker waits for the dead one's lease, then repeats the same request.
    clock.advance(timedelta(seconds=300))
    assert (await tick(core, revoker)).renewed == 1
    key = renewal_idempotence_key(sub_id, 2, 0)
    assert fake.renewal_calls == [key, key]
    renewals = [p for p in await payments(core) if p.purpose is PaymentPurpose.RENEWAL]
    assert len(renewals) == 1


async def test_a_pending_charge_is_settled_by_the_webhook(
    core_with_decider: CoreFactory, clock: FakeClock, revoker: FakeRevoker
) -> None:
    core = await core_with_decider(lambda request: "pending")
    fake = fake_of(core)
    sub_id = await subscribe(core, fake, MEMBER)

    clock.set(FEB_28)
    assert (await tick(core, revoker)).pending == 1
    [attempt] = await attempts(core)
    assert attempt.status is ChargeAttemptStatus.PENDING
    clock.advance(timedelta(minutes=10))
    assert (await tick(core, revoker)).charged == 0

    sub = await load_sub(core, sub_id)
    request = RenewalRequest(
        subscription_id=sub_id,
        member_telegram_id=MEMBER,
        plan_code="month",
        amount=Money(99000, "RUB"),
        for_period=2,
        attempt=0,
        payment_method_id=sub.payment_method_id,
        idempotence_key=renewal_idempotence_key(sub_id, 2, 0),
        description="",
    )
    [event] = await fake.parse_webhook(*fake.renewal_webhook(request, succeeded=True))
    assert (await apply_event(core, event)).outcome is PaymentOutcome.APPLIED
    [attempt] = await attempts(core)
    assert attempt.status is ChargeAttemptStatus.SUCCEEDED

    clock.advance(timedelta(hours=1))
    assert (await tick(core, revoker)).charged == 0
    assert (await load_sub(core, sub_id)).current_period_end == MAR_31


async def test_a_charge_still_pending_after_the_recheck_is_asked_again_with_the_same_key(
    core_with_decider: CoreFactory, clock: FakeClock, revoker: FakeRevoker
) -> None:
    core = await core_with_decider(lambda request: "pending")
    fake = fake_of(core)
    sub_id = await subscribe(core, fake, MEMBER)
    clock.set(FEB_28)
    await tick(core, revoker)
    clock.advance(timedelta(seconds=900))
    assert (await tick(core, revoker)).pending == 1
    key = renewal_idempotence_key(sub_id, 2, 0)
    assert fake.renewal_calls == [key, key]
    [attempt] = await attempts(core)
    assert attempt.calls == 2


async def test_two_workers_charge_each_renewal_exactly_once(
    core: Core, fake: FakeProvider, clock: FakeClock, revoker: FakeRevoker
) -> None:
    sub_ids = [await subscribe(core, fake, MEMBER + i) for i in range(12)]
    clock.set(FEB_28)
    first = Scheduler(core, revoker, batch_size=4)
    second = Scheduler(core, revoker, batch_size=4)
    for _ in range(3):
        reports = await asyncio.gather(first.tick(), second.tick())
        assert all(report.errors == 0 for report in reports)

    assert sorted(fake.renewal_calls) == sorted(
        renewal_idempotence_key(sub_id, 2, 0) for sub_id in sub_ids
    )
    for sub_id in sub_ids:
        assert (await load_sub(core, sub_id)).periods_paid == 2
    renewals = [p for p in await payments(core) if p.purpose is PaymentPurpose.RENEWAL]
    assert len(renewals) == 12
    assert len(await notifications(core, "renewed")) == 12


class OverchargingProvider(FakeProvider):
    """Reports a renewal for more than was asked: the payment cannot be applied."""

    async def charge_renewal(self, request: RenewalRequest) -> ChargeResult:
        self.renewal_calls.append(request.idempotence_key)
        return PaymentSucceeded(
            provider=self.name,
            provider_payment_id=self.renewal_payment_id(request.idempotence_key),
            subscription_id=request.subscription_id,
            amount=Money(request.amount.minor + 100, request.amount.currency),
            occurred_at=FEB_28,
            purpose=PaymentPurpose.RENEWAL,
            for_period=request.for_period,
            attempt=request.attempt,
        )


async def test_a_settled_charge_that_did_not_renew_is_not_requested_again_in_a_loop(
    settings: Settings, clock: FakeClock, revoker: FakeRevoker
) -> None:
    provider = OverchargingProvider(secret="s", base_url="http://localhost:8000", clock=clock)
    core = await build_core(settings, clock=clock, providers=ProviderRegistry([provider]))
    try:
        sub_id = await subscribe(core, provider, MEMBER)
        clock.set(FEB_28)
        report = await Scheduler(core, revoker).tick()
        assert (report.charged, report.renewed, report.errors) == (1, 0, 1)
        [alert] = await notifications(core, "owner_alert")
        assert "ожидалось 990 ₽" in alert.text
        assert (await load_sub(core, sub_id)).status is SubStatus.ACTIVE

        assert (await Scheduler(core, revoker).tick()).errors == 0  # backed off: not picked up
        clock.advance(timedelta(seconds=300))
        assert (await Scheduler(core, revoker).tick()).errors == 1
        clock.advance(timedelta(seconds=300))
        assert (await Scheduler(core, revoker).tick()).errors == 1
        assert provider.renewal_calls == [renewal_idempotence_key(sub_id, 2, 0)]
    finally:
        await core.aclose()


# --- cancel, expiry, access --------------------------------------------------------------------


async def test_a_canceled_subscription_is_not_charged_and_ends_at_the_period_end(
    core: Core, fake: FakeProvider, clock: FakeClock, revoker: FakeRevoker
) -> None:
    sub_id = await subscribe(core, fake, MEMBER)
    await club.cancel(core, telegram_id=MEMBER, subscription_id=sub_id)

    clock.set(FEB_28 - 3 * DAY)
    await tick(core, revoker)
    [reminder] = await notifications(core, "reminder")
    assert "закончится доступ к клубу: автопродление отключено" in reminder.text
    assert reminder.buttons == [
        {"text": "Включить автопродление", "callback_data": f"resume:{sub_id}"}
    ]

    clock.set(FEB_28 - timedelta(seconds=1))
    assert (await tick(core, revoker)).expired == 0
    clock.set(FEB_28)
    report = await tick(core, revoker)
    assert (report.charged, report.expired, report.removed) == (0, 1, 1)
    sub = await load_sub(core, sub_id)
    assert sub.status is SubStatus.EXPIRED
    assert sub.expire_reason is ExpireReason.CANCELED
    [ended] = await notifications(core, "access_ended")
    assert ended.text.startswith("Оплаченный период закончился")
    assert fake.renewal_calls == []


async def test_the_owner_and_admins_are_never_removed(
    core: Core, fake: FakeProvider, clock: FakeClock, revoker: FakeRevoker
) -> None:
    for user in (OWNER_ID, ADMIN_ID):
        sub_id = await subscribe(core, fake, user)
        await club.cancel(core, telegram_id=user, subscription_id=sub_id)
    clock.set(FEB_28)
    report = await tick(core, revoker)
    assert report.expired == 2
    assert revoker.calls == []
    kinds = [e.kind for e in await access_events(core)]
    assert kinds == [AccessEventKind.SKIPPED_ADMIN, AccessEventKind.SKIPPED_ADMIN]
    assert (await tick(core, revoker)).removed == 0


async def test_removal_outcomes_are_recorded_and_network_errors_retried(
    core: Core, fake: FakeProvider, clock: FakeClock, revoker: FakeRevoker
) -> None:
    left, no_rights, flaky = MEMBER, MEMBER + 1, MEMBER + 2
    revoker.outcomes = {left: RevokeOutcome.NOT_PARTICIPANT, no_rights: RevokeOutcome.NO_RIGHTS}
    revoker.failures = {flaky: 1}
    subs = {}
    for user in (left, no_rights, flaky):
        subs[user] = await subscribe(core, fake, user)
        await club.cancel(core, telegram_id=user, subscription_id=subs[user])

    clock.set(FEB_28)
    report = await tick(core, revoker)
    assert (report.expired, report.removed, report.revoke_errors) == (3, 0, 1)
    assert (await load_sub(core, subs[left])).access_revoked_at == FEB_28
    assert (await load_sub(core, subs[no_rights])).access_revoked_at == FEB_28
    assert (await load_sub(core, subs[flaky])).access_revoked_at is None

    clock.advance(timedelta(seconds=299))
    assert (await tick(core, revoker)).removed == 0
    clock.advance(timedelta(seconds=1))
    assert (await tick(core, revoker)).removed == 1
    assert (await load_sub(core, subs[flaky])).access_revoked_at is not None

    events = [(e.telegram_id, e.kind) for e in await access_events(core)]
    assert events == [
        (left, AccessEventKind.NOT_PARTICIPANT),
        (no_rights, AccessEventKind.NO_RIGHTS),
        (flaky, AccessEventKind.REVOKE_ERROR),
        (flaky, AccessEventKind.REMOVED),
    ]
    [alert] = await notifications(core, "owner_alert")
    assert alert.chat_id == OWNER_ID
    assert f"В чате {CHAT_ID} у бота нет права блокировать участников" in alert.text


async def test_a_member_who_paid_again_before_removal_ran_is_left_in(
    core: Core, fake: FakeProvider, clock: FakeClock, revoker: FakeRevoker
) -> None:
    old_id = await subscribe(core, fake, MEMBER)
    await club.cancel(core, telegram_id=MEMBER, subscription_id=old_id)
    revoker.failures = {MEMBER: 1}
    clock.set(FEB_28)
    await tick(core, revoker)
    assert (await load_sub(core, old_id)).access_revoked_at is None

    await subscribe(core, fake, MEMBER, plan="year")
    clock.advance(timedelta(seconds=300))
    assert (await tick(core, revoker)).removed == 0
    assert (await load_sub(core, old_id)).access_revoked_at is not None
    assert [e.kind for e in await access_events(core)][-1] is AccessEventKind.SKIPPED_HAS_ACCESS
    assert len(revoker.calls) == 1


async def test_paying_after_expiry_reactivates_from_the_payment(
    core: Core, fake: FakeProvider, clock: FakeClock, revoker: FakeRevoker
) -> None:
    await add_invite_link(core)
    sub_id = await subscribe(core, fake, MEMBER)
    await club.cancel(core, telegram_id=MEMBER, subscription_id=sub_id)
    clock.set(FEB_28)
    await tick(core, revoker)
    expired = await load_sub(core, sub_id)
    assert expired.status is SubStatus.EXPIRED
    assert expired.access_revoked_at == FEB_28
    assert revoker.calls == [(CHAT_ID, MEMBER)]

    clock.advance(timedelta(days=5))
    link = await club.start_repayment(core, telegram_id=MEMBER, subscription_id=sub_id)
    session = fake.read_checkout(link.url.rsplit("/", 1)[1])
    [event] = await fake.parse_webhook(*fake.checkout_webhook(session, succeeded=True))
    assert (await apply_event(core, event)).outcome is PaymentOutcome.APPLIED

    sub = await load_sub(core, sub_id)
    back = FEB_28 + timedelta(days=5)
    assert sub.status is SubStatus.ACTIVE
    assert sub.periods_paid == 2
    assert sub.current_period_start == back
    assert sub.current_period_end == datetime(2026, 4, 5, 9, 0, tzinfo=UTC)
    assert sub.access_revoked_at is None
    activations = await notifications(core, "activated")
    assert len(activations) == 2
    assert "снова открыт" in activations[-1].text
    assert activations[-1].buttons == [{"text": "Вступить в клуб", "url": "https://t.me/+club"}]

    # The next expiry removes the member again.
    await club.cancel(core, telegram_id=MEMBER, subscription_id=sub_id)
    clock.set(datetime(2026, 4, 5, 9, 0, tzinfo=UTC))
    assert (await tick(core, revoker)).removed == 1
    assert len(revoker.calls) == 2


async def test_paying_an_old_subscription_while_another_is_live_needs_a_refund(
    core: Core, fake: FakeProvider, clock: FakeClock, revoker: FakeRevoker
) -> None:
    old_id = await subscribe(core, fake, MEMBER)
    await club.cancel(core, telegram_id=MEMBER, subscription_id=old_id)
    clock.set(FEB_28)
    await tick(core, revoker)
    stale_link = await club.start_repayment(core, telegram_id=MEMBER, subscription_id=old_id)
    new_id = await subscribe(core, fake, MEMBER, plan="quarter")
    assert new_id != old_id

    session = fake.read_checkout(stale_link.url.rsplit("/", 1)[1])
    [event] = await fake.parse_webhook(*fake.checkout_webhook(session, succeeded=True))
    assert (await apply_event(core, event)).outcome is PaymentOutcome.NEEDS_REFUND
    assert (await load_sub(core, old_id)).status is SubStatus.EXPIRED
    [alert] = await notifications(core, "owner_alert")
    assert f"#{new_id}" in alert.text


# --- Telegram Stars ----------------------------------------------------------------------------


async def stars_subscriber(core: Core, clock: FakeClock) -> tuple[int, datetime]:
    core.providers.register(StubStarsProvider())
    link = await club.start_checkout(core, telegram_id=MEMBER, plan_code="month", provider="stars")
    end = clock.now() + timedelta(days=30)
    await apply_event(
        core,
        PaymentSucceeded(
            provider="stars",
            provider_payment_id="ch_1",
            subscription_id=link.subscription_id,
            amount=Money(500, "XTR"),
            occurred_at=clock.now(),
            purpose=PaymentPurpose.CHECKOUT,
            period_end=end,
            payment_method_id="ch_1",
        ),
    )
    return link.subscription_id, end


async def test_a_stars_renewal_that_never_arrives_ends_access_without_charging(
    core: Core, clock: FakeClock, revoker: FakeRevoker
) -> None:
    sub_id, end = await stars_subscriber(core, clock)

    clock.set(end - 3 * DAY)
    await tick(core, revoker)
    [reminder] = await notifications(core, "reminder")
    assert "Telegram продлит подписку за 500 ⭐" in reminder.text

    clock.set(end + timedelta(minutes=59))
    assert (await tick(core, revoker)).renewals_missing == 0
    clock.set(end + timedelta(minutes=60))
    report = await tick(core, revoker)
    assert (report.renewals_missing, report.charged, report.provider_errors) == (1, 0, 0)
    sub = await load_sub(core, sub_id)
    assert sub.status is SubStatus.PAST_DUE
    assert sub.next_charge_at is None
    assert sub.grace_until == end + timedelta(minutes=60) + 3 * DAY
    [failed] = await notifications(core, "payment_failed")
    assert "Telegram не продлил подписку за 500 ⭐" in failed.text

    clock.set(end + timedelta(minutes=60) + 3 * DAY)
    report = await tick(core, revoker)
    assert (report.expired, report.removed) == (1, 1)


async def test_a_late_stars_renewal_during_grace_restores_the_subscription(
    core: Core, clock: FakeClock, revoker: FakeRevoker
) -> None:
    sub_id, end = await stars_subscriber(core, clock)
    clock.set(end + timedelta(hours=2))
    await tick(core, revoker)
    assert (await load_sub(core, sub_id)).status is SubStatus.PAST_DUE

    renewal = PaymentSucceeded(
        provider="stars",
        provider_payment_id="ch_2",
        subscription_id=sub_id,
        amount=Money(500, "XTR"),
        occurred_at=clock.now(),
        purpose=PaymentPurpose.RENEWAL,
        period_end=end + timedelta(days=30),
    )
    assert (await apply_event(core, renewal)).outcome is PaymentOutcome.APPLIED
    sub = await load_sub(core, sub_id)
    assert sub.status is SubStatus.ACTIVE
    assert sub.current_period_end == end + timedelta(days=30)
    assert sub.next_charge_at == end + timedelta(days=30, minutes=60)
    assert sub.payment_method_id == "ch_1"


async def test_the_loop_runs_ticks_until_stopped(
    core: Core, fake: FakeProvider, clock: FakeClock, revoker: FakeRevoker
) -> None:
    await subscribe(core, fake, MEMBER)
    clock.set(FEB_28)
    stop = asyncio.Event()
    task = asyncio.create_task(Scheduler(core, revoker).run(stop, interval=0.01))
    for _ in range(100):
        if fake.renewal_calls:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert len(fake.renewal_calls) == 1
