"""Periodic work, as one :meth:`Scheduler.tick` that is safe to run from several workers.

A tick does, in this order:

1. **Renewals.** Claims subscriptions whose ``next_charge_at`` has come (regular renewal or a
   dunning retry) and charges them. The charge attempt row, keyed by (subscription, period,
   attempt), is committed *before* the provider call; if the worker dies mid-call the lease
   runs out, another worker re-claims the subscription and repeats the call with the same
   idempotence key, which the provider deduplicates. The result goes through
   :func:`paidclub.payments.apply_payment_event` like any webhook. Provider-renewed
   subscriptions (Stars) are not charged: if the renewal payment has not arrived by
   ``next_charge_at`` they go past due without retries. Crypto has nothing to charge: its
   provider answers ``no_saved_payment_method`` at once, the subscription goes past due, and
   each dunning step sends the member the "pay" button again.

   A charge whose outcome stays unknown (the provider keeps timing out) is repeated with the
   same key, and the owner is told after :data:`ALERT_AFTER_CALLS` tries. At the end of the
   grace period the member would have had if it had failed, the subscription stops waiting
   for it (:func:`~paidclub.domain.subscriptions.charge_unsettled`): no retry is charged, and
   access ends like after a failed renewal.
2. **Pending charges and checkouts.** A charge the provider reported as pending is looked up
   by its payment id every ``pending_recheck_seconds`` until the provider decides, then
   applied. This runs for the charge, not the subscription, so a charge that settles after
   the member cancelled is still recorded. Checkout payments whose notification may have
   been lost are looked up the same way (:mod:`paidclub.checkouts`), and Telegram Stars
   payments that were stored but failed to apply are applied again
   (:func:`paidclub.billing.stars.retry_stored_payments`). The owner's TON wallet is read
   once, if a USDT checkout is waiting, and its transfers are matched to checkouts by comment
   (:func:`paidclub.ton_payments.poll_ton_wallet`).
3. **Expiry.** Past-due subscriptions whose grace window closed (and that have no retry due)
   and canceled subscriptions whose paid period ended become ``expired``.
4. **Access.** Members of expired subscriptions are removed from the club chats.
5. **Reminders.** Before a renewal or the end of access, at each configured offset.

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
from paidclub.billing import MEMBER_RENEWED
from paidclub.billing.base import (
    PROVIDER_NOT_CONFIGURED,
    ChargeResult,
    PaymentFailed,
    PaymentPending,
    PaymentPurpose,
    RenewalRequest,
)
from paidclub.billing.stars import retry_stored_payments
from paidclub.checkouts import reconcile_checkouts
from paidclub.container import Core
from paidclub.db import claim, in_transaction, release
from paidclub.domain import subscriptions as domain
from paidclub.domain.subscriptions import RenewalMode, SubStatus
from paidclub.models import ChargeAttempt, ChargeAttemptStatus, PaymentOutcome, Subscription
from paidclub.notifications import MessageDraft, enqueue
from paidclub.payments import alert_owner, apply_payment_event, notify_renewal_failed
from paidclub.repo import price_of, state_of, write_state
from paidclub.ton_payments import poll_ton_wallet

log = logging.getLogger(__name__)

_APPLIED = PaymentOutcome.APPLIED
#: The owner is told once a charge has been tried this many times without an answer.
ALERT_AFTER_CALLS = 3
#: However early the deadline, an undecided charge gets at least this long.
UNSETTLED_PATIENCE = timedelta(hours=6)


@dataclass
class TickReport:
    charged: int = 0
    renewed: int = 0
    charge_failed: int = 0
    pending: int = 0
    provider_errors: int = 0
    #: Pending charges looked up at the provider.
    pending_checked: int = 0
    #: Renewals whose charge stayed undecided until its deadline.
    unsettled: int = 0
    #: Checkout payments looked up, and those of them applied by the lookup.
    checkouts_checked: int = 0
    checkouts_recovered: int = 0
    #: Stored Telegram Stars payments applied on a retry.
    stars_applied: int = 0
    #: Requests to toncenter (at most one a pass), new USDT transfers found, and the TON
    #: checkouts they completed.
    ton_requests: int = 0
    ton_transfers: int = 0
    ton_paid: int = 0
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
        for step in (
            self._renewals,
            self._pending_charges,
            self._checkouts,
            self._stars_payments,
            self._ton_payments,
            self._expire,
            self._revoke,
            self._remind,
        ):
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
            if row is not None and (row.calls > 0 or row.status is not ChargeAttemptStatus.STARTED):
                # An earlier request for this charge has not moved the subscription on.
                deadline = max(
                    domain.renewal_deadline(state_of(sub), core.policy),
                    row.created_at + UNSETTLED_PATIENCE,
                )
                if now >= deadline:
                    await self._give_up(session, sub, row, now)
                    report.unsettled += 1
                    return None
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
            elif row.status is ChargeAttemptStatus.PENDING:
                # The provider has the charge and has not decided; step 2 asks it, so the
                # request is not sent again.
                if row.next_check_at is None:
                    row.next_check_at = now
                sub.claim_token = None
                sub.claimed_until = max(
                    row.next_check_at, now + timedelta(seconds=core.settings.provider_retry_seconds)
                )
                return None
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
            first_sent_at = row.created_at if row.calls > 0 else None
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
                    first_sent_at=first_sent_at,
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
                reason=PROVIDER_NOT_CONFIGURED,
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
                await self._record_attempt_error(
                    charge.attempt_id, request, f"{type(exc).__name__}: {exc}"
                )
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
                attempt.next_check_at = recheck
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

    async def _record_attempt_error(
        self, attempt_id: int, request: RenewalRequest, error: str
    ) -> None:
        core = self.core
        async with core.sessions() as session, session.begin():
            row = await session.get(ChargeAttempt, attempt_id, populate_existing=True)
            if row is None:  # pragma: no cover - rows are never deleted
                return
            row.last_error = error[:2000]
            row.updated_at = core.clock.now()
            if row.calls >= ALERT_AFTER_CALLS:
                await alert_owner(
                    session,
                    core,
                    key=f"owner:charge_errors:{request.idempotence_key}",
                    text=f"Списание за продление подписки #{request.subscription_id} "
                    f"({request.amount.human()}) не получило ответа {row.calls} раза подряд: "
                    f"{error[:300]}. Повторы идут с тем же ключом идемпотентности "
                    f"{request.idempotence_key}, двойного списания не будет.",
                )

    async def _give_up(
        self, session: AsyncSession, sub: Subscription, row: ChargeAttempt, now: datetime
    ) -> None:
        """The charge in ``row`` is still undecided at its deadline: stop waiting for it."""
        core = self.core
        write_state(sub, domain.charge_unsettled(state_of(sub), policy=core.policy), now=now)
        sub.claim_token, sub.claimed_until = None, None
        row.last_error = (row.last_error or "") + " | no answer by the deadline"
        row.updated_at = now
        log.error(
            "subscription %s: charge %s (%s) still undecided, access ends like after a failure",
            sub.id,
            row.idempotence_key,
            row.status.value,
        )
        await alert_owner(
            session,
            core,
            key=f"owner:unsettled:{row.idempotence_key}",
            text=f"Списание за продление подписки #{sub.id} (участник {sub.member.telegram_id}, "
            f"{price_of(sub).human()}) так и не получило ответа от {sub.provider} "
            f"(ключ {row.idempotence_key}). Новых попыток не будет, доступ закончится как "
            "после неудачного списания. Проверьте платёж в личном кабинете провайдера: если "
            "деньги списаны, придёт уведомление и подписка восстановится.",
        )

    # --- 2. pending charges ----------------------------------------------------------------

    async def _pending_charges(self, report: TickReport) -> None:
        core = self.core
        for _ in range(self.max_batches):
            now = core.clock.now()
            async with core.sessions() as session:
                ids = list(
                    (
                        await session.scalars(
                            select(ChargeAttempt.id)
                            .where(
                                ChargeAttempt.status == ChargeAttemptStatus.PENDING,
                                ChargeAttempt.provider_payment_id.is_not(None),
                                ChargeAttempt.next_check_at <= now,
                            )
                            .order_by(ChargeAttempt.next_check_at)
                            .limit(self.batch_size)
                        )
                    ).all()
                )
            if not ids:
                return
            for attempt_id in ids:
                try:
                    await self._check_pending(attempt_id, report)
                except Exception:
                    report.errors += 1
                    log.exception("checking pending charge %s failed", attempt_id)

    async def _check_pending(self, attempt_id: int, report: TickReport) -> None:
        core = self.core
        now = core.clock.now()
        recheck = now + timedelta(seconds=core.settings.pending_recheck_seconds)
        async with core.sessions() as session, session.begin():
            # Moving next_check_at is the claim: a second worker's update matches nothing.
            claimed = await session.execute(
                update(ChargeAttempt)
                .where(
                    ChargeAttempt.id == attempt_id,
                    ChargeAttempt.status == ChargeAttemptStatus.PENDING,
                    ChargeAttempt.next_check_at <= now,
                )
                .values(next_check_at=recheck)
            )
            if claimed.rowcount != 1:  # type: ignore[attr-defined]
                return
            row = await session.get(ChargeAttempt, attempt_id)
            assert row is not None
            assert row.provider_payment_id is not None
            sub = await session.get(Subscription, row.subscription_id)
            assert sub is not None
            provider_name, payment_id = sub.provider, row.provider_payment_id
        provider = core.providers.get(provider_name)
        if provider is None:
            log.warning("pending charge %s: provider %s is not enabled", payment_id, provider_name)
            return
        try:
            result = await provider.check_payment(payment_id)
        except Exception as exc:
            report.provider_errors += 1
            log.warning("checking pending charge %s: %s", payment_id, exc)
            return
        report.pending_checked += 1
        if result is None:
            # The provider cannot look it up; only its notification can settle it.
            async with core.sessions() as session, session.begin():
                await session.execute(
                    update(ChargeAttempt)
                    .where(ChargeAttempt.id == attempt_id)
                    .values(next_check_at=None)
                )
            return
        if isinstance(result, PaymentPending):
            return

        async def apply(session: AsyncSession) -> PaymentOutcome:
            return (await apply_payment_event(session, result, core)).outcome

        outcome = await in_transaction(core.sessions, apply)
        log.info("pending charge %s settled: %s", payment_id, outcome.value)
        if outcome is _APPLIED:
            if isinstance(result, PaymentFailed):
                report.charge_failed += 1
            else:
                report.renewed += 1

    async def _checkouts(self, report: TickReport) -> None:
        result = await reconcile_checkouts(
            self.core, batch_size=self.batch_size, max_batches=self.max_batches
        )
        report.checkouts_checked += result.checked
        report.checkouts_recovered += result.recovered
        report.errors += result.errors

    async def _stars_payments(self, report: TickReport) -> None:
        report.stars_applied += await retry_stored_payments(self.core, batch_size=self.batch_size)

    async def _ton_payments(self, report: TickReport) -> None:
        result = await poll_ton_wallet(self.core)
        report.ton_requests += result.requests
        report.ton_transfers += result.transfers
        report.ton_paid += result.paid
        report.provider_errors += result.errors

    # --- 3. expiry -------------------------------------------------------------------------

    async def _expire(self, report: TickReport) -> None:
        core = self.core
        provider_renewed = sorted(core.providers.provider_renewed())
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
                    Subscription.provider.not_in(provider_renewed),
                    Subscription.current_period_end <= now,
                ),
                # A provider-renewed one waits for a renewal the member may have turned back
                # on at the provider (see domain.payment_succeeded).
                and_(
                    Subscription.status == SubStatus.CANCELED,
                    Subscription.provider.in_(provider_renewed),
                    Subscription.current_period_end <= now - core.policy.renewal_wait,
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
            mode = core.providers.renewal_mode(sub.provider)
            wait = core.policy.renewal_wait if mode is RenewalMode.PROVIDER else timedelta(0)
            reason = domain.expiry_due(state_of(sub), now, renewal_wait=wait)
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

    # --- 4. access -------------------------------------------------------------------------

    async def _revoke(self, report: TickReport) -> None:
        result = await revoke_expired_access(
            self.core, self.revoker, batch_size=self.batch_size, max_batches=self.max_batches
        )
        report.removed += result.removed
        report.revoke_errors += result.errors

    # --- 5. reminders ----------------------------------------------------------------------

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
        provider_renewed = core.providers.renewal_mode(sub.provider) is RenewalMode.PROVIDER
        if sub.status is SubStatus.CANCELED:
            return texts.reminder_canceled(
                subscription_id=sub.id,
                period_end=end,
                offset=offset,
                tz=core.tz,
                provider_renewed=provider_renewed,
            )
        if provider_renewed:
            template = texts.reminder_stars
        elif sub.provider in MEMBER_RENEWED:
            template = texts.reminder_crypto
        elif sub.payment_method_id is None:
            template = texts.reminder_manual
        else:
            template = texts.reminder_auto_renew
        return template(
            subscription_id=sub.id, amount=price_of(sub), period_end=end, offset=offset, tz=core.tz
        )
