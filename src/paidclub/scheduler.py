"""Periodic work, as one :meth:`Scheduler.tick` that is safe to run from several workers.

A tick does, in this order:

1. **Renewals.** Claims subscriptions whose ``next_charge_at`` has come (regular renewal or a
   dunning retry) and charges them. The charge attempt row, keyed by (subscription, period,
   attempt), is committed *before* the provider call; if the worker dies mid-call the lease
   runs out, another worker re-claims the subscription and repeats the call with the same
   idempotence key, which the provider deduplicates. The result goes through
   :func:`paidclub.payments.apply_payment_event` like any webhook. Provider-renewed
   subscriptions (Stars) are not charged: if the renewal payment has not arrived by
   ``next_charge_at`` they go past due without retries.
2. **Expiry.** Past-due subscriptions whose grace window closed (and that have no retry due)
   and canceled subscriptions whose paid period ended become ``expired``.
3. **Access.** Members of expired subscriptions are removed from the club chats.
4. **Reminders.** Before a renewal or the end of access, at each configured offset.

Renewals run before expiry on purpose: a last retry scheduled exactly at the end of the grace
window gets its chance before the subscription expires.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from paidclub import texts
from paidclub.access import AccessRevoker, revoke_expired_access
from paidclub.billing.base import (
    ChargeResult,
    PaymentFailed,
    PaymentPending,
    PaymentPurpose,
    RenewalRequest,
)
from paidclub.container import Core
from paidclub.db import claim, in_transaction, release
from paidclub.domain import subscriptions as domain
from paidclub.domain.subscriptions import RenewalMode, SubStatus
from paidclub.models import ChargeAttempt, ChargeAttemptStatus, PaymentOutcome, Subscription
from paidclub.notifications import MessageDraft, enqueue
from paidclub.payments import apply_payment_event, notify_renewal_failed
from paidclub.repo import price_of, state_of, write_state

log = logging.getLogger(__name__)

_APPLIED = PaymentOutcome.APPLIED


@dataclass
class TickReport:
    charged: int = 0
    renewed: int = 0
    charge_failed: int = 0
    pending: int = 0
    provider_errors: int = 0
    renewals_missing: int = 0
    expired: int = 0
    removed: int = 0
    revoke_errors: int = 0
    reminders: int = 0
    errors: int = 0


@dataclass(frozen=True, slots=True)
class _Charge:
    """What phase 1 of a renewal decided, carried outside the transaction."""

    attempt_id: int
    provider: str
    request: RenewalRequest


def reminder_key(subscription_id: int, period_end: datetime, offset: timedelta) -> str:
    seconds = int(offset.total_seconds())
    label = f"{seconds // 86400}d" if seconds % 86400 == 0 else f"{seconds}s"
    return f"reminder:{subscription_id}:{period_end.astimezone(UTC):%Y%m%dT%H%M%SZ}:{label}"


class Scheduler:
    def __init__(
        self, core: Core, revoker: AccessRevoker, *, batch_size: int = 50, max_batches: int = 100
    ) -> None:
        self.core = core
        self.revoker = revoker
        self.batch_size = batch_size
        self.max_batches = max_batches

    async def tick(self) -> TickReport:
        report = TickReport()
        for step in (self._renewals, self._expire, self._revoke, self._remind):
            try:
                await step(report)
            except Exception:
                report.errors += 1
                log.exception("scheduler step %s failed", step.__name__)
        return report

    async def run(self, stop: asyncio.Event, *, interval: float | None = None) -> None:
        interval = interval or self.core.settings.scheduler_interval_seconds
        while not stop.is_set():
            report = await self.tick()
            if any(vars(report).values()):
                log.info("tick: %s", {k: v for k, v in vars(report).items() if v})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)

    # --- 1. renewals -----------------------------------------------------------------------

    async def _renewals(self, report: TickReport) -> None:
        core = self.core
        for _ in range(self.max_batches):
            now = core.clock.now()
            async with core.sessions() as session, session.begin():
                token, ids = await claim(
                    session,
                    Subscription,
                    Subscription.status.in_([SubStatus.ACTIVE, SubStatus.PAST_DUE]),
                    Subscription.next_charge_at.is_not(None),
                    Subscription.next_charge_at <= now,
                    now=now,
                    lease=core.settings.claim_lease,
                    limit=self.batch_size,
                    order_by=Subscription.next_charge_at,
                )
            if not ids:
                return
            for sub_id in ids:
                try:
                    await self._renew_one(sub_id, token, report)
                except Exception:
                    report.errors += 1
                    log.exception("renewal of subscription %s failed", sub_id)
                    await self._release(sub_id, token, backoff=True)

    async def _release(self, sub_id: int, token: str, *, backoff: bool) -> None:
        until = None
        if backoff:
            until = self.core.clock.now() + timedelta(
                seconds=self.core.settings.provider_retry_seconds
            )
        async with self.core.sessions() as session, session.begin():
            await release(session, Subscription, sub_id, token, until=until)

    async def _renew_one(self, sub_id: int, token: str, report: TickReport) -> None:
        core = self.core

        async def prepare(session: AsyncSession) -> _Charge | None:
            now = core.clock.now()
            sub = await session.get(Subscription, sub_id, populate_existing=True)
            if sub is None or sub.claim_token != token:
                return None
            due = (
                sub.status in (SubStatus.ACTIVE, SubStatus.PAST_DUE)
                and sub.next_charge_at is not None
                and sub.next_charge_at <= now
            )
            if not due:
                sub.claim_token, sub.claimed_until = None, None
                return None
            if core.providers.renewal_mode(sub.provider) is RenewalMode.PROVIDER:
                # Telegram charges Stars renewals itself; the payment should be here by now.
                attempt = sub.dunning_attempt
                state = domain.charge_failed(
                    state_of(sub), now=now, policy=core.policy, retries=False
                )
                write_state(sub, state, now=now)
                sub.claim_token, sub.claimed_until = None, None
                await notify_renewal_failed(session, core, sub, attempt=attempt, now=now)
                report.renewals_missing += 1
                return None

            for_period, attempt = sub.periods_paid + 1, sub.dunning_attempt
            key = domain.renewal_idempotence_key(sub.id, for_period, attempt)
            row = (
                await session.scalars(
                    select(ChargeAttempt).where(ChargeAttempt.idempotence_key == key)
                )
            ).one_or_none()
            if row is None:
                row = ChargeAttempt(
                    subscription_id=sub.id,
                    for_period=for_period,
                    attempt=attempt,
                    idempotence_key=key,
                    status=ChargeAttemptStatus.STARTED,
                    calls=0,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            elif row.status in (ChargeAttemptStatus.SUCCEEDED, ChargeAttemptStatus.FAILED):
                # The provider already answered this exact request, but the answer did not
                # move the subscription (an amount mismatch, say; the owner has been alerted).
                # Asking again would get the same answer, so back off instead of looping.
                log.error(
                    "subscription %s: charge %s already %s but the renewal is still due",
                    sub.id,
                    key,
                    row.status.value,
                )
                report.errors += 1
                sub.claim_token = None
                sub.claimed_until = now + timedelta(seconds=core.settings.provider_retry_seconds)
                return None
            row.calls += 1
            row.updated_at = now
            await session.flush()
            plan = core.settings.plan(sub.plan_code)
            return _Charge(
                attempt_id=row.id,
                provider=sub.provider,
                request=RenewalRequest(
                    subscription_id=sub.id,
                    member_telegram_id=sub.member.telegram_id,
                    plan_code=sub.plan_code,
                    amount=price_of(sub),
                    for_period=for_period,
                    attempt=attempt,
                    payment_method_id=sub.payment_method_id,
                    idempotence_key=key,
                    description=f"Продление подписки «{plan.title if plan else sub.plan_code}»",
                ),
            )

        charge = await in_transaction(core.sessions, prepare)
        if charge is None:
            return
        request = charge.request
        provider = core.providers.get(charge.provider)
        report.charged += 1
        result: ChargeResult
        if provider is None:
            # The subscription's provider was disabled in settings: renewals cannot happen, so
            # the member goes through dunning like any other failed charge.
            result = PaymentFailed(
                provider=charge.provider,
                provider_payment_id=f"unavailable:{request.idempotence_key}",
                subscription_id=sub_id,
                amount=request.amount,
                occurred_at=core.clock.now(),
                purpose=PaymentPurpose.RENEWAL,
                reason="provider_not_configured",
                for_period=request.for_period,
                attempt=request.attempt,
            )
        else:
            try:
                result = await provider.charge_renewal(request)
            except Exception as exc:
                # Outcome unknown (timeout, 5xx, a bug): keep the attempt row and retry later
                # with the same idempotence key.
                report.provider_errors += 1
                log.warning("charge for subscription %s: %s", sub_id, exc)
                await self._record_attempt_error(charge.attempt_id, f"{type(exc).__name__}: {exc}")
                await self._release(sub_id, token, backoff=True)
                return

        async def settle(session: AsyncSession) -> PaymentOutcome | None:
            now = core.clock.now()
            attempt = await session.get(ChargeAttempt, charge.attempt_id, populate_existing=True)
            assert attempt is not None
            attempt.provider_payment_id = result.provider_payment_id
            attempt.updated_at = now
            if isinstance(result, PaymentPending):
                attempt.status = ChargeAttemptStatus.PENDING
                recheck = now + timedelta(seconds=core.settings.pending_recheck_seconds)
                await release(session, Subscription, sub_id, token, until=recheck)
                return None
            await session.flush()
            applied = await apply_payment_event(session, result, core)
            sub = await session.get(Subscription, sub_id)
            if sub is not None and sub.claim_token == token:
                sub.claim_token = None
                # An answer that could not be applied leaves the renewal due; do not pick it
                # up again in this same tick.
                retry = timedelta(seconds=core.settings.provider_retry_seconds)
                sub.claimed_until = None if applied.outcome is _APPLIED else now + retry
            return applied.outcome

        outcome = await in_transaction(core.sessions, settle)
        if isinstance(result, PaymentPending):
            report.pending += 1
        elif outcome is not _APPLIED and outcome is not PaymentOutcome.DUPLICATE:
            report.errors += 1
            log.error("renewal of subscription %s not applied: %s", sub_id, outcome)
        elif isinstance(result, PaymentFailed):
            report.charge_failed += 1
        else:
            report.renewed += 1

    async def _record_attempt_error(self, attempt_id: int, error: str) -> None:
        async with self.core.sessions() as session, session.begin():
            await session.execute(
                update(ChargeAttempt)
                .where(ChargeAttempt.id == attempt_id)
                .values(last_error=error[:2000], updated_at=self.core.clock.now())
            )

    # --- 2. expiry -------------------------------------------------------------------------

    async def _expire(self, report: TickReport) -> None:
        core = self.core
        for _ in range(self.max_batches):
            now = core.clock.now()
            due = or_(
                and_(
                    Subscription.status == SubStatus.PAST_DUE,
                    Subscription.grace_until <= now,
                    or_(Subscription.next_charge_at.is_(None), Subscription.next_charge_at > now),
                ),
                and_(
                    Subscription.status == SubStatus.CANCELED,
                    Subscription.current_period_end <= now,
                ),
            )
            async with core.sessions() as session:
                ids = list(
                    (
                        await session.scalars(
                            select(Subscription.id)
                            .where(due)
                            .order_by(Subscription.id)
                            .limit(self.batch_size)
                        )
                    ).all()
                )
            if not ids:
                return
            for sub_id in ids:
                if await in_transaction(core.sessions, self._expire_one(sub_id)):
                    report.expired += 1

    def _expire_one(self, sub_id: int) -> Callable[[AsyncSession], Awaitable[bool]]:
        core = self.core

        async def work(session: AsyncSession) -> bool:
            now = core.clock.now()
            sub = await session.get(Subscription, sub_id, populate_existing=True)
            if sub is None:
                return False
            reason = domain.expiry_due(state_of(sub), now)
            if reason is None:
                return False
            write_state(sub, domain.expire(state_of(sub), now=now, reason=reason), now=now)
            await enqueue(
                session,
                key=f"expired:{sub.id}:{sub.periods_paid}",
                kind="access_ended",
                chat_id=sub.member.telegram_id,
                member_id=sub.member_id,
                message=texts.access_ended(reason=reason),
                now=now,
            )
            return True

        return work

    # --- 3. access -------------------------------------------------------------------------

    async def _revoke(self, report: TickReport) -> None:
        result = await revoke_expired_access(
            self.core, self.revoker, batch_size=self.batch_size, max_batches=self.max_batches
        )
        report.removed += result.removed
        report.revoke_errors += result.errors

    # --- 4. reminders ----------------------------------------------------------------------

    async def _remind(self, report: TickReport) -> None:
        core = self.core
        policy = core.policy
        if not policy.reminder_offsets:
            return
        now = core.clock.now()
        horizon = now + max(policy.reminder_offsets)
        last_id = 0
        while True:
            async with core.sessions() as session, session.begin():
                subs = (
                    await session.scalars(
                        select(Subscription)
                        .where(
                            Subscription.status.in_([SubStatus.ACTIVE, SubStatus.CANCELED]),
                            Subscription.current_period_end > now,
                            Subscription.current_period_end <= horizon,
                            Subscription.id > last_id,
                        )
                        .order_by(Subscription.id)
                        .limit(self.batch_size)
                    )
                ).all()
                if not subs:
                    return
                last_id = subs[-1].id
                for sub in subs:
                    offset = domain.due_reminder(state_of(sub), now, policy)
                    if offset is None:
                        continue
                    assert sub.current_period_end is not None
                    inserted = await enqueue(
                        session,
                        key=reminder_key(sub.id, sub.current_period_end, offset),
                        kind="reminder",
                        chat_id=sub.member.telegram_id,
                        member_id=sub.member_id,
                        message=self._reminder_text(sub, offset),
                        now=now,
                        not_after=sub.current_period_end,
                    )
                    if inserted:
                        report.reminders += 1

    def _reminder_text(self, sub: Subscription, offset: timedelta) -> MessageDraft:
        core = self.core
        end = sub.current_period_end
        assert end is not None
        if sub.status is SubStatus.CANCELED:
            return texts.reminder_canceled(
                subscription_id=sub.id, period_end=end, offset=offset, tz=core.tz
            )
        if core.providers.renewal_mode(sub.provider) is RenewalMode.PROVIDER:
            template = texts.reminder_stars
        elif sub.payment_method_id is None:
            template = texts.reminder_manual
        else:
            template = texts.reminder_auto_renew
        return template(
            subscription_id=sub.id, amount=price_of(sub), period_end=end, offset=offset, tz=core.tz
        )
