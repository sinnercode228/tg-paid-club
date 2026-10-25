"""Subscription and revenue statistics: what ``/stats``, ``/export`` and the ``/admin`` dashboard
show. Every number is defined here once.

A *window* is the half-open interval ``[start, end)`` of UTC instants. Days and months are
calendar days and months in ``settings.display_timezone`` (Europe/Moscow by default), so "today"
means the owner's today.

**Snapshot** (the club right now, from ``subscriptions``)

``live``
    Subscriptions whose status keeps the member in the club: ``active`` (paid, auto-renew on),
    ``past_due`` (a renewal failed, retries and the grace period are running) and ``canceled``
    (auto-renew off, paid until the period end). A member has at most one live subscription, so
    this is also the number of members.
``MRR``
    Monthly recurring revenue of ``active`` and ``past_due`` subscriptions: the price
    snapshotted on each subscription divided by its length in months (a 30-day period is one
    month, a year is 12, the same rule as :attr:`paidclub.config.PlanConfig.months_equivalent`),
    summed per currency. Canceled subscriptions are left out because they will not pay again.
    Rubles and Telegram Stars are never added together.

**Activity in a window** (from ``subscription_events`` and ``payments``, by ``created_at``)

``new``
    pending → active: a subscription's first payment. A member who comes back after expiry
    through a fresh checkout is a new subscription again.
``reactivated``
    expired → active: a late payment on the same subscription after access ended.
``first_payments`` / ``renewals``
    Applied successful payments for period 1 / for period 2 and later, whoever initiated them:
    our renewal charge, a Telegram Stars auto-renewal, or the member's "pay" button after a
    failed charge (including the payment that reactivates an expired subscription). Every
    applied payment is exactly one of the two.
``dunning_started``
    active → past_due: a renewal that failed (or, for Stars, never arrived).
``recovered``
    past_due → active: paid during the grace period.
``cancellations``
    Auto-renew switched off: → canceled, plus past_due → expired with reason ``canceled``
    (cancelling while past due ends access at once, so there is no canceled step).
``resumed``
    canceled → active: auto-renew switched back on before the period ended.
``churned``
    → expired, split into ``churned_unpaid`` (grace ran out) and ``churned_after_cancel``.
``failed_charges``
    Failed payments with purpose ``renewal``. Every attempt counts, so one subscription in
    dunning can add up to three. A Stars renewal that never arrived leaves no payment and is
    counted only in ``dunning_started``.
``failed_checkouts``
    Failed payments the member made on a checkout page (first payment or the "pay" button).
``revenue``
    Sum of applied successful payments, per currency. Money that arrived but changed nothing
    (a repeat payment for a paid period, a wrong amount, an unknown subscription) is not
    revenue: it is counted in ``needs_attention`` and the owner was alerted to refund it.
    Refunds are not recorded, so this is gross revenue.
``exposed``
    Subscriptions that had access at some point in the window: live at its start, plus
    ``new`` and ``reactivated`` inside it.
``churn_rate``
    ``churned / exposed``. Dividing by the live count at the start alone would put members who
    joined and left inside the window in the numerator only, and a young club would see
    rates above 100 %; this way the rate stays between 0 and 1 for any window.

A payment that was first recorded as failed and later reported as succeeded counts once, as a
success, in the bucket of the day it was first recorded.

Buckets are filled in Python from the window's rows rather than with SQL date functions:
SQLite has no time zone support, and this way the daily rows add up to the totals exactly.
The live count per day is replayed from ``subscription_events`` starting from the status each
subscription had at the window start.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, fields
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paidclub.billing.base import PaymentPurpose
from paidclub.config import PlanConfig
from paidclub.container import Core
from paidclub.domain.subscriptions import LIVE_STATUSES, ExpireReason, SubStatus
from paidclub.models import Member, Payment, PaymentStatus, Subscription, SubscriptionEvent
from paidclub.money import CURRENCY_EXPONENTS, Money

BucketUnit = Literal["day", "month"]

_LIVE = frozenset(LIVE_STATUSES)
#: Statuses whose subscriptions are expected to pay again, and so count towards MRR.
MRR_STATUSES: tuple[SubStatus, ...] = (SubStatus.ACTIVE, SubStatus.PAST_DUE)
#: Longest window the owner can ask for, in days.
MAX_WINDOW_DAYS = 366
_INSTANT = timedelta(microseconds=1)


def _ordered_currencies(currencies: Iterable[str]) -> list[str]:
    known = list(CURRENCY_EXPONENTS)
    return sorted(set(currencies), key=lambda code: (known.index(code), code))


# --- counters ------------------------------------------------------------------------------


@dataclass
class Counts:
    """Activity counters for one window, day, month or plan. See the module docstring."""

    new: int = 0
    reactivated: int = 0
    first_payments: int = 0
    renewals: int = 0
    dunning_started: int = 0
    recovered: int = 0
    cancellations: int = 0
    resumed: int = 0
    churned: int = 0
    churned_unpaid: int = 0
    churned_after_cancel: int = 0
    failed_charges: int = 0
    failed_checkouts: int = 0
    needs_attention: int = 0
    #: Applied revenue in minor units (kopecks, stars) per currency.
    revenue_minor: dict[str, int] = field(default_factory=dict)

    def add_event(
        self, from_status: SubStatus | None, to_status: SubStatus, reason: str | None
    ) -> None:
        if to_status is SubStatus.ACTIVE:
            if from_status is None or from_status is SubStatus.PENDING:
                self.new += 1
            elif from_status is SubStatus.EXPIRED:
                self.reactivated += 1
            elif from_status is SubStatus.PAST_DUE:
                self.recovered += 1
            elif from_status is SubStatus.CANCELED:
                self.resumed += 1
        elif to_status is SubStatus.PAST_DUE:
            self.dunning_started += 1
        elif to_status is SubStatus.CANCELED:
            self.cancellations += 1
        elif to_status is SubStatus.EXPIRED:
            self.churned += 1
            if reason == ExpireReason.CANCELED.value:
                self.churned_after_cancel += 1
                if from_status is not SubStatus.CANCELED:
                    self.cancellations += 1
            else:
                self.churned_unpaid += 1

    def add_payment(
        self,
        *,
        status: PaymentStatus,
        purpose: PaymentPurpose,
        applied: bool,
        for_period: int | None,
        amount_minor: int,
        currency: str,
    ) -> None:
        if status is PaymentStatus.SUCCEEDED:
            if not applied:
                self.needs_attention += 1
                return
            self.revenue_minor[currency] = self.revenue_minor.get(currency, 0) + amount_minor
            if (for_period or 1) > 1:
                self.renewals += 1
            else:
                self.first_payments += 1
        elif purpose is PaymentPurpose.RENEWAL:
            self.failed_charges += 1
        else:
            self.failed_checkouts += 1

    def merge(self, other: Counts) -> None:
        for item in fields(self):
            if item.name == "revenue_minor":
                continue
            setattr(self, item.name, getattr(self, item.name) + getattr(other, item.name))
        for currency, minor in other.revenue_minor.items():
            self.revenue_minor[currency] = self.revenue_minor.get(currency, 0) + minor

    @property
    def payments(self) -> int:
        """Applied successful payments."""
        return self.first_payments + self.renewals

    @property
    def revenue(self) -> dict[str, Money]:
        """Revenue per currency, rubles first; currencies without payments are left out."""
        return {
            code: Money(self.revenue_minor[code], code)
            for code in _ordered_currencies(self.revenue_minor)
        }

    def revenue_in(self, currency: str) -> Money:
        return Money(self.revenue_minor.get(currency, 0), currency)


@dataclass
class Holding:
    """Live subscriptions of the club or of one plan at the moment of the snapshot."""

    active: int = 0
    past_due: int = 0
    canceled: int = 0
    #: Unrounded MRR in minor units per currency; :attr:`mrr` rounds it once, at the end.
    mrr_exact: dict[str, Decimal] = field(default_factory=dict)

    @property
    def live(self) -> int:
        return self.active + self.past_due + self.canceled

    @property
    def mrr(self) -> dict[str, Money]:
        return {
            code: Money(int(self.mrr_exact[code].to_integral_value(ROUND_HALF_EVEN)), code)
            for code in _ordered_currencies(self.mrr_exact)
        }

    def mrr_in(self, currency: str) -> Money:
        exact = self.mrr_exact.get(currency, Decimal(0))
        return Money(int(exact.to_integral_value(ROUND_HALF_EVEN)), currency)

    def add(
        self,
        status: SubStatus,
        count: int,
        *,
        currency: str,
        price_minor: int,
        period_unit: str,
        period_count: int,
    ) -> None:
        if status is SubStatus.ACTIVE:
            self.active += count
        elif status is SubStatus.PAST_DUE:
            self.past_due += count
        elif status is SubStatus.CANCELED:
            self.canceled += count
        if status in MRR_STATUSES:
            monthly = Decimal(price_minor) / months_in_period(period_unit, period_count)
            self.mrr_exact[currency] = self.mrr_exact.get(currency, Decimal(0)) + monthly * count


def whole_units(values: dict[str, Money]) -> dict[str, Money]:
    """Amounts rounded to whole rubles and stars, for showing MRR, which is an estimate."""
    return {
        code: Money.from_decimal(money.to_decimal().quantize(Decimal(1), ROUND_HALF_EVEN), code)
        for code, money in values.items()
    }


def months_in_period(period_unit: str, period_count: int) -> Decimal:
    """Length of a billing period in months: 30 days count as one month."""
    if period_unit == "month":
        return Decimal(period_count)
    return Decimal(period_count) / Decimal(30)


# --- report shapes -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Snapshot:
    at: datetime
    total: Holding
    by_plan: dict[str, Holding]


@dataclass(frozen=True, slots=True)
class Bucket:
    """One day or one month of a series."""

    start: date
    counts: Counts
    #: Live subscriptions at the end of the bucket (at the report end for the current one).
    live: int


@dataclass(frozen=True, slots=True)
class PlanStats:
    code: str
    title: str
    now: Holding
    period: Counts


@dataclass(frozen=True, slots=True)
class Report:
    start: datetime
    end: datetime
    tz: tzinfo
    snapshot: Snapshot
    live_at_start: int
    totals: Counts
    days: list[Bucket]
    plans: list[PlanStats]

    @property
    def exposed(self) -> int:
        """Subscriptions with access at some point in the window."""
        return self.live_at_start + self.totals.new + self.totals.reactivated

    @property
    def churn_rate(self) -> Decimal | None:
        """Churned in the window divided by :attr:`exposed`; ``None`` for an empty club."""
        if self.exposed == 0:
            return None
        return (Decimal(self.totals.churned) / Decimal(self.exposed)).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_EVEN
        )

    @property
    def currencies(self) -> list[str]:
        """Currencies with revenue in the window or MRR now."""
        return _ordered_currencies([*self.totals.revenue_minor, *self.snapshot.total.mrr_exact])


# --- windows -------------------------------------------------------------------------------


def _local_midnight(day: date, tz: tzinfo) -> datetime:
    return datetime.combine(day, time(), tzinfo=tz).astimezone(UTC)


def window(now: datetime, days: int, tz: tzinfo) -> tuple[datetime, datetime]:
    """The last ``days`` calendar days in ``tz`` up to ``now``; today is the first of them."""
    if not 1 <= days <= MAX_WINDOW_DAYS:
        raise ValueError(f"days must be between 1 and {MAX_WINDOW_DAYS}")
    first = now.astimezone(tz).date() - timedelta(days=days - 1)
    return _local_midnight(first, tz), now


def month_window(now: datetime, months: int, tz: tzinfo) -> tuple[datetime, datetime]:
    """The current calendar month in ``tz`` and the ``months - 1`` before it, up to ``now``."""
    if months < 1:
        raise ValueError("months must be positive")
    first = now.astimezone(tz).date().replace(day=1)
    for _ in range(months - 1):
        first = (first - timedelta(days=1)).replace(day=1)
    return _local_midnight(first, tz), now


def _bucket_of(moment: datetime, tz: tzinfo, unit: BucketUnit) -> date:
    day = moment.astimezone(tz).date()
    return day if unit == "day" else day.replace(day=1)


def _next_bucket(start: date, unit: BucketUnit) -> date:
    if unit == "day":
        return start + timedelta(days=1)
    return (start.replace(day=28) + timedelta(days=4)).replace(day=1)


def _bucket_starts(start: datetime, end: datetime, tz: tzinfo, unit: BucketUnit) -> list[date]:
    # The window is half-open: a window ending at midnight does not include the next day.
    current, last = _bucket_of(start, tz, unit), _bucket_of(end - _INSTANT, tz, unit)
    starts = []
    while current <= last:
        starts.append(current)
        current = _next_bucket(current, unit)
    return starts


# --- queries -------------------------------------------------------------------------------


async def live_at(session: AsyncSession, moment: datetime) -> int:
    """Live subscriptions at ``moment``, from the last status event of each before it."""
    ranked = (
        select(
            SubscriptionEvent.to_status.label("status"),
            func.row_number()
            .over(
                partition_by=SubscriptionEvent.subscription_id,
                order_by=(SubscriptionEvent.created_at.desc(), SubscriptionEvent.id.desc()),
            )
            .label("rank"),
        )
        .where(SubscriptionEvent.created_at < moment)
        .subquery()
    )
    count = await session.scalar(
        select(func.count())
        .select_from(ranked)
        .where(ranked.c.rank == 1, ranked.c.status.in_(LIVE_STATUSES))
    )
    return int(count or 0)


async def snapshot(session: AsyncSession, *, at: datetime) -> Snapshot:
    """Live subscriptions and MRR as stored right now (``at`` only labels the result)."""
    rows = await session.execute(
        select(
            Subscription.plan_code,
            Subscription.status,
            Subscription.currency,
            Subscription.price_minor,
            Subscription.period_unit,
            Subscription.period_count,
            func.count(),
        )
        .where(Subscription.status.in_(LIVE_STATUSES))
        .group_by(
            Subscription.plan_code,
            Subscription.status,
            Subscription.currency,
            Subscription.price_minor,
            Subscription.period_unit,
            Subscription.period_count,
        )
    )
    total = Holding()
    by_plan: dict[str, Holding] = {}
    for plan_code, status, currency, price_minor, unit, period_count, count in rows:
        details: dict[str, Any] = {
            "currency": currency,
            "price_minor": price_minor,
            "period_unit": unit,
            "period_count": period_count,
        }
        total.add(status, count, **details)
        by_plan.setdefault(plan_code, Holding()).add(status, count, **details)
    return Snapshot(at=at, total=total, by_plan=by_plan)


@dataclass(frozen=True, slots=True)
class _Series:
    live_at_start: int
    buckets: list[Bucket]
    totals: Counts
    by_plan: dict[str, Counts]


async def _series(
    session: AsyncSession, *, start: datetime, end: datetime, tz: tzinfo, unit: BucketUnit
) -> _Series:
    if end <= start:
        raise ValueError("the window must end after it starts")
    starting_live = await live_at(session, start)
    events = await session.execute(
        select(
            SubscriptionEvent.created_at,
            SubscriptionEvent.from_status,
            SubscriptionEvent.to_status,
            SubscriptionEvent.reason,
            SubscriptionEvent.plan_code,
        )
        .where(SubscriptionEvent.created_at >= start, SubscriptionEvent.created_at < end)
        .order_by(SubscriptionEvent.created_at, SubscriptionEvent.id)
    )
    payments = await session.execute(
        select(
            Payment.created_at,
            Payment.status,
            Payment.purpose,
            Payment.applied,
            Payment.for_period,
            Payment.amount_minor,
            Payment.currency,
            Payment.plan_code,
        ).where(Payment.created_at >= start, Payment.created_at < end)
    )

    starts = _bucket_starts(start, end, tz, unit)
    counts = {bucket: Counts() for bucket in starts}
    live_after: dict[date, int] = {}
    by_plan: dict[str, Counts] = {}
    live = starting_live
    events_iter = iter(events.all())
    pending = next(events_iter, None)
    for bucket in starts:
        boundary = min(_local_midnight(_next_bucket(bucket, unit), tz), end)
        while pending is not None and pending.created_at < boundary:
            change = (pending.from_status, pending.to_status, pending.reason)
            counts[_bucket_of(pending.created_at, tz, unit)].add_event(*change)
            by_plan.setdefault(pending.plan_code, Counts()).add_event(*change)
            live += (pending.to_status in _LIVE) - (pending.from_status in _LIVE)
            pending = next(events_iter, None)
        live_after[bucket] = live

    for payment in payments:
        targets = [counts[_bucket_of(payment.created_at, tz, unit)]]
        if payment.plan_code is not None:
            targets.append(by_plan.setdefault(payment.plan_code, Counts()))
        for target in targets:
            target.add_payment(
                status=payment.status,
                purpose=payment.purpose,
                applied=payment.applied,
                for_period=payment.for_period,
                amount_minor=payment.amount_minor,
                currency=payment.currency,
            )

    totals = Counts()
    for bucket_counts in counts.values():
        totals.merge(bucket_counts)
    buckets = [Bucket(start=b, counts=counts[b], live=live_after[b]) for b in starts]
    return _Series(starting_live, buckets, totals, by_plan)


def _plan_order(plans: Sequence[PlanConfig], codes: Iterable[str]) -> list[tuple[str, str]]:
    """Configured plans in their configured order, then retired plan codes alphabetically."""
    configured = [(plan.code, plan.title) for plan in plans]
    known = {code for code, _ in configured}
    retired = sorted(set(codes) - known)
    return configured + [(code, code) for code in retired]


async def build_report(
    session: AsyncSession,
    *,
    start: datetime,
    end: datetime,
    tz: tzinfo,
    plans: Sequence[PlanConfig],
    now: datetime | None = None,
) -> Report:
    """Daily report for ``[start, end)`` plus a snapshot of the live subscriptions."""
    series = await _series(session, start=start, end=end, tz=tz, unit="day")
    snap = await snapshot(session, at=now or end)
    plan_rows = [
        PlanStats(
            code=code,
            title=title,
            now=snap.by_plan.get(code, Holding()),
            period=series.by_plan.get(code, Counts()),
        )
        for code, title in _plan_order(plans, [*snap.by_plan, *series.by_plan])
    ]
    return Report(
        start=start,
        end=end,
        tz=tz,
        snapshot=snap,
        live_at_start=series.live_at_start,
        totals=series.totals,
        days=series.buckets,
        plans=plan_rows,
    )


async def build_months(
    session: AsyncSession, *, start: datetime, end: datetime, tz: tzinfo
) -> list[Bucket]:
    """Monthly buckets for ``[start, end)``."""
    return (await _series(session, start=start, end=end, tz=tz, unit="month")).buckets


async def club_report(core: Core, *, days: int = 30) -> Report:
    """The report for the last ``days`` days, as ``/stats`` and the dashboard show it.

    The window ends just after "now", so a payment recorded at this very instant is in the
    daily numbers as well as in the snapshot.
    """
    now = core.clock.now()
    start, _ = window(now, days, core.tz)
    end = now + _INSTANT
    async with core.sessions() as session:
        return await build_report(
            session, start=start, end=end, tz=core.tz, plans=core.settings.plans, now=now
        )


async def club_months(core: Core, *, months: int = 12) -> list[Bucket]:
    """The current month and the ``months - 1`` before it, up to and including now."""
    now = core.clock.now()
    start, _ = month_window(now, months, core.tz)
    end = now + _INSTANT
    async with core.sessions() as session:
        return await build_months(session, start=start, end=end, tz=core.tz)


async def payments_needing_attention(session: AsyncSession, *, limit: int = 20) -> list[Payment]:
    """Successful payments that changed nothing (refund candidates), newest first."""
    result = await session.scalars(
        select(Payment)
        .where(Payment.status == PaymentStatus.SUCCEEDED, Payment.applied.is_(False))
        .order_by(Payment.created_at.desc(), Payment.id.desc())
        .limit(limit)
    )
    return list(result.all())


# --- JSON ----------------------------------------------------------------------------------


def _money_map(values: dict[str, Money]) -> dict[str, str]:
    return {code: money.to_decimal_str() for code, money in values.items()}


def counts_to_dict(counts: Counts) -> dict[str, Any]:
    data: dict[str, Any] = {
        item.name: getattr(counts, item.name)
        for item in fields(counts)
        if item.name != "revenue_minor"
    }
    data["payments"] = counts.payments
    data["revenue"] = _money_map(counts.revenue)
    return data


def holding_to_dict(holding: Holding) -> dict[str, Any]:
    return {
        "live": holding.live,
        "active": holding.active,
        "past_due": holding.past_due,
        "canceled": holding.canceled,
        "mrr": _money_map(holding.mrr),
    }


def report_to_dict(report: Report) -> dict[str, Any]:
    """JSON-ready form of a report: amounts as decimal strings, times in ISO 8601 UTC."""
    churn = report.churn_rate
    return {
        "generated_at": report.snapshot.at.astimezone(UTC).isoformat(),
        "start": report.start.astimezone(UTC).isoformat(),
        "end": report.end.astimezone(UTC).isoformat(),
        "timezone": str(report.tz),
        "now": holding_to_dict(report.snapshot.total),
        "live_at_start": report.live_at_start,
        "exposed": report.exposed,
        "churn_rate": None if churn is None else str(churn),
        "totals": counts_to_dict(report.totals),
        "plans": [
            {
                "code": plan.code,
                "title": plan.title,
                "now": holding_to_dict(plan.now),
                "period": counts_to_dict(plan.period),
            }
            for plan in report.plans
        ],
        "days": [
            {"date": day.start.isoformat(), "live": day.live, **counts_to_dict(day.counts)}
            for day in report.days
        ],
    }


# --- CSV export ----------------------------------------------------------------------------

#: A cell starting with one of these is a formula in Excel and LibreOffice. Usernames and names
#: come from Telegram users, so they are neutralised with a leading apostrophe.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: object, tz: tzinfo) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.astimezone(tz).isoformat(timespec="seconds")
    if isinstance(value, StrEnum):
        return value.value
    text = str(value)
    if text.startswith(_FORMULA_START):
        return "'" + text
    return text


def _csv(header: Sequence[str], rows: Iterable[Sequence[object]], tz: tzinfo) -> bytes:
    """RFC 4180 CSV in UTF-8 with a BOM, so spreadsheet apps read Cyrillic correctly."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([_cell(value, tz) for value in row])
    return buffer.getvalue().encode("utf-8-sig")


SUBSCRIPTION_COLUMNS = (
    "subscription_id",
    "telegram_id",
    "username",
    "first_name",
    "plan_code",
    "provider",
    "status",
    "price",
    "currency",
    "period_unit",
    "period_count",
    "periods_paid",
    "saved_payment_method",
    "created_at",
    "activated_at",
    "current_period_start",
    "current_period_end",
    "next_charge_at",
    "grace_until",
    "canceled_at",
    "expired_at",
    "expire_reason",
    "access_revoked_at",
)

PAYMENT_COLUMNS = (
    "payment_id",
    "created_at",
    "occurred_at",
    "provider",
    "provider_payment_id",
    "subscription_id",
    "telegram_id",
    "plan_code",
    "purpose",
    "status",
    "outcome",
    "applied",
    "amount",
    "currency",
    "for_period",
    "attempt",
    "period_start",
    "period_end",
    "failure_reason",
)


async def export_subscriptions(session: AsyncSession, tz: tzinfo) -> bytes:
    """Every subscription with its member, one row each. Times are in ``tz`` with the offset.

    The saved payment method itself is not exported, only whether there is one.
    """
    subs = await session.scalars(select(Subscription).order_by(Subscription.id))
    rows = (
        (
            sub.id,
            sub.member.telegram_id,
            sub.member.username,
            sub.member.first_name,
            sub.plan_code,
            sub.provider,
            sub.status,
            Money(sub.price_minor, sub.currency).to_decimal_str(),
            sub.currency,
            sub.period_unit,
            sub.period_count,
            sub.periods_paid,
            sub.payment_method_id is not None,
            sub.created_at,
            sub.activated_at,
            sub.current_period_start,
            sub.current_period_end,
            sub.next_charge_at,
            sub.grace_until,
            sub.canceled_at,
            sub.expired_at,
            sub.expire_reason,
            sub.access_revoked_at,
        )
        for sub in subs.unique()
    )
    return _csv(SUBSCRIPTION_COLUMNS, rows, tz)


async def export_payments(session: AsyncSession, tz: tzinfo) -> bytes:
    """Every recorded payment, successful or not, oldest first."""
    result = await session.execute(
        select(Payment, Member.telegram_id)
        .outerjoin(Member, Member.id == Payment.member_id)
        .order_by(Payment.created_at, Payment.id)
    )
    rows = (
        (
            payment.id,
            payment.created_at,
            payment.occurred_at,
            payment.provider,
            payment.provider_payment_id,
            payment.subscription_id,
            telegram_id,
            payment.plan_code,
            payment.purpose,
            payment.status,
            payment.outcome,
            payment.applied,
            Money(payment.amount_minor, payment.currency).to_decimal_str(),
            payment.currency,
            payment.for_period,
            payment.attempt,
            payment.period_start,
            payment.period_end,
            payment.failure_reason,
        )
        for payment, telegram_id in result
    )
    return _csv(PAYMENT_COLUMNS, rows, tz)
