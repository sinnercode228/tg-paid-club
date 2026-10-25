"""Statistics on a hand-built month of club history, with every expected number derived by hand
in the comments, plus one run through the real checkout, renewal and expiry code paths."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paidclub import club
from paidclub.billing.base import PaymentPurpose
from paidclub.billing.fake import FakeProvider
from paidclub.clock import FakeClock
from paidclub.container import Core
from paidclub.domain.subscriptions import ExpireReason, SubStatus
from paidclub.models import Member, Payment, PaymentOutcome, PaymentStatus, Subscription
from paidclub.money import Money
from paidclub.scheduler import Scheduler
from paidclub.stats import (
    Counts,
    Report,
    build_months,
    build_report,
    club_report,
    export_payments,
    export_subscriptions,
    month_window,
    months_in_period,
    report_to_dict,
    window,
)
from tests.helpers import FakeRevoker, subscribe

MSK = ZoneInfo("Europe/Moscow")
#: The report window: March 2026 in Moscow time, 2026-02-28 21:00 to 2026-03-31 21:00 UTC.
MARCH = datetime(2026, 3, 1, tzinfo=MSK).astimezone(UTC)
APRIL = datetime(2026, 4, 1, tzinfo=MSK).astimezone(UTC)
FEBRUARY = datetime(2026, 2, 1, tzinfo=MSK).astimezone(UTC)
FEB_28_9 = datetime(2026, 2, 28, 9, 0, tzinfo=UTC)
EVIL_NAME = '=HYPERLINK("http://evil.example","x")'


def utc(month: int, day: int, hour: int = 10, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


@dataclass(frozen=True)
class Plan:
    code: str
    price_minor: int
    currency: str = "RUB"
    unit: str = "month"
    count: int = 1
    provider: str = "fake"


MONTH = Plan("month", 99_000)
QUARTER = Plan("quarter", 249_000, count=3)
YEAR = Plan("year", 890_000, count=12)
MONTH_STARS = Plan("month", 500, currency="XTR", provider="stars")
#: A plan the owner no longer sells (not in settings), billed every 15 days.
LEGACY = Plan("legacy15", 49_500, unit="day", count=15)


class Dataset:
    """Writes subscriptions through the ORM, so ``subscription_events`` comes from the real
    before-flush hook, with each status change stamped at a chosen moment."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.next_tg = 5000
        self.payments = 0
        self.subs: dict[str, Subscription] = {}

    async def member(self, name: str, *, at: datetime, first_name: str | None = None) -> Member:
        self.next_tg += 1
        member = Member(
            telegram_id=self.next_tg, username=name.lower(), first_name=first_name, created_at=at
        )
        self.session.add(member)
        await self.session.flush()
        return member

    async def sub(
        self, name: str, plan: Plan, *, at: datetime, first_name: str | None = None
    ) -> Subscription:
        member = await self.member(name, at=at, first_name=first_name)
        sub = Subscription(
            member_id=member.id,
            plan_code=plan.code,
            provider=plan.provider,
            status=SubStatus.PENDING,
            period_unit=plan.unit,
            period_count=plan.count,
            price_minor=plan.price_minor,
            currency=plan.currency,
            created_at=at,
            updated_at=at,
        )
        self.session.add(sub)
        await self.session.flush()
        self.subs[name] = sub
        return sub

    async def move(
        self, name: str, status: SubStatus, *, at: datetime, reason: ExpireReason | None = None
    ) -> None:
        sub = self.subs[name]
        sub.status = status
        sub.updated_at = at
        if status is SubStatus.ACTIVE:
            sub.activated_at = sub.activated_at or at
            sub.current_period_end = at + timedelta(days=30)
        if status is SubStatus.EXPIRED:
            sub.expire_reason = reason
            sub.expired_at = at
        await self.session.flush()

    async def active(self, name: str, plan: Plan, *, at: datetime) -> None:
        await self.sub(name, plan, at=at)
        await self.move(name, SubStatus.ACTIVE, at=at)

    def pay(
        self,
        name: str | None,
        *,
        at: datetime,
        for_period: int | None,
        succeeded: bool = True,
        purpose: PaymentPurpose = PaymentPurpose.RENEWAL,
        outcome: PaymentOutcome | None = None,
        amount_minor: int | None = None,
    ) -> None:
        sub = self.subs[name] if name else None
        if outcome is None:
            outcome = PaymentOutcome.APPLIED if succeeded else PaymentOutcome.RECORDED
        if amount_minor is None:
            assert sub is not None
            amount_minor = sub.price_minor
        self.payments += 1
        self.session.add(
            Payment(
                provider=sub.provider if sub else "fake",
                provider_payment_id=f"pay-{self.payments}",
                subscription_id=sub.id if sub else None,
                member_id=sub.member_id if sub else None,
                plan_code=sub.plan_code if sub else None,
                purpose=purpose,
                status=PaymentStatus.SUCCEEDED if succeeded else PaymentStatus.FAILED,
                outcome=outcome,
                applied=succeeded and outcome is PaymentOutcome.APPLIED,
                amount_minor=amount_minor,
                currency=sub.currency if sub else "RUB",
                for_period=for_period,
                attempt=None,
                occurred_at=at,
                created_at=at,
            )
        )


async def build_dataset(core: Core) -> None:
    """Eleven members around March 2026 (Moscow time)."""
    async with core.sessions() as session, session.begin():
        d = Dataset(session)
        # Before the window. H subscribed in January and lost access on 25 February.
        await d.active("E", YEAR, at=utc(1, 15))
        await d.active("H", MONTH, at=utc(1, 10))
        await d.move("H", SubStatus.PAST_DUE, at=utc(2, 10))
        await d.move("H", SubStatus.EXPIRED, at=utc(2, 25), reason=ExpireReason.PAYMENT_FAILED)
        # 2026-02-01 00:00 UTC is 03:00 on 1 February in Moscow: February's first new member.
        await d.active("D", MONTH, at=utc(2, 1, 0))
        await d.active("C", MONTH, at=utc(2, 5))
        await d.active("A", MONTH, at=utc(2, 10))
        d.pay("A", at=utc(2, 10), for_period=1, purpose=PaymentPurpose.CHECKOUT)
        await d.active("M", LEGACY, at=utc(2, 15))
        await d.active("F", MONTH, at=utc(2, 20))
        await d.active("I", MONTH, at=utc(2, 20))
        # Live when March starts: A, C, D, E, F, I, M = 7.

        # D: the renewal fails at 22:00 UTC on 28 February, which is 01:00 on 1 March in
        # Moscow, so it belongs to March. Two retries fail, then the grace period ends.
        await d.move("D", SubStatus.PAST_DUE, at=utc(2, 28, 22))
        d.pay("D", at=utc(2, 28, 22), for_period=2, succeeded=False)
        d.pay("D", at=utc(3, 2), for_period=2, succeeded=False)
        d.pay("D", at=utc(3, 4), for_period=2, succeeded=False)
        await d.move("D", SubStatus.EXPIRED, at=utc(3, 4, 11), reason=ExpireReason.PAYMENT_FAILED)
        # B: a new quarterly member.
        await d.active("B", QUARTER, at=utc(3, 2))
        d.pay("B", at=utc(3, 2), for_period=1, purpose=PaymentPurpose.CHECKOUT)
        # E: cancels auto-renew, changes their mind.
        await d.move("E", SubStatus.CANCELED, at=utc(3, 3))
        await d.move("E", SubStatus.ACTIVE, at=utc(3, 8))
        # C: two failed charges, then pays during the grace period.
        await d.move("C", SubStatus.PAST_DUE, at=utc(3, 5))
        d.pay("C", at=utc(3, 5), for_period=2, succeeded=False)
        d.pay("C", at=utc(3, 6), for_period=2, succeeded=False)
        d.pay("C", at=utc(3, 7), for_period=2)
        await d.move("C", SubStatus.ACTIVE, at=utc(3, 7))
        # A: a normal renewal, and a second payment for the same period that changed nothing.
        d.pay("A", at=utc(3, 10), for_period=2)
        d.pay(
            "A",
            at=utc(3, 11),
            for_period=2,
            purpose=PaymentPurpose.CHECKOUT,
            outcome=PaymentOutcome.NEEDS_REFUND,
        )
        # F: cancels, and access ends with the paid period.
        await d.move("F", SubStatus.CANCELED, at=utc(3, 12))
        await d.move("F", SubStatus.EXPIRED, at=utc(3, 20), reason=ExpireReason.CANCELED)
        # G: a new member paying in Telegram Stars.
        await d.active("G", MONTH_STARS, at=utc(3, 15))
        d.pay("G", at=utc(3, 15), for_period=1, purpose=PaymentPurpose.CHECKOUT)
        # H: comes back and pays for period 3 on the old subscription.
        await d.move("H", SubStatus.ACTIVE, at=utc(3, 18))
        d.pay("H", at=utc(3, 18), for_period=3, purpose=PaymentPurpose.CHECKOUT)
        # I: the renewal fails and they cancel while past due, which ends access at once.
        await d.move("I", SubStatus.PAST_DUE, at=utc(3, 20))
        d.pay("I", at=utc(3, 20), for_period=2, succeeded=False)
        await d.move("I", SubStatus.EXPIRED, at=utc(3, 22), reason=ExpireReason.CANCELED)
        # J: the card is declined on the checkout page; the subscription stays pending.
        await d.sub("J", MONTH, at=utc(3, 25), first_name=EVIL_NAME)
        d.pay("J", at=utc(3, 25), for_period=1, succeeded=False, purpose=PaymentPurpose.CHECKOUT)
        # Money for a subscription we do not know.
        d.pay(
            None,
            at=utc(3, 26),
            for_period=None,
            purpose=PaymentPurpose.CHECKOUT,
            outcome=PaymentOutcome.UNKNOWN_SUBSCRIPTION,
            amount_minor=99_000,
        )
        # 21:30 UTC on 31 March is already 1 April in Moscow: outside the window.
        d.pay("A", at=utc(3, 31, 21, 30), for_period=3)
        await session.flush()


async def march_report(core: Core) -> Report:
    async with core.sessions() as session:
        return await build_report(
            session, start=MARCH, end=APRIL, tz=MSK, plans=core.settings.plans, now=APRIL
        )


# --- the hand-built month -------------------------------------------------------------------


async def test_counts_for_the_window(core: Core) -> None:
    await build_dataset(core)
    report = await march_report(core)
    totals = report.totals

    assert totals.new == 2  # B, G
    assert totals.reactivated == 1  # H
    assert totals.first_payments == 2  # B, G
    assert totals.renewals == 3  # A period 2, C period 2, H period 3
    assert totals.payments == 5
    assert totals.dunning_started == 3  # D, C, I
    assert totals.recovered == 1  # C
    assert totals.cancellations == 3  # E and F to canceled, I canceled while past due
    assert totals.resumed == 1  # E
    assert totals.churned == 3  # D, F, I
    assert totals.churned_unpaid == 1  # D
    assert totals.churned_after_cancel == 2  # F, I
    assert totals.failed_charges == 6  # D x3, C x2, I x1
    assert totals.failed_checkouts == 1  # J
    assert totals.needs_attention == 2  # A's repeat payment, the unknown subscription


async def test_revenue_keeps_currencies_apart_and_ignores_unapplied_money(core: Core) -> None:
    await build_dataset(core)
    report = await march_report(core)

    # 990 (A) + 2490 (B) + 990 (C) + 990 (H); not A's refund case, not the unknown payment,
    # not A's period 3 that was paid on 1 April Moscow time.
    assert report.totals.revenue == {
        "RUB": Money.from_decimal("5460.00", "RUB"),
        "XTR": Money(500, "XTR"),
    }
    assert report.currencies == ["RUB", "XTR"]


async def test_live_members_and_churn_rate(core: Core) -> None:
    await build_dataset(core)
    report = await march_report(core)

    assert report.live_at_start == 7  # A, C, D, E, F, I, M
    # 7 - D + B - F + G + H - I = 7; the same as the subscriptions table says now.
    assert report.days[-1].live == 7
    assert report.snapshot.total.live == 7
    assert (report.snapshot.total.active, report.snapshot.total.past_due) == (7, 0)
    assert report.exposed == 10  # 7 at the start + 2 new + 1 reactivated
    assert report.churn_rate == Decimal("0.3000")


async def test_mrr_normalises_every_period_to_a_month(core: Core) -> None:
    await build_dataset(core)
    snapshot = (await march_report(core)).snapshot

    # Active RUB subscriptions, in kopecks per month:
    #   A, C, H: 99 000 each; B: 249 000 / 3 = 83 000; E: 890 000 / 12 = 74 166.67;
    #   M: 49 500 per 15 days = 99 000.  Sum = 553 166.67, rounded once -> 553 167.
    assert snapshot.total.mrr == {
        "RUB": Money(553_167, "RUB"),
        "XTR": Money(500, "XTR"),
    }
    assert snapshot.by_plan["year"].mrr == {"RUB": Money(74_167, "RUB")}
    assert snapshot.by_plan["month"].mrr == {
        "RUB": Money(297_000, "RUB"),
        "XTR": Money(500, "XTR"),
    }
    assert months_in_period("day", 15) == Decimal("0.5")
    assert months_in_period("month", 12) == Decimal(12)


async def test_canceled_subscriptions_keep_access_but_not_mrr(core: Core) -> None:
    await build_dataset(core)
    async with core.sessions() as session, session.begin():
        b = (
            await session.scalars(select(Subscription).join(Member).where(Member.username == "b"))
        ).one()
        assert b.plan_code == "quarter"
        b.status = SubStatus.CANCELED
        b.updated_at = utc(3, 31, 12)
    snapshot = (await march_report(core)).snapshot

    assert snapshot.total.live == 7
    assert snapshot.total.canceled == 1
    assert snapshot.total.mrr["RUB"] == Money(553_167 - 83_000, "RUB")


async def test_days_are_moscow_calendar_days(core: Core) -> None:
    await build_dataset(core)
    report = await march_report(core)
    days = {day.start.day: day for day in report.days}

    assert len(report.days) == 31
    assert report.days[0].start.isoformat() == "2026-03-01"
    # D's failure at 22:00 UTC on 28 February is 1 March in Moscow.
    assert days[1].counts.dunning_started == 1
    assert days[1].counts.failed_charges == 1
    assert days[2].counts.new == 1
    assert days[2].counts.revenue == {"RUB": Money(249_000, "RUB")}
    assert days[2].counts.failed_charges == 1
    assert [days[n].live for n in (1, 2, 4, 15, 18, 20, 22, 31)] == [7, 8, 7, 8, 9, 8, 7, 7]
    # The days add up to the totals.
    summed = Counts()
    for day in report.days:
        summed.merge(day.counts)
    assert summed == report.totals


async def test_plans_in_configured_order_then_retired_ones(core: Core) -> None:
    await build_dataset(core)
    report = await march_report(core)
    plans = {plan.code: plan for plan in report.plans}

    assert [plan.code for plan in report.plans] == ["month", "quarter", "year", "legacy15"]
    assert plans["legacy15"].title == "legacy15"
    assert plans["month"].title == "1 месяц"
    assert plans["month"].now.live == 4  # A, C, H, G
    assert plans["month"].period.renewals == 3
    assert plans["month"].period.churned == 3
    assert plans["month"].period.revenue == {
        "RUB": Money(297_000, "RUB"),
        "XTR": Money(500, "XTR"),
    }
    assert plans["quarter"].period.new == 1
    # The unknown-subscription payment has no plan: in the totals, in no plan.
    assert sum(plan.period.needs_attention for plan in report.plans) == 1


async def test_months(core: Core) -> None:
    await build_dataset(core)
    async with core.sessions() as session:
        months = await build_months(session, start=FEBRUARY, end=APRIL, tz=MSK)

    assert [month.start.isoformat() for month in months] == ["2026-02-01", "2026-03-01"]
    february, march = months
    assert february.counts.new == 6  # D, C, A, M, F, I
    assert february.counts.churned_unpaid == 1  # H
    assert february.counts.revenue == {"RUB": Money(99_000, "RUB")}
    assert february.live == 7
    assert march.counts.renewals == 3
    assert march.live == 7


async def test_report_as_json(core: Core) -> None:
    await build_dataset(core)
    data = report_to_dict(await march_report(core))

    assert data["timezone"] == "Europe/Moscow"
    assert data["start"] == "2026-02-28T21:00:00+00:00"
    assert data["now"] == {
        "live": 7,
        "active": 7,
        "past_due": 0,
        "canceled": 0,
        "mrr": {"RUB": "5531.67", "XTR": "500"},
    }
    assert data["churn_rate"] == "0.3000"
    assert data["totals"]["revenue"] == {"RUB": "5460.00", "XTR": "500"}
    assert data["totals"]["payments"] == 5
    assert len(data["days"]) == 31
    assert data["days"][0]["date"] == "2026-03-01"


async def test_empty_club(core: Core) -> None:
    report = await march_report(core)

    assert report.churn_rate is None
    assert report.totals == Counts()
    assert report.snapshot.total.mrr == {}
    assert all(day.live == 0 for day in report.days)


# --- windows --------------------------------------------------------------------------------


def test_window_counts_today_as_the_first_day() -> None:
    now = datetime(2026, 3, 15, 12, 0, tzinfo=UTC)
    start, end = window(now, 7, MSK)
    assert start == datetime(2026, 3, 9, tzinfo=MSK)
    assert end == now
    # 22:30 UTC is already the next day in Moscow.
    late = datetime(2026, 3, 15, 22, 30, tzinfo=UTC)
    assert window(late, 1, MSK)[0] == datetime(2026, 3, 16, tzinfo=MSK)


def test_month_window_crosses_the_year() -> None:
    now = datetime(2026, 2, 10, 12, 0, tzinfo=UTC)
    assert month_window(now, 3, MSK)[0] == datetime(2025, 12, 1, tzinfo=MSK)


@pytest.mark.parametrize("days", [0, 367])
def test_window_rejects_odd_lengths(days: int) -> None:
    with pytest.raises(ValueError, match="days"):
        window(datetime(2026, 3, 15, tzinfo=UTC), days, MSK)


# --- CSV ------------------------------------------------------------------------------------


def _read_csv(content: bytes) -> list[dict[str, str]]:
    assert content.startswith(b"\xef\xbb\xbf")  # BOM: Excel reads the Cyrillic right
    return list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))


async def test_subscription_export(core: Core) -> None:
    await build_dataset(core)
    async with core.sessions() as session:
        rows = _read_csv(await export_subscriptions(session, MSK))

    assert len(rows) == 11
    by_user = {row["username"]: row for row in rows}
    e = by_user["e"]
    assert e["plan_code"] == "year"
    assert e["price"] == "8900.00"
    assert e["currency"] == "RUB"
    assert e["status"] == "active"
    assert e["activated_at"] == "2026-01-15T13:00:00+03:00"
    assert e["saved_payment_method"] == "false"
    assert by_user["g"]["price"] == "500"
    assert by_user["d"]["expire_reason"] == "payment_failed"
    # A name typed by a Telegram user must not become a spreadsheet formula.
    assert by_user["j"]["first_name"] == "'" + EVIL_NAME


async def test_payment_export(core: Core) -> None:
    await build_dataset(core)
    async with core.sessions() as session:
        rows = _read_csv(await export_payments(session, MSK))

    assert len(rows) == 16
    assert [row["created_at"] for row in rows] == sorted(row["created_at"] for row in rows)
    unknown = next(row for row in rows if row["outcome"] == "unknown_subscription")
    assert unknown["telegram_id"] == ""
    assert unknown["amount"] == "990.00"
    assert unknown["applied"] == "false"
    failed = [row for row in rows if row["status"] == "failed"]
    assert len(failed) == 7


# --- through the real code paths -------------------------------------------------------------


async def test_stats_follow_real_renewals_failures_and_expiry(
    core: Core, clock: FakeClock, fake: FakeProvider, revoker: FakeRevoker
) -> None:
    await subscribe(core, fake, 101)
    await subscribe(core, fake, 102, card="decline")
    quarterly = await subscribe(core, fake, 103, plan="quarter")
    await club.cancel(core, telegram_id=103, subscription_id=quarterly)

    scheduler = Scheduler(core, revoker)
    # 31 Jan -> 28 Feb: 101 renews, 102 is declined; then retries at +1 and +3 days fail
    # and 102 expires when the grace period closes.
    clock.set(datetime(2026, 2, 28, 9, 0, tzinfo=UTC))
    await scheduler.tick()
    for _ in range(3):
        clock.advance(timedelta(days=1))
        await scheduler.tick()

    report = await club_report(core, days=40)  # 23 January to 3 March
    totals = report.totals
    assert totals.new == 3
    assert totals.renewals == 1
    assert totals.dunning_started == 1
    assert totals.failed_charges == 3
    assert totals.cancellations == 1
    assert totals.churned == totals.churned_unpaid == 1
    assert totals.revenue == {"RUB": Money.from_decimal("5460.00", "RUB")}  # 990*3 + 2490
    now = report.snapshot.total
    assert (now.live, now.active, now.canceled) == (2, 1, 1)
    assert now.mrr == {"RUB": Money.from_decimal("990.00", "RUB")}  # canceled is not MRR
    assert report.days[-1].live == now.live
    assert report.churn_rate == Decimal("0.3333")


async def test_renewals_without_a_saved_card_are_not_counted_as_failed_charges(
    core: Core, clock: FakeClock, fake: FakeProvider, revoker: FakeRevoker
) -> None:
    """A member who paid without saving a card renews with the "pay" button. Each dunning
    attempt records a failure, but nothing was sent to a provider: those are counted on
    their own, and «Неудачные списания» stays about real declines."""
    await subscribe(core, fake, 201, card=None)
    await subscribe(core, fake, 202, card="decline")
    scheduler = Scheduler(core, revoker)
    for moment in (FEB_28_9, FEB_28_9 + timedelta(days=1), FEB_28_9 + timedelta(days=3)):
        clock.set(moment)
        await scheduler.tick()

    totals = (await club_report(core, days=30)).totals
    assert (totals.failed_charges, totals.renewals_not_charged) == (3, 3)
    assert totals.dunning_started == 2
    assert totals.needs_attention == 0


def test_a_disabled_provider_is_not_a_failed_charge_either() -> None:
    counts = Counts()
    for reason in ("provider_not_configured", "no_saved_payment_method", "card_declined"):
        counts.add_payment(
            status=PaymentStatus.FAILED,
            purpose=PaymentPurpose.RENEWAL,
            applied=False,
            for_period=2,
            amount_minor=99_000,
            currency="RUB",
            failure_reason=reason,
        )
    assert (counts.failed_charges, counts.renewals_not_charged) == (1, 2)
