"""Checkouts whose payment the provider created up front, looked up until the provider decides.

A first payment is applied when the provider's notification arrives. Notifications can be
lost for good: a wrong notification URL in the shop settings, the app or its proxy down for
longer than the provider retries (24 hours at YooKassa), a TLS problem. The member would
have paid and never get in, and nothing in the database would say so.

So every checkout with a provider payment id is recorded when the member gets the link
(:func:`track_checkout`), and the scheduler looks the payment up
(:meth:`~paidclub.billing.base.BillingProvider.check_payment`): five minutes after it was
created, then every 15 minutes for two hours, then hourly, until the provider decides or
:data:`GIVE_UP_AFTER` has passed. A decided payment goes through
:func:`paidclub.payments.apply_payment_event` like a notification would, so whichever comes
first applies it and the other is a duplicate. When the lookup is what applied a successful
payment, its notification did not come, and the owner is told to check the notification URL.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from paidclub.billing.base import PaymentPending, PaymentSucceeded
from paidclub.container import Core
from paidclub.db import in_transaction, insert_ignore
from paidclub.models import Checkout, PaymentOutcome
from paidclub.payments import alert_owner, apply_payment_event

log = logging.getLogger(__name__)

#: The first lookup: a member who pays at once has been applied by the notification by then.
FIRST_CHECK = timedelta(minutes=5)
#: Lookups stop after this long; YooKassa cancels an unpaid payment, and a Crypto Pay invoice
#: expires (``PAIDCLUB_CRYPTOPAY_INVOICE_LIFETIME_MINUTES``), well before.
GIVE_UP_AFTER = timedelta(hours=48)


def next_check_in(age: timedelta) -> timedelta:
    """Every 15 minutes while the checkout is young, hourly later."""
    return timedelta(minutes=15) if age < timedelta(hours=2) else timedelta(hours=1)


async def track_checkout(
    core: Core,
    *,
    subscription_id: int,
    provider: str,
    provider_payment_id: str,
    for_period: int,
) -> None:
    """Record a checkout the member was sent to, so it is looked up if nothing arrives."""
    now = core.clock.now()

    async def work(session: AsyncSession) -> None:
        await insert_ignore(
            session,
            Checkout.__table__,  # type: ignore[arg-type]
            {
                "subscription_id": subscription_id,
                "provider": provider,
                "provider_payment_id": provider_payment_id,
                "for_period": for_period,
                "next_check_at": now + FIRST_CHECK,
                "checks": 0,
                "created_at": now,
            },
            ["provider", "provider_payment_id"],
        )

    await in_transaction(core.sessions, work)


@dataclass
class CheckoutReport:
    checked: int = 0
    #: Applied by a lookup: the notification never came.
    recovered: int = 0
    errors: int = 0


async def reconcile_checkouts(
    core: Core, *, batch_size: int = 50, max_batches: int = 100
) -> CheckoutReport:
    report = CheckoutReport()
    for _ in range(max_batches):
        now = core.clock.now()
        async with core.sessions() as session:
            ids = list(
                (
                    await session.scalars(
                        select(Checkout.id)
                        .where(Checkout.next_check_at <= now)
                        .order_by(Checkout.next_check_at)
                        .limit(batch_size)
                    )
                ).all()
            )
        if not ids:
            break
        for checkout_id in ids:
            try:
                await _check(core, checkout_id, report)
            except Exception:
                report.errors += 1
                log.exception("looking up checkout %s failed", checkout_id)
    return report


async def _check(core: Core, checkout_id: int, report: CheckoutReport) -> None:
    now = core.clock.now()
    async with core.sessions() as session, session.begin():
        row = await session.get(Checkout, checkout_id)
        if row is None or row.next_check_at is None or row.next_check_at > now:
            return
        age = now - row.created_at
        # The last lookup marks it abandoned up front; a decided payment overwrites that.
        last = age >= GIVE_UP_AFTER
        values: dict[str, object] = {"checks": Checkout.checks + 1}
        if last:
            values.update(next_check_at=None, result="abandoned", settled_at=now)
        else:
            values["next_check_at"] = now + next_check_in(age)
        # Moving next_check_at is the claim: a second worker's update matches nothing.
        claimed = await session.execute(
            update(Checkout)
            .where(Checkout.id == checkout_id, Checkout.next_check_at == row.next_check_at)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:  # type: ignore[attr-defined]
            return
        provider_name, payment_id = row.provider, row.provider_payment_id

    provider = core.providers.get(provider_name)
    if provider is None:
        return
    try:
        result = await provider.check_payment(payment_id)
    except Exception as exc:
        report.errors += 1
        log.warning("looking up %s payment %s: %s", provider_name, payment_id, exc)
        return
    report.checked += 1
    if isinstance(result, PaymentPending):
        return

    async def apply(session: AsyncSession) -> PaymentOutcome | None:
        row = await session.get(Checkout, checkout_id, populate_existing=True)
        assert row is not None
        if result is None:
            # The provider cannot look this payment up (the fake checkout page sends its own
            # notification); there is nothing more to do here.
            _finish(row, "untracked", core.clock.now())
            return None
        outcome = (await apply_payment_event(session, result, core)).outcome
        _finish(row, outcome.value, core.clock.now())
        if outcome is not PaymentOutcome.DUPLICATE and isinstance(result, PaymentSucceeded):
            await alert_owner(
                session,
                core,
                key=f"owner:no_notification:{provider_name}:{core.clock.now():%Y-%m-%d}",
                text=f"Уведомление от {provider_name} о платеже {payment_id} не пришло, платёж "
                "нашёлся проверкой статуса. Проверьте адрес уведомлений в настройках магазина: "
                f"{core.settings.public_base_url}/webhooks/{provider_name}.",
            )
        return outcome

    outcome = await in_transaction(core.sessions, apply)
    if outcome is not None and outcome is not PaymentOutcome.DUPLICATE:
        report.recovered += 1
        log.warning(
            "checkout %s/%s applied by a lookup (%s): its notification did not arrive",
            provider_name,
            payment_id,
            outcome.value,
        )


def _finish(row: Checkout, result: str, now: datetime) -> None:
    row.next_check_at = None
    row.result = result
    row.settled_at = now
