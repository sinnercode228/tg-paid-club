"""Subscription state machine, period math and dunning schedule, as pure functions.

Nothing here touches the database, the clock or a provider: every function takes the current
state plus "now" and returns the next state, so the rules can be read and tested on their own.

States::

    pending ──paid──> active ──charge failed──> past_due ──paid──> active
                        │  ^                        │
                 cancel │  │ resume                 └─grace over, no retries left──> expired
                        v  │
                     canceled ──period end──> expired

    expired ──paid (late payment or the "pay" button)──> active, starting from the payment
    expired (after a cancel) ──renewal charge sent before the cancel──> canceled, next period
    canceled ──Telegram renewed after the cancel (member turned it back on)──> active

Periods are computed from a billing anchor, never from "now": the n-th period after the anchor
ends at ``anchor + n * plan period``. Paying late does not shift the schedule, and month ends
do not drift (a subscription anchored on 31 January renews on 28/29 February, then 31 March).
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

PeriodUnit = Literal["month", "day"]


class SubStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    EXPIRED = "expired"


#: Statuses in which a subscription still belongs to the member. At most one per member.
LIVE_STATUSES: tuple[SubStatus, ...] = (SubStatus.ACTIVE, SubStatus.PAST_DUE, SubStatus.CANCELED)


class RenewalMode(StrEnum):
    #: We charge a saved payment method ourselves (YooKassa, the fake provider). Without one
    #: (a card that was not saved, or crypto, which never has one) the renewal fails into
    #: dunning and the member pays with the "pay" button.
    MERCHANT = "merchant"
    #: The provider charges on its own schedule and tells us afterwards (Telegram Stars).
    PROVIDER = "provider"


class ExpireReason(StrEnum):
    PAYMENT_FAILED = "payment_failed"
    CANCELED = "canceled"


class InvalidTransition(Exception):
    def __init__(self, action: str, status: SubStatus) -> None:
        super().__init__(f"cannot {action} a subscription in status {status.value!r}")
        self.action = action
        self.status = status


@dataclass(frozen=True, slots=True)
class BillingPolicy:
    #: How long access survives a failed renewal while retries run.
    grace: timedelta
    #: Retry offsets measured from the first failed charge, ascending.
    retry_offsets: tuple[timedelta, ...]
    #: Reminder offsets before the period end, largest first.
    reminder_offsets: tuple[timedelta, ...]
    #: For provider-renewed subscriptions: how long to wait for the renewal payment.
    renewal_wait: timedelta = timedelta(0)


@dataclass(frozen=True, slots=True)
class SubState:
    status: SubStatus
    period_unit: PeriodUnit
    period_count: int
    anchor_at: datetime | None = None
    #: ``periods_paid`` at the moment of the anchor; periods are counted from there.
    anchor_periods: int = 0
    periods_paid: int = 0
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None
    #: When the scheduler should act next: charge (merchant) or check for the renewal (provider).
    next_charge_at: datetime | None = None
    #: 0 = the regular renewal charge, 1.. = dunning retries.
    dunning_attempt: int = 0
    past_due_since: datetime | None = None
    grace_until: datetime | None = None
    activated_at: datetime | None = None
    canceled_at: datetime | None = None
    expired_at: datetime | None = None
    expire_reason: ExpireReason | None = None


# --- period math -------------------------------------------------------------------------------


def add_months(moment: datetime, months: int) -> datetime:
    """Calendar-month addition that clamps to the last day of a shorter month."""
    month_index = moment.month - 1 + months
    year = moment.year + month_index // 12
    month = month_index % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def add_period(moment: datetime, unit: PeriodUnit, count: int) -> datetime:
    if count < 0:
        raise ValueError("period count must not be negative")
    if unit == "month":
        return add_months(moment, count)
    return moment + timedelta(days=count)


def period_end_after(state: SubState, periods_paid: int) -> datetime:
    """End of the period that ``periods_paid`` payments cover, counted from the anchor."""
    if state.anchor_at is None:
        raise ValueError("subscription has no billing anchor yet")
    periods = periods_paid - state.anchor_periods
    return add_period(state.anchor_at, state.period_unit, state.period_count * periods)


def renewal_idempotence_key(subscription_id: int, for_period: int, attempt: int) -> str:
    """Stable key for one charge attempt; a retried request after a crash reuses it."""
    return f"paidclub-{subscription_id}-{for_period}-{attempt}"


# --- transitions -------------------------------------------------------------------------------


def _next_check(end: datetime, mode: RenewalMode, policy: BillingPolicy) -> datetime:
    return end if mode is RenewalMode.MERCHANT else end + policy.renewal_wait


def payment_succeeded(
    state: SubState,
    *,
    now: datetime,
    policy: BillingPolicy,
    mode: RenewalMode,
    period_end: datetime | None = None,
    renewal: bool = False,
    paid_at: datetime | None = None,
) -> SubState:
    """Apply a successful payment for the next unpaid period (``periods_paid + 1``).

    ``period_end`` is set when the provider is authoritative about the period (Telegram Stars
    sends ``subscription_expiration_date``); otherwise the end follows from the anchor.

    ``renewal`` marks a renewal charge rather than a payment the member made on a checkout
    page. A renewal charge can settle after the member turned auto-renew off: it was sent
    before the cancel, or it was still pending. It pays the period after the last paid one,
    and auto-renew stays off, also when the subscription has expired meanwhile.

    A provider-renewed subscription is the exception: the cancel stopped the renewal at the
    provider, so a renewal the provider charged after it (``paid_at``) means the member turned
    renewal back on there, and auto-renew is on again.
    """
    paid = state.periods_paid + 1
    late_after_cancel = (
        renewal
        and state.status is SubStatus.EXPIRED
        and state.expire_reason is ExpireReason.CANCELED
    )
    restarted = state.status is SubStatus.PENDING or (
        state.status is SubStatus.EXPIRED and not late_after_cancel
    )
    if restarted:
        # A first payment, or a payment after access already ended: the new period starts now.
        anchored = replace(state, anchor_at=now, anchor_periods=state.periods_paid)
        start = now
    elif state.status in LIVE_STATUSES or late_after_cancel:
        anchored = state
        assert state.current_period_end is not None
        start = state.current_period_end
    else:  # pragma: no cover - exhaustive over SubStatus
        raise InvalidTransition("pay", state.status)

    end = period_end if period_end is not None else period_end_after(anchored, paid)
    if end <= start:
        raise ValueError(f"paid period would end ({end}) before it starts ({start})")

    reenabled = (
        renewal
        and mode is RenewalMode.PROVIDER
        and state.canceled_at is not None
        and paid_at is not None
        and paid_at >= state.canceled_at
    )
    keeps_canceled = (state.status is SubStatus.CANCELED or late_after_cancel) and not reenabled
    return replace(
        anchored,
        status=SubStatus.CANCELED if keeps_canceled else SubStatus.ACTIVE,
        periods_paid=paid,
        current_period_start=start,
        current_period_end=end,
        next_charge_at=None if keeps_canceled else _next_check(end, mode, policy),
        dunning_attempt=0,
        past_due_since=None,
        grace_until=None,
        activated_at=state.activated_at or now,
        canceled_at=None if restarted or reenabled else state.canceled_at,
        expired_at=None,
        expire_reason=None,
    )


def charge_failed(
    state: SubState, *, now: datetime, policy: BillingPolicy, retries: bool = True
) -> SubState:
    """A renewal charge (attempt ``state.dunning_attempt``) failed, or never arrived.

    The first failure opens the grace window and schedules retries relative to itself; later
    failures keep that window and move on to the next retry. ``retries=False`` is for
    provider-renewed subscriptions, where there is nothing for us to retry.
    """
    if state.status is SubStatus.ACTIVE:
        assert state.current_period_end is not None
        since = max(now, state.current_period_end)
        grace_until = since + policy.grace
        attempt = 1
    elif state.status is SubStatus.PAST_DUE:
        assert state.past_due_since is not None
        since = state.past_due_since
        grace_until = state.grace_until or since + policy.grace
        attempt = state.dunning_attempt + 1
    else:
        raise InvalidTransition("fail a renewal of", state.status)

    next_retry: datetime | None = None
    if retries and attempt - 1 < len(policy.retry_offsets):
        next_retry = since + policy.retry_offsets[attempt - 1]
    return replace(
        state,
        status=SubStatus.PAST_DUE,
        past_due_since=since,
        grace_until=grace_until,
        dunning_attempt=attempt,
        next_charge_at=next_retry,
    )


def renewal_deadline(state: SubState, policy: BillingPolicy) -> datetime:
    """Until when a renewal charge whose outcome is unknown is waited for.

    That is the end of the grace period the member would get had the charge failed: the
    period end plus grace for the regular renewal, the grace window for a dunning retry.
    """
    if state.status is SubStatus.ACTIVE:
        assert state.current_period_end is not None
        return state.current_period_end + policy.grace
    if state.status is SubStatus.PAST_DUE:
        assert state.grace_until is not None
        return state.grace_until
    raise InvalidTransition("wait for a renewal of", state.status)


def charge_unsettled(state: SubState, *, policy: BillingPolicy) -> SubState:
    """Stop waiting for a renewal charge that is still undecided at its deadline.

    The provider kept timing out, or the charge stayed pending. No further charge is
    scheduled: if the first one went through after all, a retry would charge twice. The grace
    window counts from the period end, as if the charge had failed there, so the subscription
    expires on the next pass; a late success still applies like any late payment.
    """
    if state.status is SubStatus.ACTIVE:
        assert state.current_period_end is not None
        since = state.current_period_end
        return replace(
            state,
            status=SubStatus.PAST_DUE,
            past_due_since=since,
            grace_until=since + policy.grace,
            dunning_attempt=state.dunning_attempt + 1,
            next_charge_at=None,
        )
    if state.status is SubStatus.PAST_DUE:
        return replace(state, next_charge_at=None)
    raise InvalidTransition("give up a renewal of", state.status)


def cancel(state: SubState, *, now: datetime) -> SubState:
    """Turn auto-renew off. Paid time is kept; a past-due subscription has none left."""
    if state.status is SubStatus.CANCELED:
        return state
    if state.status is SubStatus.ACTIVE:
        return replace(state, status=SubStatus.CANCELED, canceled_at=now, next_charge_at=None)
    if state.status is SubStatus.PAST_DUE:
        return expire(replace(state, canceled_at=now), now=now, reason=ExpireReason.CANCELED)
    raise InvalidTransition("cancel", state.status)


def resume(state: SubState, *, now: datetime, policy: BillingPolicy, mode: RenewalMode) -> SubState:
    """Turn auto-renew back on before the paid period ends."""
    if state.status is SubStatus.ACTIVE:
        return state
    if state.status is not SubStatus.CANCELED:
        raise InvalidTransition("resume", state.status)
    assert state.current_period_end is not None
    if state.current_period_end <= now:
        raise InvalidTransition("resume", state.status)
    return replace(
        state,
        status=SubStatus.ACTIVE,
        canceled_at=None,
        next_charge_at=_next_check(state.current_period_end, mode, policy),
    )


def expire(state: SubState, *, now: datetime, reason: ExpireReason) -> SubState:
    if state.status not in LIVE_STATUSES:
        raise InvalidTransition("expire", state.status)
    return replace(
        state,
        status=SubStatus.EXPIRED,
        expired_at=now,
        expire_reason=reason,
        next_charge_at=None,
        dunning_attempt=0,
    )


# --- questions the scheduler and the bot ask ---------------------------------------------------


def expiry_due(
    state: SubState, now: datetime, *, renewal_wait: timedelta = timedelta(0)
) -> ExpireReason | None:
    """Why the subscription should expire now, or ``None`` if it should not.

    A past-due subscription whose last retry is due right now is not expired yet: the retry
    runs first (it may succeed), and only then does the grace window close.

    ``renewal_wait`` is for provider-renewed subscriptions: a canceled one stays until the
    renewal the member may have turned back on at the provider has had time to arrive.
    """
    if state.status is SubStatus.CANCELED:
        assert state.current_period_end is not None
        ended = state.current_period_end + renewal_wait <= now
        return ExpireReason.CANCELED if ended else None
    if state.status is SubStatus.PAST_DUE:
        assert state.grace_until is not None
        retry_pending = state.next_charge_at is not None and state.next_charge_at <= now
        if state.grace_until <= now and not retry_pending:
            return ExpireReason.PAYMENT_FAILED
    return None


def has_access(state: SubState, now: datetime) -> bool:
    if state.status is SubStatus.ACTIVE:
        # An active subscription past its end is waiting for the renewal charge; the scheduler
        # moves it to past_due (with grace) or extends it, so it keeps access meanwhile.
        return True
    if state.status is SubStatus.PAST_DUE:
        return state.grace_until is not None and now < state.grace_until
    if state.status is SubStatus.CANCELED:
        return state.current_period_end is not None and now < state.current_period_end
    return False


def due_reminder(state: SubState, now: datetime, policy: BillingPolicy) -> timedelta | None:
    """The reminder offset to send now, if any.

    Only the closest offset whose moment has passed is returned, so a worker that was down
    during the 3-day window sends the 1-day reminder instead of both. Offsets that would fall
    on or before the period start (a 1-day plan with a 3-day reminder) are skipped.
    """
    if state.status not in (SubStatus.ACTIVE, SubStatus.CANCELED):
        return None
    end, start = state.current_period_end, state.current_period_start
    if end is None or start is None or now >= end:
        return None
    due = [
        offset for offset in policy.reminder_offsets if end - offset <= now and end - offset > start
    ]
    return min(due) if due else None
