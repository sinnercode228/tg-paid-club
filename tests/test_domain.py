from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from paidclub.domain import subscriptions as d
from paidclub.domain.subscriptions import (
    BillingPolicy,
    ExpireReason,
    InvalidTransition,
    RenewalMode,
    SubState,
    SubStatus,
)

T0 = datetime(2026, 1, 31, 9, 0, tzinfo=UTC)
DAY = timedelta(days=1)
POLICY = BillingPolicy(
    grace=3 * DAY,
    retry_offsets=(1 * DAY, 3 * DAY),
    reminder_offsets=(3 * DAY, 1 * DAY),
    renewal_wait=timedelta(hours=1),
)
MERCHANT = RenewalMode.MERCHANT


def pending(unit: d.PeriodUnit = "month", count: int = 1) -> SubState:
    return SubState(status=SubStatus.PENDING, period_unit=unit, period_count=count)


def paid(state: SubState, now: datetime, mode: RenewalMode = MERCHANT) -> SubState:
    return d.payment_succeeded(state, now=now, policy=POLICY, mode=mode)


# --- period math -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "months", "expected"),
    [
        (datetime(2026, 1, 31, tzinfo=UTC), 1, datetime(2026, 2, 28, tzinfo=UTC)),
        (datetime(2028, 1, 31, tzinfo=UTC), 1, datetime(2028, 2, 29, tzinfo=UTC)),
        (datetime(2026, 1, 31, tzinfo=UTC), 2, datetime(2026, 3, 31, tzinfo=UTC)),
        (datetime(2026, 11, 30, tzinfo=UTC), 3, datetime(2027, 2, 28, tzinfo=UTC)),
        (datetime(2026, 5, 15, 12, 30, tzinfo=UTC), 12, datetime(2027, 5, 15, 12, 30, tzinfo=UTC)),
    ],
)
def test_add_months_clamps_to_the_end_of_shorter_months(
    start: datetime, months: int, expected: datetime
) -> None:
    assert d.add_months(start, months) == expected


def test_monthly_periods_follow_the_anchor_and_do_not_drift() -> None:
    state = paid(pending(), T0)
    ends = [state.current_period_end]
    for _ in range(3):
        assert state.current_period_end is not None
        state = paid(state, state.current_period_end)
        ends.append(state.current_period_end)
    # Feb 28 is a clamp, not a new anchor: March goes back to the 31st.
    assert ends == [
        datetime(2026, 2, 28, 9, 0, tzinfo=UTC),
        datetime(2026, 3, 31, 9, 0, tzinfo=UTC),
        datetime(2026, 4, 30, 9, 0, tzinfo=UTC),
        datetime(2026, 5, 31, 9, 0, tzinfo=UTC),
    ]


def test_first_payment_activates_from_the_payment_moment() -> None:
    state = paid(pending("month", 3), T0)
    assert state.status is SubStatus.ACTIVE
    assert state.periods_paid == 1
    assert state.current_period_start == T0
    assert state.current_period_end == datetime(2026, 4, 30, 9, 0, tzinfo=UTC)
    assert state.next_charge_at == state.current_period_end
    assert state.activated_at == T0


def test_a_late_renewal_extends_from_the_period_end_not_from_now() -> None:
    state = paid(pending(), T0)
    failed = d.charge_failed(
        state, now=state.current_period_end + timedelta(hours=2), policy=POLICY
    )
    late = failed.current_period_end + 2 * DAY  # type: ignore[operator]
    renewed = paid(failed, late)
    assert renewed.status is SubStatus.ACTIVE
    assert renewed.current_period_start == state.current_period_end
    assert renewed.current_period_end == datetime(2026, 3, 31, 9, 0, tzinfo=UTC)
    assert renewed.past_due_since is None
    assert renewed.dunning_attempt == 0


def test_provider_renewed_subscriptions_wait_for_the_payment_before_the_check() -> None:
    state = paid(pending("day", 30), T0, mode=RenewalMode.PROVIDER)
    assert state.current_period_end == T0 + 30 * DAY
    assert state.next_charge_at == T0 + 30 * DAY + timedelta(hours=1)


def test_provider_supplied_period_end_wins() -> None:
    telegram_end = T0 + 30 * DAY + timedelta(minutes=7)
    state = d.payment_succeeded(
        pending(), now=T0, policy=POLICY, mode=RenewalMode.PROVIDER, period_end=telegram_end
    )
    assert state.current_period_end == telegram_end


def test_period_end_before_start_is_rejected() -> None:
    with pytest.raises(ValueError, match="before it starts"):
        d.payment_succeeded(pending(), now=T0, policy=POLICY, mode=MERCHANT, period_end=T0)


# --- dunning -----------------------------------------------------------------------------------


def test_dunning_schedule_retries_after_one_and_three_days_then_stops() -> None:
    state = paid(pending(), T0)
    end = state.current_period_end
    assert end is not None

    first = d.charge_failed(state, now=end, policy=POLICY)
    assert first.status is SubStatus.PAST_DUE
    assert first.past_due_since == end
    assert first.grace_until == end + 3 * DAY
    assert first.dunning_attempt == 1
    assert first.next_charge_at == end + 1 * DAY

    second = d.charge_failed(first, now=end + 1 * DAY, policy=POLICY)
    assert second.dunning_attempt == 2
    assert second.next_charge_at == end + 3 * DAY
    assert second.grace_until == end + 3 * DAY

    third = d.charge_failed(second, now=end + 3 * DAY, policy=POLICY)
    assert third.dunning_attempt == 3
    assert third.next_charge_at is None
    assert third.grace_until == end + 3 * DAY


def test_grace_counts_from_the_failure_when_the_charge_ran_late() -> None:
    state = paid(pending(), T0)
    late = state.current_period_end + timedelta(hours=5)  # type: ignore[operator]
    failed = d.charge_failed(state, now=late, policy=POLICY)
    assert failed.grace_until == late + 3 * DAY
    assert failed.next_charge_at == late + 1 * DAY


def test_without_retries_a_failure_only_opens_the_grace_window() -> None:
    state = paid(pending(), T0, mode=RenewalMode.PROVIDER)
    failed = d.charge_failed(state, now=state.next_charge_at, policy=POLICY, retries=False)  # type: ignore[arg-type]
    assert failed.status is SubStatus.PAST_DUE
    assert failed.next_charge_at is None


def test_expiry_waits_for_a_retry_that_is_due() -> None:
    state = paid(pending(), T0)
    end = state.current_period_end
    assert end is not None
    second = d.charge_failed(
        d.charge_failed(state, now=end, policy=POLICY), now=end + DAY, policy=POLICY
    )
    # The last retry is scheduled exactly at the end of grace: it runs before expiry.
    assert second.next_charge_at == second.grace_until
    assert d.expiry_due(second, end + 3 * DAY) is None
    third = d.charge_failed(second, now=end + 3 * DAY, policy=POLICY)
    assert d.expiry_due(third, end + 3 * DAY) is ExpireReason.PAYMENT_FAILED


def test_past_due_keeps_access_only_during_grace() -> None:
    state = paid(pending(), T0)
    end = state.current_period_end
    assert end is not None
    failed = d.charge_failed(state, now=end, policy=POLICY)
    assert d.has_access(failed, end + 3 * DAY - timedelta(seconds=1))
    assert not d.has_access(failed, end + 3 * DAY)


# --- cancel, resume, expire --------------------------------------------------------------------


def test_cancel_keeps_access_until_the_period_end_and_then_expires() -> None:
    state = paid(pending(), T0)
    end = state.current_period_end
    assert end is not None
    canceled = d.cancel(state, now=T0 + DAY)
    assert canceled.status is SubStatus.CANCELED
    assert canceled.next_charge_at is None
    assert d.has_access(canceled, end - timedelta(seconds=1))
    assert d.expiry_due(canceled, end - timedelta(seconds=1)) is None
    assert d.expiry_due(canceled, end) is ExpireReason.CANCELED
    assert not d.has_access(canceled, end)
    assert d.cancel(canceled, now=T0 + 2 * DAY) == canceled


def test_canceling_while_past_due_ends_access_at_once() -> None:
    state = paid(pending(), T0)
    failed = d.charge_failed(state, now=state.current_period_end, policy=POLICY)  # type: ignore[arg-type]
    now = state.current_period_end + DAY  # type: ignore[operator]
    canceled = d.cancel(failed, now=now)
    assert canceled.status is SubStatus.EXPIRED
    assert canceled.expire_reason is ExpireReason.CANCELED
    assert canceled.canceled_at == now


def test_resume_before_the_end_restores_auto_renew() -> None:
    state = paid(pending(), T0)
    canceled = d.cancel(state, now=T0 + DAY)
    resumed = d.resume(canceled, now=T0 + 2 * DAY, policy=POLICY, mode=MERCHANT)
    assert resumed.status is SubStatus.ACTIVE
    assert resumed.canceled_at is None
    assert resumed.next_charge_at == state.current_period_end


def test_resume_after_the_end_is_refused() -> None:
    state = paid(pending(), T0)
    canceled = d.cancel(state, now=T0 + DAY)
    with pytest.raises(InvalidTransition):
        d.resume(canceled, now=state.current_period_end, policy=POLICY, mode=MERCHANT)  # type: ignore[arg-type]


def test_a_payment_on_a_canceled_subscription_extends_it_but_keeps_auto_renew_off() -> None:
    state = d.cancel(paid(pending(), T0), now=T0 + DAY)
    extended = paid(state, T0 + 2 * DAY)
    assert extended.status is SubStatus.CANCELED
    assert extended.current_period_end == datetime(2026, 3, 31, 9, 0, tzinfo=UTC)
    assert extended.next_charge_at is None


def test_paying_after_expiry_starts_a_new_period_from_the_payment() -> None:
    state = paid(pending(), T0)
    end = state.current_period_end
    assert end is not None
    expired = d.expire(d.cancel(state, now=T0), now=end, reason=ExpireReason.CANCELED)
    back = end + 10 * DAY
    again = paid(expired, back)
    assert again.status is SubStatus.ACTIVE
    assert again.periods_paid == 2
    assert again.anchor_at == back
    assert again.current_period_start == back
    assert again.current_period_end == d.add_months(back, 1)
    assert again.canceled_at is None
    assert again.expired_at is None
    assert again.activated_at == T0


TRANSITIONS: dict[str, Callable[[SubState], SubState]] = {
    "fail": lambda state: d.charge_failed(state, now=T0, policy=POLICY),
    "cancel": lambda state: d.cancel(state, now=T0),
    "expire": lambda state: d.expire(state, now=T0, reason=ExpireReason.CANCELED),
    "resume": lambda state: d.resume(state, now=T0, policy=POLICY, mode=MERCHANT),
}


@pytest.mark.parametrize(
    ("action", "status"),
    [
        ("fail", SubStatus.PENDING),
        ("fail", SubStatus.CANCELED),
        ("fail", SubStatus.EXPIRED),
        ("cancel", SubStatus.PENDING),
        ("cancel", SubStatus.EXPIRED),
        ("expire", SubStatus.PENDING),
        ("expire", SubStatus.EXPIRED),
        ("resume", SubStatus.PAST_DUE),
    ],
)
def test_invalid_transitions_raise(action: str, status: SubStatus) -> None:
    state = replace(paid(pending(), T0), status=status)
    transition = TRANSITIONS[action]
    with pytest.raises(InvalidTransition):
        transition(state)


# --- reminders ---------------------------------------------------------------------------------


def test_reminders_fire_at_three_days_then_one_day() -> None:
    state = paid(pending(), T0)
    end = state.current_period_end
    assert end is not None
    assert d.due_reminder(state, end - 4 * DAY, POLICY) is None
    assert d.due_reminder(state, end - 3 * DAY, POLICY) == 3 * DAY
    assert d.due_reminder(state, end - 2 * DAY, POLICY) == 3 * DAY
    assert d.due_reminder(state, end - DAY, POLICY) == DAY
    assert d.due_reminder(state, end, POLICY) is None


def test_a_worker_that_missed_the_three_day_window_sends_only_the_closest_reminder() -> None:
    state = paid(pending(), T0)
    end = state.current_period_end
    assert end is not None
    assert d.due_reminder(state, end - timedelta(hours=10), POLICY) == DAY


def test_reminders_that_would_fall_before_the_period_start_are_skipped() -> None:
    short = paid(pending("day", 2), T0)
    assert d.due_reminder(short, T0, POLICY) is None
    assert d.due_reminder(short, T0 + DAY + timedelta(minutes=1), POLICY) == DAY


def test_no_reminders_while_past_due() -> None:
    state = paid(pending(), T0)
    failed = d.charge_failed(state, now=state.current_period_end, policy=POLICY)  # type: ignore[arg-type]
    assert d.due_reminder(failed, state.current_period_end, POLICY) is None  # type: ignore[arg-type]


def test_renewal_idempotence_key_names_subscription_period_and_attempt() -> None:
    assert d.renewal_idempotence_key(42, 3, 1) == "paidclub-42-3-1"
    assert len(d.renewal_idempotence_key(2**40, 10_000, 10)) <= 64
