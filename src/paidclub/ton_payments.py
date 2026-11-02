"""USDT-on-TON checkouts: recording them, finding the transfers that pay them, applying them.

The provider (:mod:`paidclub.billing.ton`) only talks to toncenter. This module holds the
rules for the money:

* **An invoice per checkout** (:func:`open_invoice`): the price in the jetton's smallest units
  and a comment nobody else has. The member gets the page link only once it is stored.
* **One request per pass** (:func:`poll_ton_wallet`, a scheduler step). While an invoice is
  open, the owner's jetton wallet is read from where the last poll stopped, at most every
  ``PAIDCLUB_TON_POLL_SECONDS``. For :data:`LATE_WATCH` after an invoice was paid or expired,
  it is still read every :data:`LATE_POLL`, so a late or second transfer is found. The first
  poll of a wallet only asks the master for its address and pins it; reading starts on the
  next pass. Moving ``ton_wallets.polled_at`` is the claim, so two workers never poll at once.
* **Once per transaction.** Every credit is stored by its transaction hash before anything else
  happens (``ton_transfers.tx_hash`` is unique), in the same database transaction as what it
  causes, so a transfer read twice, by two workers or after a restart, counts once.
* **Added up by comment.** Transfers with an invoice's comment add up. Below the price the
  member is told how much arrived and how much is left, and the invoice stays open for another
  ``PAIDCLUB_TON_INVOICE_LIFETIME_MINUTES``. At the price or above it, the invoice is applied
  through :func:`paidclub.payments.apply_payment_event` as one payment of the price; more than
  the price is accepted, and a surplus of a cent or more is reported to the owner.
* **Late money still counts.** A transfer made after its invoice expired is added up and
  applied like any other; core decides what it pays for (a period paid meanwhile becomes
  ``needs_refund``), and the owner is told it came late. An invoice that expires partly paid
  is reported to the owner and the member. Money for an invoice already paid, and transfers
  whose comment matches no invoice, are reported to the owner: none of it is dropped silently.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from paidclub import texts
from paidclub.billing.base import PaymentPurpose, PaymentSucceeded, ProviderError
from paidclub.billing.ton import (
    IncomingTransfer,
    TonProvider,
    format_units,
    normalize_comment,
)
from paidclub.billing.tonaddress import TonAddress
from paidclub.container import Core
from paidclub.db import in_transaction, insert_ignore
from paidclub.models import PaymentOutcome, TonInvoice, TonInvoiceStatus, TonTransfer, TonWallet
from paidclub.money import Money
from paidclub.notifications import enqueue
from paidclub.payments import alert_owner, apply_payment_event

log = logging.getLogger(__name__)

#: Paid and expired invoices keep the wallet watched this long, so that a late transfer, or a
#: second one for a paid invoice, is still found and counted or reported.
LATE_WATCH = timedelta(days=7)
#: How often the wallet is read while nothing is open, only watched.
LATE_POLL = timedelta(minutes=5)
#: The very first read of a wallet starts this long before its oldest watched invoice.
FIRST_READ_MARGIN = timedelta(minutes=5)
#: Invoices expired per pass.
EXPIRE_BATCH = 100


@dataclass
class TonReport:
    #: Requests made to toncenter this pass: 0 or 1.
    requests: int = 0
    #: Credits stored for the first time, and invoices they completed and applied.
    transfers: int = 0
    paid: int = 0
    errors: int = 0


def ton_provider(core: Core) -> TonProvider | None:
    found = core.providers.get("ton")
    return found if isinstance(found, TonProvider) else None


def usdt(units: int, decimals: int) -> str:
    """``"9,5 USDT"``, written like the other amounts members read."""
    return f"{format_units(units, decimals).replace('.', ',')} USDT"


def _lifetime(core: Core) -> timedelta:
    return timedelta(minutes=core.settings.ton_invoice_lifetime_minutes)


async def open_invoice(
    core: Core,
    provider: TonProvider,
    *,
    subscription_id: int,
    for_period: int,
    comment: str,
    amount: Money,
) -> None:
    """Record the invoice a checkout page shows. Errors propagate: without the record the
    page does not exist and a transfer could not be matched, so the member gets no link."""
    code = normalize_comment(comment)
    if code is None:
        raise ValueError(f"not a usable comment: {comment!r}")
    now = core.clock.now()

    async def work(session: AsyncSession) -> None:
        session.add(
            TonInvoice(
                subscription_id=subscription_id,
                for_period=for_period,
                comment=code,
                amount_units=provider.units(amount),
                received_units=0,
                status=TonInvoiceStatus.OPEN,
                created_at=now,
                expires_at=now + _lifetime(core),
            )
        )

    await in_transaction(core.sessions, work)


async def find_invoice(core: Core, comment: str) -> TonInvoice | None:
    code = normalize_comment(comment)
    if code is None:
        return None
    async with core.sessions() as session:
        return (
            await session.scalars(select(TonInvoice).where(TonInvoice.comment == code))
        ).one_or_none()


# --- the scheduler step ------------------------------------------------------------------------


async def poll_ton_wallet(core: Core) -> TonReport:
    """Expire invoices, then, if anything is watched and the poll is due, read the wallet once."""
    report = TonReport()
    provider = ton_provider(core)
    if provider is None:
        return report
    now = core.clock.now()
    await _expire_invoices(core, provider, now)
    watched = await _watched(core, now)
    if watched is None:
        return report
    first, waiting = watched
    interval = timedelta(seconds=core.settings.ton_poll_seconds)
    wallet = await _claim_poll(core, provider, now, interval if waiting else LATE_POLL)
    if wallet is None:
        return report
    report.requests = 1
    try:
        if wallet.jetton_wallet is None:
            pinned = await provider.resolve_jetton_wallet()
            await _pin(core, wallet.id, pinned)
            return report  # one request per pass: the wallet is read from the next one
        batch = await provider.incoming(
            TonAddress.parse(wallet.jetton_wallet),
            after_lt=wallet.last_lt,
            since=first - FIRST_READ_MARGIN,
        )
    except ProviderError as exc:
        report.errors += 1
        log.warning("polling the TON wallet: %s", exc)
        await _alert(
            core,
            key=f"owner:ton_unavailable:{now:%Y-%m-%d}",
            text=f"Не удаётся проверить TON-кошелёк через toncenter: {str(exc)[:300]}. Пока это "
            "так, переводы USDT не находятся и подписки по ним не включаются.",
        )
        return report
    for transfer in batch.transfers:
        recorded, paid = await in_transaction(core.sessions, _recorder(core, provider, transfer))
        report.transfers += recorded
        report.paid += paid
    for tx_hash in batch.unreadable:
        await _alert(
            core,
            key=f"owner:ton_unreadable:{tx_hash}",
            text=f"На USDT-кошелёк клуба пришёл перевод, который не удалось разобрать "
            f"(транзакция {tx_hash}). Проверьте его вручную в обозревателе TON.",
        )
    if batch.last_lt is not None:
        await _advance(core, wallet.id, batch.last_lt)
    return report


async def _watched(core: Core, now: datetime) -> tuple[datetime, bool] | None:
    """When the oldest watched invoice was made and whether any is open; ``None`` if nothing
    is watched."""
    since = now - LATE_WATCH
    async with core.sessions() as session:
        oldest, open_count = (
            await session.execute(
                select(
                    func.min(TonInvoice.created_at),
                    func.count().filter(TonInvoice.status == TonInvoiceStatus.OPEN),
                ).where(
                    or_(
                        TonInvoice.status == TonInvoiceStatus.OPEN,
                        and_(
                            TonInvoice.status == TonInvoiceStatus.EXPIRED,
                            TonInvoice.expires_at > since,
                        ),
                        and_(
                            TonInvoice.status == TonInvoiceStatus.PAID, TonInvoice.paid_at > since
                        ),
                    )
                )
            )
        ).one()
    return None if oldest is None else (oldest, bool(open_count))


async def _claim_poll(
    core: Core, provider: TonProvider, now: datetime, interval: timedelta
) -> TonWallet | None:
    """The wallet row, if this worker may poll now; moving ``polled_at`` is the claim."""
    due = now - interval

    async def work(session: AsyncSession) -> TonWallet | None:
        await insert_ignore(
            session,
            TonWallet.__table__,  # type: ignore[arg-type]
            {
                "owner_address": provider.wallet.raw,
                "jetton_master": provider.jetton_master.raw,
                "created_at": now,
            },
            ["owner_address", "jetton_master"],
        )
        row = (
            await session.scalars(
                select(TonWallet).where(
                    TonWallet.owner_address == provider.wallet.raw,
                    TonWallet.jetton_master == provider.jetton_master.raw,
                )
            )
        ).one()
        claimed = await session.execute(
            update(TonWallet)
            .where(
                TonWallet.id == row.id,
                or_(TonWallet.polled_at.is_(None), TonWallet.polled_at <= due),
            )
            .values(polled_at=now)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:  # type: ignore[attr-defined]
            return None
        await session.refresh(row)
        return row

    return await in_transaction(core.sessions, work)


async def _pin(core: Core, wallet_id: int, jetton_wallet: TonAddress) -> None:
    async with core.sessions() as session, session.begin():
        await session.execute(
            update(TonWallet)
            .where(TonWallet.id == wallet_id, TonWallet.jetton_wallet.is_(None))
            .values(jetton_wallet=jetton_wallet.raw)
        )


async def _advance(core: Core, wallet_id: int, last_lt: int) -> None:
    async with core.sessions() as session, session.begin():
        await session.execute(
            update(TonWallet)
            .where(
                TonWallet.id == wallet_id,
                or_(TonWallet.last_lt.is_(None), TonWallet.last_lt < last_lt),
            )
            .values(last_lt=last_lt)
        )


async def _alert(core: Core, *, key: str, text: str) -> None:
    async def work(session: AsyncSession) -> None:
        await alert_owner(session, core, key=key, text=text)

    await in_transaction(core.sessions, work)


# --- one transfer ------------------------------------------------------------------------------


def _recorder(
    core: Core, provider: TonProvider, transfer: IncomingTransfer
) -> Callable[[AsyncSession], Awaitable[tuple[int, int]]]:
    """Store ``transfer`` and do what it means, in one database transaction. The work returns
    ``(1 if it was stored now, 1 if it completed an invoice that core applied)``."""

    async def work(session: AsyncSession) -> tuple[int, int]:
        now = core.clock.now()
        code = normalize_comment(transfer.comment)
        invoice = None
        if code is not None:
            invoice = (
                await session.scalars(
                    select(TonInvoice)
                    .where(TonInvoice.comment == code)
                    .execution_options(populate_existing=True)
                )
            ).one_or_none()
        stored = await insert_ignore(
            session,
            TonTransfer.__table__,  # type: ignore[arg-type]
            {
                "tx_hash": transfer.tx_hash,
                "lt": transfer.lt,
                "amount_units": transfer.amount,
                "comment": transfer.comment[:255] if transfer.comment else None,
                "sender": transfer.sender,
                "invoice_id": invoice.id if invoice is not None else None,
                "occurred_at": transfer.occurred_at,
                "created_at": now,
            },
            ["tx_hash"],
        )
        if not stored:
            return 0, 0
        amount = usdt(transfer.amount, provider.decimals)
        if invoice is None:
            said = f"с комментарием «{transfer.comment}»" if transfer.comment else "без комментария"
            await alert_owner(
                session,
                core,
                key=f"owner:ton_unmatched:{transfer.tx_hash}",
                text=f"На TON-кошелёк клуба пришло {amount} {said}, и ни один счёт этого не "
                f"ждал (транзакция {transfer.tx_hash}, отправитель {transfer.sender or '—'}). "
                "Верните деньги или продлите подписку вручную.",
            )
            return 1, 0
        paid = await _count(session, core, provider, invoice, transfer, now)
        return 1, int(paid)

    return work


async def _count(
    session: AsyncSession,
    core: Core,
    provider: TonProvider,
    invoice: TonInvoice,
    transfer: IncomingTransfer,
    now: datetime,
) -> bool:
    """Add ``transfer`` to ``invoice``; returns whether it completed it and core applied it."""
    decimals = provider.decimals
    sub = invoice.subscription
    received = invoice.received_units + transfer.amount
    invoice.received_units = received
    total, got = usdt(invoice.amount_units, decimals), usdt(received, decimals)
    ref = f"Счёт {invoice.comment} (подписка #{sub.id}, участник {sub.member.telegram_id})"

    if invoice.status is TonInvoiceStatus.PAID:
        await alert_owner(
            session,
            core,
            key=f"owner:ton_extra:{transfer.tx_hash}",
            text=f"{ref} уже оплачен, а пришло ещё {usdt(transfer.amount, decimals)} "
            f"(транзакция {transfer.tx_hash}). Верните лишнее.",
        )
        return False
    if transfer.occurred_at > invoice.expires_at:
        await alert_owner(
            session,
            core,
            key=f"owner:ton_late:{transfer.tx_hash}",
            text=f"{ref}: перевод {usdt(transfer.amount, decimals)} пришёл после того, как "
            f"счёт истёк. Он засчитан: всего пришло {got} из {total}.",
        )

    if received < invoice.amount_units:
        left = usdt(invoice.amount_units - received, decimals)
        invoice.status = TonInvoiceStatus.OPEN
        invoice.expires_at = max(invoice.expires_at, now + _lifetime(core))
        await enqueue(
            session,
            key=f"ton_underpaid:{invoice.id}:{received}",
            kind="ton_underpaid",
            chat_id=sub.member.telegram_id,
            member_id=sub.member_id,
            message=texts.ton_underpaid(
                received=got,
                total=total,
                left=left,
                comment=invoice.comment,
                page_url=provider.page_url(invoice.comment),
            ),
            now=now,
        )
        return False

    event = PaymentSucceeded(
        provider=provider.name,
        provider_payment_id=invoice.comment,
        subscription_id=invoice.subscription_id,
        amount=Money(invoice.amount_units // provider.units_per_cent, "USD"),
        occurred_at=transfer.occurred_at,
        purpose=PaymentPurpose.CHECKOUT,
        for_period=invoice.for_period,
    )
    result = await apply_payment_event(session, event, core)
    invoice.status = TonInvoiceStatus.PAID
    invoice.paid_at = now
    invoice.outcome = result.outcome.value
    surplus = received - invoice.amount_units
    if surplus:
        log.info("%s overpaid by %s", invoice.comment, usdt(surplus, decimals))
    if surplus >= provider.units_per_cent:
        await alert_owner(
            session,
            core,
            key=f"owner:ton_overpaid:{invoice.id}",
            text=f"{ref} оплачен с переплатой: пришло {got} вместо {total}. Переплата "
            f"{usdt(surplus, decimals)} осталась на кошельке, при необходимости верните её.",
        )
    return result.outcome is PaymentOutcome.APPLIED


# --- expiry ------------------------------------------------------------------------------------


async def _expire_invoices(core: Core, provider: TonProvider, now: datetime) -> None:
    async with core.sessions() as session:
        ids = list(
            (
                await session.scalars(
                    select(TonInvoice.id)
                    .where(
                        TonInvoice.status == TonInvoiceStatus.OPEN,
                        TonInvoice.expires_at <= now,
                    )
                    .order_by(TonInvoice.expires_at)
                    .limit(EXPIRE_BATCH)
                )
            ).all()
        )
    for invoice_id in ids:

        async def work(session: AsyncSession, invoice_id: int = invoice_id) -> None:
            invoice = await session.get(TonInvoice, invoice_id, populate_existing=True)
            if invoice is None or invoice.status is not TonInvoiceStatus.OPEN:
                return
            if invoice.expires_at > now:
                return
            invoice.status = TonInvoiceStatus.EXPIRED
            if not invoice.received_units:
                return
            sub, decimals = invoice.subscription, provider.decimals
            got, total = (
                usdt(invoice.received_units, decimals),
                usdt(invoice.amount_units, decimals),
            )
            left = usdt(invoice.amount_units - invoice.received_units, decimals)
            await alert_owner(
                session,
                core,
                key=f"owner:ton_partial:{invoice.id}",
                text=f"Счёт {invoice.comment} (подписка #{sub.id}, участник "
                f"{sub.member.telegram_id}) истёк оплаченным не полностью: пришло {got} из "
                f"{total}. Доплата с тем же комментарием ещё засчитается; если её не будет, "
                "верните деньги.",
            )
            await enqueue(
                session,
                key=f"ton_partial_expired:{invoice.id}",
                kind="ton_partial_expired",
                chat_id=sub.member.telegram_id,
                member_id=sub.member_id,
                message=texts.ton_partial_expired(
                    received=got,
                    total=total,
                    left=left,
                    comment=invoice.comment,
                    page_url=provider.page_url(invoice.comment),
                ),
                now=now,
            )

        await in_transaction(core.sessions, work)
